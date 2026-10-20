"""F9 (Connect): the active panel switches to a typed location, on its own
backend or another registered one, only once that location can be listed.
The dialog is answered with Enter here; its buttons are covered in
test_input_dialog_buttons.py."""

from __future__ import annotations

from tests.ui.conftest import Tree
from tests.ui.support import (
    SIZE,
    TRANSFER_ONLY,
    FakeConnector,
    Notes,
    RecordingLocal,
    RecordingMemory,
    StubEngine,
    input_value,
    make_app,
    settle,
)
from twinfm.ui.dialogs import InputDialog


async def _connect(pilot, app, text: str) -> None:  # type: ignore[no-untyped-def]
    await pilot.press("f9")
    await pilot.pause()
    assert isinstance(app.screen, InputDialog)
    app.screen.query_one("Input").value = text
    await pilot.press("enter")
    await settle(pilot)


async def test_connect_is_prefilled_with_the_panel_location_and_switches_to_a_typed_path(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("f9")
        await pilot.pause()
        assert input_value(app) == f"local:{tree.L}"
        await pilot.press("escape")
        await settle(pilot)

        await _connect(pilot, app, f"{tree.L}/Sub")
        assert app.left_panel.path == f"{tree.L}/Sub"
        assert app.left_panel.names == ["..", "inner.txt"]
        assert app.right_panel.path == tree.R


async def test_connect_to_an_unlistable_path_shows_an_error_and_the_panel_stays(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = app.left_panel.names

        await _connect(pilot, app, f"local:{tree.L}/missing")

        assert app.return_code is None
        assert app.left_panel.path == tree.L
        assert app.left_panel.names == before
        assert [s for _, _, s in notes.items] == ["error"], notes.items


async def test_connect_to_another_backend_kind_opens_it_at_its_home(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    remote = RecordingMemory(TRANSFER_ONLY, home="/home/me", label="fake-btty")
    connector = FakeConnector({"btty": remote})
    app = make_app((local, tree.L), (local, tree.R), engine, connector)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()

        await _connect(pilot, app, "btty:")

        right = app.right_panel
        assert right.backend is remote
        assert right.path == "/home/me"
        assert not right.listable
        assert right.query_one("#message").display
        assert app.left_panel.path == tree.L
        assert connector.calls == [("btty", False)]


async def test_f7_onto_an_existing_name_reports_the_error_and_keeps_running(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("f7")
        await pilot.pause()
        await pilot.press(*"alpha", "enter")
        await settle(pilot)

        assert app.return_code is None
        assert app.screen is app.main
        assert [s for _, _, s in notes.items] == ["error"], notes.items
        assert (tree.left / "alpha").is_dir()
