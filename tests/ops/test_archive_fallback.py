"""[W3] When archive mode does not apply, or falls back, ``archive=True``
ends exactly as ``archive=False``: the same trees, the same Report, the same
questions, each asked once.

Specification: ``Engine.copy`` in twinfm.ops.model (when archive mode
applies; "the whole batch falls back to the per-file transfer, ending with
the tree and Report that archive=False gives"), the "Falling back"
paragraph of twinfm.ops.archive ("the answers the user already gave are
replayed instead of asked again"), and the [W3] fallback criterion.

Each test runs one scenario twice, in two identical Worlds: once with
``archive=False`` and once with ``archive=True``, and compares them.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.backend.fake_btty import FakeBtty
from tests.ops.support import (
    Answers,
    World,
    fail,
    leftovers,
    make_world,
    no_duplicates,
    ops_of,
    relative,
    remote_without_tar_filter,
    summary,
    symlink,
    tree,
    write,
)
from tests.ops.test_archive_transfer import DIRECTIONS, DOWN, UP, Direction, archives_moved
from twinfm.backend.base import FsError, Unsupported
from twinfm.backend.btty_scripts import merge_script, stat_script, unpack_script
from twinfm.backend.btty import BttyBackend
from twinfm.backend.local import LocalBackend
from twinfm.backend.memory import MemoryBackend
from twinfm.ops import engine
from twinfm.ops.model import Conflict, OnError, Report


@dataclass
class Outcome:
    w: World
    report: Report
    answers: Answers


def run_both(
    tmp_path: Path,
    seed: Callable[[World], None],
    act: Callable[[World, Answers, bool], Report],
    answers: Callable[[], Answers] = Answers,
    **world: Any,
) -> tuple[Outcome, Outcome]:
    """The scenario with archive=False, then with archive=True, each in a fresh World."""
    outcomes = []
    for archive in (False, True):
        w = make_world(tmp_path / f"archive-{int(archive)}", **world)  # equal lengths
        seed(w)
        a = answers()
        outcomes.append(Outcome(w, act(w, a, archive), a))
    return outcomes[0], outcomes[1]


def assert_same(off: Outcome, on: Outcome) -> None:
    assert tree(on.w.local) == tree(off.w.local)
    assert tree(on.w.remote) == tree(off.w.remote)
    assert summary(on.report, on.w.base) == summary(off.report, off.w.base)
    assert relative(on.answers.conflicts_asked, on.w.base) == relative(off.answers.conflicts_asked, off.w.base)
    assert relative(on.answers.errors_asked, on.w.base) == relative(off.answers.errors_asked, off.w.base)
    assert no_duplicates(on.report)
    assert leftovers(on.w.base) == []


def no_archive_moved(on: Outcome) -> None:
    assert archives_moved(on.w) == []
    assert ops_of(on.w.fake) == []
    assert all(p.stage is None for p in on.answers.progress_seen)


def seed_conflicts(way: Direction) -> Callable[[World], None]:
    def seed(w: World) -> None:
        src, dst = way.src_root(w), way.dst_root(w)
        for rel in ("proj/a", "proj/b", "proj/c", "proj/sub/d"):
            write(f"{src}/{rel}", f"new {rel}".encode())
        write(f"{dst}/proj/a", b"old a")
        write(f"{dst}/proj/b", b"old b")

    return seed


def copy_proj(way: Direction, op: str = "copy") -> Callable[[World, Answers, bool], Report]:
    def act(w: World, a: Answers, archive: bool) -> Report:
        fn = getattr(engine, op)
        return fn(way.src(w), [f"{way.src_root(w)}/proj"], way.dst(w), way.dst_root(w), a, archive=archive)

    return act


def skip_then_overwrite() -> Answers:
    return Answers([Conflict.SKIP, Conflict.OVERWRITE])


# -- archive mode does not apply ------------------------------------------------------------


@DIRECTIONS
def test_a_single_file_goes_per_file(tmp_path: Path, way: Direction) -> None:
    def seed(w: World) -> None:
        write(f"{way.src_root(w)}/one.txt", b"one")
        write(f"{way.dst_root(w)}/one.txt", b"old")

    def act(w: World, a: Answers, archive: bool) -> Report:
        path = f"{way.src_root(w)}/one.txt"
        return engine.copy(way.src(w), [path], way.dst(w), way.dst_root(w), a, archive=archive)

    off, on = run_both(tmp_path, seed, act)
    assert_same(off, on)
    no_archive_moved(on)
    assert len(on.answers.conflicts_asked) == 1


@DIRECTIONS
def test_a_selected_symlink_to_a_directory_goes_per_file_and_is_skipped(
    tmp_path: Path, way: Direction
) -> None:
    def seed(w: World) -> None:
        write(f"{way.src_root(w)}/real/f", b"f")
        symlink(f"{way.src_root(w)}/alias", "real")

    def act(w: World, a: Answers, archive: bool) -> Report:
        path = f"{way.src_root(w)}/alias"
        return engine.copy(way.src(w), [path], way.dst(w), way.dst_root(w), a, archive=archive)

    off, on = run_both(tmp_path, seed, act)
    assert_same(off, on)
    no_archive_moved(on)
    assert on.report.skipped == [f"{way.src_root(on.w)}/alias"]


@DIRECTIONS
def test_a_selection_from_two_directories_goes_per_file(tmp_path: Path, way: Direction) -> None:
    def seed(w: World) -> None:
        write(f"{way.src_root(w)}/one/a/x", b"x")
        write(f"{way.src_root(w)}/two/b/y", b"y")

    def act(w: World, a: Answers, archive: bool) -> Report:
        root = way.src_root(w)
        return engine.copy(way.src(w), [f"{root}/one/a", f"{root}/two/b"], way.dst(w), way.dst_root(w), a, archive=archive)

    off, on = run_both(tmp_path, seed, act)
    assert_same(off, on)
    no_archive_moved(on)


@DIRECTIONS
def test_a_transfer_only_btty_side_goes_per_file(tmp_path: Path, way: Direction) -> None:
    def seed(w: World) -> None:
        for name in ("a", "b"):
            write(f"{way.src_root(w)}/{name}", name.encode())
        if way is UP:
            write(f"{way.src_root(w)}/sub/c", b"c")
        write(f"{way.dst_root(w)}/a", b"old a")

    def act(w: World, a: Answers, archive: bool) -> Report:
        root = way.src_root(w)
        names = ["a", "b", "sub"] if way is UP else ["a", "b"]
        return engine.copy(way.src(w), [f"{root}/{n}" for n in names], way.dst(w), way.dst_root(w), a, archive=archive)

    off, on = run_both(tmp_path, seed, act, btty_exec=False)
    assert_same(off, on)
    no_archive_moved(on)


class NoArchiveDirBtty(BttyBackend):
    """A btty store with no place to pack a remote selection in."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.archive_dir = None


