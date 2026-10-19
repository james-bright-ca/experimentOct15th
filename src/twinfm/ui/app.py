"""TwinFM: the two-panel app, its keys and the file operations.

Keys, in Midnight Commander tradition: F5 copy, F6 move, Ctrl-N rename in
place, F7 make directory, F8 delete, F9 connect the panel to another backend
or path, F10 quit; Enter opens a directory, Backspace goes to the parent, Tab
switches panel, Space / Insert / Ctrl-T select, Ctrl-R reloads. Shift-F6
renames too where the terminal sends it as ESC[17;2~ (xterm, iTerm2); macOS
Terminal.app's Shift-F6 cannot be read, so the footer shows Ctrl-N.
Terminals that send F-keys as media keys can use Esc then the digit (Esc 5
is F5 ... Esc 0 is F10), typed slowly or fast, or Option with the digit on a
US layout (the character it types, such as ∞ for Option-5, is read as that
digit).

F4 (or Esc then 4, or Option-4) edits the file on the cursor row of a local
panel in $VISUAL, else $EDITOR, else vim, with the app suspended until the
editor exits. Ctrl-R reloads past any cached listing: a backend may cache
listings (btty does) and keeps them current only for twinfm's own changes.
F5 and F6 of a folder or several items between a local panel and another
store that packs archives (Cap.ARCHIVE) offer an Archive checkbox, checked:
the batch then travels as one archive (see ``Engine.copy``).

Threading: the UI thread never calls a backend or the engine. Listings,
connects and operations run in Textual thread workers. When the engine needs
an answer, its callback pushes a dialog through ``call_from_thread`` and
waits for it on the worker thread. The editor (F4) blocks the UI thread
while it runs; workers carry on, and their questions wait until it exits.
"""

from __future__ import annotations

import importlib
import os
import posixpath
import shlex
import subprocess
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from functools import partial
from typing import Any, cast

from textual import events
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer

from twinfm import backend as registry
from twinfm.backend.base import Backend, Cap, Entry, FsError
from twinfm.backend.local import LocalBackend
from twinfm.config import Settings
from twinfm.ops.model import Callbacks, Conflict, Engine, OnError, Progress, Report
from twinfm.ui.dialogs import (
    ConfirmDialog,
    ConflictDialog,
    Dialog,
    ErrorDialog,
    InputDialog,
    ProgressDialog,
    SummaryDialog,
    TransferDialog,
)
from twinfm.ui.panel import Panel

__all__ = ["FileManagerScreen", "TwinFM"]

Location = tuple[Backend, str]
OpenBackend = Callable[[str, Settings], Backend]
RunEditor = Callable[[list[str]], int]

# Esc then a digit stands for the F-key of that number (0 is F10).
_ESC_DIGITS = {
    "4": "edit",
    "5": "copy",
    "6": "move",
    "7": "mkdir",
    "8": "delete",
    "9": "connect",
    "0": "quit",
}

# Textual 8.2.8 reads a fast Esc+digit as the character macOS Option+digit
# types on a US layout; each stands for its digit.
_OPTION_DIGITS = {"¢": "4", "∞": "5", "§": "6", "¶": "7", "•": "8", "ª": "9", "º": "0"}


def _normalised(path: str) -> str:
    # normpath keeps a POSIX leading "//"; backend paths start with exactly one "/".
    return "/" + posixpath.normpath(path).lstrip("/")


def _editor_argv(path: str) -> list[str]:
    """$VISUAL, else $EDITOR, else vim, split as a shell would, then ``path``;
    vim when the chosen value is empty or does not parse."""
    value = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vim"
    try:
        command = shlex.split(value)
    except ValueError:  # an unclosed quote
        command = []
    return [*(command or ["vim"]), path]


def _archive_applies(src: Backend, dst: Backend, items: Sequence[Entry]) -> bool:
    """Whether the engine could move ``items`` from ``src`` to ``dst`` as one
    archive (see ``Engine.copy``), so the Archive checkbox is worth showing:
    both stores have Cap.ARCHIVE, exactly one is local, and there is a
    directory or several items."""
    return (
        Cap.ARCHIVE in src.caps
        and Cap.ARCHIVE in dst.caps
        and isinstance(src, LocalBackend) != isinstance(dst, LocalBackend)
        and (len(items) > 1 or items[0].kind == "dir")
    )


