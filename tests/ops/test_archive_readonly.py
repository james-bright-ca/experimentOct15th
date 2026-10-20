"""Architect decision A4: when the archive-mode scan finds a directory
without owner read, write and search permission (selected, nested, or in
one item among several), the whole batch goes per file.

Specification: ``Engine.copy`` in twinfm.ops.model ("it falls back in the
same way when the scan finds a directory without owner read, write and
search permission") and A4's checks: no .tgz is moved, no .twinfm- entry is
left, and the trees, Report and questions equal those of archive=False;
trees whose directories are all 0o755 still use archive mode.

The fake remote runs the remote programs as the test user, so a missed
fallback fails with PermissionDenied on the "remote" side too. Permission
bits do not bind root, hence the skip; the ``chmod`` fixture gives owner
rwx back in teardown.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.backend.fake_btty import FakeBtty
from tests.ops.support import STAGES, Answers, World, leftovers, make_world, ops_of, tree, write
from tests.ops.test_archive_fallback import assert_same, no_archive_moved, run_both
from tests.ops.test_archive_transfer import DIRECTIONS, Direction, archives_moved
from twinfm.ops import engine
from twinfm.ops.model import Conflict, OnError, Report

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="permission bits do not bind root (A4)"
)

Chmod = Callable[[str, int], None]

# Each layout: the files to create below the source root, the directories to
# chmod (deepest first) with their mode, and the selected names.
LAYOUTS: dict[str, tuple[list[str], list[tuple[str, int]], list[str]]] = {
    "selected-0o555": (["ro/f", "ro/g"], [("ro", 0o555)], ["ro"]),
    "nested-0o555": (["top/a", "top/ro/f"], [("top/ro", 0o555)], ["top"]),
    "nested-0o500": (["top/a", "top/ro/f"], [("top/ro", 0o500)], ["top"]),
    "deep-0o555": (["top/mid/ro/f", "top/mid/x"], [("top/mid/ro", 0o555)], ["top"]),
    "one-of-several": (["ok/a", "ro/f", "plain.txt"], [("ro", 0o555)], ["ok", "ro", "plain.txt"]),
    "no-owner-search": (["top/a", "top/lk/f"], [("top/lk", 0o600)], ["top"]),
}


def seeder(way: Direction, layout: str, chmod: Chmod, mode: int | None = None, extra: Callable[[World], None] | None = None) -> Callable[[World], None]:
    files, dirs, _ = LAYOUTS[layout]

    def seed(w: World) -> None:
        root = way.src_root(w)
        for rel in files:
            write(f"{root}/{rel}", rel.encode())
        if extra is not None:
            extra(w)
        for rel, m in dirs:
            chmod(f"{root}/{rel}", m if mode is None else mode)

    return seed


def act_on(way: Direction, layout: str, op: str) -> Callable[[World, Answers, bool], Report]:
    names = LAYOUTS[layout][2]

    def act(w: World, a: Answers, archive: bool) -> Report:
        paths = [f"{way.src_root(w)}/{n}" for n in names]
        return getattr(engine, op)(way.src(w), paths, way.dst(w), way.dst_root(w), a, archive=archive)

    return act


@DIRECTIONS
@pytest.mark.parametrize("layout", list(LAYOUTS))
def test_a_tree_holding_a_locked_directory_is_copied_per_file(
    tmp_path: Path, way: Direction, layout: str, chmod: Chmod, systmp: str, fake_cls: type[FakeBtty]) -> None:
    off, on = run_both(tmp_path, seeder(way, layout, chmod), act_on(way, layout, "copy"), fake_cls=fake_cls)
    assert_same(off, on)
    no_archive_moved(on)
    assert os.listdir(on.w.rtmp) == [] and os.listdir(systmp) == []
    if layout != "no-owner-search":  # an unsearchable directory cannot be copied at all
        assert on.report.ok


@DIRECTIONS
@pytest.mark.parametrize("layout", ["selected-0o555", "nested-0o555", "one-of-several"])
def test_a_tree_holding_a_locked_directory_is_moved_per_file(
    tmp_path: Path, way: Direction, layout: str, chmod: Chmod
) -> None:
    """Deleting from a read-only source directory fails the same way on both paths."""
    off, on = run_both(
        tmp_path, seeder(way, layout, chmod), act_on(way, layout, "move"), lambda: Answers(error=OnError.SKIP)
    )
    assert_same(off, on)
    no_archive_moved(on)
    assert on.report.failed  # the entries of the read-only directory could not be removed


@DIRECTIONS
def test_merging_a_locked_tree_into_an_existing_writable_directory_goes_per_file(
    tmp_path: Path, way: Direction, chmod: Chmod
) -> None:
    def existing(w: World) -> None:
        write(f"{way.dst_root(w)}/ro/already", b"there")

    off, on = run_both(
        tmp_path, seeder(way, "selected-0o555", chmod, extra=existing), act_on(way, "selected-0o555", "copy")
    )
    assert_same(off, on)
    no_archive_moved(on)
    assert tree(f"{way.dst_root(on.w)}/ro") == {
        "already": ("file", b"there"),
        "f": ("file", b"ro/f"),
        "g": ("file", b"ro/g"),
    }


@DIRECTIONS
def test_a_conflict_inside_a_locked_tree_is_asked_exactly_once(
    tmp_path: Path, way: Direction, chmod: Chmod
) -> None:
    def existing(w: World) -> None:
        write(f"{way.dst_root(w)}/ro/f", b"old f")

    off, on = run_both(
        tmp_path,
        seeder(way, "selected-0o555", chmod, extra=existing),
        act_on(way, "selected-0o555", "copy"),
        lambda: Answers([Conflict.SKIP]),
    )
    assert_same(off, on)
    assert len(on.answers.conflicts_asked) == 1
    assert tree(f"{way.dst_root(on.w)}/ro")["f"] == ("file", b"old f")


@DIRECTIONS
@pytest.mark.parametrize("layout", ["selected-0o555", "nested-0o555", "one-of-several"])
@pytest.mark.parametrize("op", ["copy", "move"])
def test_the_same_trees_with_writable_directories_use_archive_mode(
    tmp_path: Path, way: Direction, layout: str, op: str, chmod: Chmod, fake_cls: type[FakeBtty]) -> None:
    w = make_world(tmp_path / "w", fake_cls=fake_cls)
    seeder(way, layout, chmod, mode=0o755)(w)
    answers = Answers()
    report = act_on(way, layout, op)(w, answers, True)
    assert report.ok
    assert len(archives_moved(w)) == 1
    merges = ["m"] * len(LAYOUTS[layout][2])  # one merge per selected item
    assert ops_of(w.fake) == (["u", *merges] if way.name == "upload" else ["p"])
    assert answers.stages == STAGES
    assert leftovers(w.base) == []
