"""LocalBackend: the shared contract on tmp_path, plus local-only behaviour.

The local-only cases are symlinks, the EXDEV fallback, permissions, the
mapping of raw OSErrors, atomic writes, special files and the registry's
local locations.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
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
