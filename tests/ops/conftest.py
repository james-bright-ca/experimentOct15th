"""Fixtures for the engine tests (helpers are in tests/ops/support.py)."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.backend import fake_btty
from tests.backend.fake_btty import FakeBtty
from tests.ops.support import FAKES


@pytest.fixture(scope="session")
def fake_python() -> None:
    """The fake btty's python3 directories (with and without tarfile's
    extraction filters), made once per session in the real temporary
    directory, before ``systmp`` redirects it."""
    fake_btty.script_env()
    fake_btty.script_env(tar_filter=False)


@pytest.fixture(autouse=True)
def systmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_python: None) -> str:
    """Autouse: the system temporary directory (``tempfile``) is an empty
    directory of this test's own, so a test can check that the engine and
    the backends removed every local temporary they made."""
    path = tmp_path.resolve() / "systmp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return str(path)


@pytest.fixture(params=FAKES, ids=lambda cls: cls.__name__)
def fake_cls(request: pytest.FixtureRequest) -> type[FakeBtty]:
    """The fake btty class a World runs on: always the local ArchiveFakeBtty,
    plus the shared tests/backend FakeBtty once it runs the archive scripts
    itself (support.shared_fake_runs_archive_scripts)."""
    return request.param


@pytest.fixture
def chmod() -> Iterator[Callable[[str, int], None]]:
    """``chmod(path, mode)`` for this test; every path changed gets owner
    rwx back in teardown, so that tmp_path can be removed."""
    changed: list[str] = []

    def change(path: str, mode: int) -> None:
        changed.append(path)
        os.chmod(path, mode)

    yield change
    for path in reversed(changed):
        if os.path.lexists(path) and not os.path.islink(path):
            os.chmod(path, os.lstat(path).st_mode & 0o7777 | 0o700)
