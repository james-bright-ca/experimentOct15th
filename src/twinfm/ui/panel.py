"""One file panel: a header with the backend label and path, the listing,
the selection and a status line.

The listing puts ``..`` first (except at ``/``), then directories (and
symlinks to directories), then everything else, each group by name without
regard to case; dotfiles are shown. A backend without Cap.LIST gets a
transfer-only view: a message instead of a listing.

Listing runs in a Textual thread worker; the result comes back to the UI
thread through ``call_from_thread``.
"""

from __future__ import annotations

import posixpath
import time
from collections.abc import Iterable
from functools import partial

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static
from textual.worker import get_current_worker

from twinfm.backend.base import Backend, Cap, Entry, FsError

__all__ = ["PARENT", "Panel", "format_size", "is_dir", "sort_entries"]

PARENT = ".."


def is_dir(entry: Entry) -> bool:
    """Whether ``entry`` opens as a directory: a directory or a symlink to one."""
    return entry.kind == "dir" or (entry.kind == "symlink" and entry.target_kind == "dir")


def sort_entries(entries: Iterable[Entry]) -> list[Entry]:
    """Directories first, then the rest, each group by case-insensitive name."""
    return sorted(entries, key=lambda e: (not is_dir(e), e.name.casefold(), e.name))


def format_size(size: int) -> str:
    """A compact size: bytes below 1 KiB, else K, M, G or T with one decimal."""
    value = float(size)
    for unit in ("", "K", "M", "G"):
        if value < 1024:
            return f"{size}" if not unit else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}T"


def _size_cell(entry: Entry) -> str:
    if entry.kind == "dir":
        return "<DIR>"
    if entry.kind == "symlink":
        return "<LINK>"
    return format_size(entry.size) if entry.size is not None else ""


def _mtime_cell(entry: Entry) -> str:
    if entry.mtime is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))


class LocationHeader(Static):
    """``label:path``, cut from the left when too long, so that the current
    directory's own name stays visible."""

    location = ""

    def show(self, location: str) -> None:
        self.location = location
        self.refresh()

    def render(self) -> Text:
        width = self.content_size.width
        text = self.location
        if 1 < width < len(text):
            text = "…" + text[len(text) - width + 1 :]
        return Text(text)


class PanelTable(DataTable[Text]):
    """The listing. Enter (or a click on the cursor row) opens the row."""


class TransferOnlyView(Static, can_focus=True):
    """Stands in for the listing when the backend cannot list directories."""


