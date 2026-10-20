"""Ctrl-O hides the panels and shows a local shell (never a btty one); Quit
asks first while a command is still running in that shell.

Every app here gets a recording ``run_shell`` (FakeShellRunner) that returns
a scripted ShellVisit, and a FakeShell whose ``running`` the test sets. The
tests of the default ``run_shell`` give the app a real LocalShell instead,
with LocalShell's ``subprocess`` replaced by a recorder, so no process can
start. The shell itself is tested in test_shell.py.
"""

from __future__ import annotations

import os
import subprocess
import types
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

import twinfm.ui.shell as shell_module
from tests.ui.conftest import Tree, send_bytes
from tests.ui.support import (
    SIZE,
    FakeShell,
    FakeShellRunner,
    Notes,
    RecordingLocal,
    RecordingMemory,
    StubEngine,
    assert_alive,
    make_app,
    move_to,
    settle,
)
from twinfm.backend.btty import BttyBackend
from twinfm.config import Settings
from twinfm.ui.app import TwinFM
from twinfm.ui.dialogs import ConfirmDialog, InputDialog
from twinfm.ui.shell import LocalShell, ShellVisit

QUESTION = "A command is still running in the shell. Quit anyway?"


def shell_app(
    left: tuple[Any, str],
    right: tuple[Any, str],
    runner: FakeShellRunner | None,
    shell: Any = None,
    engine: StubEngine | None = None,
) -> TwinFM:
    return make_app(left, right, engine, run_shell=runner, shell=shell if shell is not None else FakeShell())


