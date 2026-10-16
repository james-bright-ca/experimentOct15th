"""LocalBackend: the local filesystem, through ``os`` with lstat semantics."""

from __future__ import annotations

import errno
import os
import posixpath
import secrets
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import BinaryIO

from twinfm.backend.base import (
    ALL_CAPS,
    AlreadyExists,
    Backend,
    Entry,
    IsADirectory,
    Kind,
    NotADirectory,
    NotFound,
    OperationFailed,
    from_oserror,
)

__all__ = ["LocalBackend", "install_file"]

_TMP_SUFFIX = ".twinfm-tmp"
_CHUNK = 1 << 20


def _temp_prefix(dest: str) -> tuple[str, str]:
    """(directory, name prefix) for a hidden temporary sibling of ``dest``."""
    parent, name = posixpath.split(dest)
    # Keep the temporary name within NAME_MAX even for a very long ``name``.
    return parent or ".", f".{name[:100]}."


def install_file(
    dest: str,
    fill: Callable[[BinaryIO], object],
    *,
    mode: int | None = None,
    times_ns: tuple[int, int] | None = None,
) -> None:
    """Write a local file at ``dest`` so that it appears complete or not at all.

    A hidden temporary sibling is created (permissions 0o666 minus the umask),
    ``fill`` writes the content to it, then ``mode`` and ``times_ns``
    (atime, mtime in ns) are applied if given and ``os.replace`` moves it over
    ``dest``. The temporary file is removed if anything fails. Whether ``dest``
    may be replaced is the caller's decision. Raises ``OSError``.
    """
    parent, prefix = _temp_prefix(dest)
    while True:
        tmp = posixpath.join(parent, f"{prefix}{secrets.token_hex(6)}{_TMP_SUFFIX}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(fd, "wb") as f:
            fill(f)
        if mode is not None:
            os.chmod(tmp, mode)
        if times_ns is not None:
            os.utime(tmp, ns=times_ns)
        os.replace(tmp, dest)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _kind(mode: int) -> Kind:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _inside(path: str, directory: os.stat_result) -> bool:
    """Whether ``path`` would lie in ``directory`` (its lstat result) or below
    it: whether the directory holding ``path``, symlinks followed as the OS
    would, is ``directory`` or one of its descendants. Directories are compared
    by identity, not by name, so neither a symlink nor a case variant of a name
    can disguise a subtree. An ancestor that cannot be reached is skipped: the
    operation itself then fails on it before writing anything."""
    parent = os.path.realpath(posixpath.dirname(path))
    while True:
        try:
            if os.path.samestat(os.stat(parent), directory):
                return True
        except OSError:
            pass
        if parent == "/":
            return False
        parent = posixpath.dirname(parent)


def _same_entry(src: str, a: os.stat_result, dst: str, b: os.stat_result) -> bool:
    """Whether ``src`` and ``dst`` (lstat results ``a`` and ``b``) are one
    directory entry: the same name, perhaps spelt through a symlink in the
    middle, or a case variant of it on a case-insensitive filesystem. Hard
    links share an inode but are separate entries, so for a file with several
    links the directory and the names decide. Raises OSError."""
    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
        return False
    if stat.S_ISDIR(a.st_mode) or a.st_nlink == 1:
        return True
    sdir, sname = posixpath.split(src)
    ddir, dname = posixpath.split(dst)
    if not os.path.samefile(sdir, ddir):
        return False
    if sname == dname:
        return True
    if {sname, dname} <= set(os.listdir(sdir)):
        return False
    return _one_name(sdir, sname, dname)


def _one_name(directory: str, a: str, b: str) -> bool:
    """Whether the different names ``a`` and ``b`` name one entry in
    ``directory`` (a case or Unicode-normalisation variant). The filesystem
    answers, in a scratch directory made inside ``directory`` and so under its
    name rules: ``a`` is created there, then ``b`` is looked up. Raises OSError."""
    scratch = tempfile.mkdtemp(prefix=".twinfm-probe.", suffix=_TMP_SUFFIX, dir=directory)
    try:
        os.mkdir(posixpath.join(scratch, a))
        try:
            os.lstat(posixpath.join(scratch, b))
        except FileNotFoundError:
            return False
        return True
    finally:
        shutil.rmtree(scratch)


class LocalBackend(Backend):
    """The local filesystem. Stateless and thread-safe; offers every Cap.

    Errors are mapped from ``errno`` (see ``base.from_oserror``). Files written
    by copy_within, upload and download go through a temporary sibling and
    ``os.replace``, and keep the source's permission bits and times; a copied
    directory tree is built in a temporary sibling directory and moved into
    place. ``rename`` falls back to copy-then-delete across filesystems (EXDEV).
    """

    id = "local"
    label = "local"
    caps = ALL_CAPS

    @contextmanager
    def _errors(self, path: str) -> Iterator[None]:
        """Re-raise I/O failures inside the block as FsError about ``path``."""
        try:
            yield
        except OSError as e:
            raise from_oserror(e, path, self.id) from e
        except ValueError as e:  # e.g. a NUL byte in a path
            raise OperationFailed(path, str(e), self.id, e) from e

    def _lstat_or_none(self, path: str) -> os.stat_result | None:
        with self._errors(path):
            try:
                return os.lstat(path)
            except FileNotFoundError:
                return None

    def _entry(self, path: str, name: str, st: os.stat_result) -> Entry:
        kind = _kind(st.st_mode)
        link_target: str | None = None
        target_kind: Kind | None = None
        if kind == "symlink":
            link_target = os.readlink(path)
            try:
                target_kind = _kind(os.stat(path).st_mode)
            except OSError:
                target_kind = None  # dangling, looping or unreachable
        size = st.st_size if kind in ("file", "symlink") else None
        return Entry(
            name=name,
            path=path,
            kind=kind,
            size=size,
            mtime=st.st_mtime,
            mode=stat.S_IMODE(st.st_mode),
            link_target=link_target,
            target_kind=target_kind,
        )

    def _check_replace(
        self, sst: os.stat_result, dst: str, dst_st: os.stat_result, overwrite: bool
    ) -> None:
        if not overwrite:
            raise AlreadyExists(dst, backend_id=self.id)
        if stat.S_ISDIR(dst_st.st_mode):
            raise IsADirectory(dst, "will not replace a directory", self.id)
        if stat.S_ISDIR(sst.st_mode):
            raise NotADirectory(dst, "will not replace a file with a directory", self.id)

    def _check_regular_file(self, path: str) -> None:
        """Following symlinks, ``path`` must be a regular file."""
        with self._errors(path):
            st = os.stat(path)
        if stat.S_ISDIR(st.st_mode):
            raise IsADirectory(path, backend_id=self.id)
        if not stat.S_ISREG(st.st_mode):
            raise OperationFailed(path, "not a regular file", self.id)

    def _copy_file(self, src: str, dst: str) -> None:
        """Copy regular file ``src`` (symlinks followed) to ``dst``, atomically."""
        with open(src, "rb") as fsrc:
            st = os.fstat(fsrc.fileno())
            install_file(
                dst,
                lambda f: shutil.copyfileobj(fsrc, f, _CHUNK),
                mode=stat.S_IMODE(st.st_mode),
                times_ns=(st.st_atime_ns, st.st_mtime_ns),
            )

    def _copy_entry(self, src: str, sst: os.stat_result, dst: str) -> None:
        """Copy ``src`` (described by lstat ``sst``) to ``dst`` through a
        temporary sibling: a link as a link, a directory as a whole tree."""
        if stat.S_ISREG(sst.st_mode):
            self._copy_file(src, dst)
            return
        parent, prefix = _temp_prefix(dst)
        if stat.S_ISLNK(sst.st_mode):
            target = os.readlink(src)
            tmp = posixpath.join(parent, f"{prefix}{secrets.token_hex(6)}{_TMP_SUFFIX}")
            os.symlink(target, tmp)
            try:
                os.replace(tmp, dst)
            except BaseException:
                with suppress(OSError):
                    os.unlink(tmp)
                raise
            return
        tmp = tempfile.mkdtemp(prefix=prefix, suffix=_TMP_SUFFIX, dir=parent)
        try:
            shutil.copytree(src, tmp, symlinks=True, dirs_exist_ok=True)
            os.replace(tmp, dst)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _check_copyable(self, src: str, sst: os.stat_result) -> None:
        if not (
            stat.S_ISREG(sst.st_mode) or stat.S_ISDIR(sst.st_mode) or stat.S_ISLNK(sst.st_mode)
        ):
            raise OperationFailed(src, "cannot copy a special file", self.id)

    # -- path algebra -------------------------------------------------------------

    def home(self) -> str:
        return posixpath.normpath(os.path.expanduser("~"))

    # -- queries ------------------------------------------------------------------

    def list_dir(self, path: str) -> list[Entry]:
        entries: list[Entry] = []
        with self._errors(path):
            with os.scandir(path) as it:
                for de in it:
                    try:
                        st = de.stat(follow_symlinks=False)
                        entries.append(self._entry(self.join(path, de.name), de.name, st))
                    except FileNotFoundError:
                        continue  # removed while we were listing
        return entries

    def stat(self, path: str) -> Entry:
        with self._errors(path):
            st = os.lstat(path)
            return self._entry(path, posixpath.basename(path.rstrip("/")) or "/", st)

    # -- same-backend mutations ---------------------------------------------------

    def mkdir(self, path: str) -> None:
        with self._errors(path):
            try:
                os.mkdir(path)
            except FileNotFoundError as e:
                raise NotFound(path, "parent directory does not exist", self.id, e) from e

    def delete(self, path: str, recursive: bool = False) -> None:
        # A trailing "/" would make lstat follow a symlink to a directory.
        path = path.rstrip("/") or "/"
        if path == "/":
            raise OperationFailed(path, "refusing to delete the root directory", self.id)
        with self._errors(path):
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(path)
            elif recursive:
                shutil.rmtree(path)
            else:
                os.rmdir(path)

    def rename(self, src: str, dst: str, overwrite: bool = False) -> None:
        with self._errors(src):
            sst = os.lstat(src)
        dst_st = self._lstat_or_none(dst)
        if dst_st is not None:
            with self._errors(src):
                same_entry = _same_entry(src, sst, dst, dst_st)
            if same_entry:
                if src == dst:
                    return
            else:
                self._check_replace(sst, dst, dst_st, overwrite)
                with self._errors(src):
                    if os.path.samestat(sst, dst_st):
                        # rename(2) does nothing when both names link one file:
                        # replacing dst with src is removing the name src.
                        os.unlink(src)
                        return
        if stat.S_ISDIR(sst.st_mode) and _inside(dst, sst):
            raise OperationFailed(dst, "cannot move a directory into itself", self.id)
        try:
            os.rename(src, dst)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise from_oserror(e, dst if e.errno == errno.ENOENT else src, self.id) from e
        except ValueError as e:
            raise OperationFailed(src, str(e), self.id, e) from e
        else:
            return
        # Different filesystems: copy, then remove the source. Uses no os.rename.
        with self._errors(dst):
            self._check_copyable(src, sst)
            self._copy_entry(src, sst, dst)
        with self._errors(src):
            if stat.S_ISDIR(sst.st_mode):
                shutil.rmtree(src)
            else:
                os.unlink(src)

    def copy_within(self, src: str, dst: str, overwrite: bool = False) -> None:
        with self._errors(src):
            sst = os.lstat(src)
        self._check_copyable(src, sst)
        dst_st = self._lstat_or_none(dst)
        if dst_st is not None:
            self._check_replace(sst, dst, dst_st, overwrite)
        if stat.S_ISDIR(sst.st_mode) and _inside(dst, sst):
            raise OperationFailed(dst, "cannot copy a directory into itself", self.id)
        with self._errors(dst):
            self._copy_entry(src, sst, dst)

    # -- cross-backend primitives -------------------------------------------------

    def download(self, path: str, local_dest: str) -> None:
        self._check_regular_file(path)
        if os.path.lexists(local_dest):
            raise AlreadyExists(local_dest, backend_id=self.id)
        with self._errors(local_dest):
            self._copy_file(path, local_dest)

    def upload(self, local_src: str, path: str, overwrite: bool = False) -> None:
        self._check_regular_file(local_src)
        dst_st = self._lstat_or_none(path)
        if dst_st is not None:
            if not overwrite:
                raise AlreadyExists(path, backend_id=self.id)
            if stat.S_ISDIR(dst_st.st_mode):
                raise IsADirectory(path, "will not replace a directory", self.id)
        with self._errors(path):
            self._copy_file(local_src, path)
