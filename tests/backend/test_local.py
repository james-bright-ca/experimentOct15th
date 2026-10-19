"""LocalBackend: the shared contract on tmp_path, plus local-only behaviour.

The local-only cases are symlinks, the EXDEV fallback, permissions, the
mapping of raw OSErrors, atomic writes, special files, the registry's local
locations, and the archive cases the contract cannot observe through its hooks.
"""

from __future__ import annotations

import errno
import gzip
import io
import os
import shutil
import socket
import stat
import tarfile
import threading
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.backend.contract import DIR, FILE, LINK, BackendContract, Node
from twinfm.backend import (
    REGISTRY,
    UnknownBackendError,
    available,
    open_backend,
    parse_location,
)
from twinfm.backend.base import (
    ALL_CAPS,
    AlreadyExists,
    Backend,
    DirectoryNotEmpty,
    FsError,
    IsADirectory,
    NotADirectory,
    NotFound,
    OperationFailed,
    PermissionDenied,
    from_oserror,
)
from twinfm.backend.local import LocalBackend
from twinfm.backend.memory import MemoryBackend
from twinfm.config import Settings

TMP_SUFFIX = ".twinfm-tmp"

needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="permission bits do not bind root"
)


def local_tree(root: str) -> dict[str, Node]:
    """Every path below ``root`` (lstat, symlinks not followed) as contract nodes."""
    result: dict[str, Node] = {}
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        with os.scandir(os.path.join(root, rel_dir) if rel_dir else root) as it:
            for de in it:
                rel = f"{rel_dir}/{de.name}" if rel_dir else de.name
                if de.is_symlink():
                    result[rel] = LINK(os.readlink(de.path))
                elif de.is_dir(follow_symlinks=False):
                    result[rel] = DIR
                    stack.append(rel)
                else:
                    result[rel] = FILE(Path(de.path).read_bytes())
    return result


def leftovers(top: Path) -> list[str]:
    """Temporary siblings LocalBackend may have left anywhere below ``top``."""
    return [
        os.path.join(dirpath, name)
        for dirpath, dirnames, filenames in os.walk(top)
        for name in dirnames + filenames
        if TMP_SUFFIX in name
    ]


class TestLocalBackendContract(BackendContract):
    @pytest.fixture
    def backend(self) -> LocalBackend:
        return LocalBackend()

    @pytest.fixture
    def root(self, tmp_path: Path) -> str:
        root = tmp_path / "root"
        root.mkdir()
        return str(root)

    def seed_dir(self, backend: Backend, path: str) -> None:
        os.mkdir(path)

    def seed_file(self, backend: Backend, path: str, data: bytes) -> None:
        Path(path).write_bytes(data)

    def seed_symlink(self, backend: Backend, path: str, target: str) -> None:
        os.symlink(target, path)

    def observe(self, backend: Backend, root: str) -> dict[str, Node]:
        return local_tree(root)


def test_the_contract_suite_is_inherited_unchanged() -> None:
    contract_tests = {name for name in dir(BackendContract) if name.startswith("test_")}
    assert contract_tests
    for name in contract_tests:
        assert getattr(TestLocalBackendContract, name) is getattr(BackendContract, name), name


@pytest.fixture
def backend() -> LocalBackend:
    return LocalBackend()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


def test_offers_every_capability(backend: LocalBackend) -> None:
    assert (backend.id, backend.label, backend.caps) == ("local", "local", ALL_CAPS)


def test_home_is_the_users_home_directory(
    backend: LocalBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path) + "/")
    assert backend.home() == str(tmp_path)


# -- list_dir ([A] criterion: file, subdirectory, dotfile, symlink to the subdirectory) --


def test_list_dir_returns_the_four_entries_with_the_symlink_described(
    backend: LocalBackend, root: Path
) -> None:
    (root / "file.txt").write_bytes(b"hello")
    (root / "sub").mkdir()
    (root / ".dotfile").write_bytes(b"")
    os.symlink("sub", root / "link-to-sub")
    entries = {e.name: e for e in backend.list_dir(str(root))}
    assert sorted(entries) == [".dotfile", "file.txt", "link-to-sub", "sub"]
    link = entries["link-to-sub"]
    assert (link.kind, link.link_target, link.target_kind) == ("symlink", "sub", "dir")
    assert entries["sub"].kind == "dir"
    assert entries["file.txt"].kind == entries[".dotfile"].kind == "file"


def test_list_dir_reports_special_files_as_other(backend: LocalBackend, root: Path) -> None:
    os.mkfifo(root / "fifo")
    (entry,) = backend.list_dir(str(root))
    assert (entry.name, entry.kind, entry.size, entry.target_kind) == ("fifo", "other", None, None)


def test_list_dir_reports_a_symlink_loop_as_dangling(backend: LocalBackend, root: Path) -> None:
    os.symlink("loop", root / "loop")
    (entry,) = backend.list_dir(str(root))
    assert (entry.kind, entry.link_target, entry.target_kind) == ("symlink", "loop", None)


def test_entries_carry_mtime_and_permission_bits(backend: LocalBackend, root: Path) -> None:
    f = root / "f"
    f.write_bytes(b"x")
    f.chmod(0o640)
    os.utime(f, (1_000_000_000, 1_234_567_890))
    entry = backend.stat(str(f))
    assert entry.mode == 0o640
    assert entry.mtime == 1_234_567_890


# -- EXDEV: rename across filesystems -------------------------------------------------


