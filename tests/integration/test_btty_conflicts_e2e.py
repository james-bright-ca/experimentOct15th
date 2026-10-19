"""End to end, archive mode meeting what is already at the destination,
answered through the real dialogs: each existing file is asked about once,
before anything is packed or sent; Skip keeps the old content, Skip all
stops the questions, Cancel (Esc) writes nothing and leaves no archive; a
move keeps a skipped file and its parent folders at the source; a
file/folder clash is an error question and a failure in the summary while
the rest of the batch arrives."""

from __future__ import annotations

from tests.backend.contract import DIR, FILE, Node
from tests.integration.support import (
    ARCHIVE_SCRIPTS,
    DOCS,
    PROJECT,
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
from tests.ui.support import (
    SIZE,
    Notes,
    assert_alive,
    dialog_text,
    dialog_title,
    focused_id,
    move_to,
)
from twinfm.ui.dialogs import ConflictDialog, ErrorDialog, SummaryDialog

EXISTING = {
    "project": DIR,
    "project/README.md": FILE(b"old readme\n"),
    "project/src": DIR,
    "project/src/main.py": FILE(b"old main\n"),
}
"""What the remote home already holds of PROJECT: two of its files, with other content."""


def project_with(old: dict[str, bytes]) -> dict[str, Node]:
    """PROJECT's folder (relative to it) with the files ``old`` keeping their old content."""
    tree = under(PROJECT, "project")
    tree.update({rel: FILE(data) for rel, data in old.items()})
    return tree


async def test_each_existing_file_is_asked_once_before_anything_is_sent_and_skip_keeps_it(
    world: World,
) -> None:
    build(world.local, PROJECT)
    build(world.home, EXISTING)
    app = btty_app(world)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True, wait=False)

        asked = []
        dialog = None
        for _ in range(2):
            dialog = await next_dialog(pilot, ConflictDialog, dialog)
            assert focused_id(app) == "skip"
            assert world.fake.count("put", *ARCHIVE_SCRIPTS) == 0, "asked before anything was packed or sent"
            asked.append(dialog_text(app).splitlines()[1])
            await pilot.press("enter")  # Skip
        await settle(pilot)

        assert sorted(asked) == [f"{world.H}/project/README.md", f"{world.H}/project/src/main.py"]
        assert app.screen is app.main, "no third question, and no failure"
        assert tree_of(world.home / "project") == project_with(
            {"README.md": b"old readme\n", "src/main.py": b"old main\n"}
        )
        assert world.fake.count("put") == 1 and world.fake.count("unpack") == 1, world.fake.kinds
        assert any("2 skipped" in m for m in notes.messages()), notes.items
        assert world.leftovers() == []


async def test_cancel_at_the_first_question_writes_nothing_and_leaves_no_archive(world: World) -> None:
    build(world.local, PROJECT)
    build(world.home, EXISTING)
    before = tree_of(world.home)
    app = btty_app(world)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True, wait=False)
        await next_dialog(pilot, ConflictDialog)

        await pilot.press("escape")  # Cancel the batch
        await settle(pilot)

        assert app.screen is app.main
        assert tree_of(world.home) == before
        assert world.fake.count("put", *ARCHIVE_SCRIPTS) == 0, world.fake.kinds
        assert "Copy cancelled" in notes.messages()
        assert world.leftovers() == []
        assert_alive(app)


async def test_skip_all_on_a_download_asks_once_and_keeps_every_existing_local_file(world: World) -> None:
    build(world.home, DOCS)
    build(world.local, {"docs": DIR, "docs/guide.md": FILE(b"local guide"), "docs/img": DIR})
    (world.local / "docs" / "img" / "logo.bin").write_bytes(b"local logo")
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await move_to(pilot, app.right_panel, "docs")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True, wait=False)
        await next_dialog(pilot, ConflictDialog)
        await pilot.click("#skip_all")
        await settle(pilot)

        assert app.screen is app.main, "Skip all: no second question"
        expected = under(DOCS, "docs")
        expected["guide.md"] = FILE(b"local guide")
        expected["img/logo.bin"] = FILE(b"local logo")
        assert tree_of(world.local / "docs") == expected
        assert world.fake.count("pack") + world.fake.count("pack-from-file") == 1
        assert world.leftovers() == []


async def test_a_move_keeps_a_skipped_file_and_its_parent_folders_at_the_source(world: World) -> None:
    build(world.local, PROJECT)
    build(world.home, {"project": DIR, "project/src": DIR, "project/src/main.py": FILE(b"old main\n")})
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f6")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True, wait=False)
        await next_dialog(pilot, ConflictDialog)
        await pilot.press("enter")  # Skip src/main.py
        await settle(pilot)

        assert tree_of(world.local) == {
            "project": DIR,
            "project/src": DIR,
            "project/src/main.py": FILE(b"print('hello')\n"),
        }, "only the skipped file and its parents stay"
        assert tree_of(world.home / "project") == project_with({"src/main.py": b"old main\n"})
        assert "project" in app.left_panel.names, "the source folder still holds the skipped file"
        assert world.leftovers() == []


async def test_a_file_where_a_folder_goes_is_an_error_and_the_rest_still_arrives(world: World) -> None:
    build(world.local, PROJECT)
    build(world.home, {"project": DIR, "project/src": FILE(b"a file named src")})
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True, wait=False)
        await next_dialog(pilot, ErrorDialog)
        assert focused_id(app) == "skip"
        assert world.fake.count("put", *ARCHIVE_SCRIPTS) == 0, "the clash is found by the scan"
        await pilot.press("enter")  # Skip
        await next_dialog(pilot, SummaryDialog)

        assert "some items failed" in dialog_title(app)
        text = dialog_text(app)
        assert f"{world.L}/project/src" in text, text
        await pilot.press("enter")
        await settle(pilot)

        expected = {
            rel: node for rel, node in under(PROJECT, "project").items() if not rel.startswith("src")
        }
        expected["src"] = FILE(b"a file named src")
        assert tree_of(world.home / "project") == expected
        assert world.fake.count("unpack") == 1, "the rest went as one archive"
        assert app.screen is app.main
        assert world.leftovers() == []
