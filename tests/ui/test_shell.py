"""The persistent local shell behind Ctrl-O (twinfm.ui.shell), without Textual.

Each test gives LocalShell a fake terminal - an ``os.openpty`` pair whose
slave side is the visit's in_fd and out_fd - and the fake shell from
tests/ui/fake_shell.py, installed as an executable named ``sh`` so that
LocalShell types ``cd`` into it. A visit blocks until Ctrl-O, so it runs in a
thread while the test types into the terminal and reads what it shows.

The pty applies ONLCR and echoes what is typed, so a command's output is
told apart from the echo of the command: ``echo HEL''LO`` echoes with the
quotes and prints ``HELLO``. One smoke test runs ``/bin/sh -i`` with a clean
environment (no rc file); no test starts the user's own shell.
"""

from __future__ import annotations

import os
import pwd
import select
import shutil
import signal
import subprocess
import termios
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import twinfm.ui.shell as shell_module
from tests.ui import fake_shell
from twinfm.ui.shell import LocalShell, ShellVisit, default_shell, process_cwd

TIMEOUT = 10.0
CTRL_O = b"\x0f"
CTRL_U = b"\x15"  # the pty's kill-line character: empties a half-typed line
PENDIN = getattr(termios, "PENDIN", 0)


def wait_until(predicate: Callable[[], bool], timeout: float = TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def shell_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "twinfm-shell" and t.is_alive()]


class Term:
    """A fake terminal. ``fd`` (the pty's slave) is what the visit gets as
    in_fd and out_fd; ``type`` writes keyboard bytes into it, and a reader
    thread keeps everything it shows in ``out``."""

    def __init__(self, rows: int = 24, columns: int = 80) -> None:
        self.master, self.fd = os.openpty()
        termios.tcsetwinsize(self.fd, (rows, columns))
        self._out = bytearray()
        self._lock = threading.Lock()
        self._stop = False
        self._thread = threading.Thread(target=self._read, name="fake-terminal", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        while not self._stop:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._out += data

    @property
    def out(self) -> bytes:
        with self._lock:
            return bytes(self._out)

    def mark(self) -> int:
        with self._lock:
            return len(self._out)

    def since(self, mark: int) -> bytes:
        return self.out[mark:]

    def wait_for(self, needle: bytes, mark: int = 0, timeout: float = TIMEOUT) -> bool:
        return wait_until(lambda: needle in self.since(mark), timeout)

    def type(self, data: bytes) -> None:
        os.write(self.master, data)

    def attrs(self) -> list[object]:
        return termios.tcgetattr(self.fd)

    def is_raw(self) -> bool:
        return not self.attrs()[3] & (termios.ICANON | termios.ECHO | termios.ISIG)

    def close(self) -> None:
        self._stop = True
        self._thread.join(2)
        for fd in (self.fd, self.master):
            try:
                os.close(fd)
            except OSError:
                pass


class Visit:
    """``shell.visit(start_dir, panel_dir, term.fd, term.fd)`` on a thread."""

    def __init__(self, shell: LocalShell, term: Term, start_dir: str, panel_dir: str | None) -> None:
        self.term = term
        self.result: ShellVisit | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run, args=(shell, start_dir, panel_dir), name="test-visit", daemon=True
        )
        self.thread.start()

    def _run(self, shell: LocalShell, start_dir: str, panel_dir: str | None) -> None:
        try:
            self.result = shell.visit(start_dir, panel_dir, self.term.fd, self.term.fd)
        except BaseException as e:  # reported by end()
            self.error = e

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()

    def wait_shown(self) -> None:
        """Until the terminal is raw: the shell has it (bytes typed now reach it)."""
        assert wait_until(lambda: self.term.is_raw() or not self.alive), "the visit never took the terminal"
        assert self.alive, f"the visit ended at once: {self.result} {self.error!r}"

    def end(self, timeout: float = TIMEOUT) -> ShellVisit:
        """Wait for the visit to return; its ShellVisit."""
        self.thread.join(timeout)
        assert not self.alive, "the visit did not end"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def toggle(self) -> ShellVisit:
        """Ctrl-O (0x0F) and wait for the visit to return."""
        self.term.type(CTRL_O)
        return self.end()


