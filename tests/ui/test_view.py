"""F3 views a local file: the injected ``run_viewer`` gets the viewer's
command and then the file's absolute path, as one last argument, and the
panel lists again afterwards with the cursor on the file. The viewer is
``[viewer] command`` from the config file, else $PAGER, else ``less -R`` when
less is on PATH, else ``more``. Other panels, directories and ``..`` are
refused; Esc 3, Alt-3, the ``£`` of a fast ESC+3 or Option-3, the F3 bytes
``ESC O R`` and ``ESC [ 1 3 ~``, and a click on the footer's ``3View`` do
what F3 does; under a dialog F3 does nothing.

No test here starts a real viewer: every app gets a recording
``run_viewer``, except the one that checks the default launcher in a
headless run, where ``subprocess.run`` is replaced by a recorder too. No
test depends on the developer's environment: the autouse ``viewer_env``
(tests/ui/conftest.py) unsets $PAGER and decides whether ``less`` is found,
so the default viewer is ``less -R`` unless a test says otherwise. The
module docstring's key reference is checked at the end.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

import twinfm.ui.app as app_module
from tests.ui.conftest import DEFAULT_VIEWER, Tree, ViewerEnv, send_bytes
from tests.ui.support import (
    SIZE,
    TRANSFER_ONLY,
    FakeCommand,
    Notes,
    RecordingLocal,
    RecordingMemory,
    assert_alive,
    make_app,
    move_to,
    settle,
)
from tests.ui.test_panel import MAIN_FOOTER
from twinfm.backend.btty import BttyBackend
from twinfm.config import Settings, load_settings
from twinfm.ui.app import FileManagerScreen, TwinFM
from twinfm.ui.dialogs import ConfirmDialog, InputDialog

LOCAL_ONLY = "F3 views local files only; copy the file to a local panel to view it"


def view_app(
    left: tuple[Any, str], right: tuple[Any, str], viewer: FakeCommand | None, settings: Settings | None = None
) -> TwinFM:
    return make_app(left, right, run_viewer=viewer, settings=settings)


def viewed(path: str) -> list[str]:
    """The argv F3 gives the default viewer (``viewer_env``'s defaults) for ``path``."""
    return [*DEFAULT_VIEWER, path]


class NoBtty:
    """A btty runner that must never be called (a transfer-only panel does not list)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return 1, "", "test runner: btty must not be called here"


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace the app module's subprocess.run, so that even the default
    launcher can never start a real viewer; records what it was asked to run."""
    ran: list[list[str]] = []

    def refuse(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(app_module.subprocess, "run", refuse)
    return ran


# -- the viewer command and afterwards ----------------------------------------------------


async def test_f3_runs_the_viewer_with_the_absolute_path_once_and_lists_again_with_the_cursor_on_the_file(
    tree: Tree, local: RecordingLocal
) -> None:
    def grow(argv: list[str]) -> None:
        # The directory changes meanwhile: a new file sorts above A.txt.
        (tree.left / ".A.txt.new").write_bytes(b"n")

    viewer = FakeCommand(effect=grow)
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "A.txt")
        row_before = panel.names.index("A.txt")
        listed = len(local.listed)

        await pilot.press("f3")
        await settle(pilot)

        assert viewer.calls == [viewed(f"{tree.L}/A.txt")]
        assert tree.L in [p for p, _ in local.listed[listed:]], "the panel did not list again"
        assert ".A.txt.new" in panel.names
        assert panel.names.index("A.txt") == row_before + 1
        assert panel.cursor_name == "A.txt"
        assert app.screen is app.main
        assert_alive(app)


async def test_the_viewer_path_is_one_argument_even_with_spaces_and_quotes(tree: Tree, local: RecordingLocal) -> None:
    name = "my notes 'draft' $HOME.txt"
    (tree.left / name).write_bytes(b"n")
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, name)
        await pilot.press("f3")
        await settle(pilot)
        assert viewer.calls == [viewed(f"{tree.L}/{name}")]


async def test_f3_in_the_right_panel_views_the_right_panels_file(tree: Tree, local: RecordingLocal) -> None:
    (tree.right / "r.txt").write_bytes(b"r")
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        await move_to(pilot, app.right_panel, "r.txt")
        await pilot.press("f3")
        await settle(pilot)
        assert viewer.calls == [viewed(f"{tree.R}/r.txt")]
        assert app.right_panel.cursor_name == "r.txt"


