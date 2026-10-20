"""While the active panel loads a listing, only Tab, Shift+Tab and quit work.

A listing is held with HeldLocal, a backend whose ``list_dir`` of a held path
waits until the test releases it, so the panel stays loading for as long as
the test needs. During the hold the status row reads ``Loading…`` and every
other key, a paste, a footer click and a click on a row change nothing: not
the cursor, the path, the selection, the find line or the screen stack; no
notification shows and no viewer, editor or shell runs. The other panel,
once listed, works as usual (D2). Quit works (D1). A listing result that a
newer request made stale is never shown, even when it was posted before
the newer request was made.

``Panel.is_loading`` is the flag (Textual's own ``Widget.loading`` would take
a loading panel out of the focus chain, so it stays False). Every test
releases its holds before the app ends: Python waits for the listing thread.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from textual import events
from textual._xterm_parser import XTermParser
from textual.pilot import Pilot
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

from tests.ui.conftest import LEFT_ORDER, Tree, send_bytes
from tests.ui.support import (
    SIZE,
    FakeCommand,
    FakeShell,
    FakeShellRunner,
    Notes,
    RecordingLocal,
    StubEngine,
    assert_alive,
    dialog_title,
    input_value,
    make_app,
    move_to,
    settle,
    wait_until,
)
from twinfm.backend.base import Entry
from twinfm.ui.app import TwinFM
from twinfm.ui.dialogs import InputDialog
from twinfm.ui.panel import Panel, PanelTable

LOADING = "Loading…"


class Gate:
    """One held path: ``reached`` once a listing of it waits, ``calls`` how
    many listings of it began; ``release`` lets them all go on."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.released = threading.Event()
        self.calls = 0

    def release(self) -> None:
        self.released.set()


class HeldLocal(RecordingLocal):
    """RecordingLocal whose ``list_dir`` of a held path waits for its gate.
    ``requests`` records every list_dir as it begins, held or not."""

    def __init__(self) -> None:
        super().__init__()
        self.gates: dict[str, Gate] = {}
        self.requests: list[str] = []

    def hold(self, path: str) -> Gate:
        gate = self.gates[path] = Gate()
        return gate

    def release_all(self) -> None:
        for gate in self.gates.values():
            gate.release()

    def list_dir(self, path: str) -> list[Entry]:
        self.requests.append(path)
        gate = self.gates.get(path)
        if gate is not None:
            gate.calls += 1
            gate.reached.set()
            gate.released.wait(30)
        return super().list_dir(path)


class Seams:
    """The recorders given to the app: nothing may run while a panel loads."""

    def __init__(self) -> None:
        self.viewer = FakeCommand()
        self.editor = FakeCommand()
        self.shell_runner = FakeShellRunner()
        self.shell = FakeShell()

    def calls(self) -> tuple[Any, ...]:
        return (self.viewer.calls, self.editor.calls, self.shell_runner.calls)


def held_app(held: HeldLocal, tree: Tree, seams: Seams, engine: StubEngine | None = None) -> TwinFM:
    return make_app(
        (held, tree.L),
        (held, tree.R),
        engine,
        run_viewer=seams.viewer,
        run_editor=seams.editor,
        run_shell=seams.shell_runner,
        shell=seams.shell,
    )


@asynccontextmanager
async def running(app: TwinFM, held: HeldLocal) -> AsyncIterator[Pilot[Any]]:
    """``app.run_test`` with the first listings done; every hold is released
    before the app ends."""
    async with app.run_test(size=SIZE) as pilot:
        try:
            await settle(pilot)
            yield pilot
        finally:
            held.release_all()


async def hold_reload(pilot: Pilot[Any], held: HeldLocal, panel: Panel) -> Gate:
    """Hold ``panel``'s directory and list it again (as the refresh after an
    operation does); return once the listing waits."""
    gate = held.hold(panel.path)
    panel.reload()
    assert await wait_until(pilot, gate.reached.is_set), "the held listing never started"
    await pilot.pause()
    assert panel.is_loading
    return gate


def status(panel: Panel) -> str:
    return str(panel.query_one("#status").content)


def line(panel: Panel) -> str:
    return str(panel.query_one("#find").content)


def footer_key(app: TwinFM, key: str) -> FooterKey:
    (found,) = [k for k in app.main.query_one(Footer).query(FooterKey) if k.key == key]
    return found