class Harness:
    """Makes terminals, shells and visits, and ends them all afterwards."""

    def __init__(self, tmp_path: Path, fake: str) -> None:
        self.base = Path(os.path.realpath(tmp_path))
        self.fake = fake
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.base), "TERM": "xterm-256color"}
        self.terms: list[Term] = []
        self.shells: list[LocalShell] = []
        self.visits: list[Visit] = []

    def dir(self, name: str) -> str:
        path = self.base / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def term(self) -> Term:
        term = Term()
        self.terms.append(term)
        return term

    def shell(self, argv: list[str] | None = None, backlog: int = 1 << 20, env: dict[str, str] | None = None) -> LocalShell:
        shell = LocalShell(argv or [self.fake, "-i"], env if env is not None else self.env, backlog=backlog)
        self.shells.append(shell)
        return shell

    def visit(self, shell: LocalShell, term: Term, start_dir: str, panel_dir: str | None = None) -> Visit:
        visit = Visit(shell, term, start_dir, panel_dir)
        self.visits.append(visit)
        return visit

    def cleanup(self) -> None:
        for visit in self.visits:
            if visit.alive:
                visit.term.type(CTRL_O)
                visit.thread.join(5)
        for shell in self.shells:
            shell.close()
        for term in self.terms:
            term.close()


@pytest.fixture
def h(tmp_path: Path, fake_shell_path: str) -> Iterator[Harness]:
    harness = Harness(tmp_path, fake_shell_path)
    yield harness
    harness.cleanup()


def run(term: Term, command: str, until: bytes, mark: int | None = None) -> int:
    """Type ``command`` and Enter; wait until ``until`` shows. Returns the mark
    taken before typing."""
    start = term.mark() if mark is None else mark
    term.type(command.encode() + b"\r")
    assert term.wait_for(until, start), f"{until!r} did not show after {command!r}: {term.since(start)[-400:]!r}"
    return start


def number_after(term: Term, mark: int, label: bytes) -> int:
    """The number the shell printed as ``<label><number>`` after ``mark``."""
    assert term.wait_for(label, mark)
    start = term.since(mark).index(label) + mark + len(label)
    assert term.wait_for(b"\r\n", start)
    return int(term.since(start).split(b"\r")[0])


def numbers(data: bytes) -> list[int]:
    """The ``%07d`` lines ``big`` printed, in the order shown."""
    return [int(x) for x in data.replace(b"\r", b"").split(b"\n") if len(x) == 7 and x.isdigit()]


# -- default_shell and process_cwd --------------------------------------------------------


def test_default_shell_is_shell_and_dash_i_when_shell_resolves(fake_shell_path: str) -> None:
    assert os.environ["SHELL"] == fake_shell_path
    assert default_shell() == [fake_shell_path, "-i"]


def _passwd_fallback() -> list[str]:
    login = pwd.getpwuid(os.getuid()).pw_shell
    return [login, "-i"] if login and shutil.which(login) else ["/bin/sh", "-i"]