class FileManagerScreen(Screen[None]):
    """The two panels and the key bar. Tracks which panel is active."""

    DEFAULT_CSS = """
    FileManagerScreen > Horizontal {
        height: 1fr;
    }
    FileManagerScreen Footer FooterKey:last-of-type {
        margin-right: 0;
    }
    """

    BINDINGS = [
        Binding("f4", "app.edit", "Edit", key_display="F4"),
        Binding("f5", "app.copy", "Copy", key_display="F5"),
        Binding("f6", "app.move", "Move", key_display="F6"),
        # The footer shows one key per action: Ctrl-N, which every terminal sends.
        Binding("shift+f6", "app.rename", "Rename", show=False),
        Binding("ctrl+n", "app.rename", "Rename", key_display="^N"),
        Binding("f7", "app.mkdir", "Mkdir", key_display="F7"),
        Binding("f8", "app.delete", "Del", key_display="F8"),
        Binding("f9", "app.connect", "Connect", key_display="F9"),
        Binding("f10", "app.quit", "Quit", key_display="F10"),
        *(Binding(f"alt+{digit}", f"app.{action}", show=False) for digit, action in _ESC_DIGITS.items()),
    ]

    def __init__(self, left: Panel, right: Panel) -> None:
        super().__init__()
        self.left_panel = left
        self.right_panel = right
        self.active_panel = left
        self._esc_pending = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield self.left_panel
            yield self.right_panel
        # Compact, without the palette key (Ctrl-P still opens it), so that
        # every key fits Terminal.app's default 80 columns.
        yield Footer(compact=True, show_command_palette=False)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        for node in event.widget.ancestors_with_self:
            if isinstance(node, Panel):
                self.active_panel = node
                return

    async def on_key(self, event: events.Key) -> None:
        # Keys reach this screen after the focused widget and before the
        # app's bindings; dialogs are other screens, so their Esc never lands here.
        if event.key == "escape":
            self._esc_pending = True
            event.stop()
            return
        if self._esc_pending:
            self._esc_pending = False
            digit = event.key
        else:
            digit = _OPTION_DIGITS.get(event.character or "", "")
        action = _ESC_DIGITS.get(digit)
        if action is not None:
            event.stop()
            await self.app.run_action(action)


