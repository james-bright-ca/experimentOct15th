"""MemoryBackend: the shared contract with its default, reduced and
transfer-only caps, plus its symlink resolution, seeding helpers and local side.

MemoryBackend does not implement pack, unpack or merge, so no contract variant
here claims Cap.ARCHIVE: its default caps are every Cap except ARCHIVE."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from tests.backend.contract import DIR, FILE, LINK, BackendContract, Node
from twinfm.backend.base import (
    ALL_CAPS,
    Backend,
    Cap,
    DirectoryNotEmpty,
    IsADirectory,
    NotADirectory,
    NotFound,
    OperationFailed,
    PermissionDenied,
    Unsupported,
)
from twinfm.backend.memory import MemoryBackend

HOME = "/home/tester"

DEFAULT_CAPS = ALL_CAPS - {Cap.ARCHIVE}
"""MemoryBackend's default caps: every Cap except ARCHIVE."""


class _MemoryContract(BackendContract):
    """Contract plumbing for MemoryBackend; subclasses choose the caps."""

    caps: frozenset[Cap] = DEFAULT_CAPS

    @pytest.fixture
    def backend(self) -> MemoryBackend:
        return MemoryBackend(self.caps, home=HOME)

    @pytest.fixture
    def root(self, backend: MemoryBackend) -> str:
        root = f"{HOME}/root"
        backend.add_dir(root)
        return root

    def seed_dir(self, backend: Backend, path: str) -> None:
        assert isinstance(backend, MemoryBackend)
        backend.add_dir(path)

    def seed_file(self, backend: Backend, path: str, data: bytes) -> None:
        assert isinstance(backend, MemoryBackend)
        backend.add_file(path, data)

    def seed_symlink(self, backend: Backend, path: str, target: str) -> None:
        assert isinstance(backend, MemoryBackend)
        backend.add_symlink(path, target)

    def observe(self, backend: Backend, root: str) -> dict[str, Node]:
        assert isinstance(backend, MemoryBackend)
        prefix = root.rstrip("/") + "/"
        tree: dict[str, Node] = {}
        for path, value in backend.snapshot().items():
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix) :]
            if value is None:
                tree[rel] = DIR
            elif isinstance(value, bytes):
                tree[rel] = FILE(value)
            else:
                tree[rel] = LINK(value)
        return tree


class TestMemoryBackendContract(_MemoryContract):
    caps = DEFAULT_CAPS


class TestMemoryBackendWithoutDeleteContract(_MemoryContract):
    caps = DEFAULT_CAPS - {Cap.DELETE}


class TestTransferOnlyMemoryBackendContract(_MemoryContract):
    caps = frozenset({Cap.UPLOAD, Cap.DOWNLOAD})


@pytest.mark.parametrize(
    "suite",
    [
        TestMemoryBackendContract,
        TestMemoryBackendWithoutDeleteContract,
        TestTransferOnlyMemoryBackendContract,
    ],
)
def test_the_contract_suite_is_inherited_unchanged(suite: type[BackendContract]) -> None:
    contract_tests = {name for name in dir(BackendContract) if name.startswith("test_")}
    assert contract_tests
    for name in contract_tests:
        assert getattr(suite, name) is getattr(BackendContract, name), name


# -- caps ([A] criterion: no Cap.DELETE) --------------------------------------------------


def test_without_delete_every_delete_raises_unsupported_and_changes_nothing() -> None:
    backend = MemoryBackend(ALL_CAPS - {Cap.DELETE})
    backend.add_file("/d/f", b"1")
    backend.add_symlink("/d/link", "f")
    backend.add_dir("/empty")
    before = backend.snapshot()
    for path, recursive in [("/d/f", False), ("/d/link", False), ("/empty", False), ("/d", True)]:
        with pytest.raises(Unsupported) as err:
            backend.delete(path, recursive=recursive)
        assert err.value.path == path
        assert err.value.backend_id == "memory"
    assert backend.snapshot() == before


