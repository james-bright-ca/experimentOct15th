"""The backend contract: what every file store (local disk, RAM, btty remote) offers.

Paths
    Plain ``str``, absolute POSIX paths owned by their backend (a remote path is
    not a local one, so ``pathlib.Path`` is never used in the contract). Callers
    pass normalised paths: they start with ``/``, have no trailing ``/`` (except
    the root ``/`` itself) and no ``.``, ``..`` or empty components; use
    ``posixpath.normpath`` on anything typed by a user. A location is the pair
    ``(backend, path)``. Two locations are on the same store exactly when their
    backend objects are identical (``src is dst``); the registry hands out one
    shared LocalBackend so that two local panels share it.

Semantics
    * The interface is synchronous. Methods are called from worker threads,
      possibly concurrently, so implementations must be thread-safe.
    * lstat semantics: an Entry describes the path itself; a symlink is reported
      as kind ``"symlink"``, never as what it points to. Mutations never follow
      a symlink at the final path component (delete removes the link, rename
      and copy_within move or copy the link itself). Symlinks in the middle of a
      path are followed as the operating system would.
    * Nothing is overwritten unless ``overwrite=True``, and even then a
      directory is never replaced and a file is never replaced by a directory.
    * A method whose capability (``Cap``) is absent from ``caps`` raises
      ``Unsupported`` before doing anything.
    * Backends raise only ``FsError`` subclasses. No raw ``OSError`` (or any
      other exception from the I/O layer) escapes a backend method.
    * A backend may cache ``list_dir`` results (BttyBackend does; LocalBackend
      and MemoryBackend do not). Only ``list_dir`` may answer from a cache:
      ``stat``, ``exists`` and ``walk`` always ask the store, because the
      engine's safety checks rest on them (so a caching backend overrides
      ``walk``). A caching backend invalidates itself on its own mutations,
      in a ``finally`` so that a call that raised (it may still have changed
      something) invalidates too, dropping at least these listings ("the
      subtree of p" is p and every directory below it)::

        mkdir(p)                    the parent of p
        delete(p), delete_many(ps)  the parent and the subtree of each path
        rename(s, d)                the parents and the subtrees of s and d
        copy_within(s, d)           the parent and the subtree of d
        upload(local, p)            the parent of p
        pack(parent, names, a)      the parent of a
        unpack(a, dest)             the parent of a; the parent and the subtree of dest
        merge(s, d)                 the parents and the subtrees of s and d

      It never stores a listing that started before an invalidation and
      finished after it, and every call returns a new list, so changing a
      returned list changes nothing cached. ``invalidate`` drops listings on
      request (Ctrl-R). Changes made to the store by anything else (an
      editor, git, a build) stay invisible until then, and so does a change
      made through another path to a directory reached through a symlink.
"""

from __future__ import annotations

import errno
import posixpath
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal

__all__ = [
    "ALL_CAPS",
    "AlreadyExists",
    "Backend",
    "BackendUnavailable",
    "Cap",
    "DirectoryNotEmpty",
    "Entry",
    "FsError",
    "IsADirectory",
    "Kind",
    "NotADirectory",
    "NotFound",
    "OperationFailed",
    "PermissionDenied",
    "Unsupported",
    "from_oserror",
]