@pytest.fixture
def cross_device(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Make every os.rename fail with EXDEV, as between two filesystems; record the calls."""
    calls: list[tuple[str, str]] = []

    def rename(src: str, dst: str, *args: object, **kwargs: object) -> None:
        calls.append((os.fspath(src), os.fspath(dst)))
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), src, None, dst)

    monkeypatch.setattr(os, "rename", rename)
    return calls


def test_exdev_rename_of_a_file_copies_then_removes_the_source(
    backend: LocalBackend, root: Path, cross_device: list[tuple[str, str]]
) -> None:
    src = root / "src"
    src.write_bytes(b"payload")
    src.chmod(0o640)
    backend.rename(str(src), str(root / "dst"))
    assert local_tree(str(root)) == {"dst": FILE(b"payload")}
    assert stat.S_IMODE(os.lstat(root / "dst").st_mode) == 0o640
    assert cross_device == [(str(src), str(root / "dst"))]


def test_exdev_rename_of_a_tree_keeps_symlinks_as_links(
    backend: LocalBackend, root: Path, cross_device: list[tuple[str, str]]
) -> None:
    (root / "outside").mkdir()
    (root / "outside" / "o").write_bytes(b"outside")
    src = root / "src"
    (src / "sub").mkdir(parents=True)
    (src / "f").write_bytes(b"1")
    (src / "sub" / "g").write_bytes(b"2")
    os.symlink("../f", src / "sub" / "rel-link")
    os.symlink(str(root / "outside"), src / "abs-link")
    (root / "dest").mkdir()
    backend.rename(str(src), str(root / "dest" / "moved"))
    assert local_tree(str(root)) == {
        "outside": DIR,
        "outside/o": FILE(b"outside"),
        "dest": DIR,
        "dest/moved": DIR,
        "dest/moved/f": FILE(b"1"),
        "dest/moved/sub": DIR,
        "dest/moved/sub/g": FILE(b"2"),
        "dest/moved/sub/rel-link": LINK("../f"),
        "dest/moved/abs-link": LINK(str(root / "outside")),
    }
    assert len(cross_device) == 1
    assert leftovers(root) == []


def test_exdev_rename_of_a_symlink_moves_the_link_and_keeps_the_target(
    backend: LocalBackend, root: Path, cross_device: list[tuple[str, str]]
) -> None:
    (root / "d").mkdir()
    (root / "d" / "f").write_bytes(b"1")
    os.symlink("d", root / "to-d")
    backend.rename(str(root / "to-d"), str(root / "moved"))
    assert local_tree(str(root)) == {"d": DIR, "d/f": FILE(b"1"), "moved": LINK("d")}


def test_exdev_rename_with_overwrite_replaces_the_destination(
    backend: LocalBackend, root: Path, cross_device: list[tuple[str, str]]
) -> None:
    (root / "src").write_bytes(b"new")
    (root / "dst").write_bytes(b"old")
    backend.rename(str(root / "src"), str(root / "dst"), overwrite=True)
    assert local_tree(str(root)) == {"dst": FILE(b"new")}


def test_exdev_rename_onto_an_existing_file_without_overwrite_refuses_before_copying(
    backend: LocalBackend, root: Path, cross_device: list[tuple[str, str]]
) -> None:
    (root / "src").write_bytes(b"new")
    (root / "dst").write_bytes(b"old")
    with pytest.raises(AlreadyExists):
        backend.rename(str(root / "src"), str(root / "dst"))
    assert local_tree(str(root)) == {"src": FILE(b"new"), "dst": FILE(b"old")}
    assert cross_device == []


def test_exdev_rename_into_its_own_subtree_through_a_symlink_refuses_and_keeps_the_source(
    backend: LocalBackend, root: Path, cross_device: list[tuple[str, str]]
) -> None:
    # The kernel reports EXDEV before the subtree check when the destination
    # lies on another filesystem mounted inside the source, so the copy-then-
    # delete fallback must refuse a subtree destination just like rename does.
    (root / "d").mkdir()
    (root / "d" / "precious").write_bytes(b"data")
    os.symlink("d", root / "to-d")
    before = local_tree(str(root))
    with pytest.raises(OperationFailed):
        backend.rename(str(root / "d"), str(root / "to-d" / "moved"))
    assert local_tree(str(root)) == before


@needs_non_root
def test_exdev_rename_that_cannot_copy_leaves_the_source_and_no_destination(
    backend: LocalBackend, root: Path, cross_device: list[tuple[str, str]]
) -> None:
    src = root / "src"
    src.mkdir()
    (src / "ok").write_bytes(b"ok")
    secret = src / "secret"
    secret.write_bytes(b"secret")
    secret.chmod(0)
    try:
        with pytest.raises(FsError):
            backend.rename(str(src), str(root / "dst"))
        assert sorted(os.listdir(root)) == ["src"]
        assert sorted(os.listdir(src)) == ["ok", "secret"]
        assert leftovers(root) == []
    finally:
        secret.chmod(0o600)


# -- permissions --------------------------------------------------------------------------


@pytest.fixture
def locked(root: Path) -> Iterator[Callable[[Path, int], None]]:
    """chmod paths for one test and restore owner access afterwards, so cleanup works."""
    changed: list[Path] = []

    def lock(path: Path, mode: int) -> None:
        path.chmod(mode)
        changed.append(path)

    yield lock
    for path in reversed(changed):
        path.chmod(0o700 if path.is_dir() else 0o600)


@needs_non_root
def test_listing_an_unreadable_directory_raises_permission_denied(
    backend: LocalBackend, root: Path, locked: Callable[[Path, int], None]
) -> None:
    (root / "closed").mkdir()
    locked(root / "closed", 0)
    with pytest.raises(PermissionDenied) as err:
        backend.list_dir(str(root / "closed"))
    assert err.value.path == str(root / "closed")
    with pytest.raises(PermissionDenied):
        backend.walk(str(root))


@needs_non_root
def test_writes_into_a_read_only_directory_raise_permission_denied_and_change_nothing(
    backend: LocalBackend, root: Path, tmp_path: Path, locked: Callable[[Path, int], None]
) -> None:
    ro = root / "ro"
    ro.mkdir()
    (ro / "f").write_bytes(b"keep")
    (ro / "d").mkdir()
    (root / "other").write_bytes(b"other")
    local_src = tmp_path / "local-src"
    local_src.write_bytes(b"upload")
    locked(ro, 0o555)
    before = local_tree(str(root))
    attempts: list[Callable[[], object]] = [
        lambda: backend.mkdir(str(ro / "new")),
        lambda: backend.delete(str(ro / "f")),
        lambda: backend.delete(str(ro / "d")),
        lambda: backend.rename(str(ro / "f"), str(ro / "g")),
        lambda: backend.rename(str(root / "other"), str(ro / "other")),
        lambda: backend.copy_within(str(ro / "f"), str(ro / "g")),
        lambda: backend.copy_within(str(ro / "d"), str(ro / "e")),
        lambda: backend.upload(str(local_src), str(ro / "up")),
        lambda: backend.upload(str(local_src), str(ro / "f"), overwrite=True),
    ]
    for attempt in attempts:
        with pytest.raises(PermissionDenied):
            attempt()
    assert local_tree(str(root)) == before
    assert leftovers(root) == []


@needs_non_root
def test_download_into_a_read_only_local_directory_raises_permission_denied(
    backend: LocalBackend, root: Path, tmp_path: Path, locked: Callable[[Path, int], None]
) -> None:
    (root / "f").write_bytes(b"x")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    locked(dest_dir, 0o555)
    with pytest.raises(PermissionDenied) as err:
        backend.download(str(root / "f"), str(dest_dir / "got"))
    assert err.value.path == str(dest_dir / "got")
    assert os.listdir(dest_dir) == []


@needs_non_root
def test_copying_a_tree_with_an_unreadable_file_leaves_no_partial_copy(
    backend: LocalBackend, root: Path, locked: Callable[[Path, int], None]
) -> None:
    src = root / "src"
    src.mkdir()
    (src / "ok").write_bytes(b"ok")
    (src / "secret").write_bytes(b"secret")
    locked(src / "secret", 0)
    with pytest.raises(FsError):
        backend.copy_within(str(src), str(root / "copy"))
    assert sorted(os.listdir(root)) == ["src"]
    assert leftovers(root) == []


@needs_non_root
def test_copying_an_unreadable_file_raises_permission_denied(
    backend: LocalBackend, root: Path, locked: Callable[[Path, int], None]
) -> None:
    (root / "secret").write_bytes(b"secret")
    locked(root / "secret", 0)
    with pytest.raises(PermissionDenied):
        backend.copy_within(str(root / "secret"), str(root / "copy"))
    with pytest.raises(PermissionDenied):
        backend.download(str(root / "secret"), str(root / "got"))
    assert sorted(os.listdir(root)) == ["secret"]


# -- no raw OSError or ValueError escapes ---------------------------------------------------


BAD_PATHS = {
    "nul": lambda root: f"{root}/bad\0name",
    "name-too-long": lambda root: f"{root}/{'n' * 1000}",
    "symlink-loop": lambda root: f"{root}/loop/child",
    "under-a-file": lambda root: f"{root}/file/child",
}

OPERATIONS: dict[str, Callable[[LocalBackend, str, str, str], object]] = {
    "list_dir": lambda b, bad, good, local: b.list_dir(bad),
    "walk": lambda b, bad, good, local: b.walk(bad),
    "stat": lambda b, bad, good, local: b.stat(bad),
    "mkdir": lambda b, bad, good, local: b.mkdir(bad),
    "delete": lambda b, bad, good, local: b.delete(bad, recursive=True),
    "rename-src": lambda b, bad, good, local: b.rename(bad, good + "-new"),
    "rename-dst": lambda b, bad, good, local: b.rename(good, bad, overwrite=True),
    "copy-src": lambda b, bad, good, local: b.copy_within(bad, good + "-new"),
    "copy-dst": lambda b, bad, good, local: b.copy_within(good, bad, overwrite=True),
    "upload-src": lambda b, bad, good, local: b.upload(bad, good + "-new"),
    "upload-dst": lambda b, bad, good, local: b.upload(local, bad, overwrite=True),
    "download-src": lambda b, bad, good, local: b.download(bad, local + "-got"),
    "download-dst": lambda b, bad, good, local: b.download(good, bad),
}


@pytest.mark.parametrize("bad", list(BAD_PATHS))
@pytest.mark.parametrize("operation", list(OPERATIONS))
def test_os_level_failures_surface_as_fs_errors(
    backend: LocalBackend, root: Path, tmp_path: Path, operation: str, bad: str
) -> None:
    os.symlink("loop", root / "loop")
    (root / "file").write_bytes(b"f")
    (root / "good").write_bytes(b"good")
    local = tmp_path / "local"
    local.write_bytes(b"local")
    before = local_tree(str(root))
    with pytest.raises(FsError):
        OPERATIONS[operation](backend, BAD_PATHS[bad](str(root)), str(root / "good"), str(local))
    assert local_tree(str(root)) == before
    assert leftovers(tmp_path) == []


def test_exists_raises_fs_errors_other_than_not_found(backend: LocalBackend, root: Path) -> None:
    (root / "file").write_bytes(b"f")
    with pytest.raises(NotADirectory):
        backend.exists(str(root / "file" / "child"))
    with pytest.raises(OperationFailed):
        backend.exists(f"{root}/bad\0name")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (errno.ENOENT, NotFound),
        (errno.EEXIST, AlreadyExists),
        (errno.ENOTDIR, NotADirectory),
        (errno.EISDIR, IsADirectory),
        (errno.ENOTEMPTY, DirectoryNotEmpty),
        (errno.EACCES, PermissionDenied),
        (errno.EPERM, PermissionDenied),
        (errno.EROFS, PermissionDenied),
        (errno.EIO, OperationFailed),
        (errno.ENOSPC, OperationFailed),
    ],
    ids=lambda v: errno.errorcode.get(v, "") if isinstance(v, int) else v.__name__,
)
def test_from_oserror_maps_errno_to_the_fs_error_class(code: int, expected: type[FsError]) -> None:
    cause = OSError(code, os.strerror(code), "/the/path")
    err = from_oserror(cause, "/the/path", "local")
    assert type(err) is expected
    assert (err.path, err.backend_id, err.cause) == ("/the/path", "local", cause)
    assert str(err) == f"{os.strerror(code)}: /the/path"


def test_from_oserror_without_errno_is_operation_failed_and_names_another_file() -> None:
    err = from_oserror(OSError("weird"), "/p")
    assert type(err) is OperationFailed
    assert str(err) == "weird: /p"
    other = from_oserror(OSError(errno.ENOENT, "No such file", "/tmp/other"), "/p")
    assert type(other) is NotFound
    assert "/tmp/other" in other.message
    assert other.path == "/p"


# -- delete ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "//", "///"])
def test_delete_refuses_the_root_directory_without_touching_anything(
    backend: LocalBackend, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    def destructive(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"destructive call reached while deleting {path!r}: {args}")

    for module, name in [(shutil, "rmtree"), (os, "rmdir"), (os, "unlink"), (os, "remove")]:
        monkeypatch.setattr(module, name, destructive)
    with pytest.raises(OperationFailed):
        backend.delete(path, recursive=True)


def test_delete_never_follows_a_symlink_even_with_a_trailing_slash(
    backend: LocalBackend, root: Path
) -> None:
    (root / "d").mkdir()
    (root / "d" / "f").write_bytes(b"1")
    os.symlink("d", root / "to-d")
    backend.delete(str(root / "to-d") + "/", recursive=True)
    assert local_tree(str(root)) == {"d": DIR, "d/f": FILE(b"1")}


# -- rename edge cases -------------------------------------------------------------------------


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_bytes(b"")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def test_rename_to_a_case_variant_renames_in_place(backend: LocalBackend, root: Path) -> None:
    if not _case_insensitive(root):
        pytest.skip("only a case-insensitive filesystem has case-variant names for one entry")
    (root / "Name").mkdir()
    (root / "Name" / "f").write_bytes(b"1")
    backend.rename(str(root / "Name"), str(root / "name"))
    assert local_tree(str(root)) == {"name": DIR, "name/f": FILE(b"1")}


def test_rename_onto_a_hard_link_of_the_same_file(backend: LocalBackend, root: Path) -> None:
    (root / "a").write_bytes(b"same inode")
    os.link(root / "a", root / "b")
    with pytest.raises(AlreadyExists):
        backend.rename(str(root / "a"), str(root / "b"))
    assert local_tree(str(root)) == {"a": FILE(b"same inode"), "b": FILE(b"same inode")}
    # A hard link is a separate entry, so with overwrite it is replaced like any
    # other file: afterwards only the destination name is left.
    backend.rename(str(root / "a"), str(root / "b"), overwrite=True)
    assert local_tree(str(root)) == {"b": FILE(b"same inode")}


@pytest.fixture
def case_insensitive_root(root: Path) -> Path:
    """``root``, on a filesystem where a case variant names the same entry."""
    if not _case_insensitive(root):
        pytest.skip("only a case-insensitive filesystem has case-variant names for one entry")
    return root


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("kind", ["file", "hard-linked-file", "dir"])
def test_rename_to_a_case_variant_renames_in_place_whatever_overwrite_says(
    backend: LocalBackend, case_insensitive_root: Path, kind: str, overwrite: bool
) -> None:
    # base.py rename: a dst naming the very entry src names "is renamed in
    # place, whatever overwrite says". Another hard link to the file does not
    # make "name" a different entry from "Name".
    root = case_insensitive_root
    if kind == "dir":
        (root / "Name").mkdir()
        (root / "Name" / "f").write_bytes(b"1")
        expected = {"name": DIR, "name/f": FILE(b"1")}
    else:
        (root / "Name").write_bytes(b"data")
        expected = {"name": FILE(b"data")}
        if kind == "hard-linked-file":
            os.link(root / "Name", root / "other")
            expected["other"] = FILE(b"data")
    backend.rename(str(root / "Name"), str(root / "name"), overwrite=overwrite)
    assert local_tree(str(root)) == expected


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("dst", ["a", "here/a"], ids=["same-path", "through-a-symlink"])
@pytest.mark.parametrize("links", [1, 2], ids=["one-link", "hard-linked"])
def test_rename_of_a_file_onto_its_own_entry_is_a_no_op(
    backend: LocalBackend, root: Path, links: int, dst: str, overwrite: bool
) -> None:
    # "here" leads back to root, so root/here/a names the entry root/a.
    (root / "a").write_bytes(b"same inode")
    os.symlink(".", root / "here")
    expected = {"a": FILE(b"same inode"), "here": LINK(".")}
    if links == 2:
        os.link(root / "a", root / "b")
        expected["b"] = FILE(b"same inode")
    backend.rename(str(root / "a"), str(root / dst), overwrite=overwrite)
    assert local_tree(str(root)) == expected


@pytest.mark.parametrize("dst", ["sub/b", "to-sub/b"], ids=["direct", "through-a-symlink"])
def test_rename_onto_a_hard_link_in_another_directory_leaves_only_the_destination(
    backend: LocalBackend, root: Path, dst: str
) -> None:
    (root / "sub").mkdir()
    os.symlink("sub", root / "to-sub")
    (root / "a").write_bytes(b"same inode")
    os.link(root / "a", root / "sub" / "b")
    backend.rename(str(root / "a"), str(root / dst), overwrite=True)
    assert local_tree(str(root)) == {
        "sub": DIR,
        "sub/b": FILE(b"same inode"),
        "to-sub": LINK("sub"),
    }


def test_rename_onto_a_case_variant_of_a_hard_link_without_overwrite_raises_and_keeps_both(
    backend: LocalBackend, case_insensitive_root: Path
) -> None:
    # "b" names the entry "B", a separate hard link, not a's own entry.
    root = case_insensitive_root
    (root / "a").write_bytes(b"same inode")
    os.link(root / "a", root / "B")
    with pytest.raises(AlreadyExists):
        backend.rename(str(root / "a"), str(root / "b"))
    assert local_tree(str(root)) == {"a": FILE(b"same inode"), "B": FILE(b"same inode")}


def test_rename_onto_a_case_variant_of_a_hard_link_leaves_only_the_destination(
    backend: LocalBackend, case_insensitive_root: Path
) -> None:
    # "b" names the entry "B": a separate hard link to a's file. With
    # overwrite it is replaced like any other file, so the name "a" must go.
    root = case_insensitive_root
    (root / "a").write_bytes(b"same inode")
    os.link(root / "a", root / "B")
    backend.rename(str(root / "a"), str(root / "b"), overwrite=True)
    names = os.listdir(root)
    assert [name.casefold() for name in names] == ["b"], names
    assert (root / "b").read_bytes() == b"same inode"


# Same-inode renames whose names differ only in spelling. For a file with
# several links the backend must tell "dst is src's own entry" (rename in place)
# from "dst is a separate hard link" (AlreadyExists, or replace it). Neither
# answer may leave a temporary or probe directory behind.

NFC_NAME = unicodedata.normalize("NFC", "cafe\N{COMBINING ACUTE ACCENT}")
NFD_NAME = unicodedata.normalize("NFD", NFC_NAME)


@pytest.fixture
def normalisation_insensitive_root(root: Path) -> Path:
    """``root``, on a filesystem where NFC and NFD spellings name one entry."""
    probe = root / NFC_NAME
    probe.write_bytes(b"")
    try:
        if not (root / NFD_NAME).exists():
            pytest.skip("only a normalisation-insensitive filesystem has NFC/NFD variants")
    finally:
        probe.unlink()
    return root


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("links", [1, 2], ids=["one-link", "hard-linked"])
def test_rename_to_a_normalisation_variant_renames_in_place_whatever_overwrite_says(
    backend: LocalBackend, normalisation_insensitive_root: Path, links: int, overwrite: bool
) -> None:
    # NFD "café" names the entry NFC "café": the same entry, renamed in place.
    # The entry must never be removed as if it were a separate hard link.
    root = normalisation_insensitive_root
    (root / NFC_NAME).write_bytes(b"data")
    expected = {NFD_NAME: FILE(b"data")}
    if links == 2:
        os.link(root / NFC_NAME, root / "other")
        expected["other"] = FILE(b"data")
    backend.rename(str(root / NFC_NAME), str(root / NFD_NAME), overwrite=overwrite)
    assert local_tree(str(root)) == expected
    assert leftovers(root) == []


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize(
    "dst", ["name", "Name", "here/name"], ids=["variant", "on-disk", "variant-through-a-symlink"]
)
@pytest.mark.parametrize("links", [1, 2], ids=["one-link", "hard-linked"])
def test_rename_with_src_spelt_unlike_its_entry_renames_in_place_whatever_overwrite_says(
    backend: LocalBackend, case_insensitive_root: Path, links: int, dst: str, overwrite: bool
) -> None:
    # "NAME" names the entry "Name", and so do "name", "Name" and "here/name"
    # (here -> "."), so every dst is src's own entry.
    root = case_insensitive_root
    (root / "Name").write_bytes(b"data")
    os.symlink(".", root / "here")
    expected = {os.path.basename(dst): FILE(b"data"), "here": LINK(".")}
    if links == 2:
        os.link(root / "Name", root / "other")
        expected["other"] = FILE(b"data")
    backend.rename(str(root / "NAME"), str(root / dst), overwrite=overwrite)
    assert local_tree(str(root)) == expected
    assert leftovers(root) == []


@pytest.mark.parametrize("src", ["a", "A"], ids=["src-on-disk", "src-variant"])
@pytest.mark.parametrize("dst", ["b", "here/b"], ids=["direct", "through-a-symlink"])
def test_rename_onto_a_variant_of_a_separate_hard_link_raises_or_replaces_it(
    backend: LocalBackend, case_insensitive_root: Path, src: str, dst: str
) -> None:
    # "b" names "B", a separate hard link to a's file; "A" names "a". Without
    # overwrite: AlreadyExists, both entries kept as they were spelt. With
    # overwrite: "B" is replaced, so only that one entry is left.
    root = case_insensitive_root
    (root / "a").write_bytes(b"same inode")
    os.link(root / "a", root / "B")
    os.symlink(".", root / "here")
    before = local_tree(str(root))
    with pytest.raises(AlreadyExists):
        backend.rename(str(root / src), str(root / dst))
    assert local_tree(str(root)) == before
    assert leftovers(root) == []
    backend.rename(str(root / src), str(root / dst), overwrite=True)
    names = sorted(os.listdir(root))
    assert [name.casefold() for name in names] == ["b", "here"], names
    assert (root / "b").read_bytes() == b"same inode"
    assert leftovers(root) == []


@needs_non_root
@pytest.mark.parametrize("overwrite", [False, True])
def test_rename_of_a_hard_linked_file_onto_its_own_path_in_a_read_only_directory_is_a_no_op(
    backend: LocalBackend, root: Path, locked: Callable[[Path, int], None], overwrite: bool
) -> None:
    ro = root / "ro"
    ro.mkdir()
    (ro / "a").write_bytes(b"same inode")
    os.link(ro / "a", ro / "b")
    locked(ro, 0o555)
    before = local_tree(str(root))
    backend.rename(str(ro / "a"), str(ro / "a"), overwrite=overwrite)
    assert local_tree(str(root)) == before
    assert leftovers(root) == []


@needs_non_root
def test_rename_onto_a_hard_link_in_a_read_only_directory_changes_nothing(
    backend: LocalBackend, root: Path, locked: Callable[[Path, int], None]
) -> None:
    # Both names are listed as spelt, so they are decided without writing: the
    # existence rule answers first, and replacing needs a writable directory.
    ro = root / "ro"
    ro.mkdir()
    (ro / "a").write_bytes(b"same inode")
    os.link(ro / "a", ro / "b")
    locked(ro, 0o555)
    before = local_tree(str(root))
    with pytest.raises(AlreadyExists):
        backend.rename(str(ro / "a"), str(ro / "b"))
    with pytest.raises(PermissionDenied):
        backend.rename(str(ro / "a"), str(ro / "b"), overwrite=True)
    assert local_tree(str(root)) == before
    assert leftovers(root) == []


@needs_non_root
@pytest.mark.parametrize("overwrite", [False, True])
def test_rename_to_a_case_variant_of_a_hard_linked_file_in_a_read_only_directory_changes_nothing(
    backend: LocalBackend,
    case_insensitive_root: Path,
    locked: Callable[[Path, int], None],
    overwrite: bool,
) -> None:
    # The in-place rename itself needs a writable directory.
    ro = case_insensitive_root / "ro"
    ro.mkdir()
    (ro / "Name").write_bytes(b"data")
    os.link(ro / "Name", ro / "other")
    locked(ro, 0o555)
    before = local_tree(str(case_insensitive_root))
    with pytest.raises(PermissionDenied):
        backend.rename(str(ro / "Name"), str(ro / "name"), overwrite=overwrite)
    assert local_tree(str(case_insensitive_root)) == before
    assert leftovers(case_insensitive_root) == []


@needs_non_root
@pytest.mark.parametrize("overwrite", [False, True])
def test_rename_onto_a_case_variant_of_a_hard_link_in_a_read_only_directory_changes_nothing(
    backend: LocalBackend,
    case_insensitive_root: Path,
    locked: Callable[[Path, int], None],
    overwrite: bool,
) -> None:
    # "b" names the separate link "B". Replacing it needs a writable directory.
    # Without overwrite the contract does not order its existence rule against
    # a permission failure, so either error is accepted; nothing may change.
    ro = case_insensitive_root / "ro"
    ro.mkdir()
    (ro / "a").write_bytes(b"same inode")
    os.link(ro / "a", ro / "B")
    locked(ro, 0o555)
    before = local_tree(str(case_insensitive_root))
    expected = PermissionDenied if overwrite else (AlreadyExists, PermissionDenied)
    with pytest.raises(expected):
        backend.rename(str(ro / "a"), str(ro / "b"), overwrite=overwrite)
    assert local_tree(str(case_insensitive_root)) == before
    assert leftovers(case_insensitive_root) == []


@pytest.mark.parametrize("operation", ["rename", "copy_within"])
@pytest.mark.parametrize("dst", ["D/inside", "D/E/deeper"])
def test_a_directory_into_its_own_subtree_through_a_case_variant_raises(
    backend: LocalBackend, case_insensitive_root: Path, operation: str, dst: str
) -> None:
    # On a case-insensitive filesystem D/inside is d/inside: inside d.
    root = case_insensitive_root
    (root / "d" / "e").mkdir(parents=True)
    (root / "d" / "f").write_bytes(b"1")
    before = local_tree(str(root))
    with pytest.raises(OperationFailed):
        getattr(backend, operation)(str(root / "d"), str(root / dst))
    assert local_tree(str(root)) == before
    assert leftovers(root) == []


@pytest.mark.parametrize("dst", ["D/moved", "D/E/deeper"])
def test_exdev_rename_into_its_own_subtree_through_a_case_variant_refuses_and_keeps_the_source(
    backend: LocalBackend,
    case_insensitive_root: Path,
    cross_device: list[tuple[str, str]],
    dst: str,
) -> None:
    # Like the symlink case: the copy-then-delete fallback must not copy d into
    # itself and then delete d.
    root = case_insensitive_root
    (root / "d" / "e").mkdir(parents=True)
    (root / "d" / "precious").write_bytes(b"data")
    before = local_tree(str(root))
    with pytest.raises(OperationFailed):
        backend.rename(str(root / "d"), str(root / dst))
    assert local_tree(str(root)) == before
    assert leftovers(root) == []


# -- atomic writes ---------------------------------------------------------------------------


@pytest.fixture
def failing_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every chunked file copy fail part way, as on a full or broken disk."""

    def copyfileobj(fsrc: object, fdst: object, length: int = 0) -> None:
        fdst.write(b"partial")  # type: ignore[attr-defined]
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(shutil, "copyfileobj", copyfileobj)


def test_a_failed_upload_keeps_the_old_file_and_leaves_no_temporary(
    backend: LocalBackend, root: Path, tmp_path: Path, failing_copy: None
) -> None:
    (root / "dst").write_bytes(b"old")
    src = tmp_path / "src"
    src.write_bytes(b"new content")
    with pytest.raises(OperationFailed) as err:
        backend.upload(str(src), str(root / "dst"), overwrite=True)
    assert err.value.path == str(root / "dst")
    assert local_tree(str(root)) == {"dst": FILE(b"old")}
    assert leftovers(tmp_path) == []


def test_a_failed_copy_or_download_leaves_nothing_behind(
    backend: LocalBackend, root: Path, tmp_path: Path, failing_copy: None
) -> None:
    (root / "f").write_bytes(b"content")
    with pytest.raises(OperationFailed):
        backend.copy_within(str(root / "f"), str(root / "copy"))
    with pytest.raises(OperationFailed):
        backend.download(str(root / "f"), str(tmp_path / "got"))
    assert local_tree(str(root)) == {"f": FILE(b"content")}
    assert not (tmp_path / "got").exists()
    assert leftovers(tmp_path) == []


def test_copy_keeps_the_mode_and_times_of_a_file(backend: LocalBackend, root: Path) -> None:
    src = root / "src"
    src.write_bytes(b"x")
    src.chmod(0o751)
    os.utime(src, ns=(1_000_000_000_123_456_789, 1_100_000_000_987_654_321))
    backend.copy_within(str(src), str(root / "copy"))
    st = os.lstat(root / "copy")
    assert stat.S_IMODE(st.st_mode) == 0o751
    assert st.st_mtime_ns == 1_100_000_000_987_654_321


def test_copying_a_directory_keeps_its_mode(backend: LocalBackend, root: Path) -> None:
    (root / "src").mkdir()
    (root / "src").chmod(0o750)
    backend.copy_within(str(root / "src"), str(root / "copy"))
    assert stat.S_IMODE(os.lstat(root / "copy").st_mode) == 0o750


# -- special files ---------------------------------------------------------------------------


def _finishes(call: Callable[[], object], seconds: float = 5.0) -> BaseException | None:
    """Run ``call`` on a daemon thread; return what it raised (None if it returned).

    Fails the test if the call is still running after ``seconds``, e.g. blocked
    opening a FIFO that has no writer.
    """
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(f"call still blocked after {seconds}s")
    return outcome[0]


def test_special_files_are_refused_instead_of_read(
    backend: LocalBackend, root: Path, tmp_path: Path
) -> None:
    os.mkfifo(root / "fifo")
    for call in (
        lambda: backend.copy_within(str(root / "fifo"), str(root / "copy")),
        lambda: backend.upload(str(root / "fifo"), str(root / "up")),
        lambda: backend.download(str(root / "fifo"), str(tmp_path / "got")),
    ):
        assert isinstance(_finishes(call), OperationFailed)
    assert sorted(os.listdir(root)) == ["fifo"]


# -- registry: local locations ---------------------------------------------------------------


def test_parse_location_makes_a_local_path_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    backend, path = parse_location("some/dir", Settings())
    assert isinstance(backend, LocalBackend)
    assert path == os.path.join(os.getcwd(), "some", "dir")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("/a/b/../c/", "/a/c"), ("//double", "/double"), ("/", "/"), ("/x/./y", "/x/y")],
)
def test_parse_location_normalises_local_paths(spec: str, expected: str) -> None:
    backend, path = parse_location(spec, Settings())
    assert isinstance(backend, LocalBackend)
    assert path == expected


