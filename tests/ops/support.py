"""Helpers for the engine tests (tests/ops).

* ``Answers`` is a scripted ``Callbacks``: it gives queued answers to
  ``ask_conflict`` / ``ask_error``, records every question, every Progress
  and every ``cancelled()`` poll, and can run a probe whenever it is asked.
* ``ArchiveFakeBtty`` is tests/backend/fake_btty.FakeBtty plus its own
  handling of the archive script shape (``python3 -I -c <program>
  <p|f|u|m> <path>... [<name>...]``), which it runs as the real remote
  would, after the same "inside the fake root" check; it was written before
  the shared fake knew that shape and keeps these tests independent of it.
  ``no_tar_filter`` makes it answer pack and unpack with ``@X NOFILTER``.
  ``FAKES`` also holds the shared FakeBtty whenever that one runs the
  archive scripts itself; ``remote_without_tar_filter`` works on both.
* ``faulty(cls)`` subclasses a real backend with per-method hooks that may
  raise an FsError or observe the store when the engine calls a method.
* ``World`` is one local directory plus one fake remote (with its
  ``remote_home`` and ``remote_tmp``) and a BttyBackend in exec mode over it.
* ``tree`` / ``modes`` / ``leftovers`` / ``summary`` observe the outcome
  directly on the local filesystem, never through the engine.

Nothing here runs the real btty: every BttyBackend gets a fake runner, and
the conftest guard shim is still first on PATH.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.backend import fake_btty
from tests.backend.fake_btty import FakeBtty
from twinfm.backend.base import Backend, Entry, FsError, OperationFailed
from twinfm.backend.btty import BttyBackend
from twinfm.backend.btty_scripts import COMMAND_BUDGET, pack_script
from twinfm.backend.local import LocalBackend
from twinfm.config import Settings
from twinfm.ops.model import Conflict, OnError, Progress, Report

TIMEOUT = 41
"""btty_timeout in every World, so a --timeout of the archive scripts stands out."""

ARCHIVE_TIMEOUT = 43
"""btty_archive_timeout in every World."""

STAGES = ["Packing", "Transferring", "Unpacking", "Merging"]
"""The archive-mode stages, in the order Progress.stage must report them."""

TEMP_PREFIX = ".twinfm-"
"""What every archive-mode temporary inside a store's directories is named with."""


# -- callbacks ------------------------------------------------------------------------


class Answers:
    """A scripted Callbacks. ``conflicts`` / ``errors`` are answered in
    order, then ``conflict`` / ``error`` for every later question.
    ``cancel_when(self)`` decides each ``cancelled()`` poll. ``probe(kind,
    path)`` runs inside every question, before it is answered."""

    def __init__(
        self,
        conflicts: Iterable[Conflict] = (),
        *,
        errors: Iterable[OnError] = (),
        conflict: Conflict = Conflict.OVERWRITE,
        error: OnError = OnError.SKIP,
        cancel_when: Callable[[Answers], bool] | None = None,
        probe: Callable[[str, str], None] | None = None,
    ) -> None:
        self._conflicts = list(conflicts)
        self._errors = list(errors)
        self.conflict = conflict
        self.error = error
        self.cancel_when = cancel_when
        self.probe = probe
        self.conflicts_asked: list[tuple[str, str]] = []  # (src.path, dst.path)
        self.errors_asked: list[tuple[str, FsError]] = []
        self.progress_seen: list[Progress] = []
        self.polls = 0

    def ask_conflict(self, src: Entry, dst: Entry) -> Conflict:
        self.conflicts_asked.append((src.path, dst.path))
        if self.probe is not None:
            self.probe("conflict", src.path)
        return self._conflicts.pop(0) if self._conflicts else self.conflict

    def ask_error(self, path: str, err: FsError) -> OnError:
        self.errors_asked.append((path, err))
        if self.probe is not None:
            self.probe("error", path)
        return self._errors.pop(0) if self._errors else self.error

    def progress(self, p: Progress) -> None:
        self.progress_seen.append(p)

    def cancelled(self) -> bool:
        self.polls += 1
        return bool(self.cancel_when is not None and self.cancel_when(self))

    @property
    def stages(self) -> list[str]:
        """Every Progress.stage reported, in order (None entries left out)."""
        return [p.stage for p in self.progress_seen if p.stage is not None]