def test_caps_label_and_home_are_fixed_at_construction() -> None:
    backend = MemoryBackend([Cap.LIST, Cap.LIST, Cap.STAT], home="/srv/data", label="remote")
    assert backend.caps == frozenset({Cap.LIST, Cap.STAT})
    assert (backend.id, backend.label, backend.home()) == ("memory", "remote", "/srv/data")
    assert backend.snapshot() == {"/srv": None, "/srv/data": None}
    default = MemoryBackend()
    assert (default.caps, default.label, default.home()) == (DEFAULT_CAPS, "memory", "/")
    assert default.snapshot() == {}


# -- archives and the listing cache ([C0] criteria) ---------------------------------------------


def test_default_caps_are_every_cap_except_archive() -> None:
    assert MemoryBackend().caps == ALL_CAPS - {Cap.ARCHIVE}
    assert Cap.ARCHIVE in ALL_CAPS
    assert Cap("archive") is Cap.ARCHIVE
    # Caps passed explicitly are still taken as given.
    assert MemoryBackend(ALL_CAPS).caps == ALL_CAPS


def test_archive_attributes_default_to_none() -> None:
    backend = MemoryBackend()
    assert (backend.archive_dir, backend.archive_max_bytes) == (None, None)


def test_pack_unpack_and_merge_raise_unsupported_naming_archive_and_change_nothing() -> None:
    backend = MemoryBackend(home="/srv")
    backend.add_file("/srv/d/f", b"1")
    backend.add_file("/srv/a.tgz", b"\x1f\x8b not really")
    backend.add_dir("/srv/t")
    before = backend.snapshot()
    calls = [
        ("/srv/d", lambda: backend.pack("/srv/d", ["f"], "/srv/new.tgz")),
        ("/srv/d", lambda: backend.pack("/srv/d", ["f"], "/srv/a.tgz")),  # would be AlreadyExists
        ("/srv/gone", lambda: backend.pack("/srv/gone", ["x"], "/srv/new.tgz")),
        ("/srv/a.tgz", lambda: backend.unpack("/srv/a.tgz", "/srv/out")),
        ("/srv/gone.tgz", lambda: backend.unpack("/srv/gone.tgz", "/srv/out")),
        ("/srv/d", lambda: backend.merge("/srv/d", "/srv/t")),
        ("/srv/d", lambda: backend.merge("/srv/d", "/srv/d/inside")),  # would be OperationFailed
        ("/srv/gone", lambda: backend.merge("/srv/gone", "/srv/t")),
    ]
    for path, call in calls:
        with pytest.raises(Unsupported, match="archive") as err:
            call()
        assert (err.value.path, err.value.backend_id) == (path, "memory")
    assert backend.snapshot() == before


def test_delete_many_deletes_in_order_and_raises_the_first_failure() -> None:
    backend = MemoryBackend()
    for name in ("a", "b", "c"):
        backend.add_file(f"/{name}", name.encode())
    backend.add_file("/full/f", b"f")
    with pytest.raises(NotFound) as err:
        backend.delete_many(["/a", "/missing", "/b"])
    assert err.value.path == "/missing"
    assert backend.snapshot() == {"/b": b"b", "/c": b"c", "/full": None, "/full/f": b"f"}
    with pytest.raises(DirectoryNotEmpty) as not_empty:
        backend.delete_many(["/b", "/full", "/c"])
    assert not_empty.value.path == "/full"
    assert backend.snapshot() == {"/c": b"c", "/full": None, "/full/f": b"f"}
    backend.delete_many(["/full/f", "/full", "/c"])
    assert backend.snapshot() == {}


def test_delete_many_without_delete_raises_unsupported_even_for_no_paths() -> None:
    backend = MemoryBackend(DEFAULT_CAPS - {Cap.DELETE}, home="/h")
    backend.add_file("/h/f", b"1")
    before = backend.snapshot()
    for paths in (["/h/f"], ["/h/missing"], []):
        with pytest.raises(Unsupported, match="delete"):
            backend.delete_many(paths)
    assert backend.snapshot() == before


@pytest.mark.parametrize("path", [None, "/", "/d", "/d/e", "/f", "/missing"])
def test_invalidate_is_a_no_op_without_any_capability(path: str | None) -> None:
    backend = MemoryBackend(set())
    backend.add_file("/d/e/f", b"1")
    backend.add_file("/f", b"2")
    before = backend.snapshot()
    assert backend.invalidate(path) is None
    assert backend.snapshot() == before


