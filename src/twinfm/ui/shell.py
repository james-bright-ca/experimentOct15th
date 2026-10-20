"""A persistent local shell on a pseudo-terminal (Ctrl-O), free of Textual.

``LocalShell`` keeps one interactive shell alive between visits. ``visit``
hands the real terminal to it: the terminal goes raw, every byte typed goes
to the shell's pty and the shell's output to the terminal, until Ctrl-O
(the byte 0x0F, or the kitty keyboard protocol's CSI-u form) or the shell's
exit. The caller gives up the terminal around a visit, as for an editor.

Between visits a reader thread keeps draining the pty, so the shell never
blocks on output: what it writes while hidden is kept, up to ``backlog``
bytes, and replayed at the next visit. At the cap the reader stops reading,
so the shell waits and nothing is dropped. A visit first moves an idle
shell with no half-typed line to the panel's directory by typing
`` cd -- <dir>`` (sh, bash, zsh, ksh, dash and fish only), and reports where
the shell is when it ends, read from the OS (``ShellVisit``).

The shell starts through a small Python helper that makes the pty its
controlling terminal and execs it, so nothing runs in a forked copy of this
multi-threaded process. POSIX only: the module imports anywhere, and
``visit`` raises OSError where there is no pty.
"""

from __future__ import annotations

import errno
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache

try:
    import termios
    import tty
except ImportError:  # no terminal control here (Windows): visit raises OSError
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

__all__ = ["LocalShell", "ShellVisit", "default_shell", "process_cwd"]

# Run as `python -I -S -c _HELPER <report fd> <shell argv...>` in a new
# session: take the pty on fd 0 as the controlling terminal, then exec the
# shell. A failure is written to the report fd as an errno; on success exec
# closes it, so the parent reads end-of-file.
_HELPER = (
    "import fcntl, os, sys, termios\n"
    "fd = int(sys.argv[1])\n"
    "try:\n"
    "    os.set_inheritable(fd, False)\n"
    "    fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "    os.execvp(sys.argv[2], sys.argv[2:])\n"
    "except OSError as e:\n"
    "    os.write(fd, str(e.errno or 0).encode())\n"
    "os._exit(127)\n"
)

# Ctrl-O: the byte 0x0F, or a CSI-u key 111 whose modifiers and event type
# _find_toggle checks.
_KEY_O = re.compile(rb"\x0f|\x1b\[111(?::\d*)*;(\d+)(?::(\d+))?(?:;[\d:]*)?u")
_CAPS_NUM_LOCK = 64 | 128
_CTRL = 4
# An escape sequence cut off at the end of a read.
_PARTIAL = re.compile(rb"\x1b(?:\[[\d:;?]*)?\Z")
_MODE = re.compile(rb"\x1b\[\?([\d;]*)([hl])")
# The alternate-screen and bracketed-paste modes, set again at each show.
_MODES = (b"1049", b"1047", b"47", b"2004")
_CD_SHELLS = frozenset({"sh", "bash", "zsh", "ksh", "dash", "fish"})
_TICK = 0.1  # how often a visit checks the terminal's size and the shell
_HOLD = 0.05  # how long a partial escape sequence waits for the rest
_CHUNK = 65536

# <sys/proc_info.h>: PROC_PIDVNODEPATHINFO fills a struct proc_vnodepathinfo,
# whose first member, the current directory, is a vnode_info followed by
# the path (MAXPATHLEN bytes); the root directory follows in the same shape.
_PROC_PIDVNODEPATHINFO = 9
_VNODE_INFO_SIZE = 152
_MAXPATHLEN = 1024
_VNODEPATHINFO_SIZE = 2 * (_VNODE_INFO_SIZE + _MAXPATHLEN)


def default_shell() -> list[str]:
    """The interactive shell to run: ``[$SHELL, "-i"]`` when ``shutil.which``
    finds $SHELL, else the passwd entry's shell, else ``["/bin/sh", "-i"]``.
    Starts nothing."""
    shell = os.environ.get("SHELL")
    if shell and shutil.which(shell):
        return [shell, "-i"]
    shell = _passwd_shell()
    if shell and shutil.which(shell):
        return [shell, "-i"]
    return ["/bin/sh", "-i"]


def _passwd_shell() -> str | None:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_shell
    except (ImportError, KeyError):
        return None


def process_cwd(pid: int) -> str | None:
    """The working directory of process ``pid`` as the OS reports it (a real
    path); None when it cannot be read or the process is gone."""
    if sys.platform.startswith("linux"):
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    if sys.platform == "darwin":
        return _darwin_cwd(pid)
    return None


