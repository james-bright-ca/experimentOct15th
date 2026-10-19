"""The Archive checkbox of F5 / F6.

Between a local panel and a store with Cap.ARCHIVE (either direction), F5
or F6 of a folder or of several items opens the transfer dialog: the target
input and an "Archive folders and multiple items" checkbox, checked. Its
answer reaches ``engine.copy`` / ``engine.move`` as ``archive=``. Every
other case (a single file, two local panels, a store without ARCHIVE, a
transfer-only panel) keeps the plain input dialog and passes archive=False.

The archive-capable store here is MemoryBackend(ALL_CAPS): the UI decides
on capabilities and on which side is a LocalBackend, so the store's own
archive implementation does not matter. The real btty in exec mode, over
FakeBtty, is end-to-end territory (tests/integration).
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

import pytest
from textual.widgets import Button, Checkbox, Input

from tests.ui.conftest import Tree
from tests.ui.support import (
    SIZE,
    TRANSFER_ONLY,
    Hold,
    RecordingLocal,
    RecordingMemory,
    StubEngine,
    assert_alive,
    dialog_text,
    dialog_title,
    focused_id,
    input_value,
    make_app,
    move_to,
    settle,
    wait_until,
)
from twinfm.backend.base import ALL_CAPS, Backend, Cap
from twinfm.backend.btty import BttyBackend
from twinfm.config import Settings
from twinfm.ops.model import Callbacks, Progress, Report
from twinfm.ui.app import TwinFM
from twinfm.ui.dialogs import ConfirmDialog, InputDialog, ProgressDialog, TransferDialog
from twinfm.ui.panel import Panel

LABEL = "Archive folders and multiple items"
REMOTE = "/remote"


class FlagEngine(StubEngine):
    """StubEngine whose copy and move record the call (archive included)
    and report every path done without transferring anything, so that a
    folder needs no real archive support underneath."""

    def copy(
        self, src: Backend, paths: Sequence[str], dst: Backend, target: str, cb: Callbacks, *, archive: bool = False
    ) -> Report:
        self._record("copy", src, list(paths), dst, target, archive=archive)
        return Report(done=list(paths))

    def move(
        self, src: Backend, paths: Sequence[str], dst: Backend, target: str, cb: Callbacks, *, archive: bool = False
    ) -> Report:
        self._record("move", src, list(paths), dst, target, archive=archive)
        return Report(done=list(paths))


class NoBtty:
    """A btty runner that must never be called."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return 1, "", "test runner: btty must not be called here"


def archive_store(caps: frozenset[Any] = ALL_CAPS) -> RecordingMemory:
    """A non-local store at /remote holding a folder and two files."""
    store = RecordingMemory(caps, home=REMOTE, label="archive-store")
    store.add_dir(f"{REMOTE}/RSub")
    store.add_file(f"{REMOTE}/RSub/x.txt", b"x")
    store.add_file(f"{REMOTE}/r1.txt", b"r1")
    store.add_file(f"{REMOTE}/r2.txt", b"r2")
    return store


SELECTIONS = {
    # side: {kind: names}
    "local": {"folder": ["Sub"], "several": ["A.txt", "b.txt"], "file": ["A.txt"]},
    "remote": {"folder": ["RSub"], "several": ["r1.txt", "r2.txt"], "file": ["r1.txt"]},
}


async def select(pilot: Any, panel: Panel, names: list[str]) -> None:
    """One name: put the cursor on it. Several: select each with Space."""
    if len(names) == 1:
        await move_to(pilot, panel, names[0])
        return
    for name in names:
        await move_to(pilot, panel, name)
        await pilot.press("space")
    assert panel.selected == set(names)


async def open_transfer(pilot: Any, app: TwinFM, key: str, names: list[str], *, right: bool = False) -> None:
    if right:
        await pilot.press("tab")
        await pilot.pause()
    await select(pilot, app.active_panel, names)
    await pilot.press(key)
    await pilot.pause()


def checkbox(app: TwinFM) -> Checkbox:
    return app.screen.query_one(Checkbox)


OPS = {"f5": "copy", "f6": "move"}


def _upload_or_download(tree: Tree, local: RecordingLocal, direction: str) -> tuple[TwinFM, FlagEngine, RecordingMemory, bool, str, str]:
    store = archive_store()
    engine = FlagEngine()
    app = make_app((local, tree.L), (store, REMOTE), engine)
    if direction == "upload":
        return app, engine, store, False, tree.L, REMOTE
    return app, engine, store, True, REMOTE, tree.L


# -- the checkbox is offered, checked --------------------------------------------------------


