"""The find line under each panel: typing a character on the main screen
searches the active panel's directory by name (the cursor goes to the first
match, as Midnight Commander's quick search does), and text that is a path
goes there on Enter.

The line takes the status row's place while it is open and never takes the
focus: the listing keeps it, and the main screen routes the keys. Esc, the
Esc-digit / Option-digit F-keys, Tab and every key the line does not use
close it and then do their usual job. Matching and path resolution are
unit-tested in test_find.py; these tests drive the app, at the wide test
size and at Terminal.app's default 80x24.

Characters are typed as real terminal bytes through Textual's parser
(``send_bytes``), except where a Pilot key name is the point. ``btty:`` goes
through FakeConnector; no test opens a real btty.
"""

from __future__ import annotations

import string
from typing import Any

import pytest
from textual import events
from textual._xterm_parser import XTermParser
from textual.pilot import Pilot
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

from tests.ui.conftest import LEFT_ORDER, Tree, parse_keys, send_bytes
from tests.ui.support import (
    SIZE,
    TRANSFER_ONLY,
    FakeConnector,
    Notes,
    RecordingLocal,
    RecordingMemory,
    StubEngine,
    assert_alive,
    dialog_text,
    dialog_title,
    input_value,
    make_app,
    move_to,
    settle,
)
from tests.ui.test_panel import MAIN_KEYS
from twinfm.ui.dialogs import InputDialog
from twinfm.ui.panel import Panel

OPENING_CHARACTERS = (
    string.ascii_lowercase + string.ascii_uppercase + string.digits + ".+-_@#$%^&*()[]~/" + "`" + "é"
)
"""Every character the user listed: a-z, A-Z, 0-9, the punctuation, the grave
accent, and a non-ASCII letter."""


@pytest.fixture(params=[SIZE, (80, 24)], ids=["wide", "80x24"])
def size(request: pytest.FixtureRequest) -> tuple[int, int]:
    return request.param


# -- reading the line ------------------------------------------------------------------


def line(panel: Panel) -> str:
    """The find line's whole text."""
    return str(panel.query_one("#find").content)


def shown(panel: Panel) -> str:
    """What the find line shows on screen (cut from the left when too long)."""
    return panel.query_one("#find").render().plain


def assert_open(panel: Panel, text: str, content: str | None = None) -> None:
    find = panel.query_one("#find")
    status = panel.query_one("#status")
    assert panel.finding, f"the find line of {panel.id} is closed"
    assert panel.find_text == text
    assert find.display and not status.display, (find.display, status.display)
    if content is not None:
        assert line(panel) == content


def assert_closed(panel: Panel) -> None:
    find = panel.query_one("#find")
    status = panel.query_one("#status")
    assert not panel.finding, f"the find line of {panel.id} is open: {line(panel)!r}"
    assert panel.find_text == ""
    assert not find.display and status.display, (find.display, status.display)


async def type_text(pilot: Pilot[Any], text: str) -> None:
    """Type ``text`` as a terminal sends it: its bytes through Textual's parser."""
    await send_bytes(pilot, text)


async def enter_text(pilot: Pilot[Any], text: str) -> None:
    """Put ``text`` into the line: typed when short, pasted when it is longer
    (a temporary directory's path is about 100 keys, one pause each). Typing
    a whole absolute path is covered by
    test_enter_on_an_absolute_directory_lists_it_off_the_main_thread_and_closes_the_line."""
    if len(text) <= 20:
        await type_text(pilot, text)
    else:
        await paste(pilot, text)


async def paste(pilot: Pilot[Any], text: str) -> None:
    """Paste ``text`` as a terminal with bracketed paste sends it."""
    parser = XTermParser()
    pasted = [e for e in parser.feed(f"\x1b[200~{text}\x1b[201~") if isinstance(e, events.Paste)]
    assert len(pasted) == 1, pasted
    pilot.app.post_message(pasted[0])
    await pilot.pause()
    await pilot.pause()


def footer_key(app: Any, key: str) -> FooterKey:
    (found,) = [k for k in app.main.query_one(Footer).query(FooterKey) if k.key == key]
    return found


def errors(notes: Notes) -> list[tuple[str, str]]:
    return [(m, t) for m, t, s in notes.items if s == "error"]


# -- opening the line and searching ------------------------------------------------------


