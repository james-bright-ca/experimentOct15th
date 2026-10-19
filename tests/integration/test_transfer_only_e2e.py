"""End to end, transfer-only mode (btty_exec False): the btty panel shows the
transfer-only message and never lists; F5 of a local file lands in the btty
panel's directory after the "will be replaced" confirmation, with no
Archive checkbox, through exactly the bscp call; a folder in the selection
is reported unsupported while the files go; F5 in the btty panel downloads
a typed remote path into the local panel's directory. Only ``--put`` and
``--get`` ever reach FakeBtty."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.backend.contract import DIR, FILE
from tests.integration.support import (
    World,
    build,
    next_dialog,
    no_checkbox,
    real_app,
    settle,
    to_right,
    tree_of,
)
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
from twinfm.backend.base import Cap
from twinfm.backend.btty import BttyBackend
from twinfm.config import Settings
from twinfm.ui.app import TwinFM
from twinfm.ui.dialogs import ConfirmDialog, ErrorDialog, InputDialog, SummaryDialog


@pytest.fixture
def transfer_only(world: World) -> Iterator[BttyBackend]:
    """BttyBackend in transfer-only mode over the world's fake, at the remote home."""
    settings = Settings(remote_home=world.H, btty_command=world.fake.command, btty_exec=False)
    backend = BttyBackend(settings, runner=world.fake)
    assert backend.caps == frozenset({Cap.UPLOAD, Cap.DOWNLOAD})
    yield backend
    assert set(world.fake.kinds) <= {"put", "get"}, f"transfer-only sent {world.fake.kinds}"


def app_for(world: World, backend: BttyBackend) -> TwinFM:
    return real_app((world.local_backend, world.L), (backend, world.H), backend.settings)


def seed(world: World) -> None:
    build(world.local, {"A.txt": FILE(b"local A\n"), "b.txt": FILE(b"local b\n"), "Sub": DIR})
    (world.local / "Sub" / "inner.txt").write_bytes(b"inner")
    build(world.home, {"remote.txt": FILE(b"from the remote\n"), "A.txt": FILE(b"remote A, replaced\n")})


async def test_the_btty_panel_shows_the_transfer_only_message_and_never_lists(
    world: World, transfer_only: BttyBackend
) -> None:
    seed(world)
    app = app_for(world, transfer_only)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        assert not right.listable
        assert right.names == []
        assert "transfer-only" in str(right.query_one("#message").content).lower()
        assert right.path == world.H
        assert world.fake.calls == [], "nothing is sent to btty to show the panel"


async def test_f5_of_a_local_file_confirms_replacing_then_lands_in_the_btty_panel_directory(
    world: World, transfer_only: BttyBackend
) -> None:
    seed(world)
    app = app_for(world, transfer_only)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f5")
        await pilot.pause()
        assert no_checkbox(app), f"no Archive checkbox in transfer-only mode: {type(app.screen).__name__}"
        assert input_value(app) == world.H
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmDialog), app.screen_stack
        assert "will be replaced" in dialog_text(app)
        assert focused_id(app) == "no"
        assert world.fake.calls == [], "nothing is sent before the confirmation"
        await pilot.press("y")
        await settle(pilot)

        assert (world.home / "A.txt").read_bytes() == b"local A\n", "replaced, as bscp does"
        assert world.fake.calls == [
            ["btty", "--yes", "--put", f"{world.L}/A.txt", "--to", f"{world.H}/A.txt"]
        ], "exactly the bscp call"
        assert app.screen is app.main
        assert_alive(app)


async def test_declining_the_confirmation_sends_nothing(world: World, transfer_only: BttyBackend) -> None:
    seed(world)
    before = tree_of(world.home)
    app = app_for(world, transfer_only)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "b.txt")
        await pilot.press("f5")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")  # the default: Cancel
        await settle(pilot)

        assert world.fake.calls == []
        assert tree_of(world.home) == before


async def test_a_folder_in_the_selection_is_reported_unsupported_and_the_files_go(
    world: World, transfer_only: BttyBackend
) -> None:
    seed(world)
    app = app_for(world, transfer_only)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        for name in ("Sub", "b.txt"):
            await move_to(pilot, left, name)
            await pilot.press("space")
        await pilot.press("f5")
        await pilot.pause()
        assert no_checkbox(app), "a folder does not bring the checkbox either"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("y")
        await next_dialog(pilot, SummaryDialog)

        assert dialog_title(app) == "Copy: some items failed"
        text = dialog_text(app)
        assert f"{world.L}/Sub" in text and "not supported" in text, text
        await pilot.press("enter")
        await settle(pilot)

        assert (world.home / "b.txt").read_bytes() == b"local b\n"
        assert not (world.home / "Sub").exists()
        assert world.fake.kinds == ["put"], "only the file was sent"
        assert left.selected == {"Sub"}, "the copied file is unselected, the failed folder stays selected"
        assert app.screen is app.main


async def test_f5_in_the_btty_panel_downloads_a_typed_path_into_the_local_panel_directory(
    world: World, transfer_only: BttyBackend
) -> None:
    seed(world)
    app = app_for(world, transfer_only)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)
        assert dialog_title(app) == "Download"
        assert input_value(app) == world.H + "/"
        await pilot.press(*"remote.txt", "enter")  # appended to the prefilled directory
        await settle(pilot)

        assert (world.local / "remote.txt").read_bytes() == b"from the remote\n"
        assert "remote.txt" in app.left_panel.names
        assert world.fake.kinds == ["get"]
        get = world.fake.calls[0]
        assert get[:4] == ["btty", "--yes", "--get", f"{world.H}/remote.txt"]
        assert world.fake.out_paths and not world.fake.out_paths[0][1], "--out never existed"
        assert world.leftovers() == []


async def test_a_download_of_a_missing_remote_file_is_an_error_question_and_a_failure(
    world: World, transfer_only: BttyBackend
) -> None:
    seed(world)
    app = app_for(world, transfer_only)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = tree_of(world.local)
        await to_right(pilot, app)
        await pilot.press("f5")
        await pilot.pause()
        await pilot.press(*"missing.txt", "enter")
        await next_dialog(pilot, ErrorDialog)
        assert f"{world.H}/missing.txt" in dialog_text(app)
        await pilot.press("enter")  # Skip
        await next_dialog(pilot, SummaryDialog)
        assert f"{world.H}/missing.txt" in dialog_text(app)
        await pilot.press("enter")
        await settle(pilot)

        assert tree_of(world.local) == before
        assert world.fake.kinds == ["get"]
        assert world.leftovers() == []
        assert app.screen is app.main
        assert_alive(app)


@pytest.mark.parametrize(("key", "what"), [("f8", "Delete"), ("f6", "Move"), ("f7", "Make directory"), ("ctrl+n", "Rename")])
async def test_operations_inside_the_transfer_only_panel_are_not_available(
    world: World, transfer_only: BttyBackend, key: str, what: str
) -> None:
    seed(world)
    app = app_for(world, transfer_only)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await pilot.press(key)
        await settle(pilot)

        assert app.screen is app.main
        assert world.fake.calls == []
        assert f"{what} is not available in transfer-only mode" in notes.messages(), notes.items
