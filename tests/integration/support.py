"""Helpers for the end-to-end tests (fixtures are in conftest.py).

* Trees: ``build`` creates a ``relative path -> DIR | FILE | LINK`` spec,
  ``tree_of`` reads one back (lstat), ``leftovers`` finds archive-mode
  temporaries. ``PROJECT`` (local) and ``DOCS`` (remote) are the folders the
  transfer tests send.
* ``World``: the directories, the FakeBtty and the backends of one test.
* ``real_app``: TwinFM with the real engine, a FakeConnector and a
  recording ``run_editor``; ``btty_app`` puts a local panel left and the
  btty panel right.
* Pilot steps: ``settle`` (tests.ui.support's, but failing instead of
  hanging when an operation waits for an answer no test gives),
  ``next_dialog``, ``to_right``, ``accept_transfer``.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import Checkbox

from tests.backend.contract import DIR, FILE, LINK, Node
from tests.backend.fake_btty import FakeBtty
from tests.ui import support as ui
from tests.ui.support import FakeConnector, focused_id
from twinfm.backend.base import Backend
from twinfm.backend.btty import BttyBackend
from twinfm.backend.local import LocalBackend
from twinfm.config import Settings
from twinfm.ui.app import TwinFM
from twinfm.ui.dialogs import InputDialog, TransferDialog

TIMEOUT = 41
"""btty_timeout for the fake: not the default, so every non-archive exec argv proves it."""

ARCHIVE_TIMEOUT = 777
"""btty_archive_timeout for the fake: pack, unpack and merge argvs must carry it."""

ARCHIVE_SCRIPTS = ("pack", "pack-from-file", "unpack", "merge")
"""FakeBtty's kinds for the archive scripts."""

LEFTOVER_PREFIX = ".twinfm-"
"""Archive mode's temporaries (staging directories and archives) start with this."""

LABEL = "Archive folders and multiple items"
"""The transfer dialog's checkbox."""


# -- trees ------------------------------------------------------------------------------

Spec = Mapping[str, Node]
"""A tree as ``relative path -> DIR | FILE(data) | LINK(target)``; parents first."""

PROJECT: dict[str, Node] = {
    "project": DIR,
    "project/README.md": FILE(b"# project\n"),
    "project/with space.txt": FILE(b"a name with a space\n"),
    "project/it's \"quoted\".txt": FILE(b"quotes\n"),
    "project/-leading-dash.txt": FILE(b"dash\n"),
    "project/star*?[ab].txt": FILE(b"glob\n"),
    "project/ünïcødé-日本.txt": FILE("non-ASCII é\n".encode()),
    "project/src": DIR,
    "project/src/main.py": FILE(b"print('hello')\n"),
    "project/src/deep": DIR,
    "project/src/deep/data.bin": FILE(bytes(range(256)) * 20),
    "project/empty": DIR,
}
"""The local folder the upload tests send: nested directories, an empty one,
binary content and names that break naive shell quoting. No symlink, so that
archive mode and the per-file transfer must give the same tree (per file, a
symlink to a file travels as its target's content)."""

PROJECT_ORDER = [
    "..",
    "empty",
    "src",
    "-leading-dash.txt",
    "it's \"quoted\".txt",
    "README.md",
    "star*?[ab].txt",
    "with space.txt",
    "ünïcødé-日本.txt",
]
"""PROJECT's folder as a panel lists it."""

DOCS: dict[str, Node] = {
    "docs": DIR,
    "docs/guide.md": FILE(b"# guide\n"),
    "docs/it's here.txt": FILE(b"remote quote\n"),
    "docs/日本語.txt": FILE("日本語\n".encode()),
    "docs/img": DIR,
    "docs/img/logo.bin": FILE(bytes(reversed(range(256))) * 12),
    "docs/img/none": DIR,
}
"""The remote folder the download tests fetch."""

REMOTE_FILES: dict[str, Node] = {
    "notes.txt": FILE(b"remote notes\n"),
    ".profile": FILE(b"export X=1\n"),
    "Beta.md": FILE(b"# beta\n"),
}
REMOTE_ORDER = ["..", "docs", ".profile", "Beta.md", "notes.txt"]
"""The seeded remote home as the panel lists it: .., directories, files, case-insensitively."""

LOCAL_FILES: dict[str, Node] = {
    "hello.txt": FILE(b"hello from local\n"),
    "other.bin": FILE(bytes(range(200))),
}


def files_in(spec: Spec) -> int:
    return sum(1 for node in spec.values() if node[0] == "file")


def under(spec: Spec, prefix: str) -> dict[str, Node]:
    """The part of ``spec`` below ``prefix`` (excluded), relative to it."""
    start = prefix.rstrip("/") + "/"
    return {path[len(start) :]: node for path, node in spec.items() if path.startswith(start)}


def build(root: Path, spec: Spec) -> None:
    """Create ``spec`` below ``root`` (which exists)."""
    for rel, (kind, value) in spec.items():
        path = root / rel
        if kind == "dir":
            path.mkdir()
        elif kind == "symlink":
            assert isinstance(value, str)
            os.symlink(value, path)
        else:
            assert isinstance(value, bytes)
            path.write_bytes(value)


def tree_of(root: Path | str) -> dict[str, Node]:
    """Every path below ``root`` (lstat: symlinks are not followed) as contract nodes."""
    result: dict[str, Node] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                result[rel] = LINK(os.readlink(path))
            elif os.path.isdir(path):
                result[rel] = DIR
            else:
                result[rel] = FILE(Path(path).read_bytes())
    return result


