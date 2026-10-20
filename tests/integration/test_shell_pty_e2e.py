"""End to end: ``python -m twinfm DIR DIR`` in a real pseudo-terminal, with
Textual's own driver, and $SHELL set to the fake shell (tests/ui/fake_shell.py).

Ctrl-O hands the terminal to the local shell (the kitty keyboard mode is
popped before the alternate screen is left), a command's output shows, a
second Ctrl-O brings the panels back (alternate screen and kitty mode pushed
again) with the active panel at the directory the shell moved to, and F10
ends twinfm with status 0 and no shell left behind.

The config is a missing file (--config and TWINFM_CONFIG), so the user's own
config is never read; HOME is a temporary directory.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path

TIMEOUT = 20.0

# Run as `python -I -S -c HELPER ARGV...`: take the pty on fd 0 as the
# controlling terminal (a new session has none), then exec ARGV.
HELPER = (
    "import fcntl, os, sys, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)


class PtyApp:
    """A program running on a pty sized 80x24; ``out`` is everything it wrote."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str) -> None:
        self.master, slave = os.openpty()
        termios.tcsetwinsize(slave, (24, 80))
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", HELPER, *argv],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        os.close(slave)
        self.out = bytearray()

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self.master, 65536)
                except OSError:  # EIO: the program closed the pty
                    return
                if not data:
                    return
                self.out += data

    def until(self, needle: bytes, start: int = 0, timeout: float = TIMEOUT) -> int:
        """The index of ``needle`` in the output at or after ``start``, -1 on timeout."""
        end = time.monotonic() + timeout
        while True:
            found = self.out.find(needle, start)
            if found >= 0 or time.monotonic() >= end:
                return found
            self.pump(0.05)

    def send(self, data: bytes) -> int:
        """Type ``data``; returns the output mark taken just before."""
        mark = len(self.out)
        os.write(self.master, data)
        return mark

    def tail(self, start: int = 0) -> bytes:
        return bytes(self.out[start:])[-600:]

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
            self.proc.wait(5)
        os.close(self.master)


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if gone(pid):
            return True
        time.sleep(0.05)
    return gone(pid)


def test_ctrl_o_toggles_a_persistent_local_shell_and_f10_leaves_none(tmp_path: Path, fake_shell_path: str) -> None:
    base = Path(os.path.realpath(tmp_path))
    panels, other, home = base / "panels", base / "other", base / "home"
    for path in (panels / "sub", other, home):
        path.mkdir(parents=True)
    (panels / "f.txt").write_text("x")
    (other / "zzz-marker.txt").write_text("x")
    missing_config = str(base / "no-config.toml")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TERM": "xterm-256color",
        "SHELL": fake_shell_path,
        "TWINFM_CONFIG": missing_config,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    argv = [sys.executable, "-m", "twinfm", "--config", missing_config, str(panels), str(panels)]
    app = PtyApp(argv, env, cwd=str(base))
    shell_pid: int | None = None
    try:
        assert app.until(b"\x1b[?1049h") >= 0, f"twinfm did not take the alternate screen: {app.tail()!r}"
        assert app.until(b"f.txt") >= 0, f"the panels did not list: {app.tail()!r}"
        app.pump(1.0)

        # Ctrl-O: the kitty keyboard mode is popped, then the alternate screen left.
        mark = app.send(b"\x0f")
        pop = app.until(b"\x1b[<u", mark)
        leave = app.until(b"\x1b[?1049l", mark)
        assert 0 <= pop < leave, f"pop {pop}, leave {leave}: {app.tail(mark)!r}"
        assert app.until(b"$ ", leave) >= 0, f"no prompt after Ctrl-O: {app.tail(mark)!r}"

        mark = app.send(b"pid\r")
        at = app.until(b"PID ", mark)
        assert at >= 0 and app.until(b"\r\n", at) >= 0, app.tail(mark)
        shell_pid = int(bytes(app.out[at + 4 :]).split(b"\r")[0])

        mark = app.send(b"echo HEL''LO-42\r")
        assert app.until(b"HELLO-42\r\n", mark) >= 0, f"no command output: {app.tail(mark)!r}"

        mark = app.send(f"cd {other}\r".encode())
        assert app.until(b"\r\n$ ", mark) >= 0, app.tail(mark)

        # Ctrl-O again: the panels come back, and the active one follows the cd.
        mark = app.send(b"\x0f")
        assert app.until(b"\x1b[?1049h", mark) >= 0, f"the alternate screen was not taken back: {app.tail(mark)!r}"
        assert app.until(b"\x1b[>25u", mark) >= 0, f"the kitty mode was not pushed again: {app.tail(mark)!r}"
        assert app.until(b"zzz-marker", mark) >= 0, f"the active panel did not follow the shell's cd: {app.tail(mark)!r}"
        app.pump(0.5)

        app.send(b"\x1b[21~")  # F10
        end = time.monotonic() + TIMEOUT
        while app.proc.poll() is None and time.monotonic() < end:
            app.pump(0.1)
        assert app.proc.poll() == 0, f"twinfm exited with {app.proc.poll()}: {app.tail()!r}"
        assert wait_gone(shell_pid), f"the shell {shell_pid} is still running after twinfm exited"
    finally:
        app.close()
        if shell_pid is not None and not gone(shell_pid):
            os.kill(shell_pid, signal.SIGKILL)


