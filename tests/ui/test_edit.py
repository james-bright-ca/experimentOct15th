"""F4 edits a local file: the injected ``run_editor`` gets
shlex.split($VISUAL, else $EDITOR, else "vim") plus the file's absolute
path, and the panel lists again afterwards with the cursor on the file.
Other panels, directories and ``..`` are refused; Esc 4, Alt-4 and the ``¢``
of a fast ESC+4 or Option-4 do what F4 does; under a dialog F4 does nothing.

No test here starts a real editor: every app gets a recording
``run_editor``, except the one that checks the default launcher in a
headless run, where ``subprocess.run`` is replaced by a recorder too.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import twinfm.ui.app as app_module
from tests.ui.conftest import Tree, send_bytes
from tests.ui.support import (
    SIZE,
    TRANSFER_ONLY,
    FakeConnector,
    Notes,
    RecordingLocal,
    RecordingMemory,
    StubEngine,
    assert_alive,
    move_to,
    settle,
)
from twinfm.backend.base import Backend
from twinfm.backend.btty import BttyBackend
from twinfm.config import Settings
from twinfm.ui.app import TwinFM
from twinfm.ui.dialogs import ConfirmDialog, InputDialog

LOCAL_ONLY = "F4 edits local files only; copy the file to a local panel to edit it"


class FakeEditor:
    """A ``run_editor`` that records each argv and returns ``status`` (or
    raises ``raises``), after running ``effect`` on the argv."""

    def __init__(
        self,
        status: int = 0,
        raises: BaseException | None = None,
        effect: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.status = status
        self.raises = raises
        self.effect = effect
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        if self.effect is not None:
            self.effect(argv)
        if self.raises is not None:
            raise self.raises
        return self.status


class NoBtty:
    """A btty runner that must never be called (a transfer-only panel does not list)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return 1, "", "test runner: btty must not be called here"


def edit_app(
    left: tuple[Backend, str],
    right: tuple[Backend, str],
    editor: FakeEditor | None,
    engine: StubEngine | None = None,
) -> TwinFM:
    """TwinFM with a stub engine, a fake connector and ``editor`` as
    run_editor (None: the app's default launcher)."""
    return TwinFM(
        left,
        right,
        Settings(btty_exec=False),
        engine=engine if engine is not None else StubEngine(),
        open_backend=FakeConnector(),
        run_editor=editor,
    )


@pytest.fixture(autouse=True)
def no_editor_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user's own $VISUAL / $EDITOR never reach a test."""
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace the app module's subprocess.run, so that even the default
    launcher can never start a real editor; records what it was asked to run."""
    ran: list[list[str]] = []

    def refuse(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(app_module.subprocess, "run", refuse)
    return ran


# -- the editor command -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("variables", "command"),
    [
        ({}, ["vim"]),
        ({"EDITOR": "nano"}, ["nano"]),
        ({"EDITOR": "nano -w"}, ["nano", "-w"]),
        ({"VISUAL": "code --wait", "EDITOR": "nano"}, ["code", "--wait"]),
        ({"EDITOR": "'/Applications/My Editor/bin/ed' --new-window"}, ["/Applications/My Editor/bin/ed", "--new-window"]),
        ({"VISUAL": ""}, ["vim"]),
        ({"EDITOR": ""}, ["vim"]),
        ({"VISUAL": "   "}, ["vim"]),
        ({"VISUAL": "vim 'unclosed"}, ["vim"]),
        ({"VISUAL": "emacs 'unclosed", "EDITOR": "nano"}, ["vim"]),
    ],
    ids=[
        "neither-set-vim",
        "EDITOR",
        "EDITOR-with-argument",
        "VISUAL-wins-over-EDITOR",
        "EDITOR-quoted-path-with-space",
        "VISUAL-empty-vim",
        "EDITOR-empty-vim",
        "VISUAL-blank-vim",
        "VISUAL-unparsable-vim",
        "chosen-value-unparsable-vim-not-EDITOR",
    ],
)
async def test_f4_runs_the_editor_command_with_the_absolute_path(
    tree: Tree,
    local: RecordingLocal,
    monkeypatch: pytest.MonkeyPatch,
    variables: dict[str, str],
    command: list[str],
) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")

        await pilot.press("f4")
        await settle(pilot)

        assert editor.calls == [[*command, f"{tree.L}/A.txt"]]
        assert_alive(app)


async def test_the_editor_path_is_one_argument_even_with_spaces_and_quotes(
    tree: Tree, local: RecordingLocal, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "my notes 'draft' $HOME.txt"
    (tree.left / name).write_bytes(b"n")
    monkeypatch.setenv("EDITOR", "vi")
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, name)
        await pilot.press("f4")
        await settle(pilot)
        assert editor.calls == [["vi", f"{tree.L}/{name}"]]


async def test_f4_follows_the_environment_at_the_time_of_the_key(
    tree: Tree, local: RecordingLocal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$VISUAL / $EDITOR are read when F4 is pressed, not when the app starts."""
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        monkeypatch.setenv("EDITOR", "nano")
        await pilot.press("f4")
        await settle(pilot)
        assert editor.calls == [["nano", f"{tree.L}/A.txt"]]