@pytest.mark.parametrize("value", [None, "/nonexistent/twinfm-test/sh", "no-such-shell-twinfm-test"])
def test_default_shell_falls_back_to_the_passwd_shell(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Only the argv is checked; nothing is started (the fallback is the
    user's real login shell)."""
    if value is None:
        monkeypatch.delenv("SHELL", raising=False)
    else:
        monkeypatch.setenv("SHELL", value)
    assert default_shell() == _passwd_fallback()


def test_default_shell_is_bin_sh_when_neither_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/nonexistent/twinfm-test/sh")
    monkeypatch.setattr(shell_module.shutil, "which", lambda *args, **kwargs: None)
    assert default_shell() == ["/bin/sh", "-i"]


def test_process_cwd_of_a_child_and_of_a_pid_that_does_not_exist(h: Harness) -> None:
    where = h.dir("child cwd")
    child = subprocess.Popen(["sleep", "30"], cwd=where)
    try:
        assert wait_until(lambda: process_cwd(child.pid) == where), process_cwd(child.pid)
    finally:
        child.kill()
        child.wait()
    assert process_cwd(child.pid) is None
    assert process_cwd(99_999_999) is None


# -- starting, typing, Ctrl-O ---------------------------------------------------------------


def test_a_shell_starts_nothing_until_its_first_visit_and_close_before_it_does_nothing(h: Harness) -> None:
    shell = h.shell()
    assert shell.running is False
    shell.close()
    shell.close()
    assert shell.running is False
    assert shell_threads() == []


def test_a_visit_runs_the_shell_in_start_dir_on_the_pty_as_its_controlling_terminal(h: Harness) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT), term.out
    visit.wait_shown()

    run(term, "pwd", f"\n{start}\r\n".encode())
    run(term, "ctty", b"CTTY True SID True")
    run(term, "echo HEL''LO", b"HELLO\r\n")

    assert visit.toggle() == ShellVisit(exited=None, cwd=start, moved=False)


def test_ctrl_o_ends_the_visit_unforwarded_and_the_bytes_before_it_reach_the_shell(h: Harness) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()

    term.type(b"echo BE''FORE" + CTRL_O)
    assert visit.end() == ShellVisit(exited=None, cwd=start, moved=False)
    assert shell.running is False

    again = h.visit(shell, term, start)
    again.wait_shown()
    mark = term.mark()
    term.type(b"\r")  # completes the line typed before Ctrl-O
    assert term.wait_for(b"\r\nBEFORE\r\n", 0), (
        f"the half line before Ctrl-O did not run as 'echo BEFORE': {term.since(mark)!r}"
    )
    again.toggle()
    # Nothing the shell's terminal showed is a Ctrl-O or its echo (^O).
    assert CTRL_O not in term.out and b"^O" not in term.out, term.out


TOGGLES = {"ctrl": b"\x1b[111;5u", "ctrl-caps-num": b"\x1b[111;69u", "ctrl-press": b"\x1b[111;5:1u"}


@pytest.mark.parametrize("toggle", [CTRL_O, *TOGGLES.values()], ids=["ctrl-o-byte", *TOGGLES])
def test_ctrl_o_never_reaches_the_shell_while_the_keys_around_it_do(h: Harness, toggle: bytes) -> None:
    """Byte for byte, with the shell reading its terminal raw (no line
    discipline that could act on Ctrl-O): the keys typed before Ctrl-O and
    those typed in the next visit reach the shell, the Ctrl-O (0x0F, or its
    kitty CSI-u form) never does."""
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    run(term, "rawline", b"RAW-READY\r\n")

    term.type(b"AB" + toggle)
    assert visit.end() == ShellVisit(exited=None, cwd=start, moved=False)

    again = h.visit(shell, term, start)
    again.wait_shown()
    mark = term.mark()
    term.type(b"C\r")
    assert term.wait_for(b"RAW ", mark), term.since(mark)
    hex_at = term.since(mark).index(b"RAW ") + mark + len(b"RAW ")
    assert term.wait_for(b"\r\n", hex_at), term.since(mark)
    got = bytes.fromhex(term.since(hex_at).split(b"\r")[0].decode())
    assert got == b"ABC\r", f"the shell read {got!r}"
    again.toggle()


@pytest.mark.parametrize("split", [False, True], ids=["one-write", "split"])
@pytest.mark.parametrize("sequence", list(TOGGLES.values()), ids=list(TOGGLES))
def test_kitty_ctrl_o_ends_the_visit(h: Harness, sequence: bytes, split: bool) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()

    if split:
        cut = sequence.index(b";") + 1
        term.type(sequence[:cut])
        time.sleep(0.01)  # the second part follows well within the 50 ms hold
        term.type(sequence[cut:])
    else:
        term.type(sequence)

    assert visit.end() == ShellVisit(exited=None, cwd=start, moved=False)


@pytest.mark.parametrize("data", [b"\x1b[111;5:3u", b"o"], ids=["ctrl-o-release", "plain-o"])
def test_a_ctrl_o_release_or_a_plain_o_does_not_end_the_visit(h: Harness, data: bytes) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()

    term.type(data)
    time.sleep(0.5)
    assert visit.alive, f"{data!r} ended the visit: {visit.result}"
    term.type(CTRL_U)
    visit.toggle()


def test_the_terminal_is_raw_during_a_visit_and_restored_after(h: Harness) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    before = term.attrs()
    assert not term.is_raw(), "precondition: the fake terminal starts cooked"

    visit = h.visit(shell, term, start)
    visit.wait_shown()
    during = term.attrs()
    assert not during[3] & (termios.ICANON | termios.ECHO | termios.ISIG), during
    visit.toggle()

    after = term.attrs()
    before[3] &= ~PENDIN  # type: ignore[operator]
    after[3] &= ~PENDIN  # type: ignore[operator]
    assert after == before


# -- the shell's end ---------------------------------------------------------------------


def test_exit_during_a_visit_returns_its_status_and_no_cwd(h: Harness) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()

    term.type(b"exit 3\r")

    assert visit.end() == ShellVisit(exited=3, cwd=None, moved=False)
    assert shell.running is False


def test_a_shell_that_died_while_hidden_is_replaced_in_start_dir(h: Harness) -> None:
    first, second = h.dir("first"), h.dir("second")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, first)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    old_pid = number_after(term, run(term, "pid", b"PID "), b"PID ")
    visit.toggle()

    os.kill(old_pid, signal.SIGKILL)
    time.sleep(0.3)

    mark = term.mark()
    again = h.visit(shell, term, second)
    assert term.wait_for(fake_shell.PROMPT, mark)
    again.wait_shown()
    run(term, "pwd", f"\n{second}\r\n".encode())
    new_pid = number_after(term, run(term, "pid", b"PID "), b"PID ")
    assert new_pid != old_pid
    result = again.toggle()
    assert result.exited is None
    assert result.cwd == second