Kind = Literal["file", "dir", "symlink", "other"]
"""What a path is, by lstat: a regular file, a directory, a symbolic link, or
anything else (fifo, socket, device)."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One path on a backend, described with lstat semantics.

    name
        The last path component (``"/"`` for the root).
    path
        The absolute path the entry was reached by. For ``list_dir(d)`` and
        ``walk(d)`` this is ``join(d, name)`` under the path that was asked
        for, even when ``d`` itself was reached through a symlink.
    kind
        ``"file"``, ``"dir"``, ``"symlink"`` or ``"other"``. A symlink is always
        ``"symlink"``, whatever it points to.
    size
        Bytes: the content length of a ``"file"``, the length of the link text
        of a ``"symlink"`` (as lstat reports it), ``None`` for ``"dir"`` and
        ``"other"``. ``None`` whenever the backend cannot know it.
    mtime
        Modification time as POSIX seconds, or ``None`` when unknown.
    mode
        Permission bits only (``stat.S_IMODE``, e.g. ``0o644``), or ``None``
        when unknown. The file type is ``kind``, not part of ``mode``.
    link_target
        For a symlink, the link text exactly as stored (it may be relative or
        dangling). ``None`` for every other kind.
    target_kind
        For a symlink, the kind of what it finally resolves to after following
        every link: ``"file"``, ``"dir"`` or ``"other"`` (never ``"symlink"``).
        ``None`` for a dangling or looping link, when unknown, and for every
        kind other than ``"symlink"``.
    """

    name: str
    path: str
    kind: Kind
    size: int | None
    mtime: float | None
    mode: int | None
    link_target: str | None = None
    target_kind: Kind | None = None


class Cap(Enum):
    """A capability. ``Backend.caps`` holds the ones a backend instance offers;
    each method names the capability it needs."""

    LIST = "list"  # list_dir, walk
    STAT = "stat"  # stat, exists
    MKDIR = "mkdir"  # mkdir
    DELETE = "delete"  # delete
    RENAME = "rename"  # rename
    COPY_WITHIN = "copy_within"  # copy_within
    UPLOAD = "upload"  # upload
    DOWNLOAD = "download"  # download
    ARCHIVE = "archive"  # pack, unpack, merge


ALL_CAPS: frozenset[Cap] = frozenset(Cap)


