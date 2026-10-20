"""One panel: sort order, navigation, selection and the status line, a
directory that cannot be listed, the Name column's width (no horizontal
scrollbar left behind by a long name), and the main screen's footer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from rich.style import Style
from rich.text import Text
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

from tests.ui.conftest import FILES, LEFT_ORDER, Tree
from tests.ui.support import (
    SIZE,
    TRANSFER_ONLY,
    Notes,
    RecordingLocal,
    RecordingMemory,
    StubEngine,
    input_value,
    make_app,
    move_to,
    settle,
)
from twinfm.backend.base import Backend
from twinfm.ui.panel import Panel, PanelTable


def _memory_like(tree: Tree) -> RecordingMemory:
    """A MemoryBackend holding the same LEFT tree as ``tree``, at the same path."""
    memory = RecordingMemory()
    for name, data in FILES.items():
        memory.add_file(f"{tree.L}/{name}", data)
    for name in ("Sub", "alpha", ".git"):
        memory.add_dir(f"{tree.L}/{name}")
    memory.add_file(f"{tree.L}/Sub/inner.txt", b"inner")
    memory.add_dir(tree.R)
    return memory


@pytest.fixture(params=["local", "memory"])
def backend(request: pytest.FixtureRequest, tree: Tree, local: RecordingLocal) -> Backend:
    return local if request.param == "local" else _memory_like(tree)


# -- layout and sort order ------------------------------------------------------------


async def test_two_panels_side_by_side_with_header_name_size_modified_and_status(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left, right = app.left_panel, app.right_panel
        assert left.region.y == right.region.y
        assert left.region.right <= right.region.x, (left.region, right.region)
        for panel in (left, right):
            header = panel.query_one("#header")
            table = panel.query_one("#table")
            status = panel.query_one("#status")
            assert header.region.y < table.region.y < status.region.y
            assert [str(c.label) for c in table.columns.values()] == ["Name", "Size", "Modified"]
        assert str(left.query_one("#status").content) == f"{len(LEFT_ORDER) - 1} items"


async def test_listing_is_parent_then_directories_then_files_case_insensitive_with_dotfiles(
    tree: Tree, backend: Backend, engine: StubEngine
) -> None:
    app = make_app((backend, tree.L), (backend, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert app.left_panel.names == LEFT_ORDER


async def test_root_has_no_parent_entry(engine: StubEngine) -> None:
    memory = RecordingMemory()
    memory.add_dir("/beta")
    memory.add_file("/Alpha.txt", b"a")
    memory.add_dir("/Gamma")
    app = make_app((memory, "/"), (memory, "/beta"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert app.left_panel.names == ["beta", "Gamma", "Alpha.txt"]
        assert app.right_panel.names == [".."]
        assert str(app.left_panel.query_one("#header").render()) == "memory:/"
        assert str(app.right_panel.query_one("#header").render()) == "memory:/beta"


async def test_listing_runs_on_a_worker_thread(tree: Tree, local: RecordingLocal, engine: StubEngine) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert {path for path, _ in local.listed} == {tree.L, tree.R}
        assert [path for path, on_main in local.listed if on_main] == []


# -- navigation -----------------------------------------------------------------------


async def test_enter_opens_a_directory_and_backspace_returns_with_cursor_on_it(
    tree: Tree, backend: Backend, engine: StubEngine
) -> None:
    app = make_app((backend, tree.L), (backend, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "Sub")

        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == f"{tree.L}/Sub"
        assert panel.names == ["..", "inner.txt"]

        await pilot.press("backspace")
        await settle(pilot)
        assert panel.path == tree.L
        assert panel.cursor_name == "Sub"
        assert panel.names == LEFT_ORDER


async def test_enter_on_a_file_stays_put(tree: Tree, local: RecordingLocal, engine: StubEngine) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("enter")
        await settle(pilot)
        assert app.left_panel.path == tree.L
        assert app.screen is app.main


async def test_tab_moves_focus_to_the_other_panel_and_back(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert app.active_panel is app.left_panel
        assert app.focused is not None and app.left_panel in app.focused.ancestors_with_self

        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel is app.right_panel
        assert app.other_panel is app.left_panel
        assert app.focused is not None and app.right_panel in app.focused.ancestors_with_self

        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel is app.left_panel


async def test_unreadable_directory_shows_an_error_and_the_panel_stays(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    locked = tree.left / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_bytes(b"s")
    locked.chmod(0)
    try:
        assert not os.access(locked, os.R_OK), "precondition: the test user cannot read a mode-000 directory"
        app = make_app((local, tree.L), (local, tree.R), engine)
        notes = Notes(app)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            panel = app.left_panel
            before = panel.names
            await move_to(pilot, panel, "locked")

            await pilot.press("enter")
            await settle(pilot)

            assert panel.path == tree.L
            assert panel.names == before
            assert panel.cursor_name == "locked"
            errors = [(m, t) for m, t, s in notes.items if s == "error"]
            assert len(errors) == 1, notes.items
            assert "locked" in errors[0][0]
    finally:
        locked.chmod(0o755)


# -- selection ------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["space", "insert", "ctrl+t"])
async def test_selection_key_toggles_cursor_row_and_moves_down(
    tree: Tree, local: RecordingLocal, engine: StubEngine, key: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "A.txt")

        await pilot.press(key)
        await pilot.pause()
        assert panel.selected == {"A.txt"}
        assert panel.cursor_name == "b.txt"

        await pilot.press("up", key)
        await pilot.pause()
        assert panel.selected == set()
        assert panel.cursor_name == "b.txt"


@pytest.mark.parametrize("key", ["space", "insert", "ctrl+t"])
async def test_parent_entry_cannot_be_selected(
    tree: Tree, local: RecordingLocal, engine: StubEngine, key: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        assert panel.cursor_name == ".."
        await pilot.press(key)
        await pilot.pause()
        assert panel.selected == set()
        assert ".." not in panel.selected


async def test_status_line_shows_selected_count_and_total_size(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        status = panel.query_one("#status")
        await move_to(pilot, panel, "A.txt")

        await pilot.press("space", "insert", "ctrl+t")  # A.txt (3), b.txt (2), c.txt (2000)
        await pilot.pause()

        assert panel.selected == {"A.txt", "b.txt", "c.txt"}
        text = str(status.content)
        assert "3 selected" in text, text
        assert "2,005" in text or "2005" in text, f"total size 2005 bytes not shown: {text!r}"


# -- the Name column and the horizontal scrollbar ----------------------------------------
#
# User report: after a folder holding a file whose name is 200 characters long,
# the panel showed a horizontal scrollbar; in another folder of much shorter
# names it still did. Each listing measures the Name column from the rows it
# shows, so a listing scrolls sideways only while a name is too wide for it.

LONG_NAME = "n" * 196 + ".txt"
"""A file name of 200 characters."""

SHORT_NAMES = ["a.txt", "notes-2026-10-19.md", "readme"]
"""File names of at most 20 characters."""


@dataclass
class Folders:
    """``long`` holds LONG_NAME and b.txt; ``short`` holds SHORT_NAMES; both
    are in ``root``, which holds nothing else."""

    root: Path
    long: Path
    short: Path


@pytest.fixture
def folders(tmp_path: Path) -> Folders:
    root = tmp_path / "W"
    long, short = root / "long", root / "short"
    long.mkdir(parents=True)
    short.mkdir()
    (long / LONG_NAME).write_bytes(b"l")
    (long / "b.txt").write_bytes(b"b")
    for name in SHORT_NAMES:
        (short / name).write_bytes(b"s")
    assert len(LONG_NAME) == 200 and max(map(len, SHORT_NAMES)) <= 20
    return Folders(root, long, short)


def listing(panel: Panel) -> PanelTable:
    return panel.query_one(PanelTable)


def scrolls_sideways(panel: Panel) -> tuple[int, bool]:
    """``(max_scroll_x, horizontal scrollbar shown)`` of the panel's listing."""
    table = listing(panel)
    return table.max_scroll_x, table.show_horizontal_scrollbar