def test_unsupported_names_the_missing_capability() -> None:
    backend = MemoryBackend({Cap.UPLOAD, Cap.DOWNLOAD}, label="transfer-only")
    with pytest.raises(Unsupported, match="list") as err:
        backend.list_dir("/")
    assert "transfer-only" in str(err.value)


# -- the seeding helpers ignore caps -----------------------------------------------------------


def test_helpers_seed_and_inspect_without_any_capability() -> None:
    backend = MemoryBackend(set())
    backend.add_file("/a/b/f", b"data", mtime=123.0, mode=0o600)
    backend.add_symlink("/a/link", "b/f")
    backend.add_dir("/a/c")
    assert backend.read_file("/a/b/f") == b"data"
    assert backend.read_file("/a/link") == b"data"
    assert backend.snapshot() == {
        "/a": None,
        "/a/b": None,
        "/a/b/f": b"data",
        "/a/link": "b/f",
        "/a/c": None,
    }
    backend.add_file("/a/b/f", b"replaced")
    assert backend.read_file("/a/b/f") == b"replaced"
    with pytest.raises(IsADirectory):
        backend.add_file("/a/c", b"x")
    with pytest.raises(IsADirectory):
        backend.read_file("/a/b")
    with pytest.raises(NotFound):
        backend.read_file("/a/missing")


def test_seeded_metadata_is_reported() -> None:
    backend = MemoryBackend()
    backend.add_file("/f", b"abc", mtime=1_234_567_890.5, mode=0o640)
    entry = backend.stat("/f")
    assert (entry.size, entry.mtime, entry.mode) == (3, 1_234_567_890.5, 0o640)


# -- POSIX symlink resolution --------------------------------------------------------------------


@pytest.fixture
def linked() -> MemoryBackend:
    """/real/sub/f, and links to /real by relative, absolute and dotted targets."""
    backend = MemoryBackend()
    backend.add_file("/real/sub/f", b"content")
    backend.add_file("/other/g", b"other")
    backend.add_symlink("/rel", "real")
    backend.add_symlink("/abs", "/real")
    backend.add_symlink("/real/sub/up", "../../other")
    backend.add_symlink("/chain1", "chain2")
    backend.add_symlink("/chain2", "chain3")
    backend.add_symlink("/chain3", "rel/sub")
    return backend


@pytest.mark.parametrize(
    "path", ["/rel/sub", "/abs/sub", "/chain1", "/rel/sub/up/../real/sub", "/abs/../real/sub"]
)
def test_symlinks_in_the_middle_of_a_path_are_followed(linked: MemoryBackend, path: str) -> None:
    names = sorted(e.name for e in linked.list_dir(path))
    assert names == ["f", "up"]
    assert linked.read_file(f"{path}/f") == b"content"


def test_a_relative_target_with_dot_dot_resolves_from_the_links_directory(
    linked: MemoryBackend,
) -> None:
    entry = linked.stat("/real/sub/up")
    assert (entry.kind, entry.target_kind) == ("symlink", "dir")
    assert [e.name for e in linked.list_dir("/real/sub/up")] == ["g"]
    assert linked.read_file("/abs/sub/up/g") == b"other"


def test_mutations_through_symlinked_directories_land_in_the_real_directory(
    linked: MemoryBackend, tmp_path: Path
) -> None:
    linked.mkdir("/rel/sub/made")
    src = tmp_path / "src"
    src.write_bytes(b"uploaded")
    linked.upload(str(src), "/abs/sub/made/up.txt")
    linked.rename("/abs/sub/f", "/rel/sub/renamed")
    linked.copy_within("/chain1/renamed", "/abs/copied")
    snapshot = linked.snapshot()
    assert snapshot["/real/sub/made"] is None
    assert snapshot["/real/sub/made/up.txt"] == b"uploaded"
    assert snapshot["/real/sub/renamed"] == b"content"
    assert snapshot["/real/copied"] == b"content"
    assert "/real/sub/f" not in snapshot
    assert snapshot["/rel"] == "real" and snapshot["/abs"] == "/real"


