"""Fixtures for the end-to-end tests (helpers are in support.py).

Every app here is the real TwinFM with the real engine (``twinfm.ops.engine``,
imported by the app itself), driven with Textual's Pilot. The panels hold
real backends: LocalBackend over a directory below tmp_path, and BttyBackend
whose runner is FakeBtty (tests/backend/fake_btty.py), which emulates btty
over a second directory below tmp_path and runs the remote python3 programs
locally. Nothing here can reach the real btty: the runner is injected, Connect
goes through FakeConnector, and the conftest PATH guard still stands behind
both. F4 gets a recording ``run_editor``, never vim.

Layout of ``world`` (all resolved, because macOS's /var is a symlink and
FakeBtty wants normalised absolute paths)::

    tmp_path/local          the local panel's directory
    tmp_path/remote         FakeBtty's root (nothing outside it is reachable)
    tmp_path/remote/home    the btty panel's directory (Settings.remote_home)
    tmp_path/remote/tmp     Settings.remote_tmp (archives of remote selections)
    tmp_path/systmp         tempfile's directory for the test, so that every
                            local temporary the engine or the backend makes
                            is visible, and its absence after an operation
                            can be checked
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.backend.fake_btty import FakeBtty, script_env, settings_for
from tests.integration.support import ARCHIVE_TIMEOUT, TIMEOUT, World
from twinfm.backend.btty import BttyBackend
from twinfm.backend.local import LocalBackend


@pytest.fixture(autouse=True)
def no_editor_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user's own $VISUAL / $EDITOR never reach a test."""
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)


@pytest.fixture
def systmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tempfile's directory for this test. FakeBtty's python3 directories are
    made first (they are cached for the session), so they stay outside it."""
    script_env()
    script_env(tar_filter=False)
    path = tmp_path.resolve() / "systmp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


@pytest.fixture
def unlock() -> Iterator[list[Path]]:
    """Directories a test made read-only; owner rwx is restored afterwards,
    so that tmp_path can be removed."""
    locked: list[Path] = []
    yield locked
    for path in locked:
        if path.exists():
            os.chmod(path, 0o755)


@pytest.fixture
def world(tmp_path: Path, systmp: Path) -> Iterator[World]:
    """An empty local directory and an empty remote home, with a BttyBackend
    in exec mode over FakeBtty (both timeouts checked on every exec argv)."""
    base = tmp_path.resolve()
    local = base / "local"
    store = base / "remote"
    home = store / "home"
    rtmp = store / "tmp"
    for path in (local, home, rtmp):
        path.mkdir(parents=True)
    fake = FakeBtty(str(store), timeout=TIMEOUT, archive_timeout=ARCHIVE_TIMEOUT)
    settings = settings_for(fake, str(home), str(rtmp))
    assert settings.btty_exec is True
    world = World(
        local=local,
        store=store,
        home=home,
        rtmp=rtmp,
        systmp=systmp,
        fake=fake,
        settings=settings,
        local_backend=LocalBackend(),
        btty=BttyBackend(settings, runner=fake),
    )
    yield world
    assert fake.rejected == [], "a command over COMMAND_BUDGET was sent to btty"
    assert "?" not in fake.kinds, "the fake was sent an argv it does not know"
