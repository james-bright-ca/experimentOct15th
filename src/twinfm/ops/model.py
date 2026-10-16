"""Shapes shared by the operations engine and the UI: the answers a user can
give, progress and result reports, the callbacks the engine calls, and the
Engine protocol the UI calls.

Threading: the UI calls Engine methods in a worker thread, and the engine
calls every Callbacks method on that same thread. A UI implementation of
``ask_conflict`` / ``ask_error`` blocks there until the user answers (bridging
to the UI thread with ``call_from_thread``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from twinfm.backend.base import Backend, Entry, FsError

__all__ = ["Callbacks", "Conflict", "Engine", "OnError", "Progress", "Report"]


class Conflict(Enum):
    """The answer to "the destination already exists"."""

    OVERWRITE = "overwrite"  # replace this destination
    SKIP = "skip"  # leave this destination alone, carry on with the batch
    OVERWRITE_ALL = "overwrite_all"  # replace this one and every later conflict, without asking
    SKIP_ALL = "skip_all"  # skip this one and every later conflict, without asking
    CANCEL = "cancel"  # stop the batch now; nothing further is written


class OnError(Enum):
    """The answer to "this item failed"."""

    RETRY = "retry"  # try the same item again
    SKIP = "skip"  # give up on this item, carry on with the batch
    SKIP_ALL = "skip_all"  # likewise for this and every later error, without asking
    ABORT = "abort"  # stop the batch now


@dataclass(frozen=True, slots=True)
class Progress:
    """Where a running operation is.

    op
        The engine operation: ``"copy"``, ``"move"`` or ``"delete"`` (a rename
        reports as ``"move"``).
    current
        The source path of the work item about to be processed.
    items_done, items_total
        Work items finished so far and in all (each file, symlink or directory
        after the selection is expanded); ``0 <= items_done <= items_total``.
    bytes_done, bytes_total
        Byte counts when the engine knows them, else None. Backends transfer
        whole files, so progress is per item.
    """

    op: str
    current: str
    items_done: int
    items_total: int
    bytes_done: int | None = None
    bytes_total: int | None = None


@dataclass(slots=True)
class Report:
    """The outcome of one engine call. Paths are source paths.

    done
        Each selected item (as given in ``paths``) that was processed
        completely, with nothing inside it skipped or failed.
    skipped
        Paths left alone on purpose without an error: a conflict answered
        SKIP / SKIP_ALL, or a symlink to a directory between backends. The path
        is the most specific one concerned (the item itself or a path inside
        a selected directory).
    failed
        ``(path, error)`` for each error that RETRY did not overcome, whatever
        the user answered next, and for each Unsupported refusal recorded
        without asking. Most specific path, as for ``skipped``.
    cancelled
        True when the batch stopped early: CANCEL, ABORT, or ``cancelled()``
        returned True.
    """

    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, FsError]] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        """True when nothing failed and the batch ran to the end."""
        return not self.failed and not self.cancelled


class Callbacks(Protocol):
    """What the engine asks of its caller while it runs. All methods are
    called on the engine's thread."""

    def ask_conflict(self, src: Entry, dst: Entry) -> Conflict:
        """A non-directory ``src`` is about to replace the existing
        non-directory ``dst`` (both Entries as their backends describe them).

        Not called once an OVERWRITE_ALL or SKIP_ALL answer applies, nor when
        the destination backend lacks Cap.STAT (the caller confirmed replacing
        up front). A directory onto a directory merges without asking; a file
        onto a directory, or a directory onto a file, is an item error
        (``ask_error``), not a conflict.
        """
        ...

    def ask_error(self, path: str, err: FsError) -> OnError:
        """Processing ``path`` failed with ``err``. Not called once SKIP_ALL
        was answered, and never for Unsupported (that is recorded in
        ``Report.failed`` directly)."""
        ...

    def progress(self, p: Progress) -> None:
        """Called before each work item starts."""
        ...

    def cancelled(self) -> bool:
        """Polled between files, never in the middle of one. True stops the
        batch as CANCEL does, with ``Report.cancelled`` True."""
        ...


class Engine(Protocol):
    """The file operations the UI performs. The module ``twinfm.ops.engine``
    implements it as module-level functions; a test stub can be any object with
    these methods.

    All methods are synchronous and block until done. An operation that is
    refused as a whole raises FsError before anything is written:
    Unsupported when a capability it needs is missing, NotADirectory for an
    unusable ``target``, OperationFailed for copying or moving onto itself or
    into its own subtree, or for an invalid name. Problems with individual items
    never raise; they go through ``cb`` and end up in the Report.

    Paths in ``paths`` are absolute paths of the selected entries on their
    backend (never ``..``). ``target`` resolution for copy and move: an
    existing directory receives each item inside it under the item's own
    name; otherwise, with exactly one item, ``target`` is the item's new full
    path; otherwise NotADirectory is raised. Two backends are the same store
    only when ``src is dst``.
    """

    def copy(
        self, src: Backend, paths: Sequence[str], dst: Backend, target: str, cb: Callbacks
    ) -> Report:
        """Copy the entries at ``paths`` on ``src`` to ``target`` on ``dst``.

        Within one store a symlink is copied as a link. Between stores a
        symlink to a file is copied as its content and a symlink to a directory
        is reported skipped.
        """
        ...

    def move(
        self, src: Backend, paths: Sequence[str], dst: Backend, target: str, cb: Callbacks
    ) -> Report:
        """Like ``copy``, then the sources are removed; needs Cap.DELETE on
        ``src``. Between stores each source file is deleted only after its own
        transfer succeeded, source directories are removed bottom-up only when
        empty, and nothing skipped or failed is deleted."""
        ...

    def delete(self, b: Backend, paths: Sequence[str], cb: Callbacks) -> Report:
        """Delete the entries at ``paths``, directories recursively; a symlink
        is removed, never its target. Needs Cap.DELETE."""
        ...

    def mkdir(self, b: Backend, parent: str, name: str) -> str:
        """Create directory ``name`` (a single path component) inside
        ``parent`` and return its path. Needs Cap.MKDIR. Raises the backend's
        FsError (e.g. AlreadyExists) instead of reporting it."""
        ...

    def rename(self, b: Backend, path: str, new_name: str, cb: Callbacks) -> Report:
        """Give ``path`` the name ``new_name`` (a single path component) in the
        same directory. Needs Cap.RENAME. Equivalent to
        ``move(b, [path], b, b.join(b.parent(path), new_name), cb)``: an
        existing directory called ``new_name`` receives the item, an existing
        file called ``new_name`` is a conflict."""
        ...
