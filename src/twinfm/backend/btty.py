"""BttyBackend: the remote Beacon IDE workspace, reached through the ``btty`` CLI.

Modes (``Settings.btty_exec``)
    Exec (the default): every cap. Listing, stat, mkdir, delete, rename and
    copy run scripts (``btty_scripts``) through ``btty --exec
    --allow-unrestricted``, which lets the app run arbitrary commands in the
    Beacon IDE. btty has no file-listing command of its own (``--list`` lists
    containers, and btty-mcp only runs commands), so a script over ``--exec``
    is the way to list. Listing, walk and stat run a ``python3`` program that
    prints JSON records (the user verified python3 and ``--exec --json``
    live); the mutations run ``sh``.
    Transfer-only (the opt-out: ``exec = false`` or ``--no-btty-exec``): caps
    are {UPLOAD, DOWNLOAD}. Only btty's native single-file ``--put`` and
    ``--get`` are used. Without STAT nothing can check whether a remote file
    exists, so ``upload`` needs ``overwrite=True`` (``--put`` silently
    replaces a file, as ``bscp`` does) and raises Unsupported otherwise.

Paths
    ``home()`` is ``Settings.remote_home``. A relative path is resolved
    against it, so btty only ever sees absolute paths and never applies its
    own ``.../<you>/default`` resolution.

The runner
    ``runner(argv) -> (exit code, stdout, stderr)`` runs one btty command.
    The default, ``run_btty``, uses ``subprocess.run`` with stdin closed and
    ``NODE_OPTIONS=""`` in the child's environment only, because btty fails
    with the user's NODE_OPTIONS. Tests inject a fake runner instead.

argv shapes (``cmd`` is ``Settings.btty_command``)
    upload   ``[cmd, "--yes", "--put", <local file>, "--to", <absolute remote path>]``
    download ``[cmd, "--yes", "--get", <absolute remote path>, "--out", <new local path>]``
             The ``--out`` path is a name inside a fresh temporary directory, so
             it never exists. The file is then installed at ``local_dest``.
    exec     ``[cmd, "--yes", "--no-launch", "--allow-unrestricted", "--json",
             "--timeout", str(Settings.btty_timeout), "--exec", <script>]``
             Each script is at most ``btty_scripts.COMMAND_BUDGET`` UTF-8 bytes.

What the runner must return (btty's own behaviour, bcli src/btty/cli.ts)
    * Every call, ``--json`` or not, may first print btty's informational line
      ``btty: <container-id> on <host>`` (observed live:
      ``btty: 6a35ed517a03 on brisk-beta-uswe2-wmp-elastic-apps-vscode-0``),
      on stdout or on stderr; which one is unverified. That line is never data
      and never an error by itself, and it is left out of error messages.
    * ``--put`` / ``--get`` success: exit 0. Other ``btty: ...`` lines are ignored.
    * ``--exec --json``: btty's result object,
      ``{"stdout": <remote stdout and stderr merged>, "stderr": "", "exit_code": <int>}``,
      and btty exits with ``exit_code``. The object is found by scanning
      stdout, then stderr, for the last JSON object with a string ``stdout``
      and an integer ``exit_code``; text around it (the informational line,
      for instance) is skipped. The status comes from the object, not from
      btty's exit code. The script's records (see ``btty_scripts``) decide
      the result.
    * A btty-level failure: a non-zero exit (1), no result object, and a
      ``btty: <message>`` line (continuation lines may follow), looked for on
      stderr first, then stdout. A ``--put`` or ``--get`` failure about the
      file itself is prefixed with the remote path: a put into a missing
      directory says ``btty: <remote path>: no such directory: <parent>``.

Error mapping for btty-level failures, by the message
    * A message prefixed with the remote path: NotFound for ``no such
      directory``, BackendUnavailable for a dropped transfer (``the remote
      went quiet``, ``the connection dropped``, ``cut short``...),
      OperationFailed for anything else. Only the text after the path counts.
    * Otherwise: OperationFailed for a rejected script (``not valid shell
      syntax``, over the line limit); Unsupported when btty refused to run it
      (``refusing to run in``, ``--exec requires --user``); NotFound for ``no
      such directory``; BackendUnavailable for authentication, credentials,
      network, no container, timeouts, dropped connections and an unrunnable
      btty (e.g. ``no containers found``, ``timed out after``); else
      OperationFailed carrying the message.
    In exec mode, a failed download is classified by a stat of the remote
    path (NotFound, IsADirectory) when the message does not say why.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from twinfm.backend.base import (
    ALL_CAPS,
    AlreadyExists,
    Backend,
    BackendUnavailable,
    Cap,
    Entry,
    FsError,
    IsADirectory,
    NotFound,
    OperationFailed,
    Unsupported,
    from_oserror,
)
from twinfm.backend.btty_scripts import (
    ScriptOutput,
    copy_script,
    delete_scripts,
    error_for,
    list_script,
    mkdir_script,
    parse_output,
    rename_script,
    stat_script,
    to_entry,
    walk_script,
)
from twinfm.backend.local import install_file
from twinfm.config import Settings

__all__ = ["TRANSFER_CAPS", "BttyBackend", "Runner", "open_btty", "run_btty"]

Runner = Callable[[list[str]], tuple[int, str, str]]
"""Runs one btty argv and returns (exit code, stdout, stderr)."""

TRANSFER_CAPS: frozenset[Cap] = frozenset({Cap.UPLOAD, Cap.DOWNLOAD})
"""The caps of a BttyBackend without exec mode."""

_INFO = re.compile(r"btty: \S+ on \S+")
"""btty's informational first line, ``btty: <container-id> on <host>``."""

