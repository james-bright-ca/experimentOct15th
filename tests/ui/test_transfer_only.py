"""A panel on a backend without LIST (MemoryBackend(caps={UPLOAD, DOWNLOAD}),
which is what btty is with exec off): a message instead of a listing, a
"will be replaced" confirmation before uploading into it, download by typed
path, and no move / mkdir / delete / rename there."""

from __future__ import annotations

import pytest

from tests.ui.conftest import Tree
from tests.ui.support import (
    SIZE,
    TRANSFER_ONLY,
    Notes,
    RecordingLocal,
    RecordingMemory,
    StubEngine,
    dialog_text,
    focused_id,
    input_value,
    make_app,
    move_to,
    settle,
)
from twinfm.ui.dialogs import ConfirmDialog, InputDialog


@pytest.fixture
def remote() -> RecordingMemory:
    remote = RecordingMemory(TRANSFER_ONLY, home="/drop")
    remote.add_file("/drop/remote.txt", b"remote")
    return remote


async def test_transfer_only_panel_shows_a_message_instead_of_a_listing(
    tree: Tree, local: RecordingLocal, remote: RecordingMemory, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (remote, "/drop"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.right_panel
        message = panel.query_one("#message")
        table = panel.query_one("#table")

        assert not panel.listable
        assert panel.names == []
        assert message.display and not table.display
        assert "transfer-only" in str(message.content).lower()
        assert remote.listed == [], "a backend without LIST is never asked to list"
        assert panel.path == "/drop"


async def test_f5_into_transfer_only_panel_confirms_replacing_before_copying(
    tree: Tree, local: RecordingLocal, remote: RecordingMemory, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (remote, "/drop"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")

        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)
        assert input_value(app) == "/drop"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmDialog), app.screen_stack
        assert "will be replaced" in dialog_text(app)
        assert focused_id(app) == "no", "the replace confirmation does not default to replacing"
        assert engine.calls == [], "nothing is copied before the confirmation"

        await pilot.press("y")
        await settle(pilot)

        assert [(c.name, c.args[1], c.args[3]) for c in engine.calls] == [("copy", [f"{tree.L}/A.txt"], "/drop")]
        assert remote.read_file("/drop/A.txt") == b"aaa"
        assert app.screen is app.main


async def test_declining_the_replace_confirmation_copies_nothing(
    tree: Tree, local: RecordingLocal, remote: RecordingMemory, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (remote, "/drop"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = remote.snapshot()
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f5")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)

        await pilot.press("enter")  # the default: Cancel
        await settle(pilot)

        assert engine.calls == []
        assert remote.snapshot() == before
        assert app.screen is app.main


@pytest.mark.parametrize(
    ("key", "what"),
    [("f8", "Delete"), ("f6", "Move"), ("f7", "Make directory"), ("shift+f6", "Rename")],
)
async def test_operations_inside_transfer_only_panel_report_not_available(
    tree: Tree, local: RecordingLocal, remote: RecordingMemory, engine: StubEngine, key: str, what: str
) -> None:
    app = make_app((local, tree.L), (remote, "/drop"), engine)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel is app.right_panel
        before = remote.snapshot()

        await pilot.press(key)
        await settle(pilot)

        assert app.screen is app.main, f"{key} opened {type(app.screen).__name__}"
        assert engine.calls == []
        assert remote.snapshot() == before
        assert any(
            what.lower() in m.lower() and "not available in transfer-only mode" in m for m in notes.messages()
        ), notes.items


async def test_f5_in_transfer_only_panel_downloads_a_typed_path_into_the_other_panel(
    tree: Tree, local: RecordingLocal, remote: RecordingMemory, engine: StubEngine
) -> None:
    app = make_app((local, tree.L), (remote, "/drop"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()

        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)
        assert input_value(app) == "/drop/"
        await pilot.press(*"remote.txt", "enter")  # appended: a path relative to the panel
        await settle(pilot)

        assert [(c.name, c.args[1], c.args[3]) for c in engine.calls] == [
            ("copy", ["/drop/remote.txt"], tree.L)
        ]
        assert (tree.left / "remote.txt").read_bytes() == b"remote"
        assert "remote.txt" in app.left_panel.names
