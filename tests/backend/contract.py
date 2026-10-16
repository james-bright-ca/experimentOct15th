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
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Callable, Mapping
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

__all__ = ["DIR", "FILE", "HOSTILE_NAMES", "LINK", "BackendContract", "Node"]

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

    # -- hostile names --------------------------------------------------------------------

    @pytest.mark.parametrize("name", HOSTILE_NAMES, ids=repr)
    def test_hostile_names_round_trip_through_every_operation(
        self, backend: Backend, root: str, local_tmp: Path, name: str
    ) -> None:
        self.need(backend, *Cap)
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
        self.seed(backend, root, {"f": FILE(b"data"), "d": DIR, "d/x": FILE(b"x")})
        up = self.local_file(local_tmp, "up", b"up")
        got = str(local_tmp / "got")
        f, d, missing, new = (self.at(root, rel) for rel in ("f", "d", "missing", "new"))
        calls: dict[Cap, list[Callable[[], object]]] = {
            Cap.LIST: [lambda: backend.list_dir(root), lambda: backend.walk(root)],
            Cap.STAT: [lambda: backend.stat(f), lambda: backend.exists(f)],
            Cap.MKDIR: [lambda: backend.mkdir(new)],
            Cap.DELETE: [lambda: backend.delete(f), lambda: backend.delete(d, recursive=True)],
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
        }
        # Unsupported comes before any other check, even for a path that would fail anyway.
        calls[Cap.LIST].append(lambda: backend.list_dir(missing))
        calls[Cap.STAT].append(lambda: backend.exists(missing))
        calls[Cap.DELETE].append(lambda: backend.delete(missing))
        calls[Cap.RENAME].append(lambda: backend.rename(missing, new))
        calls[Cap.DOWNLOAD].append(lambda: backend.download(missing, got))
        before = self.observe(backend, root)
        for call in calls[cap]:
            with pytest.raises(Unsupported):
                call()
        assert self.observe(backend, root) == before
        assert sorted(os.listdir(local_tmp)) == ["up"]
