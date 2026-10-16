"""Rename from a real terminal's bytes (user report: in a macOS terminal,
Shift-F6 does nothing from the keyboard; only clicking the footer works).

Pilot injects key *names*, so it cannot show what Textual makes of the bytes
a terminal sends. These tests feed raw input through Textual's own xterm
parser (the one its Linux/macOS driver uses) and post the resulting events to
a running app, exactly as the driver does. textual._xterm_parser is private
API; it is used here because the question is precisely what Textual 8.2.8's
parser produces.
"""

from __future__ import annotations

import time

import pytest
from textual import constants, events
from textual._xterm_parser import XTermParser

from tests.ui.conftest import Tree
from tests.ui.support import (
    SIZE,
    RecordingLocal,
    StubEngine,
    dialog_title,
    input_value,
    make_app,
    move_to,
    settle,
)
from twinfm.ui.dialogs import InputDialog


def parse(data: str) -> list[events.Key]:
    """The key events Textual's xterm parser makes of ``data`` (a lone or
    leading Esc is flushed after the parser's escape delay, as a pause in
    typing would)."""
    parser = XTermParser()
    out = [e for e in parser.feed(data) if isinstance(e, events.Key)]
    time.sleep(constants.ESCAPE_DELAY + 0.05)
    out += [e for e in parser.tick() if isinstance(e, events.Key)]
    return out


async def _send_bytes(pilot, app, data: str) -> list[str]:  # type: ignore[no-untyped-def]
    keys = parse(data)
    for key in keys:
        app.post_message(key)
        await pilot.pause()
    await pilot.pause()
    return [k.key for k in keys]


async def _rename_opens(tree: Tree, local: RecordingLocal, engine: StubEngine, *chunks: str) -> tuple[bool, list[str]]:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "b.txt")
        names: list[str] = []
        for chunk in chunks:  # separate chunks arrive with a pause between them
            names += await _send_bytes(pilot, app, chunk)
        opened = (
            isinstance(app.screen, InputDialog)
            and dialog_title(app) == "Rename"
            and input_value(app) == "b.txt"
        )
        return opened, names


# Shift-F6 in the xterm modifier form (iTerm2 and xterm default; terminfo
# xterm-256color kf18). The VT220 F18 code ESC[32~ is Shift-F8 in rxvt and
# Shift-F10 in Terminal.app, not Shift-F6, so it is not a Shift-F6 case.
SHIFT_F6 = {
    "xterm-iTerm2": "\x1b[17;2~",
}


@pytest.mark.parametrize("data", list(SHIFT_F6.values()), ids=list(SHIFT_F6))
async def test_shift_f6_bytes_open_rename(
    tree: Tree, local: RecordingLocal, engine: StubEngine, data: str
) -> None:
    opened, names = await _rename_opens(tree, local, engine, data)
    assert opened, f"{data!r} parsed as {names}; the Rename dialog did not open"


async def test_ctrl_n_byte_opens_rename(tree: Tree, local: RecordingLocal, engine: StubEngine) -> None:
    """Ctrl-N (0x0E) is the non-F-key alternative for Shift-F6."""
    opened, names = await _rename_opens(tree, local, engine, "\x0e")
    assert opened, f"0x0e parsed as {names}; the Rename dialog did not open"


# macOS Terminal.app's built-in keyboard profile sends Shift-F5..F12 as the
# VT220 codes of F13..F20 (terminfo nsterm kf13..kf20 = ESC[25~ .. ESC[34~),
# so Shift-F6 arrives as ESC[26~. This machine's Terminal prefs hold no
# keyMapBoundKeys override, i.e. the built-in default applies.
TERMINAL_APP_SHIFT_F6 = "\x1b[26~"


async def test_shift_f6_from_macos_terminal_app_opens_rename(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    opened, names = await _rename_opens(tree, local, engine, TERMINAL_APP_SHIFT_F6)
    assert opened, f"{TERMINAL_APP_SHIFT_F6!r} parsed as {names}; the Rename dialog did not open"


def test_parser_key_names_for_shift_f6_sequences() -> None:
    """What Textual 8.2.8 calls each sequence (printed for the report); the
    rename bindings are shift+f6, f18 and ctrl+n. rxvt's Shift-F6 (ESC[29~)
    and VT220 F18 (ESC[32~) are listed for reference only."""
    sequences = {
        "xterm-iTerm2 Shift-F6": "\x1b[17;2~",
        "Terminal.app Shift-F6": TERMINAL_APP_SHIFT_F6,
        "rxvt Shift-F6": "\x1b[29~",
        "VT220 F18": "\x1b[32~",
        "Ctrl-N": "\x0e",
        "Ctrl-Z byte": "\x1a",
        "Esc-r fast": "\x1br",
        "Esc-0 fast": "\x1b0",
    }
    seen = {label: [k.key for k in parse(data)] for label, data in sequences.items()}
    print(seen)
    assert seen["xterm-iTerm2 Shift-F6"] in (["shift+f6"], ["f18"]), seen
    assert seen["Ctrl-N"] == ["ctrl+n"], seen


# -- Esc-digit from real bytes -------------------------------------------------------

F_KEY_BYTES = {"5": "\x1b[15~", "6": "\x1b[17~", "7": "\x1b[18~", "8": "\x1b[19~", "9": "\x1b[20~"}


async def _opened_by(tree: Tree, local: RecordingLocal, *chunks: str) -> tuple[str, str, list[str]]:
    engine = StubEngine()
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "b.txt")
        names: list[str] = []
        for chunk in chunks:
            names += await _send_bytes(pilot, app, chunk)
        if app.screen is app.main:
            return ("main", "", names)
        return (type(app.screen).__name__, dialog_title(app), names)


@pytest.mark.parametrize("digit", list(F_KEY_BYTES))
@pytest.mark.parametrize("typing", ["fast", "slow"])
async def test_escape_digit_bytes_open_what_the_f_key_bytes_open(
    tree: Tree, local: RecordingLocal, digit: str, typing: str
) -> None:
    """Fast: ESC and the digit arrive together (Textual reads alt+digit).
    Slow: the digit follows after the escape delay (escape, then the digit)."""
    by_f_key = await _opened_by(tree, local, F_KEY_BYTES[digit])
    chunks = ("\x1b" + digit,) if typing == "fast" else ("\x1b", digit)
    by_escape = await _opened_by(tree, local, *chunks)
    assert by_f_key[0] != "main", f"F{digit} bytes parsed as {by_f_key[2]} opened nothing"
    assert by_escape[:2] == by_f_key[:2], f"Esc {digit} ({typing}) parsed as {by_escape[2]}: {by_escape[:2]} != {by_f_key[:2]}"


@pytest.mark.parametrize("chunks", [("\x1b[21~",), ("\x1b0",), ("\x1b", "0")], ids=["F10", "Esc-0-fast", "Esc-0-slow"])
async def test_f10_and_escape_0_bytes_quit(tree: Tree, local: RecordingLocal, chunks: tuple[str, ...]) -> None:
    app = make_app((local, tree.L), (local, tree.R), StubEngine())
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        for chunk in chunks:
            await _send_bytes(pilot, app, chunk)
        assert app.return_code == 0