class TwinFM(App[None]):
    """Two panels, each a location ``(backend, path)``.

    ``engine`` defaults to the module ``twinfm.ops.engine``, imported on first
    use; ``open_backend`` (used by Connect) defaults to the backend registry.
    ``run_editor(argv)`` (used by F4) runs the editor command ``argv`` and
    returns its exit status; the default suspends the app and runs it in the
    terminal.
    """

    TITLE = "twinfm"

    def __init__(
        self,
        left: Location,
        right: Location,
        settings: Settings,
        engine: Engine | None = None,
        open_backend: OpenBackend | None = None,
        run_editor: RunEditor | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self._locations = (left, right)
        self._engine = engine
        self._open_backend = open_backend or registry.open_backend
        self._run_editor = run_editor or self._edit_suspended
        self._stopping = threading.Event()
        self._main: FileManagerScreen | None = None

    def get_default_screen(self) -> Screen[Any]:
        (left_backend, left_path), (right_backend, right_path) = self._locations
        self._main = FileManagerScreen(
            Panel(left_backend, left_path, id="left"), Panel(right_backend, right_path, id="right")
        )
        return self._main

    def on_unmount(self) -> None:
        self._stopping.set()

    # -- state -------------------------------------------------------------------

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = cast(Engine, importlib.import_module("twinfm.ops.engine"))
        return self._engine

    @property
    def main(self) -> FileManagerScreen:
        assert self._main is not None, "the app has not been composed yet"
        return self._main

    @property
    def left_panel(self) -> Panel:
        return self.main.left_panel

    @property
    def right_panel(self) -> Panel:
        return self.main.right_panel

    @property
    def active_panel(self) -> Panel:
        return self.main.active_panel

    @property
    def other_panel(self) -> Panel:
        main = self.main
        return main.right_panel if main.active_panel is main.left_panel else main.left_panel

    # -- thread bridge -------------------------------------------------------------

    def _call_ui(self, callback: Callable[..., object], *args: Any, **kwargs: Any) -> bool:
        """From a worker: run ``callback`` on the UI thread and wait for it.
        False, without running it, once the app is closing."""
        if self._stopping.is_set():
            return False
        try:
            self.call_from_thread(callback, *args, **kwargs)
        except RuntimeError:  # the app stopped meanwhile
            return False
        return True

    # -- actions -------------------------------------------------------------------

    def _not_available(self, what: str) -> None:
        self.notify(f"{what} is not available in transfer-only mode", severity="warning")

    def action_edit(self) -> None:
        """F4: edit the file on the cursor row of a local panel, then list the
        panel again with the cursor on it."""
        panel = self.active_panel
        if not isinstance(panel.backend, LocalBackend):
            self.notify(
                "F4 edits local files only; copy the file to a local panel to edit it",
                severity="warning",
            )
            return
        entry = panel.cursor_entry
        if entry is None:
            return
        if entry.kind != "file" and not (entry.kind == "symlink" and entry.target_kind == "file"):
            self.notify(f'"{entry.name}" is not a file', title="Edit", severity="warning", markup=False)
            return
        try:
            status = self._run_editor(_editor_argv(entry.path))
        except SuspendNotSupported:
            self.notify("This terminal cannot be handed over to an editor", title="Edit")
        except OSError as e:
            self.notify(str(e), title="Cannot start the editor", severity="error", markup=False)
        else:
            if status != 0:
                self.notify(f"The editor exited with status {status}", title="Edit", severity="warning")
        self._refresh([panel.backend], panel, entry.name)

    def _edit_suspended(self, argv: list[str]) -> int:
        """The default ``run_editor``: give the terminal to ``argv`` until it exits."""
        failure: OSError | None = None
        status = 0
        with self.suspend():
            try:
                status = subprocess.run(argv, check=False).returncode
            except OSError as e:
                # An exception leaving the block would skip Textual's resume of
                # the terminal, so it is raised only once the app has it back.
                failure = e
        if failure is not None:
            raise failure
        return status

    def action_copy(self) -> None:
        self._transfer("copy")

    def action_move(self) -> None:
        self._transfer("move")

    def _transfer(self, op: str) -> None:
        src, dst = self.active_panel, self.other_panel
        verb = op.capitalize()
        if not src.listable:
            if op == "copy":
                self._download(src, dst)
            else:
                self._not_available(verb)
            return
        items = src.targets()
        if not items:
            return
        what = f'"{items[0].name}"' if len(items) == 1 else f"{len(items)} items"
        paths = [e.path for e in items]
        src_backend, dst_backend = src.backend, dst.backend
        # Relative to the source directory on one store (as MC does), else to the destination's.
        base = src.path if dst_backend is src_backend else dst.path

        def chosen(text: str | None, archive: bool = False) -> None:
            if not text or not text.strip():
                return
            target = _normalised(text if text.startswith("/") else dst_backend.join(base, text))
            self._confirm_replace(
                dst_backend,
                partial(self._run_transfer, op, src_backend, paths, dst_backend, target, src, archive),
            )

        def answered(result: tuple[str, bool] | None) -> None:
            if result is not None:
                chosen(*result)

        prompt = f"{verb} {what} to:"
        if _archive_applies(src_backend, dst_backend, items):
            self.push_screen(TransferDialog(verb, prompt, dst.path), answered)
        else:
            self.push_screen(InputDialog(verb, prompt, dst.path), chosen)

    def _download(self, src: Panel, dst: Panel) -> None:
        """F5 in a transfer-only panel: fetch one file by its typed path."""
        src_backend, dst_backend, target = src.backend, dst.backend, dst.path

        def chosen(text: str | None) -> None:
            if not text or not text.strip():
                return
            path = _normalised(text if text.startswith("/") else src_backend.join(src.path, text))
            self._confirm_replace(
                dst_backend,
                partial(self._run_transfer, "copy", src_backend, [path], dst_backend, target, None),
            )

        prompt = f"Path of the file on {src_backend.label} to download into {dst_backend.label}:{target}"
        prefill = src.path.rstrip("/") + "/"
        self.push_screen(InputDialog("Download", prompt, prefill, select=False), chosen)

    def _confirm_replace(self, dst: Backend, then: Callable[[], None]) -> None:
        """Without STAT the engine cannot detect conflicts: say so first."""
        if Cap.STAT in dst.caps:
            then()
            return

        def confirmed(ok: bool | None) -> None:
            if ok:
                then()

        message = (
            f"{dst.label} cannot check which files already exist: "
            "files with the same name will be replaced."
        )
        self.push_screen(
            ConfirmDialog("Replace files?", message, yes="Replace", no="Cancel"), confirmed
        )

    def _run_transfer(
        self,
        op: str,
        src: Backend,
        paths: list[str],
        dst: Backend,
        target: str,
        source: Panel | None,
        archive: bool = False,
    ) -> None:
        def call(cb: Callbacks) -> Report:
            method = self.engine.copy if op == "copy" else self.engine.move
            return method(src, paths, dst, target, cb, archive=archive)

        changed = [dst] if op == "copy" else [dst, src]
        self._run_operation(op.capitalize(), call, changed, source)

    def action_rename(self) -> None:
        panel = self.active_panel
        if not panel.listable:
            self._not_available("Rename")
            return
        entry = panel.cursor_entry
        if entry is None:
            return
        backend = panel.backend

        def chosen(name: str | None) -> None:
            if not name or name == entry.name:
                return
            self._run_operation(
                "Rename",
                lambda cb: self.engine.rename(backend, entry.path, name, cb),
                [backend],
                panel,
                cursor=name,
            )

        self.push_screen(InputDialog("Rename", f'Rename "{entry.name}" to:', entry.name), chosen)

    def action_mkdir(self) -> None:
        panel = self.active_panel
        if not panel.listable:
            self._not_available("Make directory")
            return
        backend, parent = panel.backend, panel.path

        def chosen(name: str | None) -> None:
            if not name:
                return
            self.run_worker(
                partial(self._mkdir, panel, backend, parent, name),
                thread=True,
                group="operation",
                name="mkdir",
            )

        self.push_screen(InputDialog("Make directory", "Name of the new directory:"), chosen)

    def _mkdir(self, panel: Panel, backend: Backend, parent: str, name: str) -> None:
        try:
            path = self.engine.mkdir(backend, parent, name)
        except FsError as e:
            self._call_ui(
                self.notify, str(e), title="Cannot make directory", severity="error", markup=False
            )
            return
        self._call_ui(self._refresh, [backend], panel, posixpath.basename(path))

    def action_delete(self) -> None:
        panel = self.active_panel
        if not panel.listable:
            self._not_available("Delete")
            return
        items = panel.targets()
        if not items:
            return
        count = len(items)
        dirs = sum(1 for e in items if e.kind == "dir")
        backend = panel.backend
        paths = [e.path for e in items]
        message = f"Delete {count} {'item' if count == 1 else 'items'}, including {dirs} directories?"
        if count == 1:
            message += f'\n"{items[0].name}"'
        message += "\nDirectories are deleted recursively. This cannot be undone."

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._run_operation(
                    "Delete", lambda cb: self.engine.delete(backend, paths, cb), [backend], panel
                )

        self.push_screen(ConfirmDialog("Delete", message), confirmed)

    def action_connect(self) -> None:
        panel = self.active_panel
        kinds = registry.available()

        def chosen(text: str | None) -> None:
            if not text or not text.strip():
                return
            self.run_worker(
                partial(self._connect, panel, text, kinds), thread=True, group="connect", name="connect"
            )

        prompt = (
            f"Location as KIND:PATH (kinds: {', '.join(kinds)}). "
            f"A path without KIND: stays on {panel.backend.label}; an empty PATH is the home directory."
        )
        self.push_screen(
            InputDialog("Connect", prompt, f"{panel.backend.id}:{panel.path}"), chosen
        )

    def _connect(self, panel: Panel, text: str, kinds: list[str]) -> None:
        backend, base = panel.backend, panel.path
        kind, sep, rest = text.partition(":")
        if sep and kind == backend.id:
            text = rest
        elif sep and kind in kinds:
            try:
                backend = self._open_backend(kind, self.settings)
            except (LookupError, FsError) as e:
                self._call_ui(self.notify, str(e), title="Cannot connect", severity="error", markup=False)
                return
            if backend is not panel.backend:
                base = backend.home()
            text = rest
        if not text:
            path = backend.home()
        else:
            path = _normalised(text if text.startswith("/") else backend.join(base, text))
        self._call_ui(panel.load, path, None, backend)

    # -- running an operation ------------------------------------------------------

    def _run_operation(
        self,
        title: str,
        call: Callable[[Callbacks], Report],
        changed: Sequence[Backend],
        source: Panel | None,
        cursor: str | None = None,
    ) -> None:
        """Run ``call`` in a worker behind a progress dialog, then report
        failures, drop what was done from ``source``'s selection and reload the
        panels showing a ``changed`` backend (``source`` with the cursor on
        ``cursor``)."""
        cancel = threading.Event()
        dialog = ProgressDialog(title, cancel.set)
        self.push_screen(dialog)
        bridge = _Bridge(self, dialog, cancel)
        self.run_worker(
            partial(self._operate, title, call, bridge, dialog, changed, source, cursor),
            thread=True,
            group="operation",
            name=title,
        )

    def _operate(
        self,
        title: str,
        call: Callable[[Callbacks], Report],
        bridge: _Bridge,
        dialog: ProgressDialog,
        changed: Sequence[Backend],
        source: Panel | None,
        cursor: str | None,
    ) -> None:
        report: Report | None = None
        error: FsError | None = None
        try:
            report = call(bridge)
        except FsError as e:
            error = e
        self._call_ui(self._operation_done, title, dialog, report, error, changed, source, cursor)

    def _operation_done(
        self,
        title: str,
        dialog: ProgressDialog,
        report: Report | None,
        error: FsError | None,
        changed: Sequence[Backend],
        source: Panel | None,
        cursor: str | None,
    ) -> None:
        if self.screen is dialog:
            dialog.dismiss()
        if error is not None:
            self.notify(str(error), title=f"{title} failed", severity="error", markup=False)
        elif report is not None:
            if source is not None:
                source.unselect(report.done)
            if report.failed:
                self.push_screen(SummaryDialog(f"{title}: some items failed", report))
            elif report.cancelled:
                self.notify(f"{title} cancelled", severity="warning")
            elif report.skipped:
                self.notify(f"{title}: {len(report.skipped)} skipped", markup=False)
        self._refresh(changed, source, cursor)

    def _refresh(self, changed: Sequence[Backend], panel: Panel | None, cursor: str | None) -> None:
        for each in (self.left_panel, self.right_panel):
            if any(each.backend is backend for backend in changed):
                each.reload(cursor if each is panel else None)


class _Bridge:
    """The engine's Callbacks for one operation. Questions become dialogs on
    the UI thread; the worker thread waits for the answer. Once the app is
    closing every question gets its stop answer and cancelled() is True."""

    def __init__(self, app: TwinFM, dialog: ProgressDialog, cancel: threading.Event) -> None:
        self._app = app
        self._dialog = dialog
        self._cancel = cancel

    def ask_conflict(self, src: Entry, dst: Entry) -> Conflict:
        return self._ask(lambda: ConflictDialog(src, dst), Conflict.CANCEL)

    def ask_error(self, path: str, err: FsError) -> OnError:
        return self._ask(lambda: ErrorDialog(path, err), OnError.ABORT)

    def progress(self, p: Progress) -> None:
        self._app._call_ui(self._dialog.update_progress, p)

    def cancelled(self) -> bool:
        return self._cancel.is_set() or self._app._stopping.is_set()

    def _ask[T](self, make: Callable[[], Dialog[T]], stop: T) -> T:
        answer: Future[T | None] = Future()

        def resolve(result: T | None) -> None:
            if not answer.done():
                answer.set_result(result)

        def show() -> None:
            self._app.push_screen(make(), resolve)

        if not self._app._call_ui(show):
            return stop
        while True:
            try:
                result = answer.result(timeout=0.1)
            except TimeoutError:
                if self._app._stopping.is_set():
                    return stop
                continue
            return stop if result is None else result
