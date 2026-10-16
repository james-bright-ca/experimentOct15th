"""One panel: sort order, navigation, selection and the status line, and a
directory that cannot be listed."""

from __future__ import annotations

import os

import pytest

from tests.ui.conftest import FILES, LEFT_ORDER, Tree
from tests.ui.support import (
    SIZE,
    Notes,
    RecordingLocal,
    RecordingMemory,
    StubEngine,
    make_app,
    move_to,
    settle,
)
from twinfm.backend.base import Backend


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