async def test_typing_a_letter_opens_the_line_in_the_status_row_with_the_cursor_on_the_prefix_match(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        left, right = app.left_panel, app.right_panel
        status_row = left.query_one("#status").region
        right_status = str(right.query_one("#status").content)

        await pilot.press("b")
        await pilot.pause()

        assert_open(left, "b", "Find: b 2/2")
        assert shown(left) == "Find: b 2/2"
        assert left.query_one("#find").region == status_row, "the line is not in the status row's place"
        # "Sub" (earlier) contains b; "b.txt" starts with it.
        assert left.cursor_name == "b.txt"
        assert_closed(right)
        assert str(right.query_one("#status").content) == right_status
        assert left.names == LEFT_ORDER and left.selected == set()
        assert app.screen is app.main


async def test_down_and_up_step_through_the_matches_one_per_key_and_wrap(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("t")
        await pilot.pause()
        # No name starts with t: the first match anywhere.
        assert panel.cursor_name == ".git"
        assert_open(panel, "t", "Find: t 1/5")

        seen = []
        for _ in range(5):
            await pilot.press("down")
            await pilot.pause()
            seen.append((panel.cursor_name, line(panel)))
        assert seen == [
            ("A.txt", "Find: t 2/5"),
            ("b.txt", "Find: t 3/5"),
            ("c.txt", "Find: t 4/5"),
            ("Zeta.md", "Find: t 5/5"),
            (".git", "Find: t 1/5"),
        ]

        await pilot.press("up")
        await pilot.pause()
        assert panel.cursor_name == "Zeta.md"
        assert_open(panel, "t", "Find: t 5/5")
        assert panel.names == LEFT_ORDER and panel.selected == set()


@pytest.mark.parametrize("how", ["a", "A", "shift+a"])
async def test_a_or_shift_a_finds_alpha(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], how: str
) -> None:
    """Matching ignores case; Shift is allowed (a ``shift+a`` key with the
    character ``A``, as a terminal reporting modifiers sends it)."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        if how == "shift+a":
            app.post_message(events.Key("shift+a", "A"))
            await pilot.pause()
            await pilot.pause()
        else:
            await pilot.press(how)
            await pilot.pause()
        typed = "a" if how == "a" else "A"
        assert_open(panel, typed, f"Find: {typed} 1/3")  # alpha, A.txt, Zeta.md
        assert panel.cursor_name == "alpha"


async def test_a_star_pattern_finds_zeta_md(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "*.md")
        assert_open(panel, "*.md", "Find: *.md 1/1")
        assert panel.cursor_name == "Zeta.md"


async def test_no_match_leaves_the_cursor_where_it_was_and_says_so(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "c.txt")
        await pilot.press("q")  # no name has a q
        await pilot.pause()
        assert_open(panel, "q", "Find: q no match")
        assert panel.cursor_name == "c.txt"

        await pilot.press("ctrl+u", "x")  # the x of .txt: A.txt is the first match
        await pilot.pause()
        assert panel.cursor_name == "A.txt"
        await pilot.press("y", "z")
        await pilot.pause()
        assert_open(panel, "xyz", "Find: xyz no match")
        assert shown(panel) == "Find: xyz no match"
        assert panel.cursor_name == "A.txt"


async def test_a_bracket_in_the_query_matches_a_literal_bracket(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    # Sorted: a1.txt before b[1].txt. As a character class, [1] would find a1.txt.
    (tree.left / "a1.txt").write_bytes(b"1")
    (tree.left / "b[1].txt").write_bytes(b"1")
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        assert panel.names.index("a1.txt") < panel.names.index("b[1].txt")
        await type_text(pilot, "[1]")
        assert_open(panel, "[1]", "Find: [1] 1/1")
        assert panel.cursor_name == "b[1].txt"


async def test_dot_dot_is_never_matched(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, ".")
        assert panel.cursor_name == ".git"
        await type_text(pilot, ".")
        assert_open(panel, "..", "Find: .. no match")
        assert panel.cursor_name == ".git"
        for key in ("down", "up"):
            await pilot.press(key)
            await pilot.pause()
            assert panel.cursor_name == ".git", f"{key} moved to {panel.cursor_name!r}"


async def test_the_line_shows_the_selection_count_while_the_status_row_is_hidden(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "A.txt")
        await pilot.press("space")
        await pilot.pause()
        assert panel.selected == {"A.txt"}
        await pilot.press("b")
        await pilot.pause()
        assert_open(panel, "b", "Find: b 2/2 · 1 sel")
        assert panel.selected == {"A.txt"}


async def test_a_long_text_is_cut_from_the_left(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    text = "/" + "".join(f"dir{i:02d}/" for i in range(30)) + "end"
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await paste(pilot, text)
        assert_open(panel, text, f"Go to: {text}")
        width = panel.query_one("#find").content_size.width
        on_screen = shown(panel)
        assert len(line(panel)) > width, "precondition: the text is wider than the line"
        assert len(on_screen) == width, (width, on_screen)
        assert on_screen.startswith("…") and on_screen.endswith("dir29/end"), on_screen


# -- which keys open it ---------------------------------------------------------------------


@pytest.mark.parametrize("side", ["left", "right"])
async def test_each_listed_character_opens_the_line_on_the_active_panel_only(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], side: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        if side == "right":
            await pilot.press("tab")
            await pilot.pause()
        active, other = app.active_panel, app.other_panel
        assert active.id == side
        path, names = active.path, active.names

        # Each character, then the DEL byte a terminal's Backspace sends.
        keys = parse_keys("".join(c + "\x7f" for c in OPENING_CHARACTERS))
        typed, deletes = keys[::2], keys[1::2]
        assert "".join(k.character or "" for k in typed) == OPENING_CHARACTERS
        assert {k.key for k in deletes} == {"backspace"}
        failed = []
        for key, delete in zip(typed, deletes):
            app.post_message(key)
            await pilot.pause()
            if not (active.finding and active.find_text == key.character and not other.finding):
                failed.append((key.key, key.character, active.finding, active.find_text, other.finding))
            app.post_message(delete)  # one character: Backspace closes the line
            await pilot.pause()
            if active.finding:
                failed.append((key.key, "still open after Backspace"))
        assert failed == []
        await settle(pilot)
        assert (active.path, active.names) == (path, names), "a Backspace went to the parent directory"
        assert app.screen is app.main
        assert_alive(app)


async def test_a_transfer_only_panel_opens_no_line(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    remote = RecordingMemory(TRANSFER_ONLY, home="/drop")
    app = make_app((local, tree.L), (remote, "/drop"), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        right = app.right_panel
        assert app.active_panel is right and not right.listable

        opened = []
        for key in parse_keys(OPENING_CHARACTERS):
            app.post_message(key)
            await pilot.pause()
            if right.finding or app.left_panel.finding or right.query_one("#find").display:
                opened.append(key.character)
        assert opened == []
        assert app.screen is app.main
        assert right.path == "/drop"
        assert_alive(app)


async def test_space_with_the_line_closed_still_selects_and_never_opens_it(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "A.txt")
        await type_text(pilot, " ")
        assert panel.selected == {"A.txt"}
        assert panel.cursor_name == "b.txt"
        assert_closed(panel)


async def test_space_with_the_line_open_types_a_space_and_selects_nothing(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    (tree.left / "big file.txt").write_bytes(b"big")
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("g")
        await pilot.pause()
        assert_open(panel, "g")
        await pilot.press("space")
        await pilot.pause()
        assert_open(panel, "g ")
        assert panel.selected == set()
        await pilot.press("f")
        await pilot.pause()
        assert_open(panel, "g f", "Find: g f 1/1")
        assert panel.cursor_name == "big file.txt"
        assert panel.selected == set()


@pytest.mark.parametrize("chunk", ["\x1ba", "\x1bq", "\x1bA"], ids=["Esc-a-fast", "Esc-q-fast", "Esc-A-fast"])
async def test_a_key_with_a_modifier_does_not_open_the_line(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], chunk: str
) -> None:
    """A fast Esc+letter arrives as alt+letter with a printable character."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        names = await send_bytes(pilot, chunk)
        assert names and names[0].startswith("alt+"), names
        assert_closed(panel)
        assert app.screen is app.main


# -- keys while the line is open ------------------------------------------------------------


@pytest.mark.parametrize("key", ["insert", "ctrl+t"])
async def test_insert_or_ctrl_t_after_a_search_closes_the_line_selects_the_match_and_moves_down(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], key: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "A.t")
        assert_open(panel, "A.t")
        assert panel.cursor_name == "A.txt"

        await pilot.press(key)
        await pilot.pause()
        assert_closed(panel)
        assert panel.selected == {"A.txt"}
        assert panel.cursor_name == "b.txt"