@pytest.mark.parametrize("what", ["folder", "several"])
@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("key", list(OPS))
async def test_folder_or_several_items_offer_the_archive_checkbox_checked_and_accepting_passes_archive_true(
    tree: Tree, local: RecordingLocal, key: str, direction: str, what: str
) -> None:
    app, engine, store, right, src_dir, dst_dir = _upload_or_download(tree, local, direction)
    names = SELECTIONS["local" if direction == "upload" else "remote"][what]
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, key, names, right=right)

        assert isinstance(app.screen, TransferDialog), f"{key} opened {type(app.screen).__name__}"
        assert dialog_title(app) == OPS[key].capitalize()
        assert input_value(app) == dst_dir, "the target is prefilled with the other panel's directory"
        assert focused_id(app) != "ok" and isinstance(app.screen.focused, Input), "focus starts in the input"
        box = checkbox(app)
        assert str(box.label) == LABEL
        assert box.value is True

        await pilot.press("enter")  # accepted unchanged
        await settle(pilot)

        assert engine.archives() == [(OPS[key], True)]
        (call,) = engine.calls
        assert call.args[1] == [f"{src_dir}/{n}" for n in names] and call.args[3] == dst_dir
        assert app.screen is app.main
        assert_alive(app)


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("key", list(OPS))
async def test_tab_to_the_checkbox_and_space_unchecks_it_and_passes_archive_false(
    tree: Tree, local: RecordingLocal, key: str, direction: str
) -> None:
    app, engine, store, right, src_dir, dst_dir = _upload_or_download(tree, local, direction)
    names = SELECTIONS["local" if direction == "upload" else "remote"]["folder"]
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, key, names, right=right)
        assert isinstance(app.screen, TransferDialog)

        await pilot.press("tab")
        assert app.screen.focused is checkbox(app), f"Tab from the input went to {app.screen.focused}"
        await pilot.press("space")
        assert checkbox(app).value is False
        await pilot.press("tab")
        assert focused_id(app) == "ok"
        await pilot.press("enter")
        await settle(pilot)

        assert engine.archives() == [(OPS[key], False)]
        assert app.screen is app.main
        assert_alive(app)


async def test_unchecked_then_enter_back_in_the_input_passes_archive_false(tree: Tree, local: RecordingLocal) -> None:
    app, engine, *_ = _upload_or_download(tree, local, "upload")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub"])
        await pilot.press("tab", "space", "shift+tab")
        assert isinstance(app.screen.focused, Input)
        await pilot.press("enter")
        await settle(pilot)
        assert engine.archives() == [("copy", False)]


async def test_unchecked_then_ok_by_mouse_passes_archive_false(tree: Tree, local: RecordingLocal) -> None:
    app, engine, *_ = _upload_or_download(tree, local, "upload")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f6", ["Sub"])
        await pilot.click(Checkbox)
        await pilot.pause()
        assert checkbox(app).value is False
        await pilot.click("#ok")
        await settle(pilot)
        assert engine.archives() == [("move", False)]
        assert app.screen is app.main


async def test_space_twice_checks_it_again(tree: Tree, local: RecordingLocal) -> None:
    app, engine, *_ = _upload_or_download(tree, local, "upload")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub"])
        await pilot.press("tab", "space", "space")
        assert checkbox(app).value is True
        await pilot.click("#ok")
        await settle(pilot)
        assert engine.archives() == [("copy", True)]


async def test_enter_on_the_checkbox_toggles_it_and_does_not_submit(tree: Tree, local: RecordingLocal) -> None:
    """TransferDialog: 'on the checkbox Enter and Space toggle it'."""
    app, engine, *_ = _upload_or_download(tree, local, "upload")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub"])
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert isinstance(app.screen, TransferDialog)
        assert checkbox(app).value is False
        assert engine.calls == []


async def test_a_typed_relative_target_resolves_against_the_destination_directory(
    tree: Tree, local: RecordingLocal
) -> None:
    app, engine, store, *_ = _upload_or_download(tree, local, "upload")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub"])
        await pilot.press(*"inbox")  # the prefill is selected: typing replaces it
        assert input_value(app) == "inbox"
        await pilot.press("enter")
        await settle(pilot)
        (call,) = engine.calls
        assert (call.name, call.args[3], call.archive) == ("copy", f"{REMOTE}/inbox", True)


async def test_an_empty_target_does_nothing(tree: Tree, local: RecordingLocal) -> None:
    app, engine, *_ = _upload_or_download(tree, local, "upload")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub"])
        app.screen.query_one(Input).value = "  "
        await pilot.press("enter")
        await settle(pilot)
        assert engine.calls == []
        assert app.screen is app.main
        assert_alive(app)