@cache
def _proc_pidinfo() -> Callable[..., int] | None:
    import ctypes
    import ctypes.util

    try:
        proc_pidinfo = ctypes.CDLL(ctypes.util.find_library("proc"), use_errno=True).proc_pidinfo
    except (OSError, AttributeError):
        return None
    proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    proc_pidinfo.restype = ctypes.c_int
    return proc_pidinfo


def _darwin_cwd(pid: int) -> str | None:
    import ctypes

    proc_pidinfo = _proc_pidinfo()
    if proc_pidinfo is None:
        return None
    info = ctypes.create_string_buffer(_VNODEPATHINFO_SIZE)
    if proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO, 0, info, _VNODEPATHINFO_SIZE) <= 0:
        return None
    path = info.raw[_VNODE_INFO_SIZE : _VNODE_INFO_SIZE + _MAXPATHLEN].split(b"\0", 1)[0]
    return os.fsdecode(path) if path else None


@dataclass(frozen=True)
class ShellVisit:
    """How a visit ended.

    ``exited`` is the shell's exit status when it ended during the visit,
    else None. ``cwd`` is the shell's working directory when the visit ended,
    None once it exited or when unreadable. ``moved`` says whether that
    differs from where the shell was when the visit began.
    """

    exited: int | None
    cwd: str | None
    moved: bool


