"""[W3] Archive mode, move: sources go only after the merge returned, and
whatever stayed behind keeps its parent directories.

Specification: ``Engine.move`` in twinfm.ops.model ("In archive mode the
sources are deleted only after the merge returned ... If the merge raised,
no source is deleted"), ``Engine.copy`` for entries of kind "other", and the
[W3] move criterion.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.backend.fake_btty import FakeBtty
from tests.ops.support import ArchiveFakeBtty, Answers, World, fail, leftovers, make_world, tree, write
from tests.ops.test_archive_transfer import DIRECTIONS, Direction, archives_moved, clean, seed_project
from twinfm.backend.base import FsError, IsADirectory, OperationFailed
from twinfm.ops import engine
from twinfm.ops.model import Conflict, OnError


def merge_spy(seen: list[dict[str, Any]], root: str) -> Any:
    """A merge hook that records the tree below ``root`` at each merge."""

    def hook(src: str, dst: str) -> FsError | None:
        seen.append(tree(root))
        return None

    return hook


def hooked_world(tmp_path: Path, fake_cls: type[FakeBtty] = ArchiveFakeBtty) -> World:
    """A World whose two backends both take hooks (``faults``) afterwards."""
    return make_world(tmp_path / "w", btty_faults={}, local_faults={}, fake_cls=fake_cls)


def on_merge(w: World, way: Direction, hook: Any) -> None:
    way.dst(w).faults["merge"] = hook  # type: ignore[attr-defined]


@DIRECTIONS
def test_a_clean_move_removes_the_whole_source_after_the_merge(
    tmp_path: Path, way: Direction, systmp: str, fake_cls: type[FakeBtty]) -> None:
    w = hooked_world(tmp_path, fake_cls)
    root = way.src_root(w)
    seen: list[dict[str, Any]] = []
    on_merge(w, way, merge_spy(seen, root))
    seed_project(root, 6)
    before = tree(root)
    want = tree(f"{root}/proj")
    report = engine.move(way.src(w), [f"{root}/proj"], way.dst(w), way.dst_root(w), Answers(), archive=True)
    assert report.ok and report.done == [f"{root}/proj"]
    assert seen == [before]  # the whole source was still there when the merge ran
    assert tree(f"{way.dst_root(w)}/proj") == want
    assert not os.path.lexists(f"{root}/proj")
    assert len(archives_moved(w)) == 1
    clean(w, systmp)


@DIRECTIONS
def test_a_skipped_file_and_its_parents_stay_while_everything_else_goes(
    tmp_path: Path, way: Direction, systmp: str, fake_cls: type[FakeBtty]) -> None:
    w = hooked_world(tmp_path, fake_cls)
    src, dst = way.src_root(w), way.dst_root(w)
    seen: list[dict[str, Any]] = []
    on_merge(w, way, merge_spy(seen, src))
    for rel in ("proj/a", "proj/sub/deep/b", "proj/sub/c", "proj/other/d"):
        write(f"{src}/{rel}", f"new {rel}".encode())
    write(f"{dst}/proj/sub/deep/b", b"old b")
    before = tree(src)
    answers = Answers([Conflict.SKIP])
    report = engine.move(way.src(w), [f"{src}/proj"], way.dst(w), dst, answers, archive=True)
    assert report.skipped == [f"{src}/proj/sub/deep/b"]
    assert report.done == [] and report.failed == [] and not report.cancelled
    assert seen == [before]  # nothing was deleted before the merge
    assert tree(src) == {
        "proj": ("dir", None),
        "proj/sub": ("dir", None),
        "proj/sub/deep": ("dir", None),
        "proj/sub/deep/b": ("file", b"new proj/sub/deep/b"),
    }
    assert tree(f"{dst}/proj") == {
        "a": ("file", b"new proj/a"),
        "sub": ("dir", None),
        "sub/deep": ("dir", None),
        "sub/deep/b": ("file", b"old b"),
        "sub/c": ("file", b"new proj/sub/c"),
        "other": ("dir", None),
        "other/d": ("file", b"new proj/other/d"),
    }
    clean(w, systmp)


@DIRECTIONS
def test_a_clashing_entry_and_its_parents_stay_at_the_source(
    tmp_path: Path, way: Direction, systmp: str
) -> None:
    w = make_world(tmp_path / "w")
    src, dst = way.src_root(w), way.dst_root(w)
    write(f"{src}/proj/keep/x", b"file x")
    write(f"{src}/proj/keep/y", b"y")
    write(f"{src}/proj/z", b"z")
    write(f"{dst}/proj/keep/x/inside", b"a directory here")
    report = engine.move(way.src(w), [f"{src}/proj"], way.dst(w), dst, Answers(error=OnError.SKIP), archive=True)
    assert [(p, type(e)) for p, e in report.failed] == [(f"{src}/proj/keep/x", IsADirectory)]
    assert tree(src) == {
        "proj": ("dir", None),
        "proj/keep": ("dir", None),
        "proj/keep/x": ("file", b"file x"),
    }
    assert tree(f"{dst}/proj") == {
        "keep": ("dir", None),
        "keep/x": ("dir", None),
        "keep/x/inside": ("file", b"a directory here"),
        "keep/y": ("file", b"y"),
        "z": ("file", b"z"),
    }
    clean(w, systmp)


@DIRECTIONS
@pytest.mark.parametrize("answer", [OnError.SKIP, OnError.ABORT])
def test_a_failed_merge_deletes_no_source(
    tmp_path: Path, way: Direction, answer: OnError, systmp: str
) -> None:
    w = hooked_world(tmp_path)
    on_merge(w, way, fail(OperationFailed))
    src, dst = way.src_root(w), way.dst_root(w)
    seed_project(src, 4)
    before_src, before_dst = tree(src), tree(dst)
    report = engine.move(way.src(w), [f"{src}/proj"], way.dst(w), dst, Answers(error=answer), archive=True)
    assert [(p, type(e)) for p, e in report.failed] == [(f"{src}/proj", OperationFailed)]
    assert report.done == []
    assert report.cancelled is (answer is OnError.ABORT)
    assert tree(src) == before_src
    assert tree(dst) == before_dst
    clean(w, systmp)


@DIRECTIONS
def test_one_item_whose_merge_failed_keeps_its_sources_and_the_other_moves(
    tmp_path: Path, way: Direction, systmp: str
) -> None:
    w = hooked_world(tmp_path)
    on_merge(w, way, fail(OperationFailed, when=lambda s, d: d.endswith("/a")))
    src, dst = way.src_root(w), way.dst_root(w)
    write(f"{src}/a/one", b"1")
    write(f"{src}/b/two", b"2")
    paths = [f"{src}/a", f"{src}/b"]
    report = engine.move(way.src(w), paths, way.dst(w), dst, Answers(error=OnError.SKIP), archive=True)
    assert [p for p, _ in report.failed] == [paths[0]]
    assert report.done == [paths[1]]
    assert tree(src) == {"a": ("dir", None), "a/one": ("file", b"1")}
    assert tree(dst) == {"b": ("dir", None), "b/two": ("file", b"2")}
    clean(w, systmp)


@DIRECTIONS
@pytest.mark.parametrize("op", ["copy", "move"])
def test_a_fifo_is_reported_skipped_and_stays_with_its_parents(
    tmp_path: Path, way: Direction, op: str, systmp: str
) -> None:
    w = make_world(tmp_path / "w")
    src, dst = way.src_root(w), way.dst_root(w)
    write(f"{src}/proj/a", b"a")
    write(f"{src}/proj/pipes/other", b"o")
    os.mkfifo(f"{src}/proj/pipes/fifo")
    report = getattr(engine, op)(way.src(w), [f"{src}/proj"], way.dst(w), dst, Answers(), archive=True)
    assert report.skipped == [f"{src}/proj/pipes/fifo"]
    assert report.failed == [] and report.done == []
    assert tree(f"{dst}/proj") == {
        "a": ("file", b"a"),
        "pipes": ("dir", None),
        "pipes/other": ("file", b"o"),
    }
    if op == "move":
        assert sorted(tree(src)) == ["proj", "proj/pipes", "proj/pipes/fifo"]
    assert len(archives_moved(w)) == 1
    assert leftovers(w.base) == []