def test_a_remote_source_without_archive_dir_goes_per_file(tmp_path: Path) -> None:
    off, on = run_both(
        tmp_path, seed_conflicts(DOWN), copy_proj(DOWN), skip_then_overwrite, btty_cls=NoArchiveDirBtty
    )
    assert_same(off, on)
    no_archive_moved(on)


@pytest.mark.parametrize("same", [True, False], ids=["one-store", "two-local-backends"])
@pytest.mark.parametrize("op", ["copy", "move"])
def test_both_sides_local_go_per_file(tmp_path: Path, same: bool, op: str) -> None:
    results = []
    for archive in (False, True):
        base = tmp_path.resolve() / str(archive)
        src_root, dst_root = base / "src", base / "dst"
        for rel in ("proj/a", "proj/sub/b", "other/c"):
            write(str(src_root / rel), rel.encode())
        write(str(dst_root / "proj/a"), b"old")
        symlink(str(src_root / "proj/link"), "a")
        src = LocalBackend()
        dst = src if same else LocalBackend()
        answers = Answers([Conflict.SKIP])
        report = getattr(engine, op)(
            src, [str(src_root / "proj"), str(src_root / "other")], dst, str(dst_root), answers, archive=archive
        )
        results.append((tree(str(base)), summary(report, str(base)), len(answers.conflicts_asked), answers.stages))
    assert results[1] == results[0]
    assert results[1][3] == []
    assert leftovers(str(tmp_path)) == []