def test_a_symlink_loop_fails_to_resolve_but_can_be_described_and_deleted() -> None:
    backend = MemoryBackend()
    backend.add_symlink("/a", "b")
    backend.add_symlink("/b", "a")
    backend.add_symlink("/self", "self")
    with pytest.raises(OperationFailed):
        backend.list_dir("/a")
    with pytest.raises(OperationFailed):
        backend.read_file("/self")
    with pytest.raises(OperationFailed):
        backend.mkdir("/self/child")
    entry = backend.stat("/a")
    assert (entry.kind, entry.link_target, entry.target_kind) == ("symlink", "b", None)
    assert {e.name: e.target_kind for e in backend.list_dir("/")} == {
        "a": None,
        "b": None,
        "self": None,
    }
    backend.delete("/self")
    assert backend.snapshot() == {"/a": "b", "/b": "a"}


def test_a_path_through_a_file_raises_not_a_directory() -> None:
    backend = MemoryBackend()
    backend.add_file("/f", b"x")
    with pytest.raises(NotADirectory):
        backend.stat("/f/child")
    with pytest.raises(NotADirectory):
        backend.list_dir("/f")


def test_delete_refuses_the_root_directory() -> None:
    backend = MemoryBackend()
    backend.add_file("/f", b"x")
    with pytest.raises(OperationFailed):
        backend.delete("/", recursive=True)
    assert backend.snapshot() == {"/f": b"x"}


# -- the local side of upload and download ---------------------------------------------------------


def test_local_side_failures_surface_as_fs_errors(tmp_path: Path) -> None:
    backend = MemoryBackend()
    backend.add_file("/f", b"x")
    with pytest.raises(OperationFailed):
        backend.upload(f"{tmp_path}/bad\0name", "/up")
    with pytest.raises(OperationFailed):
        backend.download("/f", f"{tmp_path}/bad\0name")
    with pytest.raises(OperationFailed):
        backend.download("/f", f"{tmp_path}/{'n' * 1000}")
    assert os.listdir(tmp_path) == []
    assert backend.snapshot() == {"/f": b"x"}


def test_uploading_a_local_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    backend = MemoryBackend()
    outcome: list[BaseException | None] = []

    def upload() -> None:
        try:
            backend.upload(str(tmp_path / "fifo"), "/up")
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=upload, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "upload blocked opening a FIFO"
    assert isinstance(outcome[0], OperationFailed)
    assert backend.snapshot() == {}


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="permission bits do not bind root"
)
def test_download_into_a_read_only_local_directory_raises_permission_denied(
    tmp_path: Path,
) -> None:
    backend = MemoryBackend()
    backend.add_file("/f", b"x")
    dest = tmp_path / "dest"
    dest.mkdir()
    dest.chmod(0o555)
    try:
        with pytest.raises(PermissionDenied) as err:
            backend.download("/f", str(dest / "got"))
        assert err.value.path == str(dest / "got")
        assert os.listdir(dest) == []
    finally:
        dest.chmod(0o755)


# -- thread safety -----------------------------------------------------------------------------------


def test_concurrent_operations_from_worker_threads_are_safe(tmp_path: Path) -> None:
    backend = MemoryBackend()
    backend.add_dir("/shared")
    src = tmp_path / "src"
    src.write_bytes(b"payload")
    writers, rounds = 4, 100
    errors: list[BaseException] = []
    start = threading.Barrier(writers + 1)

    def writer(n: int) -> None:
        try:
            start.wait()
            for i in range(rounds):
                backend.mkdir(f"/shared/w{n}-{i}")
                backend.upload(str(src), f"/shared/w{n}-{i}/f")
                backend.rename(f"/shared/w{n}-{i}/f", f"/shared/w{n}-{i}/g")
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    def reader() -> None:
        try:
            start.wait()
            for _ in range(50):
                backend.walk("/shared")
                backend.list_dir("/shared")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(writers)]
    threads.append(threading.Thread(target=reader))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    expected = {f"w{n}-{i}" for n in range(writers) for i in range(rounds)}
    assert {e.name for e in backend.list_dir("/shared")} == expected
    assert all(backend.read_file(f"/shared/{name}/g") == b"payload" for name in expected)
