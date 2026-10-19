"""[B] The engine's per-file path (``archive=False``, the default) over
LocalBackend and MemoryBackend.

Specification: the docstrings of ``twinfm.ops.model`` (Engine, Callbacks,
Report) and ``twinfm.ops.engine``, and the carried-over [B] criteria.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.ops.support import Answers, fail, faulty, symlink, tree, write
from twinfm.backend.base import (
    ALL_CAPS,
    Backend,
    Cap,
    NotADirectory,
    OperationFailed,
    PermissionDenied,
    Unsupported,
)
from twinfm.backend.local import LocalBackend
from twinfm.backend.memory import MemoryBackend
from twinfm.ops import engine
from twinfm.ops.model import Conflict, OnError

MEMORY_CAPS = ALL_CAPS - {Cap.ARCHIVE}


def mem_tree(m: MemoryBackend, root: str) -> dict[str, tuple[str, bytes | str | None]]:
    """``tree`` for a MemoryBackend: every path below ``root``, relative to it."""
    result: dict[str, tuple[str, bytes | str | None]] = {}
    for path, value in m.snapshot().items():
        if path.startswith(root + "/"):
            kind = "dir" if value is None else "symlink" if isinstance(value, str) else "file"
            result[path[len(root) + 1 :]] = (kind, value)
    return result


class Store:
    """One side of a transfer: a backend, a root directory on it, and
    direct seeding and observation that bypass the engine."""

    def __init__(self, backend: Backend, root: str) -> None:
        self.b = backend
        self.root = root

    def path(self, rel: str) -> str:
        return f"{self.root}/{rel}" if rel else self.root

    def file(self, rel: str, data: bytes) -> str:
        if isinstance(self.b, MemoryBackend):
            self.b.add_file(self.path(rel), data)
        else:
            write(self.path(rel), data)
        return self.path(rel)

    def dir(self, rel: str) -> str:
        if isinstance(self.b, MemoryBackend):
            self.b.add_dir(self.path(rel))
        else:
            os.makedirs(self.path(rel), exist_ok=True)
        return self.path(rel)

    def link(self, rel: str, target: str) -> str:
        if isinstance(self.b, MemoryBackend):
            self.b.add_symlink(self.path(rel), target)
        else:
            symlink(self.path(rel), target)
        return self.path(rel)

    def tree(self, rel: str = "") -> dict[str, tuple[str, bytes | str | None]]:
        if isinstance(self.b, MemoryBackend):
            return mem_tree(self.b, self.path(rel))
        return tree(self.path(rel))


def local_store(tmp_path: Path, name: str, backend: Backend | None = None) -> Store:
    root = tmp_path.resolve() / name
    root.mkdir()
    return Store(backend or LocalBackend(), str(root))


def memory_store(name: str, backend: MemoryBackend | None = None) -> Store:
    m = backend or MemoryBackend()
    root = f"/{name}"
    m.add_dir(root)
    return Store(m, root)


PAIRS: dict[str, Callable[[Path], tuple[Store, Store]]] = {
    "local->memory": lambda t: (local_store(t, "src"), memory_store("dst")),
    "memory->local": lambda t: (memory_store("src"), local_store(t, "dst")),
    "local->local": lambda t: (
        (shared := local_store(t, "src")),
        Store(shared.b, local_store(t, "dst").root),
    ),
    "memory->memory": lambda t: (
        (shared := memory_store("src")),
        Store(shared.b, memory_store("dst", shared.b).root),  # type: ignore[arg-type]
    ),
}


def seed_selection(s: Store) -> list[str]:
    """One file and one nested directory; returns the selection."""
    s.file("one.txt", b"\x00one\xff" * 100)
    s.file("nest/top.bin", bytes(range(256)))
    s.file("nest/deeper/leaf", b"leaf")
    s.dir("nest/deeper/empty")
    return [s.path("one.txt"), s.path("nest")]


SELECTION_TREE = {
    "one.txt": ("file", b"\x00one\xff" * 100),
    "nest": ("dir", None),
    "nest/top.bin": ("file", bytes(range(256))),
    "nest/deeper": ("dir", None),
    "nest/deeper/leaf": ("file", b"leaf"),
    "nest/deeper/empty": ("dir", None),
}


# -- copying a selection ------------------------------------------------------------------


@pytest.mark.parametrize("pair", ["local->memory", "memory->local", "local->local"])
def test_a_file_and_a_nested_directory_arrive_byte_for_byte(tmp_path: Path, pair: str) -> None:
    src, dst = PAIRS[pair](tmp_path)
    paths = seed_selection(src)
    answers = Answers()
    report = engine.copy(src.b, paths, dst.b, dst.root, answers)
    assert report.ok
    assert sorted(report.done) == sorted(paths)
    assert dst.tree() == SELECTION_TREE
    assert src.tree() == SELECTION_TREE  # a copy leaves the source alone
    assert answers.conflicts_asked == [] and answers.errors_asked == []


# -- conflicts --------------------------------------------------------------------------------


def two_conflicts(pair: str, tmp_path: Path) -> tuple[Store, Store, list[str]]:
    src, dst = PAIRS[pair](tmp_path)
    paths = [src.file(n, f"new {n}".encode()) for n in ("a", "b", "c")]
    dst.file("a", b"old a")
    dst.file("b", b"old b")
    return src, dst, paths


@pytest.mark.parametrize("pair", ["local->memory", "memory->local", "local->local"])
def test_each_existing_file_is_asked_about_once_and_skip_keeps_it(tmp_path: Path, pair: str) -> None:
    src, dst, paths = two_conflicts(pair, tmp_path)
    answers = Answers([Conflict.SKIP, Conflict.OVERWRITE])
    report = engine.copy(src.b, paths, dst.b, dst.root, answers)
    assert answers.conflicts_asked == [(src.path("a"), dst.path("a")), (src.path("b"), dst.path("b"))]
    assert dst.tree() == {"a": ("file", b"old a"), "b": ("file", b"new b"), "c": ("file", b"new c")}
    assert report.skipped == [src.path("a")]
    assert sorted(report.done) == [src.path("b"), src.path("c")]
    assert report.failed == [] and not report.cancelled


@pytest.mark.parametrize("pair", ["local->memory", "memory->local", "local->local"])
def test_overwrite_all_stops_further_prompts(tmp_path: Path, pair: str) -> None:
    src, dst, paths = two_conflicts(pair, tmp_path)
    answers = Answers([Conflict.OVERWRITE_ALL], conflict=Conflict.CANCEL)
    report = engine.copy(src.b, paths, dst.b, dst.root, answers)
    assert len(answers.conflicts_asked) == 1
    assert dst.tree() == {"a": ("file", b"new a"), "b": ("file", b"new b"), "c": ("file", b"new c")}
    assert report.ok and sorted(report.done) == sorted(paths)


@pytest.mark.parametrize("pair", ["local->memory", "memory->local", "local->local"])
def test_skip_all_stops_further_prompts_and_keeps_every_conflict(tmp_path: Path, pair: str) -> None:
    src, dst, paths = two_conflicts(pair, tmp_path)
    answers = Answers([Conflict.SKIP_ALL], conflict=Conflict.CANCEL)
    report = engine.copy(src.b, paths, dst.b, dst.root, answers)
    assert len(answers.conflicts_asked) == 1
    assert dst.tree() == {"a": ("file", b"old a"), "b": ("file", b"old b"), "c": ("file", b"new c")}
    assert sorted(report.skipped) == [src.path("a"), src.path("b")]
    assert report.done == [src.path("c")]


@pytest.mark.parametrize("pair", ["local->memory", "memory->local", "local->local"])
def test_cancel_writes_no_further_files(tmp_path: Path, pair: str) -> None:
    src, dst, paths = two_conflicts(pair, tmp_path)
    answers = Answers([Conflict.OVERWRITE, Conflict.CANCEL])
    report = engine.copy(src.b, paths, dst.b, dst.root, answers)
    assert report.cancelled is True and not report.ok
    assert len(answers.conflicts_asked) == 2
    assert dst.tree() == {"a": ("file", b"new a"), "b": ("file", b"old b")}  # c never written
    assert report.done == [src.path("a")]


# -- refusing a copy onto itself ---------------------------------------------------------


@pytest.mark.parametrize("store", ["local", "memory"])
@pytest.mark.parametrize("op", ["copy", "move"])
@pytest.mark.parametrize("into", ["d", "d/sub", "d/sub/deeper"])
def test_a_directory_into_itself_or_its_subtree_is_refused_before_any_write(
    tmp_path: Path, store: str, op: str, into: str
) -> None:
    s = local_store(tmp_path, "s") if store == "local" else memory_store("s")
    s.file("d/f", b"f")
    s.file("d/sub/deeper/g", b"g")
    before = s.tree()
    answers = Answers()
    with pytest.raises(OperationFailed):
        getattr(engine, op)(s.b, [s.path("d")], s.b, s.path(into), answers)
    assert s.tree() == before
    assert answers.progress_seen == []


@pytest.mark.parametrize("op", ["copy", "move"])
def test_a_directory_into_its_subtree_through_a_symlink_is_refused(tmp_path: Path, op: str) -> None:
    s = local_store(tmp_path, "s")
    s.file("d/sub/f", b"f")
    s.link("alias", "d/sub")
    before = s.tree()
    with pytest.raises(OperationFailed):
        getattr(engine, op)(s.b, [s.path("d")], s.b, s.path("alias"), Answers())
    assert s.tree() == before


# -- target resolution -------------------------------------------------------------------


@pytest.mark.parametrize("pair", ["local->memory", "memory->local", "local->local"])
def test_an_existing_directory_target_receives_the_items_inside_it(tmp_path: Path, pair: str) -> None:
    src, dst = PAIRS[pair](tmp_path)
    src.file("y/z", b"z")
    paths = [src.file("x", b"x"), src.path("y")]
    dst.dir("into")
    report = engine.copy(src.b, paths, dst.b, dst.path("into"), Answers())
    assert report.ok
    assert dst.tree() == {
        "into": ("dir", None),
        "into/x": ("file", b"x"),
        "into/y": ("dir", None),
        "into/y/z": ("file", b"z"),
    }


@pytest.mark.parametrize("pair", ["local->memory", "memory->local", "local->local"])
@pytest.mark.parametrize("what", ["file", "dir"])
def test_one_item_onto_a_missing_target_takes_that_name(tmp_path: Path, pair: str, what: str) -> None:
    src, dst = PAIRS[pair](tmp_path)
    if what == "file":
        path = src.file("item", b"i")
    else:
        src.file("item/inner", b"i")
        path = src.path("item")
    report = engine.copy(src.b, [path], dst.b, dst.path("renamed"), Answers())
    assert report.ok and report.done == [path]
    expected = (
        {"renamed": ("file", b"i")}
        if what == "file"
        else {"renamed": ("dir", None), "renamed/inner": ("file", b"i")}
    )
    assert dst.tree() == expected


@pytest.mark.parametrize("pair", ["local->memory", "memory->local", "local->local"])
@pytest.mark.parametrize("target", ["a-file", "missing"])
@pytest.mark.parametrize("op", ["copy", "move"])
def test_two_items_need_a_directory_target(tmp_path: Path, pair: str, target: str, op: str) -> None:
    src, dst = PAIRS[pair](tmp_path)
    paths = [src.file("p", b"p"), src.file("q", b"q")]
    dst.file("a-file", b"keep")
    before_src, before_dst = src.tree(), dst.tree()
    with pytest.raises(NotADirectory):
        getattr(engine, op)(src.b, paths, dst.b, dst.path(target), Answers())
    assert (src.tree(), dst.tree()) == (before_src, before_dst)


# -- moves between stores ----------------------------------------------------------------


def test_a_failed_upload_keeps_that_file_and_its_parents_and_removes_every_uploaded_one(
    tmp_path: Path,
) -> None:
    src = local_store(tmp_path, "src")
    for rel in ("proj/a", "proj/sub/b", "proj/sub/c", "proj/other/d"):
        src.file(rel, rel.encode())
    m = faulty(MemoryBackend)(faults={"upload": fail(when=lambda local, path, *a: path.endswith("/sub/b"))})
    dst = memory_store("dst", m)
    answers = Answers(error=OnError.SKIP)
    report = engine.move(src.b, [src.path("proj")], dst.b, dst.root, answers)
    assert [p for p, _ in answers.errors_asked] == [src.path("proj/sub/b")]
    assert [(p, type(e)) for p, e in report.failed] == [(src.path("proj/sub/b"), OperationFailed)]
    assert report.done == []
    assert src.tree() == {
        "proj": ("dir", None),
        "proj/sub": ("dir", None),
        "proj/sub/b": ("file", b"proj/sub/b"),
    }
    assert dst.tree() == {
        "proj": ("dir", None),
        "proj/a": ("file", b"proj/a"),
        "proj/sub": ("dir", None),
        "proj/sub/c": ("file", b"proj/sub/c"),
        "proj/other": ("dir", None),
        "proj/other/d": ("file", b"proj/other/d"),
    }


def test_a_move_between_stores_removes_the_sources_once_everything_went(tmp_path: Path) -> None:
    src, dst = memory_store("src"), local_store(tmp_path, "dst")
    paths = seed_selection(src)
    report = engine.move(src.b, paths, dst.b, dst.root, Answers())
    assert report.ok and sorted(report.done) == sorted(paths)
    assert dst.tree() == SELECTION_TREE
    assert src.tree() == {}


def test_moving_out_of_a_store_without_delete_is_refused_before_any_write(tmp_path: Path) -> None:
    m = MemoryBackend(MEMORY_CAPS - {Cap.DELETE})
    src, dst = memory_store("src", m), local_store(tmp_path, "dst")
    paths = seed_selection(src)
    with pytest.raises(Unsupported):
        engine.move(src.b, paths, dst.b, dst.root, Answers())
    assert dst.tree() == {} and src.tree() == SELECTION_TREE


# -- a destination without STAT and MKDIR ---------------------------------------------------


def test_copying_into_a_store_without_stat_and_mkdir_uploads_files_and_fails_directories(
    tmp_path: Path,
) -> None:
    src = local_store(tmp_path, "src")
    paths = [src.file("f1", b"new f1"), src.file("f2", b"f2"), src.file("d1/inner", b"i")]
    paths[2] = src.path("d1")
    m = MemoryBackend({Cap.UPLOAD, Cap.DOWNLOAD})
    m.add_dir("/in")
    m.add_file("/in/f1", b"old f1")
    answers = Answers(conflict=Conflict.CANCEL, error=OnError.ABORT)
    report = engine.copy(src.b, paths, m, "/in", answers)
    assert mem_tree(m, "/in") == {"f1": ("file", b"new f1"), "f2": ("file", b"f2")}
    assert sorted(report.done) == [src.path("f1"), src.path("f2")]
    assert [(p, type(e)) for p, e in report.failed] == [(src.path("d1"), Unsupported)]
    assert answers.conflicts_asked == [] and answers.errors_asked == []
    assert not report.cancelled


# -- symlinks between stores ---------------------------------------------------------------


@pytest.mark.parametrize("archive", [None, False], ids=["archive-omitted", "archive-False"])
def test_a_symlink_to_a_file_arrives_as_content_and_one_to_a_directory_is_skipped(
    tmp_path: Path, archive: bool | None
) -> None:
    src, dst = local_store(tmp_path, "src"), memory_store("dst")
    src.file("real.txt", b"content")
    src.file("realdir/x", b"x")
    paths = [src.link("to-file", "real.txt"), src.link("to-dir", "realdir")]
    kw = {} if archive is None else {"archive": archive}
    report = engine.copy(src.b, paths, dst.b, dst.root, Answers(), **kw)
    assert dst.tree() == {"to-file": ("file", b"content")}
    assert report.done == [src.path("to-file")]
    assert report.skipped == [src.path("to-dir")]
    assert report.failed == [] and not report.cancelled


def test_symlinks_inside_a_copied_directory_follow_the_same_rule(tmp_path: Path) -> None:
    src, dst = memory_store("src"), local_store(tmp_path, "dst")
    src.file("d/real.txt", b"content")
    src.file("d/realdir/x", b"x")
    src.link("d/to-file", "real.txt")
    src.link("d/to-dir", "realdir")
    report = engine.copy(src.b, [src.path("d")], dst.b, dst.root, Answers())
    tree_ = dst.tree()
    assert tree_["d/to-file"] == ("file", b"content")
    assert "d/to-dir" not in tree_
    assert report.skipped == [src.path("d/to-dir")]
    assert report.done == []  # something inside d was skipped


# -- delete --------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ["local", "memory"])
def test_deleting_a_mixed_selection_removes_links_not_their_targets(tmp_path: Path, store: str) -> None:
    s = local_store(tmp_path, "s") if store == "local" else memory_store("s")
    s.file("keep/target.txt", b"t")
    s.file("keep/targetdir/inside", b"i")
    paths = [
        s.file("f", b"f"),
        s.link("link-to-file", "keep/target.txt"),
        s.link("link-to-dir", "keep/targetdir"),
        s.link("dangling", "nowhere"),
        s.file("d/deep/er/file", b"x"),
    ]
    paths[-1] = s.path("d")
    answers = Answers()
    report = engine.delete(s.b, paths, answers)
    assert report.ok and report.done == paths
    assert s.tree() == {
        "keep": ("dir", None),
        "keep/target.txt": ("file", b"t"),
        "keep/targetdir": ("dir", None),
        "keep/targetdir/inside": ("file", b"i"),
    }
    assert answers.errors_asked == []


def failing_memory(times: int | None, *names: str) -> MemoryBackend:
    return faulty(MemoryBackend)(
        faults={
            "delete": fail(
                PermissionDenied,
                when=lambda path, *a, **k: os.path.basename(path) in names,
                times=times,
            )
        }
    )


def test_delete_retry_tries_the_item_again() -> None:
    s = memory_store("s", failing_memory(1, "b"))
    paths = [s.file(n, n.encode()) for n in ("a", "b", "c")]
    answers = Answers(errors=[OnError.RETRY])
    report = engine.delete(s.b, paths, answers)
    assert [p for p, _ in answers.errors_asked] == [s.path("b")]
    assert report.ok and report.done == paths
    assert s.tree() == {}


def test_delete_skip_all_suppresses_further_error_prompts() -> None:
    s = memory_store("s", failing_memory(None, "b", "c"))
    paths = [s.file(n, n.encode()) for n in ("a", "b", "c", "d")]
    answers = Answers(errors=[OnError.SKIP_ALL], error=OnError.ABORT)
    report = engine.delete(s.b, paths, answers)
    assert [p for p, _ in answers.errors_asked] == [s.path("b")]
    assert [(p, type(e)) for p, e in report.failed] == [
        (s.path("b"), PermissionDenied),
        (s.path("c"), PermissionDenied),
    ]
    assert report.done == [s.path("a"), s.path("d")]
    assert s.tree() == {"b": ("file", b"b"), "c": ("file", b"c")}
    assert not report.cancelled


def test_delete_abort_stops_further_deletions() -> None:
    s = memory_store("s", failing_memory(None, "b"))
    paths = [s.file(n, n.encode()) for n in ("a", "b", "c")]
    answers = Answers(errors=[OnError.ABORT])
    report = engine.delete(s.b, paths, answers)
    assert report.cancelled is True
    assert report.done == [s.path("a")]
    assert [p for p, _ in report.failed] == [s.path("b")]
    assert s.tree() == {"b": ("file", b"b"), "c": ("file", b"c")}


# -- cancelled() --------------------------------------------------------------------------


@pytest.mark.parametrize("pair", ["local->memory", "memory->local", "local->local"])
def test_cancelled_after_the_first_file_stops_the_batch(tmp_path: Path, pair: str) -> None:
    src, dst = PAIRS[pair](tmp_path)
    paths = [src.file(n, n.encode()) for n in ("a", "b", "c")]
    answers = Answers(cancel_when=lambda a: len(a.progress_seen) >= 1)
    report = engine.copy(src.b, paths, dst.b, dst.root, answers)
    assert report.cancelled is True
    assert report.done == [src.path("a")]
    assert dst.tree() == {"a": ("file", b"a")}


def test_cancelled_after_the_first_file_stops_a_delete(tmp_path: Path) -> None:
    s = local_store(tmp_path, "s")
    paths = [s.file(n, n.encode()) for n in ("a", "b", "c")]
    answers = Answers(cancel_when=lambda a: len(a.progress_seen) >= 1)
    report = engine.delete(s.b, paths, answers)
    assert report.cancelled is True and report.done == [s.path("a")]
    assert s.tree() == {"b": ("file", b"b"), "c": ("file", b"c")}