# -- output while hidden -------------------------------------------------------------------


def _start_later(h: Harness, term: Term, visit: Visit, count: int) -> tuple[Path, int]:
    """Type ``later 1 COUNT MARKER`` and leave the visit before it prints.
    Returns the marker file and the terminal's mark once the visit ended."""
    marker = h.base / f"done-{count}"
    run(term, f"later 1 {count} {marker}", b"\r\n")
    visit.toggle()
    return marker, term.mark()


def test_output_made_while_hidden_lets_the_job_finish_and_is_replayed_in_order(h: Harness) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    marker, hidden = _start_later(h, term, visit, 4000)

    assert wait_until(marker.exists), "the job did not finish while the shell was hidden"
    time.sleep(0.2)
    assert term.since(hidden) == b"", "the hidden shell wrote to the terminal"

    again = h.visit(shell, term, start)
    assert term.wait_for(b"BIG-END", hidden)
    again.wait_shown()
    assert numbers(term.since(hidden)) == list(range(500))
    again.toggle()


def test_with_a_small_backlog_the_hidden_job_waits_and_no_byte_is_lost(h: Harness) -> None:
    start = h.dir("start")
    count = 200_000  # far more than the pty and the 256-byte backlog hold
    term, shell = h.term(), h.shell(backlog=256)
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    marker, hidden = _start_later(h, term, visit, count)

    time.sleep(2.0)
    assert not marker.exists(), "the job finished although the backlog was full"
    assert term.since(hidden) == b"", "the hidden shell wrote to the terminal"

    again = h.visit(shell, term, start)
    assert term.wait_for(b"BIG-END", hidden, timeout=30)
    assert marker.exists()
    again.wait_shown()
    assert numbers(term.since(hidden)) == list(range(count // 8))
    again.toggle()


# -- following the panel and reporting where the shell is -------------------------------------


def test_an_idle_shell_moves_to_panel_dir_even_with_a_space_and_a_quote(h: Harness) -> None:
    start, target = h.dir("start"), h.dir("two 'q' dir")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    assert shell.running is False
    visit.toggle()

    again = h.visit(shell, term, start, target)
    again.wait_shown()
    run(term, "pwd", f"\n{target}\r\n".encode())
    assert again.toggle() == ShellVisit(exited=None, cwd=target, moved=False)


def test_no_cd_is_typed_while_a_job_runs_and_running_follows_the_job(h: Harness) -> None:
    start, target = h.dir("start"), h.dir("target")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    assert shell.running is False
    run(term, "sleep 2", b"JOB ")
    assert wait_until(lambda: shell.running), "running stayed False during sleep"
    visit.toggle()

    mark = term.mark()
    again = h.visit(shell, term, start, target)
    again.wait_shown()
    time.sleep(0.5)
    assert b" cd -- " not in term.since(mark), term.since(mark)
    assert wait_until(lambda: not shell.running, timeout=5), "running stayed True after sleep ended"
    assert term.wait_for(fake_shell.PROMPT, mark)
    run(term, "pwd", f"\n{start}\r\n".encode())
    assert again.toggle() == ShellVisit(exited=None, cwd=start, moved=False)


def test_no_cd_is_typed_after_a_half_typed_line(h: Harness) -> None:
    start, target = h.dir("start"), h.dir("target")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    term.type(b"echo half" + CTRL_O)
    visit.end()

    mark = term.mark()
    again = h.visit(shell, term, start, target)
    again.wait_shown()
    time.sleep(0.5)
    assert b" cd -- " not in term.since(mark), term.since(mark)
    term.type(CTRL_U)
    run(term, "pwd", f"\n{start}\r\n".encode())
    again.toggle()


def test_no_cd_is_typed_into_a_shell_of_an_unknown_name(h: Harness) -> None:
    unknown = fake_shell.install(h.dir("bin"), "fakeshell")
    start, target = h.dir("start"), h.dir("target")
    term, shell = h.term(), h.shell([unknown, "-i"])
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    visit.toggle()

    mark = term.mark()
    again = h.visit(shell, term, start, target)
    again.wait_shown()
    time.sleep(0.5)
    assert b" cd -- " not in term.since(mark), term.since(mark)
    run(term, "pwd", f"\n{start}\r\n".encode())
    again.toggle()


def test_a_cd_typed_during_a_visit_gives_moved_and_the_real_path(h: Harness) -> None:
    start, target = h.dir("start"), h.dir("target")
    link = h.base / "link-to-target"
    link.symlink_to(target)
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()

    run(term, f"cd {link}", b"\r\n" + fake_shell.PROMPT)  # the prompt after the cd

    assert visit.toggle() == ShellVisit(exited=None, cwd=target, moved=True)


# -- the terminal around a visit --------------------------------------------------------------


def test_a_resize_during_a_visit_reaches_the_shell(h: Harness) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    run(term, "size", b"SIZE 24x80")

    mark = term.mark()
    termios.tcsetwinsize(term.fd, (30, 100))
    assert term.wait_for(b"WINCH 30x100", mark), term.since(mark)
    run(term, "size", b"SIZE 30x100")
    visit.toggle()


def test_modes_left_on_are_set_again_and_a_foreground_job_gets_sigwinch_at_the_next_show(h: Harness) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    run(term, "modes", b"MODES-ON")
    visit.toggle()

    mark = term.mark()
    again = h.visit(shell, term, start)
    again.wait_shown()
    assert term.wait_for(b"\x1b[?1049h", mark) and term.wait_for(b"\x1b[?2004h", mark), term.since(mark)

    run(term, "jobwinch", b"JOB-READY")
    again.toggle()
    mark = term.mark()
    third = h.visit(shell, term, start)
    assert term.wait_for(b"JOB-WINCH", mark), term.since(mark)
    third.wait_shown()
    assert wait_until(lambda: not shell.running)
    third.toggle()


# -- close -----------------------------------------------------------------------------------


def test_close_ends_the_shell_and_its_job_reaps_it_and_ends_the_reader(h: Harness) -> None:
    start = h.dir("start")
    term, shell = h.term(), h.shell()
    visit = h.visit(shell, term, start)
    assert term.wait_for(fake_shell.PROMPT)
    visit.wait_shown()
    shell_pid = number_after(term, run(term, "pid", b"PID "), b"PID ")
    job_pid = number_after(term, run(term, "sleep 30", b"JOB "), b"JOB ")
    assert wait_until(lambda: shell.running)
    visit.toggle()
    assert shell_threads(), "precondition: the reader thread runs while the shell is hidden"

    shell.close()

    assert gone(shell_pid), "the shell is still running (or an unreaped zombie)"
    with pytest.raises(ChildProcessError):
        os.waitpid(shell_pid, os.WNOHANG)
    assert wait_until(lambda: gone(job_pid), timeout=5), "the shell's foreground job is still running"
    assert shell_threads() == []
    assert shell.running is False
    shell.close()  # again: nothing to do


def test_a_shell_that_cannot_start_raises_oserror(h: Harness) -> None:
    term = h.term()
    shell = h.shell([str(h.base / "no-such-shell"), "-i"])
    with pytest.raises(OSError):
        shell.visit(h.dir("start"), None, term.fd, term.fd)
    assert shell.running is False
    assert shell_threads() == []


# -- /bin/sh ----------------------------------------------------------------------------------


def test_bin_sh_smoke_job_control_cd_and_exit(h: Harness) -> None:
    """A real POSIX shell, with a clean environment and no rc file: running
    follows a job, a cd is typed into a directory with a space and a quote,
    a typed cd is reported, and exit returns its status."""
    start, target = h.dir("a"), h.dir("b c'd")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(h.base),
        "ENV": "",
        "PS1": "SH$ ",
        "TERM": "xterm",
        "BASH_SILENCE_DEPRECATION_WARNING": "1",
    }
    term, shell = h.term(), h.shell(["/bin/sh", "-i"], env=env)
    visit = h.visit(shell, term, start)
    assert term.wait_for(b"SH$ "), term.out
    visit.wait_shown()
    mark = term.mark()
    term.type(b"sleep 2\r")
    assert wait_until(lambda: shell.running, timeout=5), "running stayed False during sleep"
    assert term.wait_for(b"SH$ ", mark, timeout=10)
    assert wait_until(lambda: not shell.running, timeout=5)
    visit.toggle()

    again = h.visit(shell, term, start, target)
    again.wait_shown()
    run(term, "pwd", f"\n{target}\r\n".encode())
    run(term, "cd /", b"SH$ ")
    assert again.toggle() == ShellVisit(exited=None, cwd="/", moved=True)

    last = h.visit(shell, term, start)
    last.wait_shown()
    term.type(b"exit 4\r")
    assert last.end() == ShellVisit(exited=4, cwd=None, moved=False)