class NoBtty:
    """A btty runner that must never be called."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return 1, "", "test runner: btty must not be called here"


def snapshot(app: TwinFM) -> list[tuple[Any, ...]]:
    return [(p.backend, p.path, p.names, p.cursor_name) for p in (app.left_panel, app.right_panel)]


# -- where the shell starts --------------------------------------------------------------


@pytest.mark.parametrize("side", ["left", "right"])
async def test_ctrl_o_on_a_local_panel_starts_and_follows_in_its_directory(
    tree: Tree, local: RecordingLocal, side: str
) -> None:
    runner = FakeShellRunner()
    app = shell_app((local, tree.L), (local, tree.R), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        if side == "right":
            await pilot.press("tab")
            await pilot.pause()
        where = tree.L if side == "left" else tree.R

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert runner.calls == [(where, where)]
        assert_alive(app)


async def test_ctrl_o_on_a_memory_panel_starts_in_the_other_local_panel_and_follows_nothing(
    tree: Tree, local: RecordingLocal
) -> None:
    memory = RecordingMemory(home="/m")
    memory.add_dir("/m/d")
    runner = FakeShellRunner()
    app = shell_app((local, tree.L), (memory, "/m"), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel.backend is memory

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert runner.calls == [(tree.L, None)]


async def test_ctrl_o_on_a_btty_panel_shows_the_local_shell_of_the_other_panel(
    tree: Tree, local: RecordingLocal
) -> None:
    """Never a btty shell: the btty panel is not where the shell starts, and
    nothing is asked of btty."""
    runner = NoBtty()
    remote = BttyBackend(Settings(btty_exec=False), runner=runner)
    shell_runner = FakeShellRunner()
    app = shell_app((local, tree.L), (remote, "/mnt/home0/wst/workspaces"), shell_runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_panel.backend is remote

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert shell_runner.calls == [(tree.L, None)]
        assert runner.calls == []


async def test_ctrl_o_with_no_local_panel_starts_in_twinfms_own_directory() -> None:
    left, right = RecordingMemory(home="/a"), RecordingMemory(home="/b")
    left.add_dir("/a/x")
    right.add_dir("/b/y")
    runner = FakeShellRunner()
    app = shell_app((left, "/a"), (right, "/b"), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("ctrl+o")
        await settle(pilot)
        assert runner.calls == [(os.getcwd(), None)]


@pytest.mark.parametrize("data", ["\x0f", "\x1b[111;5u"], ids=["ctrl-o-byte", "kitty-ctrl-o"])
async def test_ctrl_o_from_terminal_bytes_runs_the_shell(tree: Tree, local: RecordingLocal, data: str) -> None:
    runner = FakeShellRunner()
    app = shell_app((local, tree.L), (local, tree.R), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        names = await send_bytes(pilot, data)
        await settle(pilot)
        assert runner.calls == [(tree.L, tree.L)], f"{data!r} parsed as {names}"


@pytest.mark.parametrize("side", ["left", "right"])
async def test_clicking_the_footers_shell_key_runs_the_shell_as_ctrl_o_does(
    tree: Tree, local: RecordingLocal, side: str
) -> None:
    """The footer's ``^OShell``: one click, one visit, started in and
    following the active panel's directory, as Ctrl-O's."""
    runner = FakeShellRunner()
    app = shell_app((local, tree.L), (local, tree.R), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        if side == "right":
            await pilot.press("tab")
            await pilot.pause()
        where = tree.L if side == "left" else tree.R
        await pilot.press("ctrl+o")
        await settle(pilot)
        assert runner.calls == [(where, where)]

        (key,) = [k for k in app.main.query_one(Footer).query(FooterKey) if k.key == "ctrl+o"]
        assert key.render().plain == "^OShell"
        await pilot.click(key)
        await settle(pilot)

        assert runner.calls == [(where, where)] * 2
        assert app.screen is app.main
        assert_alive(app)


# -- after the visit --------------------------------------------------------------------


async def test_a_visit_that_moved_lists_the_new_directory_in_the_local_panel(
    tree: Tree, local: RecordingLocal
) -> None:
    target = f"{tree.L}/Sub"
    runner = FakeShellRunner(ShellVisit(exited=None, cwd=target, moved=True))
    app = shell_app((local, tree.L), (local, tree.R), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        listed = len(local.listed)

        await pilot.press("ctrl+o")
        await settle(pilot)

        panel = app.left_panel
        assert panel.path == target
        assert panel.names == ["..", "inner.txt"]
        assert (target, False) in local.listed[listed:], "the new directory was not listed on a worker"
        assert app.right_panel.path == tree.R


async def test_a_visit_that_did_not_move_lists_nothing(tree: Tree, local: RecordingLocal) -> None:
    runner = FakeShellRunner(ShellVisit(exited=None, cwd=f"{tree.L}/Sub", moved=False))
    app = shell_app((local, tree.L), (local, tree.R), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before, listed = snapshot(app), len(local.listed)

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert len(runner.calls) == 1
        assert local.listed[listed:] == []
        assert snapshot(app) == before


async def test_a_non_local_active_panel_never_follows_the_shell(tree: Tree, local: RecordingLocal) -> None:
    memory = RecordingMemory(home="/m")
    memory.add_dir("/m/d")
    runner = FakeShellRunner(ShellVisit(exited=None, cwd=f"{tree.L}/Sub", moved=True))
    app = shell_app((local, tree.L), (memory, "/m"), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("tab")
        await pilot.pause()
        before, listed, mem_listed = snapshot(app), len(local.listed), len(memory.listed)

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert len(runner.calls) == 1
        assert snapshot(app) == before
        assert local.listed[listed:] == [] and memory.listed[mem_listed:] == []


def _symlinked(tree: Tree, tmp_path: Path) -> tuple[str, str]:
    link = tmp_path / "link-to-L"
    link.symlink_to(tree.left)
    return str(link), os.path.realpath(tree.left)


def _tmp(tree: Tree, tmp_path: Path) -> tuple[str, str]:
    return "/tmp", os.path.realpath("/tmp")  # /private/tmp on macOS


@pytest.mark.parametrize("paths", [_symlinked, _tmp], ids=["symlinked-dir", "tmp-and-private-tmp"])
async def test_a_cwd_with_the_same_real_path_as_the_panel_lists_nothing(
    tree: Tree, local: RecordingLocal, tmp_path: Path, paths: Any
) -> None:
    panel_path, cwd = paths(tree, tmp_path)
    runner = FakeShellRunner(ShellVisit(exited=None, cwd=cwd, moved=True))
    app = shell_app((local, panel_path), (local, tree.R), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        listed = len(local.listed)

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert runner.calls == [(panel_path, panel_path)]
        assert app.left_panel.path == panel_path
        assert local.listed[listed:] == []


async def test_a_shell_that_exited_gives_one_notice_and_changes_no_panel(
    tree: Tree, local: RecordingLocal
) -> None:
    runner = FakeShellRunner(ShellVisit(exited=3, cwd=None, moved=False))
    app = shell_app((local, tree.L), (local, tree.R), runner)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before, listed = snapshot(app), len(local.listed)

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert len(notes.items) == 1, notes.items
        message, _, severity = notes.items[0]
        assert severity == "information"
        assert "The shell exited (status 3)" in message and "Ctrl-O starts a new one" in message, message
        assert snapshot(app) == before
        assert local.listed[listed:] == []
        assert_alive(app)


# -- failures -----------------------------------------------------------------------------


@pytest.fixture
def popen_calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """LocalShell's subprocess module, with a Popen that records and refuses."""
    calls: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("a test tried to start a shell process")

    fake = types.SimpleNamespace(Popen=refuse, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(shell_module, "subprocess", fake)
    return calls


async def test_the_default_run_shell_in_a_headless_run_gives_a_notice_and_starts_nothing(
    tree: Tree, local: RecordingLocal, fake_shell_path: str, popen_calls: list[Any]
) -> None:
    shell = LocalShell([fake_shell_path, "-i"])
    app = make_app((local, tree.L), (local, tree.R), shell=shell)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert popen_calls == []
        assert len(notes.items) == 1, notes.items
        assert notes.items[0][2] in ("information", "warning"), notes.items
        assert shell.running is False
        assert_alive(app)
        assert app.screen is app.main

        await pilot.press("ctrl+o")  # and again: the app is still usable
        await settle(pilot)
        assert len(notes.items) == 2 and popen_calls == []


async def test_a_run_shell_that_raises_oserror_gives_one_error_notice(tree: Tree, local: RecordingLocal) -> None:
    runner = FakeShellRunner(raises=OSError(5, "Input/output error"))
    app = shell_app((local, tree.L), (local, tree.R), runner)
    notes = Notes(app)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        before = snapshot(app)
        await pilot.press("ctrl+o")
        await settle(pilot)

        assert len(runner.calls) == 1
        assert [(t, s) for _, t, s in notes.items] == [("Cannot start the shell", "error")], notes.items
        assert "Input/output error" in notes.items[0][0]
        assert snapshot(app) == before
        assert_alive(app)


# -- with the find line, under a dialog ------------------------------------------------------


async def test_ctrl_o_with_the_find_line_open_closes_it_then_runs_the_shell(
    tree: Tree, local: RecordingLocal
) -> None:
    app: TwinFM

    class Runner(FakeShellRunner):
        def __call__(self, start_dir: str, panel_dir: str | None) -> Any:
            self.line_open_at_call = app.left_panel.finding
            return super().__call__(start_dir, panel_dir)

    runner = Runner()
    app = shell_app((local, tree.L), (local, tree.R), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        panel = app.left_panel
        await pilot.press("b")
        await pilot.pause()
        assert panel.finding and panel.find_text == "b"

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert runner.calls == [(tree.L, tree.L)]
        assert runner.line_open_at_call is False, "the shell ran with the find line still open"
        assert not panel.finding


@pytest.mark.parametrize(
    ("key", "dialog"),
    [("f7", InputDialog), ("f8", ConfirmDialog), ("f9", InputDialog), ("f5", InputDialog)],
    ids=["mkdir", "delete", "connect", "copy"],
)
async def test_ctrl_o_under_a_dialog_does_nothing(
    tree: Tree, local: RecordingLocal, key: str, dialog: type
) -> None:
    runner = FakeShellRunner()
    app = shell_app((local, tree.L), (local, tree.R), runner)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await move_to(pilot, app.left_panel, "A.txt")
        await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, dialog), app.screen_stack
        opened = app.screen

        await pilot.press("ctrl+o")
        await settle(pilot)

        assert runner.calls == []
        assert app.screen is opened
        assert_alive(app)


# -- quit ----------------------------------------------------------------------------------


QUIT_KEYS = {"F10": ("f10",), "Esc-0": ("escape", "0"), "Ctrl-Q": ("ctrl+q",), "Alt-0": ("alt+0",)}


async def _press_quit(pilot: Any, how: str) -> None:
    if how == "Option-0":
        await send_bytes(pilot, "º")
    else:
        await pilot.press(*QUIT_KEYS[how])
    await pilot.pause()


@pytest.mark.parametrize("how", [*QUIT_KEYS, "Option-0"])
async def test_quit_while_a_command_runs_asks_and_cancel_keeps_the_app(
    tree: Tree, local: RecordingLocal, how: str
) -> None:
    shell = FakeShell(running=True)
    app = shell_app((local, tree.L), (local, tree.R), FakeShellRunner(), shell)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)

        await _press_quit(pilot, how)

        assert isinstance(app.screen, ConfirmDialog), f"{how} opened {app.screen_stack}"
        text = "\n".join(str(w.content) for w in app.screen.query("Static"))
        assert QUESTION in text, text
        assert str(app.screen.query_one("#yes").label) == "Quit"
        assert str(app.screen.query_one("#no").label) == "Cancel"

        await pilot.click("#no")
        await pilot.pause()

        assert_alive(app)
        assert app.screen is app.main
        assert shell.closed == 0
    assert shell.closed == 1


