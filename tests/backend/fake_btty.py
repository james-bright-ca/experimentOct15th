"""A fake ``btty`` runner for BttyBackend tests. It never runs the real btty.

``FakeBtty(root)`` is a ``Runner``: it takes one btty argv and returns
``(exit code, stdout, stderr)``, as the real CLI would. The "remote" is the
local filesystem below ``root``. It emulates what btty.py's docstring documents
(and bcli src/btty/cli.ts, copy.ts and exec.ts implement):

btty's informational line
    Every call first prints ``INFO_LINE`` (``btty: <container-id> on <host>``,
    the form the user observed live) with a CRLF, on ``info="stderr"`` (where
    bcli writes it) or ``info="stdout"``; ``info=None`` leaves it out. It is
    printed even with ``--json``, before the JSON.
``--put <local> --to <path>``
    The local file is written to ``<path>.btty-tmp`` and moved onto ``<path>``
    with rename semantics, as btty's ``mv "$t" "$p"`` does (so a directory, or
    a symlink to one, at ``<path>`` receives the temporary file inside it). A
    missing parent fails with ``btty: <path>: no such directory: <parent>``;
    an existing file is replaced silently. Success prints
    ``btty: <n> bytes -> <path>`` on stderr.
``--get <path> --out <out>``
    An existing ``--out`` directory receives the file under the remote name;
    any other existing ``--out`` is refused (``already exists -- refusing to
    overwrite``). An unreadable remote path fails with ``btty: <path>: ...``.
    Whether each ``--out`` already existed is recorded in ``out_paths``.
``--exec <script>`` with ``--yes --no-launch --allow-unrestricted --json --timeout N``
    A script over ``btty_scripts.COMMAND_BUDGET`` UTF-8 bytes is rejected
    (exit 1, ``btty: that command makes a <n> byte line, over the ...``) and
    recorded in ``rejected``; it never runs. Otherwise the script runs with
    ``/bin/sh -c`` (stdin closed, stdout and stderr merged, ``python3`` on
    PATH), its output gets the pty's CRLF line ends, and stdout is btty's
    result object ``{"stdout": ..., "stderr": "", "exit_code": n}`` on one
    line; btty exits with ``n``. (Real btty refuses a script ``bash -n``
    rejects; here a syntax error shows as a script that did not finish.)

Every argv is checked: ``argv[0]`` must be ``command`` and ``argv[1]``
``--yes``, and the rest must be one of the three shapes in btty.py's
docstring. A malformed argv raises AssertionError, which fails the test.

Script shapes (``script_kind``, ``script_paths``, ``script_names``)
    Each script is ``unalias -a 2>/dev/null`` then one of:

    ``python3 -I -c <program> <op> <path>... [<name>...]``
        ``l``, ``w``, ``s``: list, walk, stat ``<path>``.
        ``p``: pack ``<archive> <parent> <name>...``.
        ``f``: pack ``<archive> <parent> <names file>``, the names read from
        the NUL-separated remote file (kind ``pack-from-file``).
        ``u``: unpack ``<archive> <dest>``.
        ``m``: merge ``<src> <dst>``.
    ``p=<path>;...``: mkdir. ``s=<src>;d=<dst>;...``: rename, or copy when
    it runs ``cp -a``. ``...for p in <path>...;do``: delete.

The timeouts
    ``timeout`` is the ``--timeout`` every exec argv must carry, except that
    the archive scripts (pack, unpack, merge: ``ARCHIVE_KINDS``) must carry
    ``archive_timeout``. None (the default for each) checks only that the
    value is a positive whole number.

The remote's python3 (``python``, ``tar_filter``)
    ``python3`` is ``python`` (default: ``$TWINFM_FAKE_REMOTE_PYTHON`` if set,
    else the interpreter running the tests), so the same suite can be run
    against another Python version. With ``tar_filter=False`` it is that
    interpreter with ``tarfile``'s extraction filters (``tar_filter``,
    ``data_filter``, ``fully_trusted_filter``) removed before the program
    runs, as on a remote Python older than 3.12 (or 3.11.4); the script text
    itself is unchanged.

Counting calls
    ``calls`` records every argv and ``kinds`` the kind of each call, in the
    same order: ``put``, ``get`` or a script kind (``list``, ``walk``,
    ``stat``, ``mkdir``, ``delete``, ``rename``, ``copy``, ``pack``,
    ``pack-from-file``, ``unpack``, ``merge``; ``?`` for an argv it refused).
    ``count(kind...)`` and ``calls_of(kind...)`` select by kind. Calls from
    several threads are recorded in pairs; the scripts themselves run
    concurrently, as on the real remote.

Safety: every remote path a call names must lie inside ``root``. For
``--exec`` the paths are read back from the script the way ``sh`` would read
them (``script_paths``); a path outside ``root``, a pack name that is not a
single path component (inline or in the names file), or a script whose shape
the fake does not know, raises AssertionError before anything runs. So a
broken backend cannot touch the rest of the disk. Archive mode packs a remote
selection in ``Settings.remote_tmp``, so that directory must lie inside
``root`` too: ``settings_for`` builds Settings that match a fake and checks
it.

``respond(code, stdout, stderr)`` queues a canned answer for the next call
(after the argv check, and with the info line added); nothing runs for it.

The scripts' shell finds ``btty`` through the same PATH as the tests, so the
conftest guard shim stands in for it there too.
"""