class LocalShell:
    """One interactive shell on a pty, kept alive between visits.

    ``argv`` is the shell's command, None for ``default_shell()`` at each
    start; ``env`` its environment, None for this process's. Nothing starts
    before the first ``visit``. ``backlog`` caps the output kept while the
    shell is hidden. Use it from one thread.
    """

    def __init__(
        self,
        argv: list[str] | None = None,
        env: Mapping[str, str] | None = None,
        backlog: int = 1 << 20,
    ) -> None:
        self._argv = list(argv) if argv is not None else None
        self._env = dict(env) if env is not None else None
        self._cap = backlog
        self._proc: subprocess.Popen[bytes] | None = None
        self._name = ""
        self._master = -1
        self._dirty = False
        # The reader thread and what it shares, under _lock: where output
        # goes (the terminal while shown, else the backlog) and the modes.
        self._reader: threading.Thread | None = None
        self._wake_r = self._wake_w = -1
        self._stopping = False
        self._ended = threading.Event()
        self._lock = threading.Lock()
        self._out: int | None = None
        self._backlog = bytearray()
        self._modes: set[bytes] = set()
        self._mode_tail = b""

    @property
    def running(self) -> bool:
        """True while the shell is alive and a command other than the shell
        holds the pty's foreground; False at the prompt, after the shell
        exited and before it started."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            return os.tcgetpgrp(self._master) != proc.pid
        except OSError:
            return False

    def visit(self, start_dir: str, panel_dir: str | None, in_fd: int, out_fd: int) -> ShellVisit:
        """Show the shell on the terminal (``in_fd`` in, ``out_fd`` out) until
        Ctrl-O or the shell's exit, and say how the visit ended.

        With no live shell a new one starts in ``start_dir``, else this
        process's directory, else home; one that died while hidden is
        replaced silently. When ``panel_dir`` is given and the shell is
        elsewhere, idle at an empty prompt, it is moved there first. The
        terminal is raw during the visit and restored after. Raises OSError
        when no shell can start or there is no pty.
        """
        if termios is None or not hasattr(os, "openpty"):
            raise OSError(errno.ENOSYS, "This platform has no pseudo-terminals")
        if not self._alive():
            self.close()
            began = self._start(start_dir, in_fd)
        else:
            began = _real(process_cwd(self._pid()))
        if panel_dir is not None and self._cd(panel_dir, began):
            began = os.path.realpath(panel_dir)
        proc = self._proc
        assert proc is not None, "the shell has not started"
        if self._pump(proc, in_fd, out_fd):
            self.close()
            return ShellVisit(exited=proc.returncode, cwd=None, moved=False)
        cwd = process_cwd(proc.pid)
        moved = cwd is not None and began is not None and _real(cwd) != began
        return ShellVisit(exited=None, cwd=cwd, moved=moved)

    def close(self, timeout: float = 1.0) -> None:
        """End the shell and its foreground job: SIGHUP, close the pty, wait
        up to ``timeout`` seconds, then SIGKILL. Reaps the shell and stops the
        reader thread. Does nothing when no shell is running."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            groups = {proc.pid, self._foreground()}
            for group in groups - {0}:
                try:
                    os.killpg(group, signal.SIGHUP)
                except OSError:
                    pass
        self._stopping = True
        self._wake()
        if self._reader is not None:
            self._reader.join()
        for fd in (self._master, self._wake_r, self._wake_w):
            os.close(fd)
        self._proc, self._reader = None, None
        self._master = self._wake_r = self._wake_w = -1
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()

    def _pid(self) -> int:
        assert self._proc is not None, "the shell has not started"
        return self._proc.pid

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._ended.is_set()

    def _foreground(self) -> int:
        """The pty's foreground process group, 0 when unknown."""
        try:
            return max(os.tcgetpgrp(self._master), 0)
        except OSError:
            return 0

    def _start(self, start_dir: str, in_fd: int) -> str:
        """Start a shell on a new pty sized like the terminal, and its reader
        thread; return the real path of the directory it started in."""
        argv = list(self._argv) if self._argv is not None else default_shell()
        master, slave = os.openpty()
        try:
            try:
                termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(in_fd))
                termios.tcsetwinsize(slave, termios.tcgetwinsize(in_fd))
            except termios.error:  # not a terminal: keep the pty's defaults
                pass
            proc, cwd = self._spawn(argv, slave, start_dir)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._proc, self._name, self._master = proc, os.path.basename(argv[0]), master
        self._dirty = self._stopping = False
        self._ended = threading.Event()
        self._out = None
        self._backlog.clear()
        self._modes.clear()
        self._mode_tail = b""
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)
        self._reader = threading.Thread(
            target=self._read, args=(master, self._wake_r, self._ended), name="twinfm-shell", daemon=True
        )
        self._reader.start()
        return os.path.realpath(cwd)

    def _spawn(self, argv: list[str], slave: int, start_dir: str) -> tuple[subprocess.Popen[bytes], str]:
        """Run ``argv`` on the pty ``slave`` in the first directory that can
        be used. Raises OSError when it cannot be executed."""
        failure: OSError | None = None
        for cwd in _start_dirs(start_dir):
            report_r, report_w = os.pipe()
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-c", _HELPER, str(report_w), *argv],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=cwd,
                    env=self._env,
                    start_new_session=True,
                    pass_fds=(report_w,),
                )
            except OSError as e:  # the directory cannot be used: try the next
                os.close(report_r)
                failure = e
                continue
            finally:
                os.close(report_w)
            with os.fdopen(report_r, "rb") as report:
                error = report.read()
            if error:
                proc.wait()
                code = int(error)
                raise OSError(code, os.strerror(code), argv[0])
            return proc, cwd
        assert failure is not None
        raise failure

    def _cd(self, panel_dir: str, cwd: str | None) -> bool:
        """Type ``cd`` to ``panel_dir`` when the shell is elsewhere, idle,
        with no half-typed line, and known to understand it."""
        if (
            os.path.realpath(panel_dir) == cwd
            or self._dirty
            or self._foreground() != self._pid()
            or self._name not in _CD_SHELLS
        ):
            return False
        self._send(b" cd -- " + os.fsencode(shlex.quote(panel_dir)) + b"\r")
        return True

    def _pump(self, proc: subprocess.Popen[bytes], in_fd: int, out_fd: int) -> bool:
        """Connect the terminal to the shell ``proc`` until Ctrl-O (False) or
        the shell's end (True)."""
        try:
            saved = termios.tcgetattr(in_fd)
        except termios.error:  # not a terminal: nothing to make raw
            saved = None
        if saved is not None:
            tty.setraw(in_fd, termios.TCSANOW)
        try:
            size = self._resize(in_fd, None)
            self._show(out_fd)
            held, held_at = b"", 0.0
            next_tick = time.monotonic() + _TICK
            while True:
                if self._ended.is_set() or proc.poll() is not None:
                    self._ended.wait(_TICK)  # let the reader pass on the last output
                    return True
                now = time.monotonic()
                if now >= next_tick:
                    size = self._resize(in_fd, size)
                    next_tick = now + _TICK
                if held and now >= held_at + _HOLD:
                    self._type(held)
                    held = b""
                until = min(next_tick, held_at + _HOLD) if held else next_tick
                if not select.select([in_fd], [], [], max(until - now, 0.0))[0]:
                    continue
                data = os.read(in_fd, 4096)
                if not data:  # the terminal is gone
                    return False
                data, held = held + data, b""
                toggle = _find_toggle(data)
                if toggle is not None:
                    self._type(data[: toggle.start()])
                    return False
                partial = _PARTIAL.search(data)
                if partial is not None:
                    data, held, held_at = data[: partial.start()], partial.group(), time.monotonic()
                self._type(data)
        finally:
            with self._lock:
                self._out = None
            if saved is not None:
                termios.tcsetattr(in_fd, termios.TCSANOW, saved)

    def _show(self, out_fd: int) -> None:
        """Replay the backlog and the modes the shell left on, send its
        output to ``out_fd`` from now on, and nudge a foreground job to
        redraw."""
        with self._lock:
            modes = b"".join(b"\x1b[?" + mode + b"h" for mode in _MODES if mode in self._modes)
            _write_all(out_fd, bytes(self._backlog) + modes)
            self._backlog.clear()
            self._out = out_fd
        self._wake()
        job = self._foreground()
        if job and job != self._pid():
            try:
                os.killpg(job, signal.SIGWINCH)
            except OSError:
                pass

    def _resize(self, in_fd: int, size: tuple[int, int] | None) -> tuple[int, int] | None:
        """Copy the terminal's size to the pty when it is not ``size``."""
        try:
            now = termios.tcgetwinsize(in_fd)
        except termios.error:
            return size
        if now != size:
            try:
                termios.tcsetwinsize(self._master, now)
            except termios.error:
                pass
        return now

    def _type(self, data: bytes) -> None:
        """Pass keyboard input to the shell; a chunk not ending in Enter or
        Ctrl-C leaves a half-typed line."""
        if data:
            self._send(data)
            self._dirty = not data.endswith((b"\r", b"\n", b"\x03"))

    def _send(self, data: bytes) -> None:
        try:
            _write_all(self._master, data)
        except OSError:  # the shell has gone; the visit notices its end
            pass

    def _wake(self) -> None:
        try:
            os.write(self._wake_w, b"w")
        except OSError:  # a wake-up is already pending, or no reader
            pass

    def _read(self, master: int, wake: int, ended: threading.Event) -> None:
        """The reader thread, the only reader of ``master``: output goes to
        the terminal while shown, else to the backlog until the cap."""
        while True:
            with self._lock:
                full = self._out is None and len(self._backlog) >= self._cap
            ready = select.select([wake] if full else [master, wake], [], [])[0]
            if wake in ready:
                os.read(wake, 512)
                if self._stopping:
                    return
            if master not in ready:
                continue
            with self._lock:
                size = _CHUNK if self._out is not None else min(_CHUNK, self._cap - len(self._backlog))
                if size <= 0:
                    continue
                try:
                    data = os.read(master, size)
                except OSError:  # EIO: the shell's side of the pty is closed
                    data = b""
                if not data:
                    ended.set()
                    return
                self._track(data)
                if self._out is None:
                    self._backlog += data
                    continue
                try:
                    _write_all(self._out, data)
                except OSError:  # the terminal has gone
                    pass

    def _track(self, data: bytes) -> None:
        """Follow the alternate-screen and bracketed-paste modes in the
        shell's output, also across reads."""
        text = self._mode_tail + data
        for found in _MODE.finditer(text):
            for mode in found.group(1).split(b";"):
                if mode not in _MODES:
                    continue
                if found.group(2) == b"h":
                    self._modes.add(mode)
                else:
                    self._modes.discard(mode)
        tail = _PARTIAL.search(text, max(len(text) - 32, 0))
        self._mode_tail = tail.group() if tail is not None else b""


def _find_toggle(data: bytes) -> re.Match[bytes] | None:
    """The first Ctrl-O in ``data``: 0x0F, or CSI-u key 111 with ctrl as the
    only modifier besides Caps Lock and Num Lock, pressed or untyped."""
    for found in _KEY_O.finditer(data):
        if found.group() == b"\x0f":
            return found
        mods, event = int(found.group(1)), found.group(2)
        if ((mods - 1) & ~_CAPS_NUM_LOCK) == _CTRL and event in (None, b"1"):
            return found
    return None


def _start_dirs(start_dir: str) -> list[str]:
    dirs = [start_dir]
    try:
        dirs.append(os.getcwd())
    except OSError:
        pass
    dirs.append(os.path.expanduser("~"))
    return dirs


def _real(path: str | None) -> str | None:
    return os.path.realpath(path) if path is not None else None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