# -- afterwards ------------------------------------------------------------------------


async def test_after_the_editor_the_panel_lists_again_with_the_cursor_on_the_file(
    tree: Tree, local: RecordingLocal
) -> None:
    """The editor grows the file and leaves a swap file that sorts above it:
    the listing shows the new size and the new file, and the cursor stays on
    the edited file although its row moved down."""

    def edit(argv: list[str]) -> None:
        path = Path(argv[-1])
        path.write_bytes(path.read_bytes() + b" and more")
        (path.parent / ".A.txt.swp").write_bytes(b"swap")

    editor = FakeEditor(effect=edit)
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await move_to(pilot, panel, "A.txt")
        row_before = panel.names.index("A.txt")
        listed_before = len(local.listed)

        await pilot.press("f4")
        await settle(pilot)

        assert len(editor.calls) == 1
        # Both panels show this backend, so both list again, as after any operation.
        assert tree.L in [p for p, _ in local.listed[listed_before:]], "the panel did not list again"
        assert ".A.txt.swp" in panel.names
        assert panel.names.index("A.txt") == row_before + 1
        assert panel.cursor_name == "A.txt"
        entry = panel.cursor_entry
        assert entry is not None and entry.size == len(b"aaa and more")
        size_cell = panel.query_one("#table").get_cell("A.txt", "size")
        assert str(size_cell) == str(len(b"aaa and more")), size_cell
        assert_alive(app)
        assert app.screen is app.main


async def test_a_symlink_to_a_file_is_edited_at_the_link_path(tree: Tree, local: RecordingLocal) -> None:
    os.symlink("A.txt", tree.left / "link-to-A")
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "link-to-A")
        await pilot.press("f4")
        await settle(pilot)
        assert editor.calls == [["vim", f"{tree.L}/link-to-A"]]


async def test_f4_in_the_right_panel_edits_the_right_panels_file(tree: Tree, local: RecordingLocal) -> None:
    (tree.right / "r.txt").write_bytes(b"r")
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        await move_to(pilot, app.right_panel, "r.txt")
        await pilot.press("f4")
        await settle(pilot)
        assert editor.calls == [["vim", f"{tree.R}/r.txt"]]
        assert app.right_panel.cursor_name == "r.txt"


# -- refusals --------------------------------------------------------------------------


