"""End to end, the btty listing cache under the real app: a change made
behind twinfm's back on the remote stays hidden (also after leaving and
re-entering the directory) until Ctrl-R, while every change twinfm makes
itself (upload, archive upload, rename, delete, mkdir, move) shows at once,
also in directories whose listing was cached before the change.

"Cached" is observed through FakeBtty: a listing served from the cache sends
no ``list`` script."""

from __future__ import annotations

import pytest

from tests.backend.contract import DIR, FILE
from tests.integration.support import (
    LOCAL_FILES,
    PROJECT,
    PROJECT_ORDER,
    World,
    accept_transfer,
    btty_app,
    build,
    next_dialog,
    settle,
    to_right,
    tree_of,
    under,
)
from tests.ui.support import SIZE, assert_alive, move_to
from twinfm.ui.dialogs import ConflictDialog


async def enter(pilot, panel, name: str) -> None:  # type: ignore[no-untyped-def]
    await move_to(pilot, panel, name)
    await pilot.press("enter")
    await settle(pilot)


async def leave(pilot) -> None:  # type: ignore[no-untyped-def]
    await pilot.press("backspace")
    await settle(pilot)


async def test_a_file_added_behind_twinfms_back_shows_only_after_ctrl_r(world: World) -> None:
    build(world.home, {"cached": DIR, "cached/old.txt": FILE(b"old")})
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        await to_right(pilot, app)
        await enter(pilot, right, "cached")
        assert right.names == ["..", "old.txt"]
        listings = world.fake.count("list")

        (world.home / "cached" / "outside.txt").write_bytes(b"made by something else")
        await leave(pilot)
        assert right.path == world.H and right.cursor_name == "cached"
        await enter(pilot, right, "cached")
        assert right.path == f"{world.H}/cached"
        assert right.names == ["..", "old.txt"], "leaving and re-entering shows the cached listing"
        assert world.fake.count("list") == listings, "both listings came from the cache"

        await pilot.press("ctrl+r")
        await settle(pilot)
        assert right.names == ["..", "old.txt", "outside.txt"]
        assert right.path == f"{world.H}/cached"
        assert world.fake.count("list") == listings + 1, "Ctrl-R listed the directory again"
        assert_alive(app)


async def test_a_file_removed_behind_twinfms_back_stays_listed_until_ctrl_r(world: World) -> None:
    build(world.home, {"a.txt": FILE(b"a"), "b.txt": FILE(b"b")})
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        assert right.names == ["..", "a.txt", "b.txt"]
        (world.home / "b.txt").unlink()
        await to_right(pilot, app)
        await move_to(pilot, right, "b.txt")

        await pilot.press("ctrl+r")
        await settle(pilot)
        assert right.names == ["..", "a.txt"]


async def test_a_file_uploaded_with_f5_shows_without_ctrl_r(world: World) -> None:
    build(world.local, LOCAL_FILES)
    build(world.home, {"cached": DIR, "cached/old.txt": FILE(b"old")})
    app = btty_app(world, right_path=f"{world.H}/cached")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        assert right.names == ["..", "old.txt"]
        listings = world.fake.count("list")
        await move_to(pilot, app.left_panel, "hello.txt")
        await pilot.press("f5")
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)

        assert (world.home / "cached" / "hello.txt").read_bytes() == b"hello from local\n"
        assert right.names == ["..", "hello.txt", "old.txt"]
        assert world.fake.count("list") == listings + 1, "the upload dropped the cached listing"


async def test_a_folder_uploaded_as_an_archive_shows_without_ctrl_r_down_to_its_contents(
    world: World,
) -> None:
    build(world.local, PROJECT)
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        assert right.names == [".."]
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True)
        assert right.names == ["..", "project"]

        await to_right(pilot, app)
        await enter(pilot, right, "project")
        assert right.names == PROJECT_ORDER


