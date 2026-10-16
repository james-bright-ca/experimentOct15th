"""Fixtures for the UI tests: a local tree on tmp_path and the test doubles
from tests/ui/support.py."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.ui.support import RecordingLocal, StubEngine

FILES = {"b.txt": b"bb", "A.txt": b"aaa", ".hidden": b"h", "c.txt": b"c" * 2000, "Zeta.md": b"z"}
DIRS = ["Sub", "alpha", ".git"]
LEFT_ORDER = ["..", ".git", "alpha", "Sub", ".hidden", "A.txt", "b.txt", "c.txt", "Zeta.md"]
"""The left directory in MC order: `..`, directories, files, each case-insensitive."""


@dataclass
class Tree:
    """LEFT holds FILES and DIRS (Sub has one file); RIGHT is empty."""

    left: Path
    right: Path

    @property
    def L(self) -> str:
        return str(self.left)

    @property
    def R(self) -> str:
        return str(self.right)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    left = tmp_path / "L"
    right = tmp_path / "R"
    left.mkdir()
    right.mkdir()
    for name, data in FILES.items():
        (left / name).write_bytes(data)
    for name in DIRS:
        (left / name).mkdir()
    (left / "Sub" / "inner.txt").write_bytes(b"inner")
    return Tree(left, right)


@pytest.fixture
def local() -> RecordingLocal:
    return RecordingLocal()


@pytest.fixture
def engine() -> StubEngine:
    return StubEngine()
