"""BackendContract: the behaviour every Backend shares, written once.

The specification is the docstrings of ``twinfm.backend.base``. Every backend
(LocalBackend, MemoryBackend, BttyBackend over the fake runner) runs this same
suite by subclassing it.

How to subclass
---------------
Subclass it in a ``test_*.py`` module under a ``Test...`` name and provide:

fixtures
    ``backend``
        The Backend under test. Its ``caps`` decide what runs: a test that
        needs a capability the backend lacks is skipped, and
        ``test_missing_capability_raises_unsupported_and_changes_nothing``
        checks every missing capability instead.
    ``root``
        An existing, empty directory on that backend, as a normalised absolute
        path. Every test works below ``root`` and nowhere else.

hooks
    These seed and observe the store directly. They bypass the backend, so
    they work whatever its caps are.

    ``seed_dir(backend, path)``
        Create a directory whose parent exists.
    ``seed_file(backend, path, data)``
        Create or replace a regular file holding ``data``.
    ``seed_symlink(backend, path, target)``
        Create a symlink with link text ``target``.
    ``observe(backend, root)``
        Map every path below ``root``, relative to it in POSIX form, to
        ``DIR``, ``FILE(data)`` or ``LINK(target)``. Symlinks are not
        followed.

attribute
    ``supports_symlinks``
        Defaults to True. Set it to False for a store that cannot hold
        symlinks; tests that need one are then skipped.

What the suite never does
-------------------------
* It never uses ``os`` on the backend's paths. All observation goes through
  the backend or the hooks.
* It never deletes ``/`` and never touches anything outside ``root``.
  Root-refusal tests belong in each subclass module, written so that a
  broken backend cannot do damage.
* The local side of upload and download is the ``local_tmp`` directory. That
  directory is on the local filesystem by definition, so ``os`` is used there.

Sibling order from ``list_dir`` and ``walk`` is unspecified, so the suite
compares sets. For ``walk`` it checks only that each directory comes before
its contents.

Archives (Cap.ARCHIVE)
----------------------
The pack / unpack / merge tests run only on a backend that claims
Cap.ARCHIVE; every other backend checks instead that the three raise
Unsupported and change nothing. They still use only the hooks: an archive
``pack`` wrote is read back with ``observe`` (its bytes) and opened with
``tarfile`` in memory, and an archive for ``unpack`` is built in memory with
``make_archive`` and put on the store with ``seed_file``. Every archive and
every extraction stays below ``root``, including what a broken backend would
write for a hostile member name. Permission bits and mtimes are compared
through ``stat``, so those tests also need Cap.STAT.
"""

from __future__ import annotations

import gzip
import io
import os
import posixpath
import random
import tarfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from twinfm.backend.base import (
    AlreadyExists,
    Backend,
    Cap,
    DirectoryNotEmpty,
    Entry,
    IsADirectory,
    NotADirectory,
    NotFound,
    OperationFailed,
    Unsupported,
)

__all__ = [
    "DIR",
    "FILE",
    "HOSTILE_NAMES",
    "LINK",
    "BackendContract",
    "Node",
    "archive_members",
    "make_archive",
]

Node = tuple[str, bytes | str | None]
"""What ``observe`` reports for one path: DIR, FILE(data) or LINK(target)."""

DIR: Node = ("dir", None)


def FILE(data: bytes) -> Node:  # noqa: N802 - reads like a constructor in tree literals
    return ("file", data)


def LINK(target: str) -> Node:  # noqa: N802
    return ("symlink", target)


HOSTILE_NAMES = [
    "with space",
    "it's",
    'double"quote',
    "new\nline",
    "tab\there",
    "-leading-dash",
    "--",
    "star*?[ab]",
    "$(echo pwned)",
    "semi;colon&amp|pipe>",
    "back\\slash",
    " padded ",
    "ünïcødé-日本語",
]
"""File names that break naive shell quoting, globbing or option parsing."""


ARCHIVE_MTIME = 1_000_000_000
"""The mtime ``make_archive`` records unless told otherwise (2001-09-09)."""

GZIP_MAGIC = b"\x1f\x8b"


def make_archive(
    members: Sequence[tuple[str, Node] | tuple[str, Node, int]], mtime: int = ARCHIVE_MTIME
) -> bytes:
    """A gzip tar in the format ``Backend.pack`` documents ("w:gz",
    compresslevel 6, PAX, ownership zeroed), holding ``members`` in the order
    given: ``(name, node)`` or ``(name, node, mode)``. The default mode is
    0o755 for a directory, 0o644 for a file and 0o777 for a symlink."""
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", compresslevel=6, format=tarfile.PAX_FORMAT
    ) as tar:
        for member in members:
            name, (kind, value) = member[0], member[1]
            info = tarfile.TarInfo(name)
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            content = None
            if kind == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
            elif kind == "file":
                assert isinstance(value, bytes)
                info.type, info.mode, info.size = tarfile.REGTYPE, 0o644, len(value)
                content = io.BytesIO(value)
            else:
                assert isinstance(value, str)
                info.type, info.mode, info.linkname = tarfile.SYMTYPE, 0o777, value
            if len(member) == 3:
                info.mode = member[2]  # type: ignore[misc]
            tar.addfile(info, content)
    return buffer.getvalue()


def archive_members(data: bytes) -> list[tuple[tarfile.TarInfo, Node]]:
    """Every member of the gzip tar ``data``, in archive order, with what it
    holds as a contract node (DIR, FILE(content) or LINK(link text)). Any other
    member type fails the test."""
    assert data[:2] == GZIP_MAGIC, f"not gzip-compressed: {data[:16]!r}"
    result: list[tuple[tarfile.TarInfo, Node]] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for info in tar.getmembers():
            if info.isdir():
                node = DIR
            elif info.isreg():
                extracted = tar.extractfile(info)
                assert extracted is not None
                node = FILE(extracted.read())
            elif info.issym():
                node = LINK(info.linkname)
            else:
                raise AssertionError(f"unexpected member type {info.type!r} for {info.name!r}")
            result.append((info, node))
    return result


def incompressible(size: int, seed: int) -> bytes:
    """``size`` deterministic pseudo-random bytes (they do not compress, so a
    cut through the archive lands inside a member)."""
    return random.Random(seed).randbytes(size)