async def test_f4_on_a_memory_panel_warns_local_only_and_does_not_edit(
    tree: Tree, local: RecordingLocal
) -> None:
    memory = RecordingMemory(home="/m")
    memory.add_file("/m/notes.txt", b"n")
    editor = FakeEditor()
    app = edit_app((local, tree.L), (memory, "/m"), editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        await move_to(pilot, app.right_panel, "notes.txt")

        await pilot.press("f4")
        await settle(pilot)

        assert editor.calls == []
        assert [(m, s) for m, _, s in notes.items] == [(LOCAL_ONLY, "warning")]
        assert_alive(app)


async def test_f4_on_a_btty_panel_warns_local_only_and_does_not_edit(
    tree: Tree, local: RecordingLocal
) -> None:
    runner = NoBtty()
    remote = BttyBackend(Settings(btty_exec=False), runner=runner)
    editor = FakeEditor()
    app = edit_app((local, tree.L), (remote, "/mnt/home0/wst/workspaces"), editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel.backend is remote

        await pilot.press("f4")
        await settle(pilot)

        assert editor.calls == []
        assert [(m, s) for m, _, s in notes.items] == [(LOCAL_ONLY, "warning")]
        assert runner.calls == []
        assert_alive(app)


async def test_f4_on_a_transfer_only_memory_panel_warns_local_only(tree: Tree, local: RecordingLocal) -> None:
    remote = RecordingMemory(TRANSFER_ONLY, home="/drop")
    editor = FakeEditor()
    app = edit_app((local, tree.L), (remote, "/drop"), editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("f4")
        await settle(pilot)
        assert editor.calls == []
        assert [(m, s) for m, _, s in notes.items] == [(LOCAL_ONLY, "warning")]


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
    [lambda tree: "Sub", _make_dir_link, _make_fifo, _make_dangling_link],
    ids=["directory", "symlink-to-directory", "fifo", "dangling-symlink"],
)
async def test_f4_on_something_other_than_a_file_warns_and_does_not_edit(
    tree: Tree, local: RecordingLocal, make: Callable[[Tree], str]
) -> None:
    name = make(tree)
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, name)

        await pilot.press("f4")
        await settle(pilot)

        assert editor.calls == []
        assert [s for _, _, s in notes.items] == ["warning"], notes.items
        assert name in notes.items[0][0], notes.items
        assert app.left_panel.path == tree.L, "F4 does not enter a directory"
        assert_alive(app)


async def test_f4_on_the_parent_entry_does_nothing(tree: Tree, local: RecordingLocal) -> None:
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert app.left_panel.cursor_name == ".."
        listed_before = len(local.listed)

        await pilot.press("f4")
        await settle(pilot)

        assert editor.calls == []
        assert notes.items == []
        assert len(local.listed) == listed_before
        assert app.screen is app.main
        assert app.left_panel.path == tree.L


@pytest.mark.parametrize(
    ("key", "dialog"),
    [("f7", InputDialog), ("f8", ConfirmDialog), ("f9", InputDialog), ("f5", InputDialog)],
    ids=["mkdir", "delete", "connect", "copy"],
)
async def test_f4_under_a_dialog_does_not_edit(
    tree: Tree, local: RecordingLocal, key: str, dialog: type
) -> None:
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, dialog), app.screen_stack
        opened = app.screen

        await pilot.press("f4")
        await settle(pilot)

        assert editor.calls == []
        assert app.screen is opened
        assert_alive(app)


async def test_cent_sign_typed_in_a_dialog_is_text_not_edit(tree: Tree, local: RecordingLocal) -> None:
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("f7")
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)

        await send_bytes(pilot, "¢")

        assert editor.calls == []
        assert app.screen.query_one("Input").value == "¢"
        assert_alive(app)


# -- other keys for F4 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "keys",
    [("f4",), ("escape", "4"), ("alt+4",)],
    ids=["F4", "Esc-then-4", "Alt-4"],
)
async def test_pilot_keys_for_edit(tree: Tree, local: RecordingLocal, keys: tuple[str, ...]) -> None:
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "b.txt")
        await pilot.press(*keys)
        await settle(pilot)
        assert editor.calls == [["vim", f"{tree.L}/b.txt"]]


@pytest.mark.parametrize(
    "chunks",
    [("\x1bOS",), ("\x1b[14~",), ("\x1b4",), ("\x1b", "4"), ("¢",)],
    ids=["F4-xterm-Terminal.app", "F4-vt220", "Esc-4-fast", "Esc-4-slow", "Option-4"],
)
async def test_terminal_bytes_for_edit(tree: Tree, local: RecordingLocal, chunks: tuple[str, ...]) -> None:
    """Real bytes through Textual's parser: a fast ESC+4 and Option-4 both
    arrive as ``¢`` (cent_sign), which Pilot's press() cannot produce."""
    editor = FakeEditor()
    app = edit_app((local, tree.L), (local, tree.R), editor)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "b.txt")
        names = await send_bytes(pilot, *chunks)
        await settle(pilot)
        assert editor.calls == [["vim", f"{tree.L}/b.txt"]], f"{chunks!r} parsed as {names}"
        assert_alive(app)


