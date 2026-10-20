"""One file panel: a header with the backend label and path, the listing,
the selection and a status line, or the find line in the status line's
place while it is open.

The find line searches the listing by name, moving the cursor to the first
match, or holds a path to go to (see ``twinfm.ui.find`` for the rules). It
never takes the focus: the main screen sends it the keys typed while the
listing has the focus. Going to another directory closes it.

The listing puts ``..`` first (except at ``/``), then directories (and
symlinks to directories), then everything else, each group by name without
regard to case; dotfiles are shown. A backend without Cap.LIST gets a
transfer-only view: a message instead of a listing.

Listing runs in a Textual thread worker; the result comes back to the UI
thread through ``call_from_thread``. Until the latest request's result is
applied the panel is loading: the status line reads ``Loading…``, the find
line ends in `` · loading…``, a click on a row does nothing, and an older
request's result is dropped. Ctrl-R refreshes: its worker first
drops the backend's cached listings of the directory (``Backend.invalidate``),
so that changes made outside twinfm show up. Every other listing may come
from the backend's cache, which the backend keeps current for twinfm's own
operations.
"""

from __future__ import annotations

import posixpath
import time
from collections.abc import Callable, Iterable
from functools import partial

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static
from textual.worker import get_current_worker

from twinfm import backend as registry
from twinfm.backend.base import Backend, Cap, Entry, FsError, NotADirectory
from twinfm.ui.find import PARENT, first_match, is_path, match_indices, step_match

__all__ = ["PARENT", "FindLine", "Panel", "format_size", "is_dir", "sort_entries"]


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


class FindLine(Static):
    """The find line: ``Find: <text> k/n`` (or ``no match``) for a search,
    ``Go to: <text>`` for a path, then `` · N sel`` when a selection exists.
    Cut from the left when too long, so that the end of the text stays
    visible. Never focusable."""

    def render(self) -> Text:
        width = self.content_size.width
        text = str(self.content)
        if 1 < width < len(text):
            text = "…" + text[len(text) - width + 1 :]
        return Text(text)


class PanelTable(DataTable[Text]):
    """The listing. Enter (or a click on the cursor row) opens the row. While
    its panel is loading a click neither moves the cursor nor opens a row."""

    async def _on_click(self, event: events.Click) -> None:
        panel = self.parent
        if isinstance(panel, Panel) and panel.is_loading:
            event.prevent_default()  # skips DataTable's own click handling


class TransferOnlyView(Static, can_focus=True):
    """Stands in for the listing when the backend cannot list directories."""