async def test_backspace_deletes_and_on_the_last_character_closes_the_line_in_the_same_directory(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "b.t")
        assert panel.cursor_name == "b.txt"
        await pilot.press("backspace")
        await pilot.pause()
        assert_open(panel, "b.")
        await pilot.press("backspace")
        await pilot.pause()
        assert_open(panel, "b")
        await pilot.press("backspace")
        await settle(pilot)
        assert_closed(panel)
        assert panel.path == tree.L
        assert panel.names == LEFT_ORDER
        assert panel.cursor_name == "b.txt"


async def test_ctrl_u_empties_the_text_and_the_line_stays_open(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "b.t")
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert_open(panel, "")
        assert line(panel).startswith("Find:") and "match" not in line(panel), line(panel)

        await pilot.press("z")
        await pilot.pause()
        assert_open(panel, "z", "Find: z 1/1")
        assert panel.cursor_name == "Zeta.md"

        # Backspace on an empty text never goes to the parent directory.
        await pilot.press("ctrl+u", "backspace")
        await settle(pilot)
        assert panel.path == tree.L
        assert panel.names == LEFT_ORDER


async def test_escape_closes_the_line_keeps_the_cursor_and_a_following_5_copies_the_match(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "c.t")
        assert panel.cursor_name == "c.txt"

        await pilot.press("escape")
        await pilot.pause()
        assert_closed(panel)
        assert panel.cursor_name == "c.txt"
        assert app.screen is app.main

        await pilot.press("5")
        await pilot.pause()
        assert app.screen is not app.main, "Esc then 5 opened nothing"
        assert dialog_title(app) == "Copy"
        assert '"c.txt"' in dialog_text(app)
        await pilot.press("enter")
        await settle(pilot)
        assert engine.names() == ["copy"]
        assert engine.calls[0].args[1] == [f"{tree.L}/c.txt"]
        assert (tree.right / "c.txt").exists()


@pytest.mark.parametrize(
    "chunks",
    [("∞",), ("\x1b5",), ("\x1b", "5")],
    ids=["Option-5", "Esc-5-fast", "Esc-5-slow"],
)
async def test_option_5_or_escape_5_with_the_line_open_closes_it_and_copies_the_match(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], chunks: tuple[str, ...]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "c.t")
        assert_open(panel, "c.t")
        assert panel.cursor_name == "c.txt"

        names = await send_bytes(pilot, *chunks)
        assert app.screen is not app.main, f"{chunks!r} parsed as {names} opened nothing"
        assert dialog_title(app) == "Copy", f"{chunks!r} parsed as {names}"
        assert '"c.txt"' in dialog_text(app)
        assert_closed(panel)
        await pilot.press("enter")
        await settle(pilot)
        assert engine.calls[0].args[1] == [f"{tree.L}/c.txt"]