async def paste(pilot: Pilot[Any], text: str) -> None:
    """Paste ``text`` as a terminal with bracketed paste sends it."""
    (event,) = [e for e in XTermParser().feed(f"\x1b[200~{text}\x1b[201~") if isinstance(e, events.Paste)]
    pilot.app.post_message(event)
    await pilot.pause()
    await pilot.pause()


async def click_row(pilot: Pilot[Any], panel: Panel, name: str) -> None:
    """Click the listing row of ``name`` (the table's header is its first line)."""
    await pilot.click(panel.query_one(PanelTable), offset=(2, panel.names.index(name) + 1))


def snapshot(app: TwinFM) -> dict[str, Any]:
    def one(panel: Panel) -> tuple[Any, ...]:
        table = panel.query_one(PanelTable)
        return (
            panel.backend,
            panel.path,
            panel.names,
            panel.cursor_name,
            table.cursor_row,
            sorted(panel.selected),
            panel.finding,
            panel.find_text,
        )

    return {
        "active": app.active_panel.id,
        "stack": list(app.screen_stack),
        "left": one(app.left_panel),
        "right": one(app.right_panel),
    }


# -- the gate ----------------------------------------------------------------------------

PRESSES: dict[str, tuple[str, ...]] = {
    "down": ("down",),
    "up": ("up",),
    "enter": ("enter",),
    "backspace": ("backspace",),
    "space": ("space",),
    "insert": ("insert",),
    "ctrl+t": ("ctrl+t",),
    "ctrl+r": ("ctrl+r",),
    "ctrl+u": ("ctrl+u",),
    "letter": ("a",),
    "f3": ("f3",),
    "f4": ("f4",),
    "f5": ("f5",),
    "f6": ("f6",),
    "f7": ("f7",),
    "f8": ("f8",),
    "f9": ("f9",),
    "ctrl+n": ("ctrl+n",),
    "ctrl+o": ("ctrl+o",),
    "escape": ("escape",),
    "escape-then-5": ("escape", "5"),
}
OTHER_INPUT = ["option-5", "kitty_slash_without_text", "paste", "footer-f5", "row-click", "cursor-row-click"]


async def give(pilot: Pilot[Any], app: TwinFM, how: str) -> None:
    panel = app.left_panel
    if how in PRESSES:
        await pilot.press(*PRESSES[how])
    elif how == "option-5":
        await send_bytes(pilot, "∞")
    elif how == "kitty_slash_without_text":
        # "/" in CSI-u form without the text field: slash with no character.
        assert await send_bytes(pilot, "\x1b[47u") == ["slash"]
    elif how == "paste":
        await paste(pilot, "/some/dir")
    elif how == "footer-f5":
        await pilot.click(footer_key(app, "f5"))
    elif how == "row-click":
        other = "A.txt" if panel.cursor_name != "A.txt" else "c.txt"
        await click_row(pilot, panel, other)
    elif how == "cursor-row-click":
        await click_row(pilot, panel, panel.cursor_name or "..")
    else:
        raise AssertionError(how)
    await pilot.pause()
    await pilot.pause()


@pytest.mark.parametrize("line_state", ["line-closed", "line-open"])
@pytest.mark.parametrize("how", [*PRESSES, *OTHER_INPUT])
async def test_while_the_active_panel_loads_other_input_changes_nothing(
    tree: Tree, how: str, line_state: str
) -> None:
    """With the line closed the cursor is on Sub (Enter or a click would open
    it); with the line open "b" was typed (the cursor on b.txt)."""
    held, seams = HeldLocal(), Seams()
    engine = StubEngine()
    app = held_app(held, tree, seams, engine)
    notes = Notes(app)
    async with running(app, held) as pilot:
        panel = app.left_panel
        if line_state == "line-closed":
            await move_to(pilot, panel, "Sub")
        else:
            await pilot.press("b")
            await pilot.pause()
            assert panel.finding and panel.cursor_name == "b.txt"
        gate = await hold_reload(pilot, held, panel)
        before, requests = snapshot(app), list(held.requests)
        if line_state == "line-open":
            assert line(panel).endswith(" · loading…"), line(panel)
        else:
            assert status(panel) == LOADING

        await give(pilot, app, how)

        assert snapshot(app) == before
        assert held.requests == requests, "a new listing was requested"
        assert notes.items == []
        assert seams.calls() == ([], [], [])
        assert panel.is_loading
        assert line(panel).endswith(" · loading…") if line_state == "line-open" else status(panel) == LOADING

        gate.release()
        await settle(pilot)

        # Dropped, not queued: nothing happens once the listing is in either.
        assert not panel.is_loading
        assert snapshot(app) == before
        assert notes.items == [] and seams.calls() == ([], [], []) and engine.calls == []
        assert_alive(app)