def after_stage(name: str) -> Callable[[Answers], bool]:
    """A ``cancel_when``: True from the first poll after stage ``name`` was reported."""
    return lambda answers: name in answers.stages


# -- the fake remote --------------------------------------------------------------------


_ARCHIVE_OPS = {"p": 2, "f": 3, "u": 2, "m": 2}
"""Archive program op -> how many absolute paths follow it (pack's names come after)."""


def archive_op(script: str) -> tuple[str, list[str], list[str]] | None:
    """(op, paths, names) of a btty_scripts archive script, else None."""
    head = "unalias -a 2>/dev/null\n"
    if not script.startswith(head):
        return None
    try:
        words = shlex.split(script[len(head) :])
    except ValueError:
        return None
    if words[:3] != ["python3", "-I", "-c"] or len(words) < 5 or words[4] not in _ARCHIVE_OPS:
        return None
    count = _ARCHIVE_OPS[words[4]]
    return words[4], words[5 : 5 + count], words[5 + count :]


def ops_of(fake: FakeBtty, since: int = 0) -> list[str]:
    """The archive ops (p, f, u, m) of the --exec calls from call ``since`` on."""
    ops = []
    for argv in fake.calls[since:]:
        if len(argv) == 9 and argv[7] == "--exec":
            found = archive_op(argv[8])
            if found is not None:
                ops.append(found[0])
    return ops


def puts_of(fake: FakeBtty, since: int = 0) -> list[str]:
    """The remote --to path of every --put from call ``since`` on."""
    return [argv[5] for argv in fake.calls[since:] if argv[2:3] == ["--put"]]


def gets_of(fake: FakeBtty, since: int = 0) -> list[str]:
    """The remote path of every --get from call ``since`` on."""
    return [argv[3] for argv in fake.calls[since:] if argv[2:3] == ["--get"]]


class ArchiveFakeBtty(FakeBtty):
    """FakeBtty that also runs the archive programs. Every path an archive
    script names (and, for pack, every packed entry) must lie inside the fake
    root, or AssertionError is raised before anything runs."""

    def __init__(self, root: str, **kw: Any) -> None:
        super().__init__(root, **kw)
        self.no_tar_filter = False

    def _exec(self, script: str) -> tuple[int, str, str]:
        found = archive_op(script)
        if found is None or len(script.encode("utf-8", "surrogateescape")) > COMMAND_BUDGET:
            return super()._exec(script)  # also refuses an over-budget script
        op, paths, names = found
        for path in paths:
            self._inside(path)
        if op == "p":
            for name in names:
                assert name not in ("", ".", "..") and "/" not in name, name
                self._inside(os.path.join(paths[1], name))
        if self.no_tar_filter and op in ("p", "f", "u"):
            if op == "f" and os.path.lexists(paths[2]):
                os.remove(paths[2])  # the program reads and removes the names file first
            status, output = 0, "@X NOFILTER \n"
        else:
            status, output = fake_btty.run_script(script)
        if self.crlf:
            output = output.replace("\n", "\r\n")
        result = json.dumps({"stdout": output, "stderr": "", "exit_code": status})
        return self._answer(status, result + "\n", "")


def remote_without_tar_filter(fake: FakeBtty) -> None:
    """Make the fake remote's python3 one without ``tarfile.tar_filter``:
    ArchiveFakeBtty answers @X NOFILTER itself; the shared FakeBtty runs the
    real programs on an interpreter whose filters were removed."""
    if isinstance(fake, ArchiveFakeBtty):
        fake.no_tar_filter = True
    else:
        fake.tar_filter = False  # type: ignore[attr-defined]


def shared_fake_runs_archive_scripts() -> bool:
    """Whether tests/backend/fake_btty.FakeBtty knows the archive script shape itself."""
    try:
        fake_btty.script_paths(pack_script("/r/p", ["n"], "/r/a.tgz") or "")
    except AssertionError:
        return False
    return True


