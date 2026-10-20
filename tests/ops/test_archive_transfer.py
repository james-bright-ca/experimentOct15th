"""[W3] Archive mode, copy: LocalBackend <-> BttyBackend (exec mode) over a
fake btty.

Specification: ``Engine.copy`` / ``Progress.stage`` in twinfm.ops.model,
the flow and temporaries described in twinfm.ops.archive, and the [W3]
criteria. The fake remote is a directory below tmp_path, so both sides are
observed directly on the local filesystem.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.backend.fake_btty import FakeBtty
from tests.ops.support import (
    STAGES,
    ArchiveFakeBtty,
    TEMP_PREFIX,
    Answers,
    World,
    after_stage,
    fail,
    gets_of,
    is_tgz,
    leftovers,
    make_world,
    modes,
    named,
    ops_of,
    puts_of,
    summary,
    symlink,
    tree,
    write,
)
from twinfm.backend.base import (
    Backend,
    IsADirectory,
    NotADirectory,
    OperationFailed,
    PermissionDenied,
)
from twinfm.ops import engine
from twinfm.ops.model import Conflict, OnError

# -- the two directions ----------------------------------------------------------------


@dataclass(frozen=True)
class Direction:
    """Which side of a World is the source. Both roots are local directories
    (the fake remote is one), so tests seed and observe them directly."""

    name: str

    def src(self, w: World) -> Backend:
        return w.lb if self.name == "upload" else w.btty

    def dst(self, w: World) -> Backend:
        return w.btty if self.name == "upload" else w.lb

    def src_root(self, w: World) -> str:
        return w.local if self.name == "upload" else w.home

    def dst_root(self, w: World) -> str:
        return w.home if self.name == "upload" else w.local


UP, DOWN = Direction("upload"), Direction("download")
DIRECTIONS = pytest.mark.parametrize("way", [UP, DOWN], ids=["upload", "download"])


def packed(w: World, since: int = 0) -> bool:
    """Whether anything was packed yet, on either side."""
    local = any(name == "pack" for name, _ in getattr(w.lb, "log", []))
    return local or any(op in ("p", "f") for op in ops_of(w.fake, since))


def archives_moved(w: World, since: int = 0) -> list[str]:
    """The .tgz paths sent with --put or fetched with --get."""
    return [p for p in puts_of(w.fake, since) + gets_of(w.fake, since) if p.endswith(".tgz")]


def seed_project(root: str, files: int) -> None:
    """``proj/``: ``files`` files of mixed modes in data/, an executable, an
    empty directory, a private directory and a relative symlink."""
    for i in range(files):
        mode = (0o644, 0o600, 0o640, 0o755)[i % 4]
        write(f"{root}/proj/data/f{i:02}.txt", f"file {i}\n".encode() * (i + 1), mode)
    write(f"{root}/proj/bin/run.sh", b"#!/bin/sh\necho hi\n", 0o755)
    os.makedirs(f"{root}/proj/empty")
    write(f"{root}/proj/private/key", b"secret", 0o600)
    os.chmod(f"{root}/proj/private", 0o700)
    symlink(f"{root}/proj/data/latest", "f00.txt")
    symlink(f"{root}/proj/up", "../proj/bin")


def clean(w: World, systmp: str) -> None:
    """No temporary is left anywhere: no .twinfm- entry on either side
    (remote_tmp included) and nothing in the local temp directory."""
    assert leftovers(w.base) == []
    assert os.listdir(w.rtmp) == []
    assert os.listdir(systmp) == []


# -- folders arrive whole -----------------------------------------------------------------


def upload_calls(tmp_path: Path, fake_cls: type[FakeBtty], files: int, systmp: str) -> int:
    w = make_world(tmp_path / f"w{files}", fake_cls=fake_cls)
    seed_project(w.local, files)
    report = engine.copy(w.lb, [w.lpath("proj")], w.btty, w.home, Answers(), archive=True)
    assert report.ok and report.done == [w.lpath("proj")]
    assert tree(w.rpath("proj")) == tree(w.lpath("proj"))
    assert modes(w.rpath("proj")) == modes(w.lpath("proj"))
    assert tree(w.rpath("proj"))["data/latest"] == ("symlink", "f00.txt")
    assert tree(w.rpath("proj"))["up"] == ("symlink", "../proj/bin")
    [archive] = archives_moved(w)
    assert os.path.dirname(archive) == w.home and os.path.basename(archive).startswith(TEMP_PREFIX)
    assert ops_of(w.fake) == ["u", "m"]
    clean(w, systmp)
    return len(w.fake.calls)


def test_a_new_folder_uploads_as_one_archive_whatever_its_size(
    tmp_path: Path, fake_cls: type[FakeBtty], systmp: str
) -> None:
    few = upload_calls(tmp_path, fake_cls, 5, systmp)
    many = upload_calls(tmp_path, fake_cls, 50, systmp)
    assert few == many
    assert many <= 8


def test_a_remote_folder_downloads_as_one_archive_through_remote_tmp(
    tmp_path: Path, fake_cls: type[FakeBtty], systmp: str
) -> None:
    w = make_world(tmp_path / "w", fake_cls=fake_cls)
    seed_project(w.home, 12)
    want, want_modes = tree(w.rpath("proj")), modes(w.rpath("proj"))
    report = engine.copy(w.btty, [w.rpath("proj")], w.lb, w.local, Answers(), archive=True)
    assert report.ok and report.done == [w.rpath("proj")]
    assert tree(w.lpath("proj")) == want
    assert modes(w.lpath("proj")) == want_modes
    assert tree(w.rpath("proj")) == want  # a copy leaves the source alone
    [archive] = archives_moved(w)
    assert os.path.dirname(archive) == w.rtmp and os.path.basename(archive).startswith(TEMP_PREFIX)
    assert ops_of(w.fake) == ["p"]
    clean(w, systmp)


@DIRECTIONS
def test_one_folder_onto_a_missing_target_takes_that_name(
    tmp_path: Path, way: Direction, systmp: str
) -> None:
    w = make_world(tmp_path / "w")
    seed_project(way.src_root(w), 3)
    target = os.path.join(way.dst_root(w), "renamed")
    report = engine.copy(way.src(w), [f"{way.src_root(w)}/proj"], way.dst(w), target, Answers(), archive=True)
    assert report.ok
    assert tree(target) == tree(f"{way.src_root(w)}/proj")
    assert not os.path.exists(os.path.join(way.dst_root(w), "proj"))
    assert len(archives_moved(w)) == 1
    clean(w, systmp)


@DIRECTIONS
def test_several_items_travel_in_one_archive(tmp_path: Path, way: Direction, systmp: str, fake_cls: type[FakeBtty]) -> None:
    w = make_world(tmp_path / "w", fake_cls=fake_cls)
    root = way.src_root(w)
    write(f"{root}/a/one", b"1")
    write(f"{root}/b/deep/two", b"2")
    write(f"{root}/c.txt", b"3")
    symlink(f"{root}/d-link", "c.txt")
    paths = [f"{root}/{n}" for n in ("a", "b", "c.txt", "d-link")]
    report = engine.copy(way.src(w), paths, way.dst(w), way.dst_root(w), Answers(), archive=True)
    assert report.ok and sorted(report.done) == sorted(paths)
    got = tree(way.dst_root(w))
    assert got == {
        "a": ("dir", None),
        "a/one": ("file", b"1"),
        "b": ("dir", None),
        "b/deep": ("dir", None),
        "b/deep/two": ("file", b"2"),
        "c.txt": ("file", b"3"),
        "d-link": ("symlink", "c.txt"),
    }
    assert len(archives_moved(w)) == 1
    clean(w, systmp)


HOSTILE = ["with space", "it's", 'dq"', "new\nline", "-dash", "--", "star*?[ab]", "$(echo x)", "semi;&|>", "back\\slash", "ünï-日本"]


@DIRECTIONS
def test_hostile_names_survive_the_archive_round_trip(tmp_path: Path, way: Direction, systmp: str, fake_cls: type[FakeBtty]) -> None:
    w = make_world(tmp_path / "w", fake_cls=fake_cls)
    root = way.src_root(w)
    for i, name in enumerate(HOSTILE):
        write(f"{root}/{name}/{name}", f"{i}".encode())
    paths = [f"{root}/{name}" for name in HOSTILE]
    report = engine.copy(way.src(w), paths, way.dst(w), way.dst_root(w), Answers(), archive=True)
    assert report.ok and sorted(report.done) == sorted(paths)
    assert tree(way.dst_root(w)) == tree(root)
    assert len(archives_moved(w)) == 1
    clean(w, systmp)


# -- conflicts are settled before anything is packed ---------------------------------------


def conflict_world(
    tmp_path: Path, way: Direction, fake_cls: type[FakeBtty] = ArchiveFakeBtty
) -> tuple[World, list[str]]:
    w = make_world(tmp_path / "w", local_faults={}, fake_cls=fake_cls)
    src, dst = way.src_root(w), way.dst_root(w)
    for rel in ("proj/a", "proj/b", "proj/c", "proj/sub/d"):
        write(f"{src}/{rel}", f"new {rel}".encode())
    write(f"{dst}/proj/a", b"old a")
    write(f"{dst}/proj/b", b"old b")
    return w, [f"{src}/proj"]


@DIRECTIONS
def test_each_existing_file_is_asked_once_before_packing_and_skip_keeps_it(
    tmp_path: Path, way: Direction, systmp: str, fake_cls: type[FakeBtty]) -> None:
    w, paths = conflict_world(tmp_path, way, fake_cls)
    seen_packed: list[bool] = []
    answers = Answers(
        [Conflict.SKIP, Conflict.OVERWRITE],
        probe=lambda kind, path: seen_packed.append(packed(w)),
    )
    src, dst = way.src_root(w), way.dst_root(w)
    report = engine.copy(way.src(w), paths, way.dst(w), dst, answers, archive=True)
    assert answers.conflicts_asked == [
        (f"{src}/proj/a", f"{dst}/proj/a"),
        (f"{src}/proj/b", f"{dst}/proj/b"),
    ]
    assert seen_packed == [False, False]
    assert packed(w)  # archive mode did run afterwards
    assert tree(f"{dst}/proj") == {
        "a": ("file", b"old a"),
        "b": ("file", b"new proj/b"),
        "c": ("file", b"new proj/c"),
        "sub": ("dir", None),
        "sub/d": ("file", b"new proj/sub/d"),
    }
    assert report.skipped == [f"{src}/proj/a"]
    assert report.done == [] and report.failed == [] and not report.cancelled
    clean(w, systmp)


@DIRECTIONS
@pytest.mark.parametrize("answer", [Conflict.OVERWRITE_ALL, Conflict.SKIP_ALL])
def test_an_all_answer_stops_further_conflict_prompts(
    tmp_path: Path, way: Direction, answer: Conflict, systmp: str
) -> None:
    w, paths = conflict_world(tmp_path, way)
    answers = Answers([answer], conflict=Conflict.CANCEL)
    dst = way.dst_root(w)
    report = engine.copy(way.src(w), paths, way.dst(w), dst, answers, archive=True)
    assert len(answers.conflicts_asked) == 1
    old = answer is Conflict.SKIP_ALL
    assert tree(f"{dst}/proj")["a"] == ("file", b"old a" if old else b"new proj/a")
    assert tree(f"{dst}/proj")["b"] == ("file", b"old b" if old else b"new proj/b")
    assert tree(f"{dst}/proj")["sub/d"] == ("file", b"new proj/sub/d")
    assert len(report.skipped) == (2 if old else 0)
    assert not report.cancelled
    clean(w, systmp)


@DIRECTIONS
@pytest.mark.parametrize("first", [True, False], ids=["first-question", "second-question"])
def test_cancel_at_a_conflict_writes_nothing_and_leaves_no_archive(
    tmp_path: Path, way: Direction, first: bool, systmp: str, fake_cls: type[FakeBtty]) -> None:
    w, paths = conflict_world(tmp_path, way, fake_cls)
    before = tree(w.base)
    answers = Answers([Conflict.CANCEL] if first else [Conflict.OVERWRITE, Conflict.CANCEL])
    report = engine.copy(way.src(w), paths, way.dst(w), way.dst_root(w), answers, archive=True)
    assert report.cancelled is True
    assert tree(w.base) == before
    assert not packed(w)
    assert archives_moved(w) == []
    assert answers.stages == []
    clean(w, systmp)


# -- file/directory clashes ----------------------------------------------------------------------


@DIRECTIONS
def test_a_clash_goes_to_ask_error_against_the_source_path_and_the_rest_arrives(
    tmp_path: Path, way: Direction, systmp: str, fake_cls: type[FakeBtty]) -> None:
    w = make_world(tmp_path / "w", fake_cls=fake_cls)
    src, dst = way.src_root(w), way.dst_root(w)
    write(f"{src}/proj/x", b"file x")
    write(f"{src}/proj/sub/f", b"f")
    write(f"{src}/proj/y", b"y")
    write(f"{dst}/proj/x/keep", b"keep")  # a directory where the source has a file
    write(f"{dst}/proj/sub", b"a file")  # a file where the source has a directory
    answers = Answers(error=OnError.SKIP)
    report = engine.copy(way.src(w), [f"{src}/proj"], way.dst(w), dst, answers, archive=True)
    assert sorted(p for p, _ in answers.errors_asked) == [f"{src}/proj/sub", f"{src}/proj/x"]
    assert sorted(((p, type(e)) for p, e in report.failed), key=lambda x: x[0]) == [
        (f"{src}/proj/sub", NotADirectory),
        (f"{src}/proj/x", IsADirectory),
    ]
    assert tree(f"{dst}/proj") == {
        "x": ("dir", None),
        "x/keep": ("file", b"keep"),
        "sub": ("file", b"a file"),
        "y": ("file", b"y"),
    }
    assert report.done == [] and not report.cancelled
    clean(w, systmp)


@DIRECTIONS
def test_a_selected_folder_onto_a_file_fails_alone(tmp_path: Path, way: Direction, systmp: str) -> None:
    w = make_world(tmp_path / "w")
    src, dst = way.src_root(w), way.dst_root(w)
    write(f"{src}/d1/f", b"1")
    write(f"{src}/d2/f", b"2")
    write(f"{dst}/d1", b"a file")
    paths = [f"{src}/d1", f"{src}/d2"]
    answers = Answers(error=OnError.SKIP)
    report = engine.copy(way.src(w), paths, way.dst(w), dst, answers, archive=True)
    assert [(p, type(e)) for p, e in report.failed] == [(f"{src}/d1", NotADirectory)]
    assert report.done == [f"{src}/d2"]
    assert tree(dst) == {"d1": ("file", b"a file"), "d2": ("dir", None), "d2/f": ("file", b"2")}
    clean(w, systmp)


@DIRECTIONS
def test_retry_on_a_clash_looks_again(tmp_path: Path, way: Direction, systmp: str) -> None:
    """RETRY re-checks the destination: once the clash is gone, the entry goes."""
    w = make_world(tmp_path / "w")
    src, dst = way.src_root(w), way.dst_root(w)
    write(f"{src}/proj/x", b"file x")
    write(f"{src}/proj/y", b"y")
    write(f"{dst}/proj/x/keep", b"keep")

    def resolve(kind: str, path: str) -> None:
        os.remove(f"{dst}/proj/x/keep")
        os.rmdir(f"{dst}/proj/x")

    answers = Answers(errors=[OnError.RETRY], probe=resolve)
    report = engine.copy(way.src(w), [f"{src}/proj"], way.dst(w), dst, answers, archive=True)
    assert len(answers.errors_asked) == 1
    assert report.ok and report.done == [f"{src}/proj"]
    assert tree(f"{dst}/proj") == {"x": ("file", b"file x"), "y": ("file", b"y")}
    clean(w, systmp)


# -- progress and cancel -------------------------------------------------------------------------


@DIRECTIONS
@pytest.mark.parametrize("op", ["copy", "move"])
def test_progress_reports_the_four_stages_in_order(tmp_path: Path, way: Direction, op: str, fake_cls: type[FakeBtty]) -> None:
    w = make_world(tmp_path / "w", fake_cls=fake_cls)
    seed_project(way.src_root(w), 4)
    path = f"{way.src_root(w)}/proj"
    answers = Answers()
    report = getattr(engine, op)(way.src(w), [path], way.dst(w), way.dst_root(w), answers, archive=True)
    assert report.ok
    assert answers.stages == STAGES
    staged = [p for p in answers.progress_seen if p.stage is not None]
    assert all(p.op == op and p.current == path for p in staged)
    assert all(0 <= p.items_done <= p.items_total for p in staged)


@DIRECTIONS
@pytest.mark.parametrize("stage", STAGES[:3])
def test_cancel_between_stages_stops_with_nothing_written_and_no_temporaries(
    tmp_path: Path, way: Direction, stage: str, systmp: str, fake_cls: type[FakeBtty]) -> None:
    w = make_world(tmp_path / "w", fake_cls=fake_cls)
    seed_project(way.src_root(w), 4)
    write(f"{way.dst_root(w)}/proj/data/f00.txt", b"old")
    before_dst = tree(way.dst_root(w))
    answers = Answers(conflict=Conflict.OVERWRITE, cancel_when=after_stage(stage))
    report = engine.copy(
        way.src(w), [f"{way.src_root(w)}/proj"], way.dst(w), way.dst_root(w), answers, archive=True
    )
    assert report.cancelled is True
    assert answers.stages == STAGES[: STAGES.index(stage) + 1]
    assert tree(way.dst_root(w)) == before_dst
    assert report.done == []
    clean(w, systmp)


# -- step failures and cleanup -----------------------------------------------------------------


def two_items(w: World, way: Direction) -> list[str]:
    root = way.src_root(w)
    write(f"{root}/a/one", b"1")
    write(f"{root}/b/two", b"2")
    return [f"{root}/a", f"{root}/b"]


def transfer_fault(way: Direction, times: int | None = None) -> dict[str, dict]:
    """A failure of the archive transfer itself (--put or --get of the .tgz)."""
    method = "upload" if way is UP else "download"
    return {"btty_faults": {method: fail(OperationFailed, when=is_tgz, times=times)}}


@DIRECTIONS
def test_a_failed_step_is_asked_once_and_recorded_against_every_item(
    tmp_path: Path, way: Direction, systmp: str
) -> None:
    w = make_world(tmp_path / "w", **transfer_fault(way))
    paths = two_items(w, way)
    answers = Answers(error=OnError.SKIP)
    report = engine.copy(way.src(w), paths, way.dst(w), way.dst_root(w), answers, archive=True)
    assert len(answers.errors_asked) == 1
    assert sorted((p, type(e)) for p, e in report.failed) == [
        (paths[0], OperationFailed),
        (paths[1], OperationFailed),
    ]
    assert report.done == [] and not report.cancelled
    assert tree(way.dst_root(w)) == {}
    clean(w, systmp)


@DIRECTIONS
def test_abort_on_a_failed_step_cancels_the_batch(tmp_path: Path, way: Direction, systmp: str) -> None:
    w = make_world(tmp_path / "w", **transfer_fault(way))
    paths = two_items(w, way)
    report = engine.copy(
        way.src(w), paths, way.dst(w), way.dst_root(w), Answers(error=OnError.ABORT), archive=True
    )
    assert report.cancelled is True and report.done == []
    assert tree(way.dst_root(w)) == {}
    clean(w, systmp)


@DIRECTIONS
def test_retry_on_a_failed_step_runs_it_again(tmp_path: Path, way: Direction, systmp: str) -> None:
    w = make_world(tmp_path / "w", **transfer_fault(way, times=1))
    paths = two_items(w, way)
    answers = Answers(errors=[OnError.RETRY], error=OnError.ABORT)
    report = engine.copy(way.src(w), paths, way.dst(w), way.dst_root(w), answers, archive=True)
    assert len(answers.errors_asked) == 1
    assert report.ok and sorted(report.done) == sorted(paths)
    assert tree(way.dst_root(w)) == tree(way.src_root(w))
    clean(w, systmp)


def test_a_failed_unpack_keeps_nothing_behind(tmp_path: Path, systmp: str) -> None:
    w = make_world(tmp_path / "w", btty_faults={"unpack": fail(OperationFailed)})
    paths = two_items(w, UP)
    report = engine.copy(w.lb, paths, w.btty, w.home, Answers(error=OnError.SKIP), archive=True)
    assert sorted(p for p, _ in report.failed) == sorted(paths)
    assert tree(w.home) == {}
    clean(w, systmp)


def test_a_failed_merge_of_one_item_leaves_the_others(tmp_path: Path, systmp: str) -> None:
    w = make_world(
        tmp_path / "w",
        btty_faults={"merge": fail(OperationFailed, when=lambda s, d: d.endswith("/a"))},
    )
    paths = two_items(w, UP)
    report = engine.copy(w.lb, paths, w.btty, w.home, Answers(error=OnError.SKIP), archive=True)
    assert [(p, type(e)) for p, e in report.failed] == [(paths[0], OperationFailed)]
    assert report.done == [paths[1]]
    assert tree(w.home) == {"b": ("dir", None), "b/two": ("file", b"2")}
    clean(w, systmp)


def test_a_staging_directory_that_cannot_be_removed_is_reported(tmp_path: Path, systmp: str) -> None:
    w = make_world(
        tmp_path / "w",
        btty_faults={"delete": fail(PermissionDenied, when=named(TEMP_PREFIX))},
    )
    seed_project(w.local, 3)
    report = engine.copy(w.lb, [w.lpath("proj")], w.btty, w.home, Answers(), archive=True)
    assert tree(w.rpath("proj")) == tree(w.lpath("proj"))  # the copy itself worked
    [(path, err)] = report.failed
    assert isinstance(err, OperationFailed) and err.message == "temporary file left behind"
    assert os.path.dirname(path) == w.home and os.path.basename(path).startswith(TEMP_PREFIX)
    assert err.path == path
    assert leftovers(w.base) == [path]  # the one reported, and only that one
    assert os.listdir(systmp) == []


def test_a_remote_archive_that_cannot_be_removed_is_reported(tmp_path: Path, systmp: str) -> None:
    w = make_world(
        tmp_path / "w",
        btty_faults={"delete": fail(PermissionDenied, when=named(TEMP_PREFIX))},
    )
    seed_project(w.home, 3)
    report = engine.copy(w.btty, [w.rpath("proj")], w.lb, w.local, Answers(), archive=True)
    assert tree(w.lpath("proj")) == tree(w.rpath("proj"))
    [(path, err)] = report.failed
    assert err.message == "temporary file left behind"
    assert os.path.dirname(path) == w.rtmp and path.endswith(".tgz")
    assert leftovers(w.base) == [path]
    assert os.listdir(systmp) == []


def test_report_after_a_successful_archive_copy_names_paths_as_given(tmp_path: Path) -> None:
    w = make_world(tmp_path / "w")
    paths = two_items(w, UP)
    report = engine.copy(w.lb, paths, w.btty, w.home, Answers(), archive=True)
    assert summary(report, w.base) == {
        "done": ["local/a", "local/b"],
        "skipped": [],
        "failed": [],
        "cancelled": False,
    }
