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

Typing any other character (not Space) opens the active panel's find line:
the cursor goes to the first name that matches, Up / Down step through the
matches, Enter closes the line and opens a matching directory, and Esc or
any other key closes it. Text with a ``/``, ``~`` alone or ``KIND:`` is a
path: Enter goes there.
A modifier key pressed on its own (Shift, Control, Option, Command, Caps
Lock) does not close the line; it waits for the key the modifier goes with.

F3 (or Esc then 3, or Option-3) views the file on the cursor row of a local
panel with more; the footer has no room for it in 80 columns. F4 (or
Esc then 4, or Option-4) edits that file in $VISUAL, else $EDITOR, else vim.
The app is suspended until the viewer or the editor exits. Ctrl-R reloads
past any cached listing: a backend may cache listings (btty does) and keeps
them current only for twinfm's own changes. F5 and F6 of a folder or several
items between a local panel and another store that packs archives
(Cap.ARCHIVE) offer an Archive checkbox, checked: the batch then travels as
one archive (see ``Engine.copy``).

Ctrl-O hides the panels and shows a local shell in the terminal, as Midnight
Commander does; Ctrl-O again brings the panels back. The shell stays alive
in the background between visits. It is always a local shell, never a btty
one, whatever the panels show.

While the active panel is loading a listing (its status line reads
``Loading…``), only Tab, Shift+Tab and quit (F10, Esc then 0, Option-0,
Ctrl-Q) work: every other key, a paste and a click on its rows are ignored.
The other panel, once listed, works as usual.

