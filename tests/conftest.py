"""Shared test configuration for twinfm.

The btty guard
--------------
The real ``btty`` drives a live Beacon IDE, so no test may ever run it.

* When pytest starts, a ``btty`` shim is put first on PATH. The autouse
  ``btty_guard`` fixture puts it back in front for every test, in case
  anything moved it.
* Running the shim does nothing except record the attempt, print a
  ``btty test guard`` message on stderr and exit with status 97
  (``GUARD_EXIT_STATUS``).
* The test phase (setup, call or teardown) during which the shim ran then
  fails, even if the code under test caught the error. A run outside any test
  (at import or collection time) fails the session.
* The terminal summary always reports how many btty executions were blocked.

The guard covers everything that finds ``btty`` through PATH: ``subprocess``
with an argv or with ``shell=True``, ``os.system``, ``os.exec*p``, and
``shutil.which`` (which returns the shim). That includes BttyBackend's default
runner with the default ``btty_command``.

The guard cannot stop a test that runs btty by absolute path, or one that
replaces PATH, so tests must do neither. A test that needs btty injects a fake
runner (tests/backend/fake_btty.py).

This module is frozen shared infrastructure. It depends only on the standard
library and pytest, so a pytester-based test can copy it into an inner session.
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

GUARD_MESSAGE = "btty test guard"
GUARD_EXIT_STATUS = 97

_SHIM = """\
#!/bin/sh
# {message}: stands in for the real btty, which a test must never run.
record=$(mktemp {records}/exec.XXXXXX 2>/dev/null) || record={records}/exec.$$.fallback
{{
  printf 'cwd: %s\\n' "$PWD"
  for arg in "$@"; do printf 'arg: %s\\n' "$arg"; done
}} > "$record" 2>/dev/null
printf '%s\\n' "{message}: refusing to run the real btty from a test; inject a fake runner" >&2
exit {status}
"""


class BttyGuard:
    """The shim directory and the record of every time the shim ran."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin_dir = root / "bin"
        self.records_dir = root / "records"
        self.shim = self.bin_dir / "btty"
        self.blamed: set[str] = set()  # records already charged to a test phase
        self.stray: set[str] = set()  # records no test phase accounts for

    def install(self) -> None:
        self.bin_dir.mkdir()
        self.records_dir.mkdir()
        self.shim.write_text(
            _SHIM.format(
                message=GUARD_MESSAGE,
                records=shlex.quote(str(self.records_dir)),
                status=GUARD_EXIT_STATUS,
            )
        )
        self.shim.chmod(0o755)

    def search_path(self, path: str | None) -> str:
        """The PATH value ``path`` with the shim directory first and nowhere else."""
        bin_dir = str(self.bin_dir)
        parts = (path if path else os.defpath).split(os.pathsep)
        return os.pathsep.join([bin_dir, *(p for p in parts if p != bin_dir)])

    def records(self) -> set[str]:
        """The names of every execution record written so far."""
        try:
            return {p.name for p in self.records_dir.iterdir()}
        except FileNotFoundError:
            return set()

    def describe(self, names: Iterable[str]) -> str:
        lines = []
        for name in sorted(names):
            try:
                text = (self.records_dir / name).read_text(errors="replace").strip()
            except OSError:
                text = "(record unreadable)"
            lines.append("  btty execution:\n" + "\n".join(f"    {line}" for line in text.splitlines()))
        return "\n".join(lines)


_GUARD = pytest.StashKey[BttyGuard]()


def pytest_configure(config: pytest.Config) -> None:
    guard = BttyGuard(Path(tempfile.mkdtemp(prefix="twinfm-btty-guard-")))
    guard.install()
    os.environ["PATH"] = guard.search_path(os.environ.get("PATH"))
    config.stash[_GUARD] = guard


def pytest_unconfigure(config: pytest.Config) -> None:
    guard = config.stash.get(_GUARD, None)
    if guard is None:
        return
    bin_dir = str(guard.bin_dir)
    path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(p for p in path.split(os.pathsep) if p != bin_dir)
    shutil.rmtree(guard.root, ignore_errors=True)


@pytest.fixture(autouse=True)
def btty_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> BttyGuard:
    """Autouse: the btty shim is first on PATH for the whole test.

    Request it by name to reach the guard, e.g. ``btty_guard.shim``.
    """
    guard = request.config.stash[_GUARD]
    monkeypatch.setenv("PATH", guard.search_path(os.environ.get("PATH")))
    return guard


def _blame(item: pytest.Item, phase: str, before: set[str]) -> None:
    guard = item.config.stash.get(_GUARD, None)
    if guard is None:
        return
    new = guard.records() - before - guard.blamed
    if not new:
        return
    guard.blamed |= new
    pytest.fail(
        f"{GUARD_MESSAGE}: the real btty must never run in tests, but `btty` was "
        f"executed {len(new)} time(s) during {phase} of {item.nodeid}.\n"
        f"{guard.describe(new)}\n"
        "Inject a fake runner instead.",
        pytrace=False,
    )


def _snapshot(item: pytest.Item) -> set[str]:
    guard = item.config.stash.get(_GUARD, None)
    return guard.records() if guard is not None else set()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Iterator[None]:
    before = _snapshot(item)
    try:
        return (yield)
    finally:
        _blame(item, "setup", before)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    before = _snapshot(item)
    try:
        return (yield)
    finally:
        _blame(item, "the test", before)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    before = _snapshot(item)
    try:
        return (yield)
    finally:
        _blame(item, "teardown", before)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    guard = session.config.stash.get(_GUARD, None)
    if guard is None:
        return
    guard.stray = guard.records() - guard.blamed
    if guard.stray:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    guard = config.stash.get(_GUARD, None)
    if guard is None:
        return
    if guard.stray:
        terminalreporter.write_sep("=", f"{GUARD_MESSAGE}: btty ran outside any test", red=True)
        terminalreporter.write_line(guard.describe(guard.stray))
    count = len(guard.records())
    terminalreporter.write_line(
        f"{GUARD_MESSAGE}: {count} btty execution(s) blocked", red=bool(count)
    )
