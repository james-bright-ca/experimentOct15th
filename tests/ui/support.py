"""Helpers for the headless Pilot tests of the twinfm UI.

* ``StubEngine`` implements the Engine protocol (ops/model.py) with plain
  backend primitives, so a test sees the effect of an operation in the
  panels without depending on the real engine. It records every call, the
  thread it ran on, and every answer the UI gave to a question.
* ``RecordingLocal`` / ``RecordingMemory`` are backends that record the
  thread each ``list_dir`` ran on.
* ``FakeConnector`` stands in for the backend registry used by F9 (Connect),
  so no test ever opens a real btty backend.
* ``Notes`` records every notification the app shows.
* ``settle`` / ``wait_until`` / ``move_to`` drive the Pilot.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from textual.pilot import Pilot
from textual.widgets import Input, Static

from twinfm.backend.base import Backend, Cap, Entry, FsError
from twinfm.backend.local import LocalBackend
from twinfm.backend.memory import MemoryBackend
from twinfm.config import Settings
from twinfm.ops.model import Callbacks, Conflict, OnError, Progress, Report
from twinfm.ui.app import TwinFM
from twinfm.ui.panel import Panel

SIZE = (160, 40)
"""Terminal size for every Pilot run: wide enough that dialogs and headers fit."""

TRANSFER_ONLY = frozenset({Cap.UPLOAD, Cap.DOWNLOAD})


def on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


# -- backends ---------------------------------------------------------------------


class RecordingLocal(LocalBackend):
    """LocalBackend that records ``(path, ran_on_main_thread)`` per list_dir."""

    def __init__(self) -> None:
        super().__init__()
        self.listed: list[tuple[str, bool]] = []

    def list_dir(self, path: str) -> list[Entry]:
        self.listed.append((path, on_main_thread()))
        return super().list_dir(path)


class RecordingMemory(MemoryBackend):
    """MemoryBackend that records ``(path, ran_on_main_thread)`` per list_dir."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.listed: list[tuple[str, bool]] = []
        super().__init__(*args, **kwargs)

    def list_dir(self, path: str) -> list[Entry]:
        self.listed.append((path, on_main_thread()))
        return super().list_dir(path)


class FakeConnector:
    """The ``open_backend`` given to TwinFM: returns the backend registered
    here for a kind, records each call and its thread, and never opens a real
    backend (in particular never a real btty)."""

    def __init__(self, backends: dict[str, Backend] | None = None) -> None:
        self.backends = dict(backends or {})
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, kind: str, settings: Settings) -> Backend:
        self.calls.append((kind, on_main_thread()))
        try:
            return self.backends[kind]
        except KeyError:
            raise LookupError(f"backend {kind!r} is not available in this test") from None


# -- engine -----------------------------------------------------------------------


@dataclass
class Call:
    """One engine call. ``archive`` is the keyword copy and move were given
    (False when the caller left it out); None for every other method."""

    name: str
    on_main_thread: bool
    args: tuple[Any, ...]
    archive: bool | None = None