def test_a_local_name_starting_with_btty_is_written_with_a_dot_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    backend, path = parse_location("./btty:notes", Settings())
    assert isinstance(backend, LocalBackend)
    assert path == os.path.join(os.getcwd(), "btty:notes")


def test_every_local_location_shares_one_local_backend() -> None:
    first, _ = parse_location("/a", Settings())
    second, _ = parse_location("/b", Settings(btty_exec=False))
    assert first is second
    assert open_backend("local", Settings()) is first
    assert available()[0] == "local"


@pytest.mark.parametrize("spec", ["btty:", "btty:/abs", "btty:rel"])
def test_a_btty_location_while_btty_is_unregistered_names_the_btty_backend(
    monkeypatch: pytest.MonkeyPatch, spec: str
) -> None:
    monkeypatch.delitem(REGISTRY, "btty", raising=False)
    with pytest.raises(UnknownBackendError) as err:
        parse_location(spec, Settings())
    assert isinstance(err.value, LookupError)
    assert err.value.kind == "btty"
    assert "'btty'" in str(err.value)
    assert "local" in str(err.value)


def test_open_backend_of_an_unknown_kind_names_it() -> None:
    with pytest.raises(UnknownBackendError, match="'nope'"):
        open_backend("nope", Settings())


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("btty:", "/remote/home"),
        ("btty:/abs/path", "/abs/path"),
        ("btty:rel/path", "/remote/home/rel/path"),
        ("btty:../up", "/remote/up"),
        ("btty:/a/../b/", "/b"),
        ("btty://x", "/x"),
    ],
)
def test_btty_locations_resolve_against_the_remote_home(
    monkeypatch: pytest.MonkeyPatch, spec: str, expected: str
) -> None:
    stand_in = MemoryBackend(label="btty stand-in")
    seen: list[Settings] = []

    def factory(settings: Settings) -> Backend:
        seen.append(settings)
        return stand_in

    monkeypatch.setitem(REGISTRY, "btty", factory)
    settings = Settings(remote_home="/remote/home/")
    assert parse_location(spec, settings) == (stand_in, expected)
    assert seen == [settings]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("btty:", "/mnt/home0/wst/workspaces"),
        (
            "btty:jake.blow.clearwateranalytics.com",
            "/mnt/home0/wst/workspaces/jake.blow.clearwateranalytics.com",
        ),
    ],
)
def test_btty_locations_with_default_settings_resolve_against_the_workspaces_root(
    monkeypatch: pytest.MonkeyPatch, spec: str, expected: str
) -> None:
    """[D2] A plain btty: opens the workspaces root; the bscp folder is one name below it."""
    stand_in = MemoryBackend(label="btty stand-in")
    monkeypatch.setitem(REGISTRY, "btty", lambda settings: stand_in)
    assert parse_location(spec, Settings()) == (stand_in, expected)


