"""Archive mode for a copy or move between stores (see ``Engine.copy``): the
batch travels as one gzip tar instead of file by file. ``engine`` calls
``transfer``; the rules for skips, failures and *_ALL answers are the
engine's ``_Batch``.

The flow
    1. Scan, before anything is written: the source tree (``walk`` of each
       selected directory), then each destination (a ``stat`` for a single
       item, else one fresh ``list_dir`` of the receiving directory) and a
       ``walk`` of each destination that is an existing directory. Every
       conflict and file/directory clash is settled through the batch now.
    2. Pack the selected names on the source side.
    3. Transfer the archive with ``upload`` or ``download``.
    4. Unpack it into the staging directory ``.twinfm-<hex>`` inside the
       receiving directory, on the target's filesystem.
    5. Prune what was skipped or failed from staging with ``delete_many``.
    6. Merge each item into its destination: one ``merge`` per selected
       item, so a receiving directory reached through a symlink works and
       one item's failure leaves the others. For a move, an item's sources
       are deleted once its merge returned.

Temporaries
    Uploading, the archive is packed in a fresh local temporary directory
    and uploaded to ``.twinfm-<hex>.tgz`` next to staging. Downloading, it
    is packed into the source's ``archive_dir``, which may be another
    filesystem, so it is only written by ``pack``, read by ``download`` and
    deleted, and downloaded into a fresh local temporary directory.
    ``unpack`` removes its archive on success; every other temporary is
    removed in ``finally``, and one that stays is recorded in
    ``Report.failed`` as OperationFailed "temporary file left behind".

Falling back
    ``transfer`` hands the batch to the per-file transfer when archive mode
    does not apply, when the scanned size or the packed archive exceeds the
    non-local side's ``archive_max_bytes``, when the scan finds a directory
    without owner read, write and search permission (or of unknown mode),
    or when ``pack``, ``unpack`` or the first ``merge`` raises
    Unsupported. Such a directory
    would be staged with its mode, and then neither merge's renames nor
    pruning and cleanup could move or remove its entries. Nothing is then
    left at the destination, and the answers the user already gave are
    replayed instead of asked again.

    A later ``merge`` raising Unsupported leaves the items before it as
    they merged; after cleanup, that item and the ones after it go per file
    through the same batch, replaying the answers in the same way.
"""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial

from twinfm.backend.base import (
    Backend,
    Cap,
    Entry,
    FsError,
    IsADirectory,
    NotADirectory,
    NotFound,
    OperationFailed,
    Unsupported,
)
from twinfm.backend.local import LocalBackend
from twinfm.ops import plan
from twinfm.ops.engine import _Batch, _ItemFailed, _make_temp_dir
from twinfm.ops.engine import _plan_selected, _Transfer
from twinfm.ops.model import Callbacks, Conflict, OnError, Progress, Report

__all__ = ["transfer"]


class _Fallback(Exception):
    """The batch goes per file instead; nothing is left at the destination."""


class _Answers:
    """Callbacks passing through to ``cb`` that remember each final answer,
    so that the per-file transfer after a fallback gives it again instead of
    asking twice. RETRY is never remembered."""

    def __init__(self, cb: Callbacks) -> None:
        self.cb = cb
        self.conflicts: dict[tuple[str, str], Conflict] = {}
        self.errors: dict[tuple[str, type[FsError], str], OnError] = {}

    def ask_conflict(self, src: Entry, dst: Entry) -> Conflict:
        key = (src.path, dst.path)
        if key not in self.conflicts:
            self.conflicts[key] = self.cb.ask_conflict(src, dst)
        return self.conflicts[key]

    def ask_error(self, path: str, err: FsError) -> OnError:
        key = (path, type(err), str(err))
        answer = self.errors.get(key) or self.cb.ask_error(path, err)
        if answer is not OnError.RETRY:
            self.errors[key] = answer
        return answer

    def progress(self, p: Progress) -> None:
        self.cb.progress(p)

    def cancelled(self) -> bool:
        return self.cb.cancelled()


@dataclass
class _Item:
    """One selected path and what of it goes."""

    path: str
    dest: str
    tree: list[Entry]  # the selected entry, then everything below it
    files: list[str] = field(default_factory=list)  # non-directories that go
    dirs: list[str] = field(default_factory=list)  # directories that go, in walk order
    prune: list[str] = field(default_factory=list)  # staging paths of what stays behind
    kept: set[str] = field(default_factory=set)  # source directories a move keeps
    clean: bool = True  # nothing inside was skipped or failed
    conflict_all: Conflict | None = None  # the batch's *_ALL answer when settling began