def leftovers(*roots: Path | str) -> list[str]:
    """Every archive-mode temporary (``.twinfm-*``) and every half-written
    btty upload (``*.btty-tmp``) below ``roots``."""
    found = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                if name.startswith(LEFTOVER_PREFIX) or name.endswith(".btty-tmp"):
                    found.append(os.path.join(dirpath, name))
    return found


# -- the world ----------------------------------------------------------------------------


@dataclass
class World:
    """One end-to-end setup: the directories, the fake btty and the backends."""

    local: Path
    store: Path
    home: Path
    rtmp: Path
    systmp: Path
    fake: FakeBtty
    settings: Settings
    local_backend: LocalBackend
    btty: BttyBackend

    @property
    def L(self) -> str:
        return str(self.local)

    @property
    def H(self) -> str:
        return str(self.home)

    def leftovers(self) -> list[str]:
        """Archive temporaries anywhere on either side, plus anything left in
        the test's tempfile directory (the engine's and btty's local staging)."""
        return leftovers(self.local, self.store) + [str(p) for p in self.systmp.iterdir()]

    def kinds_since(self, start: int) -> list[str]:
        """The kinds of the btty calls made since ``start`` calls."""
        return self.fake.kinds[start:]

    def seed(self) -> None:
        """The local directory gets PROJECT and LOCAL_FILES; the remote home
        gets DOCS and REMOTE_FILES."""
        build(self.local, {**PROJECT, **LOCAL_FILES})
        build(self.home, {**DOCS, **REMOTE_FILES})


# -- the app ------------------------------------------------------------------------------


class RecordingEditor:
    """``run_editor`` that records each argv, then runs ``effect`` on it and
    returns ``status``. Never starts a process."""

    def __init__(self, effect: Callable[[list[str]], None] | None = None, status: int = 0) -> None:
        self.effect = effect
        self.status = status
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        if self.effect is not None:
            self.effect(argv)
        return self.status


def real_app(
    left: tuple[Backend, str],
    right: tuple[Backend, str],
    settings: Settings,
    *,
    editor: RecordingEditor | None = None,
    connector: FakeConnector | None = None,
) -> TwinFM:
    """TwinFM with the real engine (``engine`` left to its default), a fake
    connector instead of the backend registry, and a recording editor."""
    return TwinFM(
        left,
        right,
        settings,
        open_backend=connector if connector is not None else FakeConnector(),
        run_editor=editor if editor is not None else RecordingEditor(),
    )


def btty_app(
    world: World,
    backend: BttyBackend | None = None,
    editor: RecordingEditor | None = None,
    right_path: str | None = None,
) -> TwinFM:
    """Left: the local directory. Right: the btty panel at the remote home
    (or ``right_path``)."""
    return real_app(
        (world.local_backend, world.L),
        (backend or world.btty, right_path or world.H),
        world.settings,
        editor=editor,
    )


# -- Pilot steps --------------------------------------------------------------------------


async def settle(pilot: Pilot[Any], timeout: float = 60.0) -> None:
    """Let every worker finish and the UI catch up (tests.ui.support.settle);
    AssertionError after ``timeout`` seconds, e.g. when a worker still waits
    for a dialog answer."""
    try:
        await asyncio.wait_for(ui.settle(pilot), timeout)
    except TimeoutError:
        raise AssertionError(
            f"the app did not settle within {timeout}s; screen stack: {pilot.app.screen_stack}"
        ) from None


async def next_dialog[S: Screen[Any]](
    pilot: Pilot[Any], kind: type[S], previous: Screen[Any] | None = None, timeout: float = 10.0
) -> S:
    """Wait until a ``kind`` dialog other than ``previous`` (one already
    answered, which may still be closing) is on top and ready for input
    (its first widget has focus), and return it."""
    app = pilot.app
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        screen = app.screen
        if (
            isinstance(screen, kind)
            and screen is not previous
            and (screen.focused is not None or not screen.focus_chain)
        ):
            await pilot.pause()
            return screen
        await pilot.pause(0.02)
    raise AssertionError(f"no new {kind.__name__} within {timeout}s; screen stack: {app.screen_stack}")


async def to_right(pilot: Pilot[Any], app: TwinFM) -> None:
    await pilot.press("tab")
    await pilot.pause()
    assert app.active_panel is app.right_panel


def no_checkbox(app: TwinFM) -> bool:
    """The open dialog is the plain target input, without the Archive checkbox."""
    return isinstance(app.screen, InputDialog) and not app.screen.query(Checkbox)


async def accept_transfer(pilot: Pilot[Any], app: TwinFM, *, archive: bool, wait: bool = True) -> None:
    """In the open transfer dialog: leave the target as it is, leave the
    checkbox checked (``archive``) or uncheck it (Tab, Space), and accept.
    ``wait``: let the operation finish (only when no question will come)."""
    assert isinstance(app.screen, TransferDialog), f"no Archive checkbox: {type(app.screen).__name__}"
    box = app.screen.query_one(Checkbox)
    assert str(box.label) == LABEL
    assert box.value is True, "the Archive checkbox starts checked"
    if archive:
        await pilot.press("enter")
    else:
        await pilot.press("tab")
        assert app.screen.focused is box
        await pilot.press("space")
        assert box.value is False
        await pilot.press("tab")
        assert focused_id(app) == "ok"
        await pilot.press("enter")
    if wait:
        await settle(pilot)
    else:
        await pilot.pause()