@pytest.mark.parametrize("checked", [True, False])
async def test_the_archive_answer_survives_the_replace_confirmation(
    tree: Tree, local: RecordingLocal, checked: bool
) -> None:
    """An archive store without STAT: the transfer dialog, then the "will be
    replaced" confirmation, and the engine still gets the checkbox's answer."""
    store = archive_store(ALL_CAPS - {Cap.STAT})
    engine = FlagEngine()
    app = make_app((local, tree.L), (store, REMOTE), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub"])
        assert isinstance(app.screen, TransferDialog)
        if not checked:
            await pilot.press("tab", "space", "shift+tab")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog), app.screen_stack
        assert "will be replaced" in dialog_text(app)
        assert engine.calls == []
        await pilot.press("y")
        await settle(pilot)
        assert engine.archives() == [("copy", checked)]
        assert app.screen is app.main


async def test_several_files_with_the_checkbox_land_in_the_other_panel(tree: Tree, local: RecordingLocal) -> None:
    """With the per-item stub engine underneath, the accepted dialog runs
    the copy and both panels show the result."""
    store = archive_store()
    engine = StubEngine()
    app = make_app((local, tree.L), (store, REMOTE), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["A.txt", "b.txt"])
        assert isinstance(app.screen, TransferDialog)
        await pilot.press("enter")
        await settle(pilot)

        assert engine.archives() == [("copy", True)]
        assert store.read_file(f"{REMOTE}/A.txt") == b"aaa"
        assert store.read_file(f"{REMOTE}/b.txt") == b"bb"
        assert {"A.txt", "b.txt"} <= set(app.right_panel.names)
        assert app.left_panel.selected == set()


# -- the transfer dialog closes exactly once ------------------------------------------------


async def _press_ok_by_mouse(pilot: Any) -> None:
    await pilot.click("#ok")


async def _press_cancel_by_mouse(pilot: Any) -> None:
    await pilot.click("#cancel")


async def _press_escape(pilot: Any) -> None:
    await pilot.press("escape")


async def _press_enter_in_input(pilot: Any) -> None:
    await pilot.press("enter")


async def _press_ok_by_keyboard(pilot: Any) -> None:
    await pilot.press("tab", "tab")  # input -> checkbox -> OK
    assert focused_id(pilot.app) == "ok"
    await pilot.press("enter")


async def _press_cancel_by_keyboard(pilot: Any) -> None:
    await pilot.press("tab", "tab", "tab")  # input -> checkbox -> OK -> Cancel
    assert focused_id(pilot.app) == "cancel"
    await pilot.press("enter")


ANSWERS = {
    "OK-click": (_press_ok_by_mouse, True),
    "Cancel-click": (_press_cancel_by_mouse, False),
    "Esc": (_press_escape, False),
    "Enter-in-input": (_press_enter_in_input, True),
    "OK-keyboard": (_press_ok_by_keyboard, True),
    "Cancel-keyboard": (_press_cancel_by_keyboard, False),
}


@pytest.mark.parametrize("answer", list(ANSWERS))
@pytest.mark.parametrize("key", list(OPS))
async def test_each_answer_closes_the_transfer_dialog_exactly_once(
    tree: Tree, local: RecordingLocal, key: str, answer: str
) -> None:
    press, runs = ANSWERS[answer]
    app, engine, store, *_ = _upload_or_download(tree, local, "upload")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        stack = list(app.screen_stack)
        await open_transfer(pilot, app, key, ["A.txt", "b.txt"])
        assert isinstance(app.screen, TransferDialog)

        await press(pilot)
        await settle(pilot)

        assert_alive(app)
        assert app.screen_stack == stack, f"screen stack {app.screen_stack}, before the dialog {stack}"
        assert app.screen is app.main
        assert engine.archives() == ([(OPS[key], True)] if runs else [])
        if not runs:
            assert app.left_panel.selected == {"A.txt", "b.txt"}, "a cancelled dialog keeps the selection"

        await pilot.press("f7")  # the main screen still answers its keys
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)


async def test_the_transfer_dialog_closes_once_over_another_dialog(tree: Tree, local: RecordingLocal) -> None:
    """A second dismiss of the transfer dialog must not pop what lies below it."""
    app, engine, *_ = _upload_or_download(tree, local, "upload")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        app.push_screen(ConfirmDialog("Below", "the dialog below"))
        await pilot.pause()
        below = app.screen
        dialog = TransferDialog("Copy", "Copy to:", REMOTE)
        results: list[object] = []
        app.push_screen(dialog, results.append)
        await pilot.pause()

        await pilot.click("#ok")
        await pilot.pause()
        dialog.dismiss(None)
        await pilot.pause()

        assert app.screen is below, app.screen_stack
        assert results == [(REMOTE, True)]
        assert_alive(app)