Threading: the UI thread never calls a backend or the engine. Listings,
connects and operations run in Textual thread workers. When the engine needs
an answer, its callback pushes a dialog through ``call_from_thread`` and
waits for it on the worker thread. The viewer (F3), the editor (F4) and the
shell (Ctrl-O) block the UI thread while they have the terminal; workers
carry on, and their questions wait until the panels are back.
"""

from __future__ import annotations

import importlib
import os
import posixpath
import shlex
import subprocess
import sys
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
from twinfm.ui.find import first_match, is_path, location_kind, resolve
from twinfm.ui.panel import Panel
from twinfm.ui.shell import LocalShell, ShellVisit

__all__ = ["FileManagerScreen", "TwinFM"]

Location = tuple[Backend, str]
OpenBackend = Callable[[str, Settings], Backend]
RunEditor = Callable[[list[str]], int]
RunShell = Callable[[str, str | None], ShellVisit]

# Esc then a digit stands for the F-key of that number (0 is F10).
_ESC_DIGITS = {
    "3": "view",
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
_OPTION_DIGITS = {"£": "3", "¢": "4", "∞": "5", "§": "6", "¶": "7", "•": "8", "ª": "9", "º": "0"}

# The keys that still work while the active panel is loading, besides Esc
# then 0 and Option-0 (Ctrl-Q, a priority binding, never reaches the screen).
_WHILE_LOADING = frozenset({"tab", "shift+tab", "f10", "alt+0"})

# Textual 8.2.8 asks the terminal for kitty keyboard flags 1 | 8 | 16, so a
# modifier pressed on its own arrives as a key of its own, with no character:
# the kitty protocol's modifier keys, including the Caps Lock and Num Lock keys.
_MODIFIER_KEYS = frozenset(
    {f"{side}_{name}" for side in ("left", "right") for name in ("shift", "control", "alt", "super", "hyper")}
    | {"left_meta", "right_meta", "iso_level3_shift", "iso_level5_shift", "caps_lock", "num_lock"}
)

# A kitty terminal that leaves out the text field sends an unmodified key as
# ESC[<code>u. Textual 8.2.8 names a punctuation key after its character
# (slash, full_stop, grave_accent, ...) and gives it no character: the
# character each such name types. Space keeps its own handling.
_PUNCTUATION_KEYS = {
    "exclamation_mark": "!", "quotation_mark": '"', "number_sign": "#", "dollar_sign": "$",
    "percent_sign": "%", "ampersand": "&", "apostrophe": "'", "left_parenthesis": "(",
    "right_parenthesis": ")", "asterisk": "*", "plus": "+", "comma": ",", "minus": "-",
    "full_stop": ".", "slash": "/", "colon": ":", "semicolon": ";", "less_than_sign": "<",
    "equals_sign": "=", "greater_than_sign": ">", "question_mark": "?", "at": "@",
    "left_square_bracket": "[", "backslash": "\\", "right_square_bracket": "]",
    "circumflex_accent": "^", "underscore": "_", "grave_accent": "`",
    "left_curly_bracket": "{", "vertical_line": "|", "right_curly_bracket": "}", "tilde": "~",
}


def _normalised(path: str) -> str:
    # normpath keeps a POSIX leading "//"; backend paths start with exactly one "/".
    return "/" + posixpath.normpath(path).lstrip("/")


def _typed(event: events.Key) -> str | None:
    """The character ``event`` types into the find line: a printable one,
    with no modifier but Shift (a fast Esc+letter arrives as ``alt+``)."""
    # A kitty terminal that leaves out the text field sends Shift+U as
    # ESC[117;2u, which Textual reads as shift+u with no character.
    modifiers, _, name = event.key.rpartition("+")
    if event.character is None and modifiers == "shift" and len(name) == 1 and "a" <= name <= "z":
        return name.upper()
    # ESC[47u, an unmodified "/" without the text field, is slash with no character.
    if event.character is None and event.key in _PUNCTUATION_KEYS:
        return _PUNCTUATION_KEYS[event.key]
    if not event.is_printable or not event.character:
        return None
    if any(modifier != "shift" for modifier in event.key.split("+")[:-1]):
        return None
    return event.character


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
        # Not in the footer, which has no room for it at 80 columns.
        Binding("f3", "app.view", "View", key_display="F3", show=False),
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
        Binding("ctrl+o", "app.shell", "Shell", show=False),
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
                if node is not self.active_panel:
                    self.active_panel.find_close()
                self.active_panel = node
                return

    def on_screen_suspend(self, event: events.ScreenSuspend) -> None:
        # A dialog opened over this screen.
        self.left_panel.find_close()
        self.right_panel.find_close()

    def on_paste(self, event: events.Paste) -> None:
        panel = self.active_panel
        if panel.is_loading:
            event.stop()  # dropped, as a typed key is
            return
        if panel.listable:
            event.stop()
            panel.find_type(event.text.splitlines()[0] if event.text else "")

    async def on_key(self, event: events.Key) -> None:
        # Keys reach this screen after the focused widget and before the
        # app's bindings; dialogs are other screens, so their Esc never lands here.
        panel = self.active_panel
        if panel.is_loading:
            # Only Tab and quit go on; Esc only arms its prefix, for Esc then 0.
            if event.key == "escape":
                self._esc_pending = True
                event.stop()
                return
            digit = event.key if self._esc_pending else _OPTION_DIGITS.get(event.character or "", "")
            if event.key not in _WHILE_LOADING and _ESC_DIGITS.get(digit) != "quit":
                self._esc_pending = False
                event.stop()
                return
        if event.key == "escape":
            panel.find_close()
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
            panel.find_close()
            await self.app.run_action(action)
            return
        if panel.finding:
            self._find_key(panel, event)
            return
        char = _typed(event)
        if char is not None and char != " " and panel.listable:
            event.stop()
            panel.find_type(char)

    def _find_key(self, panel: Panel, event: events.Key) -> None:
        """A key while ``panel``'s find line is open. A key the line does not
        use closes it and goes on to do its own job on the cursor row."""
        # A modifier pressed on its own is not such a key: the line stays open.
        char = _typed(event)
        key = event.key
        if char is not None:
            panel.find_type(char)
        elif key == "backspace":
            panel.find_backspace()
        elif key == "ctrl+u":
            panel.find_clear()
        elif key in ("up", "down"):
            panel.find_step(1 if key == "down" else -1)
        elif key == "enter":
            text = panel.find_text
            if is_path(text, registry.available()):
                cast("TwinFM", self.app).go_to_typed(panel, text)
            else:
                panel.find_close()
                if first_match(text, panel.names) is not None:
                    return  # the listing's Enter opens the match
        elif key in _MODIFIER_KEYS:
            return  # a modifier on its own: the line waits for the key it modifies
        else:
            panel.find_close()
            return
        event.stop()


class TwinFM(App[None]):
    """Two panels, each a location ``(backend, path)``.

    ``engine`` defaults to the module ``twinfm.ops.engine``, imported on first
    use; ``open_backend`` (used by Connect) defaults to the backend registry.
    ``run_editor(argv)`` (used by F4) runs the editor command ``argv`` and
    returns its exit status; the default suspends the app and runs it in the
    terminal. ``run_viewer(argv)`` (used by F3) does the same for the viewer.
    ``run_shell(start_dir, panel_dir)`` (used by Ctrl-O) shows the local shell
    and returns how the visit ended; the default suspends the app and hands
    the terminal to ``shell`` (default: a ``LocalShell``, which starts nothing
    until the first Ctrl-O). Quit asks first while a command runs in it.
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
        run_viewer: RunEditor | None = None,
        run_shell: RunShell | None = None,
        shell: LocalShell | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self._locations = (left, right)
        self._engine = engine
        self._open_backend = open_backend or registry.open_backend
        self._run_editor = run_editor or self._run_suspended
        self._run_viewer = run_viewer or self._run_suspended
        self._run_shell = run_shell or self._visit_shell
        self._shell = shell if shell is not None else LocalShell()
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
        self._shell.close()

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

    def action_view(self) -> None:
        """F3: view the file on the cursor row of a local panel with more,
        then list the panel again with the cursor on it."""
        panel = self.active_panel
        if not isinstance(panel.backend, LocalBackend):
            self.notify(
                "F3 views local files only; copy the file to a local panel to view it",
                severity="warning",
            )
            return
        entry = panel.cursor_entry
        if entry is None:
            return
        if entry.kind != "file" and not (entry.kind == "symlink" and entry.target_kind == "file"):
            self.notify(f'"{entry.name}" is not a file', title="View", severity="warning", markup=False)
            return
        try:
            status = self._run_viewer(["more", entry.path])
        except SuspendNotSupported:
            self.notify("This terminal cannot be handed over to a viewer", title="View")
        except OSError as e:
            self.notify(str(e), title="Cannot start more", severity="error", markup=False)
        else:
            if status != 0:
                self.notify(f"more exited with status {status}", title="View", severity="warning")
        self._refresh([panel.backend], panel, entry.name)

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

    def _run_suspended(self, argv: list[str]) -> int:
        """The default ``run_editor`` and ``run_viewer``: give the terminal to
        ``argv`` until it exits."""
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

    def action_shell(self) -> None:
        """Ctrl-O: hide the panels and show the local shell (never a btty one)
        until Ctrl-O again. It starts in the active panel's directory when that
        panel is local, else in the other panel's if that one is, else in
        twinfm's own; a local active panel then follows the shell's cd."""
        panel, other = self.active_panel, self.other_panel
        local = isinstance(panel.backend, LocalBackend)
        try:
            if local:
                start_dir = panel.path
            elif isinstance(other.backend, LocalBackend):
                start_dir = other.path
            else:
                start_dir = os.getcwd()
            visit = self._run_shell(start_dir, panel.path if local else None)
        except SuspendNotSupported:
            self.notify("This terminal cannot be handed over to a shell", title="Shell")
            return
        except OSError as e:
            self.notify(str(e), title="Cannot start the shell", severity="error", markup=False)
            return
        if visit.exited is not None:
            self.notify(f"The shell exited (status {visit.exited}); Ctrl-O starts a new one", title="Shell")
        elif (
            visit.moved
            and visit.cwd is not None
            and local
            and os.path.realpath(visit.cwd) != os.path.realpath(panel.path)
        ):
            panel.load(visit.cwd)

    def _visit_shell(self, start_dir: str, panel_dir: str | None) -> ShellVisit:
        """The default ``run_shell``: give the terminal to the local shell
        until Ctrl-O or the shell's exit."""
        failure: OSError | None = None
        visit = ShellVisit(exited=None, cwd=None, moved=False)
        with self.suspend():
            try:
                visit = self._shell.visit(
                    start_dir, panel_dir, sys.__stdin__.fileno(), sys.__stderr__.fileno()
                )
            except OSError as e:
                # Raised once the app has the terminal back, as in _run_suspended.
                failure = e
        if failure is not None:
            raise failure
        return visit

    async def action_quit(self) -> None:
        """F10, Esc then 0, Option-0 and Ctrl-Q: quit, asking first while a
        command is still running in the shell."""
        if not self._shell.running:
            self.exit()
            return

        def answered(ok: bool | None) -> None:
            if ok:
                self.exit()

        self.push_screen(
            ConfirmDialog(
                "Quit", "A command is still running in the shell. Quit anyway?", yes="Quit", no="Cancel"
            ),
            answered,
        )

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

    def go_to_typed(self, panel: Panel, text: str) -> None:
        """Enter on the path ``text`` in ``panel``'s find line: ``KIND:PATH``
        connects as F9 does; any other path resolves against the panel's
        directory, or its backend's home for ``~``."""
        kinds = registry.available()
        if location_kind(text, kinds) is not None:
            self.run_worker(
                partial(self._connect, panel, text, kinds, go_to=True),
                thread=True,
                group="connect",
                name="connect",
            )
        else:
            panel.go_to(resolve(text, panel.path, panel.backend.home()))

    def _connect(self, panel: Panel, text: str, kinds: list[str], go_to: bool = False) -> None:
        """Open the location ``text`` (``KIND:PATH`` or a path) in ``panel``,
        through ``Panel.go_to`` when ``go_to`` (the find line), else as F9 does."""
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
        if go_to:
            self._call_ui(panel.go_to, path, backend)
        else:
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
