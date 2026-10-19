"""The UI thread never calls a backend or the engine: listings, the
invalidate of Ctrl-R, connects and every engine call run in worker threads."""

from __future__ import annotations

import threading

from tests.ui.conftest import LoggingLocal, LoggingMemory, Tree
from tests.ui.support import (
    SIZE,
    FakeConnector,
    StubEngine,
    make_app,
    move_to,
    settle,
)


async def test_list_dir_invalidate_open_backend_and_every_engine_call_run_off_the_main_thread(
    tree: Tree, logged: LoggingLocal, engine: StubEngine
) -> None:
    assert threading.current_thread() is threading.main_thread(), "the Pilot drives the app from the main thread"
    local = logged
    remote = LoggingMemory(home="/remote")
    remote.add_file("/remote/r.txt", b"r")
    connector = FakeConnector({"btty": remote})
    app = make_app((local, tree.L), (local, tree.R), engine, connector)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel

        await move_to(pilot, left, "A.txt")
        await pilot.press("f5")  # copy
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)

        await move_to(pilot, left, "b.txt")
        await pilot.press("f6")  # move
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)

        await move_to(pilot, left, "c.txt")
        await pilot.press("shift+f6")  # rename
        await pilot.pause()
        await pilot.press(*"see.txt", "enter")
        await settle(pilot)

        await pilot.press("f7")  # mkdir
        await pilot.pause()
        await pilot.press(*"made", "enter")
        await settle(pilot)

        await move_to(pilot, left, "Zeta.md")
        await pilot.press("f8")  # delete
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)

        await move_to(pilot, left, "Sub")
        await pilot.press("enter")  # list a subdirectory
        await settle(pilot)

        await pilot.press("tab", "f9")  # connect the right panel to another backend
        await pilot.pause()
        app.screen.query_one("Input").value = "btty:"
        await pilot.press("enter")
        await settle(pilot)

        assert app.right_panel.backend is remote and app.right_panel.path == "/remote"
        assert left.path == f"{tree.L}/Sub"

        await pilot.press("ctrl+r")  # refresh the right panel (remote)
        await settle(pilot)
        await pilot.press("tab", "ctrl+r")  # and the left one (local)
        await settle(pilot)

    assert engine.names() == ["copy", "move", "rename", "mkdir", "delete"]
    assert [c.name for c in engine.calls if c.on_main_thread] == []
    assert local.listed, "no listing was recorded"
    assert [p for p, on_main in local.listed if on_main] == []
    assert remote.listed and [p for p, on_main in remote.listed if on_main] == []
    assert local.invalidated() == [("invalidate", f"{tree.L}/Sub", False)]
    assert remote.invalidated() == [("invalidate", "/remote", False)]
    assert connector.calls == [("btty", False)]
