"""MemoryBackend: an in-RAM tree, a second backend type so that cross-backend
code paths (and backends with reduced capabilities) can be exercised without a
remote."""

from __future__ import annotations

import os
import posixpath
import stat
import threading
import time
from collections.abc import Iterable

from twinfm.backend.base import (
    ALL_CAPS,
    AlreadyExists,
    Backend,
    Cap,
    DirectoryNotEmpty,
    Entry,
    FsError,
    IsADirectory,
    Kind,
    NotADirectory,
    NotFound,
    OperationFailed,
    from_oserror,
)
from twinfm.backend.local import install_file

__all__ = ["MemoryBackend"]

_MAX_LINKS = 40


class _Node:
    __slots__ = ("children", "data", "kind", "mode", "mtime", "target")

    def __init__(
        self,
        kind: Kind,
        *,
        data: bytes = b"",
        target: str | None = None,
        mtime: float | None = None,
        mode: int | None = None,
    ) -> None:
        self.kind: Kind = kind
        self.data = data
        self.target = target
        self.children: dict[str, _Node] = {}
        self.mtime = time.time() if mtime is None else mtime
        if mode is None:
            mode = {"dir": 0o755, "symlink": 0o777}.get(kind, 0o644)
        self.mode = mode

    def clone(self) -> _Node:
        copy = _Node(self.kind, data=self.data, target=self.target, mtime=self.mtime, mode=self.mode)
        copy.children = {name: child.clone() for name, child in self.children.items()}
        return copy


def _components(path: str) -> list[str]:
    return [part for part in path.split("/") if part and part != "."]