FAKES: list[type[FakeBtty]] = [ArchiveFakeBtty]
if shared_fake_runs_archive_scripts():
    FAKES.append(FakeBtty)


# -- fault injection --------------------------------------------------------------------

Hook = Callable[..., FsError | None]

_FAULTABLE = (
    "list_dir",
    "walk",
    "stat",
    "mkdir",
    "delete",
    "delete_many",
    "upload",
    "download",
    "pack",
    "unpack",
    "merge",
)

_faulty_classes: dict[type[Backend], type[Backend]] = {}


def faulty[B: Backend](cls: type[B]) -> type[B]:
    """A subclass of the real backend ``cls`` whose instances take
    ``faults={method: hook}`` (keyword-only). Before each call of a listed
    method the call is appended to ``log`` as ``(method, args)`` and
    ``hook(*args, **kwargs)`` runs; an FsError it returns is raised instead
    of calling the real method."""
    if cls in _faulty_classes:
        return _faulty_classes[cls]  # type: ignore[return-value]

    def make(name: str) -> Callable[..., Any]:
        real = getattr(cls, name)

        def method(self: Any, *args: Any, **kwargs: Any) -> Any:
            self.log.append((name, args))
            hook = self.faults.get(name)
            if hook is not None:
                err = hook(*args, **kwargs)
                if err is not None:
                    raise err
            return real(self, *args, **kwargs)

        method.__name__ = name
        return method

    def __init__(self: Any, *args: Any, faults: dict[str, Hook] | None = None, **kwargs: Any) -> None:
        cls.__init__(self, *args, **kwargs)
        self.faults = dict(faults or {})
        self.log = []

    namespace: dict[str, Any] = {name: make(name) for name in _FAULTABLE}
    namespace["__init__"] = __init__
    sub = type(f"Faulty{cls.__name__}", (cls,), namespace)
    _faulty_classes[cls] = sub
    return sub  # type: ignore[return-value]


def fail(
    kind: type[FsError] = OperationFailed,
    *,
    when: Callable[..., bool] = lambda *a, **k: True,
    times: int | None = None,
    message: str = "injected fault",
) -> Hook:
    """A hook raising ``kind`` about the call's first argument whenever
    ``when(*args)`` holds, at most ``times`` times (None: always)."""
    count = 0

    def hook(*args: Any, **kwargs: Any) -> FsError | None:
        nonlocal count
        if not when(*args, **kwargs) or (times is not None and count >= times):
            return None
        count += 1
        return kind(str(args[0]) if args else "?", message, "test")

    return hook


def named(prefix: str) -> Callable[..., bool]:
    """A ``when``: the call's first argument's last component starts with ``prefix``."""
    return lambda *args, **kw: bool(args) and os.path.basename(str(args[0])).startswith(prefix)


def is_tgz(*args: Any, **kw: Any) -> bool:
    """A ``when`` for a call whose arguments name a .twinfm-*.tgz archive."""
    return any(
        isinstance(a, str) and os.path.basename(a).startswith(TEMP_PREFIX) and a.endswith(".tgz")
        for a in args
    )


# -- worlds -----------------------------------------------------------------------------


@dataclass
class World:
    """One local directory and one fake remote, all below ``base``."""

    base: str
    local: str
    remote: str
    home: str
    rtmp: str
    fake: FakeBtty
    btty: BttyBackend
    lb: LocalBackend
    settings: Settings
    extra: dict[str, Any] = field(default_factory=dict)

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.base)

    def lpath(self, rel: str) -> str:
        return os.path.join(self.local, rel)

    def rpath(self, rel: str) -> str:
        return os.path.join(self.home, rel)


