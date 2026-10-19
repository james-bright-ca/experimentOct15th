"""F5 copy, F6 move, rename (Ctrl-N, or Shift-F6 where the terminal can send
it), F7 mkdir and F8 delete, driven with the keyboard against a stub engine
on LocalBackend over tmp_path.

Every dialog here is answered with the keyboard (Enter in the input, or the
dialog's own keys); the buttons are covered in test_input_dialog_buttons.py.
"""

from __future__ import annotations

import pytest

from tests.ui.conftest import Tree
from tests.ui.support import (
    SIZE,
    RecordingLocal,
    StubEngine,
    dialog_text,
    dialog_title,
    focused_id,
    input_value,
    make_app,
    move_to,
    settle,
)
from twinfm.ui.dialogs import ConfirmDialog, InputDialog


async def test_f5_copies_two_selected_files_to_the_other_panel(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left, right = app.left_panel, app.right_panel
        await move_to(pilot, left, "A.txt")
        await pilot.press("space", "space")
        await pilot.pause()
        assert left.selected == {"A.txt", "b.txt"}

        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)
        assert input_value(app) == tree.R, "the target is prefilled with the other panel's directory"
        await pilot.press("enter")  # accepted unchanged
        await settle(pilot)

        assert app.screen is app.main
        assert (tree.right / "A.txt").read_bytes() == b"aaa"
        assert (tree.right / "b.txt").read_bytes() == b"bb"
        assert sorted(p.name for p in tree.right.iterdir()) == ["A.txt", "b.txt"]
        assert left.selected == set(), "the copied files are no longer selected"
        assert right.names == ["..", "A.txt", "b.txt"], "the other panel shows the copies"
        assert {"A.txt", "b.txt"} <= set(left.names), "the source panel still lists the originals"
        assert [(c.name, c.args[1], c.args[3]) for c in engine.calls] == [
            ("copy", [f"{tree.L}/A.txt", f"{tree.L}/b.txt"], tree.R)
        ]


async def test_f6_moves_the_cursor_file_to_the_other_panel(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left, right = app.left_panel, app.right_panel
        await move_to(pilot, left, "c.txt")

        await pilot.press("f6")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)
        assert input_value(app) == tree.R
        await pilot.press("enter")
        await settle(pilot)

        assert not (tree.left / "c.txt").exists()
        assert (tree.right / "c.txt").read_bytes() == b"c" * 2000
        assert "c.txt" not in left.names
        assert right.names == ["..", "c.txt"]
        assert engine.names() == ["move"]


# F18 is not a rename key: Textual 8.2.8 can never produce it (see test_terminal_keys.py).
@pytest.mark.parametrize("key", ["shift+f6", "ctrl+n"], ids=["Shift-F6", "Ctrl-N"])
async def test_rename_dialog_is_prefilled_with_the_name_and_renames_in_place(
    tree: Tree, local: RecordingLocal, engine: StubEngine, key: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        await move_to(pilot, left, "b.txt")

        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, InputDialog), f"{key} opened {type(app.screen).__name__}"
        assert input_value(app) == "b.txt"
        await pilot.press(*"bee.txt")  # the prefill is selected: typing replaces it
        assert input_value(app) == "bee.txt"
        await pilot.press("enter")
        await settle(pilot)

        assert not (tree.left / "b.txt").exists()
        assert (tree.left / "bee.txt").read_bytes() == b"bb"
        assert "bee.txt" in left.names and "b.txt" not in left.names
        assert left.path == tree.L
        assert sorted(p.name for p in tree.right.iterdir()) == [], "renamed in place, nothing in the other panel"
        assert [(c.name, c.args[1], c.args[2]) for c in engine.calls] == [
            ("rename", f"{tree.L}/b.txt", "bee.txt")
        ]


async def test_f7_creates_a_directory_and_puts_the_cursor_on_it(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel

        await pilot.press("f7")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)
        assert input_value(app) == ""
        await pilot.press(*"newdir", "enter")
        await settle(pilot)

        assert (tree.left / "newdir").is_dir()
        assert "newdir" in left.names
        assert left.cursor_name == "newdir"


async def test_f8_states_count_and_directories_and_default_no_deletes_nothing(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        before = sorted(p.name for p in tree.left.iterdir())
        await move_to(pilot, left, "alpha")
        await pilot.press("space", "space")  # alpha, Sub
        await move_to(pilot, left, "A.txt")
        await pilot.press("space")
        assert left.selected == {"alpha", "Sub", "A.txt"}

        await pilot.press("f8")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        text = dialog_text(app)
        assert "3 items" in text, text
        assert "including 2 directories" in text, text
        assert focused_id(app) == "no", "the delete confirmation opens with focus on No"

        await pilot.press("enter")  # accept the default
        await settle(pilot)

        assert app.screen is app.main
        assert engine.calls == []
        assert sorted(p.name for p in tree.left.iterdir()) == before
        assert left.selected == {"alpha", "Sub", "A.txt"}


async def test_f8_single_file_states_one_item_and_no_directories(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f8")
        await pilot.pause()
        text = dialog_text(app)
        assert "1 item" in text and "including 0 directories" in text, text
        assert dialog_title(app) == "Delete"
        await pilot.press("escape")
        await settle(pilot)
        assert (tree.left / "A.txt").exists()
        assert engine.calls == []


async def test_f8_answered_yes_deletes_the_selection(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        await move_to(pilot, left, "Sub")
        await pilot.press("space")
        await move_to(pilot, left, "A.txt")
        await pilot.press("space")

        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)

        assert not (tree.left / "Sub").exists()
        assert not (tree.left / "A.txt").exists()
        assert "Sub" not in left.names and "A.txt" not in left.names
        assert left.selected == set()
        assert engine.names() == ["delete"]


@pytest.mark.parametrize(("button", "deleted"), [("#no", False), ("#yes", True)])
async def test_f8_confirmation_buttons_by_mouse(
    tree: Tree, local: RecordingLocal, engine: StubEngine, button: str, deleted: bool
) -> None:
    """ConfirmDialog keeps the shared button handler, unlike InputDialog."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f8")
        await pilot.pause()

        await pilot.click(button)
        await settle(pilot)

        assert app.return_code is None and app.is_running
        assert app.screen is app.main
        assert (tree.left / "A.txt").exists() is not deleted
        assert engine.names() == (["delete"] if deleted else [])


async def test_f8_on_the_parent_entry_does_nothing(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert app.left_panel.cursor_name == ".."
        await pilot.press("f8")
        await pilot.pause()
        assert app.screen is app.main
        assert engine.calls == []