@pytest.mark.parametrize("size", [SIZE, (80, 24)], ids=["wide", "80x24"])
async def test_footer_shows_f4_edit(tree: Tree, local: RecordingLocal, size: tuple[int, int]) -> None:
    from textual.widgets import Footer
    from textual.widgets._footer import FooterKey

    app = edit_app((local, tree.L), (local, tree.R), FakeEditor())
    async with app.run_test(size=size) as pilot:
        await settle(pilot)
        keys = [k for k in app.main.query_one(Footer).query(FooterKey) if k.key == "f4"]
        assert len(keys) == 1, [(k.key, k.description) for k in app.main.query_one(Footer).query(FooterKey)]
        key = keys[0]
        assert (key.key_display, key.description) == ("F4", "Edit")
        assert "F4" in key.render().plain and "Edit" in key.render().plain
        width, height = size
        assert 0 <= key.region.x and key.region.right <= width and key.region.y == height - 1, key.region


# -- the editor's outcome ----------------------------------------------------------------


async def test_an_editor_that_cannot_start_gives_an_error_notice(tree: Tree, local: RecordingLocal) -> None:
    editor = FakeEditor(raises=FileNotFoundError(2, "No such file or directory", "vim"))
    app = edit_app((local, tree.L), (local, tree.R), editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f4")
        await settle(pilot)

        assert len(editor.calls) == 1
        assert [s for _, _, s in notes.items] == ["error"], notes.items
        assert "No such file or directory" in notes.items[0][0], notes.items
        assert_alive(app)
        assert app.screen is app.main
        assert (tree.left / "A.txt").read_bytes() == b"aaa"


@pytest.mark.parametrize("status", [1, 2, 127])
async def test_a_non_zero_exit_gives_a_warning(tree: Tree, local: RecordingLocal, status: int) -> None:
    editor = FakeEditor(status=status)
    app = edit_app((local, tree.L), (local, tree.R), editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f4")
        await settle(pilot)

        assert [s for _, _, s in notes.items] == ["warning"], notes.items
        assert str(status) in notes.items[0][0], notes.items
        assert_alive(app)


async def test_a_zero_exit_gives_no_notice(tree: Tree, local: RecordingLocal) -> None:
    editor = FakeEditor(status=0)
    app = edit_app((local, tree.L), (local, tree.R), editor)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press("f4")
        await settle(pilot)
        assert len(editor.calls) == 1
        assert notes.items == []


async def test_the_default_launcher_in_a_headless_run_gives_a_notice_and_the_app_keeps_running(
    tree: Tree, local: RecordingLocal, no_subprocess: list[list[str]]
) -> None:
    """No run_editor injected: the app cannot hand a headless terminal to an
    editor (Textual's SuspendNotSupported), says so, and never spawns one."""
    app = edit_app((local, tree.L), (local, tree.R), editor=None)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")

        await pilot.press("f4")
        await settle(pilot)

        assert no_subprocess == [], "the default launcher started a process in a headless run"
        assert len(notes.items) == 1, notes.items
        assert notes.items[0][2] in ("information", "warning"), notes.items
        assert_alive(app)
        assert app.screen is app.main
        assert app.left_panel.cursor_name == "A.txt"

        await pilot.press("f4")  # and again: the app is still usable
        await settle(pilot)
        assert len(notes.items) == 2 and no_subprocess == []


def test_the_app_docstring_names_f4_and_its_editor_order() -> None:
    doc = app_module.__doc__ or ""
    assert "F4" in doc, doc
    assert "$VISUAL" in doc and "$EDITOR" in doc and "vim" in doc, doc
