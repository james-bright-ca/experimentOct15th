"""Esc followed by a digit does what the F-key of that number does (Esc 0 is
F10), for terminals that send F-keys as media keys; F10 quits.

These use Pilot key names. What a legacy terminal's bytes become (a fast
ESC+digit, macOS Option+digit) is covered in test_terminal_keys.py."""

from __future__ import annotations

import pytest

from tests.ui.conftest import DEFAULT_VIEWER, Tree
from tests.ui.support import (
    SIZE,
    FakeCommand,
    RecordingLocal,
    StubEngine,
    dialog_text,
    dialog_title,
    make_app,
    move_to,
    settle,
)
from twinfm.ui.dialogs import InputDialog


def _what_opened(app) -> tuple[str, str, str]:  # type: ignore[no-untyped-def]
    screen = app.screen
    if screen is app.main:
        return ("main", "", "")
    return (type(screen).__name__, dialog_title(app), dialog_text(app))


@pytest.mark.parametrize("digit", ["5", "6", "7", "8", "9"])
async def test_escape_then_digit_opens_what_the_f_key_opens(
    tree: Tree, local: RecordingLocal, engine: StubEngine, digit: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")

        await pilot.press(f"f{digit}")
        await pilot.pause()
        by_f_key = _what_opened(app)
        prefill = app.screen.query_one("Input").value if isinstance(app.screen, InputDialog) else None
        await pilot.press("escape")  # the dialog's own Esc closes it
        await settle(pilot)
        assert app.screen is app.main

        await pilot.press("escape", digit)
        await pilot.pause()
        by_escape = _what_opened(app)

        assert by_f_key[0] != "main", f"F{digit} opened nothing"
        assert by_escape == by_f_key
        if prefill is not None:
            assert app.screen.query_one("Input").value == prefill
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is app.main
        assert engine.calls == []


@pytest.mark.parametrize("digit", ["5", "6", "7", "8", "9"])
async def test_alt_digit_opens_what_the_f_key_opens(
    tree: Tree, local: RecordingLocal, engine: StubEngine, digit: str
) -> None:
    """Alt+digit, as a terminal using the kitty keyboard protocol reports it.
    (A legacy terminal's ESC+digit bytes are covered in test_terminal_keys.py.)"""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press(f"f{digit}")
        await pilot.pause()
        by_f_key = _what_opened(app)
        assert by_f_key[0] != "main", f"F{digit} opened nothing"
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is app.main

        await pilot.press(f"alt+{digit}")
        await pilot.pause()
        assert _what_opened(app) == by_f_key


@pytest.mark.parametrize("keys", [("escape", "3"), ("alt+3",)], ids=["Esc-then-3", "Alt-3"])
async def test_escape_3_and_alt_3_view_what_f3_views(
    tree: Tree, local: RecordingLocal, engine: StubEngine, keys: tuple[str, ...]
) -> None:
    """F3 opens no dialog: it hands the file to the viewer, so the viewer's
    calls are compared instead of the screen."""
    viewer = FakeCommand()
    app = make_app((local, tree.L), (local, tree.R), engine, run_viewer=viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f3")
        await settle(pilot)
        by_f_key = list(viewer.calls)
        assert by_f_key == [[*DEFAULT_VIEWER, f"{tree.L}/A.txt"]]

        await pilot.press(*keys)
        await settle(pilot)

        assert viewer.calls == by_f_key * 2
        assert app.screen is app.main
        assert engine.calls == []


async def test_escape_then_other_key_cancels_the_prefix(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    """Down after Esc ends the prefix, so the 5 that follows is the plain
    character 5, not F5: it opens the find line, and no dialog opens."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        assert not any("5" in name for name in panel.names), "precondition: no listed name contains 5"
        await pilot.press("escape", "down", "5")
        await pilot.pause()
        assert app.screen is app.main, f"the 5 after Esc, Down opened {type(app.screen).__name__}"
        assert panel.finding and panel.find_text == "5"
        assert str(panel.query_one("#find").content) == "Find: 5 no match"
        # No match: the cursor stays where Down put it.
        assert panel.cursor_name == panel.names[1]
        assert engine.calls == []


async def test_f5_through_escape_5_runs_the_same_copy(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("escape", "5")
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)
        assert (tree.right / "A.txt").read_bytes() == b"aaa"
        assert engine.names() == ["copy"]


@pytest.mark.parametrize("keys", [("f10",), ("escape", "0"), ("alt+0",)], ids=["F10", "Esc-0", "Alt-0"])
async def test_quit_keys_exit_the_app(
    tree: Tree, local: RecordingLocal, engine: StubEngine, keys: tuple[str, ...]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press(*keys)
        await pilot.pause()
        assert app.return_code == 0
    assert app.return_code == 0
    assert not app.is_running