@pytest.mark.parametrize("op", ["copy", "move"])
@pytest.mark.parametrize("direction", ["local->memory", "memory->local"])
def test_a_memory_side_lacks_archive_and_goes_per_file(tmp_path: Path, op: str, direction: str) -> None:
    results = []
    for archive in (False, True):
        local_root = tmp_path.resolve() / str(archive)  # same depth for both runs
        local_root.mkdir()
        m = MemoryBackend()
        m.add_dir("/m")
        lb = LocalBackend()
        if direction == "local->memory":
            for rel in ("proj/a", "proj/b", "proj/sub/c"):
                write(str(local_root / rel), rel.encode())
            m.add_file("/m/proj/a", b"old a")
            src, dst, paths, target = lb, m, [str(local_root / "proj")], "/m"
        else:
            for rel in ("proj/a", "proj/b", "proj/sub/c"):
                m.add_file(f"/m/{rel}", rel.encode())
            write(str(local_root / "proj/a"), b"old a")
            src, dst, paths, target = m, lb, ["/m/proj"], str(local_root)
        answers = Answers([Conflict.SKIP])
        report = getattr(engine, op)(src, paths, dst, target, answers, archive=archive)
        results.append(
            (
                tree(str(local_root)),
                m.snapshot(),
                summary(report, str(local_root)),
                len(answers.conflicts_asked),
                answers.stages,
            )
        )
    assert results[1] == results[0]
    assert results[1][4] == []


# -- archive mode falls back --------------------------------------------------------------------


def no_tar_filter(w: World) -> None:
    remote_without_tar_filter(w.fake)


@pytest.mark.parametrize("op", ["copy", "move"])
def test_a_remote_without_tar_filter_falls_back_after_its_failed_pack(
    tmp_path: Path, op: str, fake_cls: type[FakeBtty]
) -> None:
    """Download: the remote pack reports NOFILTER (Unsupported), after the
    conflicts were settled; the per-file transfer replays those answers."""

    def seed(w: World) -> None:
        seed_conflicts(DOWN)(w)
        no_tar_filter(w)

    off, on = run_both(tmp_path, seed, copy_proj(DOWN, op), skip_then_overwrite, fake_cls=fake_cls)
    assert_same(off, on)
    assert len(on.answers.conflicts_asked) == 2
    assert archives_moved(on.w) == []
    assert os.listdir(on.w.rtmp) == []


@pytest.mark.parametrize("op", ["copy", "move"])
def test_a_remote_without_tar_filter_falls_back_after_its_failed_unpack(
    tmp_path: Path, op: str, fake_cls: type[FakeBtty]
) -> None:
    """Upload: the local pack works and the archive goes up, then the remote
    unpack reports NOFILTER; the uploaded archive is removed and the batch
    goes per file with the answers already given."""

    def seed(w: World) -> None:
        seed_conflicts(UP)(w)
        no_tar_filter(w)

    off, on = run_both(tmp_path, seed, copy_proj(UP, op), skip_then_overwrite, fake_cls=fake_cls)
    assert_same(off, on)
    assert len(on.answers.conflicts_asked) == 2
    assert ops_of(on.w.fake) == ["u"]


@DIRECTIONS
def test_a_local_pack_or_unpack_without_support_falls_back(tmp_path: Path, way: Direction) -> None:
    method = "pack" if way is UP else "unpack"
    off, on = run_both(
        tmp_path,
        seed_conflicts(way),
        copy_proj(way),
        skip_then_overwrite,
        local_faults={method: fail(Unsupported)},
    )
    assert_same(off, on)
    assert len(on.answers.conflicts_asked) == 2


def test_answers_to_a_clash_are_replayed_after_a_fallback(tmp_path: Path) -> None:
    def seed(w: World) -> None:
        write(f"{w.local}/proj/x", b"file x")
        write(f"{w.local}/proj/y", b"y")
        write(f"{w.home}/proj/x/inside", b"a directory here")
        no_tar_filter(w)

    off, on = run_both(tmp_path, seed, copy_proj(UP), lambda: Answers(error=OnError.SKIP))
    assert_same(off, on)
    assert len(on.answers.errors_asked) == 1


@pytest.mark.parametrize("answer", [Conflict.OVERWRITE_ALL, Conflict.SKIP_ALL])
def test_an_all_answer_given_before_a_fallback_still_covers_the_batch(tmp_path: Path, answer: Conflict) -> None:
    def seed(w: World) -> None:
        seed_conflicts(UP)(w)
        no_tar_filter(w)

    off, on = run_both(tmp_path, seed, copy_proj(UP), lambda: Answers([answer], conflict=Conflict.CANCEL))
    assert_same(off, on)
    assert len(on.answers.conflicts_asked) == 1


