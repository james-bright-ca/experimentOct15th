"""Planning for the operations engine: what an operation touches and where each
entry goes, decided before anything is written.

* ``check_selection`` and ``check_name`` refuse invalid paths and names.
* ``destinations`` resolves the ``target`` of a copy or move as the Engine
  protocol defines it.
* ``refuse_onto_self`` refuses copying or moving an item onto itself, or a
  directory into its own subtree, within one store. Paths are compared with
  their symlinks followed (and, on the local filesystem, by identity, so a
  case variant of a name on a case-insensitive filesystem is caught too).
* ``source_entry`` and ``expand`` turn one selected path into ordered work items.

Nothing here writes and nothing here asks the user: problems are raised as
FsError, and the engine decides what to do with them.
"""

from __future__ import annotations

import os
import posixpath
import stat
from collections.abc import Sequence
from dataclasses import dataclass

from twinfm.backend.base import (
    Backend,
    Cap,
    Entry,
    FsError,
    NotADirectory,
    NotFound,
    OperationFailed,
)
from twinfm.backend.local import LocalBackend

__all__ = [
    "WorkItem",
    "check_name",
    "check_selection",
    "destinations",
    "expand",
    "inside",
    "is_dir",
    "name_of",
    "refuse_onto_self",
    "same_entry",
    "source_entry",
    "stat_or_none",
]

_MAX_LINKS = 40


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One unit of work: a source entry and the full destination path it goes to.

    whole
        The entry is copied or moved with one backend call (``copy_within`` or
        ``rename``), a directory as a whole tree: same store, and nothing at
        ``dest`` (or ``dest`` is the entry itself, under another case of its
        name). Otherwise a directory is created or merged at ``dest`` and its
        contents follow as work items of their own.
    """

    entry: Entry
    dest: str
    whole: bool = False


# -- names and paths ----------------------------------------------------------------


def name_of(path: str) -> str:
    """The last component of a normalised path (``""`` for ``/``)."""
    return posixpath.basename(path)


def check_name(b: Backend, parent: str, name: str) -> None:
    """Raise OperationFailed unless ``name`` is a single usable path component."""
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise OperationFailed(parent, f"invalid name {name!r}", b.id)


def check_selection(b: Backend, paths: Sequence[str]) -> None:
    """Raise OperationFailed for a selected path that is not an absolute path
    of an entry (``/`` itself, or one ending in ``.`` or ``..``)."""
    for path in paths:
        if not path.startswith("/"):
            raise OperationFailed(path, "not an absolute path", b.id)
        check_name(b, b.parent(path), name_of(path))


def is_dir(entry: Entry) -> bool:
    """Whether ``entry`` is a directory or a symlink that resolves to one."""
    return entry.kind == "dir" or (entry.kind == "symlink" and entry.target_kind == "dir")


def stat_or_none(b: Backend, path: str) -> Entry | None:
    """``b.stat(path)``, or None when nothing is there. Other errors propagate."""
    try:
        return b.stat(path)
    except NotFound:
        return None


def _text_inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part and part != "."]


# -- following symlinks -------------------------------------------------------------


def _stat_quiet(b: Backend, path: str) -> Entry | None:
    try:
        return b.stat(path)
    except FsError:
        return None


def _real(b: Backend, path: str, follow_last: bool = True) -> str:
    """``path`` with its symlinks followed through ``b.stat``; from the first
    component that cannot be looked up on, the rest stays as written."""
    if not follow_last:
        return posixpath.join(_real(b, b.parent(path)), name_of(path))
    todo = _parts(path)
    real: list[str] = []
    links = 0
    while todo:
        name = todo.pop(0)
        if name == "..":  # only from link text; the path itself is normalised
            if real:
                real.pop()
            continue
        entry = _stat_quiet(b, "/" + "/".join([*real, name]))
        if entry is None:
            return posixpath.normpath("/" + "/".join([*real, name, *todo]))
        if entry.kind == "symlink" and entry.link_target and links < _MAX_LINKS:
            links += 1
            if entry.link_target.startswith("/"):
                real = []
            todo = _parts(entry.link_target) + todo
            continue
        real.append(name)
    return "/" + "/".join(real)


def _resolve_pair(b: Backend, x: str, y: str, follow_y_last: bool = True) -> tuple[str, str]:
    """``x`` and ``y`` with their symlinks followed (``y``'s last component
    only if ``follow_y_last``), as far as ``b`` shows them.

    Both paths resolve alike up to their common prefix. When no symlink
    appears after it, the paths compare as they are written, and they are
    returned as they are: this keeps lookups to the few components after
    the prefix, which matters on a remote backend.
    """
    if Cap.STAT not in b.caps:
        return x, y
    xs, ys = _parts(x), _parts(y)
    common = 0
    while common < min(len(xs), len(ys)) and xs[common] == ys[common]:
        common += 1
    for parts, last in ((xs, len(xs)), (ys, len(ys) if follow_y_last else len(ys) - 1)):
        for i in range(common + 1, last + 1):
            entry = _stat_quiet(b, "/" + "/".join(parts[:i]))
            if entry is None:
                break
            if entry.kind == "symlink":
                return _real(b, x), _real(b, y, follow_y_last)
    return x, y


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except (OSError, ValueError):
        return None


def same_entry(b: Backend, a: str, c: str) -> bool:
    """Whether paths ``a`` and ``c`` name one directory entry on ``b``: the
    same path, the same path reached through a symlinked directory, or (on
    the local filesystem) a case variant on a case-insensitive filesystem.
    A hard link is a separate entry."""
    if a == c:
        return True
    if isinstance(b, LocalBackend):
        sa, sc = _lstat(a), _lstat(c)
        return (
            sa is not None
            and sc is not None
            and (sa.st_dev, sa.st_ino) == (sc.st_dev, sc.st_ino)
            and (stat.S_ISDIR(sa.st_mode) or sa.st_nlink == 1)
        )
    if name_of(a) != name_of(c):
        return False
    pa, pc = _resolve_pair(b, b.parent(a), b.parent(c))
    return pa == pc


def inside(b: Backend, path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or lies below it, with symlinks followed
    (``root``'s own last component is not followed: a symlink has nothing
    below it). The textual relation alone already counts."""
    if _text_inside(path, root):
        return True
    if isinstance(b, LocalBackend):
        root_st = _lstat(root)
        if root_st is None or not stat.S_ISDIR(root_st.st_mode):
            return False
        try:
            probe = os.path.realpath(path)
        except (OSError, ValueError):
            return False
        while True:
            st = _lstat(probe)
            if st is not None and (st.st_dev, st.st_ino) == (root_st.st_dev, root_st.st_ino):
                return True
            if probe == "/":
                return False
            probe = posixpath.dirname(probe)
    real_path, real_root = _resolve_pair(b, path, root, follow_y_last=False)
    return _text_inside(real_path, real_root)