from __future__ import annotations

import atexit
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from collections.abc import Sequence
from functools import cache

from twinfm.backend.btty_scripts import COMMAND_BUDGET
from twinfm.config import Settings

__all__ = [
    "ARCHIVE_KINDS",
    "EXEC_FLAGS",
    "INFO_LINE",
    "PYTHON_ENV",
    "SHELL",
    "FakeBtty",
    "run_script",
    "script_env",
    "script_kind",
    "script_names",
    "script_paths",
    "settings_for",
]

INFO_LINE = "btty: 6a35ed517a03 on brisk-beta-uswe2-wmp-elastic-apps-vscode-0"
"""btty's informational first line, as the user observed it live."""

SHELL = "/bin/sh"
"""The shell the fake runs scripts with (the remote's sh is dash; btty evals in bash)."""

EXEC_FLAGS = ["--yes", "--no-launch", "--allow-unrestricted", "--json", "--timeout"]
"""What every --exec argv carries after the command, before the timeout value."""

ARCHIVE_KINDS = frozenset({"pack", "pack-from-file", "unpack", "merge"})
"""The script kinds that carry ``Settings.btty_archive_timeout``."""

PYTHON_ENV = "TWINFM_FAKE_REMOTE_PYTHON"
"""Names the interpreter the fake remote's ``python3`` runs, when set."""

_HEAD = "unalias -a 2>/dev/null\n"

_PYTHON_KINDS = {
    "l": "list",
    "w": "walk",
    "s": "stat",
    "p": "pack",
    "f": "pack-from-file",
    "u": "unpack",
    "m": "merge",
}
_PYTHON_PATHS = {"l": 1, "w": 1, "s": 1, "p": 2, "f": 3, "u": 2, "m": 2}
"""How many absolute paths follow each python3 op (pack's names come after them)."""

# python3 with tarfile's extraction filters removed, run as
# `<python> -I <this file> -I -c <program> <args>...`.
_WITHOUT_TAR_FILTER = """\
import sys, tarfile
for name in ("tar_filter", "data_filter", "fully_trusted_filter"):
    if hasattr(tarfile, name):
        delattr(tarfile, name)
args = sys.argv[1:]
if args[:1] == ["-I"]:
    args = args[1:]
if args[:1] != ["-c"] or len(args) < 2:
    sys.exit("fake python3 without tar_filter: only `python3 [-I] -c <program>` is emulated")
sys.argv = ["-c", *args[2:]]
exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
"""


def _default_python() -> str:
    return os.environ.get(PYTHON_ENV) or sys.executable


@cache
def _python_dir(python: str, tar_filter: bool) -> str:
    """A directory holding only ``python3``: a symlink to ``python`` (its base
    installation; the remote programs use only the standard library and run
    with ``-I``), or with ``tar_filter=False`` a wrapper that removes
    tarfile's extraction filters first."""
    real = os.path.realpath(shutil.which(python) or python)
    assert os.access(real, os.X_OK), f"fake btty: no python3 interpreter at {python!r}"
    path = tempfile.mkdtemp(prefix="twinfm-fake-btty-python-")
    atexit.register(shutil.rmtree, path, True)
    target = os.path.join(path, "python3")
    if tar_filter:
        os.symlink(real, target)
    else:
        shim = os.path.join(path, "without_tar_filter.py")
        with open(shim, "w") as f:
            f.write(_WITHOUT_TAR_FILTER)
        with open(target, "w") as f:
            f.write(f'#!/bin/sh\nexec {shlex.quote(real)} -I {shlex.quote(shim)} "$@"\n')
        os.chmod(target, 0o755)
    return path