def _tree(b: Backend, path: str, entry: Entry | None) -> list[Entry]:
    """The selected ``path`` (``entry`` when already known) and every entry below it."""
    if entry is None:
        entry = plan.source_entry(b, path)
    return [entry, *b.walk(path)] if entry.kind == "dir" else [entry]


def _below(b: Backend, root: str, top: str, path: str) -> str:
    """Where ``path``, which lies below ``top``, lands when ``top`` lands at ``root``."""
    for part in path[len(top) :].split("/"):
        if part:
            root = b.join(root, part)
    return root


def _keep_parents(b: Backend, kept: set[str], path: str, top: str) -> None:
    while path not in (top, "/"):
        path = b.parent(path)
        kept.add(path)


def _or_fall_back(action: Callable[[], None]) -> None:
    """``action()`` (pack, unpack or merge), with Unsupported turned into _Fallback."""
    try:
        action()
    except Unsupported as err:
        raise _Fallback from err


def transfer(
    op: str,
    src: Backend,
    paths: Sequence[str],
    dests: Sequence[str],
    dst: Backend,
    cb: Callbacks,
) -> Report | Callbacks:
    """Copy or move (``op``) ``paths`` on ``src`` to ``dests`` on ``dst`` in
    archive mode; the engine has checked the capabilities and resolved
    ``dests``. Returns the Report, or the Callbacks to transfer the batch
    per file with: ``cb`` itself when archive mode does not apply, else
    ``cb`` replaying the answers already given."""
    upload = isinstance(src, LocalBackend)
    if (
        Cap.ARCHIVE not in src.caps
        or Cap.ARCHIVE not in dst.caps
        or upload == isinstance(dst, LocalBackend)
        or (not upload and src.archive_dir is None)
        or len(set(paths)) != len(paths)
        or len({src.parent(path) for path in paths}) != 1
    ):
        return cb
    known: Entry | None = None
    if len(paths) == 1:
        try:
            known = plan.source_entry(src, paths[0])
        except FsError:
            return cb  # the per-file transfer reports it
        if known.kind != "dir":
            return cb
    answers = _Answers(cb)
    try:
        return _Run(_Batch(op, answers), src, dst, dst.parent(dests[0])).go(paths, dests, known)
    except _Fallback:
        return answers