async def test_enter_on_a_directory_match_opens_it_and_closes_the_line(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("s", "u")
        await pilot.pause()
        assert panel.cursor_name == "Sub"
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == f"{tree.L}/Sub"
        assert panel.names == ["..", "inner.txt"]
        assert_closed(panel)


async def test_enter_on_a_file_match_closes_the_line_and_the_panel_stays(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "b.t")
        await pilot.press("enter")
        await settle(pilot)
        assert_closed(panel)
        assert panel.path == tree.L
        assert panel.cursor_name == "b.txt"
        assert app.screen is app.main


async def test_enter_with_no_match_closes_the_line_and_opens_nothing(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("s", "q")  # s finds Sub; sq finds nothing and leaves the cursor there
        await pilot.pause()
        assert panel.cursor_name == "Sub"  # a directory: Enter on its row would open it
        assert_open(panel, "sq", "Find: sq no match")
        await pilot.press("enter")
        await settle(pilot)
        assert_closed(panel)
        assert panel.path == tree.L
        assert panel.cursor_name == "Sub"
        assert panel.names == LEFT_ORDER


@pytest.mark.parametrize(
    ("key", "cursor"),
    [
        # In the listing, Home / End scroll sideways and leave the row cursor (as with the line closed).
        ("home", "b.txt"),
        ("end", "b.txt"),
        ("ctrl+home", ".."),
        ("ctrl+end", "Zeta.md"),
        ("pageup", ".."),
        ("pagedown", "Zeta.md"),
        ("ctrl+r", "b.txt"),
    ],
)
async def test_another_key_closes_the_line_and_does_its_own_job(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], key: str, cursor: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("b")
        await pilot.pause()
        assert panel.cursor_name == "b.txt"
        await pilot.press(key)
        await settle(pilot)
        assert_closed(panel)
        assert panel.cursor_name == cursor
        assert panel.path == tree.L


async def test_a_fast_escape_letter_closes_the_line_without_typing(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("c")
        await pilot.pause()
        assert_open(panel, "c")
        names = await send_bytes(pilot, "\x1ba")
        assert names == ["alt+a"], names
        assert_closed(panel)
        assert panel.cursor_name == "c.txt"


# -- what else closes it -----------------------------------------------------------------------


async def test_tab_closes_the_line_and_only_one_panel_ever_has_one_open(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        left, right = app.left_panel, app.right_panel
        await pilot.press("b")
        await pilot.pause()
        assert_open(left, "b")

        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel is right
        assert_closed(left)
        assert left.cursor_name == "b.txt"
        assert_closed(right)

        await pilot.press("z")
        await pilot.pause()
        assert_open(right, "z", "Find: z no match")
        assert_closed(left)

        # A click moves the focus to the left panel without a key.
        await pilot.click(left.query_one("#table"), offset=(3, 1))
        await pilot.pause()
        assert app.active_panel is left
        assert_closed(right)


@pytest.mark.parametrize("how", ["F7", "footer-F7", "no-key"])
async def test_opening_a_dialog_closes_the_line(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], how: str
) -> None:
    """F7, a click on the footer's F7, and a dialog opened without any key
    (as a worker opens one)."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("b")
        await pilot.pause()
        assert_open(panel, "b")

        if how == "F7":
            await pilot.press("f7")
        elif how == "footer-F7":
            await pilot.click(footer_key(app, "f7"))
        else:
            await app.run_action("mkdir")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog), type(app.screen).__name__
        assert dialog_title(app) == "Make directory"
        assert_closed(panel)

        await pilot.press("escape")
        await settle(pilot)
        assert app.screen is app.main
        assert_closed(panel)
        assert panel.cursor_name == "b.txt"


async def test_changing_directory_closes_the_line(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    """The mouse moves the cursor and opens a directory without a key."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        table = panel.query_one("#table")
        await pilot.press("b")
        await pilot.pause()
        sub_row = (3, 1 + LEFT_ORDER.index("Sub"))  # below the column header

        await pilot.click(table, offset=sub_row)
        await pilot.pause()
        assert panel.cursor_name == "Sub"
        assert_open(panel, "b", "Find: b 1/2")  # moving the cursor keeps the line, and k/n follows

        await pilot.click(table, offset=sub_row)  # a click on the cursor row opens it
        await settle(pilot)
        assert panel.path == f"{tree.L}/Sub"
        assert_closed(panel)


async def test_a_reload_of_the_same_directory_keeps_the_line_open_and_recounts(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("b")
        await pilot.pause()
        assert_open(panel, "b", "Find: b 2/2")

        (tree.left / "bb.log").write_bytes(b"b")
        panel.reload()  # what the refresh after an operation does
        await settle(pilot)
        assert "bb.log" in panel.names
        assert_open(panel, "b", "Find: b 2/3")
        assert panel.cursor_name == "b.txt"


# -- the layout ---------------------------------------------------------------------------------


async def test_the_listing_keeps_its_height_and_the_footer_its_keys_with_the_line_open(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        table = panel.query_one("#table")

        def footer() -> list[tuple[str, Any]]:
            return sorted((k.key, k.region) for k in app.main.query_one(Footer).query(FooterKey))

        closed = (table.region, footer())
        await pilot.press("b")
        await pilot.pause()
        assert_open(panel, "b")
        opened = (table.region, footer())

        assert opened[0].height == closed[0].height, (closed[0], opened[0])
        assert opened == closed
        assert {key for key, _ in opened[1]} == MAIN_KEYS
        find = panel.query_one("#find").region
        assert find.height == 1 and find.y == closed[0].bottom, (find, closed[0])


# -- paste ------------------------------------------------------------------------------------


async def test_pasting_opens_the_line_with_the_first_line_of_the_text(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        assert app.focused is panel.query_one("#table")
        await paste(pilot, "/some/dir\nsecond line")
        assert_open(panel, "/some/dir", "Go to: /some/dir")
        assert_closed(app.right_panel)


async def test_pasting_into_an_open_line_appends(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("b")
        await pilot.pause()
        await paste(pilot, ".txt\nnot this")
        assert_open(panel, "b.txt", "Find: b.txt 1/1")
        assert panel.cursor_name == "b.txt"


async def test_pasting_on_a_transfer_only_panel_opens_nothing(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    remote = RecordingMemory(TRANSFER_ONLY, home="/drop")
    app = make_app((local, tree.L), (remote, "/drop"), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        await paste(pilot, "/some/dir")
        assert not app.right_panel.finding
        assert not app.left_panel.finding
        assert_alive(app)


# -- typed paths ------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["/tmp", "~", "~/x", "Sub/", "../", "btty:/x", "btty:", "local:x"])
async def test_path_text_reads_go_to_and_up_down_do_nothing(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], text: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, text)  # until the / or : it is a search and may move the cursor
        assert_open(panel, text, f"Go to: {text}")
        cursor = panel.cursor_name
        for key in ("down", "down", "up"):
            await pilot.press(key)
            await pilot.pause()
            assert panel.cursor_name == cursor, f"{key} moved the cursor in path mode"
            assert_open(panel, text, f"Go to: {text}")


@pytest.mark.parametrize("text", ["~foo", ".", ".."])
async def test_tilde_word_dot_and_dot_dot_stay_searches(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], text: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, text)
        assert_open(panel, text)
        assert line(panel).startswith(f"Find: {text} "), line(panel)


async def test_enter_on_an_absolute_directory_lists_it_off_the_main_thread_and_closes_the_line(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        (tree.right / "r.txt").write_bytes(b"r")
        local.listed.clear()
        await type_text(pilot, tree.R)
        assert_open(panel, tree.R, f"Go to: {tree.R}")
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == tree.R
        assert panel.names == ["..", "r.txt"]
        assert_closed(panel)
        assert (tree.R, False) in local.listed
        assert [p for p, on_main in local.listed if on_main] == []


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("Sub/", "L/Sub"),
        ("Sub//", "L/Sub"),
        ("./Sub", "L/Sub"),
        ("../", "."),
        ("..//R/", "R"),
        ("{L}//Sub//", "L/Sub"),
        ("{L}/Sub/../", "L"),
        ("{L}/./", "L"),
    ],
)
async def test_relative_and_untidy_paths_are_resolved_and_normalised(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], text: str, where: str
) -> None:
    root = tree.left.parent
    typed = text.format(L=tree.L)
    expected = str(root) if where == "." else f"{root}/{where}"
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        local.listed.clear()
        await enter_text(pilot, typed)
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == expected
        assert_closed(panel)
        assert [p for p, on_main in local.listed if on_main] == []


async def test_slash_dot_dot_is_the_root(engine: StubEngine, size: tuple[int, int]) -> None:
    memory = RecordingMemory(home="/home/me")
    memory.add_file("/home/me/f.txt", b"f")
    app = make_app((memory, "/home/me"), (memory, "/home/me"), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "/..")
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == "/"
        assert panel.names == ["home"]
        assert_closed(panel)


@pytest.mark.parametrize("text", ["~", "~/x", "~/x/"])
@pytest.mark.parametrize("side", ["local", "memory"])
async def test_tilde_goes_to_the_panel_backends_own_home(
    tree: Tree,
    local: RecordingLocal,
    engine: StubEngine,
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    side: str,
) -> None:
    """The left panel is local (its home is $HOME, pointed at tree.R here);
    the right one is a MemoryBackend whose home is /home/me."""
    monkeypatch.setenv("HOME", tree.R)
    (tree.right / "x").mkdir()
    memory = RecordingMemory(home="/home/me")
    memory.add_dir("/home/me/x")
    memory.add_dir("/elsewhere")
    app = make_app((local, tree.L), (memory, "/elsewhere"), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        if side == "memory":
            await pilot.press("tab")
            await pilot.pause()
        panel = app.active_panel
        home = tree.R if side == "local" else "/home/me"
        expected = home if text == "~" else f"{home}/x"
        await type_text(pilot, text)
        assert_open(panel, text, f"Go to: {text}")
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == expected
        assert_closed(panel)
        assert app.other_panel.path == (tree.L if side == "memory" else "/elsewhere")


@pytest.mark.parametrize(
    ("start", "text", "parent", "cursor"),
    [
        ("R", "{L}/c.txt", "L", "c.txt"),
        ("L", "Sub/inner.txt", "L/Sub", "inner.txt"),
        ("L", "./Zeta.md/", "L", "Zeta.md"),
    ],
)
async def test_enter_on_a_file_lists_its_parent_with_the_cursor_on_the_file(
    tree: Tree,
    local: RecordingLocal,
    engine: StubEngine,
    size: tuple[int, int],
    start: str,
    text: str,
    parent: str,
    cursor: str,
) -> None:
    root = tree.left.parent
    app = make_app((local, f"{root}/{start}"), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        local.listed.clear()
        await enter_text(pilot, text.format(L=tree.L))
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == f"{root}/{parent}"
        assert panel.cursor_name == cursor
        assert_closed(panel)
        assert [p for p, on_main in local.listed if on_main] == []


async def test_a_symlinked_directory_keeps_the_typed_path(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    (tree.left / "link").symlink_to(tree.left / "Sub")
    app = make_app((local, tree.R), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        typed = f"{tree.L}/link"
        await enter_text(pilot, typed)
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == typed
        assert panel.names == ["..", "inner.txt"]
        assert_closed(panel)


async def test_a_symlink_to_a_file_is_revealed_like_the_file(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    (tree.left / "flink").symlink_to(tree.left / "c.txt")
    app = make_app((local, tree.R), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await enter_text(pilot, f"{tree.L}/flink")
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == tree.L
        assert panel.cursor_name == "flink"
        assert_closed(panel)


@pytest.mark.parametrize("text", ["{L}/missing", "missing/", "Sub/nothing/deeper", "{L}/A.txt/x"])
async def test_enter_on_a_missing_path_gives_one_error_and_the_panel_and_line_stay(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], text: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    notes = Notes(app)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        typed = text.format(L=tree.L)
        local.listed.clear()
        await enter_text(pilot, typed)  # typed, it is a search until the first / and may move the cursor
        cursor = panel.cursor_name
        await pilot.press("enter")
        await settle(pilot)
        assert len(errors(notes)) == 1, notes.items
        assert panel.path == tree.L
        assert panel.names == LEFT_ORDER
        assert panel.cursor_name == cursor
        assert_open(panel, typed, f"Go to: {typed}")
        assert local.listed, "the typed path was never listed"
        assert [p for p, on_main in local.listed if on_main] == []
        assert app.screen is app.main
        assert_alive(app)


# -- KIND: locations ----------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "path"), [("btty:/x", "/x"), ("btty:", "/home/me"), ("btty:x", "/home/me/x")])
async def test_btty_location_goes_through_the_connector_on_a_worker_and_switches_backend(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], text: str, path: str
) -> None:
    remote = RecordingMemory(home="/home/me", label="fake-btty")
    remote.add_file("/x/r.txt", b"r")
    remote.add_file("/home/me/x/h.txt", b"h")
    connector = FakeConnector({"btty": remote})
    app = make_app((local, tree.L), (local, tree.R), engine, connector)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, text)
        assert_open(panel, text, f"Go to: {text}")
        await pilot.press("enter")
        await settle(pilot)
        assert connector.calls == [("btty", False)]
        assert panel.backend is remote
        assert panel.path == path
        assert_closed(panel)
        assert (path, False) in remote.listed
        assert [p for p, on_main in remote.listed if on_main] == []
        assert app.right_panel.backend is local and app.right_panel.path == tree.R


async def test_btty_location_when_the_connector_fails_gives_one_error_and_the_panel_stays(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    connector = FakeConnector({})  # raises LookupError for btty
    app = make_app((local, tree.L), (local, tree.R), engine, connector)
    notes = Notes(app)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "btty:/x")
        await pilot.press("enter")
        await settle(pilot)
        assert connector.calls == [("btty", False)]
        assert len(errors(notes)) == 1, notes.items
        assert panel.backend is local
        assert panel.path == tree.L
        assert panel.names == LEFT_ORDER
        assert_alive(app)


async def test_btty_location_that_cannot_be_listed_gives_one_error_and_the_line_stays(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    remote = RecordingMemory(home="/home/me")
    connector = FakeConnector({"btty": remote})
    app = make_app((local, tree.L), (local, tree.R), engine, connector)
    notes = Notes(app)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "btty:/missing")
        await pilot.press("enter")
        await settle(pilot)
        assert len(errors(notes)) == 1, notes.items
        assert panel.backend is local
        assert panel.path == tree.L
        assert panel.names == LEFT_ORDER
        assert_open(panel, "btty:/missing", "Go to: btty:/missing")


async def test_local_location_stays_on_the_local_backend(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    connector = FakeConnector({})
    app = make_app((local, tree.R), (local, tree.R), engine, connector)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await enter_text(pilot, f"local:{tree.L}/Sub")
        await pilot.press("enter")
        await settle(pilot)
        assert panel.backend is local
        assert panel.path == f"{tree.L}/Sub"
        assert connector.calls == []
        assert_closed(panel)


async def test_a_btty_location_of_a_file_opens_its_directory_with_the_cursor_on_the_file(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    """KIND: paths go to Panel.go_to as other typed paths do: a file opens
    its directory on the connected backend with the cursor on it."""
    remote = RecordingMemory(home="/home/me", label="fake-btty")
    for name in ("a.txt", "file.txt", "z.txt"):
        remote.add_file(f"/dir/{name}", b"r")
    connector = FakeConnector({"btty": remote})
    app = make_app((local, tree.L), (local, tree.R), engine, connector)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "btty:/dir/file.txt")
        assert_open(panel, "btty:/dir/file.txt", "Go to: btty:/dir/file.txt")
        await pilot.press("enter")
        await settle(pilot)
        assert connector.calls == [("btty", False)]
        assert panel.backend is remote
        assert panel.path == "/dir"
        assert panel.cursor_name == "file.txt"
        assert_closed(panel)
        assert [p for p, on_main in remote.listed if on_main] == []


async def test_a_local_location_of_the_panels_own_directory_closes_the_line(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    """Going to where the panel already is still ends the typed path: the line closes."""
    connector = FakeConnector({})
    app = make_app((local, tree.L), (local, tree.R), engine, connector)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await enter_text(pilot, f"local:{tree.L}")
        await pilot.press("enter")
        await settle(pilot)
        assert panel.backend is local
        assert panel.path == tree.L
        assert connector.calls == []
        assert_closed(panel)


# -- the kitty keyboard protocol (CSI-u) ---------------------------------------------------
#
# User report from a live run: typing "/Users" - "/" then Shift - the Shift
# alone cleared "/" and closed "Go to:". Textual 8.2.8 asks the terminal for
# kitty flags 1 | 8 | 16 (disambiguate, report all keys as escape codes,
# report associated text), so a modifier pressed on its own arrives as a key
# of its own, with no character. A terminal that leaves out the text field
# sends Shift+letter as shift+<letter> with no character either.
#
# Expected: a modifier on its own leaves the line and its text as they are,
# and Shift+letter types the capital letter, in both forms.

BARE_MODIFIERS = {
    "left-shift": "\x1b[57441;2u",
    "right-shift": "\x1b[57447;2u",
    "left-ctrl": "\x1b[57442;5u",
    "left-alt": "\x1b[57443;3u",
    "left-super": "\x1b[57444;9u",
    "caps-lock": "\x1b[57358u",
}
"""A modifier key pressed on its own, as a terminal in kitty mode reports it."""

SHIFT_LETTER = {"U": "\x1b[117;2u", "E": "\x1b[101;2u", "Z": "\x1b[122;2u"}
"""Shift+letter in CSI-u form without the text field."""

SHIFT_U_WITH_TEXT = "\x1b[117;2;85u"
"""Shift+U in CSI-u form with the text field (85 is U)."""


def _kitty(text: str) -> str:
    """``text`` as unmodified keys in CSI-u form with their text field."""
    return "".join(f"\x1b[{ord(c)};;{ord(c)}u" for c in text)


def _kitty_no_text(text: str) -> str:
    """``text`` as unmodified keys in CSI-u form without the text field, as a
    terminal that leaves the text field out sends every key."""
    return "".join(f"\x1b[{ord(c)}u" for c in text)


def test_kitty_sequences_parse_as_the_tests_below_assume() -> None:
    """What Textual 8.2.8's parser makes of the bytes sent below."""
    def parsed(data: str) -> list[tuple[str, str | None]]:
        return [(k.key, k.character) for k in parse_keys(data)]

    assert {label: parsed(data) for label, data in BARE_MODIFIERS.items()} == {
        "left-shift": [("left_shift", None)],
        "right-shift": [("right_shift", None)],
        "left-ctrl": [("left_control", None)],
        "left-alt": [("left_alt", None)],
        "left-super": [("left_super", None)],
        "caps-lock": [("caps_lock", None)],
    }
    assert {c: parsed(data) for c, data in SHIFT_LETTER.items()} == {
        "U": [("shift+u", None)],
        "E": [("shift+e", None)],
        "Z": [("shift+z", None)],
    }
    assert parsed(SHIFT_U_WITH_TEXT) == [("U", "U")]
    assert parsed(_kitty("/s")) == [("slash", "/"), ("s", "s")]
    # Without the text field Textual names "/" but gives it no character.
    assert parsed(_kitty_no_text("/s")) == [("slash", None), ("s", "s")]


async def test_each_listed_character_without_the_text_field_opens_the_line_with_that_character(
    tree: Tree, local: RecordingLocal, engine: StubEngine
) -> None:
    """The terminal model of the kitty-without-text cases: every key in CSI-u
    form without the text field (``ESC [ <code> u``), as the Shift+letter
    sequences above are sent. Each listed character opens the line holding
    that character; Backspace (``ESC [ 127 u``) closes it again."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        failed = []
        for character in OPENING_CHARACTERS:
            names = await send_bytes(pilot, _kitty_no_text(character))
            if not (panel.finding and panel.find_text == character):
                failed.append((character, names, panel.finding, panel.find_text))
            await send_bytes(pilot, "\x1b[127u")
            if panel.finding:
                panel.find_close()
        assert failed == [], f"{len(failed)} character(s) did not open the line as themselves: {failed}"
        assert panel.path == tree.L
        assert_alive(app)


@pytest.mark.parametrize("modifier", list(BARE_MODIFIERS))
@pytest.mark.parametrize(("text", "content"), [("/", "Go to: /"), ("b", "Find: b 2/2")], ids=["path", "search"])
async def test_a_modifier_key_on_its_own_keeps_the_line_and_its_text(
    tree: Tree,
    local: RecordingLocal,
    engine: StubEngine,
    size: tuple[int, int],
    modifier: str,
    text: str,
    content: str,
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, text)
        assert_open(panel, text, content)
        cursor = panel.cursor_name

        names = await send_bytes(pilot, BARE_MODIFIERS[modifier])
        assert panel.finding, f"{BARE_MODIFIERS[modifier]!r} (parsed as {names}) closed the line {content!r}"
        assert_open(panel, text, content)
        assert panel.cursor_name == cursor
        assert app.screen is app.main


@pytest.mark.parametrize("form", ["without-text", "with-text"])
async def test_shift_u_after_a_slash_types_a_capital_u(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], form: str
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "/")
        assert_open(panel, "/", "Go to: /")

        data = SHIFT_LETTER["U"] if form == "without-text" else SHIFT_U_WITH_TEXT
        names = await send_bytes(pilot, data)
        assert panel.finding, f"{data!r} (parsed as {names}) closed the line 'Go to: /'"
        assert_open(panel, "/U", "Go to: /U")


async def test_shift_letter_without_text_types_the_capital_in_a_search(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("z")
        await pilot.pause()
        assert_open(panel, "z", "Find: z 1/1")

        names = await send_bytes(pilot, SHIFT_LETTER["E"])
        assert panel.finding, f"{SHIFT_LETTER['E']!r} (parsed as {names}) closed the line 'Find: z 1/1'"
        assert_open(panel, "zE", "Find: zE 1/1")
        assert panel.cursor_name == "Zeta.md"


async def test_shift_letter_without_text_opens_the_line_with_the_capital(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        names = await send_bytes(pilot, SHIFT_LETTER["Z"])
        assert panel.finding, f"{SHIFT_LETTER['Z']!r} (parsed as {names}) did not open the line"
        assert_open(panel, "Z", "Find: Z 1/1")
        assert panel.cursor_name == "Zeta.md"


@pytest.mark.parametrize(
    "keys",
    [
        "/Users",
        _kitty("/") + BARE_MODIFIERS["left-shift"] + SHIFT_U_WITH_TEXT + _kitty("sers"),
        _kitty_no_text("/") + BARE_MODIFIERS["left-shift"] + SHIFT_LETTER["U"] + _kitty_no_text("sers"),
    ],
    ids=["legacy", "kitty-with-text", "kitty-without-text"],
)
async def test_typing_slash_users_goes_to_users(engine: StubEngine, size: tuple[int, int], keys: str) -> None:
    """The user's case, whole: "/", Shift held for U, then "sers", then Enter."""
    memory = RecordingMemory(home="/home/me")
    memory.add_dir("/Users/me")
    app = make_app((memory, "/home/me"), (memory, "/home/me"), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        names = await send_bytes(pilot, keys)
        assert panel.finding and panel.find_text == "/Users", (
            f"{keys!r} parsed as {names}: finding={panel.finding} text={panel.find_text!r}"
        )
        assert_open(panel, "/Users", "Go to: /Users")
        await pilot.press("enter")
        await settle(pilot)
        assert panel.path == "/Users"
        assert panel.names == ["..", "me"]
        assert_closed(panel)


# -- around the kitty fix: what still must not type, and keys that must still work ------------

MODIFIED_LETTER = {
    "alt+u": "\x1b[117;3u",
    "alt+shift+u": "\x1b[117;4u",
    "ctrl+shift+u": "\x1b[117;6u",
    "super+u": "\x1b[117;9u",
    "shift+super+u": "\x1b[117;10u",
}
"""A letter with a modifier other than Shift alone, in CSI-u form without the
text field, keyed by the name Textual 8.2.8 parses it as."""


@pytest.mark.parametrize("modifier", list(BARE_MODIFIERS))
async def test_a_modifier_key_on_its_own_with_the_line_closed_does_nothing(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], modifier: str
) -> None:
    """A bare modifier opens no line and moves, selects and opens nothing;
    the next character still opens the line as usual."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "b.txt")

        names = await send_bytes(pilot, BARE_MODIFIERS[modifier])
        assert_closed(panel)
        assert panel.cursor_name == "b.txt", f"{BARE_MODIFIERS[modifier]!r} (parsed as {names}) moved the cursor"
        assert panel.selected == set()
        assert panel.path == tree.L
        assert app.screen is app.main
        assert engine.calls == []

        await type_text(pilot, "z")
        assert_open(panel, "z", "Find: z 1/1")
        assert panel.cursor_name == "Zeta.md"


@pytest.mark.parametrize("name", list(MODIFIED_LETTER))
async def test_a_letter_with_alt_ctrl_or_super_without_text_is_never_typed(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], name: str
) -> None:
    """Only Shift+<letter> without the text field types its capital. With Alt,
    Ctrl or Super as well the letter is not typed: with the line closed it
    opens nothing, and with the line open it is a key the line does not use,
    so the line closes without the letter."""
    data = MODIFIED_LETTER[name]
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        cursor = panel.cursor_name

        names = await send_bytes(pilot, data)
        assert names == [name], f"{data!r} parsed as {names}"
        assert_closed(panel)
        assert panel.cursor_name == cursor
        assert app.screen is app.main

        await type_text(pilot, "/")
        assert_open(panel, "/", "Go to: /")
        await send_bytes(pilot, data)
        assert_closed(panel)
        assert panel.path == tree.L
        assert app.screen is app.main


async def test_ctrl_u_in_csi_u_form_empties_the_text_and_the_line_stays_open(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    """In kitty mode Ctrl-U arrives as ESC[117;5u (ctrl+u, no character): it
    still clears the line, and is not typed as a U."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "b.t")
        assert_open(panel, "b.t")

        names = await send_bytes(pilot, "\x1b[117;5u")
        assert names == ["ctrl+u"], names
        assert_open(panel, "")
        assert panel.path == tree.L


async def test_shift_f6_with_the_line_open_closes_it_and_renames_the_match(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    """Shift-F6 (ESC[17;2~, shift+f6) is not a Shift+letter: it closes the
    line and opens Rename for the match, as any F-key does its own job."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await type_text(pilot, "b.t")
        assert panel.cursor_name == "b.txt"

        names = await send_bytes(pilot, "\x1b[17;2~")
        assert names == ["shift+f6"], names
        assert_closed(panel)
        assert app.screen is not app.main, f"{names} opened nothing"
        assert (dialog_title(app), input_value(app)) == ("Rename", "b.txt")


# -- punctuation without the text field: typed, extending the line, and never with a modifier ---
#
# Without the text field Textual 8.2.8 names an unmodified punctuation key
# after its character (slash, full_stop, ...) and gives it no character.
# Such a key types its character, as it does in the other terminal models;
# with Alt, Ctrl or Super it is not typed, as for a letter.

PUNCTUATION_FORMS = {"legacy": str, "kitty-with-text": _kitty, "kitty-without-text": _kitty_no_text}
"""Each terminal model's bytes for an unmodified key."""


@pytest.mark.parametrize("form", list(PUNCTUATION_FORMS))
async def test_every_printable_ascii_punctuation_character_opens_the_line_as_itself(
    tree: Tree, local: RecordingLocal, engine: StubEngine, form: str
) -> None:
    """Typing any character but Space opens the line (the module docstring),
    and the user's own list ends in "etc.": each printable ASCII punctuation
    character opens the line holding that character, the same in every
    terminal model."""
    encode = PUNCTUATION_FORMS[form]
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        failed = []
        for character in string.punctuation:
            names = await send_bytes(pilot, encode(character))
            if not (panel.finding and panel.find_text == character):
                failed.append((character, names, panel.finding, panel.find_text))
            if panel.finding:
                panel.find_close()
                await pilot.pause()
        assert failed == [], f"{len(failed)} character(s) did not open the line as themselves: {failed}"
        assert panel.path == tree.L
        assert app.screen is app.main
        assert_alive(app)


async def test_punctuation_without_the_text_field_extends_an_open_search(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int]
) -> None:
    """"b.t" sent as unshifted keys without the text field: the "." reaches
    the open line (the line's own key handling, not the key that opens it)."""
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        names = await send_bytes(pilot, _kitty_no_text("b.t"))
        assert names == ["b", "full_stop", "t"], names
        assert_open(panel, "b.t", "Find: b.t 1/1")
        assert panel.cursor_name == "b.txt"


@pytest.mark.parametrize(
    ("text", "where"),
    [("../", "."), ("alpha/", "L/alpha"), ("./alpha", "L/alpha")],
)
async def test_a_relative_path_typed_without_the_text_field_goes_there(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], text: str, where: str
) -> None:
    """A typed path whose "." and "/" are sent without the text field, after
    the line is open, reads "Go to:" and Enter lists it off the main thread."""
    root = tree.left.parent
    expected = str(root) if where == "." else f"{root}/{where}"
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        local.listed.clear()
        await send_bytes(pilot, _kitty_no_text(text))
        assert_open(panel, text, f"Go to: {text}")
        await send_bytes(pilot, "\x1b[13u")  # Enter in CSI-u form
        await settle(pilot)
        assert panel.path == expected
        assert_closed(panel)
        assert [p for p, on_main in local.listed if on_main] == []


MODIFIED_PUNCTUATION = {
    "alt+slash": "\x1b[47;3u",
    "ctrl+slash": "\x1b[47;5u",
    "super+slash": "\x1b[47;9u",
    "alt+full_stop": "\x1b[46;3u",
    "ctrl+minus": "\x1b[45;5u",
    "super+grave_accent": "\x1b[96;9u",
}
"""A punctuation key with Alt, Ctrl or Super, in CSI-u form without the text
field, keyed by the name Textual 8.2.8 parses it as."""


@pytest.mark.parametrize("name", list(MODIFIED_PUNCTUATION))
async def test_punctuation_with_alt_ctrl_or_super_without_text_is_never_typed(
    tree: Tree, local: RecordingLocal, engine: StubEngine, size: tuple[int, int], name: str
) -> None:
    """As for a letter: with the line closed it opens nothing, and with the
    line open it is a key the line does not use, so the line closes without
    the character."""
    data = MODIFIED_PUNCTUATION[name]
    app = make_app((local, tree.L), (local, tree.R), engine)
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        panel = app.left_panel
        cursor = panel.cursor_name

        names = await send_bytes(pilot, data)
        assert names == [name], f"{data!r} parsed as {names}"
        assert_closed(panel)
        assert panel.cursor_name == cursor
        assert app.screen is app.main

        await type_text(pilot, "b")
        assert_open(panel, "b", "Find: b 2/2")
        await send_bytes(pilot, data)
        assert_closed(panel)
        assert panel.path == tree.L
        assert app.screen is app.main