IDLE_NO_OP = {("ctrl+u", "line-closed"), ("escape", "line-closed"), ("cursor-row-click", "line-open")}
"""Inputs that do nothing visible even when no panel loads: Ctrl-U and Esc
with the line closed, and a click on a file's cursor row."""


@pytest.mark.parametrize(
    ("how", "line_state"),
    [
        (how, state)
        for state in ("line-closed", "line-open")
        for how in [*PRESSES, *OTHER_INPUT]
        if (how, state) not in IDLE_NO_OP
    ],
)
async def test_the_same_input_does_something_when_no_panel_loads(tree: Tree, how: str, line_state: str) -> None:
    """The control for the test above: each input it gives a loading panel
    does reach the app and act when the panel is not loading."""
    held, seams = HeldLocal(), Seams()
    engine = StubEngine()
    app = held_app(held, tree, seams, engine)
    notes = Notes(app)
    async with running(app, held) as pilot:
        panel = app.left_panel
        if line_state == "line-closed":
            await move_to(pilot, panel, "Sub")
        else:
            await pilot.press("b")
            await pilot.pause()
        before, requests = snapshot(app), list(held.requests)

        await give(pilot, app, how)

        acted = (
            snapshot(app) != before
            or held.requests != requests
            or notes.items != []
            or seams.calls() != ([], [], [])
        )
        assert acted, f"{how} ({line_state}) did nothing on a panel that is not loading"


async def test_a_held_listing_reads_loading_and_once_applied_shows_the_count_and_keys_work(tree: Tree) -> None:
    held, seams = HeldLocal(), Seams()
    app = held_app(held, tree, seams)
    async with running(app, held) as pilot:
        panel = app.left_panel
        target = f"{tree.L}/Sub"
        gate = held.hold(target)
        await move_to(pilot, panel, "Sub")

        await pilot.press("enter")
        assert await wait_until(pilot, gate.reached.is_set)
        await pilot.pause()

        assert panel.is_loading
        assert panel.loading is False, "Textual's own loading flag would drop the panel from the focus chain"
        assert status(panel) == LOADING
        assert panel.path == tree.L

        gate.release()
        await settle(pilot)

        assert not panel.is_loading
        assert panel.path == target
        assert status(panel) == "1 items"
        assert panel.cursor_name == ".."
        await pilot.press("down")
        await pilot.pause()
        assert panel.cursor_name == "inner.txt"


# -- the other panel works (D2) ----------------------------------------------------------


@pytest.mark.parametrize("key", ["tab", "shift+tab"])
async def test_tab_and_shift_tab_reach_the_other_panel_which_works_while_the_first_is_held(
    tree: Tree, key: str
) -> None:
    for name in ("r1.txt", "r2.txt"):
        (tree.right / name).write_bytes(b"r")
    held, seams = HeldLocal(), Seams()
    app = held_app(held, tree, seams)
    async with running(app, held) as pilot:
        left, right = app.left_panel, app.right_panel
        await hold_reload(pilot, held, left)

        await pilot.press(key)
        await pilot.pause()
        assert app.active_panel is right
        assert app.focused is not None and right in app.focused.ancestors_with_self

        assert right.cursor_name == ".."
        await pilot.press("down")
        await pilot.pause()
        assert right.cursor_name == "r1.txt"

        await pilot.press("f7")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog) and dialog_title(app) == "Make directory"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.main
        assert left.is_loading and status(left) == LOADING

        await pilot.press(key)  # and back to the loading panel: Tab still works
        await pilot.pause()
        assert app.active_panel is left