@DIRECTIONS
@pytest.mark.parametrize("op", ["copy", "move"])
def test_a_scanned_total_over_the_limit_falls_back_before_any_question(
    tmp_path: Path, way: Direction, op: str
) -> None:
    def seed(w: World) -> None:
        src, dst = way.src_root(w), way.dst_root(w)
        for rel in ("proj/a", "proj/b", "proj/sub/c"):
            write(f"{src}/{rel}", b"x" * 100)
        write(f"{dst}/proj/a", b"old a")

    off, on = run_both(tmp_path, seed, copy_proj(way, op), lambda: Answers([Conflict.SKIP]), archive_max_bytes=299)
    assert_same(off, on)
    no_archive_moved(on)
    assert len(on.answers.conflicts_asked) == 1


@DIRECTIONS
def test_a_scanned_total_at_the_limit_still_uses_archive_mode(tmp_path: Path, way: Direction) -> None:
    """Compressible files totalling exactly the limit: the packed archive
    (a few hundred bytes) is far below it, so archive mode runs."""
    w = make_world(tmp_path / "w", archive_max_bytes=30_000)
    for rel in ("proj/a", "proj/b", "proj/sub/c"):
        write(f"{way.src_root(w)}/{rel}", b"x" * 10_000)
    report = copy_proj(way)(w, Answers(), True)
    assert report.ok
    assert len(archives_moved(w)) == 1


@DIRECTIONS
def test_a_packed_archive_over_the_limit_falls_back_with_the_answers_given(
    tmp_path: Path, way: Direction
) -> None:
    """Incompressible files exactly at the limit: the scan passes, the packed
    archive (headers and gzip framing on top) does not."""

    def seed(w: World) -> None:
        rng = random.Random(7)
        src, dst = way.src_root(w), way.dst_root(w)
        for rel, size in (("proj/a", 300), ("proj/b", 300), ("proj/c", 300), ("proj/sub/d", 100)):
            write(f"{src}/{rel}", rng.randbytes(size))
        write(f"{dst}/proj/a", b"old a")
        write(f"{dst}/proj/b", b"old b")

    off, on = run_both(tmp_path, seed, copy_proj(way), skip_then_overwrite, archive_max_bytes=1000)
    assert_same(off, on)
    assert len(on.answers.conflicts_asked) == 2
    assert archives_moved(on.w) == []
    assert os.listdir(on.w.rtmp) == []


# -- an archive script too long to send ---------------------------------------------------


def fits(build: Callable[..., object], *args: str) -> bool:
    try:
        build(*args)
    except FsError:
        return False
    return True


def test_a_merge_script_too_long_to_send_falls_back_like_the_other_archive_scripts(tmp_path: Path) -> None:
    """Plan step 12: "an archive script that cannot fit raises Unsupported
    (not OperationFailed), so the batch falls back to per-file transfer";
    btty_scripts: "so that the caller transfers per file".

    Two files go into a receiving directory whose path plus each name is 270
    bytes: the unpack script fits, the per-file path's stat script fits
    (up to 273), but the merge script (staging path plus destination) does
    not. archive=True must still end as archive=False does."""
    names = ["f" * 69 + "1", "f" * 69 + "2"]

    def into(w: World) -> str:
        pad = 200 - len(w.home) - 1
        assert pad > 0, "tmp_path is too long for this test"
        return f"{w.home}/{'d' * pad}"

    def seed(w: World) -> None:
        for n in names:
            write(f"{w.local}/{n}", n.encode())
        os.makedirs(into(w))
        token = ".twinfm-" + "0" * 16  # the staging name's length
        dest = f"{into(w)}/{names[0]}"
        assert len(dest) == 271
        assert fits(unpack_script, f"{into(w)}/{token}.tgz", f"{into(w)}/{token}")
        assert fits(stat_script, dest)
        assert not fits(merge_script, f"{into(w)}/{token}/{names[0]}", dest)

    def act(w: World, a: Answers, archive: bool) -> Report:
        paths = [f"{w.local}/{n}" for n in names]
        return engine.copy(w.lb, paths, w.btty, into(w), a, archive=archive)

    off, on = run_both(tmp_path, seed, act)
    assert off.report.ok  # per file, both files arrive
    assert summary(on.report, on.w.base) == summary(off.report, off.w.base)
    assert_same(off, on)