_FAILED = ("not valid shell syntax", "byte line, over the", "byte command line, over the")
_UNSUPPORTED = ("refusing to run in", "requires --user")
_DROPPED = (
    "the remote went quiet",
    "the connection dropped",
    "cut short",
    "the remote sent nothing back",
    "the remote never acknowledged",
    "the payload went up",
)
_UNAVAILABLE = (
    "containers found",
    "container found",
    "container appeared",
    "not in container",
    "cannot reach",
    "cannot open ws",
    "discovery socket",
    ".beacon",
    "credentials",
    "client-id",
    "token",
    "jwt",
    "not authorized",
    "failed to start",
    "app-proxy",
    "timed out",
    "went quiet",
    "went silent",
    "connection",
    "sent nothing back",
    "never acknowledged",
    "sent no result",
    "cut short",
    "cannot run",
)


def run_btty(argv: list[str]) -> tuple[int, str, str]:
    """The default runner: run ``argv`` with stdin closed and the current
    environment, except that NODE_OPTIONS is the empty string (as the user's
    ``bssh="NODE_OPTIONS='' btty"`` alias does). ``os.environ`` itself is never
    changed, and PATH is inherited, so ``btty`` is found through PATH. A
    command that cannot be started returns 127 and a ``btty: cannot run``
    message."""
    env = os.environ.copy()
    env["NODE_OPTIONS"] = ""
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
    except OSError as e:
        return 127, "", f"btty: cannot run {argv[0]}: {e.strerror or e}"
    return done.returncode, done.stdout or "", done.stderr or ""


def _lines(text: str) -> list[str]:
    """The non-empty lines of ``text``, without btty's informational line."""
    lines = (line.strip() for line in text.replace("\r", "").split("\n"))
    return [line for line in lines if line and not _INFO.fullmatch(line)]


def _btty_message(code: int, stdout: str, stderr: str) -> str:
    """The reason btty gave: its last ``btty: `` line and anything after it,
    from stderr or else stdout; otherwise everything it printed."""
    err, out = _lines(stderr), _lines(stdout)
    for lines in (err, out):
        starts = [i for i, line in enumerate(lines) if line.startswith("btty: ")]
        if starts:
            first = starts[-1]
            return " ".join([lines[first][len("btty: ") :], *lines[first + 1 :]])[:500]
    return " ".join(err + out)[:500] or f"btty exited with status {code}"


def _classify(message: str, path: str) -> type[FsError]:
    label = f"{path}: "
    if message.startswith(label):
        # btty prefixes a --put or --get failure with the remote path; look only
        # past it, so that words in the path cannot decide the class.
        detail = message[len(label) :].lower()
        if detail.startswith("no such directory"):
            return NotFound
        if detail.startswith(_DROPPED):
            return BackendUnavailable
        return OperationFailed
    text = message.lower()
    if any(p in text for p in _FAILED):
        return OperationFailed
    if any(p in text for p in _UNSUPPORTED):
        return Unsupported
    if "no such directory" in text:
        return NotFound
    if any(p in text for p in _UNAVAILABLE):
        return BackendUnavailable
    return OperationFailed