class BackendContract:
    """The shared Backend suite. See the module docstring for how to subclass it."""

    supports_symlinks = True

    # -- hooks: subclasses implement these ---------------------------------------

    def seed_dir(self, backend: Backend, path: str) -> None:
        raise NotImplementedError(f"{type(self).__name__} must implement seed_dir")

    def seed_file(self, backend: Backend, path: str, data: bytes) -> None:
        raise NotImplementedError(f"{type(self).__name__} must implement seed_file")

    def seed_symlink(self, backend: Backend, path: str, target: str) -> None:
        raise NotImplementedError(f"{type(self).__name__} must implement seed_symlink")

    def observe(self, backend: Backend, root: str) -> dict[str, Node]:
        raise NotImplementedError(f"{type(self).__name__} must implement observe")

    # -- fixtures and helpers shared by every subclass ---------------------------

    @pytest.fixture
    def local_tmp(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        """An empty local directory for the local side of upload and download."""
        return tmp_path_factory.mktemp("contract-local")

    @staticmethod
    def at(root: str, rel: str) -> str:
        return posixpath.join(root, rel)

    @staticmethod
    def need(backend: Backend, *caps: Cap) -> None:
        missing = [cap.name for cap in caps if cap not in backend.caps]
        if missing:
            pytest.skip(f"{backend.label} lacks {', '.join(missing)}")

    def need_symlinks(self) -> None:
        if not self.supports_symlinks:
            pytest.skip("this store cannot hold symlinks")

    def seed(self, backend: Backend, root: str, tree: Mapping[str, Node]) -> None:
        """Create ``tree`` (relative path -> DIR / FILE / LINK) below ``root``, parents first."""
        for rel in sorted(tree, key=lambda r: (r.count("/"), r)):
            kind, value = tree[rel]
            path = self.at(root, rel)
            if kind == "dir":
                self.seed_dir(backend, path)
            elif kind == "file":
                assert isinstance(value, bytes)
                self.seed_file(backend, path, value)
            else:
                assert isinstance(value, str)
                self.need_symlinks()
                self.seed_symlink(backend, path, value)

    @staticmethod
    def local_file(local_tmp: Path, name: str, data: bytes) -> str:
        path = local_tmp / name
        path.write_bytes(data)
        return str(path)

    @staticmethod
    def by_name(entries: list[Entry]) -> dict[str, Entry]:
        names = [e.name for e in entries]
        assert len(names) == len(set(names)), f"duplicate names in {names}"
        return {e.name: e for e in entries}

    @staticmethod
    def assert_permission_bits(entry: Entry) -> None:
        assert entry.mode is None or entry.mode & ~0o7777 == 0, oct(entry.mode or 0)

    def archive_bytes(self, backend: Backend, root: str, rel: str) -> bytes:
        """The content of the regular file ``rel`` below ``root``, read with ``observe``."""
        kind, data = self.observe(backend, root)[rel]
        assert kind == "file" and isinstance(data, bytes), f"{rel} is a {kind}, not a file"
        return data

    @staticmethod
    def below(tree: Mapping[str, Node], prefix: str) -> dict[str, Node]:
        """The part of ``tree`` below the relative directory ``prefix``, relative to it."""
        start = prefix + "/"
        return {rel[len(start) :]: node for rel, node in tree.items() if rel.startswith(start)}

    # -- harness sanity ------------------------------------------------------------

    def test_root_starts_empty(self, backend: Backend, root: str) -> None:
        assert root.startswith("/") and posixpath.normpath(root) == root
        assert self.observe(backend, root) == {}

    # -- path algebra (no I/O, no caps) ------------------------------------------

    def test_home_is_an_absolute_normalised_path(self, backend: Backend) -> None:
        home = backend.home()
        assert home.startswith("/")
        assert home == "/" or not home.endswith("/")
        assert "/./" not in home + "/" and "/../" not in home + "/"

    def test_join_and_parent(self, backend: Backend, root: str) -> None:
        child = backend.join(root, "a b")
        assert child == root + "/a b"
        assert backend.parent(child) == root
        assert backend.parent("/top") == "/"
        assert backend.parent("/") == "/"
        assert backend.join("/", "x") == "/x"

    def test_close_is_idempotent(self, backend: Backend) -> None:
        backend.close()
        backend.close()

    # -- list_dir -------------------------------------------------------------------

    def test_list_dir_returns_exactly_the_entries_and_no_dot_entries(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.LIST)
        self.seed(
            backend,
            root,
            {"file.txt": FILE(b"hello"), "sub": DIR, ".hidden": FILE(b"."), "to-sub": LINK("sub")},
        )
        entries = self.by_name(backend.list_dir(root))
        assert set(entries) == {"file.txt", "sub", ".hidden", "to-sub"}
        link = entries["to-sub"]
        assert link.kind == "symlink"
        assert link.link_target == "sub"
        assert link.target_kind == "dir"
        for name, entry in entries.items():
            assert entry.path == backend.join(root, name)

    def test_list_dir_describes_each_kind_with_lstat_semantics(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.LIST)
        self.seed(
            backend,
            root,
            {
                "f": FILE(b"12345"),
                "d": DIR,
                "to-f": LINK("f"),
                "to-d-abs": LINK(self.at(root, "d")),
                "dangling": LINK("missing"),
            },
        )
        entries = self.by_name(backend.list_dir(root))

        f = entries["f"]
        assert (f.kind, f.size, f.link_target, f.target_kind) == ("file", 5, None, None)
        d = entries["d"]
        assert (d.kind, d.size, d.link_target, d.target_kind) == ("dir", None, None, None)

        to_f = entries["to-f"]
        assert (to_f.kind, to_f.link_target, to_f.target_kind) == ("symlink", "f", "file")
        assert to_f.size in (None, len(b"f"))
        to_d = entries["to-d-abs"]
        assert (to_d.kind, to_d.link_target, to_d.target_kind) == (
            "symlink",
            self.at(root, "d"),
            "dir",
        )
        dangling = entries["dangling"]
        assert (dangling.kind, dangling.link_target, dangling.target_kind) == (
            "symlink",
            "missing",
            None,
        )
        for entry in entries.values():
            self.assert_permission_bits(entry)
            assert entry.mtime is None or isinstance(entry.mtime, float | int)

    def test_list_dir_of_an_empty_directory_is_empty(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.LIST)
        self.seed(backend, root, {"empty": DIR})
        assert backend.list_dir(self.at(root, "empty")) == []

    def test_list_dir_follows_a_symlink_to_a_directory(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.LIST)
        self.seed(backend, root, {"d": DIR, "d/x": FILE(b"1"), "to-d": LINK("d")})
        entries = backend.list_dir(self.at(root, "to-d"))
        assert [(e.name, e.path, e.kind) for e in entries] == [
            ("x", self.at(root, "to-d/x"), "file")
        ]

    def test_list_dir_of_a_missing_path_raises_not_found(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.LIST)
        with pytest.raises(NotFound):
            backend.list_dir(self.at(root, "missing"))

    def test_list_dir_of_a_file_raises_not_a_directory(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.LIST)
        self.seed(backend, root, {"f": FILE(b"x")})
        with pytest.raises(NotADirectory):
            backend.list_dir(self.at(root, "f"))

    # -- walk -----------------------------------------------------------------------

    def test_walk_lists_everything_below_with_each_directory_before_its_contents(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.LIST)
        self.seed(
            backend,
            root,
            {
                "a": DIR,
                "a/b": DIR,
                "a/b/f": FILE(b"1"),
                "a/g": FILE(b"2"),
                "a/b/up": LINK(".."),
                "h": FILE(b"3"),
                "to-a": LINK("a"),
            },
        )
        entries = backend.walk(root)
        paths = [e.path for e in entries]
        expected = ["a", "a/b", "a/b/f", "a/g", "a/b/up", "h", "to-a"]
        assert sorted(paths) == sorted(self.at(root, rel) for rel in expected)
        for index, path in enumerate(paths):
            parent = posixpath.dirname(path)
            if parent != root:
                assert paths.index(parent) < index, f"{parent} listed after {path}"
        kinds = {e.path: e.kind for e in entries}
        assert kinds[self.at(root, "to-a")] == "symlink"
        assert kinds[self.at(root, "a/b/up")] == "symlink"

    def test_walk_through_a_symlink_reports_paths_under_the_link(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.LIST)
        self.seed(backend, root, {"d": DIR, "d/e": DIR, "d/e/f": FILE(b"1"), "to-d": LINK("d")})
        paths = sorted(e.path for e in backend.walk(self.at(root, "to-d")))
        assert paths == [self.at(root, "to-d/e"), self.at(root, "to-d/e/f")]

    def test_walk_of_an_empty_directory_is_empty(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.LIST)
        assert backend.walk(root) == []

    # -- stat / exists --------------------------------------------------------------

    def test_stat_describes_the_path_itself(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.STAT)
        self.seed(backend, root, {"f": FILE(b"abc"), "d": DIR, "to-d": LINK("d")})
        f = backend.stat(self.at(root, "f"))
        assert (f.name, f.path, f.kind, f.size) == ("f", self.at(root, "f"), "file", 3)
        d = backend.stat(self.at(root, "d"))
        assert (d.name, d.kind, d.size) == ("d", "dir", None)
        link = backend.stat(self.at(root, "to-d"))
        assert (link.name, link.kind, link.link_target, link.target_kind) == (
            "to-d",
            "symlink",
            "d",
            "dir",
        )

    def test_stat_of_a_dangling_symlink_succeeds(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.STAT)
        self.seed(backend, root, {"dangling": LINK("missing")})
        entry = backend.stat(self.at(root, "dangling"))
        assert (entry.kind, entry.link_target, entry.target_kind) == ("symlink", "missing", None)

    def test_stat_of_a_missing_path_raises_not_found(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.STAT)
        with pytest.raises(NotFound):
            backend.stat(self.at(root, "missing"))

    def test_exists(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.STAT)
        self.seed(backend, root, {"f": FILE(b""), "d": DIR, "dangling": LINK("missing")})
        assert backend.exists(self.at(root, "f"))
        assert backend.exists(self.at(root, "d"))
        assert backend.exists(self.at(root, "dangling"))
        assert not backend.exists(self.at(root, "missing"))

    # -- mkdir ------------------------------------------------------------------------

    def test_mkdir_creates_an_empty_directory(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.MKDIR)
        backend.mkdir(self.at(root, "new"))
        assert self.observe(backend, root) == {"new": DIR}

    @pytest.mark.parametrize(
        "existing", [DIR, FILE(b"x"), LINK("missing")], ids=["dir", "file", "dangling-link"]
    )
    def test_mkdir_of_an_existing_path_raises_already_exists(
        self, backend: Backend, root: str, existing: Node
    ) -> None:
        self.need(backend, Cap.MKDIR)
        self.seed(backend, root, {"p": existing})
        before = self.observe(backend, root)
        with pytest.raises(AlreadyExists) as err:
            backend.mkdir(self.at(root, "p"))
        assert err.value.path == self.at(root, "p")
        assert self.observe(backend, root) == before

    def test_mkdir_under_a_missing_parent_raises_not_found(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.MKDIR)
        with pytest.raises(NotFound):
            backend.mkdir(self.at(root, "missing/new"))
        assert self.observe(backend, root) == {}

    def test_mkdir_under_a_file_raises_not_a_directory(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.MKDIR)
        self.seed(backend, root, {"f": FILE(b"x")})
        with pytest.raises(NotADirectory):
            backend.mkdir(self.at(root, "f/new"))
        assert self.observe(backend, root) == {"f": FILE(b"x")}

    # -- delete -----------------------------------------------------------------------

    def test_delete_a_file(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(backend, root, {"f": FILE(b"x"), "keep": FILE(b"k")})
        backend.delete(self.at(root, "f"))
        assert self.observe(backend, root) == {"keep": FILE(b"k")}

    def test_delete_an_empty_directory_without_recursive(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(backend, root, {"d": DIR})
        backend.delete(self.at(root, "d"))
        assert self.observe(backend, root) == {}

    def test_delete_a_non_empty_directory_without_recursive_raises_and_removes_nothing(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(backend, root, {"d": DIR, "d/f": FILE(b"1"), "d/e": DIR, "d/e/g": FILE(b"2")})
        before = self.observe(backend, root)
        with pytest.raises(DirectoryNotEmpty):
            backend.delete(self.at(root, "d"))
        assert self.observe(backend, root) == before

    def test_delete_recursive_removes_the_whole_tree(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(
            backend,
            root,
            {"d": DIR, "d/f": FILE(b"1"), "d/e": DIR, "d/e/g": FILE(b"2"), "keep": FILE(b"k")},
        )
        backend.delete(self.at(root, "d"), recursive=True)
        assert self.observe(backend, root) == {"keep": FILE(b"k")}

    def test_delete_a_symlink_to_a_file_keeps_the_target(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(backend, root, {"f": FILE(b"x"), "to-f": LINK("f")})
        backend.delete(self.at(root, "to-f"))
        assert self.observe(backend, root) == {"f": FILE(b"x")}

    def test_delete_recursive_of_a_symlink_to_a_directory_keeps_the_target_tree(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(backend, root, {"d": DIR, "d/f": FILE(b"1"), "d/e": DIR, "to-d": LINK("d")})
        backend.delete(self.at(root, "to-d"), recursive=True)
        assert self.observe(backend, root) == {"d": DIR, "d/f": FILE(b"1"), "d/e": DIR}

    def test_delete_recursive_never_follows_symlinks_inside_the_tree(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(
            backend,
            root,
            {
                "outside": DIR,
                "outside/f": FILE(b"keep me"),
                "doomed": DIR,
                "doomed/rel": LINK("../outside"),
                "doomed/abs": LINK(self.at(root, "outside/f")),
            },
        )
        backend.delete(self.at(root, "doomed"), recursive=True)
        assert self.observe(backend, root) == {"outside": DIR, "outside/f": FILE(b"keep me")}

    def test_delete_a_dangling_symlink(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(backend, root, {"dangling": LINK("missing")})
        backend.delete(self.at(root, "dangling"))
        assert self.observe(backend, root) == {}

    def test_delete_a_missing_path_raises_not_found(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.DELETE)
        with pytest.raises(NotFound):
            backend.delete(self.at(root, "missing"), recursive=True)

    # -- rename -----------------------------------------------------------------------

    def test_rename_a_file_to_a_free_name(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"a": FILE(b"data")})
        backend.rename(self.at(root, "a"), self.at(root, "b"))
        assert self.observe(backend, root) == {"b": FILE(b"data")}

    def test_rename_moves_a_whole_directory_tree(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(
            backend,
            root,
            {"src": DIR, "src/f": FILE(b"1"), "src/sub": DIR, "src/sub/g": FILE(b"2"), "dst": DIR},
        )
        backend.rename(self.at(root, "src"), self.at(root, "dst/moved"))
        assert self.observe(backend, root) == {
            "dst": DIR,
            "dst/moved": DIR,
            "dst/moved/f": FILE(b"1"),
            "dst/moved/sub": DIR,
            "dst/moved/sub/g": FILE(b"2"),
        }

    def test_rename_moves_a_symlink_itself(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"d": DIR, "d/f": FILE(b"1"), "to-d": LINK("d")})
        backend.rename(self.at(root, "to-d"), self.at(root, "renamed"))
        assert self.observe(backend, root) == {"d": DIR, "d/f": FILE(b"1"), "renamed": LINK("d")}

    def test_rename_onto_an_existing_file_without_overwrite_raises_and_changes_nothing(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"src": FILE(b"new"), "dst": FILE(b"old")})
        with pytest.raises(AlreadyExists) as err:
            backend.rename(self.at(root, "src"), self.at(root, "dst"))
        assert err.value.path == self.at(root, "dst")
        assert self.observe(backend, root) == {"src": FILE(b"new"), "dst": FILE(b"old")}

    def test_rename_onto_an_existing_file_with_overwrite_replaces_it(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"src": FILE(b"new"), "dst": FILE(b"old")})
        backend.rename(self.at(root, "src"), self.at(root, "dst"), overwrite=True)
        assert self.observe(backend, root) == {"dst": FILE(b"new")}

    @pytest.mark.parametrize("overwrite", [False, True])
    @pytest.mark.parametrize("src", [FILE(b"f"), DIR], ids=["file", "dir"])
    def test_rename_never_replaces_a_directory(
        self, backend: Backend, root: str, src: Node, overwrite: bool
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"src": src, "dst": DIR, "dst/inside": FILE(b"i")})
        before = self.observe(backend, root)
        expected = IsADirectory if overwrite else AlreadyExists
        with pytest.raises(expected):
            backend.rename(self.at(root, "src"), self.at(root, "dst"), overwrite=overwrite)
        assert self.observe(backend, root) == before

    def test_rename_a_directory_onto_a_file_with_overwrite_raises_not_a_directory(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"src": DIR, "src/f": FILE(b"1"), "dst": FILE(b"old")})
        before = self.observe(backend, root)
        with pytest.raises(NotADirectory):
            backend.rename(self.at(root, "src"), self.at(root, "dst"), overwrite=True)
        assert self.observe(backend, root) == before

    def test_rename_with_overwrite_replaces_a_symlink_not_its_target(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(
            backend, root, {"src": FILE(b"new"), "d": DIR, "d/f": FILE(b"1"), "to-d": LINK("d")}
        )
        backend.rename(self.at(root, "src"), self.at(root, "to-d"), overwrite=True)
        assert self.observe(backend, root) == {"d": DIR, "d/f": FILE(b"1"), "to-d": FILE(b"new")}

    @pytest.mark.parametrize("overwrite", [False, True])
    @pytest.mark.parametrize("src", [FILE(b"f"), DIR], ids=["file", "dir"])
    def test_rename_to_the_same_path_is_a_no_op(
        self, backend: Backend, root: str, src: Node, overwrite: bool
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"p": src})
        backend.rename(self.at(root, "p"), self.at(root, "p"), overwrite=overwrite)
        assert self.observe(backend, root) == {"p": src}

    @pytest.mark.parametrize("dst", ["d/inside", "d/e/deeper"])
    def test_rename_a_directory_into_its_own_subtree_raises(
        self, backend: Backend, root: str, dst: str
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"d": DIR, "d/e": DIR, "d/f": FILE(b"1")})
        before = self.observe(backend, root)
        with pytest.raises(OperationFailed):
            backend.rename(self.at(root, "d"), self.at(root, dst))
        assert self.observe(backend, root) == before

    def test_rename_a_directory_into_its_own_subtree_through_a_symlink_raises(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"d": DIR, "d/f": FILE(b"1"), "to-d": LINK("d")})
        before = self.observe(backend, root)
        with pytest.raises(OperationFailed):
            backend.rename(self.at(root, "d"), self.at(root, "to-d/moved"))
        assert self.observe(backend, root) == before

    def test_rename_a_directory_through_a_symlink_leading_out_of_it_is_not_a_subtree_move(
        self, backend: Backend, root: str
    ) -> None:
        # The destination is spelt under d, but once the middle symlink is
        # followed (as the OS would) it is out/moved, outside d's subtree.
        self.need(backend, Cap.RENAME)
        self.seed(
            backend, root, {"d": DIR, "d/f": FILE(b"1"), "d/to-out": LINK("../out"), "out": DIR}
        )
        backend.rename(self.at(root, "d"), self.at(root, "d/to-out/moved"))
        assert self.observe(backend, root) == {
            "out": DIR,
            "out/moved": DIR,
            "out/moved/f": FILE(b"1"),
            "out/moved/to-out": LINK("../out"),
        }

    @pytest.mark.parametrize("operation", ["rename", "copy_within"])
    def test_a_directory_into_its_own_subtree_through_a_symlink_to_a_subdirectory_raises(
        self, backend: Backend, root: str, operation: str
    ) -> None:
        # to-e leads to d/e, so to-e/inside is d/e/inside: two levels below d.
        self.need(backend, Cap.RENAME if operation == "rename" else Cap.COPY_WITHIN)
        self.seed(
            backend, root, {"d": DIR, "d/e": DIR, "d/f": FILE(b"1"), "to-e": LINK("d/e")}
        )
        before = self.observe(backend, root)
        with pytest.raises(OperationFailed):
            getattr(backend, operation)(self.at(root, "d"), self.at(root, "to-e/inside"))
        assert self.observe(backend, root) == before

    def test_rename_to_a_sibling_sharing_a_name_prefix_is_not_a_subtree_move(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"a": DIR, "a/f": FILE(b"1")})
        backend.rename(self.at(root, "a"), self.at(root, "ab"))
        assert self.observe(backend, root) == {"ab": DIR, "ab/f": FILE(b"1")}

    def test_rename_a_missing_source_raises_not_found(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.RENAME)
        with pytest.raises(NotFound):
            backend.rename(self.at(root, "missing"), self.at(root, "b"))
        assert self.observe(backend, root) == {}

    def test_rename_to_a_missing_parent_raises_not_found_and_keeps_the_source(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.RENAME)
        self.seed(backend, root, {"a": FILE(b"1")})
        with pytest.raises(NotFound):
            backend.rename(self.at(root, "a"), self.at(root, "missing/b"))
        assert self.observe(backend, root) == {"a": FILE(b"1")}

    # -- copy_within --------------------------------------------------------------------

    def test_copy_a_file_to_a_free_name(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"a": FILE(b"data")})
        backend.copy_within(self.at(root, "a"), self.at(root, "b"))
        assert self.observe(backend, root) == {"a": FILE(b"data"), "b": FILE(b"data")}

    def test_copy_keeps_the_permission_bits_of_a_file(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.COPY_WITHIN, Cap.STAT)
        self.seed(backend, root, {"a": FILE(b"data")})
        backend.copy_within(self.at(root, "a"), self.at(root, "b"))
        src, dst = backend.stat(self.at(root, "a")), backend.stat(self.at(root, "b"))
        if src.mode is not None and dst.mode is not None:
            assert dst.mode == src.mode

    def test_copy_a_directory_copies_the_tree_and_keeps_symlinks_as_links(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        tree = {
            "src": DIR,
            "src/f": FILE(b"1"),
            "src/sub": DIR,
            "src/sub/g": FILE(b"2"),
            "src/sub/empty": DIR,
            "src/to-f": LINK("f"),
            "src/dangling": LINK("nowhere"),
        }
        self.seed(backend, root, tree)
        backend.copy_within(self.at(root, "src"), self.at(root, "copy"))
        copied = {"copy" + rel[len("src") :]: node for rel, node in tree.items()}
        assert self.observe(backend, root) == {**tree, **copied}

    def test_copy_a_symlink_copies_the_link_itself(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"d": DIR, "d/f": FILE(b"1"), "to-d": LINK("d")})
        backend.copy_within(self.at(root, "to-d"), self.at(root, "copy"))
        assert self.observe(backend, root) == {
            "d": DIR,
            "d/f": FILE(b"1"),
            "to-d": LINK("d"),
            "copy": LINK("d"),
        }

    def test_a_copy_is_independent_of_its_source(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"src": DIR, "src/f": FILE(b"original")})
        backend.copy_within(self.at(root, "src"), self.at(root, "copy"))
        self.seed_file(backend, self.at(root, "copy/f"), b"changed")
        self.seed_dir(backend, self.at(root, "copy/added"))
        assert self.observe(backend, root) == {
            "src": DIR,
            "src/f": FILE(b"original"),
            "copy": DIR,
            "copy/f": FILE(b"changed"),
            "copy/added": DIR,
        }

    def test_copy_onto_an_existing_file_without_overwrite_raises_and_changes_nothing(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"src": FILE(b"new"), "dst": FILE(b"old")})
        with pytest.raises(AlreadyExists) as err:
            backend.copy_within(self.at(root, "src"), self.at(root, "dst"))
        assert err.value.path == self.at(root, "dst")
        assert self.observe(backend, root) == {"src": FILE(b"new"), "dst": FILE(b"old")}

    def test_copy_onto_an_existing_file_with_overwrite_replaces_it(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"src": FILE(b"new"), "dst": FILE(b"old")})
        backend.copy_within(self.at(root, "src"), self.at(root, "dst"), overwrite=True)
        assert self.observe(backend, root) == {"src": FILE(b"new"), "dst": FILE(b"new")}

    @pytest.mark.parametrize("overwrite", [False, True])
    @pytest.mark.parametrize("src", [FILE(b"f"), DIR], ids=["file", "dir"])
    def test_copy_never_replaces_a_directory(
        self, backend: Backend, root: str, src: Node, overwrite: bool
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"src": src, "dst": DIR, "dst/inside": FILE(b"i")})
        before = self.observe(backend, root)
        expected = IsADirectory if overwrite else AlreadyExists
        with pytest.raises(expected):
            backend.copy_within(self.at(root, "src"), self.at(root, "dst"), overwrite=overwrite)
        assert self.observe(backend, root) == before

    def test_copy_a_directory_onto_a_file_with_overwrite_raises_not_a_directory(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"src": DIR, "src/f": FILE(b"1"), "dst": FILE(b"old")})
        before = self.observe(backend, root)
        with pytest.raises(NotADirectory):
            backend.copy_within(self.at(root, "src"), self.at(root, "dst"), overwrite=True)
        assert self.observe(backend, root) == before

    def test_copy_with_overwrite_replaces_a_symlink_not_its_target(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(
            backend, root, {"src": FILE(b"new"), "d": DIR, "d/f": FILE(b"1"), "to-d": LINK("d")}
        )
        backend.copy_within(self.at(root, "src"), self.at(root, "to-d"), overwrite=True)
        assert self.observe(backend, root) == {
            "src": FILE(b"new"),
            "d": DIR,
            "d/f": FILE(b"1"),
            "to-d": FILE(b"new"),
        }

    @pytest.mark.parametrize("src", [FILE(b"f"), DIR], ids=["file", "dir"])
    def test_copy_onto_itself_counts_as_existing(self, backend: Backend, root: str, src: Node) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"p": src})
        with pytest.raises(AlreadyExists):
            backend.copy_within(self.at(root, "p"), self.at(root, "p"))
        assert self.observe(backend, root) == {"p": src}

    @pytest.mark.parametrize("dst", ["d/inside", "d/e/deeper"])
    def test_copy_a_directory_into_its_own_subtree_raises(
        self, backend: Backend, root: str, dst: str
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"d": DIR, "d/e": DIR, "d/f": FILE(b"1")})
        before = self.observe(backend, root)
        with pytest.raises(OperationFailed):
            backend.copy_within(self.at(root, "d"), self.at(root, dst))
        assert self.observe(backend, root) == before

    def test_copy_a_directory_into_its_own_subtree_through_a_symlink_raises(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"d": DIR, "d/f": FILE(b"1"), "to-d": LINK("d")})
        before = self.observe(backend, root)
        with pytest.raises(OperationFailed):
            backend.copy_within(self.at(root, "d"), self.at(root, "to-d/copy"))
        assert self.observe(backend, root) == before

    def test_copy_a_directory_through_a_symlink_leading_out_of_it_is_not_a_subtree_copy(
        self, backend: Backend, root: str
    ) -> None:
        # d/to-out/copy is out/copy once the middle symlink is followed.
        self.need(backend, Cap.COPY_WITHIN)
        tree = {"d": DIR, "d/f": FILE(b"1"), "d/to-out": LINK("../out"), "out": DIR}
        self.seed(backend, root, tree)
        backend.copy_within(self.at(root, "d"), self.at(root, "d/to-out/copy"))
        assert self.observe(backend, root) == {
            **tree,
            "out/copy": DIR,
            "out/copy/f": FILE(b"1"),
            "out/copy/to-out": LINK("../out"),
        }

    def test_copy_to_a_sibling_sharing_a_name_prefix_is_not_a_subtree_copy(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"a": DIR, "a/f": FILE(b"1")})
        backend.copy_within(self.at(root, "a"), self.at(root, "ab"))
        assert self.observe(backend, root) == {
            "a": DIR,
            "a/f": FILE(b"1"),
            "ab": DIR,
            "ab/f": FILE(b"1"),
        }

    def test_copy_a_missing_source_raises_not_found(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        with pytest.raises(NotFound):
            backend.copy_within(self.at(root, "missing"), self.at(root, "b"))
        assert self.observe(backend, root) == {}

    @pytest.mark.parametrize("src", [FILE(b"f"), DIR], ids=["file", "dir"])
    def test_copy_to_a_missing_parent_raises_not_found(
        self, backend: Backend, root: str, src: Node
    ) -> None:
        self.need(backend, Cap.COPY_WITHIN)
        self.seed(backend, root, {"a": src})
        with pytest.raises(NotFound):
            backend.copy_within(self.at(root, "a"), self.at(root, "missing/b"))
        assert self.observe(backend, root) == {"a": src}

    # -- download -------------------------------------------------------------------------

    def test_download_writes_the_file_to_a_new_local_path(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.DOWNLOAD)
        data = bytes(range(256)) * 64
        self.seed(backend, root, {"f": FILE(data)})
        dest = local_tmp / "got"
        backend.download(self.at(root, "f"), str(dest))
        assert dest.read_bytes() == data
        assert sorted(os.listdir(local_tmp)) == ["got"]
        assert self.observe(backend, root) == {"f": FILE(data)}

    def test_download_follows_a_symlink_to_a_file(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.DOWNLOAD)
        self.seed(backend, root, {"f": FILE(b"target"), "to-f": LINK("f")})
        dest = local_tmp / "got"
        backend.download(self.at(root, "to-f"), str(dest))
        assert not dest.is_symlink()
        assert dest.read_bytes() == b"target"

    def test_download_refuses_an_existing_local_destination(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.DOWNLOAD)
        self.seed(backend, root, {"f": FILE(b"remote")})
        dest = self.local_file(local_tmp, "existing", b"local")
        with pytest.raises(AlreadyExists) as err:
            backend.download(self.at(root, "f"), dest)
        assert err.value.path == dest
        assert Path(dest).read_bytes() == b"local"
        os.symlink("nowhere", local_tmp / "dangling")
        with pytest.raises(AlreadyExists):
            backend.download(self.at(root, "f"), str(local_tmp / "dangling"))
        assert os.readlink(local_tmp / "dangling") == "nowhere"
        assert sorted(os.listdir(local_tmp)) == ["dangling", "existing"]

    def test_download_to_a_missing_local_parent_raises_not_found(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.DOWNLOAD)
        self.seed(backend, root, {"f": FILE(b"x")})
        dest = str(local_tmp / "missing" / "got")
        with pytest.raises(NotFound) as err:
            backend.download(self.at(root, "f"), dest)
        assert err.value.path == dest
        assert os.listdir(local_tmp) == []

    def test_download_of_a_directory_raises_is_a_directory(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.DOWNLOAD)
        self.seed(backend, root, {"d": DIR, "d/f": FILE(b"x")})
        with pytest.raises(IsADirectory):
            backend.download(self.at(root, "d"), str(local_tmp / "got"))
        assert os.listdir(local_tmp) == []

    def test_download_of_a_missing_path_raises_not_found(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.DOWNLOAD)
        with pytest.raises(NotFound):
            backend.download(self.at(root, "missing"), str(local_tmp / "got"))
        assert os.listdir(local_tmp) == []

    # -- upload ---------------------------------------------------------------------------

    def test_upload_creates_the_file(self, backend: Backend, root: str, local_tmp: Path) -> None:
        self.need(backend, Cap.UPLOAD)
        data = bytes(range(256)) * 64
        src = self.local_file(local_tmp, "src", data)
        backend.upload(src, self.at(root, "up"))
        assert self.observe(backend, root) == {"up": FILE(data)}
        assert Path(src).read_bytes() == data

    def test_upload_onto_an_existing_file_without_overwrite_raises_and_keeps_its_bytes(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.UPLOAD)
        self.seed(backend, root, {"dst": FILE(b"old")})
        src = self.local_file(local_tmp, "src", b"new")
        with pytest.raises(AlreadyExists) as err:
            backend.upload(src, self.at(root, "dst"))
        assert err.value.path == self.at(root, "dst")
        assert self.observe(backend, root) == {"dst": FILE(b"old")}

    def test_upload_with_overwrite_replaces_the_file(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.UPLOAD)
        self.seed(backend, root, {"dst": FILE(b"old and longer")})
        src = self.local_file(local_tmp, "src", b"new")
        backend.upload(src, self.at(root, "dst"), overwrite=True)
        assert self.observe(backend, root) == {"dst": FILE(b"new")}

    @pytest.mark.parametrize("overwrite", [False, True])
    def test_upload_never_replaces_a_directory(
        self, backend: Backend, root: str, local_tmp: Path, overwrite: bool
    ) -> None:
        self.need(backend, Cap.UPLOAD)
        self.seed(backend, root, {"dst": DIR, "dst/inside": FILE(b"i")})
        src = self.local_file(local_tmp, "src", b"new")
        expected = IsADirectory if overwrite else AlreadyExists
        with pytest.raises(expected):
            backend.upload(src, self.at(root, "dst"), overwrite=overwrite)
        assert self.observe(backend, root) == {"dst": DIR, "dst/inside": FILE(b"i")}

    def test_upload_with_overwrite_replaces_a_symlink_not_its_target(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.UPLOAD)
        self.seed(backend, root, {"f": FILE(b"target"), "to-f": LINK("f")})
        src = self.local_file(local_tmp, "src", b"new")
        backend.upload(src, self.at(root, "to-f"), overwrite=True)
        assert self.observe(backend, root) == {"f": FILE(b"target"), "to-f": FILE(b"new")}

    def test_upload_to_a_missing_parent_raises_not_found_and_creates_no_directories(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.UPLOAD)
        src = self.local_file(local_tmp, "src", b"x")
        with pytest.raises(NotFound):
            backend.upload(src, self.at(root, "missing/up"), overwrite=True)
        assert self.observe(backend, root) == {}

    def test_upload_of_a_missing_local_file_raises_not_found(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.UPLOAD)
        src = str(local_tmp / "missing")
        with pytest.raises(NotFound) as err:
            backend.upload(src, self.at(root, "up"))
        assert err.value.path == src
        assert self.observe(backend, root) == {}

    def test_upload_of_a_local_directory_raises_is_a_directory(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.UPLOAD)
        (local_tmp / "dir").mkdir()
        with pytest.raises(IsADirectory):
            backend.upload(str(local_tmp / "dir"), self.at(root, "up"), overwrite=True)
        assert self.observe(backend, root) == {}

    def test_upload_follows_a_local_symlink(
        self, backend: Backend, root: str, local_tmp: Path
    ) -> None:
        self.need(backend, Cap.UPLOAD)
        self.local_file(local_tmp, "real", b"content")
        os.symlink("real", local_tmp / "link")
        backend.upload(str(local_tmp / "link"), self.at(root, "up"))
        assert self.observe(backend, root) == {"up": FILE(b"content")}

    # -- delete_many ----------------------------------------------------------------------

    def test_delete_many_deletes_each_path_in_the_order_given(
        self, backend: Backend, root: str
    ) -> None:
        # Without recursive, d can go only once d/f and d/g are gone.
        self.need(backend, Cap.DELETE)
        self.seed(
            backend,
            root,
            {"d": DIR, "d/f": FILE(b"1"), "d/g": FILE(b"2"), "x": FILE(b"x"), "keep": FILE(b"k")},
        )
        backend.delete_many(
            [self.at(root, "d/f"), self.at(root, "d/g"), self.at(root, "d"), self.at(root, "x")]
        )
        assert self.observe(backend, root) == {"keep": FILE(b"k")}

    def test_delete_many_recursive_removes_whole_trees(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(
            backend,
            root,
            {
                "a": DIR,
                "a/b": DIR,
                "a/b/f": FILE(b"1"),
                "c": DIR,
                "c/g": FILE(b"2"),
                "keep": DIR,
                "keep/h": FILE(b"3"),
            },
        )
        backend.delete_many([self.at(root, "a"), self.at(root, "c")], recursive=True)
        assert self.observe(backend, root) == {"keep": DIR, "keep/h": FILE(b"3")}

    @pytest.mark.parametrize(
        ("failing", "error"),
        [("missing", NotFound), ("full", DirectoryNotEmpty)],
        ids=["missing", "non-empty-directory"],
    )
    def test_delete_many_stops_at_the_first_failure_and_raises_it(
        self, backend: Backend, root: str, failing: str, error: type[Exception]
    ) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(
            backend,
            root,
            {"first": FILE(b"1"), "full": DIR, "full/f": FILE(b"2"), "last": FILE(b"3")},
        )
        with pytest.raises(error) as err:
            backend.delete_many(
                [self.at(root, "first"), self.at(root, failing), self.at(root, "last")]
            )
        assert err.value.path == self.at(root, failing)  # type: ignore[attr-defined]
        # first stays deleted, the failing path is intact, last was never attempted.
        assert self.observe(backend, root) == {
            "full": DIR,
            "full/f": FILE(b"2"),
            "last": FILE(b"3"),
        }

    def test_delete_many_of_no_paths_changes_nothing(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.DELETE)
        self.seed(backend, root, {"f": FILE(b"1")})
        assert backend.delete_many([]) is None
        assert self.observe(backend, root) == {"f": FILE(b"1")}

    # -- invalidate (the listing cache) ----------------------------------------------------

    def test_invalidate_returns_none_and_changes_nothing(self, backend: Backend, root: str) -> None:
        # It needs no capability, so this runs whatever the caps are.
        self.seed(backend, root, {"d": DIR, "d/e": DIR, "d/e/f": FILE(b"1"), "g": FILE(b"2")})
        before = self.observe(backend, root)
        for path in (
            root,
            self.at(root, "d"),
            self.at(root, "d/e"),
            self.at(root, "g"),
            self.at(root, "missing"),
            "/",
            None,
        ):
            assert backend.invalidate(path) is None, path
        assert backend.invalidate() is None
        assert self.observe(backend, root) == before

    def test_after_invalidate_the_next_listing_asks_the_store(
        self, backend: Backend, root: str
    ) -> None:
        # The hooks change the store behind the backend's back, as an editor
        # would. A backend without a cache sees it at once; a caching one must
        # see it after invalidate.
        self.need(backend, Cap.LIST)
        self.seed(backend, root, {"d": DIR, "d/e": DIR})
        d, e = self.at(root, "d"), self.at(root, "d/e")
        for path in (root, d, e):
            backend.list_dir(path)
            self.seed_file(backend, backend.join(path, "new"), b"n")

        def names(path: str) -> set[str]:
            return {entry.name for entry in backend.list_dir(path)}

        backend.invalidate(d)
        assert names(d) == {"e", "new"}
        assert names(e) == {"new"}
        backend.invalidate()
        assert names(root) == {"d", "new"}
        self.seed_file(backend, self.at(root, "newer"), b"n")
        backend.invalidate("/")
        assert names(root) == {"d", "new", "newer"}

    # -- archive attributes ------------------------------------------------------------------

    def test_archive_attributes_are_none_or_well_formed(self, backend: Backend) -> None:
        archive_dir, max_bytes = backend.archive_dir, backend.archive_max_bytes
        assert archive_dir is None or (
            isinstance(archive_dir, str)
            and archive_dir.startswith("/")
            and posixpath.normpath(archive_dir) == archive_dir
        ), archive_dir
        assert max_bytes is None or (type(max_bytes) is int and max_bytes > 0), max_bytes

    # -- pack ----------------------------------------------------------------------------------

    def test_pack_writes_one_gzip_tar_of_the_selection_named_relative_to_the_parent(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        tree = {
            "src": DIR,
            "src/f": FILE(b"one"),
            "src/d": DIR,
            "src/d/g": FILE(b"two"),
            "src/d/sub": DIR,
            "src/d/sub/h": FILE(b"three"),
            "src/d/empty": DIR,
            "src/unselected": FILE(b"not packed"),
        }
        self.seed(backend, root, tree)
        backend.pack(self.at(root, "src"), ["f", "d"], self.at(root, "a.tgz"))
        data = self.archive_bytes(backend, root, "a.tgz")
        assert self.observe(backend, root) == {**tree, "a.tgz": FILE(data)}, "packed entries changed"
        members = archive_members(data)
        names = [info.name for info, _ in members]
        assert len(names) == len(set(names)), f"duplicate members: {names}"
        assert {info.name: node for info, node in members} == {
            "f": FILE(b"one"),
            "d": DIR,
            "d/g": FILE(b"two"),
            "d/sub": DIR,
            "d/sub/h": FILE(b"three"),
            "d/empty": DIR,
        }
        for index, name in enumerate(names):
            parent = posixpath.dirname(name)
            if parent:
                assert names.index(parent) < index, f"{parent} is stored after {name}"
        for info, _ in members:
            assert not info.name.startswith("/") and ".." not in info.name.split("/"), info.name
            assert (info.uid, info.gid, info.uname, info.gname) == (0, 0, "", ""), info.name

    def test_pack_records_permission_bits_and_mtimes(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.ARCHIVE, Cap.STAT)
        self.seed(
            backend, root, {"src": DIR, "src/f": FILE(b"1"), "src/d": DIR, "src/d/g": FILE(b"2")}
        )
        backend.pack(self.at(root, "src"), ["f", "d"], self.at(root, "a.tgz"))
        members = archive_members(self.archive_bytes(backend, root, "a.tgz"))
        assert sorted(info.name for info, _ in members) == ["d", "d/g", "f"]
        for info, _ in members:
            entry = backend.stat(self.at(root, "src/" + info.name))
            if entry.mode is not None:
                assert info.mode == entry.mode, (info.name, oct(info.mode), oct(entry.mode))
            if entry.mtime is not None:
                assert abs(info.mtime - entry.mtime) < 1, (info.name, info.mtime, entry.mtime)

    def test_pack_stores_symlinks_as_links_and_never_follows_them(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        tree = {
            "src": DIR,
            "src/outside": DIR,
            "src/outside/secret": FILE(b"must not be packed"),
            "src/d": DIR,
            "src/d/f": FILE(b"1"),
            "src/d/to-f": LINK("f"),
            "src/d/to-outside": LINK("../outside"),
            "src/d/abs": LINK(self.at(root, "src/outside/secret")),
            "src/d/dangling": LINK("nowhere"),
            "src/selected-link": LINK("outside"),
        }
        self.seed(backend, root, tree)
        backend.pack(self.at(root, "src"), ["d", "selected-link"], self.at(root, "a.tgz"))
        packed = archive_members(self.archive_bytes(backend, root, "a.tgz"))
        assert {info.name: node for info, node in packed} == {
            "d": DIR,
            "d/f": FILE(b"1"),
            "d/to-f": LINK("f"),
            "d/to-outside": LINK("../outside"),
            "d/abs": LINK(self.at(root, "src/outside/secret")),
            "d/dangling": LINK("nowhere"),
            "selected-link": LINK("outside"),
        }
        after = self.observe(backend, root)
        assert after.pop("a.tgz")[0] == "file"
        assert after == tree, "the packed entries changed"

    def test_pack_follows_a_symlink_to_a_directory_at_parent(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(backend, root, {"d": DIR, "d/x": FILE(b"1"), "to-d": LINK("d")})
        backend.pack(self.at(root, "to-d"), ["x"], self.at(root, "a.tgz"))
        packed = archive_members(self.archive_bytes(backend, root, "a.tgz"))
        assert [(info.name, node) for info, node in packed] == [("x", FILE(b"1"))]

    def test_pack_of_no_names_writes_an_archive_without_members(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(backend, root, {"src": DIR, "src/f": FILE(b"1")})
        backend.pack(self.at(root, "src"), [], self.at(root, "a.tgz"))
        assert archive_members(self.archive_bytes(backend, root, "a.tgz")) == []
        after = self.observe(backend, root)
        assert after.pop("a.tgz")[0] == "file"
        assert after == {"src": DIR, "src/f": FILE(b"1")}

    @pytest.mark.parametrize(
        "existing", [DIR, FILE(b"x"), LINK("missing")], ids=["dir", "file", "dangling-link"]
    )
    def test_pack_onto_an_existing_path_raises_already_exists_and_leaves_it_unchanged(
        self, backend: Backend, root: str, existing: Node
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(backend, root, {"src": DIR, "src/f": FILE(b"1"), "a.tgz": existing})
        before = self.observe(backend, root)
        with pytest.raises(AlreadyExists) as err:
            backend.pack(self.at(root, "src"), ["f"], self.at(root, "a.tgz"))
        assert err.value.path == self.at(root, "a.tgz")
        assert self.observe(backend, root) == before

    def test_pack_into_a_missing_directory_raises_not_found(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(backend, root, {"src": DIR, "src/f": FILE(b"1")})
        with pytest.raises(NotFound):
            backend.pack(self.at(root, "src"), ["f"], self.at(root, "missing/a.tgz"))
        assert self.observe(backend, root) == {"src": DIR, "src/f": FILE(b"1")}

    def test_pack_of_a_missing_parent_raises_not_found_about_it(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        with pytest.raises(NotFound) as err:
            backend.pack(self.at(root, "missing"), ["f"], self.at(root, "a.tgz"))
        assert err.value.path == self.at(root, "missing")
        assert self.observe(backend, root) == {}

    def test_pack_with_a_missing_name_raises_not_found_about_it_and_leaves_no_archive(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        tree = {"src": DIR, "src/a": FILE(b"a"), "src/b": FILE(b"b")}
        self.seed(backend, root, tree)
        with pytest.raises(NotFound) as err:
            backend.pack(self.at(root, "src"), ["a", "missing", "b"], self.at(root, "a.tgz"))
        assert err.value.path == self.at(root, "src/missing")
        assert self.observe(backend, root) == tree

    def test_pack_of_a_file_as_parent_raises_not_a_directory(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(backend, root, {"f": FILE(b"1")})
        with pytest.raises(NotADirectory):
            backend.pack(self.at(root, "f"), ["x"], self.at(root, "a.tgz"))
        assert self.observe(backend, root) == {"f": FILE(b"1")}

    # -- unpack --------------------------------------------------------------------------------

    def test_unpack_extracts_into_a_new_directory_and_removes_the_archive(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        data = make_archive(
            [("f", FILE(b"one")), ("d", DIR), ("d/g", FILE(b"two")), ("d/empty", DIR)]
        )
        self.seed(backend, root, {"a.tgz": FILE(data), "parent": DIR})
        backend.unpack(self.at(root, "a.tgz"), self.at(root, "parent/dest"))
        assert self.observe(backend, root) == {
            "parent": DIR,
            "parent/dest": DIR,
            "parent/dest/f": FILE(b"one"),
            "parent/dest/d": DIR,
            "parent/dest/d/g": FILE(b"two"),
            "parent/dest/d/empty": DIR,
        }

    def test_unpack_of_an_archive_without_members_creates_an_empty_directory(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(backend, root, {"a.tgz": FILE(make_archive([]))})
        backend.unpack(self.at(root, "a.tgz"), self.at(root, "dest"))
        assert self.observe(backend, root) == {"dest": DIR}

    def test_unpack_recreates_symlinks_with_their_link_text_unchanged(
        self, backend: Backend, root: str
    ) -> None:
        # Absolute links and links leading out of dest are kept as they are
        # (the "tar" filter, not the stricter "data" one).
        self.need(backend, Cap.ARCHIVE)
        self.need_symlinks()
        absolute = self.at(root, "outside/target")
        data = make_archive(
            [
                ("d", DIR),
                ("d/f", FILE(b"1")),
                ("d/rel", LINK("f")),
                ("d/up", LINK("../../outside")),
                ("abs", LINK(absolute)),
                ("dangling", LINK("nowhere")),
            ]
        )
        self.seed(backend, root, {"a.tgz": FILE(data), "outside": DIR})
        backend.unpack(self.at(root, "a.tgz"), self.at(root, "dest"))
        assert self.observe(backend, root) == {
            "outside": DIR,
            "dest": DIR,
            "dest/d": DIR,
            "dest/d/f": FILE(b"1"),
            "dest/d/rel": LINK("f"),
            "dest/d/up": LINK("../../outside"),
            "dest/abs": LINK(absolute),
            "dest/dangling": LINK("nowhere"),
        }

    def test_unpack_applies_the_recorded_mtimes_and_modes_without_special_or_group_other_write_bits(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE, Cap.STAT)
        mtime = 1_234_567_890
        members = [
            ("d", DIR, 0o777),
            ("d/private", DIR, 0o700),
            ("d/setid", FILE(b"x"), 0o6777),
            ("d/read-only", FILE(b"r"), 0o444),
            ("d/shared", FILE(b"s"), 0o666),
            ("d/plain", FILE(b"p"), 0o640),
            ("sticky", DIR, 0o1777),
        ]
        self.seed(backend, root, {"a.tgz": FILE(make_archive(members, mtime=mtime))})
        backend.unpack(self.at(root, "a.tgz"), self.at(root, "dest"))
        expected = {
            "d": 0o755,
            "d/private": 0o700,
            "d/setid": 0o755,
            "d/read-only": 0o444,
            "d/shared": 0o644,
            "d/plain": 0o640,
            "sticky": 0o755,
        }
        for rel, mode in expected.items():
            entry = backend.stat(self.at(root, "dest/" + rel))
            if entry.mode is not None:
                assert entry.mode == mode, (rel, oct(entry.mode), oct(mode))
            if entry.mtime is not None:
                assert abs(entry.mtime - mtime) < 1, (rel, entry.mtime)

    @pytest.mark.parametrize("escape", ["dot-dot", "absolute-name", "through-a-symlink"])
    def test_unpack_refuses_a_member_that_would_land_outside_dest(
        self, backend: Backend, root: str, escape: str
    ) -> None:
        # Each escape aims at root/escaped (an absolute member name too), so a
        # backend that wrongly extracts it still writes nowhere outside root.
        self.need(backend, Cap.ARCHIVE)
        ok = ("ok", FILE(b"extracted first"))
        if escape == "dot-dot":
            members = [ok, ("../escaped", FILE(b"x"))]
        elif escape == "absolute-name":
            members = [ok, (self.at(root, "escaped"), FILE(b"x"))]
        else:
            self.need_symlinks()
            members = [ok, ("esc", LINK(root)), ("esc/escaped", FILE(b"x"))]
        self.seed(backend, root, {"a.tgz": FILE(make_archive(members))})
        before = self.observe(backend, root)
        with pytest.raises(OperationFailed):
            backend.unpack(self.at(root, "a.tgz"), self.at(root, "dest"))
        # dest is removed with what was extracted, and the archive is kept.
        assert self.observe(backend, root) == before

    @pytest.mark.parametrize(
        "damage",
        [
            "not-gzip",
            "gzip-but-not-tar",
            "truncated",
            "gzip-trailer-cut-off",
            "content-changed-under-the-recorded-crc",
            "bytes-after-the-gzip-member",
            "hard-link-to-a-member-not-in-the-archive",
        ],
    )
    def test_unpack_of_a_damaged_archive_raises_operation_failed_and_keeps_it(
        self, backend: Backend, root: str, damage: str
    ) -> None:
        # The trailer and bytes-after cases leave every tar header intact, so
        # only the end of the gzip stream shows the damage: tar reading stops
        # at the end-of-archive block, before the trailer, unless it is read.
        # pack stores a hard link only to a file it stored before it, so a
        # link to a member that is not in the archive is not pack's either.
        self.need(backend, Cap.ARCHIVE)
        if damage == "not-gzip":
            data = b"this is not a gzip-compressed tar\n" * 32
        elif damage == "gzip-but-not-tar":
            data = gzip.compress(b"plain text, not a tar archive\n" * 64)
        elif damage == "truncated":
            whole = make_archive(
                [("a", FILE(incompressible(64 * 1024, 1))), ("b", FILE(incompressible(64 * 1024, 2)))]
            )
            data = whole[: len(whole) // 2]
        elif damage == "hard-link-to-a-member-not-in-the-archive":
            buffer = io.BytesIO()
            with tarfile.open(
                fileobj=buffer, mode="w:gz", compresslevel=6, format=tarfile.PAX_FORMAT
            ) as tar:
                ok = tarfile.TarInfo("ok")
                ok.size, ok.mtime = 1, ARCHIVE_MTIME
                tar.addfile(ok, io.BytesIO(b"1"))
                link = tarfile.TarInfo("h")
                link.type, link.linkname, link.mtime = tarfile.LNKTYPE, "missing", ARCHIVE_MTIME
                tar.addfile(link)
            data = buffer.getvalue()
            # Harness check: a well-formed gzip tar; only the link is wrong.
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                assert [m.name for m in tar.getmembers()] == ["ok", "h"]
            gzip.decompress(data)
        else:
            whole = make_archive(
                [("f", FILE(b"the original content\n" * 64)), ("g", FILE(b"second file\n"))]
            )
            if damage == "gzip-trailer-cut-off":
                data = whole[:-8]  # every deflate byte, without the CRC-32 and size
            elif damage == "bytes-after-the-gzip-member":
                data = whole + b"not a gzip member\n"  # neither a gzip header nor zero padding
            else:
                plain = gzip.decompress(whole)
                with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
                    offset = tar.getmember("f").offset_data
                altered = bytearray(plain)
                altered[offset] ^= 0x20  # "the original" -> "The original"
                rewrapped = gzip.compress(bytes(altered), compresslevel=6, mtime=0)
                data = rewrapped[:-8] + whole[-8:]  # the trailer recorded for the original
                assert gzip.decompress(rewrapped) != plain and whole[-4:] == rewrapped[-4:]
            # Harness check: reading the whole gzip stream does see the damage.
            with pytest.raises((EOFError, gzip.BadGzipFile)):
                gzip.decompress(data)
        self.seed(backend, root, {"a.tgz": FILE(data)})
        with pytest.raises(OperationFailed):
            backend.unpack(self.at(root, "a.tgz"), self.at(root, "dest"))
        assert self.observe(backend, root) == {"a.tgz": FILE(data)}

    def test_unpack_of_a_missing_archive_raises_not_found_and_creates_nothing(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        with pytest.raises(NotFound):
            backend.unpack(self.at(root, "missing.tgz"), self.at(root, "dest"))
        assert self.observe(backend, root) == {}

    @pytest.mark.parametrize(
        "existing", [DIR, FILE(b"x"), LINK("missing")], ids=["dir", "file", "dangling-link"]
    )
    def test_unpack_onto_an_existing_path_raises_already_exists_and_keeps_the_archive(
        self, backend: Backend, root: str, existing: Node
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(
            backend, root, {"a.tgz": FILE(make_archive([("f", FILE(b"1"))])), "dest": existing}
        )
        before = self.observe(backend, root)
        with pytest.raises(AlreadyExists) as err:
            backend.unpack(self.at(root, "a.tgz"), self.at(root, "dest"))
        assert err.value.path == self.at(root, "dest")
        assert self.observe(backend, root) == before

    def test_unpack_under_a_missing_parent_raises_not_found_about_dest(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(backend, root, {"a.tgz": FILE(make_archive([("f", FILE(b"1"))]))})
        before = self.observe(backend, root)
        with pytest.raises(NotFound) as err:
            backend.unpack(self.at(root, "a.tgz"), self.at(root, "missing/dest"))
        assert err.value.path == self.at(root, "missing/dest")
        assert self.observe(backend, root) == before

    # -- pack, then unpack ----------------------------------------------------------------------

    def test_pack_then_unpack_reproduces_the_selection_with_its_modes_and_mtimes(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        tree = {
            "src": DIR,
            "src/f": FILE(b"one"),
            "src/d": DIR,
            "src/d/g": FILE(bytes(range(256)) * 64),
            "src/d/empty": DIR,
            "src/d/sub": DIR,
            "src/d/sub/h": FILE(b""),
            "src/unselected": FILE(b"stays behind"),
        }
        self.seed(backend, root, tree)
        backend.pack(self.at(root, "src"), ["f", "d"], self.at(root, "a.tgz"))
        backend.unpack(self.at(root, "a.tgz"), self.at(root, "out"))
        selection = {
            rel: node for rel, node in self.below(tree, "src").items() if rel != "unselected"
        }
        assert self.observe(backend, root) == {
            **tree,
            "out": DIR,
            **{f"out/{rel}": node for rel, node in selection.items()},
        }
        if Cap.STAT not in backend.caps:
            return
        for rel in selection:
            original = backend.stat(self.at(root, f"src/{rel}"))
            copy = backend.stat(self.at(root, f"out/{rel}"))
            if original.mode is not None and copy.mode is not None:
                assert copy.mode == original.mode & 0o755, (rel, oct(copy.mode), oct(original.mode))
            if original.mtime is not None and copy.mtime is not None:
                assert abs(copy.mtime - original.mtime) < 1, (rel, copy.mtime, original.mtime)

    def test_hostile_names_round_trip_through_pack_and_unpack(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        tree: dict[str, Node] = {"src": DIR, "src/-dir $(x)": DIR, "src/-dir $(x)/'q' *": FILE(b"in")}
        tree.update({f"src/{name}": FILE(name.encode()) for name in HOSTILE_NAMES})
        self.seed(backend, root, tree)
        selection = self.below(tree, "src")
        names = [rel for rel in selection if "/" not in rel]
        backend.pack(self.at(root, "src"), names, self.at(root, "a.tgz"))
        packed = archive_members(self.archive_bytes(backend, root, "a.tgz"))
        assert {info.name: node for info, node in packed} == selection
        backend.unpack(self.at(root, "a.tgz"), self.at(root, "out"))
        assert self.observe(backend, root) == {
            **tree,
            "out": DIR,
            **{f"out/{rel}": node for rel, node in selection.items()},
        }

    # -- merge -----------------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "src", [FILE(b"f"), DIR, LINK("anywhere")], ids=["file", "dir", "symlink"]
    )
    def test_merge_to_a_free_path_renames(self, backend: Backend, root: str, src: Node) -> None:
        self.need(backend, Cap.ARCHIVE)
        moved: dict[str, Node] = {"s": src}
        if src == DIR:
            moved.update({"s/x": FILE(b"1"), "s/sub": DIR, "s/sub/y": FILE(b"2")})
        self.seed(backend, root, {**moved, "t": DIR})
        backend.merge(self.at(root, "s"), self.at(root, "t/moved"))
        assert self.observe(backend, root) == {
            "t": DIR,
            **{"t/moved" + rel[len("s") :]: node for rel, node in moved.items()},
        }

    def test_merge_of_directories_merges_entry_by_entry_and_removes_the_source(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(
            backend,
            root,
            {
                "s": DIR,
                "s/a": FILE(b"new a"),
                "s/sub": DIR,
                "s/sub/x": FILE(b"new x"),
                "s/sub/deeper": DIR,
                "s/sub/deeper/z": FILE(b"z"),
                "s/fresh": DIR,
                "s/fresh/y": FILE(b"y"),
                "s/empty": DIR,
                "t": DIR,
                "t/a": FILE(b"old a"),
                "t/keep": FILE(b"kept"),
                "t/sub": DIR,
                "t/sub/x": FILE(b"old x"),
                "t/sub/w": FILE(b"kept w"),
            },
        )
        backend.merge(self.at(root, "s"), self.at(root, "t"))
        assert self.observe(backend, root) == {
            "t": DIR,
            "t/a": FILE(b"new a"),
            "t/keep": FILE(b"kept"),
            "t/sub": DIR,
            "t/sub/x": FILE(b"new x"),
            "t/sub/w": FILE(b"kept w"),
            "t/sub/deeper": DIR,
            "t/sub/deeper/z": FILE(b"z"),
            "t/fresh": DIR,
            "t/fresh/y": FILE(b"y"),
            "t/empty": DIR,
        }

    def test_merge_replaces_a_non_directory_and_never_follows_a_symlink_at_the_destination(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(
            backend,
            root,
            {
                "new": FILE(b"new"),
                "old": FILE(b"old"),
                "s": DIR,
                "s/to-d": FILE(b"replaces the link"),
                "s/link": LINK("new-target"),
                "t": DIR,
                "t/to-d": LINK("../d"),
                "t/link": FILE(b"replaced by a link"),
                "d": DIR,
                "d/inside": FILE(b"untouched"),
            },
        )
        backend.merge(self.at(root, "new"), self.at(root, "old"))
        backend.merge(self.at(root, "s"), self.at(root, "t"))
        assert self.observe(backend, root) == {
            "old": FILE(b"new"),
            "t": DIR,
            "t/to-d": FILE(b"replaces the link"),
            "t/link": LINK("new-target"),
            "d": DIR,
            "d/inside": FILE(b"untouched"),
        }

    def test_merge_keeps_the_destination_directory_with_its_own_mode(
        self, backend: Backend, root: str
    ) -> None:
        # t is an empty directory, where a plain rename of s onto it would
        # succeed and take s's mode. unpack gives the two different modes.
        self.need(backend, Cap.ARCHIVE, Cap.STAT)
        data = make_archive([("s", DIR, 0o750), ("s/f", FILE(b"1")), ("t", DIR, 0o700)])
        self.seed(backend, root, {"a.tgz": FILE(data)})
        backend.unpack(self.at(root, "a.tgz"), self.at(root, "stage"))
        s, t = self.at(root, "stage/s"), self.at(root, "stage/t")
        modes = (backend.stat(s).mode, backend.stat(t).mode)
        backend.merge(s, t)
        assert self.observe(backend, root) == {
            "stage": DIR,
            "stage/t": DIR,
            "stage/t/f": FILE(b"1"),
        }
        if None not in modes:
            assert modes == (0o750, 0o700), [oct(m or 0) for m in modes]
            assert backend.stat(t).mode == 0o700

    @pytest.mark.parametrize(
        ("at_src", "at_dst", "error"),
        [
            (FILE(b"new"), DIR, IsADirectory),
            (LINK("elsewhere"), DIR, IsADirectory),
            (DIR, FILE(b"old"), NotADirectory),
            (DIR, LINK("../../d"), NotADirectory),
        ],
        ids=["file-onto-dir", "symlink-onto-dir", "dir-onto-file", "dir-onto-symlink-to-dir"],
    )
    def test_merge_clash_raises_about_the_deepest_path_and_moves_nothing(
        self, backend: Backend, root: str, at_src: Node, at_dst: Node, error: type[Exception]
    ) -> None:
        # Kinds are taken by lstat: a symlink is a non-directory at either end.
        self.need(backend, Cap.ARCHIVE)
        tree = {
            "d": DIR,
            "d/inside": FILE(b"untouched"),
            "s": DIR,
            "s/sub": DIR,
            "s/sub/c": at_src,
            "t": DIR,
            "t/sub": DIR,
            "t/sub/c": at_dst,
        }
        if at_src == DIR:
            tree["s/sub/c/x"] = FILE(b"x")
        if at_dst == DIR:
            tree["t/sub/c/y"] = FILE(b"y")
        self.seed(backend, root, tree)
        with pytest.raises(error) as err:
            backend.merge(self.at(root, "s"), self.at(root, "t"))
        assert err.value.path == self.at(root, "t/sub/c")  # type: ignore[attr-defined]
        assert self.observe(backend, root) == tree

    def test_merge_of_a_file_onto_a_directory_raises_is_a_directory(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        tree = {"f": FILE(b"1"), "d": DIR, "d/x": FILE(b"x")}
        self.seed(backend, root, tree)
        with pytest.raises(IsADirectory) as err:
            backend.merge(self.at(root, "f"), self.at(root, "d"))
        assert err.value.path == self.at(root, "d")
        assert self.observe(backend, root) == tree

    def test_merge_stops_at_the_first_error_and_loses_nothing(
        self, backend: Backend, root: str
    ) -> None:
        # Entries merge in no particular order, so each one is either still in
        # s or already in t, never both, never neither.
        self.need(backend, Cap.ARCHIVE)
        tree = {
            "s": DIR,
            "s/a": FILE(b"a"),
            "s/b": DIR,
            "s/b/f": FILE(b"bf"),
            "s/c": FILE(b"c"),
            "s/clash": DIR,
            "s/clash/x": FILE(b"x"),
            "t": DIR,
            "t/clash": FILE(b"old"),
        }
        self.seed(backend, root, tree)
        with pytest.raises(NotADirectory) as err:
            backend.merge(self.at(root, "s"), self.at(root, "t"))
        assert err.value.path == self.at(root, "t/clash")
        after = self.observe(backend, root)
        for rel in ("s", "s/clash", "s/clash/x", "t", "t/clash"):
            assert after.get(rel) == tree[rel], rel
        movable = ("a", "b", "b/f", "c")
        for rel in movable:
            in_s, in_t = after.get(f"s/{rel}"), after.get(f"t/{rel}")
            assert (in_s is None) != (in_t is None), f"{rel}: in s {in_s}, in t {in_t}"
            assert (in_s or in_t) == tree[f"s/{rel}"], rel
        allowed = {"s", "s/clash", "s/clash/x", "t", "t/clash"}
        allowed |= {f"{side}/{rel}" for side in "st" for rel in movable}
        assert set(after) <= allowed, sorted(set(after) - allowed)

    @pytest.mark.parametrize("dst", ["s", "s/inside", "s/sub/deeper"])
    def test_merge_into_itself_or_its_own_subtree_raises_before_anything_moves(
        self, backend: Backend, root: str, dst: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        tree = {"s": DIR, "s/sub": DIR, "s/f": FILE(b"1")}
        self.seed(backend, root, tree)
        with pytest.raises(OperationFailed):
            backend.merge(self.at(root, "s"), self.at(root, dst))
        assert self.observe(backend, root) == tree

    def test_merge_to_a_sibling_sharing_a_name_prefix_is_not_a_subtree_merge(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(backend, root, {"s": DIR, "s/f": FILE(b"1")})
        backend.merge(self.at(root, "s"), self.at(root, "sb"))
        assert self.observe(backend, root) == {"sb": DIR, "sb/f": FILE(b"1")}

    def test_merge_of_a_missing_source_raises_not_found(self, backend: Backend, root: str) -> None:
        self.need(backend, Cap.ARCHIVE)
        self.seed(backend, root, {"t": DIR})
        with pytest.raises(NotFound):
            backend.merge(self.at(root, "missing"), self.at(root, "t"))
        assert self.observe(backend, root) == {"t": DIR}

    def test_merge_to_a_missing_parent_raises_not_found_and_keeps_the_source(
        self, backend: Backend, root: str
    ) -> None:
        self.need(backend, Cap.ARCHIVE)
        tree = {"s": DIR, "s/f": FILE(b"1")}
        self.seed(backend, root, tree)
        with pytest.raises(NotFound):
            backend.merge(self.at(root, "s"), self.at(root, "missing/t"))
        assert self.observe(backend, root) == tree

    # -- hostile names --------------------------------------------------------------------

    @pytest.mark.parametrize("name", HOSTILE_NAMES, ids=repr)
    def test_hostile_names_round_trip_through_every_operation(
        self, backend: Backend, root: str, local_tmp: Path, name: str
    ) -> None:
        # The caps this test calls, named one by one: ``*Cap`` would also ask
        # for ARCHIVE and skip the test on every backend without it.
        self.need(
            backend,
            Cap.LIST,
            Cap.STAT,
            Cap.MKDIR,
            Cap.DELETE,
            Cap.RENAME,
            Cap.COPY_WITHIN,
            Cap.UPLOAD,
            Cap.DOWNLOAD,
        )
        src = self.local_file(local_tmp, "src", b"payload")
        folder = self.at(root, name)
        backend.mkdir(folder)
        target = backend.join(folder, name)
        backend.upload(src, target)
        assert [(e.name, e.path) for e in backend.list_dir(folder)] == [(name, target)]
        assert backend.stat(target).name == name
        backend.copy_within(target, target + " copy")
        renamed = backend.join(folder, "renamed " + name)
        backend.rename(target, renamed)
        assert sorted(e.name for e in backend.list_dir(folder)) == sorted(
            [name + " copy", "renamed " + name]
        )
        backend.download(renamed, str(local_tmp / "got"))
        assert (local_tmp / "got").read_bytes() == b"payload"
        backend.delete(backend.join(folder, name + " copy"))
        assert self.observe(backend, root) == {
            name: DIR,
            f"{name}/renamed {name}": FILE(b"payload"),
        }
        backend.delete(folder, recursive=True)
        assert self.observe(backend, root) == {}

    @pytest.mark.parametrize("name", HOSTILE_NAMES, ids=repr)
    def test_hostile_names_round_trip_through_upload_and_download(
        self, backend: Backend, root: str, local_tmp: Path, name: str
    ) -> None:
        self.need(backend, Cap.UPLOAD, Cap.DOWNLOAD)
        src = self.local_file(local_tmp, "src", b"payload " + name.encode())
        backend.upload(src, self.at(root, name), overwrite=True)
        assert self.observe(backend, root) == {name: FILE(b"payload " + name.encode())}
        backend.download(self.at(root, name), str(local_tmp / "got"))
        assert (local_tmp / "got").read_bytes() == b"payload " + name.encode()

    # -- missing capabilities ---------------------------------------------------------------

    @pytest.mark.parametrize("cap", list(Cap), ids=lambda cap: cap.name)
    def test_missing_capability_raises_unsupported_and_changes_nothing(
        self, backend: Backend, root: str, local_tmp: Path, cap: Cap
    ) -> None:
        if cap in backend.caps:
            pytest.skip(f"{backend.label} has {cap.name}")
        archive = make_archive([("x", FILE(b"x"))])
        self.seed(
            backend,
            root,
            {"f": FILE(b"data"), "d": DIR, "d/x": FILE(b"x"), "a.tgz": FILE(archive)},
        )
        up = self.local_file(local_tmp, "up", b"up")
        got = str(local_tmp / "got")
        f, d, missing, new = (self.at(root, rel) for rel in ("f", "d", "missing", "new"))
        tgz = self.at(root, "a.tgz")
        calls: dict[Cap, list[Callable[[], object]]] = {
            Cap.LIST: [lambda: backend.list_dir(root), lambda: backend.walk(root)],
            Cap.STAT: [lambda: backend.stat(f), lambda: backend.exists(f)],
            Cap.MKDIR: [lambda: backend.mkdir(new)],
            Cap.DELETE: [
                lambda: backend.delete(f),
                lambda: backend.delete(d, recursive=True),
                lambda: backend.delete_many([f, d], recursive=True),
                lambda: backend.delete_many([]),
            ],
            Cap.RENAME: [lambda: backend.rename(f, new)],
            Cap.COPY_WITHIN: [
                lambda: backend.copy_within(f, new),
                lambda: backend.copy_within(d, new),
            ],
            Cap.UPLOAD: [
                lambda: backend.upload(up, new),
                lambda: backend.upload(up, f, overwrite=True),
            ],
            Cap.DOWNLOAD: [lambda: backend.download(f, got)],
            Cap.ARCHIVE: [
                lambda: backend.pack(root, ["f", "d"], new),
                lambda: backend.unpack(tgz, new),
                lambda: backend.merge(d, new),
                lambda: backend.merge(f, self.at(root, "d/x")),
            ],
        }
        # Unsupported comes before any other check, even for a path that would fail anyway.
        calls[Cap.LIST].append(lambda: backend.list_dir(missing))
        calls[Cap.STAT].append(lambda: backend.exists(missing))
        calls[Cap.DELETE].append(lambda: backend.delete(missing))
        calls[Cap.DELETE].append(lambda: backend.delete_many([missing]))
        calls[Cap.RENAME].append(lambda: backend.rename(missing, new))
        calls[Cap.DOWNLOAD].append(lambda: backend.download(missing, got))
        calls[Cap.ARCHIVE] += [
            lambda: backend.pack(missing, ["x"], new),
            lambda: backend.pack(root, ["f"], tgz),
            lambda: backend.unpack(missing, new),
            lambda: backend.unpack(tgz, d),
            lambda: backend.merge(missing, new),
            lambda: backend.merge(d, self.at(root, "d/inside")),
        ]
        before = self.observe(backend, root)
        for call in calls[cap]:
            with pytest.raises(Unsupported):
                call()
        assert self.observe(backend, root) == before
        assert sorted(os.listdir(local_tmp)) == ["up"]
