"""A fake interactive shell for the Ctrl-O tests, so that no test ever starts
the user's real shell or reads its rc files.

Run as a program (``install`` makes it an executable), it prints the prompt
``$ `` and reads one line at a time from its terminal. Commands:

``echo WORDS``          print the words
``cd DIR``              change directory (``--`` is ignored)
``pwd``                 print the working directory
``pid``                 print ``PID <its pid>``
``exit [N]``            exit with status N (0)
``sleep SECONDS``       run ``sleep`` as a foreground job: its own process
                        group, made the terminal's foreground, as a job-control
                        shell does; prints ``JOB <pid>`` first
``big N [MARKER]``      print about N bytes as numbered lines ``%07d``, then
                        create the file MARKER (if given), then print ``BIG-END``
``later SECONDS N MARKER``  sleep, then ``big N MARKER``
``size``                print ``SIZE <rows>x<columns>`` of its terminal
``ctty``                print whether its terminal is its controlling terminal
                        and it leads its session
``modes``               switch the alternate screen and bracketed paste on (the
                        sequence split across two writes), then ``MODES-ON``
``jobwinch``            a foreground job that prints ``JOB-READY``, then
                        ``JOB-WINCH`` on SIGWINCH, and ends

On SIGWINCH the shell itself prints ``WINCH <rows>x<columns>``. Flags such as
``-i`` are accepted and ignored. Standard library only.

``install(directory, name)`` writes an executable copy of this file named
``name`` (default ``sh``, a name LocalShell types ``cd`` into), with the test
interpreter in its ``#!`` line, and returns its path.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

PROMPT = b"$ "

_JOB_WINCH = (
    "import os, signal, time\n"
    "seen = []\n"
    "signal.signal(signal.SIGWINCH, lambda *a: seen.append(1))\n"
    "os.write(1, b'JOB-READY\\r\\n')\n"
    "end = time.monotonic() + 10\n"
    "while not seen and time.monotonic() < end:\n"
    "    time.sleep(0.02)\n"
    "if seen:\n"
    "    os.write(1, b'JOB-WINCH\\r\\n')\n"
)


def install(directory: str | os.PathLike[str], name: str = "sh") -> str:
    """Write this fake shell as the executable ``directory/name``; return its path."""
    path = Path(directory) / name
    source = Path(__file__).read_text()
    path.write_text(f"#!{sys.executable} -I\n{source}")
    path.chmod(0o755)
    return str(path)


def _say(text: str) -> None:
    os.write(1, text.encode() + b"\n")


def _size() -> str:
    size = os.get_terminal_size(0)
    return f"{size.lines}x{size.columns}"


def _foreground_job(argv: list[str]) -> None:
    """Run ``argv`` in its own process group as the terminal's foreground
    job, then take the terminal back."""
    job = subprocess.Popen(argv, process_group=0)
    _say(f"JOB {job.pid}")
    try:
        os.tcsetpgrp(0, job.pid)
    except OSError:
        pass
    job.wait()
    os.tcsetpgrp(0, os.getpgrp())


def _big(count: int, marker: str | None) -> None:
    data = b"".join(b"%07d\n" % i for i in range(count // 8))
    view = memoryview(data)
    while view:
        view = view[os.write(1, view) :]
    if marker:
        Path(marker).write_bytes(b"done")
    _say("BIG-END")


def _run(words: list[str]) -> None:
    command, args = words[0], words[1:]
    if command == "echo":
        _say(" ".join(args))
    elif command == "cd":
        args = [a for a in args if a != "--"]
        try:
            os.chdir(args[0] if args else os.path.expanduser("~"))
        except OSError as e:
            _say(f"cd: {e}")
    elif command == "pwd":
        _say(os.getcwd())
    elif command == "pid":
        _say(f"PID {os.getpid()}")
    elif command == "exit":
        sys.exit(int(args[0]) if args else 0)
    elif command == "sleep":
        _foreground_job(["sleep", args[0]])
    elif command == "big":
        _big(int(args[0]), args[1] if len(args) > 1 else None)
    elif command == "later":
        time.sleep(float(args[0]))
        _big(int(args[1]), args[2])
    elif command == "size":
        _say(f"SIZE {_size()}")
    elif command == "ctty":
        leads = os.getsid(0) == os.getpid()
        _say(f"CTTY {os.tcgetpgrp(0) == os.getpgrp()} SID {leads}")
    elif command == "modes":
        os.write(1, b"\x1b[?1049h\x1b[?20")
        os.write(1, b"04h")
        _say("MODES-ON")
    elif command == "jobwinch":
        _foreground_job([sys.executable, "-I", "-S", "-c", _JOB_WINCH])
    else:
        _say(f"unknown: {command}")


def main() -> None:
    # A job-control shell takes the terminal back from a finished job.
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    signal.signal(signal.SIGWINCH, lambda *_: os.write(1, f"WINCH {_size()}\r\n".encode()))
    while True:
        os.write(1, PROMPT)
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        try:
            words = shlex.split(line.decode(errors="replace"))
        except ValueError:
            _say("PARSE-ERROR")
            continue
        if words:
            _run(words)


if __name__ == "__main__":
    main()