def _json_result(*streams: str) -> tuple[str, int] | None:
    """(remote output, exit_code) from btty's ``--json`` result object: the
    last JSON object with a string ``stdout`` and an integer ``exit_code`` in
    the first of ``streams`` that has one. Text around it is skipped."""
    decoder = json.JSONDecoder()
    for text in streams:
        found: tuple[str, int] | None = None
        start = text.find("{")
        while start != -1:
            try:
                data, end = decoder.raw_decode(text, start)
            except ValueError:
                start = text.find("{", start + 1)
                continue
            if isinstance(data, dict):
                output, status = data.get("stdout"), data.get("exit_code")
                if isinstance(output, str) and isinstance(status, int) and not isinstance(status, bool):
                    found = output, status
            start = text.find("{", end)
        if found is not None:
            return found
    return None


class BttyBackend(Backend):
    """The remote workspace over btty. Construction does no I/O. Stateless
    apart from its settings and runner, so it is thread-safe.

    Beyond the Backend contract it offers ``delete_many``, which deletes many
    paths in as few btty calls as the command budget allows.
    """

    id = "btty"
    label = "btty"

    def __init__(self, settings: Settings, runner: Runner | None = None) -> None:
        self.settings = settings
        self.runner: Runner = runner if runner is not None else run_btty
        self.caps = ALL_CAPS if settings.btty_exec else TRANSFER_CAPS

    # -- running btty -------------------------------------------------------------

    def _abs(self, path: str) -> str:
        if "\0" in path:
            raise OperationFailed(path, "a path cannot contain a NUL byte", self.id)
        # normpath keeps a POSIX leading "//"; remote paths start with exactly one "/".
        return "/" + posixpath.normpath(posixpath.join(self.settings.remote_home, path)).lstrip("/")

    def _failure(self, path: str, code: int, stdout: str, stderr: str) -> FsError:
        if code == 0:
            return OperationFailed(path, "btty returned no result", self.id)
        message = _btty_message(code, stdout, stderr)
        return _classify(message, path)(path, message, self.id)

    def _transfer(self, argv: list[str], path: str) -> None:
        code, stdout, stderr = self.runner(argv)
        if code != 0:
            raise self._failure(path, code, stdout, stderr)

    def _script(self, script: str, path: str) -> ScriptOutput:
        """Run ``script`` with ``btty --exec``; raise what it reports, if anything."""
        s = self.settings
        argv = [
            s.btty_command,
            "--yes",
            "--no-launch",
            "--allow-unrestricted",
            "--json",
            "--timeout",
            str(s.btty_timeout),
            "--exec",
            script,
        ]
        code, stdout, stderr = self.runner(argv)
        result = _json_result(stdout, stderr)
        if result is None:
            raise self._failure(path, code, stdout, stderr)
        output = parse_output(result[0])
        error = error_for(output, path, self.id, result[1])
        if error is not None:
            raise error
        return output

    @contextmanager
    def _local(self, path: str) -> Iterator[None]:
        """Re-raise local-filesystem failures inside the block as FsError about ``path``."""
        try:
            yield
        except OSError as e:
            raise from_oserror(e, path, self.id) from e
        except ValueError as e:  # e.g. a NUL byte in a path
            raise OperationFailed(path, str(e), self.id, e) from e

    # -- path algebra -------------------------------------------------------------

    def home(self) -> str:
        return self.settings.remote_home

    # -- queries ------------------------------------------------------------------

    def list_dir(self, path: str) -> list[Entry]:
        self.require(Cap.LIST, path)
        path = self._abs(path)
        return [to_entry(r, path) for r in self._script(list_script(path), path).entries]

    def walk(self, path: str) -> list[Entry]:
        self.require(Cap.LIST, path)
        path = self._abs(path)
        return [to_entry(r, path) for r in self._script(walk_script(path), path).entries]

    def stat(self, path: str) -> Entry:
        self.require(Cap.STAT, path)
        path = self._abs(path)
        entries = self._script(stat_script(path), path).entries
        if not entries:
            raise NotFound(path, backend_id=self.id)
        r = entries[0]
        return Entry(
            name=posixpath.basename(path) or "/",
            path=path,
            kind=r.kind,
            size=r.size,
            mtime=r.mtime,
            mode=r.mode,
            link_target=r.link_target,
            target_kind=r.target_kind,
        )

    # -- same-backend mutations ---------------------------------------------------

    def mkdir(self, path: str) -> None:
        self.require(Cap.MKDIR, path)
        path = self._abs(path)
        self._script(mkdir_script(path), path)

    def delete(self, path: str, recursive: bool = False) -> None:
        self.require(Cap.DELETE, path)
        self._delete([path], recursive)

    def delete_many(self, paths: Sequence[str], recursive: bool = False) -> None:
        """``delete`` for each of ``paths``, in order, in as few btty calls as
        the command budget allows. Stops at the first failure and raises it;
        what was already removed stays removed. Every path is checked (root,
        length) before anything is deleted."""
        self.require(Cap.DELETE, paths[0] if paths else self.home())
        self._delete(paths, recursive)

    def _delete(self, paths: Sequence[str], recursive: bool) -> None:
        targets = [self._abs(p) for p in paths]
        if "/" in targets:
            raise OperationFailed("/", "refusing to delete the root directory", self.id)
        for chunk, script in delete_scripts(targets, recursive):
            self._script(script, chunk[0])

    def rename(self, src: str, dst: str, overwrite: bool = False) -> None:
        self.require(Cap.RENAME, src)
        src, dst = self._abs(src), self._abs(dst)
        self._script(rename_script(src, dst, overwrite), src)

    def copy_within(self, src: str, dst: str, overwrite: bool = False) -> None:
        self.require(Cap.COPY_WITHIN, src)
        src, dst = self._abs(src), self._abs(dst)
        self._script(copy_script(src, dst, overwrite), src)

    # -- cross-backend primitives -------------------------------------------------

    def download(self, path: str, local_dest: str) -> None:
        self.require(Cap.DOWNLOAD, path)
        path = self._abs(path)
        if os.path.lexists(local_dest):
            raise AlreadyExists(local_dest, backend_id=self.id)
        if not os.path.isdir(os.path.dirname(os.path.abspath(local_dest))):
            raise NotFound(local_dest, "parent directory does not exist", self.id)
        with self._local(local_dest):
            staging = tempfile.mkdtemp(prefix="twinfm-btty-")
        try:
            fetched = os.path.join(staging, "download")
            try:
                self._transfer(
                    [self.settings.btty_command, "--yes", "--get", path, "--out", fetched], path
                )
            except (BackendUnavailable, Unsupported):
                raise
            except FsError as err:
                if Cap.STAT in self.caps:
                    self._explain_download(path, err)
                raise
            with self._local(local_dest), open(fetched, "rb") as src:
                install_file(local_dest, lambda f: shutil.copyfileobj(src, f))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _explain_download(self, path: str, err: FsError) -> None:
        """Raise NotFound or IsADirectory if that is why ``--get`` failed."""
        try:
            entry = self.stat(path)
        except NotFound as missing:
            raise missing from err
        if entry.kind == "dir" or entry.target_kind == "dir":
            raise IsADirectory(path, backend_id=self.id) from err

    def upload(self, local_src: str, path: str, overwrite: bool = False) -> None:
        self.require(Cap.UPLOAD, path)
        path = self._abs(path)
        exec_mode = Cap.STAT in self.caps
        if not overwrite and not exec_mode:
            raise Unsupported(
                path,
                "without btty exec mode a remote file cannot be checked, so upload must overwrite",
                self.id,
            )
        with self._local(local_src):
            st = os.stat(local_src)
        if stat.S_ISDIR(st.st_mode):
            raise IsADirectory(local_src, backend_id=self.id)
        if not stat.S_ISREG(st.st_mode):
            raise OperationFailed(local_src, "not a regular file", self.id)
        if path == "/":
            raise IsADirectory(path, "will not replace a directory", self.id)
        if exec_mode:
            try:
                existing: Entry | None = self.stat(path)
            except NotFound:
                existing = None
            if existing is not None:
                if not overwrite:
                    raise AlreadyExists(path, backend_id=self.id)
                if existing.kind == "dir":
                    raise IsADirectory(path, "will not replace a directory", self.id)
                if existing.kind == "symlink":
                    # --put moves its temporary file onto the path with `mv`, which
                    # would land inside the directory a symlink points to.
                    self.delete(path)
        self._transfer(
            [self.settings.btty_command, "--yes", "--put", local_src, "--to", path], path
        )


_shared: dict[Settings, BttyBackend] = {}
_shared_lock = threading.Lock()


def open_btty(settings: Settings) -> BttyBackend:
    """The one BttyBackend for ``settings``, created on first use with the
    default runner. A registry factory: two calls with equal Settings return
    the same object, so the engine treats them as one store. No I/O."""
    with _shared_lock:
        backend = _shared.get(settings)
        if backend is None:
            backend = _shared[settings] = BttyBackend(settings)
        return backend