@dataclass
class StubEngine:
    """The Engine protocol over backend primitives.

    copy / move put each selected path into ``target`` (an existing directory)
    under its own name. A destination that exists (when the destination has
    STAT) is a conflict asked through ``cb.ask_conflict``; a path listed in
    ``fail`` is an item error asked through ``cb.ask_error``. ``between`` is
    called with the item index after each item, on the worker thread.

    copy / move take the keyword-only ``archive`` of the Engine protocol and
    record it in ``Call.archive`` (see ``archives``); the stub transfers item
    by item whatever it says.
    """

    calls: list[Call] = field(default_factory=list)
    answers: list[Conflict | OnError] = field(default_factory=list)
    fail: dict[str, FsError] = field(default_factory=dict)
    between: Callable[[int], None] | None = None

    def _record(self, name: str, *args: Any, archive: bool | None = None) -> None:
        self.calls.append(Call(name, on_main_thread(), args, archive))

    def names(self) -> list[str]:
        return [c.name for c in self.calls]

    def archives(self) -> list[tuple[str, bool | None]]:
        """``(name, archive)`` for every copy and move call, in order."""
        return [(c.name, c.archive) for c in self.calls if c.name in ("copy", "move")]

    def copy(
        self,
        src: Backend,
        paths: Sequence[str],
        dst: Backend,
        target: str,
        cb: Callbacks,
        *,
        archive: bool = False,
    ) -> Report:
        self._record("copy", src, list(paths), dst, target, archive=archive)
        return self._transfer("copy", src, paths, dst, target, cb)

    def move(
        self,
        src: Backend,
        paths: Sequence[str],
        dst: Backend,
        target: str,
        cb: Callbacks,
        *,
        archive: bool = False,
    ) -> Report:
        self._record("move", src, list(paths), dst, target, archive=archive)
        return self._transfer("move", src, paths, dst, target, cb)

    def delete(self, b: Backend, paths: Sequence[str], cb: Callbacks) -> Report:
        self._record("delete", b, list(paths))
        report = Report()
        for path in paths:
            b.delete(path, recursive=True)
            report.done.append(path)
        return report

    def mkdir(self, b: Backend, parent: str, name: str) -> str:
        self._record("mkdir", b, parent, name)
        path = b.join(parent, name)
        b.mkdir(path)
        return path

    def rename(self, b: Backend, path: str, new_name: str, cb: Callbacks) -> Report:
        self._record("rename", b, path, new_name)
        b.rename(path, b.join(b.parent(path), new_name))
        return Report(done=[path])

    def _transfer(
        self, op: str, src: Backend, paths: Sequence[str], dst: Backend, target: str, cb: Callbacks
    ) -> Report:
        report = Report()
        for index, path in enumerate(paths):
            if cb.cancelled():
                report.cancelled = True
                break
            cb.progress(Progress(op, path, index, len(paths)))
            dest = dst.join(target, posixpath.basename(path))
            if path in self.fail:
                err = self.fail[path]
                answer = cb.ask_error(path, err)
                self.answers.append(answer)
                report.failed.append((path, err))
                if answer is OnError.ABORT:
                    report.cancelled = True
                    break
                continue
            if Cap.STAT in dst.caps and dst.exists(dest):
                conflict = cb.ask_conflict(src.stat(path), dst.stat(dest))
                self.answers.append(conflict)
                if conflict is Conflict.CANCEL:
                    report.cancelled = True
                    break
                if conflict in (Conflict.SKIP, Conflict.SKIP_ALL):
                    report.skipped.append(path)
                    continue
            self._put(op, src, path, dst, dest)
            report.done.append(path)
            if self.between is not None:
                self.between(index)
        return report

    @staticmethod
    def _put(op: str, src: Backend, path: str, dst: Backend, dest: str) -> None:
        if src is dst:
            if op == "move":
                src.rename(path, dest, overwrite=True)
            else:
                src.copy_within(path, dest, overwrite=True)
            return
        stage = tempfile.mkdtemp(prefix="twinfm-ui-test-")
        try:
            local = os.path.join(stage, "item")
            src.download(path, local)
            dst.upload(local, dest, overwrite=True)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        if op == "move":
            src.delete(path, recursive=True)


class Hold:
    """``StubEngine.between`` that stops the worker after the first item until
    ``release`` is set; ``reached`` tells the test it got there."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, index: int) -> None:
        if index == 0:
            self.reached.set()
            self.release.wait(10)


# -- the app ----------------------------------------------------------------------


def make_app(
    left: tuple[Backend, str],
    right: tuple[Backend, str],
    engine: StubEngine | None = None,
    connector: FakeConnector | None = None,
) -> TwinFM:
    """TwinFM with a stub engine and a fake connector (never the registry)."""
    return TwinFM(
        left,
        right,
        Settings(btty_exec=False),
        engine=engine if engine is not None else StubEngine(),
        open_backend=connector if connector is not None else FakeConnector(),
    )


class Notes:
    """Records every ``app.notify`` call (message, title, severity) and still
    shows the notification."""

    def __init__(self, app: TwinFM) -> None:
        self.items: list[tuple[str, str, str]] = []
        original = app.notify

        def spy(message: str, **kwargs: Any) -> None:
            self.items.append((message, kwargs.get("title", ""), kwargs.get("severity", "information")))
            original(message, **kwargs)

        app.notify = spy  # type: ignore[method-assign]

    def messages(self) -> list[str]:
        return [m for m, _, _ in self.items]


async def settle(pilot: Pilot[Any], rounds: int = 4) -> None:
    """Let every worker finish and the UI catch up. Only for moments when no
    worker waits for a dialog answer (use ``wait_until`` then)."""
    for _ in range(rounds):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def wait_until(pilot: Pilot[Any], predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.02)
    return predicate()


async def move_to(pilot: Pilot[Any], panel: Panel, name: str) -> None:
    """Move the focused panel's cursor onto ``name`` with the arrow keys."""
    names = panel.names
    assert name in names, f"{name!r} is not listed in {panel.path}: {names}"
    delta = names.index(name) - names.index(panel.cursor_name or names[0])
    if delta:
        await pilot.press(*(["down"] * delta if delta > 0 else ["up"] * -delta))
    assert panel.cursor_name == name, f"cursor on {panel.cursor_name!r}, wanted {name!r}"


def dialog_text(app: TwinFM) -> str:
    """Every label and text block on the current screen, joined by newlines."""
    return "\n".join(str(w.content) for w in app.screen.query(Static))


def dialog_title(app: TwinFM) -> str:
    return str(app.screen.query_one(".title", Static).content)


def input_value(app: TwinFM) -> str:
    return app.screen.query_one(Input).value


def focused_id(app: TwinFM) -> str | None:
    focused = app.screen.focused
    return focused.id if focused is not None else None


def assert_alive(app: TwinFM) -> None:
    """The app has not exited (an unhandled exception exits it with code 1)."""
    assert app.return_code is None and app.is_running, (
        f"the app exited (return code {app.return_code}); screen stack: {app.screen_stack}"
    )