async def test_a_symlink_to_a_file_is_viewed_at_the_link_path(tree: Tree, local: RecordingLocal) -> None:
    os.symlink("A.txt", tree.left / "link-to-A")
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "link-to-A")
        await pilot.press("f3")
        await settle(pilot)
        assert viewer.calls == [viewed(f"{tree.L}/link-to-A")]


# -- other keys for F3 ----------------------------------------------------------------------


@pytest.mark.parametrize("keys", [("f3",), ("escape", "3"), ("alt+3",)], ids=["F3", "Esc-then-3", "Alt-3"])
async def test_pilot_keys_for_view(tree: Tree, local: RecordingLocal, keys: tuple[str, ...]) -> None:
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "b.txt")
        await pilot.press(*keys)
        await settle(pilot)
        assert viewer.calls == [viewed(f"{tree.L}/b.txt")]
        assert app.left_panel.cursor_name == "b.txt"
        assert not app.left_panel.finding


@pytest.mark.parametrize(
    "chunks",
    [("\x1bOR",), ("\x1b[13~",), ("\x1b3",), ("\x1b", "3"), ("£",)],
    ids=["F3-xterm-Terminal.app", "F3-vt220", "Esc-3-fast", "Esc-3-slow", "Option-3"],
)
async def test_terminal_bytes_for_view(tree: Tree, local: RecordingLocal, chunks: tuple[str, ...]) -> None:
    """Real bytes through Textual's parser: a fast ESC+3 and Option-3 both
    arrive as ``£`` (pound_sign), which Pilot's press() cannot produce."""
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "b.txt")
        names = await send_bytes(pilot, *chunks)
        await settle(pilot)
        assert viewer.calls == [viewed(f"{tree.L}/b.txt")], f"{chunks!r} parsed as {names}"
        assert not app.left_panel.finding, "the key also opened the find line"
        assert_alive(app)


# -- refusals -----------------------------------------------------------------------------


async def test_f3_on_a_memory_panel_warns_local_only_and_does_not_view(tree: Tree, local: RecordingLocal) -> None:
    memory = RecordingMemory(home="/m")
    memory.add_file("/m/notes.txt", b"n")
    viewer = FakeCommand()
    app = view_app((local, tree.L), (memory, "/m"), viewer)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        await move_to(pilot, app.right_panel, "notes.txt")

        await pilot.press("f3")
        await settle(pilot)

        assert viewer.calls == []
        assert [(m, s) for m, _, s in notes.items] == [(LOCAL_ONLY, "warning")]
        assert_alive(app)


async def test_f3_on_a_btty_panel_warns_local_only_and_does_not_view(tree: Tree, local: RecordingLocal) -> None:
    runner = NoBtty()
    remote = BttyBackend(Settings(btty_exec=False), runner=runner)
    viewer = FakeCommand()
    app = view_app((local, tree.L), (remote, "/mnt/home0/wst/workspaces"), viewer)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel.backend is remote

        await pilot.press("f3")
        await settle(pilot)

        assert viewer.calls == []
        assert [(m, s) for m, _, s in notes.items] == [(LOCAL_ONLY, "warning")]
        assert runner.calls == []


async def test_f3_on_a_transfer_only_panel_warns_local_only(tree: Tree, local: RecordingLocal) -> None:
    remote = RecordingMemory(TRANSFER_ONLY, home="/drop")
    viewer = FakeCommand()
    app = view_app((local, tree.L), (remote, "/drop"), viewer)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("f3")
        await settle(pilot)
        assert viewer.calls == []
        assert [(m, s) for m, _, s in notes.items] == [(LOCAL_ONLY, "warning")]


async def test_f3_on_a_directory_gives_one_not_a_file_warning(tree: Tree, local: RecordingLocal) -> None:
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "Sub")

        await pilot.press("f3")
        await settle(pilot)

        assert viewer.calls == []
        assert notes.items == [('"Sub" is not a file', "View", "warning")]
        assert app.left_panel.path == tree.L, "F3 does not enter a directory"
        assert_alive(app)


def _make_fifo(tree: Tree) -> str:
    os.mkfifo(tree.left / "pipe")
    return "pipe"


def _make_dir_link(tree: Tree) -> str:
    os.symlink("Sub", tree.left / "link-to-Sub")
    return "link-to-Sub"


def _make_dangling_link(tree: Tree) -> str:
    os.symlink("nowhere", tree.left / "dangling")
    return "dangling"


