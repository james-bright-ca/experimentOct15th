"""Fixtures for the UI tests: a local tree on tmp_path and the test doubles
from tests/ui/support.py, plus

* ``LoggingLocal`` / ``LoggingMemory``: backends that log every ``list_dir``
  and ``invalidate`` in order, with the thread each ran on (the Ctrl-R and
  threading tests read the order);
* ``parse_keys`` / ``send_bytes``: real terminal bytes through Textual's own
  xterm parser, for keys that Pilot's key names cannot produce (such as the
  ``¢`` a fast ESC+4 or Option-4 becomes);
* the autouse ``shell_guard``: $SHELL is the fake shell (tests/ui/fake_shell.py)
  for every test, so a LocalShell (Ctrl-O) can never start the user's real
  shell or read its rc files."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from textual import constants, events
from textual._xterm_parser import XTermParser
from textual.pilot import Pilot

from tests.ui import fake_shell
from tests.ui.support import RecordingLocal, RecordingMemory, StubEngine, on_main_thread
from twinfm.backend.base import Entry
from twinfm.ui.shell import default_shell

FILES = {"b.txt": b"bb", "A.txt": b"aaa", ".hidden": b"h", "c.txt": b"c" * 2000, "Zeta.md": b"z"}
DIRS = ["Sub", "alpha", ".git"]
LEFT_ORDER = ["..", ".git", "alpha", "Sub", ".hidden", "A.txt", "b.txt", "c.txt", "Zeta.md"]
"""The left directory in MC order: `..`, directories, files, each case-insensitive."""


@dataclass
class Tree:
    """LEFT holds FILES and DIRS (Sub has one file); RIGHT is empty."""

    left: Path
    right: Path

    @property
    def L(self) -> str:
        return str(self.left)

    @property
    def R(self) -> str:
        return str(self.right)


@pytest.fixture(scope="session")
def fake_shell_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The fake shell as an executable named ``sh`` (see tests/ui/fake_shell.py)."""
    return fake_shell.install(tmp_path_factory.mktemp("fake-shell"))


@pytest.fixture(autouse=True)
def shell_guard(fake_shell_path: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Autouse: $SHELL is the fake shell for the whole test, and the shell a
    LocalShell would start (``default_shell()``) is that fake. An unresolvable
    $SHELL would fall back to the user's real login shell, so the fixture
    fails the test rather than let that happen."""
    monkeypatch.setenv("SHELL", fake_shell_path)
    resolved = default_shell()
    assert resolved == [fake_shell_path, "-i"], (
        f"shell guard: default_shell() is {resolved}, not the fake shell {fake_shell_path}"
    )
    return fake_shell_path


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    left = tmp_path / "L"
    right = tmp_path / "R"
    left.mkdir()
    right.mkdir()
    for name, data in FILES.items():
        (left / name).write_bytes(data)
    for name in DIRS:
        (left / name).mkdir()
    (left / "Sub" / "inner.txt").write_bytes(b"inner")
    return Tree(left, right)


@pytest.fixture
def local() -> RecordingLocal:
    return RecordingLocal()


@pytest.fixture
def engine() -> StubEngine:
    return StubEngine()


# -- backends that log list_dir and invalidate -------------------------------------------

Event = tuple[str, str | None, bool]
"""``(method, path, ran_on_main_thread)``, method "list_dir" or "invalidate"."""


class LoggingLocal(RecordingLocal):
    """RecordingLocal that also logs ``list_dir`` and ``invalidate`` in call order."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[Event] = []

    def list_dir(self, path: str) -> list[Entry]:
        self.events.append(("list_dir", path, on_main_thread()))
        return super().list_dir(path)

    def invalidate(self, path: str | None = None) -> None:
        self.events.append(("invalidate", path, on_main_thread()))
        super().invalidate(path)

    def invalidated(self) -> list[Event]:
        return [e for e in self.events if e[0] == "invalidate"]


class LoggingMemory(RecordingMemory):
    """RecordingMemory that also logs ``list_dir`` and ``invalidate`` in call order."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.events: list[Event] = []
        super().__init__(*args, **kwargs)

    def list_dir(self, path: str) -> list[Entry]:
        self.events.append(("list_dir", path, on_main_thread()))
        return super().list_dir(path)

    def invalidate(self, path: str | None = None) -> None:
        self.events.append(("invalidate", path, on_main_thread()))
        super().invalidate(path)

    def invalidated(self) -> list[Event]:
        return [e for e in self.events if e[0] == "invalidate"]


@pytest.fixture
def logged() -> LoggingLocal:
    return LoggingLocal()


# -- real terminal bytes ----------------------------------------------------------------


def parse_keys(data: str) -> list[events.Key]:
    """The key events Textual's xterm parser (the one its Linux/macOS driver
    uses) makes of ``data``. A lone or trailing Esc is flushed after the
    parser's escape delay, as a pause in typing would. textual._xterm_parser
    is private API, used because the question is what Textual 8.2.8 makes
    of the bytes."""
    parser = XTermParser()
    out = [e for e in parser.feed(data) if isinstance(e, events.Key)]
    time.sleep(constants.ESCAPE_DELAY + 0.05)
    out += [e for e in parser.tick() if isinstance(e, events.Key)]
    return out


async def send_bytes(pilot: Pilot[Any], *chunks: str) -> list[str]:
    """Post the key events of each chunk to the app, as the driver does;
    chunks arrive with a pause between them. Returns the key names."""
    names: list[str] = []
    for chunk in chunks:
        for key in parse_keys(chunk):
            names.append(key.key)
            pilot.app.post_message(key)
            await pilot.pause()
    await pilot.pause()
    return names