class Panel(Vertical):
    """A directory on a backend, its listing and its selection.

    ``backend`` and ``path`` change only once a listing of the new location
    succeeded; a failed listing shows an error and the panel stays where it
    was. ``selected`` holds names in the current directory.
    """

    DEFAULT_CSS = """
    Panel {
        width: 1fr;
        border: round $panel;
    }
    Panel:focus-within {
        border: round $accent;
    }
    Panel > #header {
        height: 1;
        text-style: bold;
    }
    Panel > PanelTable {
        height: 1fr;
    }
    Panel > TransferOnlyView {
        height: 1fr;
        padding: 1 1;
    }
    Panel > #status {
        height: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("space,insert,ctrl+t", "toggle_select", "Select", show=False),
        Binding("backspace", "parent", "Parent directory", show=False),
        Binding("ctrl+r", "reload", "Reload", show=False),
    ]

    def __init__(self, backend: Backend, path: str, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.path = path
        self.entries: list[Entry] = []
        self.selected: set[str] = set()
        self._rows: list[str] = []
        self._header = LocationHeader(id="header")
        self._table = PanelTable(cursor_type="row", id="table")
        self._message = TransferOnlyView(markup=False, id="message")
        self._status = Static(markup=False, id="status")

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._table
        yield self._message
        yield self._status

    def on_mount(self) -> None:
        self._table.add_column("Name", key="name")
        self._table.add_column("Size", key="size", width=8)
        self._table.add_column("Modified", key="mtime", width=16)
        self._render_listing(None)
        self.load(self.path)

    # -- state -----------------------------------------------------------------

    @property
    def listable(self) -> bool:
        """False for a transfer-only backend (no Cap.LIST)."""
        return Cap.LIST in self.backend.caps

    @property
    def names(self) -> list[str]:
        """The listed names in display order, ``..`` included."""
        return list(self._rows)

    @property
    def cursor_name(self) -> str | None:
        """The name on the cursor row, or None when nothing is listed."""
        if not self._rows:
            return None
        return self._rows[min(self._table.cursor_row, len(self._rows) - 1)]

    @property
    def cursor_entry(self) -> Entry | None:
        """The entry on the cursor row; None for ``..`` or an empty listing."""
        return self._entry(self.cursor_name)

    @property
    def status_text(self) -> str:
        """What the status line says."""
        if not self.listable:
            return "transfer-only"
        if self.selected:
            total = sum(
                e.size for e in self.entries
                if e.name in self.selected and e.kind != "dir" and e.size is not None
            )
            return f"{len(self.selected)} selected, {total:,} bytes"
        return f"{len(self.entries)} items"

    def targets(self) -> list[Entry]:
        """What an operation acts on: the selected entries in display order,
        else the entry on the cursor row. Never ``..``."""
        if self.selected:
            return [e for e in self.entries if e.name in self.selected]
        entry = self.cursor_entry
        return [entry] if entry is not None else []

    def _entry(self, name: str | None) -> Entry | None:
        if name is None or name == PARENT:
            return None
        return next((e for e in self.entries if e.name == name), None)

    # -- loading ---------------------------------------------------------------

    def load(self, path: str, cursor: str | None = None, backend: Backend | None = None) -> None:
        """List ``path`` on ``backend`` (default: the current one) in a worker
        and show it, with the cursor on ``cursor`` if that name is listed."""
        self._status.update("Loading...")
        self.run_worker(
            partial(self._list, self.app, backend or self.backend, path, cursor),
            thread=True,
            group="list",
            exclusive=True,
            name=f"list {path}",
        )

    def reload(self, cursor: str | None = None) -> None:
        """List the current directory again, keeping the cursor on its name."""
        self.load(self.path, cursor or self.cursor_name)

    def _list(self, app: App[object], backend: Backend, path: str, cursor: str | None) -> None:
        worker = get_current_worker()
        entries: list[Entry] | None = None
        error: FsError | None = None
        if Cap.LIST in backend.caps:
            try:
                entries = backend.list_dir(path)
            except FsError as e:
                error = e
        if worker.is_cancelled:
            return
        try:
            app.call_from_thread(self._apply, backend, path, entries, error, cursor)
        except RuntimeError:
            pass  # the app has stopped

    def _apply(
        self,
        backend: Backend,
        path: str,
        entries: list[Entry] | None,
        error: FsError | None,
        cursor: str | None,
    ) -> None:
        if not self.is_attached:
            return
        if error is not None:
            self.app.notify(str(error), title="Cannot open directory", severity="error", markup=False)
            self._render_status()
            return
        moved = backend is not self.backend or path != self.path
        self.backend = backend
        self.path = path
        self.entries = sort_entries(entries or [])
        if moved:
            self.selected.clear()
        else:
            self.selected &= {e.name for e in self.entries}
        self._render_listing(cursor if cursor is not None or moved else self.cursor_name)

    # -- rendering -------------------------------------------------------------

    def _render_listing(self, cursor: str | None) -> None:
        had_focus = self.screen.focused in (self._table, self._message)
        self._header.show(f"{self.backend.label}:{self.path}")
        listable = self.listable
        self._table.display = listable
        self._message.display = not listable
        if listable:
            self._rows = ([PARENT] if self.path != "/" else []) + [e.name for e in self.entries]
            self._table.clear()
            if self.path != "/":
                self._table.add_row(Text(PARENT), Text("<UP>", justify="right"), Text(""), key=PARENT)
            for entry in self.entries:
                self._table.add_row(*self._cells(entry), key=entry.name)
            index = self._rows.index(cursor) if cursor in self._rows else 0
            self._table.move_cursor(row=index, animate=False)
        else:
            self._rows = []
            self._table.clear()
            self._message.update(
                f"Transfer-only: {self.backend.label} cannot list directories.\n\n"
                "F5 here downloads a file by its path into the other panel.\n"
                f"F5 in the other panel uploads files into {self.path}.\n"
                "F9 changes the location."
            )
        if had_focus:
            self.focus_view()
        self._render_status()

    def _cells(self, entry: Entry) -> tuple[Text, Text, Text]:
        style = ""
        if is_dir(entry):
            style = "bold"
        if entry.name in self.selected:
            style = "bold yellow"
        return (
            Text(entry.name, style=style),
            Text(_size_cell(entry), style=style, justify="right"),
            Text(_mtime_cell(entry), style=style),
        )

    def _render_row(self, entry: Entry) -> None:
        name, size, mtime = self._cells(entry)
        self._table.update_cell(entry.name, "name", name)
        self._table.update_cell(entry.name, "size", size)
        self._table.update_cell(entry.name, "mtime", mtime)

    def _render_status(self) -> None:
        self._status.update(self.status_text)

    def focus_view(self) -> None:
        """Focus the listing, or the transfer-only message."""
        (self._table if self.listable else self._message).focus()

    # -- selection and navigation -------------------------------------------------

    def unselect(self, paths: Iterable[str]) -> None:
        """Drop the entries at ``paths`` (absolute paths) from the selection."""
        names = {posixpath.basename(p) for p in paths if self.backend.parent(p) == self.path}
        for entry in self.entries:
            if entry.name in names and entry.name in self.selected:
                self.selected.discard(entry.name)
                self._render_row(entry)
        self._render_status()

    def action_toggle_select(self) -> None:
        """Toggle the cursor row's selection (never ``..``) and move down."""
        if not self.listable:
            return
        entry = self.cursor_entry
        if entry is not None:
            self.selected ^= {entry.name}
            self._render_row(entry)
            self._render_status()
        self._table.action_cursor_down()

    def action_parent(self) -> None:
        """Go to the parent directory with the cursor on the one just left."""
        if not self.listable or self.path == "/":
            return
        self.load(self.backend.parent(self.path), posixpath.basename(self.path))

    def action_reload(self) -> None:
        if self.listable:
            self.reload()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        name = self.cursor_name
        if name == PARENT:
            self.action_parent()
            return
        entry = self._entry(name)
        if entry is not None and is_dir(entry):
            self.load(self.backend.join(self.path, entry.name))