def test_f10_while_a_command_runs_in_the_shell_asks_first_and_quit_leaves_no_process(
    tmp_path: Path, fake_shell_path: str
) -> None:
    base = Path(os.path.realpath(tmp_path))
    panels, home = base / "panels", base / "home"
    for path in (panels, home):
        path.mkdir(parents=True)
    (panels / "f.txt").write_text("x")
    missing_config = str(base / "no-config.toml")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TERM": "xterm-256color",
        "SHELL": fake_shell_path,
        "TWINFM_CONFIG": missing_config,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    argv = [sys.executable, "-m", "twinfm", "--config", missing_config, str(panels), str(panels)]
    app = PtyApp(argv, env, cwd=str(base))
    pids: list[int] = []
    try:
        assert app.until(b"f.txt") >= 0, f"the panels did not list: {app.tail()!r}"
        app.pump(1.0)
        mark = app.send(b"\x0f")
        assert app.until(b"$ ", mark) >= 0, f"no prompt after Ctrl-O: {app.tail(mark)!r}"

        for command, label in ((b"pid\r", b"PID "), (b"sleep 4242\r", b"JOB ")):
            mark = app.send(command)
            at = app.until(label, mark)
            assert at >= 0 and app.until(b"\r\n", at) >= 0, app.tail(mark)
            pids.append(int(bytes(app.out[at + len(label) :]).split(b"\r")[0]))
        shell_pid, job_pid = pids
        app.pump(0.3)

        mark = app.send(b"\x0f")
        assert app.until(b"f.txt", mark) >= 0, f"the panels did not come back: {app.tail(mark)!r}"
        app.pump(0.5)

        mark = app.send(b"\x1b[21~")  # F10 while sleep runs
        assert app.until(b"Quit anyway", mark) >= 0, f"F10 did not ask: {app.tail(mark)!r}"
        app.pump(0.3)
        app.send(b"n")  # Cancel
        app.pump(1.0)
        assert app.proc.poll() is None, "Cancel ended twinfm"
        assert not gone(job_pid), "Cancel ended the command"

        mark = app.send(b"\x1b[21~")
        assert app.until(b"Quit anyway", mark) >= 0, f"F10 did not ask again: {app.tail(mark)!r}"
        app.pump(0.3)
        app.send(b"y")  # Quit
        end = time.monotonic() + TIMEOUT
        while app.proc.poll() is None and time.monotonic() < end:
            app.pump(0.1)
        assert app.proc.poll() == 0, f"twinfm exited with {app.proc.poll()}: {app.tail()!r}"
        assert wait_gone(shell_pid), f"the shell {shell_pid} is still running"
        assert wait_gone(job_pid), f"the command {job_pid} is still running"
    finally:
        app.close()
        for pid in pids:
            if not gone(pid):
                os.kill(pid, signal.SIGKILL)
