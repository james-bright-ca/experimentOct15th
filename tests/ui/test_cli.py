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