# -- no checkbox ---------------------------------------------------------------------


def _symlink_to_dir(tree: Tree) -> list[str]:
    import os

    os.symlink("Sub", tree.left / "link-to-Sub")
    return ["link-to-Sub"]


@pytest.mark.parametrize("key", list(OPS))
@pytest.mark.parametrize(
    ("direction", "names"),
    [
        ("upload", ["A.txt"]),
        ("download", ["r1.txt"]),
        ("upload", _symlink_to_dir),
    ],
    ids=["single-file-upload", "single-file-download", "single-symlink-to-a-folder"],
)
async def test_a_single_non_folder_item_gets_the_plain_dialog_and_archive_false(
    tree: Tree, local: RecordingLocal, key: str, direction: str, names: Any
) -> None:
    if callable(names):
        names = names(tree)
    app, engine, store, right, *_ = _upload_or_download(tree, local, direction)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, key, names, right=right)

        assert type(app.screen) is InputDialog, f"{key} opened {type(app.screen).__name__}"
        assert list(app.screen.query(Checkbox)) == []
        assert LABEL not in dialog_text(app)
        await pilot.press("enter")
        await settle(pilot)
        assert engine.archives() == [(OPS[key], False)]


def _two_local(tree: Tree, local: RecordingLocal) -> tuple[tuple[Backend, str], tuple[Backend, str]]:
    return (local, tree.L), (local, tree.R)


def _two_local_instances(tree: Tree, local: RecordingLocal) -> tuple[tuple[Backend, str], tuple[Backend, str]]:
    return (local, tree.L), (RecordingLocal(), tree.R)


def _default_memory(tree: Tree, local: RecordingLocal) -> tuple[tuple[Backend, str], tuple[Backend, str]]:
    store = RecordingMemory(home=REMOTE)  # the default caps: every Cap but ARCHIVE
    return (local, tree.L), (store, REMOTE)


def _two_archive_stores_neither_local(
    tree: Tree, local: RecordingLocal
) -> tuple[tuple[Backend, str], tuple[Backend, str]]:
    left = archive_store()
    left.add_dir(f"{REMOTE}/Sub")
    left.add_file(f"{REMOTE}/A.txt", b"a")
    left.add_file(f"{REMOTE}/b.txt", b"b")
    return (left, REMOTE), (archive_store(), REMOTE)


@pytest.mark.parametrize("what", ["folder", "several"])
@pytest.mark.parametrize("key", list(OPS))
@pytest.mark.parametrize(
    "panels",
    [_two_local, _two_local_instances, _default_memory, _two_archive_stores_neither_local],
    ids=["two-local-panels", "two-LocalBackends", "memory-default-caps", "two-archive-stores-none-local"],
)
async def test_without_one_local_and_one_archive_store_folders_get_the_plain_dialog_and_archive_false(
    tree: Tree, local: RecordingLocal, panels: Any, key: str, what: str
) -> None:
    left, right = panels(tree, local)
    engine = FlagEngine()
    app = make_app(left, right, engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, key, SELECTIONS["local"][what])

        assert type(app.screen) is InputDialog, f"{key} opened {type(app.screen).__name__}"
        assert list(app.screen.query(Checkbox)) == []
        assert input_value(app) == right[1]
        await pilot.press("enter")
        await settle(pilot)
        assert engine.archives() == [(OPS[key], False)]
        assert_alive(app)


async def test_two_local_panels_copy_a_folder_exactly_as_before(tree: Tree, local: RecordingLocal) -> None:
    """Between two local panels F5 of a folder is today's copy, through the plain dialog."""
    engine = StubEngine()
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub"])
        assert type(app.screen) is InputDialog
        await pilot.press("enter")
        await settle(pilot)
        assert (tree.right / "Sub" / "inner.txt").read_bytes() == b"inner"
        assert engine.archives() == [("copy", False)]
        assert "Sub" in app.right_panel.names


