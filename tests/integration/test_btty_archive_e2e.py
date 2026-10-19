"""End to end, more of archive mode through the UI: a typed target that
names a new folder (the item arrives under that name, in both directions),
symlinks in a downloaded folder (kept as links, relative and absolute), a
remote selection with more names than one btty command holds (the names go
up as a file, which is removed too), and a remote folder name that needs
quoting."""

from __future__ import annotations

import os

from tests.backend.contract import DIR, FILE, LINK
from tests.integration.support import (
    ARCHIVE_SCRIPTS,
    DOCS,
    PROJECT,
    World,
    accept_transfer,
    btty_app,
    build,
    settle,
    to_right,
    tree_of,
    under,
)
from tests.ui.support import SIZE, input_value, move_to
from twinfm.backend.btty_scripts import COMMAND_BUDGET
from twinfm.ui.dialogs import TransferDialog


async def test_an_archive_upload_to_a_typed_new_name_arrives_under_that_name(world: World) -> None:
    build(world.local, PROJECT)
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, TransferDialog)
        await pilot.press(*"project copy")  # the prefill is selected: relative to the btty panel
        assert input_value(app) == "project copy"
        await accept_transfer(pilot, app, archive=True)

        assert tree_of(world.home) == {"project copy": DIR, **{
            f"project copy/{rel}": node for rel, node in under(PROJECT, "project").items()
        }}
        assert world.fake.count("unpack") == 1 and world.fake.count("merge") == 1
        assert "project copy" in app.right_panel.names
        assert world.leftovers() == []


async def test_an_archive_download_to_a_typed_new_name_arrives_under_that_name(world: World) -> None:
    build(world.home, DOCS)
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await move_to(pilot, app.right_panel, "docs")
        await pilot.press("f5")
        await pilot.pause()
        await pilot.press(*f"{world.L}/docs-v2")
        await accept_transfer(pilot, app, archive=True)

        assert tree_of(world.local) == {"docs-v2": DIR, **{
            f"docs-v2/{rel}": node for rel, node in under(DOCS, "docs").items()
        }}
        assert world.fake.count("pack") == 1
        assert "docs-v2" in app.left_panel.names
        assert world.leftovers() == []


async def test_an_archive_download_keeps_symlinks_as_links(world: World) -> None:
    build(
        world.home,
        {
            "linked": DIR,
            "linked/target.txt": FILE(b"target"),
            "linked/relative": LINK("target.txt"),
            "linked/absolute": LINK("/etc/hosts"),
            "linked/dangling": LINK("missing.txt"),
        },
    )
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await move_to(pilot, app.right_panel, "linked")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True)

        assert app.screen is app.main
        assert tree_of(world.local / "linked") == {
            "target.txt": FILE(b"target"),
            "relative": LINK("target.txt"),
            "absolute": LINK("/etc/hosts"),
            "dangling": LINK("missing.txt"),
        }
        assert world.fake.count("pack") == 1 and world.fake.count("get") == 1
        assert world.leftovers() == []


async def test_a_remote_selection_too_long_for_one_command_goes_as_one_archive(world: World) -> None:
    names = [f"remote-file-with-a-long-name-{i:03d}.txt" for i in range(60)]
    assert sum(len(n) + 3 for n in names) > COMMAND_BUDGET, "the names alone overflow one command"
    build(world.home, {name: FILE(name.encode()) for name in names})
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        await to_right(pilot, app)
        await move_to(pilot, right, names[0])
        await pilot.press(*["space"] * len(names))
        assert right.selected == set(names)
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True)

        assert tree_of(world.local) == {name: FILE(name.encode()) for name in names}
        assert world.fake.count("pack-from-file") == 1, world.fake.kinds
        assert world.fake.count("get") == 1, "one download, the archive"
        assert world.fake.rejected == []
        assert right.selected == set()
        assert world.leftovers() == [], "the names file went with the archive"
        assert list(world.rtmp.iterdir()) == []


async def test_a_remote_folder_whose_name_needs_quoting_downloads_as_one_archive(world: World) -> None:
    odd = "it's a \"folder\" $(x) -dash"
    build(world.home, {odd: DIR, f"{odd}/inside.txt": FILE(b"inside"), f"{odd}/sub": DIR})
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await move_to(pilot, app.right_panel, odd)
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True)

        assert tree_of(world.local / odd) == {"inside.txt": FILE(b"inside"), "sub": DIR}
        assert world.fake.count("pack") == 1
        assert not set(world.fake.kinds) - {"list", "stat", "walk", "get", "delete", *ARCHIVE_SCRIPTS}
        assert world.leftovers() == []
        assert os.path.isdir(world.home / odd), "a copy leaves the source"