# -- archives: what the shared contract cannot observe --------------------------------------
#
# TestLocalBackendContract runs the archive contract through the hooks only. These
# cases need the local filesystem itself: fifos and sockets, hard links, real
# permission bits, crafted tar members and gzip streams, EXDEV, symlinks in the
# destination path, and LocalBackend's own temporaries. The specification is
# Backend.pack, Backend.unpack and Backend.merge in twinfm.backend.base.

ARCHIVE_MTIME = 1_000_000_000

TarMember = tuple[tarfile.TarInfo, bytes | None]


def tar_members(archive: Path) -> dict[str, tarfile.TarInfo]:
    """Every member of the gzip tar at ``archive``, of any type, by name."""
    data = archive.read_bytes()
    assert data[:2] == b"\x1f\x8b", f"not gzip-compressed: {data[:16]!r}"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
    names = [info.name for info in members]
    assert len(names) == len(set(names)), f"duplicate members: {names}"
    return {info.name: info for info in members}


def member(
    name: str,
    kind: bytes = tarfile.REGTYPE,
    data: bytes = b"",
    *,
    linkname: str = "",
    mode: int | None = None,
    uid: int = 0,
    gid: int = 0,
    uname: str = "",
    gname: str = "",
) -> TarMember:
    """One crafted tar member: a file holding ``data``, or a directory, symlink or
    hard link (``kind``). Mode defaults to 0o644 / 0o755 / 0o777."""
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mtime = ARCHIVE_MTIME
    info.linkname = linkname
    info.uid, info.gid, info.uname, info.gname = uid, gid, uname, gname
    content = None
    if kind == tarfile.REGTYPE:
        info.size, content, default = len(data), data, 0o644
    elif kind == tarfile.DIRTYPE:
        default = 0o755
    else:
        default = 0o777
    info.mode = default if mode is None else mode
    return info, content