@pytest.mark.parametrize("what", ["folder", "several"])
async def test_a_transfer_only_memory_panel_gets_the_plain_dialog_and_archive_false(
    tree: Tree, local: RecordingLocal, what: str
) -> None:
    remote = RecordingMemory(TRANSFER_ONLY, home="/drop")
    engine = FlagEngine()
    app = make_app((local, tree.L), (remote, "/drop"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", SELECTIONS["local"][what])
        assert type(app.screen) is InputDialog
        assert list(app.screen.query(Checkbox)) == []
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog), "the replace confirmation still follows"
        await pilot.press("y")
        await settle(pilot)
        assert engine.archives() == [("copy", False)]


async def test_a_transfer_only_btty_panel_gets_the_plain_dialog_and_archive_false(
    tree: Tree, local: RecordingLocal
) -> None:
    runner = NoBtty()
    remote = BttyBackend(Settings(btty_exec=False), runner=runner)
    engine = FlagEngine()
    app = make_app((local, tree.L), (remote, "/mnt/home0/wst/workspaces"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub", "A.txt"])
        assert type(app.screen) is InputDialog
        assert list(app.screen.query(Checkbox)) == []
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("y")
        await settle(pilot)
        assert engine.archives() == [("copy", False)]
        assert runner.calls == [], "the UI never calls btty itself"


async def test_download_from_a_transfer_only_panel_passes_archive_false(tree: Tree, local: RecordingLocal) -> None:
    remote = RecordingMemory(TRANSFER_ONLY, home="/drop")
    remote.add_file("/drop/remote.txt", b"remote")
    engine = FlagEngine()
    app = make_app((local, tree.L), (remote, "/drop"), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("f5")
        await pilot.pause()
        assert type(app.screen) is InputDialog and dialog_title(app) == "Download"
        await pilot.press(*"remote.txt", "enter")
        await settle(pilot)
        assert engine.archives() == [("copy", False)]


# -- the progress dialog shows the stage ------------------------------------------------

STAGES = ["Packing", "Transferring", "Unpacking", "Merging"]


class StageEngine(FlagEngine):
    """copy reports each archive stage in turn and waits for the test to
    release it, so the test can read the progress dialog at every stage."""

    def __init__(self) -> None:
        super().__init__()
        self.release = [threading.Event() for _ in STAGES]
        self.reported: list[str] = []

    def copy(
        self, src: Backend, paths: Sequence[str], dst: Backend, target: str, cb: Callbacks, *, archive: bool = False
    ) -> Report:
        self._record("copy", src, list(paths), dst, target, archive=archive)
        for index, stage in enumerate(STAGES):
            cb.progress(Progress("copy", paths[0], 0, len(paths), stage=stage))
            self.reported.append(stage)
            if not self.release[index].wait(10):
                break
        return Report(done=list(paths))


async def test_the_progress_dialog_shows_each_archive_stage(tree: Tree, local: RecordingLocal) -> None:
    store = archive_store()
    engine = StageEngine()
    app = make_app((local, tree.L), (store, REMOTE), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub"])
        assert isinstance(app.screen, TransferDialog)
        await pilot.press("enter")
        try:
            for index, stage in enumerate(STAGES):
                assert await wait_until(pilot, lambda: len(engine.reported) > index), engine.reported
                await pilot.pause()
                assert isinstance(app.screen, ProgressDialog), app.screen_stack
                text = dialog_text(app)
                assert stage in text, f"stage {stage!r} not shown: {text!r}"
                assert f"{tree.L}/Sub" in text, text
                assert " done" not in text.split("\n")[1], f"per-item counts shown for a stage: {text!r}"
                engine.release[index].set()
        finally:
            for event in engine.release:
                event.set()
        await settle(pilot)
        assert app.screen is app.main
        assert engine.archives() == [("copy", True)]


async def test_without_a_stage_the_progress_dialog_shows_items_done(tree: Tree, local: RecordingLocal) -> None:
    hold = Hold()
    engine = StubEngine(between=hold)
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["A.txt", "b.txt"])
        await pilot.press("enter")
        try:
            assert await wait_until(pilot, hold.reached.is_set)
            assert isinstance(app.screen, ProgressDialog)
            text = dialog_text(app)
            assert "0 of 2 done" in text, text
            assert not any(stage in text for stage in STAGES), text
        finally:
            hold.release.set()
        await settle(pilot)
        assert app.screen is app.main


async def test_the_transfer_dialog_buttons_are_ok_and_cancel(tree: Tree, local: RecordingLocal) -> None:
    app, *_ = _upload_or_download(tree, local, "upload")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await open_transfer(pilot, app, "f5", ["Sub"])
        assert [(b.id, str(b.label)) for b in app.screen.query(Button)] == [("ok", "OK"), ("cancel", "Cancel")]
        await pilot.press("escape")
        await settle(pilot)
