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

Safety: every remote path a call names must lie inside ``root``. For
``--exec`` the paths are read back from the script the way ``sh`` would read
them (``script_paths``); a path outside ``root``, or a script whose shape the
fake does not know, raises AssertionError before anything runs. So a broken
backend cannot touch the rest of the disk.

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
from collections import deque
from collections.abc import Sequence
from functools import cache

from twinfm.backend.btty_scripts import COMMAND_BUDGET

__all__ = [
    "EXEC_FLAGS",
    "INFO_LINE",
    "SHELL",
    "FakeBtty",
    "run_script",
    "script_env",
    "script_paths",
]

INFO_LINE = "btty: 6a35ed517a03 on brisk-beta-uswe2-wmp-elastic-apps-vscode-0"
"""btty's informational first line, as the user observed it live."""

SHELL = "/bin/sh"
"""The shell the fake runs scripts with (the remote's sh is dash; btty evals in bash)."""

EXEC_FLAGS = ["--yes", "--no-launch", "--allow-unrestricted", "--json", "--timeout"]
"""What every --exec argv carries after the command, before the timeout value."""

_HEAD = "unalias -a 2>/dev/null\n"


@cache
def _python_dir() -> str:
    """A directory holding only ``python3``: a symlink to the interpreter
    running the tests (its base installation; the remote program uses only
    the standard library and runs with ``-I``)."""
    path = tempfile.mkdtemp(prefix="twinfm-fake-btty-python-")
    atexit.register(shutil.rmtree, path, True)
    os.symlink(os.path.realpath(sys.executable), os.path.join(path, "python3"))
    return path


def script_env() -> dict[str, str]:
    """The environment scripts run in: the current one, with ``python3`` first
    on PATH. The rest of PATH is kept, so ``btty`` still resolves to the
    conftest guard shim."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([_python_dir(), env.get("PATH") or os.defpath])
    return env


def run_script(script: str, *, shell: str = SHELL, cwd: str = "/") -> tuple[int, str]:
    """Run ``script`` with ``shell -c`` as btty's remote shell would:
    (exit status, stdout and stderr merged). ``cwd`` defaults to ``/``, so a
    script that used a relative path could not write anywhere."""
    done = subprocess.run(
        [shell, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=script_env(),
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


def script_paths(script: str) -> list[str]:
    """The paths a ``btty_scripts`` script names, read back as ``sh`` reads
    its quoting. AssertionError for a script of unknown shape."""
    assert script.startswith(_HEAD), f"fake btty: unknown script shape: {script[:80]!r}"
    body = script[len(_HEAD) :]
    if body.startswith("python3 "):  # list, walk, stat: python3 -I -c <program> <op> <path>
        words = shlex.split(body)
        assert words[:3] == ["python3", "-I", "-c"] and len(words) == 6, words[:3]
        return [words[5]]
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


class FakeBtty:
    """A btty stand-in over the local directory ``root``. See the module docstring.

    calls
        Every argv received, in order.
    rejected
        The --exec scripts refused for exceeding ``COMMAND_BUDGET``.
    out_paths
        ``(--out path, whether it existed at call time)`` for each --get.
    """

    def __init__(
        self,
        root: str,
        *,
        command: str = "btty",
        timeout: int | None = None,
        info: str | None = "stderr",
        crlf: bool = True,
    ) -> None:
        assert root.startswith("/") and posixpath.normpath(root) == root, root
        assert info in ("stdout", "stderr", None), info
        self.root = root
        self.command = command
        self.timeout = timeout
        self.info = info
        self.crlf = crlf
        self.calls: list[list[str]] = []
        self.rejected: list[str] = []
        self.out_paths: list[tuple[str, bool]] = []
        self._canned: deque[tuple[int, str, str]] = deque()

    # -- what the tests look at -------------------------------------------------------

    def respond(self, code: int, stdout: str = "", stderr: str = "") -> None:
        """Answer the next call with this instead of emulating it."""
        self._canned.append((code, stdout, stderr))

    def of_kind(self, flag: str) -> list[list[str]]:
        """The recorded calls whose action is ``flag`` (--put, --get or --exec)."""
        return [argv for argv in self.calls if flag in argv[1:3] or argv[7:8] == [flag]]

    @property
    def scripts(self) -> list[str]:
        """The script of every --exec call, in order."""
        return [argv[8] for argv in self.of_kind("--exec")]

    # -- the runner ---------------------------------------------------------------------

    def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        argv = list(argv)
        self.calls.append(argv)
        action = self._check_argv(argv)
        if self._canned:
            return self._answer(*self._canned.popleft())
        if action == "--put":
            return self._put(argv[3], argv[5])
        if action == "--get":
            return self._get(argv[3], argv[5])
        return self._exec(argv[8])

    def _check_argv(self, argv: list[str]) -> str:
        assert all(isinstance(a, str) for a in argv), f"fake btty: non-str argv {argv!r}"
        assert argv[:2] == [self.command, "--yes"], f"fake btty: bad argv start {argv!r}"
        if len(argv) == 6 and argv[2] == "--put" and argv[4] == "--to":
            assert argv[5].startswith("/"), f"fake btty: relative --to {argv!r}"
            return "--put"
        if len(argv) == 6 and argv[2] == "--get" and argv[4] == "--out":
            assert argv[3].startswith("/"), f"fake btty: relative --get {argv!r}"
            return "--get"
        assert len(argv) == 9 and argv[1:6] == EXEC_FLAGS and argv[7] == "--exec", (
            f"fake btty: unexpected argv {argv!r}"
        )
        assert argv[6].isdigit() and int(argv[6]) > 0, f"fake btty: bad --timeout {argv!r}"
        if self.timeout is not None:
            assert argv[6] == str(self.timeout), f"fake btty: --timeout {argv[6]} != {self.timeout}"
        return "--exec"

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

    def _exec(self, script: str) -> tuple[int, str, str]:
        size = len(script.encode("utf-8", "surrogateescape"))
        if size > COMMAND_BUDGET:
            self.rejected.append(script)
            return self._fail(
                f"that command makes a {size} byte line, over the {COMMAND_BUDGET} limit -- "
                "it is typed into a shell in canonical mode, where anything longer is "
                "silently truncated"
            )
        for path in script_paths(script):
            self._inside(path)
        status, output = run_script(script)
        if self.crlf:
            output = output.replace("\n", "\r\n")
        result = json.dumps({"stdout": output, "stderr": "", "exit_code": status})
        return self._answer(status, result + "\n", "")
