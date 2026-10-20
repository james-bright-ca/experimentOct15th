"""The command line: --help lists the flags, and `twinfm LEFT RIGHT` opens
the panels there (or both at the current directory without arguments)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ui.conftest import Tree
from tests.ui.support import SIZE, settle
from twinfm.__main__ import build_app

FLAGS = ["--btty-exec", "--no-btty-exec", "--remote-home", "--config", "--version"]


def test_python_m_twinfm_help_exits_0_and_lists_every_flag() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "twinfm", "--help"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    missing = [flag for flag in FLAGS if flag not in result.stdout]
    assert missing == [], f"--help does not list {missing}:\n{result.stdout}"


def _help_of(argv0: list[str]) -> str:
    """``argv0 --help`` as one line of words: argparse wraps to the terminal
    width, so the wording is checked with whitespace collapsed."""
    env = {**os.environ, "COLUMNS": "100", "NO_COLOR": "1"}
    result = subprocess.run(
        [*argv0, "--help"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return " ".join(result.stdout.split())


def _console_script() -> list[str]:
    """The installed ``twinfm`` command next to this interpreter (.venv/bin/twinfm)."""
    script = Path(sys.executable).with_name("twinfm")
    assert script.is_file(), f"no twinfm console script next to {sys.executable}; is the project installed?"
    return [str(script)]


@pytest.mark.parametrize(
    "argv0", [lambda: [sys.executable, "-m", "twinfm"], _console_script], ids=["python-m-twinfm", "twinfm-script"]
)
def test_help_says_exec_mode_is_on_by_default_and_no_btty_exec_turns_it_off(argv0) -> None:  # type: ignore[no-untyped-def]
    """[D1] exec mode is the default and --no-btty-exec is the opt-out."""
    text = _help_of(argv0())
    assert "exec mode, on by default" in text, text
    assert "--no-btty-exec turns it off" in text, text
    for flag in FLAGS:
        assert flag in text, f"--help does not list {flag}: {text}"


def test_help_does_not_call_exec_mode_off_by_default() -> None:
    """The first iteration's help said exec was opt-in; nothing may say so now."""
    text = _help_of([sys.executable, "-m", "twinfm"]).lower()
    for stale in ("off by default", "disabled by default", "transfer-only by default", "default: off"):
        assert stale not in text, f"--help still says {stale!r}: {text}"


def test_version_exits_0_and_names_the_program() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "twinfm", "--version"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("twinfm "), result.stdout


@pytest.fixture
def no_config(tmp_path: Path) -> list[str]:
    """Flags that point the app at a config file that does not exist, so the
    user's real config never leaks into a test."""
    return ["--config", str(tmp_path / "absent.toml")]


def assert_header_shows(panel, location: str) -> None:  # type: ignore[no-untyped-def]
    """The header shows ``location``, or (when it is too long for the panel)
    its tail after a leading ellipsis, so the directory's own name stays visible."""
    shown = str(panel.query_one("#header").render())
    if shown != location:
        assert shown.startswith("…") and location.endswith(shown[1:]), (shown, location)
        assert shown.endswith("/" + location.rsplit("/", 1)[1]), (shown, location)


async def test_left_and_right_arguments_open_the_two_panels(tree: Tree, no_config: list[str]) -> None:
    app = build_app([tree.L, tree.R, *no_config])
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert app.left_panel.path == tree.L
        assert app.right_panel.path == tree.R
        assert_header_shows(app.left_panel, f"local:{tree.L}")
        assert_header_shows(app.right_panel, f"local:{tree.R}")
        assert "A.txt" in app.left_panel.names
        assert app.right_panel.names == [".."]


async def test_relative_arguments_resolve_against_the_current_directory(
    tree: Tree, no_config: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tree.left.parent)
    app = build_app(["L", "R", *no_config])
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        assert (app.left_panel.path, app.right_panel.path) == (tree.L, tree.R)


async def test_no_arguments_open_both_panels_at_the_current_directory(
    tree: Tree, no_config: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tree.left)
    app = build_app(no_config)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        cwd = os.getcwd()
        assert app.left_panel.path == cwd
        assert app.right_panel.path == cwd
        assert app.left_panel.names == app.right_panel.names
        assert "A.txt" in app.left_panel.names
