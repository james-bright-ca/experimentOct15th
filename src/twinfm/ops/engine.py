"""The operations engine: copy, move, delete, mkdir and rename over any two
backends. The module implements ``twinfm.ops.model.Engine`` with module-level
functions; the docstrings there define the semantics.

How an item travels
    Within one store (``src is dst``) an item whose destination is free is
    copied or moved whole with ``copy_within`` or ``rename``. Otherwise a
    directory is walked: each directory is created at the destination (an
    existing directory is merged into) and each other entry is transferred on
    its own, with a conflict question when its destination exists. Between
    stores a file goes by ``dst.upload`` straight from a local source, or by
    ``src.download`` into a fresh temporary directory and then ``dst.upload``.

Moves between stores, and merging moves
    Each source entry is deleted only after its own transfer succeeded.
    Source directories are removed bottom-up, and only when everything below
    them went; nothing skipped or failed is ever deleted.

Capabilities
    Missing capabilities refuse the whole operation up front with Unsupported:
    move needs DELETE on ``src``, delete DELETE, mkdir MKDIR and rename
    RENAME; within one store a copy needs COPY_WITHIN and a move RENAME;
    between stores ``dst`` needs UPLOAD and a non-local ``src`` DOWNLOAD.
    Without STAT on ``dst`` the target is taken to be a directory and files
    are uploaded with ``overwrite=True`` without asking (the caller confirmed
    that up front). Without STAT on ``src`` every selected path is taken to
    be a file. Unsupported errors met on the way are recorded without asking.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from functools import partial

from twinfm.backend.base import (
    AlreadyExists,
    Backend,
    Cap,
    Entry,
    FsError,
    IsADirectory,
    NotADirectory,
    Unsupported,
    from_oserror,
)
from twinfm.backend.local import LocalBackend
from twinfm.ops import plan
from twinfm.ops.model import Callbacks, Conflict, OnError, Progress, Report

__all__ = ["copy", "delete", "mkdir", "move", "rename"]


class _ItemFailed(Exception):
    """The current item failed; the failure is already in the report."""


class _Batch:
    """The state of one engine call: the report, the remembered *_ALL answers
    and whether the batch has stopped."""

    def __init__(self, op: str, cb: Callbacks) -> None:
        self.op = op
        self.cb = cb
        self.report = Report()
        self.skip_errors = False
        self.conflict_all: Conflict | None = None
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True
        self.report.cancelled = True

    def go_on(self) -> bool:
        """Poll ``cancelled()``; False (and the batch stops) once it is True."""
        if self.cb.cancelled():
            self.stop()
        return not self.stopped

    def run[T](self, path: str, action: Callable[[], T]) -> T:
        """``action()``, asking the caller about each FsError (RETRY runs it
        again). A failure that stays is recorded against ``path`` and raised
        as _ItemFailed. Unsupported is recorded without asking."""
        while True:
            try:
                return action()
            except FsError as err:
                if not isinstance(err, Unsupported) and not self.skip_errors:
                    answer = self.cb.ask_error(path, err)
                    if answer is OnError.RETRY:
                        continue
                    if answer is OnError.SKIP_ALL:
                        self.skip_errors = True
                    elif answer is OnError.ABORT:
                        self.stop()
                self.report.failed.append((path, err))
                raise _ItemFailed from err

    def overwrite(self, src: Entry, dst: Entry) -> bool:
        """Whether ``src`` replaces the existing ``dst``. CANCEL stops the batch."""
        answer = self.conflict_all or self.cb.ask_conflict(src, dst)
        if answer in (Conflict.OVERWRITE_ALL, Conflict.SKIP_ALL):
            self.conflict_all = answer
        elif answer is Conflict.CANCEL:
            self.stop()
        return answer in (Conflict.OVERWRITE, Conflict.OVERWRITE_ALL)


def _make_temp_dir() -> str:
    try:
        return tempfile.mkdtemp(prefix="twinfm-")
    except OSError as e:
        raise from_oserror(e, tempfile.gettempdir()) from e


def _make_dir(dst: Backend, path: str) -> None:
    """Create directory ``path``, or merge into the directory already there."""
    try:
        dst.mkdir(path)
    except (AlreadyExists, Unsupported) as err:
        if Cap.STAT not in dst.caps:
            if isinstance(err, AlreadyExists):
                return  # nothing to look with: take it for a directory
            raise
        existing = plan.stat_or_none(dst, path)
        if existing is None:
            raise
        if not plan.is_dir(existing):
            raise NotADirectory(path, "will not replace a file with a directory", dst.id) from err


class _Transfer:
    """One copy or move: carries out the planned work items."""

    def __init__(
        self, batch: _Batch, src: Backend, dst: Backend, move: bool, total: int
    ) -> None:
        self.batch = batch
        self.src = src
        self.dst = dst
        self.move = move
        self.same = src is dst
        self.total = total
        self.done = 0

    def selected(self, path: str, items: list[plan.WorkItem]) -> None:
        """Process the work items of the selected ``path``; it is done when
        nothing in it was skipped or failed and the batch did not stop."""
        clean = True
        blocked: list[str] = []  # directories that failed: their contents are left alone
        kept: set[str] = set()  # source directories a move must keep
        emptied: list[str] = []  # source directories a move removes once emptied
        for item in items:
            current = item.entry.path
            if any(current.startswith(d + "/") for d in blocked):
                self.done += 1
                continue
            if not self.batch.go_on():
                return
            self.batch.cb.progress(Progress(self.batch.op, current, self.done, self.total))
            ok = self.one(item)
            self.done += 1
            if self.batch.stopped:
                return
            if not ok:
                clean = False
                self.keep_parents(kept, current, path)
                if item.entry.kind == "dir":
                    kept.add(current)
                    blocked.append(current)
            elif self.move and item.entry.kind == "dir" and not item.whole:
                emptied.append(current)
        for directory in reversed(emptied):
            if directory in kept:
                continue
            try:
                self.batch.run(directory, partial(self.src.delete, directory))
            except _ItemFailed:
                clean = False
                self.keep_parents(kept, directory, path)
                if self.batch.stopped:
                    return
        if clean:
            self.batch.report.done.append(path)

    def keep_parents(self, kept: set[str], path: str, top: str) -> None:
        while path not in (top, "/"):
            path = self.src.parent(path)
            kept.add(path)

    def one(self, item: plan.WorkItem) -> bool:
        """Process one work item; False when it was skipped or failed (recorded)."""
        entry, dest = item.entry, item.dest
        try:
            if item.whole:
                act = self.src.rename if self.move else self.src.copy_within
                self.batch.run(entry.path, partial(act, entry.path, dest))
            elif entry.kind == "dir":
                self.batch.run(entry.path, partial(_make_dir, self.dst, dest))
            elif not self.same and entry.kind == "symlink" and entry.target_kind == "dir":
                self.batch.report.skipped.append(entry.path)
                return False
            else:
                return self.file(entry, dest)
        except _ItemFailed:
            return False
        return True

    def file(self, entry: Entry, dest: str) -> bool:
        """Transfer a non-directory entry, asking first if ``dest`` exists."""
        overwrite = Cap.STAT not in self.dst.caps
        if not overwrite:
            existing = self.batch.run(entry.path, partial(self.existing, dest))
            if existing is not None:
                overwrite = self.batch.overwrite(entry, existing)
                if not overwrite:
                    if not self.batch.stopped:
                        self.batch.report.skipped.append(entry.path)
                    return False
        self.batch.run(entry.path, partial(self.put, entry.path, dest, overwrite))
        if self.move and not self.same:
            self.batch.run(entry.path, partial(self.src.delete, entry.path))
        return True

    def existing(self, dest: str) -> Entry | None:
        found = plan.stat_or_none(self.dst, dest)
        if found is not None and found.kind == "dir":
            raise IsADirectory(dest, "will not replace a directory", self.dst.id)
        return found

    def put(self, path: str, dest: str, overwrite: bool) -> None:
        if self.same:
            act = self.src.rename if self.move else self.src.copy_within
            act(path, dest, overwrite)
        elif isinstance(self.src, LocalBackend):
            self.dst.upload(path, dest, overwrite)
        else:
            staging = _make_temp_dir()
            try:
                local = os.path.join(staging, "content")
                self.src.download(path, local)
                self.dst.upload(local, dest, overwrite)
            finally:
                shutil.rmtree(staging, ignore_errors=True)


def _plan_selected(src: Backend, path: str, dest: str, dst: Backend) -> list[plan.WorkItem]:
    return plan.expand(src, plan.source_entry(src, path), dest, dst)


def _transfer(
    op: str,
    src: Backend,
    paths: Sequence[str],
    dst: Backend,
    target: str,
    cb: Callbacks,
    need_delete: bool,
) -> Report:
    move = op == "move"
    paths = list(paths)
    first = paths[0] if paths else target
    if need_delete:
        src.require(Cap.DELETE, first)
    if src is dst:
        src.require(Cap.RENAME if move else Cap.COPY_WITHIN, first)
    else:
        dst.require(Cap.UPLOAD, target)
        if not isinstance(src, LocalBackend):
            src.require(Cap.DOWNLOAD, first)
    plan.check_selection(src, paths)
    if not paths:
        return Report()
    dests = plan.destinations(src, paths, dst, target)
    if src is dst:
        plan.refuse_onto_self(src, paths, dests, move)

    batch = _Batch(op, cb)
    planned: list[tuple[str, list[plan.WorkItem]]] = []
    for path, dest in zip(paths, dests, strict=True):
        try:
            planned.append((path, batch.run(path, partial(_plan_selected, src, path, dest, dst))))
        except _ItemFailed:
            if batch.stopped:
                return batch.report
    transfer = _Transfer(batch, src, dst, move, sum(len(items) for _, items in planned))
    for path, items in planned:
        transfer.selected(path, items)
        if batch.stopped:
            break
    return batch.report


def copy(src: Backend, paths: Sequence[str], dst: Backend, target: str, cb: Callbacks) -> Report:
    """Copy the entries at ``paths`` on ``src`` to ``target`` on ``dst``
    (see ``Engine.copy``)."""
    return _transfer("copy", src, paths, dst, target, cb, need_delete=False)


def move(src: Backend, paths: Sequence[str], dst: Backend, target: str, cb: Callbacks) -> Report:
    """Move the entries at ``paths`` on ``src`` to ``target`` on ``dst``
    (see ``Engine.move``). Needs Cap.DELETE on ``src``."""
    return _transfer("move", src, paths, dst, target, cb, need_delete=True)


def delete(b: Backend, paths: Sequence[str], cb: Callbacks) -> Report:
    """Delete the entries at ``paths``, directories recursively, a symlink
    and never its target (see ``Engine.delete``). Each selected path is one
    work item."""
    paths = list(paths)
    b.require(Cap.DELETE, paths[0] if paths else b.home())
    plan.check_selection(b, paths)
    batch = _Batch("delete", cb)
    for done, path in enumerate(paths):
        if not batch.go_on():
            break
        cb.progress(Progress("delete", path, done, len(paths)))
        try:
            batch.run(path, partial(b.delete, path, recursive=True))
        except _ItemFailed:
            if batch.stopped:
                break
            continue
        batch.report.done.append(path)
    return batch.report


def mkdir(b: Backend, parent: str, name: str) -> str:
    """Create directory ``name`` inside ``parent`` and return its path (see
    ``Engine.mkdir``). Raises the backend's FsError."""
    b.require(Cap.MKDIR, parent)
    plan.check_name(b, parent, name)
    path = b.join(parent, name)
    b.mkdir(path)
    return path


def rename(b: Backend, path: str, new_name: str, cb: Callbacks) -> Report:
    """Give ``path`` the name ``new_name`` in its directory, as
    ``move(b, [path], b, b.join(b.parent(path), new_name), cb)`` (see
    ``Engine.rename``). Needs Cap.RENAME; a merge that empties source
    directories also needs Cap.DELETE, recorded per directory if missing."""
    b.require(Cap.RENAME, path)
    parent = b.parent(path)
    plan.check_name(b, parent, new_name)
    return _transfer("move", b, [path], b, b.join(parent, new_name), cb, need_delete=False)