@pytest.mark.parametrize(
    "make",
    [_make_dir_link, _make_fifo, _make_dangling_link],
    ids=["symlink-to-directory", "fifo", "dangling-symlink"],
)
async def test_f3_on_something_else_that_is_not_a_file_warns_and_does_not_view(
    tree: Tree, local: RecordingLocal, make: Callable[[Tree], str]
) -> None:
    name = make(tree)
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, name)
        await pilot.press("f3")
        await settle(pilot)
        assert viewer.calls == []
        assert notes.items == [(f'"{name}" is not a file', "View", "warning")]


async def test_f3_on_the_parent_entry_does_nothing(tree: Tree, local: RecordingLocal) -> None:
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert app.left_panel.cursor_name == ".."
        listed = len(local.listed)

        await pilot.press("f3")
        await settle(pilot)

        assert viewer.calls == []
        assert notes.items == []
        assert len(local.listed) == listed
        assert app.screen is app.main
        assert app.left_panel.path == tree.L


@pytest.mark.parametrize(
    ("key", "dialog"),
    [("f7", InputDialog), ("f8", ConfirmDialog), ("f9", InputDialog), ("f5", InputDialog)],
    ids=["mkdir", "delete", "connect", "copy"],
)
async def test_f3_under_a_dialog_does_not_view(tree: Tree, local: RecordingLocal, key: str, dialog: type) -> None:
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, dialog), app.screen_stack
        opened = app.screen

        await pilot.press("f3")
        await settle(pilot)

        assert viewer.calls == []
        assert app.screen is opened
        assert_alive(app)


async def test_pound_sign_typed_in_a_dialog_is_text_not_view(tree: Tree, local: RecordingLocal) -> None:
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("f7")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)

        await send_bytes(pilot, "£")

        assert viewer.calls == []
        assert app.screen.query_one("Input").value == "£"
        assert_alive(app)


# -- with the find line -------------------------------------------------------------------


async def test_f3_with_the_find_line_open_closes_it_and_views_the_match(tree: Tree, local: RecordingLocal) -> None:
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("b")
        await pilot.pause()
        assert panel.finding and panel.cursor_name == "b.txt"

        await pilot.press("f3")
        await settle(pilot)

        assert viewer.calls == [viewed(f"{tree.L}/b.txt")]
        assert not panel.finding
        assert panel.cursor_name == "b.txt"


async def test_pound_sign_with_the_find_line_open_views_the_match_and_types_nothing(
    tree: Tree, local: RecordingLocal
) -> None:
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("b")
        await pilot.pause()
        assert panel.finding and panel.find_text == "b" and panel.cursor_name == "b.txt"

        names = await send_bytes(pilot, "£")
        await settle(pilot)

        assert viewer.calls == [viewed(f"{tree.L}/b.txt")], f"£ parsed as {names}"
        assert not panel.finding and panel.find_text == ""


# -- which viewer --------------------------------------------------------------------------

BAT = "bat --paging=always"

SOURCES: dict[str, tuple[str | None, str | None, bool, list[str]]] = {
    "setting": (BAT, "less -FRX", True, ["bat", "--paging=always"]),
    "PAGER": (None, "less -FRX", True, ["less", "-FRX"]),
    "less": (None, None, True, ["less", "-R"]),
    "more": (None, None, False, ["more"]),
}
"""Where F3's viewer comes from: ``(viewer_command, $PAGER, less on PATH,
the command run before the path)``."""


def from_source(source: str, monkeypatch: pytest.MonkeyPatch, viewer_env: ViewerEnv) -> tuple[Settings, list[str]]:
    """Set up ``SOURCES[source]``; the Settings to give the app and the command F3 must run."""
    setting, pager, less, command = SOURCES[source]
    if pager is not None:
        monkeypatch.setenv("PAGER", pager)
    viewer_env.less = less
    return Settings(btty_exec=False, viewer_command=setting), command


async def f3_on(
    tree: Tree, local: RecordingLocal, viewer: FakeCommand, settings: Settings, name: str
) -> list[list[str]]:
    """Run an app with ``settings``, press F3 on ``name`` in the left panel and
    return what the viewer was asked to run."""
    app = view_app((local, tree.L), (local, tree.R), viewer, settings)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, name)
        await pilot.press("f3")
        await settle(pilot)
        assert app.screen is app.main
        assert_alive(app)
    return viewer.calls


