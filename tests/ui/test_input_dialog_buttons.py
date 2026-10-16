"""The OK and Cancel buttons of every InputDialog close it once and keep the app running.

User-reported (live terminal): F9 (Connect), then the Cancel button, crashed
the app with ScreenStackError "Can't pop screen; there must be at least one
screen on the stack" from dialogs.py on_button_pressed -> dismiss. The same
dialog class serves F5/F6 (copy/move target), Shift-F6 (rename), F7 (mkdir)
and the transfer-only download, so each is checked with both buttons, by mouse
and by keyboard (Tab to the button, then Enter).
"""

from __future__ import annotations

import pytest

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
    input_value,
    make_app,
    move_to,
    settle,
)
from twinfm.ui.dialogs import InputDialog

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

        await pilot.press(*opener)
        await pilot.pause()
        assert isinstance(app.screen, InputDialog), f"{opener} opened {type(app.screen).__name__}"

        await pilot.click("#cancel")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main, f"dialog still open: {app.screen_stack}"
        assert (panel.backend, panel.path, panel.names) == before
        assert connector.calls == []


async def test_connect_cancel_button_by_keyboard_keeps_app_running(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = (app.left_panel.path, app.left_panel.names)

        await pilot.press("f9")
        await pilot.pause()
        await pilot.press("tab", "tab")  # Input -> OK -> Cancel
        await pilot.pause()
        assert app.screen.focused is not None and app.screen.focused.id == "cancel"
        await pilot.press("enter")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main
        assert (app.left_panel.path, app.left_panel.names) == before


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


@pytest.mark.parametrize("op", OPS)
async def test_cancel_button_closes_dialog_without_acting(
    tree: Tree, local: RecordingLocal, engine: StubEngine, op: str
) -> None:
    app, _ = _app_for(op, tree, local, engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = sorted(p.name for p in tree.left.iterdir()), sorted(p.name for p in tree.right.iterdir())
        await _open(pilot, app, op, tree)

        await pilot.click("#cancel")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main, f"dialog still open: {app.screen_stack}"
        assert engine.calls == []
        after = sorted(p.name for p in tree.left.iterdir()), sorted(p.name for p in tree.right.iterdir())
        assert after == before


@pytest.mark.parametrize("op", OPS)
async def test_ok_button_runs_the_operation_once(
    tree: Tree, local: RecordingLocal, engine: StubEngine, op: str
) -> None:
    app, remote = _app_for(op, tree, local, engine)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await _open(pilot, app, op, tree)
        typed = {"rename": "renamed.txt", "mkdir": "newdir", "download": "/drop/remote.txt"}.get(op)
        if typed is not None:
            app.screen.query_one("Input").value = typed
        else:
            assert input_value(app) == tree.R  # accepted unchanged

        await pilot.click("#ok")
        await settle(pilot)

        assert_alive(app)
        assert app.screen is app.main, f"screen stack after OK: {app.screen_stack}"
        assert engine.names() == [{"download": "copy"}.get(op, op)], engine.names()
        expected = {
            "copy": (tree.right / "A.txt").read_bytes() == b"aaa" and (tree.left / "A.txt").exists(),
            "move": (tree.right / "A.txt").exists() and not (tree.left / "A.txt").exists(),
            "rename": (tree.left / "renamed.txt").exists() and not (tree.left / "A.txt").exists(),
            "mkdir": (tree.left / "newdir").is_dir(),
            "download": (tree.left / "remote.txt").read_bytes() == b"remote"
            if (tree.left / "remote.txt").exists()
            else False,
        }[op]
        assert expected, f"{op} did not take effect; notifications: {notes.items}"