async def test_with_both_panels_held_each_tab_and_shift_tab_still_switches(tree: Tree) -> None:
    held, seams = HeldLocal(), Seams()
    app = held_app(held, tree, seams)
    async with running(app, held) as pilot:
        left, right = app.left_panel, app.right_panel
        await hold_reload(pilot, held, left)
        await hold_reload(pilot, held, right)
        assert left.is_loading and right.is_loading

        expected = left
        for key in ["tab", "tab", "shift+tab", "shift+tab", "tab", "shift+tab"]:
            await pilot.press(key)
            await pilot.pause()
            expected = right if expected is left else left
            assert app.active_panel is expected, f"{key} did not switch the active panel"
            assert app.focused is not None and expected in app.focused.ancestors_with_self
        assert left.is_loading and right.is_loading


# -- quit works (D1) ----------------------------------------------------------------------

QUITS = {
    "F10": ("f10",),
    "Esc-then-0": ("escape", "0"),
    "Alt-0": ("alt+0",),
    "Ctrl-Q": ("ctrl+q",),
}


@pytest.mark.parametrize("how", [*QUITS, "Option-0", "footer-F10"])
async def test_quit_keys_end_the_app_while_the_active_panel_loads(tree: Tree, how: str) -> None:
    held, seams = HeldLocal(), Seams()
    app = held_app(held, tree, seams)
    async with running(app, held) as pilot:
        await hold_reload(pilot, held, app.left_panel)

        if how == "Option-0":
            await send_bytes(pilot, "º")
        elif how == "footer-F10":
            await pilot.click(footer_key(app, "f10"))
        else:
            await pilot.press(*QUITS[how])
        await pilot.pause()

        assert app.return_code == 0, f"{how} did not end the app"
    assert not app.is_running
    assert seams.shell.closed == 1


# -- a typed path ---------------------------------------------------------------------------


async def test_a_typed_path_shows_loading_on_the_line_drops_typing_and_closes_on_success(tree: Tree) -> None:
    held, seams = HeldLocal(), Seams()
    app = held_app(held, tree, seams)
    async with running(app, held) as pilot:
        panel = app.left_panel
        target = f"{tree.L}/Sub"
        gate = held.hold(target)
        await send_bytes(pilot, "Sub/")
        assert line(panel) == "Go to: Sub/"

        await pilot.press("enter")
        assert await wait_until(pilot, gate.reached.is_set)
        await pilot.pause()
        assert panel.is_loading
        assert line(panel) == "Go to: Sub/ · loading…"

        await send_bytes(pilot, "xy")
        assert panel.find_text == "Sub/"
        assert line(panel) == "Go to: Sub/ · loading…"

        gate.release()
        await settle(pilot)

        assert panel.path == target
        assert not panel.is_loading
        assert not panel.finding
        assert status(panel) == "1 items"


async def test_a_typed_path_that_fails_leaves_the_line_open_with_its_text_and_ends_loading(tree: Tree) -> None:
    held, seams = HeldLocal(), Seams()
    app = held_app(held, tree, seams)
    notes = Notes(app)
    async with running(app, held) as pilot:
        panel = app.left_panel
        gate = held.hold(f"{tree.L}/nope")
        await send_bytes(pilot, "nope/")

        await pilot.press("enter")
        assert await wait_until(pilot, gate.reached.is_set)
        await pilot.pause()
        assert line(panel) == "Go to: nope/ · loading…"
        await send_bytes(pilot, "z")
        assert panel.find_text == "nope/"

        gate.release()
        await settle(pilot)

        assert not panel.is_loading
        assert panel.finding and panel.find_text == "nope/"
        assert line(panel) == "Go to: nope/"
        assert [s for _, _, s in notes.items] == ["error"], notes.items
        assert panel.path == tree.L and panel.names == LEFT_ORDER


# -- stale results ----------------------------------------------------------------------------


async def test_an_older_listing_that_ends_after_a_newer_request_is_never_shown(tree: Tree) -> None:
    held, seams = HeldLocal(), Seams()
    app = held_app(held, tree, seams)
    async with running(app, held) as pilot:
        panel = app.left_panel
        older, newer = f"{tree.L}/Sub", f"{tree.L}/alpha"
        old_gate, new_gate = held.hold(older), held.hold(newer)
        panel.load(older)
        assert await wait_until(pilot, old_gate.reached.is_set)
        panel.load(newer)
        assert await wait_until(pilot, new_gate.reached.is_set)

        old_gate.release()  # the older listing ends first
        assert await wait_until(pilot, lambda: (older, False) in held.listed)
        await pilot.pause(0.3)

        assert panel.path == tree.L and panel.names == LEFT_ORDER, "the older result was shown"
        assert panel.is_loading and status(panel) == LOADING

        new_gate.release()
        await settle(pilot)

        assert panel.path == newer and panel.names == [".."]
        assert not panel.is_loading