class _Run:
    """One copy or move in archive mode."""

    def __init__(self, batch: _Batch, src: Backend, dst: Backend, into: str) -> None:
        self.batch = batch
        self.src = src
        self.dst = dst
        self.move = batch.op == "move"
        self.upload = isinstance(src, LocalBackend)
        self.remote = dst if self.upload else src
        self.into = into  # the directory on dst that receives every item
        self.name = ".twinfm-" + secrets.token_hex(8)
        self.staging = dst.join(into, self.name)
        self.single = False
        self.listing: dict[str, Entry] | None = None
        self.items: list[_Item] = []
        self.temp: str | None = None  # the local temporary directory
        self.temps: list[tuple[Backend, str]] = []  # temporaries on the stores
        self.rest: list[_Item] = []  # the items left to the per-file transfer

    def go(self, paths: Sequence[str], dests: Sequence[str], known: Entry | None) -> Report:
        batch = self.batch
        items = self.scan(paths, dests, known)
        entries = [e for item in items for e in item.tree]
        limit = self.remote.archive_max_bytes
        size = sum(e.size or 0 for e in entries if e.kind == "file")
        locked = any(
            e.kind == "dir" and (e.mode is None or e.mode & 0o700 != 0o700) for e in entries
        )
        if not batch.stopped and (locked or (limit is not None and size > limit)):
            raise _Fallback
        self.single = len(items) == 1
        for item in items:
            if batch.stopped:
                break
            if self.settle(item):
                self.items.append(item)
        if self.items and not batch.stopped:
            try:
                self.carry()
            except _ItemFailed:
                pass  # recorded against every item: nothing more can go
            finally:
                self.clean_up()
            if self.rest:
                self.per_file()
        return batch.report

    # -- the scan -----------------------------------------------------------------

    def scan(self, paths: Sequence[str], dests: Sequence[str], known: Entry | None) -> list[_Item]:
        items: list[_Item] = []
        for path, dest in zip(paths, dests, strict=True):
            try:
                tree = self.batch.run(path, partial(_tree, self.src, path, known))
            except _ItemFailed:
                if self.batch.stopped:
                    break
                continue
            items.append(_Item(path, dest, tree))
        return items

    def found(self, item: _Item) -> Entry | None:
        """What is at ``item.dest``."""
        if self.single:
            return plan.stat_or_none(self.dst, item.dest)
        if self.listing is None:
            self.dst.invalidate(self.into)  # a cached listing must not decide a conflict
            self.listing = {e.name: e for e in self.dst.list_dir(self.into)}
        return self.listing.get(plan.name_of(item.dest))

    def look(self, entry: Entry, dest: str, seen: list[Entry | None]) -> Entry | None:
        """What is at ``dest``: the scanned ``seen`` entry the first time, a
        fresh stat on each RETRY. Raises the clash when ``entry`` may neither
        replace it nor merge into it (kinds by lstat, as ``merge`` takes them)."""
        found = seen.pop() if seen else plan.stat_or_none(self.dst, dest)
        if found is None:
            return None
        if entry.kind == "dir" and found.kind != "dir":
            if plan.is_dir(found):
                raise NotADirectory(dest, "will not merge a directory into a symlink", self.dst.id)
            raise NotADirectory(dest, "will not replace a file with a directory", self.dst.id)
        if entry.kind != "dir" and found.kind == "dir":
            raise IsADirectory(dest, "will not replace a directory", self.dst.id)
        return found

    def settle(self, item: _Item) -> bool:
        """Settle the conflicts and clashes of ``item`` through the batch and
        sort its entries into what goes and what stays; False when nothing
        of it goes or the batch stopped."""
        batch = self.batch
        item.conflict_all = batch.conflict_all
        root = item.tree[0]
        if root.kind == "other":
            batch.report.skipped.append(item.path)
            return False
        try:
            found = batch.run(item.path, partial(self.found, item))
        except _ItemFailed:
            return False
        staged = self.dst.join(self.staging, plan.name_of(item.path))
        below: dict[str, Entry] = {}
        blocked: list[str] = []  # directories left out: their contents stay too
        for entry in item.tree:
            if any(entry.path.startswith(d + "/") for d in blocked):
                continue
            top = entry is root
            dest = item.dest if top else _below(self.dst, item.dest, item.path, entry.path)
            existing = found if top else below.get(dest)
            goes = entry.kind != "other"  # pack leaves those out
            if not goes:
                batch.report.skipped.append(entry.path)
            elif existing is not None:
                try:
                    existing = batch.run(entry.path, partial(self.look, entry, dest, [existing]))
                    if top and existing is not None and entry.kind == "dir":
                        walked = batch.run(entry.path, partial(self.dst.walk, dest))
                        below = {e.path: e for e in walked}
                except _ItemFailed:
                    goes = False
                else:
                    if (
                        existing is not None
                        and entry.kind != "dir"
                        and not batch.overwrite(entry, existing)
                    ):
                        goes = False
                        if not batch.stopped:
                            batch.report.skipped.append(entry.path)
            if batch.stopped:
                return False
            if goes:
                (item.dirs if entry.kind == "dir" else item.files).append(entry.path)
                continue
            if top:
                return False
            item.clean = False
            _keep_parents(self.src, item.kept, entry.path, item.path)
            if entry.kind == "dir":
                blocked.append(entry.path)
            if entry.kind != "other":
                item.prune.append(_below(self.dst, staged, item.path, entry.path))
        return True

    # -- the steps ----------------------------------------------------------------

    def stage(self, name: str) -> bool:
        """Poll ``cancelled()``, then report step ``name``; False when the batch stopped."""
        if not self.batch.go_on():
            return False
        total = sum(len(item.files) + len(item.dirs) for item in self.items)
        self.batch.cb.progress(Progress(self.batch.op, self.items[0].path, 0, total, stage=name))
        return True

    def step[T](self, action: Callable[[], T]) -> T:
        """``action()``, a step of the whole batch, through ``batch.run``. A
        failure that stays is recorded against every item, then raised as
        _ItemFailed."""
        try:
            return self.batch.run(self.items[0].path, action)
        except _ItemFailed:
            err = self.batch.report.failed[-1][1]
            self.batch.report.failed.extend((item.path, err) for item in self.items[1:])
            raise

    def create(self, b: Backend, path: str, action: Callable[[], None]) -> None:
        """Run ``action``, a pack or unpack that creates ``path`` on ``b``: a
        temporary from then on. Unsupported falls back (nothing was created)."""
        self.temps.append((b, path))
        try:
            self.step(partial(_or_fall_back, action))
        except _Fallback:
            self.temps.remove((b, path))
            raise

    def carry(self) -> None:
        """Pack, transfer, unpack, prune and merge the settled items."""
        src, dst, items = self.src, self.dst, self.items
        if not self.stage("Packing"):
            return
        self.temp = self.step(_make_temp_dir)
        local = os.path.join(self.temp, self.name + ".tgz")
        packed = local
        if not self.upload:
            assert src.archive_dir is not None  # transfer() checked it
            packed = src.join(src.archive_dir, self.name + ".tgz")
        names = [plan.name_of(item.path) for item in items]
        self.create(src, packed, partial(src.pack, src.parent(items[0].path), names, packed))
        limit = self.remote.archive_max_bytes
        if limit is not None and (self.step(partial(src.stat, packed)).size or 0) > limit:
            raise _Fallback

        if not self.stage("Transferring"):
            return
        archive = local
        if self.upload:
            archive = dst.join(self.into, self.name + ".tgz")
            self.temps.append((dst, archive))
            self.step(partial(dst.upload, local, archive))
        else:
            self.step(partial(src.download, packed, local))

        if not self.stage("Unpacking"):
            return
        self.create(dst, self.staging, partial(dst.unpack, archive, self.staging))
        if (dst, archive) in self.temps:
            self.temps.remove((dst, archive))  # unpack removed it
        prune = [path for item in items for path in item.prune]
        if prune:
            self.step(partial(dst.delete_many, prune, recursive=True))

        if not self.stage("Merging"):
            return
        for index, item in enumerate(items):
            staged = dst.join(self.staging, plan.name_of(item.path))
            merge = partial(dst.merge, staged, item.dest)
            try:
                self.batch.run(item.path, partial(_or_fall_back, merge))
            except _Fallback:
                if index == 0:
                    raise  # nothing merged yet: the whole batch goes per file
                self.leave(items[index:])
                return
            except _ItemFailed:
                if self.batch.stopped:
                    return
                continue
            if self.move:
                self.remove_sources(item)
            elif item.clean:
                self.batch.report.done.append(item.path)
            if self.batch.stopped:
                return

    def remove_sources(self, item: _Item) -> None:
        """After ``item`` merged: delete all of it when clean, else its files
        that went, then its directories bottom-up except those still holding
        something that stayed."""
        run = self.batch.run
        try:
            if item.clean:
                run(item.path, partial(self.src.delete, item.path, recursive=True))
                self.batch.report.done.append(item.path)
                return
            if item.files:
                run(item.path, partial(self.src.delete_many, item.files))
        except _ItemFailed:
            return
        for directory in reversed(item.dirs):
            if directory in item.kept:
                continue
            try:
                run(directory, partial(self.src.delete, directory))
            except _ItemFailed:
                if self.batch.stopped:
                    return
                _keep_parents(self.src, item.kept, directory, item.path)

    def leave(self, rest: list[_Item]) -> None:
        """Leave ``rest``, the items not merged yet, to ``per_file``: drop
        what their scan put in the Report, which the per-file transfer
        records again."""
        tops = tuple(item.path for item in rest)
        below = tuple(top + "/" for top in tops)

        def theirs(path: str) -> bool:
            return path in tops or path.startswith(below)

        report = self.batch.report
        report.skipped[:] = [path for path in report.skipped if not theirs(path)]
        report.failed[:] = [(path, err) for path, err in report.failed if not theirs(path)]
        self.rest = rest

    def per_file(self) -> None:
        """Transfer ``self.rest`` per file through this batch, as the
        engine's per-file transfer does. Each item starts from the *_ALL
        conflict answer its scan started from, so that the answers already
        given replay instead of being asked again."""
        batch = self.batch
        planned: list[tuple[_Item, list[plan.WorkItem]]] = []
        for item in self.rest:
            work = partial(_plan_selected, self.src, item.path, item.dest, self.dst)
            try:
                planned.append((item, batch.run(item.path, work)))
            except _ItemFailed:
                if batch.stopped:
                    return
        total = sum(len(work_items) for _, work_items in planned)
        transfer = _Transfer(batch, self.src, self.dst, self.move, total)
        batch.conflict_all = None
        for item, work_items in planned:
            batch.conflict_all = batch.conflict_all or item.conflict_all
            transfer.selected(item.path, work_items)
            if batch.stopped:
                break

    def clean_up(self) -> None:
        """Remove every temporary; record each one that stays."""
        for b, path in reversed(self.temps):
            try:
                b.delete(path, recursive=True)
            except NotFound:
                pass
            except FsError as err:
                self.left_behind(path, b.id, err)
        if self.temp is not None:
            shutil.rmtree(self.temp, ignore_errors=True)
            if os.path.lexists(self.temp):
                self.left_behind(self.temp, (self.src if self.upload else self.dst).id, None)

    def left_behind(self, path: str, backend_id: str, cause: BaseException | None) -> None:
        err = OperationFailed(path, "temporary file left behind", backend_id, cause)
        self.batch.report.failed.append((path, err))
