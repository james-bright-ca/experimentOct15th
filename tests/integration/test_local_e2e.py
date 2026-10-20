"""End to end, Local <-> Local: F5, F6, Shift-F6, Ctrl-N, F7 and F8 under
Pilot with the real engine change the directories below tmp_path as the [C]
criteria describe, and F4 edits a local file through the injected
run_editor (never a real vim)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from textual.widgets import DataTable

from tests.integration.support import RecordingEditor, next_dialog, real_app, settle, tree_of
from tests.ui.support import (
    SIZE,
    Notes,
    assert_alive,
    dialog_text,
    dialog_title,
    focused_id,
    input_value,
    move_to,
)
from twinfm.backend.local import LocalBackend
from twinfm.config import Settings
from twinfm.ui.dialogs import ConfirmDialog, ConflictDialog, InputDialog, TransferDialog
from twinfm.ui.panel import Panel

SETTINGS = Settings(btty_exec=False)
"""Only Connect reads the app's settings, and no test here connects."""


class Pair:
    """``left`` holds files, a nested folder, an empty folder and a symlink;
    ``right`` holds one file."""

    def __init__(self, base: Path) -> None:
        self.left = base / "L"
        self.right = base / "R"
        self.left.mkdir()
        self.right.mkdir()
        (self.left / "A.txt").write_bytes(b"aaa")
        (self.left / "b.txt").write_bytes(b"bb")
        (self.left / "c.txt").write_bytes(b"c" * 2000)
        (self.left / "Sub").mkdir()
        (self.left / "Sub" / "inner.txt").write_bytes(b"inner")
        (self.left / "Sub" / "deeper").mkdir()
        (self.left / "Sub" / "deeper" / "leaf.txt").write_bytes(b"leaf")
        (self.left / "alpha").mkdir()
        os.symlink("A.txt", self.left / "link-to-A")
        (self.right / "keep.txt").write_bytes(b"right side")

    @property
    def L(self) -> str:
        return str(self.left)

    @property
    def R(self) -> str:
        return str(self.right)


@pytest.fixture
def pair(tmp_path: Path) -> Pair:
    return Pair(tmp_path.resolve())


def local_app(pair: Pair, editor: RecordingEditor | None = None):  # type: ignore[no-untyped-def]
    local = LocalBackend()
    return real_app((local, pair.L), (local, pair.R), SETTINGS, editor=editor)


def size_cell(panel: Panel, name: str) -> str:
    """The Size column of ``name``'s row, as shown."""
    table = panel.query_one("#table", DataTable)
    return str(table.get_cell(name, "size"))


# -- F5 / F6 ----------------------------------------------------------------------------


async def test_f5_copies_two_selected_files_and_both_panels_show_the_result(pair: Pair) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left, right = app.left_panel, app.right_panel
        await move_to(pilot, left, "A.txt")
        await pilot.press("space", "space")
        assert left.selected == {"A.txt", "b.txt"}

        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog), "two local panels never offer the Archive checkbox"
        assert input_value(app) == pair.R
        await pilot.press("enter")  # accepted unchanged
        await settle(pilot)

        assert app.screen is app.main
        assert tree_of(pair.right) == {
            "A.txt": ("file", b"aaa"),
            "b.txt": ("file", b"bb"),
            "keep.txt": ("file", b"right side"),
        }
        assert (pair.left / "A.txt").read_bytes() == b"aaa", "a copy leaves the source"
        assert left.selected == set(), "the copied files are no longer selected"
        assert right.names == ["..", "A.txt", "b.txt", "keep.txt"]
        assert {"A.txt", "b.txt"} <= set(left.names)
        assert_alive(app)


async def test_f5_copies_a_folder_with_its_whole_tree(pair: Pair) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "Sub")
        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)
        await pilot.press("enter")
        await settle(pilot)

        assert tree_of(pair.right / "Sub") == tree_of(pair.left / "Sub")
        assert tree_of(pair.right / "Sub") == {
            "inner.txt": ("file", b"inner"),
            "deeper": ("dir", None),
            "deeper/leaf.txt": ("file", b"leaf"),
        }
        assert "Sub" in app.right_panel.names