@pytest.mark.parametrize("how", [*QUIT_KEYS, "Option-0"])
async def test_quit_while_a_command_runs_ends_the_app_on_quit(tree: Tree, local: RecordingLocal, how: str) -> None:
    shell = FakeShell(running=True)
    app = shell_app((local, tree.L), (local, tree.R), FakeShellRunner(), shell)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await _press_quit(pilot, how)
        assert isinstance(app.screen, ConfirmDialog), f"{how} opened {app.screen_stack}"

        await pilot.click("#yes")
        await pilot.pause()

        assert app.return_code == 0
    assert not app.is_running
    assert shell.closed == 1


@pytest.mark.parametrize("how", [*QUIT_KEYS, "Option-0"])
async def test_quit_with_no_command_running_ends_the_app_at_once(
    tree: Tree, local: RecordingLocal, how: str
) -> None:
    shell = FakeShell(running=False)
    app = shell_app((local, tree.L), (local, tree.R), FakeShellRunner(), shell)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await _press_quit(pilot, how)
        assert not any(isinstance(s, ConfirmDialog) for s in app.screen_stack), app.screen_stack
        assert app.return_code == 0
    assert not app.is_running
    assert shell.closed == 1


async def test_the_app_closes_its_shell_once_on_exit_after_a_cancelled_question(
    tree: Tree, local: RecordingLocal
) -> None:
    shell = FakeShell(running=True)
    app = shell_app((local, tree.L), (local, tree.R), FakeShellRunner(), shell)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("f10")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert app.screen is app.main
        shell.running = False  # the command ended
        await pilot.press("f10")
        await pilot.pause()
        assert app.return_code == 0
    assert shell.closed == 1