class FsError(Exception):
    """Base of every error a backend raises.

    path
        The path the failed operation was about (for two-path operations, the
        one that is at fault, e.g. ``dst`` for AlreadyExists).
    message
        Human-readable reason. Defaults to the subclass's ``default_message``.
    backend_id
        ``Backend.id`` of the backend that raised it, or ``None``.
    cause
        The underlying exception (an ``OSError``, a subprocess failure...) or
        ``None``. Backends also chain it with ``raise ... from cause``.

    ``str(err)`` is ``"<message>: <path>"``.
    """

    default_message = "operation failed"

    def __init__(
        self,
        path: str,
        message: str | None = None,
        backend_id: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.path = path
        self.message = message or self.default_message
        self.backend_id = backend_id
        self.cause = cause
        super().__init__(path, self.message)

    def __str__(self) -> str:
        return f"{self.message}: {self.path}"


class NotFound(FsError):
    """The path, or a directory leading to it (e.g. the parent for mkdir), does not exist."""

    default_message = "no such file or directory"


class AlreadyExists(FsError):
    """Something already exists at the destination and ``overwrite`` was not given."""

    default_message = "already exists"


class NotADirectory(FsError):
    """A directory was required (to list, to hold a new entry, or to be
    replaced only by a directory) but the path is something else."""

    default_message = "not a directory"


class IsADirectory(FsError):
    """A file was required (to read, to write, or to be replaced) but the path is a directory."""

    default_message = "is a directory"


class DirectoryNotEmpty(FsError):
    """A non-recursive delete of a directory that still has entries; nothing was removed."""

    default_message = "directory not empty"


class PermissionDenied(FsError):
    """The store refused access (permissions, read-only filesystem)."""

    default_message = "permission denied"


class Unsupported(FsError):
    """The backend lacks the capability for this method, or the remote side
    refused to run the command. Retrying will not help."""

    default_message = "operation not supported"


class BackendUnavailable(FsError):
    """The whole backend is down (authentication, network, no container,
    timeout). Worth retrying later."""

    default_message = "backend unavailable"


class OperationFailed(FsError):
    """Anything else; ``message`` carries the raw reason."""

    default_message = "operation failed"


_ERRNO_ERRORS: dict[int, type[FsError]] = {
    errno.ENOENT: NotFound,
    errno.EEXIST: AlreadyExists,
    errno.ENOTDIR: NotADirectory,
    errno.EISDIR: IsADirectory,
    errno.ENOTEMPTY: DirectoryNotEmpty,
    errno.EACCES: PermissionDenied,
    errno.EPERM: PermissionDenied,
    errno.EROFS: PermissionDenied,
}


def from_oserror(err: OSError, path: str, backend_id: str | None = None) -> FsError:
    """Translate an ``OSError`` into the matching ``FsError`` subclass.

    The class follows ``err.errno`` (ENOENT -> NotFound, EEXIST -> AlreadyExists,
    ENOTDIR -> NotADirectory, EISDIR -> IsADirectory, ENOTEMPTY ->
    DirectoryNotEmpty, EACCES/EPERM/EROFS -> PermissionDenied, anything else or
    no errno -> OperationFailed). ``path`` becomes the error's path; when the
    OSError names a different file, that file is appended to the message. The
    caller raises the result ``from err``.
    """
    cls = _ERRNO_ERRORS.get(err.errno, OperationFailed) if err.errno is not None else OperationFailed
    message = err.strerror or str(err) or cls.default_message
    if err.filename is not None and str(err.filename) != path:
        message = f"{message} ({err.filename})"
    return cls(path, message, backend_id, err)


class Backend(ABC):
    """A file store. Subclasses set ``id``, ``label`` and ``caps`` (as class or
    instance attributes) and implement the abstract methods.

    id
        Short stable identifier of the backend kind, the key it is registered
        under (``"local"``, ``"btty"``, ``"memory"``). Used in ``FsError.backend_id``.
    label
        Text for a panel header, e.g. ``"local"`` or ``"btty"``.
    caps
        The capabilities this instance offers; fixed for its lifetime.
    archive_dir
        A directory on this store where ``pack`` may write the archive of a
        selection taken from this store, or ``None`` (the default) when the
        store has none. The archive orchestrator reads it only from the
        non-local side of an archive transfer, when that side is the source
        (a LocalBackend's archives go in a fresh local temp directory); a
        non-local source without one is transferred per file. It may be on
        another filesystem than the selection, so an archive there is only
        written by ``pack``, read by ``download`` and removed by ``delete``,
        never renamed elsewhere. BttyBackend: ``Settings.remote_tmp``.
    archive_max_bytes
        The largest batch, in bytes, this store moves as one archive, or
        ``None`` (the default) for no limit. Read from the non-local side of
        an archive transfer: when the scan's total size of the regular files
        (uncompressed) or the size of the packed archive exceeds it, the
        batch is transferred per file instead. BttyBackend:
        ``Settings.archive_max_bytes``.

    All five are read-only for callers and fixed for the instance's
    lifetime; a subclass sets them as class or instance attributes.
    """

    id: str
    label: str
    caps: frozenset[Cap]
    archive_dir: str | None = None
    archive_max_bytes: int | None = None

    # -- pure path algebra: no I/O, never raises FsError ---------------------

    @abstractmethod
    def home(self) -> str:
        """The absolute path a panel opens at when none is given."""

    def join(self, parent: str, name: str) -> str:
        """The path of ``name`` (a single component, no ``/``) inside ``parent``."""
        return posixpath.join(parent, name)

    def parent(self, path: str) -> str:
        """The directory holding ``path``; ``parent("/") == "/"``."""
        return posixpath.dirname(path.rstrip("/")) or "/"

    def require(self, cap: Cap, path: str) -> None:
        """Raise ``Unsupported`` (about ``path``) unless ``cap`` is in ``caps``.
        Implementations call it first thing in every capability-gated method."""
        if cap not in self.caps:
            raise Unsupported(path, f"{cap.value} is not supported by {self.label}", self.id)

    # -- queries --------------------------------------------------------------

    @abstractmethod
    def list_dir(self, path: str) -> list[Entry]:
        """The entries directly inside directory ``path``, unsorted, never ``.``
        or ``..``. A symlink to a directory at ``path`` is followed.

        Needs Cap.LIST. NotFound if ``path`` does not exist, NotADirectory if it
        is not a directory, PermissionDenied if it cannot be read.
        """

    @abstractmethod
    def stat(self, path: str) -> Entry:
        """The Entry for ``path`` itself (lstat: a symlink is not followed).

        Needs Cap.STAT. NotFound if nothing exists at ``path`` (a dangling
        symlink does exist).
        """

    def exists(self, path: str) -> bool:
        """Whether anything (including a dangling symlink) exists at ``path``.

        Needs Cap.STAT (Unsupported otherwise). Other errors than NotFound propagate.
        """
        try:
            self.stat(path)
        except NotFound:
            return False
        return True

    def walk(self, path: str) -> list[Entry]:
        """Every entry below directory ``path`` (not ``path`` itself), each
        directory before its contents; sibling order is unspecified.

        ``path`` is followed if it is a symlink to a directory, like list_dir;
        symlinks below it are listed as symlinks and never descended into.
        Needs Cap.LIST. Errors are list_dir's; the first one aborts the walk.
        """
        result: list[Entry] = []
        stack = [iter(sorted(self.list_dir(path), key=lambda e: e.name))]
        while stack:
            entry = next(stack[-1], None)
            if entry is None:
                stack.pop()
                continue
            result.append(entry)
            if entry.kind == "dir":
                stack.append(iter(sorted(self.list_dir(entry.path), key=lambda e: e.name)))
        return result

    # -- same-backend mutations ----------------------------------------------

    @abstractmethod
    def mkdir(self, path: str) -> None:
        """Create directory ``path`` (one level; the parent must exist).

        Needs Cap.MKDIR. AlreadyExists if anything exists at ``path``, NotFound
        if the parent is missing, NotADirectory if the parent is not a directory.
        """

    @abstractmethod
    def delete(self, path: str, recursive: bool = False) -> None:
        """Remove ``path``: a file, a symlink (never its target) or a directory.

        Needs Cap.DELETE. NotFound if nothing is there. A non-empty directory
        needs ``recursive=True``, otherwise DirectoryNotEmpty is raised and
        nothing is removed. A recursive delete never follows symlinks; if it
        fails part way, what was already removed stays removed and the first
        error is raised. Deleting the root ``/`` raises OperationFailed.
        """

    def delete_many(self, paths: Sequence[str], recursive: bool = False) -> None:
        """``delete(path, recursive)`` for each of ``paths``, in order.

        Needs Cap.DELETE, checked before anything is deleted, even when
        ``paths`` is empty. Stops at the first failure and raises it: what
        was already removed stays removed and the later paths are not
        attempted. The default calls ``delete`` once per path. A backend may
        override it to do the work in fewer round trips with the same
        result, and may then refuse the whole call before deleting anything
        (BttyBackend does so for the root ``/`` and for over-long paths).
        """
        self.require(Cap.DELETE, paths[0] if paths else self.home())
        for path in paths:
            self.delete(path, recursive)

    @abstractmethod
    def rename(self, src: str, dst: str, overwrite: bool = False) -> None:
        """Move ``src`` (any kind, a whole tree for a directory) to the full
        path ``dst`` on this backend. ``dst`` is the new name, not a directory
        to move into.

        Needs Cap.RENAME. NotFound if ``src`` or the parent of ``dst`` is
        missing. If ``dst`` names the very entry ``src`` names (the same path,
        or a case variant on a case-insensitive filesystem) it is renamed in
        place, whatever ``overwrite`` says. Otherwise, if anything exists at
        ``dst``: AlreadyExists unless ``overwrite``; with ``overwrite``,
        IsADirectory if ``dst`` is a directory, NotADirectory if ``src`` is a
        directory and ``dst`` is not, else ``dst`` is replaced. Moving a
        directory to itself or into its own subtree raises OperationFailed.
        A failure leaves ``src`` in place.
        """

    @abstractmethod
    def copy_within(self, src: str, dst: str, overwrite: bool = False) -> None:
        """Copy ``src`` to the full path ``dst`` on this backend: a file's
        content and permission bits, a symlink as a symlink, a directory as a
        whole tree (symlinks inside kept as links).

        Needs Cap.COPY_WITHIN. Existence rules, errors and the self/subtree
        refusal are rename's (a same-entry ``dst`` counts as existing). The
        copy appears at ``dst`` all at once or not at all where the backend
        can manage it (LocalBackend: temp sibling, then ``os.replace``).
        """

    # -- the only cross-backend primitives: regular files only -----------------

    @abstractmethod
    def download(self, path: str, local_dest: str) -> None:
        """Copy the content of file ``path`` (a symlink is followed) to the
        local-filesystem path ``local_dest``.

        Needs Cap.DOWNLOAD. NotFound if ``path`` is missing, IsADirectory if it
        is a directory. ``local_dest`` must not exist (AlreadyExists) and its
        parent must (NotFound, about ``local_dest``). The file appears at
        ``local_dest`` complete or not at all. Local-side failures are raised
        as FsError about ``local_dest``.
        """

    @abstractmethod
    def upload(self, local_src: str, path: str, overwrite: bool = False) -> None:
        """Copy the content of the local-filesystem file ``local_src`` (a
        symlink is followed) to file ``path`` on this backend.

        Needs Cap.UPLOAD. NotFound / IsADirectory about ``local_src`` if it is
        missing or a directory. NotFound if the parent of ``path`` is missing
        (no directories are created). If anything exists at ``path``:
        AlreadyExists unless ``overwrite``; IsADirectory if it is a directory;
        otherwise it is replaced. Modification time and mode are not
        necessarily preserved.
        """

    # -- archives (Cap.ARCHIVE): one transfer for a whole batch -----------------
    #
    # A backend lacking Cap.ARCHIVE needs no code: these defaults raise
    # Unsupported. A backend claiming it overrides all three.

    def pack(self, parent: str, names: Sequence[str], archive: str) -> None:
        """Write the entries ``names`` inside directory ``parent`` to the new
        file ``archive`` on this backend, as one gzip-compressed tar.

        Needs Cap.ARCHIVE. ``names`` are single path components (no ``/``),
        any number of them, each an entry directly inside ``parent``; a
        symlink to a directory at ``parent`` is followed, as by list_dir. A
        backend whose commands have a size limit still writes one archive
        holding every name. No names gives an archive with no members.

        The format is what Python's ``tarfile`` writes in mode ``"w:gz"``
        with ``compresslevel=6`` and its default (PAX) format, so any
        backend's ``unpack`` reads any backend's archive:

        * members are named relative to ``parent``: ``name`` for a selected
          entry, ``name/<path inside it>`` below a selected directory; never
          absolute, never with a ``..`` component;
        * a directory is added with everything below it, itself first;
        * a symlink is stored as a link holding its link text unchanged and
          is never followed, whether selected or below a selected directory;
        * entries of kind ``"other"`` (fifo, socket, device) are left out
          without an error, whether selected or below a selected directory;
        * permission bits and mtime are recorded, ownership is not: uid and
          gid are 0, uname and gname are empty.

        AlreadyExists if anything exists at ``archive``, which is left
        unchanged. NotFound if the parent of ``archive`` is missing, or if
        ``parent`` or one of ``names`` is missing (about that path).
        NotADirectory if ``parent`` is not a directory. Unsupported, with
        nothing written, when this backend cannot pack this request
        (BttyBackend: a remote python3 without ``tarfile.tar_filter``, or a
        command that cannot fit its size limit); the caller then transfers
        per file. Whatever the failure, a partial archive the call wrote is
        removed (what was at ``archive`` before is never touched); a timeout
        or a lost connection may still leave one behind, so the caller
        treats ``archive`` as its own temporary and deletes it afterwards,
        ignoring NotFound. The packed entries never change.
        """
        self.require(Cap.ARCHIVE, parent)
        raise Unsupported(
            parent, f"{Cap.ARCHIVE.value} is not implemented by {self.label}", self.id
        )

    def unpack(self, archive: str, dest: str) -> None:
        """Extract the archive file ``archive``, as ``pack`` writes it, into
        the new directory ``dest`` on this backend, then remove ``archive``.

        Needs Cap.ARCHIVE. The call creates ``dest``: AlreadyExists if
        anything exists at ``dest``, NotFound (about ``dest``) if its parent
        is missing. A member named ``a/b`` ends up at ``dest/a/b``.
        Extraction follows Python's ``tarfile`` "tar" filter with the owner
        fields cleared:

        * a member with an absolute name, or one that would end up outside
          ``dest`` (through ``..`` or through a symlink extracted earlier),
          raises OperationFailed. The "tar" filter alone strips a leading
          ``/`` instead of refusing it, so implementations check for
          absolute names themselves;
        * a symlink is recreated as a link with its link text unchanged,
          even an absolute one or one pointing outside ``dest`` (the
          stricter "data" filter, which refuses those, is not used);
        * files and directories get the recorded mtime and permission bits,
          minus setuid, setgid, sticky and group/other write;
        * ownership is never restored: uid, gid, uname and gname are set to
          None before extraction, so everything belongs to the user running
          it, even when that user is root.

        On success ``archive`` is removed. On any failure ``dest`` is
        removed with whatever was extracted, ``archive`` is kept and the
        error is raised: NotFound if ``archive`` is missing, OperationFailed
        if it is not an archive ``pack`` could have written (corrupt,
        truncated, not gzip). Unsupported, with ``dest`` never created, when
        this backend cannot extract with these semantics (BttyBackend: a
        remote python3 without ``tarfile.tar_filter``); the caller then
        transfers per file. As with ``pack``, a timeout or a lost connection
        may still leave ``dest`` behind, and the caller deletes it.
        """
        self.require(Cap.ARCHIVE, archive)
        raise Unsupported(
            archive, f"{Cap.ARCHIVE.value} is not implemented by {self.label}", self.id
        )

    def merge(self, src: str, dst: str) -> None:
        """Move ``src`` to the full path ``dst`` on this backend, merging
        directories: how an archive transfer moves its staging directory
        into the target.

        Needs Cap.ARCHIVE. Kinds are taken by lstat at both ends, so a
        symlink is never followed, at ``src`` or at ``dst``. It never asks
        (the caller resolved conflicts before packing) and never copies:
        every step is a rename within this backend::

            at src / at dst                result
            anything / nothing             src is renamed to dst
            directory / directory          each entry of src is merged into
                                           dst/<name> by these same rules,
                                           then src, now empty, is removed;
                                           the directory at dst stays, with
                                           its own mode
            non-directory / non-directory  dst is replaced by src (a symlink
                                           at dst is replaced, not followed)
            non-directory / directory      IsADirectory
            directory / non-directory      NotADirectory

        A clash is raised about the deepest path concerned (the ``dst/...``
        path where it was found). Entries are merged in no particular order.
        The first error stops the merge and is raised: what was already
        moved stays moved, and ``src`` keeps the rest (a directory of
        ``src`` is removed only once empty). NotFound if nothing is at
        ``src`` or the parent of ``dst`` is missing. OperationFailed, before
        anything moves, if ``dst`` is ``src`` or lies below it (compared as
        paths). A rename across filesystems fails with OperationFailed, so
        the caller keeps ``src`` on the filesystem of ``dst`` (it stages
        inside the target directory).
        """
        self.require(Cap.ARCHIVE, src)
        raise Unsupported(
            src, f"{Cap.ARCHIVE.value} is not implemented by {self.label}", self.id
        )

    # -- listing cache ----------------------------------------------------------

    def invalidate(self, path: str | None = None) -> None:
        """Drop cached ``list_dir`` results for directory ``path`` and every
        directory below it (``path`` and the paths starting with
        ``path + "/"``; ``/`` means all of them), or for every directory when
        ``path`` is None, so that the next ``list_dir`` of each asks the
        store. The listing of the parent of ``path`` is kept.

        Needs no capability, does no I/O, changes nothing on the store and
        does not raise. Safe from any thread at any time, also while a
        ``list_dir`` of an affected directory runs (that listing is then not
        cached). The default does nothing: a backend without a cache (see
        Semantics) has nothing to drop.
        """

    def close(self) -> None:
        """Release any resources. Idempotent; the default does nothing."""