async def test_f6_moves_the_cursor_file_to_the_other_panel(pair: Pair) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left, right = app.left_panel, app.right_panel
        await move_to(pilot, left, "c.txt")

        await pilot.press("f6")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)
        assert dialog_title(app) == "Move"
        assert input_value(app) == pair.R
        await pilot.press("enter")
        await settle(pilot)

        assert not (pair.left / "c.txt").exists()
        assert (pair.right / "c.txt").read_bytes() == b"c" * 2000
        assert "c.txt" not in left.names
        assert right.names == ["..", "c.txt", "keep.txt"]


async def test_f6_moves_a_folder_and_its_tree_leaves_the_source(pair: Pair) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = tree_of(pair.left / "Sub")
        await move_to(pilot, app.left_panel, "Sub")
        await pilot.press("f6")
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)

        assert not (pair.left / "Sub").exists()
        assert tree_of(pair.right / "Sub") == before
        assert "Sub" not in app.left_panel.names
        assert "Sub" in app.right_panel.names


async def test_f5_onto_an_existing_file_opens_the_overwrite_dialog_on_skip_and_skip_keeps_it(
    pair: Pair,
) -> None:
    (pair.right / "A.txt").write_bytes(b"old right A")
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f5")
        await pilot.pause()
        await pilot.press("enter")
        await next_dialog(pilot, ConflictDialog)

        assert focused_id(app) == "skip", "the overwrite dialog opens with focus on Skip"
        assert f"{pair.R}/A.txt" in dialog_text(app)
        await pilot.press("enter")  # Skip
        await settle(pilot)

        assert (pair.right / "A.txt").read_bytes() == b"old right A"
        assert app.screen is app.main
        assert_alive(app)


async def test_overwrite_in_the_overwrite_dialog_replaces_the_file(pair: Pair) -> None:
    (pair.right / "A.txt").write_bytes(b"old right A")
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f5")
        await pilot.pause()
        await pilot.press("enter")
        await next_dialog(pilot, ConflictDialog)

        await pilot.click("#overwrite")
        await settle(pilot)

        assert (pair.right / "A.txt").read_bytes() == b"aaa"
        assert app.screen is app.main


# -- rename, mkdir ------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["shift+f6", "ctrl+n"], ids=["Shift-F6", "Ctrl-N"])
async def test_rename_key_opens_a_prefilled_dialog_and_renames_in_place(pair: Pair, key: str) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        await move_to(pilot, left, "b.txt")

        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, InputDialog), f"{key} opened {type(app.screen).__name__}"
        assert dialog_title(app) == "Rename"
        assert input_value(app) == "b.txt"
        await pilot.press(*"bee.txt")  # the prefill is selected: typing replaces it
        await pilot.press("enter")
        await settle(pilot)

        assert not (pair.left / "b.txt").exists()
        assert (pair.left / "bee.txt").read_bytes() == b"bb"
        assert "bee.txt" in left.names and "b.txt" not in left.names
        assert left.cursor_name == "bee.txt"
        assert left.path == pair.L
        assert tree_of(pair.right) == {"keep.txt": ("file", b"right side")}, "renamed in place"


@pytest.mark.parametrize("key", ["shift+f6", "ctrl+n"], ids=["Shift-F6", "Ctrl-N"])
async def test_rename_key_renames_a_folder_with_its_contents(pair: Pair, key: str) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = tree_of(pair.left / "Sub")
        await move_to(pilot, app.left_panel, "Sub")
        await pilot.press(key)
        await pilot.pause()
        assert input_value(app) == "Sub"
        await pilot.press(*"Renamed", "enter")
        await settle(pilot)

        assert not (pair.left / "Sub").exists()
        assert tree_of(pair.left / "Renamed") == before
        assert app.left_panel.cursor_name == "Renamed"