def columns(panel: Panel) -> list[tuple[str, int | None, bool]]:
    """``(label, fixed width, auto width)`` of each listing column, in order."""
    return [
        (str(c.label), None if c.auto_width else c.width, c.auto_width) for c in listing(panel).columns.values()
    ]


COLUMNS = [("Name", None, True), ("Size", 8, False), ("Modified", 16, False)]


async def test_leaving_a_folder_with_a_200_character_name_for_short_names_drops_the_horizontal_scrollbar(
    folders: Folders, local: RecordingLocal, engine: StubEngine
) -> None:
    """At 160x40: the long name's listing scrolls sideways; Backspace to the
    parent and Enter on ``short`` lists names of at most 20 characters, and
    then nothing scrolls sideways and no horizontal scrollbar shows. Going
    back into ``long`` makes the listing scroll sideways again."""
    app = make_app((local, str(folders.long)), (local, str(folders.root)), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        assert LONG_NAME in panel.names
        max_x, bar = scrolls_sideways(panel)
        assert max_x > 0 and bar, f"the 200-character name does not scroll sideways: {(max_x, bar)}"

        await pilot.press("backspace")
        await settle(pilot)
        assert panel.path == str(folders.root) and panel.cursor_name == "long"
        await move_to(pilot, panel, "short")
        await pilot.press("enter")
        await settle(pilot)

        assert panel.path == str(folders.short)
        assert panel.names == ["..", *SHORT_NAMES]
        assert scrolls_sideways(panel) == (0, False), (
            f"after leaving the 200-character name the listing still scrolls sideways: {scrolls_sideways(panel)}"
        )
        assert columns(panel) == COLUMNS

        await pilot.press("backspace")
        await settle(pilot)
        await move_to(pilot, panel, "long")
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == str(folders.long)
        assert scrolls_sideways(panel)[0] > 0 and scrolls_sideways(panel)[1]


@pytest.mark.parametrize("how", ["ctrl-r-after-an-outside-rename", "ctrl-n-rename-in-twinfm"])
async def test_a_reload_after_the_long_name_was_renamed_short_drops_the_horizontal_scrollbar(
    folders: Folders, local: RecordingLocal, engine: StubEngine, how: str
) -> None:
    """The same directory listed again, once its only long name became
    ``short.txt``: renamed outside twinfm, then Ctrl-R; or renamed with
    Ctrl-N, after which twinfm lists the directory again with the cursor on
    the new name."""
    app = make_app((local, str(folders.long)), (local, str(folders.root)), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        assert scrolls_sideways(panel)[0] > 0

        if how == "ctrl-r-after-an-outside-rename":
            (folders.long / LONG_NAME).rename(folders.long / "short.txt")
            await pilot.press("ctrl+r")
        else:
            await move_to(pilot, panel, LONG_NAME)
            await pilot.press("ctrl+n")
            await pilot.pause()
            assert input_value(app) == LONG_NAME
            await pilot.press(*"short.txt")  # the prefill is selected: typing replaces it
            await pilot.press("enter")
        await settle(pilot)

        assert panel.path == str(folders.long)
        assert panel.names == ["..", "b.txt", "short.txt"]
        if how == "ctrl-n-rename-in-twinfm":
            assert panel.cursor_name == "short.txt"
        assert scrolls_sideways(panel) == (0, False), (
            f"the reload kept the 200-character Name column: {scrolls_sideways(panel)}"
        )
        assert columns(panel) == COLUMNS


async def test_selecting_in_a_listing_after_a_new_one_styles_the_whole_row(
    folders: Folders, local: RecordingLocal, engine: StubEngine
) -> None:
    """Each listing adds its columns again: selection still restyles the
    row's Name, Size and Modified cells, and the cursor moves down."""
    app = make_app((local, str(folders.long)), (local, str(folders.root)), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("backspace")
        await settle(pilot)
        await move_to(pilot, panel, "short")
        await pilot.press("enter")
        await settle(pilot)
        await move_to(pilot, panel, "a.txt")

        await pilot.press("space")
        await pilot.pause()

        assert panel.selected == {"a.txt"}
        assert panel.cursor_name == "notes-2026-10-19.md"
        table = listing(panel)
        styles = [str(table.get_cell("a.txt", column).style) for column in ("name", "size", "mtime")]
        assert styles == ["bold yellow"] * 3, styles
        assert str(table.get_cell("readme", "name").style) == ""


# -- the footer --------------------------------------------------------------------------

MAIN_FOOTER = (
    ("ctrl+r", "^RRefresh"),
    ("ctrl+o", "^OShell"),
    ("f3", "3View"),
    ("f4", "4Edit"),
    ("f5", "5Copy"),
    ("f6", "6Move"),
    ("ctrl+n", "^NRename"),
    ("f7", "7Mkdir"),
    ("f8", "8Del"),
    ("f9", "9Connect"),
    ("f10", "10Quit"),
)
"""The main screen's footer from left to right: each key and what it reads,
in Midnight Commander's style (the user's choice: short labels, the digit
right against the description, F3 and Ctrl-O shown). ^R Refresh is the
panel's; the rest are the screen's."""

MAIN_KEYS = frozenset(key for key, _ in MAIN_FOOTER)
"""Every key the main screen's footer shows."""

MAIN_FOOTER_LINE = "^RRefresh ^OShell 3View 4Edit 5Copy 6Move ^NRename 7Mkdir 8Del 9Connect 10Quit"
"""The footer line, 78 columns: the keys of MAIN_FOOTER one column apart."""


@pytest.mark.parametrize("focus", ["left", "right", "transfer-only"])
@pytest.mark.parametrize("size", [(80, 24), SIZE], ids=["Terminal.app-default-80x24", "wide"])
async def test_the_footer_shows_the_eleven_keys_in_order_mc_style_within_the_screen_width(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], focus: str
) -> None:
    """At Terminal.app's default 80x24 window (and at 160x40) every
    main-screen footer key is fully visible: in MAIN_FOOTER's order, each
    reading as listed, inside the screen on the bottom line, one column
    apart, the last ending at column 78, no two overlapping and the footer
    not scrolled sideways."""
    right = (RecordingMemory(TRANSFER_ONLY, home="/drop"), "/drop") if focus == "transfer-only" else (local, tree.R)
    app = make_app((local, tree.L), right, engine)
    width, height = size
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        if focus != "left":
            await pilot.press("tab")
            await pilot.pause()
            assert app.active_panel is app.right_panel
        footer = app.main.query_one(Footer)
        keys = sorted(footer.query(FooterKey), key=lambda k: k.region.x)
        shown = [(k.key, k.render().plain, k.region) for k in keys]

        assert [(k.key, k.render().plain) for k in keys] == list(MAIN_FOOTER), shown
        for key in keys:
            region = key.region
            assert region.width == len(key.render().plain), f"{key.key} {region} is cut or padded: {shown}"
            assert region.x >= 0 and region.right <= width, f"{key.key} {region} is outside {width} columns: {shown}"
            assert region.y == height - 1, f"{key.key} {region} is not on the bottom line"
        for before, after in zip(keys, keys[1:]):
            assert after.region.x == before.region.right + 1, f"{before.key} and {after.key} are not one apart: {shown}"
        line = [" "] * width
        for key in keys:
            line[key.region.x : key.region.right] = key.render().plain
        assert "".join(line).rstrip() == MAIN_FOOTER_LINE
        assert keys[0].region.x == 0 and keys[-1].region.right == 78, shown
        assert footer.max_scroll_x == 0, f"the footer scrolls sideways by {footer.max_scroll_x}: {shown}"


@pytest.mark.parametrize("size", [(80, 24), SIZE], ids=["80x24", "wide"])
async def test_each_footer_key_is_bold_and_styled_apart_from_its_description(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    """With no space between them, the key (``3``) and its description
    (``View``) still read apart: the key is bold, in a style of its own."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        for key in app.main.query_one(Footer).query(FooterKey):
            text = key.render()
            assert text.plain == f"{key.key_display}{key.description}", text.plain

            def style_at(offset: int, text: Text = text) -> Style:
                styles = [span.style for span in text.spans if span.start <= offset < span.end]
                return Style.combine(s if isinstance(s, Style) else Style.parse(s) for s in styles)

            key_style, description_style = style_at(0), style_at(len(key.key_display))
            assert key_style.bold, f"{key.key}: the key {key.key_display!r} is not bold ({key_style})"
            assert key_style != description_style, f"{key.key}: key and description look the same ({key_style})"
