"""End to end, a local panel and a btty panel in exec mode: the real app and
engine under Pilot, LocalBackend over tmp_path/local and BttyBackend whose
runner is FakeBtty over tmp_path/remote (see conftest.py). The listing
cache and Ctrl-R are in test_btty_cache_e2e.py, conflicts and cancelling in
archive mode in test_btty_conflicts_e2e.py.

* The btty panel lists the fake remote directory, whether the app starts on
  it or connects to it with F9.
* F5 uploads a file directly, and a folder (or several items) as one archive
  when the Archive checkbox stays checked: packed locally, one ``--put``,
  unpacked and merged on the remote. F5 from the btty panel downloads a file
  directly and a folder as one archive packed in remote_tmp. F6 does the
  same and removes the sources.
* With the checkbox unchecked the same folders travel per file and end up
  as the same trees. Archive mode falls back to per file, with the same
  tree, over the size limit, without the remote's tar filters (Unsupported)
  and for a directory without owner rwx; a store without ARCHIVE gets no
  checkbox at all.
* Shift-F6 / Ctrl-N rename, F7 makes a directory and F8 deletes on the
  remote side.
* F4 is refused on the btty panel, and works on the local panel beside it.
* Two btty panels copy and move within the remote (no checkbox, no transfer).

After every operation the result is checked in the fake remote directory, in
the local directory and in both panels, and no ``.twinfm-`` temporary (or
any local staging directory) is left anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.backend.contract import DIR, FILE
from tests.backend.fake_btty import settings_for
from tests.integration.support import (
    ARCHIVE_SCRIPTS,
    DOCS,
    LEFTOVER_PREFIX,
    PROJECT,
    REMOTE_ORDER,
    RecordingEditor,
    World,
    accept_transfer,
    btty_app,
    build,
    files_in,
    no_checkbox,
    real_app,
    settle,
    to_right,
    tree_of,
    under,
)
from tests.ui.support import (
    SIZE,
    FakeConnector,
    Notes,
    assert_alive,
    dialog_text,
    dialog_title,
    focused_id,
    input_value,
    move_to,
)
from twinfm.backend.btty import BttyBackend
from twinfm.backend.memory import MemoryBackend
from twinfm.ui.dialogs import ConfirmDialog, InputDialog, SummaryDialog


# -- listing --------------------------------------------------------------------------------


async def test_the_btty_panel_lists_the_fake_remote_directory(world: World) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        assert right.backend is world.btty
        assert right.path == world.H
        assert right.names == REMOTE_ORDER
        assert world.fake.kinds == ["list"], "one listing of the remote home"
        assert world.fake.calls[0][6] == "41", "the configured btty_timeout"

        await to_right(pilot, app)
        await move_to(pilot, right, "docs")
        await pilot.press("enter")
        await settle(pilot)
        assert right.path == f"{world.H}/docs"
        assert right.names == ["..", "img", "guide.md", "it's here.txt", "日本語.txt"]
        await move_to(pilot, right, "guide.md")
        assert right.cursor_entry is not None and right.cursor_entry.size == len(b"# guide\n")


async def test_f9_connects_a_panel_to_btty_and_it_lists_the_remote_home(world: World) -> None:
    world.seed()
    connector = FakeConnector({"btty": world.btty})
    app = real_app(
        (world.local_backend, world.L), (world.local_backend, world.L), world.settings, connector=connector
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await pilot.press("f9")
        await pilot.pause()
        assert dialog_title(app) == "Connect"
        await pilot.press(*"btty:", "enter")  # the prefill is selected: typing replaces it
        await settle(pilot)

        right = app.right_panel
        assert connector.calls == [("btty", False)], "connected off the main thread"
        assert right.backend is world.btty
        assert right.path == world.H
        assert right.names == REMOTE_ORDER
        assert app.left_panel.backend is world.local_backend
        assert_alive(app)


# -- F5: single files go directly ------------------------------------------------------------


async def test_f5_uploads_a_single_file_directly(world: World) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        start = len(world.fake.kinds)
        await move_to(pilot, app.left_panel, "hello.txt")
        await pilot.press("f5")
        await pilot.pause()
        assert no_checkbox(app), "a single file is never archived"
        assert input_value(app) == world.H
        await pilot.press("enter")
        await settle(pilot)

        assert (world.home / "hello.txt").read_bytes() == b"hello from local\n"
        assert "hello.txt" in app.right_panel.names
        assert "hello.txt" in app.left_panel.names
        done = world.kinds_since(start)
        assert done.count("put") == 1 and not set(done) & set(ARCHIVE_SCRIPTS), done
        (put,) = world.fake.calls_of("put")
        assert put == ["btty", "--yes", "--put", f"{world.L}/hello.txt", "--to", f"{world.H}/hello.txt"]
        assert world.leftovers() == []
        assert app.screen is app.main


async def test_f5_from_the_btty_panel_downloads_a_single_file_directly(world: World) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        start = len(world.fake.kinds)
        await to_right(pilot, app)
        await move_to(pilot, app.right_panel, "notes.txt")
        await pilot.press("f5")
        await pilot.pause()
        assert no_checkbox(app)
        assert input_value(app) == world.L
        await pilot.press("enter")
        await settle(pilot)

        assert (world.local / "notes.txt").read_bytes() == b"remote notes\n"
        assert (world.home / "notes.txt").read_bytes() == b"remote notes\n", "a copy leaves the source"
        assert "notes.txt" in app.left_panel.names
        assert "notes.txt" in app.right_panel.names
        done = world.kinds_since(start)
        assert done.count("get") == 1 and not set(done) & set(ARCHIVE_SCRIPTS), done
        assert world.leftovers() == []


# -- F5 of a folder: archive on and off --------------------------------------------------------


@pytest.mark.parametrize("archive", [True, False], ids=["archive-checked", "archive-unchecked"])
async def test_f5_uploads_a_folder_and_both_modes_give_the_same_tree(world: World, archive: bool) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        start = len(world.fake.kinds)
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f5")
        await pilot.pause()
        assert input_value(app) == world.H
        await accept_transfer(pilot, app, archive=archive)

        assert tree_of(world.home / "project") == under(PROJECT, "project")
        assert tree_of(world.local / "project") == under(PROJECT, "project"), "a copy leaves the source"
        assert "project" in app.right_panel.names
        assert "project" in app.left_panel.names
        done = world.kinds_since(start)
        if archive:
            assert done.count("put") == 1, f"one upload, the archive: {done}"
            assert done.count("unpack") == 1 and done.count("merge") == 1, done
            assert "mkdir" not in done, done
            (put,) = world.fake.calls_of("put")
            name = os.path.basename(put[5])
            assert name.startswith(LEFTOVER_PREFIX) and name.endswith(".tgz"), put
        else:
            assert done.count("put") == files_in(PROJECT), done
            assert not set(done) & set(ARCHIVE_SCRIPTS), done
        assert world.leftovers() == []
        assert app.screen is app.main
        assert_alive(app)


@pytest.mark.parametrize("archive", [True, False], ids=["archive-checked", "archive-unchecked"])
async def test_f5_from_the_btty_panel_downloads_a_folder_and_both_modes_give_the_same_tree(
    world: World, archive: bool
) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        start = len(world.fake.kinds)
        await to_right(pilot, app)
        await move_to(pilot, app.right_panel, "docs")
        await pilot.press("f5")
        await pilot.pause()
        assert input_value(app) == world.L
        await accept_transfer(pilot, app, archive=archive)

        assert tree_of(world.local / "docs") == under(DOCS, "docs")
        assert tree_of(world.home / "docs") == under(DOCS, "docs"), "a copy leaves the source"
        assert "docs" in app.left_panel.names
        assert "docs" in app.right_panel.names
        done = world.kinds_since(start)
        if archive:
            assert done.count("pack") + done.count("pack-from-file") == 1, done
            assert done.count("get") == 1, f"one download, the archive: {done}"
            (get,) = world.fake.calls_of("get")
            assert os.path.dirname(get[3]) == str(world.rtmp), "packed in remote_tmp"
            assert os.path.basename(get[3]).startswith(LEFTOVER_PREFIX), get
        else:
            assert done.count("get") == files_in(DOCS), done
            assert not set(done) & set(ARCHIVE_SCRIPTS), done
        assert world.leftovers() == []
        assert list(world.rtmp.iterdir()) == []
        assert app.screen is app.main


async def test_f5_uploads_several_selected_items_as_one_archive(world: World) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        start = len(world.fake.kinds)
        for name in ("project", "hello.txt", "other.bin"):
            await move_to(pilot, left, name)
            await pilot.press("space")
        assert left.selected == {"project", "hello.txt", "other.bin"}
        await pilot.press("f5")
        await pilot.pause()
        assert "Copy 3 items to:" in dialog_text(app)
        await accept_transfer(pilot, app, archive=True)

        assert tree_of(world.home / "project") == under(PROJECT, "project")
        assert (world.home / "hello.txt").read_bytes() == b"hello from local\n"
        assert (world.home / "other.bin").read_bytes() == bytes(range(200))
        done = world.kinds_since(start)
        assert done.count("put") == 1 and done.count("unpack") == 1, done
        assert done.count("merge") == 3, "one merge per selected item"
        assert left.selected == set(), "what was copied is no longer selected"
        assert {"project", "hello.txt", "other.bin"} <= set(app.right_panel.names)
        assert world.leftovers() == []


async def test_f5_downloads_several_selected_items_as_one_archive(world: World) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        start = len(world.fake.kinds)
        await to_right(pilot, app)
        for name in ("docs", "notes.txt", ".profile"):
            await move_to(pilot, right, name)
            await pilot.press("space")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True)

        assert tree_of(world.local / "docs") == under(DOCS, "docs")
        assert (world.local / "notes.txt").read_bytes() == b"remote notes\n"
        assert (world.local / ".profile").read_bytes() == b"export X=1\n"
        done = world.kinds_since(start)
        assert done.count("get") == 1 and done.count("pack") + done.count("pack-from-file") == 1, done
        assert right.selected == set()
        assert {"docs", "notes.txt", ".profile"} <= set(app.left_panel.names)
        assert world.leftovers() == []


async def test_archive_upload_keeps_a_relative_symlink_as_a_link_and_file_modes(world: World) -> None:
    build(
        world.local,
        {
            "tools": DIR,
            "tools/run.sh": FILE(b"#!/bin/sh\necho run\n"),
            "tools/private.txt": FILE(b"private\n"),
            "tools/link": ("symlink", "run.sh"),
        },
    )
    os.chmod(world.local / "tools" / "run.sh", 0o755)
    os.chmod(world.local / "tools" / "private.txt", 0o600)
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "tools")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True)

        remote = world.home / "tools"
        assert tree_of(remote) == {
            "run.sh": FILE(b"#!/bin/sh\necho run\n"),
            "private.txt": FILE(b"private\n"),
            "link": ("symlink", "run.sh"),
        }
        assert os.stat(remote / "run.sh").st_mode & 0o777 == 0o755
        assert os.stat(remote / "private.txt").st_mode & 0o777 == 0o600
        assert world.fake.count("unpack") == 1
        assert world.leftovers() == []


# -- F6 ------------------------------------------------------------------------------------------


async def test_f6_moves_a_folder_to_btty_as_one_archive_and_removes_the_local_source(world: World) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f6")
        await pilot.pause()
        assert dialog_title(app) == "Move"
        await accept_transfer(pilot, app, archive=True)

        assert tree_of(world.home / "project") == under(PROJECT, "project")
        assert not (world.local / "project").exists()
        assert "project" not in app.left_panel.names
        assert "project" in app.right_panel.names
        assert world.fake.count("merge") == 1
        assert world.leftovers() == []


async def test_f6_moves_a_folder_from_btty_as_one_archive_and_removes_the_remote_source(world: World) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await move_to(pilot, app.right_panel, "docs")
        await pilot.press("f6")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True)

        assert tree_of(world.local / "docs") == under(DOCS, "docs")
        assert not (world.home / "docs").exists()
        assert "docs" not in app.right_panel.names
        assert "docs" in app.left_panel.names
        assert world.fake.count("pack") + world.fake.count("pack-from-file") == 1
        assert world.leftovers() == []


@pytest.mark.parametrize("archive", [True, False], ids=["archive-checked", "archive-unchecked"])
async def test_f6_moves_several_local_items_to_btty_and_removes_every_source(world: World, archive: bool) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        for name in ("project", "hello.txt"):
            await move_to(pilot, left, name)
            await pilot.press("space")
        await pilot.press("f6")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=archive)

        assert tree_of(world.home / "project") == under(PROJECT, "project")
        assert (world.home / "hello.txt").read_bytes() == b"hello from local\n"
        assert tree_of(world.local) == {"other.bin": FILE(bytes(range(200)))}
        assert left.names == ["..", "other.bin"]
        assert world.leftovers() == []


# -- fallbacks: archive mode asked for, per file done --------------------------------------------


def fallback_backend(world: World, reason: str, side: Path, unlock: list[Path]) -> BttyBackend:
    """The btty backend for ``reason``, after setting up the condition
    (``side`` is the directory holding the folder that travels)."""
    if reason == "over-the-size-limit":
        settings = settings_for(world.fake, world.H, str(world.rtmp), archive_max_bytes=64)
        return BttyBackend(settings, runner=world.fake)
    if reason == "no-tar-filter":
        world.fake.tar_filter = False  # the remote's python3 predates tarfile's filters
        return world.btty
    assert reason == "dir-without-owner-write"
    locked = next(p for p in side.rglob("*") if p.is_dir() and not p.is_symlink() and any(p.iterdir()))
    os.chmod(locked, 0o555)
    unlock.append(locked)
    return world.btty


FALLBACKS = ["over-the-size-limit", "no-tar-filter", "dir-without-owner-write"]


@pytest.mark.parametrize("reason", FALLBACKS)
async def test_a_folder_upload_falls_back_to_per_file_with_the_same_tree(
    world: World, reason: str, unlock: list[Path]
) -> None:
    world.seed()
    backend = fallback_backend(world, reason, world.local / "project", unlock)
    app = btty_app(world, backend)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        start = len(world.fake.kinds)
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True)

        assert app.screen is app.main, f"no failure to report: {dialog_text(app)}"
        assert tree_of(world.home / "project") == under(PROJECT, "project")
        done = world.kinds_since(start)
        assert "merge" not in done, done
        assert done.count("put") - done.count("unpack") == files_in(PROJECT), f"per file: {done}"
        assert world.leftovers() == []
        assert "project" in app.right_panel.names


@pytest.mark.parametrize("reason", FALLBACKS)
async def test_a_folder_download_falls_back_to_per_file_with_the_same_tree(
    world: World, reason: str, unlock: list[Path]
) -> None:
    world.seed()
    backend = fallback_backend(world, reason, world.home / "docs", unlock)
    app = btty_app(world, backend)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        start = len(world.fake.kinds)
        await to_right(pilot, app)
        await move_to(pilot, app.right_panel, "docs")
        await pilot.press("f5")
        await pilot.pause()
        await accept_transfer(pilot, app, archive=True)

        assert app.screen is app.main, f"no failure to report: {dialog_text(app)}"
        assert tree_of(world.local / "docs") == under(DOCS, "docs")
        done = world.kinds_since(start)
        assert done.count("get") == files_in(DOCS), f"per file: {done}"
        assert world.leftovers() == []
        assert list(world.rtmp.iterdir()) == []
        assert "docs" in app.left_panel.names


async def test_a_store_without_archive_gets_no_checkbox_and_the_folder_goes_per_file(world: World) -> None:
    build(world.local, PROJECT)
    memory = MemoryBackend(home="/mem")  # every cap but ARCHIVE
    app = real_app((world.local_backend, world.L), (memory, "/mem"), world.settings)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "project")
        await pilot.press("f5")
        await pilot.pause()
        assert no_checkbox(app), type(app.screen).__name__
        await pilot.press("enter")
        await settle(pilot)

        snapshot = memory.snapshot()
        expected = {
            f"/mem/{rel}": (value if kind == "file" else None) for rel, (kind, value) in PROJECT.items()
        }
        assert {p: v for p, v in snapshot.items() if p.startswith("/mem/")} == expected
        assert "project" in app.right_panel.names
        assert world.leftovers() == []


# -- rename, mkdir, delete on the remote side ---------------------------------------------------


@pytest.mark.parametrize("key", ["shift+f6", "ctrl+n"], ids=["Shift-F6", "Ctrl-N"])
@pytest.mark.parametrize(("old", "new"), [("notes.txt", "renamed notes.txt"), ("docs", "manual")])
async def test_rename_on_the_btty_panel(world: World, key: str, old: str, new: str) -> None:
    world.seed()
    before = tree_of(world.home / old) if (world.home / old).is_dir() else None
    local_before = tree_of(world.local)
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        await to_right(pilot, app)
        await move_to(pilot, right, old)
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, InputDialog), f"{key} opened {type(app.screen).__name__}"
        assert dialog_title(app) == "Rename"
        assert input_value(app) == old
        await pilot.press(*new, "enter")
        await settle(pilot)

        assert not os.path.lexists(world.home / old)
        if before is None:
            assert (world.home / new).read_bytes() == b"remote notes\n"
        else:
            assert tree_of(world.home / new) == before
        assert new in right.names and old not in right.names, "shown without Ctrl-R"
        assert right.cursor_name == new
        assert right.path == world.H
        assert world.fake.count("rename") == 1
        assert tree_of(world.local) == local_before, "renamed in place, nothing arrived locally"


async def test_f7_makes_a_directory_on_the_btty_panel(world: World) -> None:
    world.seed()
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await pilot.press("f7")
        await pilot.pause()
        await pilot.press(*"new folder", "enter")
        await settle(pilot)

        assert (world.home / "new folder").is_dir()
        assert app.right_panel.cursor_name == "new folder"
        assert world.fake.count("mkdir") == 1


async def test_f8_deletes_a_file_and_a_folder_on_the_btty_panel(world: World) -> None:
    world.seed()
    local_before = tree_of(world.local)
    app = btty_app(world)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        right = app.right_panel
        await to_right(pilot, app)
        for name in ("docs", "notes.txt"):
            await move_to(pilot, right, name)
            await pilot.press("space")

        await pilot.press("f8")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        text = dialog_text(app)
        assert "2 items" in text and "including 1 directories" in text, text
        assert focused_id(app) == "no"
        await pilot.press("enter")  # the default, No: nothing happens
        await settle(pilot)
        assert (world.home / "docs").is_dir() and (world.home / "notes.txt").exists()
        assert "delete" not in world.fake.kinds

        await pilot.press("f8")
        await pilot.pause()
        await pilot.press("y")
        await settle(pilot)

        assert sorted(os.listdir(world.home)) == [".profile", "Beta.md"]
        assert right.names == ["..", ".profile", "Beta.md"], "shown without Ctrl-R"
        assert right.selected == set()
        assert "delete" in world.fake.kinds
        assert tree_of(world.local) == local_before, "nothing local changed"
        assert world.leftovers() == []


# -- F4 --------------------------------------------------------------------------------------------


async def test_f4_on_the_btty_panel_is_refused_without_the_editor_or_btty(world: World) -> None:
    world.seed()
    editor = RecordingEditor()
    app = btty_app(world, editor=editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await to_right(pilot, app)
        await move_to(pilot, app.right_panel, "notes.txt")
        calls = len(world.fake.calls)

        await pilot.press("f4")
        await settle(pilot)

        assert editor.calls == []
        assert "F4 edits local files only; copy the file to a local panel to edit it" in notes.messages()
        assert len(world.fake.calls) == calls, "nothing was sent to btty"
        assert app.screen is app.main
        assert_alive(app)


async def test_f4_on_the_local_panel_beside_btty_edits_the_file(world: World) -> None:
    world.seed()

    def append(argv: list[str]) -> None:
        with open(argv[-1], "ab") as f:
            f.write(b"edited\n")

    editor = RecordingEditor(append)
    app = btty_app(world, editor=editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "hello.txt")
        await pilot.press("f4")
        await settle(pilot)

        assert editor.calls == [["vim", f"{world.L}/hello.txt"]]
        assert (world.local / "hello.txt").read_bytes() == b"hello from local\nedited\n"
        entry = app.left_panel.cursor_entry
        assert entry is not None and entry.name == "hello.txt" and entry.size == 24
        assert SummaryDialog not in [type(s) for s in app.screen_stack]


# -- two btty panels: one store ------------------------------------------------------------------


async def test_two_btty_panels_copy_and_move_within_the_remote_without_a_checkbox(world: World) -> None:
    world.seed()
    app = real_app((world.btty, world.H), (world.btty, f"{world.H}/docs"), world.settings)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left, right = app.left_panel, app.right_panel
        await move_to(pilot, left, "notes.txt")
        await pilot.press("f5")
        await pilot.pause()
        assert no_checkbox(app), "no local side: no Archive checkbox"
        assert input_value(app) == f"{world.H}/docs"
        await pilot.press("enter")
        await settle(pilot)

        assert (world.home / "docs" / "notes.txt").read_bytes() == b"remote notes\n"
        assert (world.home / "notes.txt").exists()
        assert "notes.txt" in right.names
        assert world.fake.count("copy") == 1 and world.fake.count("put", "get") == 0

        await move_to(pilot, left, "Beta.md")
        await pilot.press("f6")
        await pilot.pause()
        await pilot.press("enter")
        await settle(pilot)

        assert not (world.home / "Beta.md").exists()
        assert (world.home / "docs" / "Beta.md").read_bytes() == b"# beta\n"
        assert "Beta.md" not in left.names and "Beta.md" in right.names, "both panels show the move"
        assert world.fake.count("rename") == 1 and world.fake.count("put", "get") == 0
        assert world.leftovers() == []