async def test_f7_creates_a_directory_and_puts_the_cursor_on_it(pair: Pair) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        await pilot.press("f7")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)
        assert input_value(app) == ""
        await pilot.press(*"new dir", "enter")
        await settle(pilot)

        assert (pair.left / "new dir").is_dir()
        assert list((pair.left / "new dir").iterdir()) == []
        assert "new dir" in left.names
        assert left.cursor_name == "new dir"


# -- F8 -----------------------------------------------------------------------------------


async def test_f8_states_count_and_directories_and_the_default_no_deletes_nothing(pair: Pair) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        before = tree_of(pair.left)
        await move_to(pilot, left, "alpha")
        await pilot.press("space", "space")  # alpha, Sub
        await move_to(pilot, left, "A.txt")
        await pilot.press("space")
        assert left.selected == {"alpha", "Sub", "A.txt"}

        await pilot.press("f8")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        text = dialog_text(app)
        assert "3 items" in text and "including 2 directories" in text, text
        assert focused_id(app) == "no", "the delete confirmation opens with focus on No"
        await pilot.press("enter")  # the default
        await settle(pilot)

        assert app.screen is app.main
        assert tree_of(pair.left) == before


async def test_f8_yes_deletes_files_a_symlink_but_not_its_target_and_folders_recursively(pair: Pair) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        for name in ("Sub", "b.txt", "link-to-A"):
            await move_to(pilot, left, name)
            await pilot.press("space")
        assert left.selected == {"Sub", "b.txt", "link-to-A"}

        await pilot.press("f8")
        await pilot.pause()
        assert "including 1 directories" in dialog_text(app)
        await pilot.press("y")
        await settle(pilot)

        assert tree_of(pair.left) == {
            "A.txt": ("file", b"aaa"),
            "c.txt": ("file", b"c" * 2000),
            "alpha": ("dir", None),
        }
        assert left.names == ["..", "alpha", "A.txt", "c.txt"]
        assert left.selected == set()
        assert_alive(app)


# -- F4 -----------------------------------------------------------------------------------


async def test_f4_runs_the_editor_on_the_local_file_and_the_size_column_updates(pair: Pair) -> None:
    def append_a_line(argv: list[str]) -> None:
        with open(argv[-1], "ab") as f:
            f.write(b"a line the editor added\n")

    editor = RecordingEditor(append_a_line)
    app = local_app(pair, editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        await move_to(pilot, left, "A.txt")
        assert size_cell(left, "A.txt") == "3"

        await pilot.press("f4")
        await settle(pilot)

        assert editor.calls == [["vim", f"{pair.L}/A.txt"]], "vim, with the file's absolute path"
        assert (pair.left / "A.txt").read_bytes() == b"aaa" + b"a line the editor added\n"
        assert size_cell(left, "A.txt") == "27", "the panel listed again after the editor exited"
        assert left.cursor_name == "A.txt"
        assert app.screen is app.main
        assert_alive(app)


async def test_f4_uses_visual_over_editor(pair: Pair, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITOR", "nano")
    monkeypatch.setenv("VISUAL", "code --wait")
    editor = RecordingEditor()
    app = local_app(pair, editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "c.txt")
        await pilot.press("f4")
        await settle(pilot)
        assert editor.calls == [["code", "--wait", f"{pair.L}/c.txt"]]


async def test_f4_on_a_folder_warns_and_runs_no_editor(pair: Pair) -> None:
    editor = RecordingEditor()
    app = local_app(pair, editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "Sub")
        await pilot.press("f4")
        await settle(pilot)
        assert editor.calls == []
        assert any("not a file" in m for m in notes.messages()), notes.items
        assert_alive(app)


async def test_folder_between_two_local_panels_offers_no_archive_checkbox(pair: Pair) -> None:
    app = local_app(pair)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "Sub")
        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog) and not isinstance(app.screen, TransferDialog)
        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is app.main
        assert not (pair.right / "Sub").exists()