@pytest.mark.parametrize("archive", [True, False], ids=["archive-checked", "archive-unchecked"])
async def test_an_upload_merging_into_cached_remote_folders_shows_the_new_files(
    world: World, archive: bool
) -> None:
    """The remote already holds project/ and project/src/ with one file
    each, and both listings are cached; the upload merges into them."""
    build(world.local, PROJECT)
    build(
        world.home,
        {
            "project": DIR,
            "project/README.md": FILE(b"old readme\n"),
            "project/src": DIR,
            "project/src/main.py": FILE(b"old main\n"),
        },
    )
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        await to_right(pilot, app)
        await enter(pilot, right, "project")
        await enter(pilot, right, "src")
        assert right.names == ["..", "main.py"]
        await leave(pilot)
        await leave(pilot)
        assert right.path == world.H

        await pilot.press("tab")
        await pilot.pause()
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=archive, wait=False)
        dialog = None
        for _ in range(2):  # README.md and src/main.py exist: overwrite both
            dialog = await next_dialog(pilot, ConflictDialog, dialog)
            await pilot.click("#overwrite")
        await settle(pilot)

        assert tree_of(world.home / "project") == under(PROJECT, "project")
        await to_right(pilot, app)
        await enter(pilot, right, "project")
        assert right.names == PROJECT_ORDER, "the merged folder's cached listing was dropped"
        await enter(pilot, right, "src")
        assert right.names == ["..", "deep", "main.py"], "and so was its subfolder's"
        (entry,) = [e for e in right.entries if e.name == "main.py"]
        assert entry.size == len(b"print('hello')\n"), "the new content's size is listed"


async def test_renaming_a_cached_folder_does_not_leave_its_old_listing_behind(world: World) -> None:
    build(world.home, {"docs": DIR, "docs/old.txt": FILE(b"old")})
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        await to_right(pilot, app)
        await enter(pilot, right, "docs")
        assert right.names == ["..", "old.txt"]
        await leave(pilot)

        await pilot.press("ctrl+n")
        await pilot.pause()
        await pilot.press(*"manual", "enter")
        await settle(pilot)
        assert right.names == ["..", "manual"]

        await pilot.press("f7")
        await pilot.pause()
        await pilot.press(*"docs", "enter")
        await settle(pilot)
        assert right.cursor_name == "docs"
        await enter(pilot, right, "docs")
        assert right.names == [".."], "the new docs is empty, whatever the old one held"
        await leave(pilot)
        await enter(pilot, right, "manual")
        assert right.names == ["..", "old.txt"]


async def test_deleting_a_cached_folder_and_making_it_again_lists_it_empty(world: World) -> None:
    build(world.home, {"docs": DIR, "docs/old.txt": FILE(b"old"), "docs/sub": DIR})
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        await to_right(pilot, app)
        await enter(pilot, right, "docs")
        await enter(pilot, right, "sub")
        await leave(pilot)
        await leave(pilot)

        await move_to(pilot, right, "docs")
        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)
        assert right.names == [".."]

        await pilot.press("f7")
        await pilot.pause()
        await pilot.press(*"docs", "enter")
        await settle(pilot)
        await enter(pilot, right, "docs")
        assert right.names == [".."], "the deleted folder's cached listing is gone"


async def test_moving_a_file_out_of_a_cached_remote_folder_shows_on_both_sides(world: World) -> None:
    build(world.home, {"docs": DIR, "docs/report.txt": FILE(b"report"), "docs/keep.txt": FILE(b"keep")})
    app = btty_app(world, right_path=f"{world.H}/docs")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        assert right.names == ["..", "keep.txt", "report.txt"]
        await to_right(pilot, app)
        await move_to(pilot, right, "report.txt")
        await pilot.press("f6")
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)

        assert (world.local / "report.txt").read_bytes() == b"report"
        assert not (world.home / "docs" / "report.txt").exists()
        assert right.names == ["..", "keep.txt"]
        assert "report.txt" in app.left_panel.names
