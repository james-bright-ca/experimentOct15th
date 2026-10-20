"""Ctrl-R lists the panel's directory past any cached listing: its list
worker calls ``backend.invalidate(path)`` and then ``list_dir(path)``, both
off the main thread. Every other listing (start-up, entering and leaving a
directory, Connect, the refresh after an operation) may come from the cache
and never invalidates. The footer shows ^R Refresh."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

from tests.ui.conftest import LoggingLocal, LoggingMemory, Tree, send_bytes
from tests.ui.support import (
    SIZE,
    TRANSFER_ONLY,
    FakeConnector,
    Notes,
    StubEngine,
    assert_alive,
    make_app,
    move_to,
    settle,
)
from twinfm.backend.base import Backend, Entry
from twinfm.backend.memory import MemoryBackend


def _memory_tree(tree: Tree) -> LoggingMemory:
    memory = LoggingMemory(home=tree.L)
    memory.add_file(f"{tree.L}/A.txt", b"aaa")
    memory.add_file(f"{tree.L}/c.txt", b"c")
    memory.add_dir(f"{tree.L}/Sub")
    memory.add_file(f"{tree.L}/Sub/inner.txt", b"inner")
    memory.add_dir(tree.R)
    return memory


@pytest.fixture(params=["local", "memory"])
def logging_backend(request: pytest.FixtureRequest, tree: Tree, logged: LoggingLocal) -> LoggingLocal | LoggingMemory:
    return logged if request.param == "local" else _memory_tree(tree)


class CachingMemory(MemoryBackend):
    """A MemoryBackend with a listing cache, as BttyBackend has one:
    ``list_dir`` answers from the cache once a directory was listed, and
    ``invalidate(path)`` drops ``path`` and everything below it. ``add_*``
    (a change made outside twinfm) does not touch the cache."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cache: dict[str, list[Entry]] = {}
        self.cache_lock = threading.Lock()
        self.store_listings = 0

    def list_dir(self, path: str) -> list[Entry]:
        with self.cache_lock:
            if path in self.cache:
                return list(self.cache[path])
        entries = super().list_dir(path)
        with self.cache_lock:
            self.store_listings += 1
            self.cache[path] = list(entries)
        return entries

    def invalidate(self, path: str | None = None) -> None:
        with self.cache_lock:
            if path is None:
                self.cache.clear()
                return
            for key in list(self.cache):
                if key == path or key.startswith(path.rstrip("/") + "/"):
                    del self.cache[key]


