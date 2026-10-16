"""The questions an operation asks while it runs: the overwrite (conflict)
dialog, the error dialog, the progress dialog's cancel and the end-of-batch
failure summary. The engine runs in a worker thread and waits for the answer."""

from __future__ import annotations

from tests.ui.conftest import Tree
from tests.ui.support import (
    SIZE,
    Hold,
    Notes,
    RecordingLocal,
    StubEngine,
    dialog_text,
    focused_id,
    make_app,
    move_to,
    settle,
    wait_until,
)
from twinfm.backend.base import OperationFailed
from twinfm.ops.model import Conflict, OnError
from twinfm.ui.dialogs import ConflictDialog, ErrorDialog, ProgressDialog, SummaryDialog


async def _copy_cursor_file(pilot, app, name: str) -> None:  # type: ignore[no-untyped-def]
    await move_to(pilot, app.left_panel, name)
    await pilot.press("f5")
    await pilot.pause()
    await pilot.press("enter")


async def test_overwrite_dialog_opens_with_focus_on_skip(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    (tree.right / "A.txt").write_bytes(b"existing")
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await _copy_cursor_file(pilot, app, "A.txt")

        assert await wait_until(pilot, lambda: isinstance(app.screen, ConflictDialog)), app.screen_stack
        assert focused_id(app) == "skip"
        assert f"{tree.R}/A.txt" in dialog_text(app)

        await pilot.press("enter")  # the default: Skip
        await settle(pilot)

        assert engine.answers == [Conflict.SKIP]
        assert (tree.right / "A.txt").read_bytes() == b"existing"
        assert app.screen is app.main


async def test_overwrite_dialog_overwrite_button_replaces_the_file(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    (tree.right / "A.txt").write_bytes(b"existing")
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await _copy_cursor_file(pilot, app, "A.txt")
        assert await wait_until(pilot, lambda: isinstance(app.screen, ConflictDialog)), app.screen_stack

        await pilot.click("#overwrite")
        await settle(pilot)

        assert engine.answers == [Conflict.OVERWRITE]
        assert (tree.right / "A.txt").read_bytes() == b"aaa"
        assert app.screen is app.main


async def test_error_dialog_offers_retry_skip_skip_all_abort_and_summary_lists_the_failure(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    engine.fail[f"{tree.L}/A.txt"] = OperationFailed(f"{tree.L}/A.txt", "disk on fire")
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await _copy_cursor_file(pilot, app, "A.txt")

        assert await wait_until(pilot, lambda: isinstance(app.screen, ErrorDialog)), app.screen_stack
        buttons = [b.id for b in app.screen.query("Button")]
        assert buttons == ["retry", "skip", "skip_all", "abort"]
        assert "disk on fire" in dialog_text(app)

        await pilot.click("#skip")
        assert await wait_until(pilot, lambda: isinstance(app.screen, SummaryDialog)), app.screen_stack
        assert engine.answers == [OnError.SKIP]
        summary = dialog_text(app)
        assert "1 failed" in summary and "disk on fire" in summary, summary

        await pilot.click("#ok")
        await settle(pilot)
        assert app.screen is app.main
        assert app.return_code is None


async def test_escape_in_progress_dialog_cancels_after_the_current_file(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    hold = Hold()
    engine.between = hold
    app = make_app((local, tree.L), (local, tree.R), engine)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        left = app.left_panel
        await move_to(pilot, left, "A.txt")
        await pilot.press("space", "space", "space")  # A.txt, b.txt, c.txt
        await pilot.press("f5")
        await pilot.pause()
        await pilot.press("enter")

        try:
            assert await wait_until(pilot, hold.reached.is_set), "the engine never finished its first file"
            assert isinstance(app.screen, ProgressDialog), app.screen_stack
            await pilot.press("escape")
            await pilot.pause()
        finally:
            hold.release.set()
        await settle(pilot)

        assert sorted(p.name for p in tree.right.iterdir()) == ["A.txt"], "only the file in progress was copied"
        assert app.screen is app.main
        assert left.selected == {"b.txt", "c.txt"}, "what was not copied stays selected"
        assert any("cancel" in m.lower() for m in notes.messages()), notes.items