# -- whole-operation decisions --------------------------------------------------------


def destinations(src: Backend, paths: Sequence[str], dst: Backend, target: str) -> list[str]:
    """The full destination path on ``dst`` of each path in ``paths``.

    An existing directory (or a symlink to one) at ``target`` receives each
    item under its own name. Otherwise, with exactly one item, ``target`` is
    its new full path; with more, NotADirectory is raised. When ``dst`` lacks
    Cap.STAT, ``target`` is taken to be an existing directory. A target that
    is the single item itself under another case of its name is its new
    name, not a directory to move into.
    """
    into = True
    if Cap.STAT in dst.caps:
        existing = stat_or_none(dst, target)
        into = existing is not None and is_dir(existing)
        if (
            into
            and src is dst
            and len(paths) == 1
            and paths[0] != target
            and same_entry(dst, paths[0], target)
        ):
            into = False
    if into:
        return [dst.join(target, name_of(path)) for path in paths]
    if len(paths) != 1:
        raise NotADirectory(target, "the target for several items must be a directory", dst.id)
    return [target]


def refuse_onto_self(b: Backend, paths: Sequence[str], dests: Sequence[str], move: bool) -> None:
    """Within one store, raise OperationFailed (before anything is written)
    when an item would land on itself or a directory inside its own subtree.
    A move to another case of the item's own name is a rename in place and
    is allowed."""
    verb = "move" if move else "copy"
    for path, dest in zip(paths, dests, strict=True):
        if same_entry(b, path, dest):
            if move and dest != path:
                continue
            raise OperationFailed(dest, f"cannot {verb} an item onto itself", b.id)
        if inside(b, dest, path):
            raise OperationFailed(dest, f"cannot {verb} a directory into itself", b.id)


# -- per-item planning --------------------------------------------------------------


def source_entry(b: Backend, path: str) -> Entry:
    """The Entry of the selected ``path``: its lstat, or, when ``b`` lacks
    Cap.STAT, a file of unknown size and time (all such a backend can do with
    it is download it)."""
    if Cap.STAT not in b.caps:
        return Entry(name_of(path), path, "file", None, None, None)
    return b.stat(path)


def expand(src: Backend, entry: Entry, dest: str, dst: Backend) -> list[WorkItem]:
    """The work items for one selected ``entry`` going to ``dest``, in order:
    the entry first, then, for a directory handled item by item, every entry
    below it (``src.walk``), each directory before its contents. ``.`` and
    ``..`` never appear.

    Within one store, an entry whose destination is free (or unknown, without
    Cap.STAT) is one ``whole`` item. A directory that ``dst`` cannot create
    (no Cap.MKDIR, and not already a directory there) is not walked: it stays
    a single item, which then fails. Raises the backends' FsError.
    """
    same = src is dst
    existing: Entry | None = None
    if Cap.STAT in dst.caps and (same or (entry.kind == "dir" and Cap.MKDIR not in dst.caps)):
        existing = stat_or_none(dst, dest)
    if same and (existing is None or same_entry(dst, entry.path, dest)):
        return [WorkItem(entry, dest, whole=True)]
    items = [WorkItem(entry, dest)]
    if entry.kind != "dir":
        return items
    if Cap.MKDIR not in dst.caps and not (existing is not None and is_dir(existing)):
        return items
    for below in src.walk(entry.path):
        target = dest
        for part in _parts(below.path[len(entry.path) :]):
            target = dst.join(target, part)
        items.append(WorkItem(below, target))
    return items