async def test_ctrl_r_invalidates_then_lists_the_panel_directory_off_the_main_thread(
    tree: Tree, logging_backend: LoggingLocal | LoggingMemory, engine: StubEngine
) -> None:
    backend = logging_backend
    app = make_app((backend, tree.L), (backend, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "c.txt")
        mark = len(backend.events)

        await pilot.press("ctrl+r")
        await settle(pilot)

        assert backend.events[mark:] == [("invalidate", tree.L, False), ("list_dir", tree.L, False)]
        assert panel.path == tree.L
        assert panel.cursor_name == "c.txt", "Ctrl-R keeps the cursor on its name"
        assert app.screen is app.main
        assert engine.calls == []


async def test_ctrl_r_byte_from_the_terminal_refreshes(tree: Tree, logged: LoggingLocal, engine: StubEngine) -> None:
    """0x12, what Terminal.app and every other terminal send for Ctrl-R."""
    app = make_app((logged, tree.L), (logged, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        mark = len(logged.events)
        names = await send_bytes(pilot, "\x12")
        await settle(pilot)
        assert logged.events[mark:] == [("invalidate", tree.L, False), ("list_dir", tree.L, False)], names


async def test_ctrl_r_refreshes_the_focused_panel_only(tree: Tree, logged: LoggingLocal, engine: StubEngine) -> None:
    app = make_app((logged, tree.L), (logged, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        mark = len(logged.events)

        await pilot.press("ctrl+r")
        await settle(pilot)

        assert logged.events[mark:] == [("invalidate", tree.R, False), ("list_dir", tree.R, False)]


async def test_ctrl_r_shows_a_change_made_outside_twinfm_which_a_cached_listing_hides(
    tree: Tree, engine: StubEngine
) -> None:
    store = CachingMemory(home="/data")
    store.add_file("/data/a.txt", b"a")
    store.add_dir("/data/sub")
    other = MemoryBackend(home="/other")
    app = make_app((store, "/data"), (other, "/other"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        assert panel.names == ["..", "sub", "a.txt"]

        store.add_file("/data/new.txt", b"made by the IDE")  # outside twinfm: the cache keeps the old listing
        await move_to(pilot, panel, "sub")
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("backspace")
        await settle(pilot)
        assert panel.path == "/data"
        assert "new.txt" not in panel.names, "precondition: leaving and re-entering uses the cached listing"
        listings = store.store_listings

        await pilot.press("ctrl+r")
        await settle(pilot)

        assert panel.names == ["..", "sub", "a.txt", "new.txt"]
        assert store.store_listings == listings + 1
        assert panel.cursor_name == "sub", "the cursor stays where it was"


async def test_navigation_connect_and_the_refresh_after_an_operation_never_invalidate(
    tree: Tree, logged: LoggingLocal, engine: StubEngine
) -> None:
    remote = LoggingMemory(home="/remote")
    remote.add_file("/remote/r.txt", b"r")
    connector = FakeConnector({"btty": remote})
    app = make_app((logged, tree.L), (logged, tree.R), engine, connector)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel

        await move_to(pilot, left, "Sub")
        await pilot.press("enter")  # enter a directory
        await settle(pilot)
        await pilot.press("backspace")  # back to the parent
        await settle(pilot)

        await move_to(pilot, left, "A.txt")
        await pilot.press("f5")  # copy: both panels of this backend list again afterwards
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)
        assert (tree.right / "A.txt").exists()

        await pilot.press("f7")  # mkdir: the panel lists again with the cursor on it
        await pilot.pause()
        await pilot.press(*"made", "enter")
        await settle(pilot)
        assert left.cursor_name == "made"

        await pilot.press("f9")  # connect the left panel to a path on its own backend
        await pilot.pause()
        app.screen.query_one("Input").value = f"local:{tree.L}/Sub"
        await pilot.press("enter")
        await settle(pilot)
        assert left.path == f"{tree.L}/Sub"

        await pilot.press("tab", "f9")  # connect the right panel to another backend
        await pilot.pause()
        app.screen.query_one("Input").value = "btty:"
        await pilot.press("enter")
        await settle(pilot)
        assert app.right_panel.backend is remote

        listed = [path for method, path, _ in logged.events if method == "list_dir"]
        assert {tree.L, f"{tree.L}/Sub", tree.R} <= set(listed), listed
        assert logged.invalidated() == [], logged.events
        assert remote.events and remote.invalidated() == [], remote.events

        await pilot.press("ctrl+r")  # and only Ctrl-R does
        await settle(pilot)
        assert remote.invalidated() == [("invalidate", "/remote", False)]
        assert logged.invalidated() == []


async def test_ctrl_r_on_a_transfer_only_panel_lists_nothing_and_keeps_running(
    tree: Tree, logged: LoggingLocal, engine: StubEngine
) -> None:
    remote = LoggingMemory(TRANSFER_ONLY, home="/drop")
    app = make_app((logged, tree.L), (remote, "/drop"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel is app.right_panel

        await pilot.press("ctrl+r")
        await settle(pilot)

        assert [e for e in remote.events if e[0] == "list_dir"] == []
        assert app.right_panel.query_one("#message").display
        assert_alive(app)


async def test_ctrl_r_in_a_directory_removed_outside_shows_an_error_and_the_panel_stays(
    tree: Tree, logged: LoggingLocal, engine: StubEngine
) -> None:
    app = make_app((logged, tree.L), (logged, tree.R), engine)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "Sub")
        await pilot.press("enter")
        await settle(pilot)
        names = panel.names
        (tree.left / "Sub" / "inner.txt").unlink()
        (tree.left / "Sub").rmdir()

        await pilot.press("ctrl+r")
        await settle(pilot)

        assert [s for _, _, s in notes.items] == ["error"], notes.items
        assert panel.path == f"{tree.L}/Sub"
        assert panel.names == names
        assert_alive(app)


def _footer_keys(app: Any) -> list[FooterKey]:
    return list(app.main.query_one(Footer).query(FooterKey))


@pytest.mark.parametrize("panel", ["left", "right"])
@pytest.mark.parametrize("size", [SIZE, (80, 24)], ids=["wide", "80x24"])
async def test_footer_shows_ctrl_r_refresh_while_a_panel_has_focus(
    tree: Tree, logged: LoggingLocal, engine: StubEngine, panel: str, size: tuple[int, int]
) -> None:
    app = make_app((logged, tree.L), (logged, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        if panel == "right":
            await pilot.press("tab")
            await pilot.pause()
        assert app.active_panel.id == panel
        refresh = [k for k in _footer_keys(app) if k.key == "ctrl+r"]
        assert len(refresh) == 1, [(k.key, k.description) for k in _footer_keys(app)]
        key = refresh[0]
        assert (key.key_display, key.description) == ("^R", "Refresh")
        text = key.render().plain
        assert "^R" in text and "Refresh" in text, text
        width, height = size
        assert key.region.x >= 0 and key.region.right <= width and key.region.y == height - 1, key.region


async def test_clicking_the_footer_refresh_key_refreshes(tree: Tree, logged: LoggingLocal, engine: StubEngine) -> None:
    app = make_app((logged, tree.L), (logged, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        mark = len(logged.events)
        (refresh,) = [k for k in _footer_keys(app) if k.key == "ctrl+r"]
        await pilot.click(refresh)
        await settle(pilot)
        assert logged.events[mark:] == [("invalidate", tree.L, False), ("list_dir", tree.L, False)]


async def test_refresh_backend_argument_is_the_panel_backend(tree: Tree, engine: StubEngine) -> None:
    """With two different stores, Ctrl-R invalidates on the focused panel's store only."""
    left = LoggingMemory(home="/a")
    right = LoggingMemory(home="/b")
    app = make_app((left, "/a"), (right, "/b"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("ctrl+r")
        await settle(pilot)
        assert left.invalidated() == [("invalidate", "/a", False)]
        assert right.invalidated() == []
        backends: list[Backend] = [app.left_panel.backend, app.right_panel.backend]
        assert backends == [left, right]


def test_the_docstrings_name_ctrl_r_as_the_refresh_past_the_cache() -> None:
    import twinfm.ui.app as app_module
    import twinfm.ui.panel as panel_module

    for module in (app_module, panel_module):
        doc = module.__doc__ or ""
        assert "Ctrl-R" in doc, f"{module.__name__}: {doc}"
        assert "cache" in doc, f"{module.__name__}: {doc}"
