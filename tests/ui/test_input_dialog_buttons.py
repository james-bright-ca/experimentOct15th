"""The OK and Cancel buttons of every InputDialog close it once and keep the app running.

User-reported (live terminal): F9 (Connect), then the Cancel button, crashed
the app with ScreenStackError "Can't pop screen; there must be at least one
screen on the stack" from dialogs.py on_button_pressed -> dismiss. The same
dialog class serves F5/F6 (copy/move target), rename, F7 (mkdir) and the
transfer-only download, so each is checked with both buttons, by mouse and
by keyboard (Tab to the button, then Enter).

The root cause was a double dismissal: InputDialog's own button handler and
the base Dialog handler both ran. Every Dialog must close at most once per
user action, whichever subclass handles the button, so that is checked on
Dialog itself as well.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from textual.widgets import Button

from tests.ui.conftest import Tree
from tests.ui.support import (
    SIZE,
    TRANSFER_ONLY,
    FakeConnector,
    Notes,
    RecordingLocal,
    RecordingMemory,
    StubEngine,
    assert_alive,
    focused_id,
    input_value,
    make_app,
    move_to,
    settle,
)
from twinfm.ui.dialogs import ConfirmDialog, Dialog, InputDialog

# -- the reported bug: Connect, then Cancel ------------------------------------------


@pytest.mark.parametrize("opener", [("f9",), ("escape", "9")], ids=["F9", "Esc-9"])
async def test_connect_cancel_button_closes_dialog_and_app_keeps_running(
    tree: Tree, local: RecordingLocal, engine: StubEngine, opener: tuple[str, ...]
) -> None:
    connector = FakeConnector()
    app = make_app((local, tree.L), (local, tree.R), engine, connector)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        before = (panel.backend, panel.path, panel.names)
        listed = len(local.listed)

        await pilot.press(*opener)
        await pilot.pause()
        assert isinstance(app.screen, InputDialog), f"{opener} opened {type(app.screen).__name__}"

        await pilot.click("#cancel")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main, f"dialog still open: {app.screen_stack}"
        assert (panel.backend, panel.path, panel.names) == before
        assert connector.calls == []
        # A connect, even to the prefilled location on the same store, would list it.
        assert local.listed[listed:] == [], "Cancel started a connect"


async def test_connect_cancel_button_by_keyboard_keeps_app_running(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = (app.left_panel.path, app.left_panel.names)
        listed = len(local.listed)

        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("tab", "tab")  # Input -> OK -> Cancel
        await pilot.pause()
        assert focused_id(app) == "cancel"
        await pilot.press("enter")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main
        assert (app.left_panel.path, app.left_panel.names) == before
        assert local.listed[listed:] == [], "Cancel started a connect"


async def test_connect_escape_key_closes_dialog_and_app_keeps_running(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = (app.left_panel.path, app.left_panel.names)

        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("escape")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main
        assert (app.left_panel.path, app.left_panel.names) == before


async def test_connect_ok_button_switches_the_panel(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)

        await pilot.press("f9")
        await pilot.pause()
        app.screen.query_one("Input").value = f"local:{tree.R}"
        await pilot.click("#ok")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main
        assert app.left_panel.path == tree.R


# -- the other InputDialog uses -------------------------------------------------------

OPS = ["copy", "move", "rename", "mkdir", "download"]


async def _open(pilot, app, op: str, tree: Tree) -> None:  # type: ignore[no-untyped-def]
    """Open the InputDialog of ``op`` with the cursor on A.txt."""
    if op == "download":
        await pilot.press("tab")  # the transfer-only right panel
        await pilot.pause()
        await pilot.press("f5")
    else:
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press({"copy": "f5", "move": "f6", "rename": "shift+f6", "mkdir": "f7"}[op])
    await pilot.pause()
    assert isinstance(app.screen, InputDialog), f"{op}: {type(app.screen).__name__} is open"


def _app_for(op: str, tree: Tree, local: RecordingLocal, engine: StubEngine):  # type: ignore[no-untyped-def]
    if op == "download":
        remote = RecordingMemory(TRANSFER_ONLY, home="/drop")
        remote.add_file("/drop/remote.txt", b"remote")
        return make_app((local, tree.L), (remote, "/drop"), engine), remote
    return make_app((local, tree.L), (local, tree.R), engine), None


def _listing(tree: Tree) -> tuple[list[str], list[str]]:
    return sorted(p.name for p in tree.left.iterdir()), sorted(p.name for p in tree.right.iterdir())


def _content(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


TYPED = {"rename": "renamed.txt", "mkdir": "newdir", "download": "/drop/remote.txt"}
"""What is typed into the dialog; copy and move accept the prefilled target."""

EFFECT: dict[str, Callable[[Tree], dict[str, object]]] = {
    "copy": lambda t: {"R/A.txt": _content(t.right / "A.txt"), "L/A.txt": _content(t.left / "A.txt")},
    "move": lambda t: {"R/A.txt": _content(t.right / "A.txt"), "L/A.txt": _content(t.left / "A.txt")},
    "rename": lambda t: {"L/renamed.txt": _content(t.left / "renamed.txt"), "L/A.txt": _content(t.left / "A.txt")},
    "mkdir": lambda t: {"L/newdir is a dir": (t.left / "newdir").is_dir()},
    "download": lambda t: {"L/remote.txt": _content(t.left / "remote.txt")},
}
"""The files that show whether ``op`` took effect, read only after it ran."""

EXPECTED: dict[str, dict[str, object]] = {
    "copy": {"R/A.txt": b"aaa", "L/A.txt": b"aaa"},
    "move": {"R/A.txt": b"aaa", "L/A.txt": None},
    "rename": {"L/renamed.txt": b"aaa", "L/A.txt": None},
    "mkdir": {"L/newdir is a dir": True},
    "download": {"L/remote.txt": b"remote"},
}


async def _press_button_by_keyboard(pilot, app, button: str) -> None:  # type: ignore[no-untyped-def]
    """Tab from the input to ``button`` (Input -> OK -> Cancel), then Enter."""
    await pilot.press(*["tab"] * {"ok": 1, "cancel": 2}[button])
    await pilot.pause()
    assert focused_id(app) == button
    await pilot.press("enter")


@pytest.mark.parametrize("by", ["mouse", "keyboard"])
@pytest.mark.parametrize("op", OPS)
async def test_cancel_button_closes_dialog_without_acting(
    tree: Tree, local: RecordingLocal, engine: StubEngine, op: str, by: str
) -> None:
    app, _ = _app_for(op, tree, local, engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = _listing(tree)
        await _open(pilot, app, op, tree)

        if by == "mouse":
            await pilot.click("#cancel")
        else:
            await _press_button_by_keyboard(pilot, app, "cancel")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main, f"dialog still open: {app.screen_stack}"
        assert engine.calls == []
        assert _listing(tree) == before


@pytest.mark.parametrize("by", ["mouse", "keyboard"])
@pytest.mark.parametrize("op", OPS)
async def test_ok_button_runs_the_operation_once(
    tree: Tree, local: RecordingLocal, engine: StubEngine, op: str, by: str
) -> None:
    app, _ = _app_for(op, tree, local, engine)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await _open(pilot, app, op, tree)
        if op in TYPED:
            app.screen.query_one("Input").value = TYPED[op]
        else:
            assert input_value(app) == tree.R  # accepted unchanged

        if by == "mouse":
            await pilot.click("#ok")
        else:
            await _press_button_by_keyboard(pilot, app, "ok")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main, f"screen stack after OK: {app.screen_stack}"
        assert engine.names() == [{"download": "copy"}.get(op, op)], engine.names()
        assert EFFECT[op](tree) == EXPECTED[op], f"{op} did not take effect; notifications: {notes.items}"


# -- Dialog itself closes at most once -------------------------------------------------


class _DismissesInItsOwnHandler(Dialog[str]):
    """A subclass written the way InputDialog was when F9-then-Cancel crashed:
    its own button handler dismisses and does not prevent the default, so
    Textual runs the base Dialog handler, which dismisses again with the
    registered answer."""

    def buttons(self) -> list[tuple[str, str, str]]:
        return [("ok", "OK", "registered-ok"), ("cancel", "Cancel", "registered-cancel")]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(f"own-{event.button.id}")


@pytest.mark.parametrize("by", ["mouse", "keyboard"])
@pytest.mark.parametrize("button", ["ok", "cancel"])
async def test_a_dialog_dismissed_twice_closes_once_over_the_main_screen(
    tree: Tree, local: RecordingLocal, engine: StubEngine, button: str, by: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        results: list[str | None] = []
        app.push_screen(_DismissesInItsOwnHandler("Twice"), results.append)
        await pilot.pause()

        if by == "mouse":
            await pilot.click(f"#{button}")
        else:
            app.screen.query_one(f"#{button}", Button).focus()
            await pilot.pause()
            await pilot.press("enter")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main, f"screen stack: {app.screen_stack}"
        assert results == [f"own-{button}"], "the result callback ran once, with the first answer"


async def test_a_dialog_dismissed_twice_leaves_the_dialog_below_it_open(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    """Over another dialog a second dismissal would not crash: it would close
    the dialog below without an answer (an engine question, a progress dialog)."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        below_results: list[bool | None] = []
        top_results: list[str | None] = []
        below = ConfirmDialog("Below", "the dialog underneath")
        app.push_screen(below, below_results.append)
        await pilot.pause()
        app.push_screen(_DismissesInItsOwnHandler("On top"), top_results.append)
        await pilot.pause()

        await pilot.click("#cancel")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is below, f"screen stack: {app.screen_stack}"
        assert top_results == ["own-cancel"]
        assert below_results == [], "the dialog below was closed without an answer"