def tar_bytes(*members: TarMember) -> bytes:
    """An uncompressed PAX tar holding ``members`` in the order given."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, content in members:
            tar.addfile(info, None if content is None else io.BytesIO(content))
    return buffer.getvalue()


def gzip_tar(*members: TarMember) -> bytes:
    """``members`` in pack's format: a gzip-compressed (level 6) PAX tar."""
    return gzip.compress(tar_bytes(*members), compresslevel=6, mtime=0)


@pytest.fixture
def victim(root: Path) -> Path:
    """root/outside/victim: a file outside every dest, which no archive may change."""
    (root / "outside").mkdir()
    path = root / "outside" / "victim"
    path.write_bytes(b"precious")
    path.chmod(0o600)
    os.utime(path, (ARCHIVE_MTIME, ARCHIVE_MTIME))
    return path


def _identity(path: Path) -> tuple[bytes, int, int, int]:
    st = os.stat(path)
    return path.read_bytes(), stat.S_IMODE(st.st_mode), st.st_mtime_ns, st.st_nlink


# -- pack ------------------------------------------------------------------------------------


def test_pack_leaves_out_fifos_and_sockets_whether_selected_or_inside_a_selected_directory(
    backend: LocalBackend, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = root / "src"
    (src / "d").mkdir(parents=True)
    (src / "f").write_bytes(b"file")
    (src / "d" / "g").write_bytes(b"nested")
    os.symlink("f", src / "link")
    os.mkfifo(src / "fifo")
    os.mkfifo(src / "d" / "fifo")
    archive = tmp_path / "a.tgz"
    monkeypatch.chdir(src)  # an AF_UNIX address is short, so bind relative names
    with socket.socket(socket.AF_UNIX) as top, socket.socket(socket.AF_UNIX) as nested:
        top.bind("sock")
        nested.bind("d/sock")
        # A fifo without a writer blocks whoever opens it, so pack must not hang.
        outcome = _finishes(
            lambda: backend.pack(str(src), ["f", "d", "link", "fifo", "sock"], str(archive))
        )
    assert outcome is None, f"pack raised {outcome!r}"
    members = tar_members(archive)
    assert {name: (info.type, info.linkname) for name, info in members.items()} == {
        "f": (tarfile.REGTYPE, ""),
        "d": (tarfile.DIRTYPE, ""),
        "d/g": (tarfile.REGTYPE, ""),
        "link": (tarfile.SYMTYPE, "f"),
    }
    assert sorted(os.listdir(src)) == ["d", "f", "fifo", "link", "sock"]
    assert sorted(os.listdir(src / "d")) == ["fifo", "g", "sock"]


def test_pack_stores_uid_and_gid_0_and_no_owner_names_on_every_member_type(
    backend: LocalBackend, root: Path, tmp_path: Path
) -> None:
    # A file, a directory, a symlink and a second hard link to the file: none
    # carries an owner, not in the ustar fields and not in a PAX header.
    src = root / "src"
    (src / "d").mkdir(parents=True)
    (src / "f").write_bytes(b"1")
    os.link(src / "f", src / "d" / "hard")
    os.symlink("f", src / "link")
    archive = tmp_path / "a.tgz"
    backend.pack(str(src), ["f", "d", "link"], str(archive))
    members = tar_members(archive)
    assert sorted(members) == ["d", "d/hard", "f", "link"]
    for name, info in members.items():
        assert (info.uid, info.gid, info.uname, info.gname) == (0, 0, "", ""), name
        owner_headers = {"uid", "gid", "uname", "gname"} & set(info.pax_headers)
        assert not owner_headers, (name, info.pax_headers)


def test_pack_of_a_directory_that_is_the_current_directory_still_includes_it(
    backend: LocalBackend, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "d").mkdir()
    (root / "d" / "g").write_bytes(b"in the cwd")
    monkeypatch.chdir(root / "d")
    archive = tmp_path / "a.tgz"
    backend.pack(str(root), ["d"], str(archive))
    assert sorted(tar_members(archive)) == ["d", "d/g"]


@needs_non_root
def test_pack_that_cannot_read_a_file_raises_permission_denied_and_leaves_no_archive(
    backend: LocalBackend, root: Path, tmp_path: Path, locked: Callable[[Path, int], None]
) -> None:
    src = root / "src"
    (src / "d").mkdir(parents=True)
    (src / "ok").write_bytes(b"ok")
    (src / "d" / "secret").write_bytes(b"secret")
    locked(src / "d" / "secret", 0)
    archive = tmp_path / "a.tgz"
    with pytest.raises(PermissionDenied):
        backend.pack(str(src), ["ok", "d"], str(archive))
    assert not os.path.lexists(archive)
    assert sorted(os.listdir(tmp_path)) == ["root"]
    assert sorted(os.listdir(src)) == ["d", "ok"]
    assert leftovers(tmp_path) == []


# -- unpack ----------------------------------------------------------------------------------


def test_pack_then_unpack_reproduces_the_tree_byte_for_byte_with_exact_mtimes_and_safe_modes(
    backend: LocalBackend, root: Path, tmp_path: Path
) -> None:
    src = root / "src"
    (src / "d" / "deep").mkdir(parents=True)
    files = {
        "f": (b"top level", 0o600),
        "d/setuid": (b"#!/bin/sh\n", 0o4755),
        "d/shared": (b"group and other may write", 0o666),
        "d/empty": (b"", 0o644),
        "d/deep/blob": (bytes(range(256)) * 1024, 0o640),
    }
    for rel, (data, mode) in files.items():
        (src / rel).write_bytes(data)
        os.chmod(src / rel, mode)
    dirs = {"d": 0o1777, "d/deep": 0o2775}  # sticky; setgid with group write
    for rel, mode in dirs.items():
        os.chmod(src / rel, mode)
    modes = {**{rel: mode for rel, (_, mode) in files.items()}, **dirs}
    mtimes = {rel: 1_100_000_000 + 1_000 * i for i, rel in enumerate(sorted(modes))}
    for rel, mtime in mtimes.items():
        os.utime(src / rel, (mtime, mtime))
    on_disk = {rel: stat.S_IMODE(os.lstat(src / rel).st_mode) for rel in modes}
    assert on_disk == modes, "the test could not set up its modes"

    archive = tmp_path / "a.tgz"
    backend.pack(str(src), ["f", "d"], str(archive))
    recorded = tar_members(archive)
    assert {rel: recorded[rel].mode for rel in modes} == modes, "pack records the modes as they are"
    out = root / "out"
    backend.unpack(str(archive), str(out))

    assert not os.path.lexists(archive)
    assert local_tree(str(out)) == local_tree(str(src))
    assert {rel: stat.S_IMODE(os.lstat(out / rel).st_mode) for rel in modes} == {
        "f": 0o600,
        "d/setuid": 0o755,
        "d/shared": 0o644,
        "d/empty": 0o644,
        "d/deep/blob": 0o640,
        "d": 0o755,
        "d/deep": 0o755,
    }
    assert {rel: os.lstat(out / rel).st_mtime_ns for rel in modes} == {
        rel: mtime * 1_000_000_000 for rel, mtime in mtimes.items()
    }
    assert leftovers(tmp_path) == []


def test_hard_links_inside_the_selection_survive_pack_and_unpack(
    backend: LocalBackend, root: Path, tmp_path: Path
) -> None:
    src = root / "src"
    (src / "d").mkdir(parents=True)
    (src / "a").write_bytes(b"one inode")
    os.link(src / "a", src / "d" / "b")
    os.link(src / "a", src / "d" / "c")
    archive = tmp_path / "a.tgz"
    backend.pack(str(src), ["a", "d"], str(archive))
    backend.unpack(str(archive), str(root / "out"))
    assert local_tree(str(root / "out")) == {
        "a": FILE(b"one inode"),
        "d": DIR,
        "d/b": FILE(b"one inode"),
        "d/c": FILE(b"one inode"),
    }
    assert not os.path.lexists(archive)


# Each escape is aimed at root/x or at root/outside/victim, so a backend that
# wrongly lets it through still writes nowhere outside the test's directory.
UNSAFE_MEMBERS: dict[str, Callable[[Path], list[TarMember]]] = {
    "absolute-name": lambda victim: [member("/x", data=b"x")],
    "dot-dot": lambda victim: [member("../x", data=b"x")],
    "dot-dot-below-a-directory": lambda victim: [
        member("a", tarfile.DIRTYPE),
        member("a/../../x", data=b"x"),
    ],
    "under-a-symlink-leading-out": lambda victim: [
        member("esc", tarfile.SYMTYPE, linkname=".."),
        member("esc/x", data=b"x"),
    ],
    "hard-link-to-an-absolute-path": lambda victim: [
        member("h", tarfile.LNKTYPE, linkname=str(victim)),
        member("h", data=b"overwritten"),
    ],
    "hard-link-through-dot-dot": lambda victim: [
        member("h", tarfile.LNKTYPE, linkname="../outside/victim"),
        member("h", data=b"overwritten"),
    ],
    "hard-link-through-a-symlink-leading-out": lambda victim: [
        member("esc", tarfile.SYMTYPE, linkname=str(victim.parent)),
        member("h", tarfile.LNKTYPE, linkname="esc/victim"),
        member("h", data=b"overwritten"),
    ],
}


@pytest.mark.parametrize("unsafe", list(UNSAFE_MEMBERS))
def test_unpack_refuses_a_member_that_escapes_dest_and_keeps_the_archive(
    backend: LocalBackend, root: Path, victim: Path, unsafe: str
) -> None:
    archive = root / "a.tgz"
    archive.write_bytes(
        gzip_tar(member("ok", data=b"extracted first"), *UNSAFE_MEMBERS[unsafe](victim))
    )
    before = local_tree(str(root))
    victim_before = _identity(victim)
    with pytest.raises(OperationFailed):
        backend.unpack(str(archive), str(root / "dest"))
    assert _identity(victim) == victim_before
    assert local_tree(str(root)) == before  # no dest, the archive kept, nothing new
    assert leftovers(root.parent) == []


def test_unpack_of_a_hard_link_to_a_member_not_in_the_archive_raises_operation_failed(
    backend: LocalBackend, root: Path
) -> None:
    # pack stores a hard link only to a file it stored before it, so this is
    # not an archive pack could have written: OperationFailed, no dest, and the
    # archive kept.
    archive = root / "a.tgz"
    archive.write_bytes(
        gzip_tar(member("ok", data=b"1"), member("h", tarfile.LNKTYPE, linkname="missing"))
    )
    before = local_tree(str(root))
    with pytest.raises(OperationFailed):
        backend.unpack(str(archive), str(root / "dest"))
    assert local_tree(str(root)) == before


def _damaged_archive(damage: str) -> bytes:
    plain = tar_bytes(
        member("f", data=b"the original content\n" * 64), member("g", data=b"second file\n")
    )
    whole = gzip.compress(plain, compresslevel=6, mtime=0)
    if damage == "plain-tar":
        return plain
    if damage == "content-changed-under-the-original-crc":
        # As if bytes changed on the way: a valid deflate stream whose content
        # differs from what its CRC-32 trailer records.
        with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
            offset = tar.getmember("f").offset_data
        altered = bytearray(plain)
        altered[offset] ^= 0x20  # "the" -> "The"
        rewrapped = gzip.compress(bytes(altered), compresslevel=6, mtime=0)
        return rewrapped[:-8] + whole[-8:-4] + rewrapped[-4:]
    if damage == "size-changed-in-the-trailer":
        # The CRC-32 still matches; only the recorded length is wrong.
        return whole[:-4] + (len(plain) + 512).to_bytes(4, "little")
    if damage == "non-gzip-bytes-after-the-gzip-member":
        # The whole gzip member intact, then bytes that do not start another.
        return whole + b"not another gzip member\n"
    assert damage == "trailer-cut-off"
    return whole[:-8]  # every deflate byte, without the CRC-32 and size


@pytest.mark.parametrize(
    "damage",
    [
        "plain-tar",
        "content-changed-under-the-original-crc",
        "trailer-cut-off",
        "size-changed-in-the-trailer",
        "non-gzip-bytes-after-the-gzip-member",
    ],
)
def test_unpack_of_an_archive_pack_could_not_have_written_raises_operation_failed(
    backend: LocalBackend, root: Path, damage: str
) -> None:
    data = _damaged_archive(damage)
    archive = root / "a.tgz"
    archive.write_bytes(data)
    dest = root / "dest"
    try:
        backend.unpack(str(archive), str(dest))
    except OperationFailed:
        pass
    else:
        extracted = local_tree(str(dest))
        f = extracted.get("f", ("missing", None))[1]
        pytest.fail(
            f"unpack accepted the damaged archive ({damage}) and removed it; "
            f"it extracted {sorted(extracted)}, with f beginning {f[:24] if f else f!r}"
        )
    assert local_tree(str(root)) == {"a.tgz": FILE(data)}
    assert leftovers(root) == []


def test_unpack_never_restores_ownership_even_when_run_as_root(
    backend: LocalBackend, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tarfile changes owners only when the effective uid is 0. The test pretends
    # to be root and records chown calls instead of making them.
    calls: list[tuple[str, int, int]] = []

    def record(path: object, uid: int, gid: int, *args: object, **kwargs: object) -> None:
        calls.append((os.fspath(path), uid, gid))  # type: ignore[arg-type]

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", record)
    monkeypatch.setattr(os, "lchown", record)
    owner = {"uid": 1234, "gid": 5678, "uname": "root", "gname": "wheel"}
    data = gzip_tar(
        member("d", tarfile.DIRTYPE, **owner),
        member("d/f", data=b"1", **owner),
        member("d/link", tarfile.SYMTYPE, linkname="f", **owner),
        member("d/hard", tarfile.LNKTYPE, linkname="d/f", **owner),
    )
    # Harness check: under this pretence, tarfile on its own restores owners.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(tmp_path / "trusted", filter="fully_trusted")
    assert any((uid, gid) != (-1, -1) for _, uid, gid in calls), calls
    calls.clear()

    archive = root / "a.tgz"
    archive.write_bytes(data)
    backend.unpack(str(archive), str(root / "dest"))
    assert local_tree(str(root / "dest")) == {
        "d": DIR,
        "d/f": FILE(b"1"),
        "d/link": LINK("f"),
        "d/hard": FILE(b"1"),
    }
    restored = [(path, uid, gid) for path, uid, gid in calls if (uid, gid) != (-1, -1)]
    assert restored == []


# -- merge -----------------------------------------------------------------------------------


def test_merge_across_filesystems_raises_operation_failed_and_copies_nothing(
    backend: LocalBackend, root: Path, cross_device: list[tuple[str, str]]
) -> None:
    # merge only renames: EXDEV is an error, never a reason to copy.
    (root / "f").write_bytes(b"file")
    (root / "s" / "sub").mkdir(parents=True)
    (root / "s" / "sub" / "x").write_bytes(b"x")
    (root / "t" / "sub").mkdir(parents=True)
    (root / "t" / "sub" / "w").write_bytes(b"w")
    before = local_tree(str(root))
    with pytest.raises(OperationFailed):
        backend.merge(str(root / "f"), str(root / "t" / "f"))
    with pytest.raises(OperationFailed):
        backend.merge(str(root / "s"), str(root / "t"))
    assert local_tree(str(root)) == before
    assert len(cross_device) == 2
    assert leftovers(root) == []


def test_merge_into_a_target_reached_through_a_symlink_merges_into_the_real_directory(
    backend: LocalBackend, root: Path
) -> None:
    # The orchestrator stages inside the target and merges each item into
    # target/<name>; here the target is spelt through a symlink to it.
    (root / "t" / "sub").mkdir(parents=True)
    (root / "t" / "sub" / "w").write_bytes(b"kept")
    os.symlink("t", root / "to-t")
    stage = root / "to-t" / ".stage"
    (stage / "sub").mkdir(parents=True)
    (stage / "sub" / "x").write_bytes(b"new")
    (stage / "fresh").mkdir()
    (stage / "fresh" / "y").write_bytes(b"y")
    (stage / "file").write_bytes(b"file")
    for name in ("sub", "fresh", "file"):
        backend.merge(str(stage / name), str(root / "to-t" / name))
    assert local_tree(str(root)) == {
        "to-t": LINK("t"),
        "t": DIR,
        "t/.stage": DIR,
        "t/sub": DIR,
        "t/sub/w": FILE(b"kept"),
        "t/sub/x": FILE(b"new"),
        "t/fresh": DIR,
        "t/fresh/y": FILE(b"y"),
        "t/file": FILE(b"file"),
    }
    assert leftovers(root) == []


def test_merge_of_a_directory_onto_a_symlink_to_a_directory_raises_and_follows_nothing(
    backend: LocalBackend, root: Path
) -> None:
    (root / "t").mkdir()
    (root / "t" / "inside").write_bytes(b"untouched")
    os.symlink("t", root / "to-t")
    (root / "s").mkdir()
    (root / "s" / "x").write_bytes(b"x")
    before = local_tree(str(root))
    with pytest.raises(NotADirectory) as err:
        backend.merge(str(root / "s"), str(root / "to-t"))
    assert err.value.path == str(root / "to-t")
    assert local_tree(str(root)) == before


def test_merge_of_a_staging_directory_inside_the_target_into_the_target(
    backend: LocalBackend, root: Path
) -> None:
    # src below dst is not a merge into itself: only dst at or below src is.
    t = root / "t"
    (t / "sub").mkdir(parents=True)
    (t / "a").write_bytes(b"old a")
    (t / "keep").write_bytes(b"kept")
    (t / "sub" / "w").write_bytes(b"kept w")
    stage = t / ".twinfm-stage"
    (stage / "sub").mkdir(parents=True)
    (stage / "a").write_bytes(b"new a")
    (stage / "sub" / "x").write_bytes(b"new x")
    backend.merge(str(stage), str(t))
    assert local_tree(str(root)) == {
        "t": DIR,
        "t/a": FILE(b"new a"),
        "t/keep": FILE(b"kept"),
        "t/sub": DIR,
        "t/sub/w": FILE(b"kept w"),
        "t/sub/x": FILE(b"new x"),
    }


@pytest.mark.parametrize("dst", ["here/s", "to-s/sub", "to-s/inside", "to-sub/deeper"])
def test_merge_into_itself_or_its_subtree_through_a_symlink_raises_before_anything_moves(
    backend: LocalBackend, root: Path, dst: str
) -> None:
    # here -> ".", to-s -> s, to-sub -> s/sub: each dst is s or lies in it.
    (root / "s" / "sub").mkdir(parents=True)
    (root / "s" / "f").write_bytes(b"1")
    (root / "s" / "empty").mkdir()
    os.symlink(".", root / "here")
    os.symlink("s", root / "to-s")
    os.symlink("s/sub", root / "to-sub")
    before = local_tree(str(root))
    with pytest.raises(OperationFailed):
        backend.merge(str(root / "s"), str(root / dst))
    assert local_tree(str(root)) == before
    assert leftovers(root) == []


@pytest.mark.parametrize("dst", ["S", "S/sub", "S/inside"])
def test_merge_into_itself_or_its_subtree_through_a_case_variant_raises_before_anything_moves(
    backend: LocalBackend, case_insensitive_root: Path, dst: str
) -> None:
    root = case_insensitive_root
    (root / "s" / "sub").mkdir(parents=True)
    (root / "s" / "f").write_bytes(b"1")
    (root / "s" / "empty").mkdir()
    before = local_tree(str(root))
    with pytest.raises(OperationFailed):
        backend.merge(str(root / "s"), str(root / dst))
    assert local_tree(str(root)) == before
    assert leftovers(root) == []


def test_merge_of_a_file_onto_another_hard_link_of_it_leaves_only_the_destination(
    backend: LocalBackend, root: Path
) -> None:
    # rename(2) does nothing when both names link one file; merge must still
    # replace dst by src, so the name src goes and src's directory can be removed.
    (root / "s").mkdir()
    (root / "t").mkdir()
    (root / "t" / "a").write_bytes(b"one inode")
    os.link(root / "t" / "a", root / "s" / "a")
    (root / "s" / "b").write_bytes(b"b")
    (root / "x").write_bytes(b"top")
    os.link(root / "x", root / "y")
    backend.merge(str(root / "s"), str(root / "t"))
    backend.merge(str(root / "x"), str(root / "y"))
    assert local_tree(str(root)) == {
        "t": DIR,
        "t/a": FILE(b"one inode"),
        "t/b": FILE(b"b"),
        "y": FILE(b"top"),
    }


@pytest.mark.parametrize("links", [1, 2], ids=["one-link", "hard-linked"])
def test_merge_of_a_file_onto_its_own_entry_through_a_symlink_loses_nothing(
    backend: LocalBackend, root: Path, links: int
) -> None:
    # root/here/a is root/a itself: not a separate hard link to remove.
    (root / "a").write_bytes(b"data")
    os.symlink(".", root / "here")
    expected = {"a": FILE(b"data"), "here": LINK(".")}
    if links == 2:
        os.link(root / "a", root / "b")
        expected["b"] = FILE(b"data")
    backend.merge(str(root / "a"), str(root / "here" / "a"))
    assert local_tree(str(root)) == expected
    assert leftovers(root) == []


def test_merge_of_a_hard_linked_file_to_a_case_variant_of_its_name_keeps_it(
    backend: LocalBackend, case_insensitive_root: Path
) -> None:
    # "name" is the entry "Name", not the separate link "other".
    root = case_insensitive_root
    (root / "Name").write_bytes(b"data")
    os.link(root / "Name", root / "other")
    backend.merge(str(root / "Name"), str(root / "name"))
    names = sorted(os.listdir(root))
    assert [name.casefold() for name in names] == ["name", "other"], names
    assert (root / "name").read_bytes() == (root / "other").read_bytes() == b"data"
    assert leftovers(root) == []