async def test_an_older_result_posted_before_the_newer_request_is_never_shown(tree: Tree) -> None:
    """The race: the older worker has listed and posted its result to the UI
    thread before the newer request is made, so the result is waiting to be
    applied when the newer request cancels its worker."""
    held, seams = HeldLocal(), Seams()
    app = held_app(held, tree, seams)
    async with running(app, held) as pilot:
        panel = app.left_panel
        older, newer = f"{tree.L}/Sub", f"{tree.L}/alpha"
        old_gate, new_gate = held.hold(older), held.hold(newer)
        panel.load(older)
        assert await wait_until(pilot, old_gate.reached.is_set)

        posted = threading.Event()
        call_from_thread = app.call_from_thread

        def spy(callback: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(callback, "__name__", "") == "_apply" and len(args) > 1 and args[1] == older:
                posted.set()
            return call_from_thread(callback, *args, **kwargs)

        app.call_from_thread = spy  # type: ignore[method-assign]
        old_gate.release()
        # Hold the UI thread (this test runs on it) until the older result is
        # posted: it cannot be applied before the newer request below.
        assert posted.wait(10), "the older listing never posted its result"
        time.sleep(0.2)
        panel.load(newer)
        await pilot.pause()
        await pilot.pause(0.3)

        assert panel.path == tree.L and panel.names == LEFT_ORDER, "the older, posted result was shown"
        assert panel.is_loading and status(panel) == LOADING

        new_gate.release()
        await settle(pilot)

        assert panel.path == newer and panel.names == [".."]
        assert not panel.is_loading


async def test_an_older_typed_path_result_posted_before_a_newer_request_is_never_shown(tree: Tree) -> None:
    """The same race for go_to (a typed path): its result is dropped too, and
    the line it left open is still the panel's."""
    held, seams = HeldLocal(), Seams()
    app = held_app(held, tree, seams)
    async with running(app, held) as pilot:
        panel = app.left_panel
        older, newer = f"{tree.L}/Sub", f"{tree.L}/alpha"
        old_gate, new_gate = held.hold(older), held.hold(newer)
        panel.go_to(older)
        assert await wait_until(pilot, old_gate.reached.is_set)

        posted = threading.Event()
        call_from_thread = app.call_from_thread

        def spy(callback: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(callback, "__name__", "") == "_apply" and len(args) > 1 and args[1] == older:
                posted.set()
            return call_from_thread(callback, *args, **kwargs)

        app.call_from_thread = spy  # type: ignore[method-assign]
        old_gate.release()
        assert posted.wait(10), "the older listing never posted its result"
        time.sleep(0.2)
        panel.load(newer)
        await pilot.pause()
        await pilot.pause(0.3)

        assert panel.path == tree.L and panel.names == LEFT_ORDER, "the older, posted result was shown"
        assert panel.is_loading

        new_gate.release()
        await settle(pilot)
        assert panel.path == newer and not panel.is_loading


# -- an operation into a loading panel ---------------------------------------------------------


async def test_f5_into_a_panel_on_its_way_to_d_leaves_it_in_d(tree: Tree) -> None:
    (tree.right / "r.txt").write_bytes(b"r")
    held, seams = HeldLocal(), Seams()
    engine = StubEngine()
    app = held_app(held, tree, seams, engine)
    async with running(app, held) as pilot:
        left, right = app.left_panel, app.right_panel
        target = f"{tree.L}/Sub"
        gate = held.hold(target)
        await move_to(pilot, left, "Sub")
        await pilot.press("enter")
        assert await wait_until(pilot, gate.reached.is_set)
        assert left.is_loading

        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel is right
        await move_to(pilot, right, "r.txt")
        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog) and input_value(app) == tree.L
        await pilot.press("enter")

        assert await wait_until(pilot, lambda: (tree.left / "r.txt").exists()), "the copy did not run"
        assert await wait_until(pilot, lambda: gate.calls >= 2), "the refresh did not list D again"
        assert left.is_loading and left.path == tree.L

        gate.release()
        await settle(pilot)

        assert left.path == target, "the refresh after the copy took the panel back to its old directory"
        assert not left.is_loading
        assert engine.names() == ["copy"]
        assert app.screen is app.main