@pytest.mark.parametrize(
    "pager", [None, "less -FRX", "", "most"], ids=["PAGER-unset", "PAGER-less-FRX", "PAGER-empty", "PAGER-most"]
)
async def test_the_config_files_viewer_command_is_run_whatever_pager_is(
    tree: Tree, local: RecordingLocal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pager: str | None
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[btty]\nexec = false\n\n[viewer]\ncommand = "{BAT}"\n')
    settings = load_settings(config)
    assert settings.viewer_command == BAT
    if pager is not None:
        monkeypatch.setenv("PAGER", pager)

    calls = await f3_on(tree, local, FakeCommand(), settings, "A.txt")

    assert calls == [["bat", "--paging=always", f"{tree.L}/A.txt"]]


@pytest.mark.parametrize(
    ("pager", "command"),
    [
        ("less -FRX", ["less", "-FRX"]),
        ("most", ["most"]),
        ("bat --style='plain header' -p", ["bat", "--style=plain header", "-p"]),
        ('  "/opt/my pager/bin/view"  --raw  ', ["/opt/my pager/bin/view", "--raw"]),
    ],
    ids=["less-FRX", "one-word", "quoted-argument", "quoted-program-and-spaces"],
)
@pytest.mark.parametrize("less", [True, False], ids=["less-on-PATH", "no-less"])
async def test_without_a_setting_pager_is_run_split_as_a_shell_would(
    tree: Tree,
    local: RecordingLocal,
    monkeypatch: pytest.MonkeyPatch,
    viewer_env: ViewerEnv,
    pager: str,
    command: list[str],
    less: bool,
) -> None:
    """$PAGER is the user's choice: it is run whether or not less is on PATH."""
    monkeypatch.setenv("PAGER", pager)
    viewer_env.less = less

    calls = await f3_on(tree, local, FakeCommand(), Settings(btty_exec=False), "A.txt")

    assert calls == [[*command, f"{tree.L}/A.txt"]]


@pytest.mark.parametrize(
    "pager",
    [None, "", "   ", "less 'unclosed"],
    ids=["PAGER-unset", "PAGER-empty", "PAGER-blank", "PAGER-unclosed-quote"],
)
@pytest.mark.parametrize("less", [True, False], ids=["less-on-PATH", "no-less"])
async def test_without_a_setting_or_a_usable_pager_f3_runs_less_r_else_more(
    tree: Tree,
    local: RecordingLocal,
    monkeypatch: pytest.MonkeyPatch,
    viewer_env: ViewerEnv,
    pager: str | None,
    less: bool,
) -> None:
    if pager is not None:
        monkeypatch.setenv("PAGER", pager)
    viewer_env.less = less
    path = f"{tree.L}/A.txt"

    calls = await f3_on(tree, local, FakeCommand(), Settings(btty_exec=False), "A.txt")

    assert calls == [["less", "-R", path] if less else ["more", path]]


@pytest.mark.parametrize("source", list(SOURCES))
async def test_a_file_name_with_spaces_quotes_and_home_is_one_argument_and_the_last(
    tree: Tree, local: RecordingLocal, monkeypatch: pytest.MonkeyPatch, viewer_env: ViewerEnv, source: str
) -> None:
    name = "my notes 'draft' \"v2\" $HOME.txt"
    (tree.left / name).write_bytes(b"n")
    settings, command = from_source(source, monkeypatch, viewer_env)

    calls = await f3_on(tree, local, FakeCommand(), settings, name)

    assert calls == [[*command, f"{tree.L}/{name}"]]


# -- the viewer's outcome -------------------------------------------------------------------


@pytest.mark.parametrize("source", list(SOURCES))
async def test_a_viewer_that_cannot_start_gives_one_error_notice_naming_it_and_nothing_else_runs(
    tree: Tree,
    local: RecordingLocal,
    monkeypatch: pytest.MonkeyPatch,
    viewer_env: ViewerEnv,
    no_subprocess: list[list[str]],
    source: str,
) -> None:
    """The chosen viewer is reported, never swapped for another (not less
    for a failing setting or $PAGER, not more for a failing less); the app
    stays on the main screen and the panel is listed again."""
    settings, command = from_source(source, monkeypatch, viewer_env)
    viewer = FakeCommand(raises=FileNotFoundError(2, "No such file or directory", command[0]))
    app = view_app((local, tree.L), (local, tree.R), viewer, settings)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        listed = len(local.listed)

        await pilot.press("f3")
        await settle(pilot)

        assert viewer.calls == [[*command, f"{tree.L}/A.txt"]], "another viewer was tried"
        assert no_subprocess == []
        assert [(t, s) for _, t, s in notes.items] == [(f"Cannot start {command[0]}", "error")], notes.items
        assert "No such file or directory" in notes.items[0][0], notes.items
        assert tree.L in [p for p, _ in local.listed[listed:]], "the panel did not list again"
        assert app.left_panel.cursor_name == "A.txt"
        assert_alive(app)
        assert app.screen is app.main


@pytest.mark.parametrize("status", [1, 2])
@pytest.mark.parametrize("source", list(SOURCES))
async def test_a_non_zero_exit_gives_one_warning_naming_the_program_and_the_status(
    tree: Tree,
    local: RecordingLocal,
    monkeypatch: pytest.MonkeyPatch,
    viewer_env: ViewerEnv,
    source: str,
    status: int,
) -> None:
    settings, command = from_source(source, monkeypatch, viewer_env)
    viewer = FakeCommand(status=status)
    app = view_app((local, tree.L), (local, tree.R), viewer, settings)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        listed = len(local.listed)

        await pilot.press("f3")
        await settle(pilot)

        assert len(viewer.calls) == 1
        assert [(m, s) for m, _, s in notes.items] == [(f"{command[0]} exited with status {status}", "warning")]
        assert tree.L in [p for p, _ in local.listed[listed:]], "the panel did not list again"
        assert_alive(app)


@pytest.mark.parametrize("outcome", ["cannot-start", "non-zero-exit"])
async def test_a_viewer_whose_name_looks_like_markup_is_named_as_it_is(
    tree: Tree, local: RecordingLocal, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """$PAGER is the user's text: a program named ``[/view]`` is named in the
    notice as it is, and the notice does not break the app."""
    monkeypatch.setenv("PAGER", "'[/view]' -x")
    viewer = (
        FakeCommand(raises=PermissionError(13, "Permission denied", "[/view]"))
        if outcome == "cannot-start"
        else FakeCommand(status=3)
    )
    app = view_app((local, tree.L), (local, tree.R), viewer)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f3")
        await settle(pilot)
        await pilot.pause(0.1)  # the notice is drawn

        assert viewer.calls == [["[/view]", "-x", f"{tree.L}/A.txt"]]
        if outcome == "cannot-start":
            assert [(t, s) for _, t, s in notes.items] == [("Cannot start [/view]", "error")], notes.items
        else:
            assert [(m, s) for m, _, s in notes.items] == [("[/view] exited with status 3", "warning")]
        assert_alive(app)
        assert app.screen is app.main


async def test_a_zero_exit_gives_no_notice(tree: Tree, local: RecordingLocal) -> None:
    viewer = FakeCommand(status=0)
    app = view_app((local, tree.L), (local, tree.R), viewer)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f3")
        await settle(pilot)
        assert len(viewer.calls) == 1
        assert notes.items == []


async def test_the_default_viewer_in_a_headless_run_gives_a_notice_and_the_app_keeps_running(
    tree: Tree, local: RecordingLocal, no_subprocess: list[list[str]]
) -> None:
    """No run_viewer injected: the app cannot hand a headless terminal to the
    viewer (Textual's SuspendNotSupported), says so, and never spawns it."""
    app = view_app((local, tree.L), (local, tree.R), viewer=None)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        listed = len(local.listed)

        await pilot.press("f3")
        await settle(pilot)

        assert no_subprocess == [], "the default viewer started a process in a headless run"
        assert tree.L in [p for p, _ in local.listed[listed:]], "the panel did not list again"
        assert len(notes.items) == 1, notes.items
        assert notes.items[0][2] in ("information", "warning"), notes.items
        assert_alive(app)
        assert app.screen is app.main
        assert app.left_panel.cursor_name == "A.txt"

        await pilot.press("f3")  # and again: the app is still usable
        await settle(pilot)
        assert len(notes.items) == 2 and no_subprocess == []


# -- the footer -----------------------------------------------------------------------------


@pytest.mark.parametrize("size", [(80, 24), (160, 40)], ids=["80x24", "160x40"])
async def test_the_footer_shows_3view_after_shell_and_before_edit(
    tree: Tree, local: RecordingLocal, size: tuple[int, int]
) -> None:
    app = view_app((local, tree.L), (local, tree.R), FakeCommand())
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        keys = sorted(app.main.query_one(Footer).query(FooterKey), key=lambda k: k.region.x)
        assert [k.key for k in keys] == [key for key, _ in MAIN_FOOTER]
        shown = [k.render().plain for k in keys]
        assert shown[1:4] == ["^OShell", "3View", "4Edit"], shown
        (f3,) = [k for k in keys if k.key == "f3"]
        width, height = size
        assert f3.region.width > 0 and f3.region.right <= width and f3.region.y == height - 1, f3.region


async def test_clicking_the_footers_3view_views_the_cursor_file_as_f3_does(tree: Tree, local: RecordingLocal) -> None:
    viewer = FakeCommand()
    app = view_app((local, tree.L), (local, tree.R), viewer)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "b.txt")
        await pilot.press("f3")
        await settle(pilot)
        assert viewer.calls == [viewed(f"{tree.L}/b.txt")]

        (key,) = [k for k in app.main.query_one(Footer).query(FooterKey) if k.key == "f3"]
        assert key.render().plain == "3View"
        await pilot.click(key)
        await settle(pilot)

        assert viewer.calls == [viewed(f"{tree.L}/b.txt")] * 2
        assert app.left_panel.cursor_name == "b.txt"
        assert app.screen is app.main
        assert_alive(app)


# -- the docstrings ------------------------------------------------------------------------


def _words(text: str) -> str:
    return " ".join(text.split())


def _paragraphs(doc: str | None) -> list[str]:
    return [_words(p) for p in re.split(r"\n\s*\n", doc or "") if p.strip()]


def test_the_app_docstring_has_one_find_line_passage_for_esc_space_a_bare_modifier_and_other_keys() -> None:
    """One passage says what each key does while the line is open, the
    text-less kitty keys included (Shift read as on a US layout); the old
    split wording ("Esc or any other key closes it", then a separate
    modifier sentence) is gone."""
    doc = _words(app_module.__doc__ or "")
    find = [p for p in _paragraphs(app_module.__doc__) if "find line" in p]
    assert len(find) == 1, find
    (passage,) = find
    for words in (
        "Esc closes it",
        "Space types a space",
        "modifier key pressed on its own",
        "leaves it open",
        "any other key closes it and does its own job",
        "kitty",
        "US layout",
    ):
        assert words in passage, (words, passage)
    assert "Esc or any other key closes it" not in doc
    assert "does not close the line" not in doc


def test_find_keys_docstring_is_followed_by_the_modifier_comment() -> None:
    source = inspect.getsource(FileManagerScreen._find_key)
    lines = [line.strip() for line in source.splitlines()]
    doc_end = next(i for i, line in enumerate(lines) if i > 1 and line.endswith('"""'))
    comment = lines[doc_end + 1]
    assert comment.startswith("#") and "modifier" in comment and "open" in comment, lines[: doc_end + 2]


@pytest.mark.parametrize("where", ["module", "action_view"])
def test_the_docstrings_give_f3s_viewer_order(where: str) -> None:
    """``[viewer] command``, then $PAGER, then ``less -R``, then more."""
    if where == "module":
        (text,) = [p for p in _paragraphs(app_module.__doc__) if p.startswith("F3 ")]
    else:
        text = _words(TwinFM.action_view.__doc__ or "")
    places = [text.find(word) for word in ("[viewer]", "$PAGER", "less -R", "more")]
    assert -1 not in places and places == sorted(places), (places, text)


def test_the_app_docstring_no_longer_says_the_footer_has_no_room_for_f3() -> None:
    doc = _words(app_module.__doc__ or "")
    assert "no room" not in doc, doc
    assert "$PAGER" in doc and "less" in doc, doc


def test_the_app_docstring_names_f3_ctrl_o_the_loading_rule_and_still_f4() -> None:
    doc = _words(app_module.__doc__ or "")
    assert "F3" in doc and "more" in doc, doc
    assert "Ctrl-O" in doc and "local shell" in doc and "never a btty" in doc, doc
    assert "loading" in doc and "only Tab, Shift+Tab and quit" in doc, doc
    for word in ("F4", "$VISUAL", "$EDITOR", "vim", "Ctrl-N", "Ctrl-R", "cache"):
        assert word in doc, word
    assert "F18" not in doc and "f18" not in doc