def script_env(*, python: str | None = None, tar_filter: bool = True) -> dict[str, str]:
    """The environment scripts run in: the current one, with ``python3`` first
    on PATH. The rest of PATH is kept, so ``btty`` still resolves to the
    conftest guard shim."""
    env = dict(os.environ)
    first = _python_dir(python or _default_python(), tar_filter)
    env["PATH"] = os.pathsep.join([first, env.get("PATH") or os.defpath])
    return env


def run_script(
    script: str,
    *,
    shell: str = SHELL,
    cwd: str = "/",
    python: str | None = None,
    tar_filter: bool = True,
) -> tuple[int, str]:
    """Run ``script`` with ``shell -c`` as btty's remote shell would:
    (exit status, stdout and stderr merged). ``cwd`` defaults to ``/``, so a
    script that used a relative path could not write anywhere. ``python`` and
    ``tar_filter`` choose the remote's ``python3`` (see the module docstring)."""
    done = subprocess.run(
        [shell, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=script_env(python=python, tar_filter=tar_filter),
        cwd=cwd,
        timeout=120,
        check=False,
    )
    return done.returncode, done.stdout.decode("utf-8", "replace")


def _words(text: str) -> shlex.shlex:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return lexer


def _body(script: str) -> str:
    assert script.startswith(_HEAD), f"fake btty: unknown script shape: {script[:80]!r}"
    return script[len(_HEAD) :]


def _python_words(body: str) -> tuple[str, list[str], list[str]]:
    """(op, paths, names) of a ``python3 -I -c <program> <op> ...`` body."""
    words = shlex.split(body)
    assert words[:3] == ["python3", "-I", "-c"] and len(words) >= 6, words[:3]
    op = words[4]
    assert op in _PYTHON_KINDS, f"fake btty: unknown python3 op {op!r}"
    count = _PYTHON_PATHS[op]
    paths, names = words[5 : 5 + count], words[5 + count :]
    assert len(paths) == count, (op, words[4:])
    assert op == "p" or not names, f"fake btty: op {op} takes {count} path(s), got {words[5:]!r}"
    return op, paths, names


def script_kind(script: str) -> str:
    """What a ``btty_scripts`` script does (see the module docstring).
    AssertionError for a script of unknown shape."""
    body = _body(script)
    if body.startswith("python3 "):
        return _PYTHON_KINDS[_python_words(body)[0]]
    if body.startswith("p="):
        return "mkdir"
    if body.startswith("s="):
        return "copy" if 'cp -a "$s"' in body else "rename"
    if "\nfor p in " in body:
        return "delete"
    raise AssertionError(f"fake btty: unknown script shape: {script[:80]!r}")


def script_names(script: str) -> list[str]:
    """The names an inline pack script (op ``p``) packs; [] for any other script."""
    body = _body(script)
    if body.startswith("python3 "):
        op, _, names = _python_words(body)
        return names if op == "p" else []
    return []


def script_paths(script: str) -> list[str]:
    """The paths a ``btty_scripts`` script names, read back as ``sh`` reads
    its quoting (pack's names, which are relative, are not paths). AssertionError
    for a script of unknown shape."""
    body = _body(script)
    if body.startswith("python3 "):
        return _python_words(body)[1]
    if body.startswith("p="):  # mkdir: p=<path>;q=...
        return [_words(body[2:]).get_token() or ""]
    if body.startswith("s="):  # rename, copy: s=<src>;d=<dst>;o=...
        lexer = _words(body[2:])
        src, semi, dst = lexer.get_token(), lexer.get_token(), lexer.get_token()
        assert semi == ";" and dst and dst.startswith("d="), (src, semi, dst)
        return [src or "", dst[2:]]
    marker = "\nfor p in "
    if marker in body:  # delete: for p in <path> <path>...;do
        lexer = _words(body[body.index(marker) + len(marker) :])
        paths = []
        while (word := lexer.get_token()) != ";":
            assert word is not None, "fake btty: unterminated path list"
            paths.append(word)
        return paths
    raise AssertionError(f"fake btty: unknown script shape: {script[:80]!r}")


def _check_name(name: str) -> None:
    assert name not in ("", ".", "..") and "/" not in name and "\0" not in name, (
        f"fake btty: pack name {name!r} is not a single path component; refusing"
    )


class FakeBtty:
    """A btty stand-in over the local directory ``root``. See the module docstring.

    calls
        Every argv received, in order.
    kinds
        The kind of each of ``calls``: ``put``, ``get`` or the script kind.
    rejected
        The --exec scripts refused for exceeding ``COMMAND_BUDGET``.
    out_paths
        ``(--out path, whether it existed at call time)`` for each --get.
    tar_filter
        Whether the remote python3 has tarfile's extraction filters; may be
        changed between calls.
    """

    def __init__(
        self,
        root: str,
        *,
        command: str = "btty",
        timeout: int | None = None,
        archive_timeout: int | None = None,
        info: str | None = "stderr",
        crlf: bool = True,
        python: str | None = None,
        tar_filter: bool = True,
    ) -> None:
        assert root.startswith("/") and posixpath.normpath(root) == root, root
        assert info in ("stdout", "stderr", None), info
        self.root = root
        self.command = command
        self.timeout = timeout
        self.archive_timeout = archive_timeout
        self.info = info
        self.crlf = crlf
        self.python = python
        self.tar_filter = tar_filter
        self.calls: list[list[str]] = []
        self.kinds: list[str] = []
        self.rejected: list[str] = []
        self.out_paths: list[tuple[str, bool]] = []
        self._canned: deque[tuple[int, str, str]] = deque()
        self._lock = threading.Lock()

    # -- what the tests look at -------------------------------------------------------

    def respond(self, code: int, stdout: str = "", stderr: str = "") -> None:
        """Answer the next call with this instead of emulating it."""
        self._canned.append((code, stdout, stderr))

    def of_kind(self, flag: str) -> list[list[str]]:
        """The recorded calls whose action is ``flag`` (--put, --get or --exec)."""
        return [argv for argv in self.calls if flag in argv[1:3] or argv[7:8] == [flag]]

    def count(self, *kinds: str) -> int:
        """How many calls were of any of ``kinds`` (``put``, ``get``, ``list``...)."""
        return sum(1 for kind in self.kinds if kind in kinds)

    def calls_of(self, *kinds: str) -> list[list[str]]:
        """The argv of every call of any of ``kinds``, in order."""
        return [argv for argv, kind in zip(self.calls, self.kinds, strict=True) if kind in kinds]

    @property
    def scripts(self) -> list[str]:
        """The script of every --exec call, in order."""
        return [argv[8] for argv in self.of_kind("--exec")]

    # -- the runner ---------------------------------------------------------------------

    def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        argv = list(argv)
        try:
            kind = self._check_argv(argv)
        except AssertionError:
            with self._lock:
                self.calls.append(argv)
                self.kinds.append("?")
            raise
        with self._lock:  # calls and kinds stay paired when threads share the fake
            self.calls.append(argv)
            self.kinds.append(kind)
            canned = self._canned.popleft() if self._canned else None
        if canned is not None:
            return self._answer(*canned)
        if kind == "put":
            return self._put(argv[3], argv[5])
        if kind == "get":
            return self._get(argv[3], argv[5])
        return self._exec(argv[8])

    def _check_argv(self, argv: list[str]) -> str:
        """The kind of a well-formed argv; AssertionError otherwise."""
        assert all(isinstance(a, str) for a in argv), f"fake btty: non-str argv {argv!r}"
        assert argv[:2] == [self.command, "--yes"], f"fake btty: bad argv start {argv!r}"
        if len(argv) == 6 and argv[2] == "--put" and argv[4] == "--to":
            assert argv[5].startswith("/"), f"fake btty: relative --to {argv!r}"
            return "put"
        if len(argv) == 6 and argv[2] == "--get" and argv[4] == "--out":
            assert argv[3].startswith("/"), f"fake btty: relative --get {argv!r}"
            return "get"
        assert len(argv) == 9 and argv[1:6] == EXEC_FLAGS and argv[7] == "--exec", (
            f"fake btty: unexpected argv {argv!r}"
        )
        assert argv[6].isdigit() and int(argv[6]) > 0, f"fake btty: bad --timeout {argv!r}"
        kind = script_kind(argv[8])
        if kind in ARCHIVE_KINDS:
            if self.archive_timeout is not None:
                assert argv[6] == str(self.archive_timeout), (
                    f"fake btty: {kind} --timeout {argv[6]} != archive timeout {self.archive_timeout}"
                )
        elif self.timeout is not None:
            assert argv[6] == str(self.timeout), f"fake btty: --timeout {argv[6]} != {self.timeout}"
        return kind

    def _inside(self, path: str) -> None:
        path = posixpath.normpath(path)
        assert path == self.root or path.startswith(self.root + "/"), (
            f"fake btty: {path!r} is outside the fake remote {self.root!r}; refusing"
        )

    def _answer(self, code: int, stdout: str, stderr: str) -> tuple[int, str, str]:
        if self.info == "stdout":
            stdout = f"{INFO_LINE}\r\n{stdout}"
        elif self.info == "stderr":
            stderr = f"{INFO_LINE}\r\n{stderr}"
        return code, stdout, stderr

    def _fail(self, message: str) -> tuple[int, str, str]:
        return self._answer(1, "", f"btty: {message}\n")

    # -- --put / --get --------------------------------------------------------------------

    def _put(self, local: str, path: str) -> tuple[int, str, str]:
        self._inside(path)
        try:
            with open(local, "rb") as f:
                data = f.read()
        except OSError as e:
            return self._fail(f"cannot read {local}: {e.strerror}")
        parent = posixpath.dirname(path)
        if not os.path.isdir(parent):
            return self._fail(f"{path}: no such directory: {parent}")
        tmp = path + ".btty-tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            # mv "$t" "$p": into the directory when $p is one (a symlink is followed).
            dest = posixpath.join(path, posixpath.basename(tmp)) if os.path.isdir(path) else path
            os.rename(tmp, dest)
        except OSError:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            return self._fail(f"{path}: wrote -1 of {len(data)} bytes to {path}")
        return self._answer(0, "", f"btty: {len(data)} bytes -> {path}\n")

    def _get(self, path: str, out: str) -> tuple[int, str, str]:
        self._inside(path)
        self.out_paths.append((out, os.path.lexists(out)))
        dest = out
        if os.path.isdir(dest):
            dest = os.path.join(dest, posixpath.basename(path))
        if os.path.exists(dest):
            return self._fail(f"{dest} already exists -- refusing to overwrite")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except IsADirectoryError:
            return self._fail(f"{path}: wc: 'standard input': Is a directory")
        except OSError as e:
            return self._fail(f"{path}: sh: 1: cannot open {path}: {e.strerror}")
        with open(dest, "wb") as f:
            f.write(data)
        return self._answer(0, "", f"btty: {len(data)} bytes -> {dest}\n")

    # -- --exec -----------------------------------------------------------------------------

    def _check_names(self, script: str, kind: str, paths: list[str]) -> None:
        for name in script_names(script):
            _check_name(name)
        if kind == "pack-from-file":
            try:
                with open(paths[2], "rb") as f:
                    listed = f.read()
            except OSError:
                return  # the program reports the missing file itself
            for raw in listed.split(b"\0"):
                if raw:
                    _check_name(raw.decode("utf-8", "surrogateescape"))

    def _exec(self, script: str) -> tuple[int, str, str]:
        size = len(script.encode("utf-8", "surrogateescape"))
        if size > COMMAND_BUDGET:
            self.rejected.append(script)
            return self._fail(
                f"that command makes a {size} byte line, over the {COMMAND_BUDGET} limit -- "
                "it is typed into a shell in canonical mode, where anything longer is "
                "silently truncated"
            )
        paths = script_paths(script)
        for path in paths:
            self._inside(path)
        self._check_names(script, script_kind(script), paths)
        status, output = run_script(script, python=self.python, tar_filter=self.tar_filter)
        if self.crlf:
            output = output.replace("\n", "\r\n")
        result = json.dumps({"stdout": output, "stderr": "", "exit_code": status})
        return self._answer(status, result + "\n", "")


def settings_for(fake: FakeBtty, home: str, tmp: str, **overrides: object) -> Settings:
    """Exec-mode Settings that match ``fake``: its command, its timeouts (the
    Settings defaults where the fake checks none), ``home`` as remote_home and
    ``tmp`` as remote_tmp. Both must lie inside the fake's root; neither is
    created. ``overrides`` are further Settings fields."""
    for path in (home, tmp):
        assert path.startswith("/") and posixpath.normpath(path) == path, path
        fake._inside(path)
    defaults = Settings()
    values: dict[str, object] = {
        "remote_home": home,
        "remote_tmp": tmp,
        "btty_command": fake.command,
        "btty_exec": True,
        "btty_timeout": fake.timeout if fake.timeout is not None else defaults.btty_timeout,
        "btty_archive_timeout": (
            fake.archive_timeout
            if fake.archive_timeout is not None
            else defaults.btty_archive_timeout
        ),
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]