class MemoryBackend(Backend):
    """An in-RAM POSIX-like tree (directories, files, symlinks) with lstat
    semantics, rooted at ``/``.

    ``caps`` (default: every Cap) is fixed at construction; a method whose
    capability is missing raises Unsupported and changes nothing. ``home`` is
    created if missing. ``upload`` and ``download`` read and write real local
    files. Thread-safe: every method holds one lock.

    The ``add_*``, ``read_file`` and ``snapshot`` helpers are not part of the
    Backend contract: they seed and inspect the tree for tests and ignore caps.
    """

    id = "memory"

    def __init__(
        self, caps: Iterable[Cap] = ALL_CAPS, *, home: str = "/", label: str = "memory"
    ) -> None:
        self.caps = frozenset(caps)
        self.label = label
        self._home = home
        self._root = _Node("dir")
        self._lock = threading.RLock()
        self._make_dirs(home)

    # -- tree internals -----------------------------------------------------------

    def _resolve(self, path: str, follow_last: bool) -> tuple[list[str], _Node | None, _Node | None]:
        """Walk ``path`` from the root, following symlinks in the middle (and
        at the end if ``follow_last``).

        Returns (real components, parent directory node, node). ``node`` is
        None when only the last component is missing; the parent is None only
        for the root. Raises NotFound or NotADirectory when a directory leading
        to the last component is missing or is not a directory.
        """
        todo = _components(path)
        real: list[str] = []
        nodes: list[_Node] = [self._root]
        links = 0
        while todo:
            name = todo.pop(0)
            if name == "..":
                if real:
                    real.pop()
                    nodes.pop()
                continue
            parent = nodes[-1]
            if parent.kind != "dir":
                raise NotADirectory(path, backend_id=self.id)
            child = parent.children.get(name)
            last = not todo
            if child is None:
                if not last:
                    raise NotFound(path, backend_id=self.id)
                return [*real, name], parent, None
            if child.kind == "symlink" and (follow_last or not last):
                links += 1
                if links > _MAX_LINKS:
                    raise OperationFailed(path, "too many levels of symbolic links", self.id)
                target = child.target or ""
                if target.startswith("/"):
                    real, nodes = [], [self._root]
                todo = _components(target) + todo
                continue
            real.append(name)
            nodes.append(child)
        return real, (nodes[-2] if len(nodes) > 1 else None), nodes[-1]

    def _existing(self, path: str, follow_last: bool) -> tuple[list[str], _Node | None, _Node]:
        real, parent, node = self._resolve(path, follow_last)
        if node is None:
            raise NotFound(path, backend_id=self.id)
        return real, parent, node

    def _entry(self, path: str, name: str, node: _Node) -> Entry:
        size: int | None = None
        link_target: str | None = None
        target_kind: Kind | None = None
        if node.kind == "file":
            size = len(node.data)
        elif node.kind == "symlink":
            link_target = node.target
            size = len((node.target or "").encode())
            try:
                target = self._resolve(path, True)[2]
            except FsError:
                target = None
            target_kind = target.kind if target is not None else None
        return Entry(name, path, node.kind, size, node.mtime, node.mode, link_target, target_kind)

    def _check_replace(self, snode: _Node, dst: str, dnode: _Node, overwrite: bool) -> None:
        if not overwrite:
            raise AlreadyExists(dst, backend_id=self.id)
        if dnode.kind == "dir":
            raise IsADirectory(dst, "will not replace a directory", self.id)
        if snode.kind == "dir":
            raise NotADirectory(dst, "will not replace a file with a directory", self.id)

    def _make_dirs(self, path: str) -> _Node:
        parts = _components(path)
        node = self._root
        for i in range(1, len(parts) + 1):
            prefix = "/" + "/".join(parts[:i])
            real, parent, found = self._resolve(prefix, True)
            if found is None:
                assert parent is not None
                found = parent.children[real[-1]] = _Node("dir")
            elif found.kind != "dir":
                raise NotADirectory(prefix, backend_id=self.id)
            node = found
        return node

    def _read_local(self, local_src: str) -> bytes:
        try:
            st = os.stat(local_src)
            if stat.S_ISDIR(st.st_mode):
                raise IsADirectory(local_src, backend_id=self.id)
            if not stat.S_ISREG(st.st_mode):
                raise OperationFailed(local_src, "not a regular file", self.id)
            with open(local_src, "rb") as f:
                return f.read()
        except OSError as e:
            raise from_oserror(e, local_src, self.id) from e
        except ValueError as e:
            raise OperationFailed(local_src, str(e), self.id, e) from e

    # -- path algebra -------------------------------------------------------------

    def home(self) -> str:
        return self._home

    # -- queries ------------------------------------------------------------------

    def list_dir(self, path: str) -> list[Entry]:
        self.require(Cap.LIST, path)
        with self._lock:
            _, _, node = self._existing(path, True)
            if node.kind != "dir":
                raise NotADirectory(path, backend_id=self.id)
            return [
                self._entry(self.join(path, name), name, child)
                for name, child in node.children.items()
            ]

    def stat(self, path: str) -> Entry:
        self.require(Cap.STAT, path)
        with self._lock:
            _, _, node = self._existing(path, False)
            return self._entry(path, posixpath.basename(path.rstrip("/")) or "/", node)

    # -- same-backend mutations ---------------------------------------------------

    def mkdir(self, path: str) -> None:
        self.require(Cap.MKDIR, path)
        with self._lock:
            real, parent, node = self._resolve(path, False)
            if node is not None:
                raise AlreadyExists(path, backend_id=self.id)
            assert parent is not None
            parent.children[real[-1]] = _Node("dir")

    def delete(self, path: str, recursive: bool = False) -> None:
        self.require(Cap.DELETE, path)
        with self._lock:
            real, parent, node = self._existing(path, False)
            if parent is None:
                raise OperationFailed(path, "refusing to delete the root directory", self.id)
            if node.kind == "dir" and node.children and not recursive:
                raise DirectoryNotEmpty(path, backend_id=self.id)
            del parent.children[real[-1]]

    def rename(self, src: str, dst: str, overwrite: bool = False) -> None:
        self.require(Cap.RENAME, src)
        with self._lock:
            sreal, sparent, snode = self._existing(src, False)
            dreal, dparent, dnode = self._resolve(dst, False)
            if dnode is snode:
                return
            if dnode is not None:
                self._check_replace(snode, dst, dnode, overwrite)
            if sparent is None or (snode.kind == "dir" and dreal[: len(sreal)] == sreal):
                raise OperationFailed(dst, "cannot move a directory into itself", self.id)
            assert dparent is not None
            del sparent.children[sreal[-1]]
            dparent.children[dreal[-1]] = snode

    def copy_within(self, src: str, dst: str, overwrite: bool = False) -> None:
        self.require(Cap.COPY_WITHIN, src)
        with self._lock:
            sreal, _, snode = self._existing(src, False)
            dreal, dparent, dnode = self._resolve(dst, False)
            if dnode is not None:
                self._check_replace(snode, dst, dnode, overwrite)
            if snode.kind == "dir" and dreal[: len(sreal)] == sreal:
                raise OperationFailed(dst, "cannot copy a directory into itself", self.id)
            assert dparent is not None
            dparent.children[dreal[-1]] = snode.clone()

    # -- cross-backend primitives -------------------------------------------------

    def download(self, path: str, local_dest: str) -> None:
        self.require(Cap.DOWNLOAD, path)
        with self._lock:
            _, _, node = self._existing(path, True)
            if node.kind == "dir":
                raise IsADirectory(path, backend_id=self.id)
            data = node.data
        if os.path.lexists(local_dest):
            raise AlreadyExists(local_dest, backend_id=self.id)
        try:
            install_file(local_dest, lambda f: f.write(data))
        except OSError as e:
            raise from_oserror(e, local_dest, self.id) from e
        except ValueError as e:
            raise OperationFailed(local_dest, str(e), self.id, e) from e

    def upload(self, local_src: str, path: str, overwrite: bool = False) -> None:
        self.require(Cap.UPLOAD, path)
        data = self._read_local(local_src)
        with self._lock:
            real, parent, node = self._resolve(path, False)
            if node is not None:
                if not overwrite:
                    raise AlreadyExists(path, backend_id=self.id)
                if node.kind == "dir":
                    raise IsADirectory(path, "will not replace a directory", self.id)
            assert parent is not None
            parent.children[real[-1]] = _Node("file", data=data)

    # -- seeding and inspection helpers (not part of the contract; ignore caps) ----

    def add_dir(self, path: str) -> None:
        """Create directory ``path`` and any missing parents, like ``mkdir -p``."""
        with self._lock:
            self._make_dirs(path)

    def add_file(
        self, path: str, data: bytes = b"", *, mtime: float | None = None, mode: int = 0o644
    ) -> None:
        """Create or replace file ``path`` holding ``data``; missing parents are created."""
        with self._lock:
            parent = self._make_dirs(self.parent(path))
            name = posixpath.basename(path)
            existing = parent.children.get(name)
            if existing is not None and existing.kind == "dir":
                raise IsADirectory(path, backend_id=self.id)
            parent.children[name] = _Node("file", data=bytes(data), mtime=mtime, mode=mode)

    def add_symlink(self, path: str, target: str) -> None:
        """Create or replace symlink ``path`` -> ``target`` (which need not
        exist); missing parents are created."""
        with self._lock:
            parent = self._make_dirs(self.parent(path))
            name = posixpath.basename(path)
            existing = parent.children.get(name)
            if existing is not None and existing.kind == "dir":
                raise IsADirectory(path, backend_id=self.id)
            parent.children[name] = _Node("symlink", target=target)

    def read_file(self, path: str) -> bytes:
        """The content of file ``path`` (symlinks followed). NotFound / IsADirectory."""
        with self._lock:
            _, _, node = self._existing(path, True)
            if node.kind == "dir":
                raise IsADirectory(path, backend_id=self.id)
            return node.data

    def snapshot(self) -> dict[str, bytes | str | None]:
        """Every path below ``/``: bytes for a file, the link text (str) for a
        symlink, None for a directory. Compare two snapshots to assert that
        an operation changed nothing."""
        result: dict[str, bytes | str | None] = {}
        with self._lock:
            stack: list[tuple[str, _Node]] = [("/", self._root)]
            while stack:
                prefix, node = stack.pop()
                for name, child in node.children.items():
                    path = posixpath.join(prefix, name)
                    if child.kind == "dir":
                        result[path] = None
                        stack.append((path, child))
                    elif child.kind == "symlink":
                        result[path] = child.target
                    else:
                        result[path] = child.data
        return result