class Panel(Vertical):
    """A directory on a backend, its listing and its selection.

    ``backend`` and ``path`` change only once a listing of the new location
    succeeded; a failed listing shows an error and the panel stays where it
    was. ``is_loading`` is True from a request until its result is applied.
    ``selected`` holds names in the current directory.
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
    Panel > #find {
        height: 1;
    }
    """

    BINDINGS = [
        Binding("space,insert,ctrl+t", "toggle_select", "Select", show=False),
        Binding("backspace", "parent", "Parent directory", show=False),
        Binding("ctrl+r", "reload", "Refresh", key_display="^R"),
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
        self._find = FindLine(markup=False, id="find")
        self._find.display = False
        self._finding = False
        self._find_text = ""
        self._request: Callable[[], None] | None = None

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._table
        yield self._message
        yield self._status
        yield self._find

    def on_mount(self) -> None:
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
    def is_loading(self) -> bool:
        """Whether a listing request (``load`` or ``go_to``) is on its way.
        Not named ``loading``: Textual's ``Widget.loading`` would take the
        panel out of the focus chain."""
        return self._request is not None

    @property
    def status_text(self) -> str:
        """What the status line says."""
        if self.is_loading:
            return "Loading…"
        if not self.listable:
            return "transfer-only"
        if self.selected:
            total = sum(
                e.size for e in self.entries
                if e.name in self.selected and e.kind != "dir" and e.size is not None
            )
            return f"{len(self.selected)} selected, {total:,} bytes"
        return f"{len(self.entries)} items"

    @property
    def finding(self) -> bool:
        """Whether the find line is open."""
        return self._finding

    @property
    def find_text(self) -> str:
        """The find line's text; empty while it is closed."""
        return self._find_text

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

    def load(
        self,
        path: str,
        cursor: str | None = None,
        backend: Backend | None = None,
        *,
        fresh: bool = False,
    ) -> None:
        """List ``path`` on ``backend`` (default: the current one) in a worker
        and show it, with the cursor on ``cursor`` if that name is listed.
        ``fresh`` drops the backend's cached listings of ``path`` first."""
        self._begin(partial(self.load, path, cursor, backend or self.backend, fresh=fresh))
        self.run_worker(
            partial(self._list, self.app, backend or self.backend, path, cursor, fresh),
            thread=True,
            group="list",
            exclusive=True,
            name=f"list {path}",
        )

    def reload(self, cursor: str | None = None) -> None:
        """List the current directory again, keeping the cursor on its name.
        While a request is on its way, make that request again instead: the
        panel still gets where it was going, with a listing made after now."""
        if self._request is not None:
            self._request()
        else:
            self.load(self.path, cursor or self.cursor_name)

    def go_to(self, path: str, backend: Backend | None = None) -> None:
        """Go to the typed ``path`` (absolute and normalised) on ``backend``
        (default: the current one), listing it in a worker. A file opens its
        directory with the cursor on it. Once there the find line closes;
        when ``path`` cannot be listed the error is shown, and the panel and
        the find line stay as they are."""
        self._begin(partial(self.go_to, path, backend or self.backend))
        self.run_worker(
            partial(self._go, self.app, backend or self.backend, path),
            thread=True,
            group="list",
            exclusive=True,
            name=f"go to {path}",
        )

    def _begin(self, request: Callable[[], None]) -> None:
        """The panel is loading until ``request``'s result is applied;
        ``request`` makes the same request again (see ``reload``)."""
        self._request = request
        self._render_status()

    def _list(
        self, app: App[object], backend: Backend, path: str, cursor: str | None, fresh: bool
    ) -> None:
        worker = get_current_worker()
        entries: list[Entry] | None = None
        error: FsError | None = None
        if Cap.LIST in backend.caps:
            if fresh:
                backend.invalidate(path)
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

    def _go(self, app: App[object], backend: Backend, path: str) -> None:
        worker = get_current_worker()
        entries: list[Entry] | None = None
        error: FsError | None = None
        cursor: str | None = None
        if Cap.LIST in backend.caps:
            # list_dir is the existence check: stat would be an uncached remote call on btty.
            try:
                entries = backend.list_dir(path)
            except NotADirectory as e:
                error = e
                parent = backend.parent(path)
                try:
                    entries = backend.list_dir(parent)
                except FsError:
                    pass
                else:
                    path, cursor, error = parent, posixpath.basename(path), None
            except FsError as e:
                error = e
        if worker.is_cancelled:
            return
        try:
            app.call_from_thread(self._apply, backend, path, entries, error, cursor, close_find=True)
        except RuntimeError:
            pass  # the app has stopped

    def _apply(
        self,
        backend: Backend,
        path: str,
        entries: list[Entry] | None,
        error: FsError | None,
        cursor: str | None,
        *,
        close_find: bool = False,
    ) -> None:
        if not self.is_attached:
            return
        # call_from_thread runs this in the posting worker's context. A newer
        # request cancelled that worker (exclusive group) on this thread, so a
        # cancelled one here means a stale result, even if it was posted first.
        if get_current_worker().is_cancelled:
            return
        self._request = None
        if error is not None:
            self.app.notify(str(error), title="Cannot open directory", severity="error", markup=False)
            self._render_status()
            return
        moved = backend is not self.backend or path != self.path
        if moved or close_find:
            self.find_close()
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
        self._clear_table()
        if listable:
            self._rows = ([PARENT] if self.path != "/" else []) + [e.name for e in self.entries]
            if self.path != "/":
                self._table.add_row(Text(PARENT), Text("<UP>", justify="right"), Text(""), key=PARENT)
            for entry in self.entries:
                self._table.add_row(*self._cells(entry), key=entry.name)
            index = self._rows.index(cursor) if cursor in self._rows else 0
            self._table.move_cursor(row=index, animate=False)
        else:
            self._rows = []
            self._message.update(
                f"Transfer-only: {self.backend.label} cannot list directories.\n\n"
                "F5 here downloads a file by its path into the other panel.\n"
                f"F5 in the other panel uploads files into {self.path}.\n"
                "F9 changes the location."
            )
        if had_focus:
            self.focus_view()
        self._render_status()

    def _clear_table(self) -> None:
        """Empty the listing, its columns too: DataTable only ever widens an
        auto-width column, so the Name column is measured afresh from the
        rows added next."""
        self._table.clear(columns=True)
        self._table.add_column("Name", key="name")
        self._table.add_column("Size", key="size", width=8)
        self._table.add_column("Modified", key="mtime", width=16)

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
        self._status.display = not self._finding
        self._find.display = self._finding
        if self._finding:
            self._find.update(self._find_line())

    def _find_line(self) -> str:
        text = self._find_text
        if self._find_is_path:
            line = f"Go to: {text}"
        else:
            line = f"Find: {text}"
            if text:
                hits = match_indices(text, self._rows)
                row = self._table.cursor_row
                if not hits:
                    line += " no match"
                else:
                    place = hits.index(row) + 1 if row in hits else "-"
                    line += f" {place}/{len(hits)}"
        if self.selected:
            line += f" · {len(self.selected)} sel"
        if self.is_loading:
            line += " · loading…"
        return line

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
        """Ctrl-R: list the current directory again, past any cached listing."""
        if self.listable:
            self.load(self.path, self.cursor_name, fresh=True)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        name = self.cursor_name
        if name == PARENT:
            self.action_parent()
            return
        entry = self._entry(name)
        if entry is not None and is_dir(entry):
            self.load(self.backend.join(self.path, entry.name))

    # -- the find line -------------------------------------------------------------

    @property
    def _find_is_path(self) -> bool:
        return is_path(self._find_text, registry.available())

    def find_type(self, text: str) -> None:
        """Add ``text`` to the find line, opening it if it is closed. A search
        puts the cursor on its first match, or leaves it when nothing matches."""
        if not self.listable or not text:
            return
        self._finding = True
        self._find_text += text
        self._find_changed()

    def find_backspace(self) -> None:
        """Delete the last character; deleting the only one closes the line."""
        if not self._finding:
            return
        self._find_text = self._find_text[:-1]
        if self._find_text:
            self._find_changed()
        else:
            self.find_close()

    def find_clear(self) -> None:
        """Empty the find line, which stays open."""
        if self._finding:
            self._find_text = ""
            self._render_status()

    def find_step(self, step: int) -> None:
        """Put the cursor on the next match (``step`` 1) or the previous one
        (``step`` -1), wrapping around. Nothing for a path."""
        if not self._finding or self._find_is_path:
            return
        index = step_match(self._find_text, self._rows, self._table.cursor_row, step)
        if index is not None:
            self._table.move_cursor(row=index, animate=False)
        self._render_status()

    def find_close(self) -> None:
        """Close the find line; the cursor stays where it is."""
        if self._finding:
            self._finding = False
            self._find_text = ""
            self._render_status()

    def _find_changed(self) -> None:
        if not self._find_is_path:
            index = first_match(self._find_text, self._rows)
            if index is not None:
                self._table.move_cursor(row=index, animate=False)
        self._render_status()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._finding:
            self._render_status()  # keep the line's k/n right when the cursor moves