def make_world(
    base: Path | str,
    *,
    fake_cls: type[FakeBtty] = ArchiveFakeBtty,
    btty_cls: type[BttyBackend] = BttyBackend,
    local_cls: type[LocalBackend] = LocalBackend,
    btty_faults: dict[str, Hook] | None = None,
    local_faults: dict[str, Hook] | None = None,
    btty_exec: bool = True,
    **settings: Any,
) -> World:
    """A fresh World in the new directory ``base`` (resolved: macOS /var is a symlink)."""
    root = Path(base)
    root.mkdir(parents=True)
    root = root.resolve()
    local, remote = root / "local", root / "remote"
    home, rtmp = remote / "home", remote / "tmp"
    for d in (local, home, rtmp):
        d.mkdir(parents=True)
    fake = fake_cls(str(remote), timeout=TIMEOUT, archive_timeout=ARCHIVE_TIMEOUT)
    cfg = Settings(
        remote_home=str(home),
        remote_tmp=str(rtmp),
        btty_exec=btty_exec,
        btty_timeout=TIMEOUT,
        btty_archive_timeout=ARCHIVE_TIMEOUT,
        **settings,
    )
    if btty_faults is not None:
        btty: BttyBackend = faulty(btty_cls)(cfg, runner=fake, faults=btty_faults)  # type: ignore[call-arg]
    else:
        btty = btty_cls(cfg, runner=fake)
    lb = faulty(local_cls)(faults=local_faults) if local_faults is not None else local_cls()  # type: ignore[call-arg]
    return World(str(root), str(local), str(remote), str(home), str(rtmp), fake, btty, lb, cfg)


# -- seeding and observing the local filesystem -------------------------------------------


def write(path: str, data: bytes = b"", mode: int | None = None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_bytes(data)
    if mode is not None:
        os.chmod(path, mode)
    return path


def symlink(path: str, target: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.symlink(target, path)
    return path


Node = tuple[str, bytes | str | None]


def tree(root: str) -> dict[str, Node]:
    """Every path below ``root`` (lstat; symlinks not followed), relative to
    it: ("dir", None), ("file", data), ("symlink", link text) or ("other",
    None) for a fifo, socket or device, which is never opened;
    ("unreadable", None) for an entry that cannot be looked at (inside a
    directory without search permission). {} if ``root`` does not exist."""
    result: dict[str, Node] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            try:
                mode = os.lstat(path).st_mode
            except PermissionError:
                result[rel] = ("unreadable", None)
                continue
            if stat.S_ISLNK(mode):
                result[rel] = ("symlink", os.readlink(path))
            elif stat.S_ISDIR(mode):
                result[rel] = ("dir", None)
            elif stat.S_ISREG(mode):
                result[rel] = ("file", Path(path).read_bytes())
            else:
                result[rel] = ("other", None)
    return result


def modes(root: str) -> dict[str, int]:
    """The permission bits of every file and directory below ``root``."""
    result: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if not stat.S_ISLNK(st.st_mode):
                result[os.path.relpath(path, root)] = stat.S_IMODE(st.st_mode)
    return result


def leftovers(*roots: str) -> list[str]:
    """Every path below ``roots`` whose name starts with ``.twinfm-``."""
    found = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            found += [os.path.join(dirpath, n) for n in dirnames + filenames if n.startswith(TEMP_PREFIX)]
    return sorted(found)


def summary(report: Report, base: str) -> dict[str, Any]:
    """``report`` with its paths relative to ``base`` and each error reduced
    to its class, so that two Worlds' Reports compare."""

    def rel(path: str) -> str:
        return os.path.relpath(path, base)

    return {
        "done": sorted(rel(p) for p in report.done),
        "skipped": sorted(rel(p) for p in report.skipped),
        "failed": sorted((rel(p), type(e).__name__) for p, e in report.failed),
        "cancelled": report.cancelled,
    }


def no_duplicates(report: Report) -> bool:
    failed = [(p, type(e), str(e)) for p, e in report.failed]
    return len(set(report.skipped)) == len(report.skipped) and len(set(failed)) == len(failed)


def relative(pairs: Sequence[tuple[str, Any]], base: str) -> list[str]:
    """The first element of each pair, relative to ``base`` (question logs)."""
    return [os.path.relpath(p[0], base) for p in pairs]
