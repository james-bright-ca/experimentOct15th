"""[W3] A merge raising Unsupported in archive mode: the first one sends the
whole batch per file, a later one sends only that item and those after it.

Specification: ``Engine.copy`` in twinfm.ops.model -- "the whole batch falls
back to the per-file transfer, ending with the tree and Report that
archive=False gives, when pack, unpack or the first merge raises
Unsupported" and "a later merge that raises Unsupported leaves the items
before it merged (and done); that item and those after it then go per file,
replaying the answers already given"; ``Engine.move`` ("If the merge raised,
no source is deleted"); ``Callbacks.cancelled``; the "Falling back"
paragraph of twinfm.ops.archive; plan step 12 ("an archive script that
cannot fit raises Unsupported (not OperationFailed), so the batch falls back
to per-file transfer").

Most scenarios run twice, in two identical Worlds, once with archive=False
and once with archive=True, and must end the same: the same trees, the same
Report, the same questions, each asked once. The merge log of the receiving
backend then shows that archive mode really merged the items before the
failing one and stopped merging there, so the comparison is about the
partial continuation and not a whole-batch fallback.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.backend.fake_btty import FakeBtty
from tests.ops.support import Answers, Hook, World, fail, gets_of, make_world, ops_of, puts_of, tree, write
from tests.ops.test_archive_fallback import Outcome, assert_same, fits
from tests.ops.test_archive_transfer import DIRECTIONS, UP, Direction
from twinfm.backend.base import OperationFailed, Unsupported
from twinfm.backend.btty_scripts import merge_script
from twinfm.ops import engine
from twinfm.ops.model import Conflict, OnError, Report

# -- helpers -------------------------------------------------------------------------------


def pair(
    tmp_path: Path,
    seed: Callable[[World], None],
    act: Callable[[World, Answers, bool], Report],
    answers_for: Callable[[World], Answers],
    **world: Any,
) -> tuple[Outcome, Outcome]:
    """The scenario with archive=False, then archive=True, each in a fresh
    World of the same path lengths; ``answers_for(w)`` builds the Callbacks
    once ``w`` is seeded (a cancel condition may look at its trees)."""
    outcomes = []
    for archive in (False, True):
        w = make_world(tmp_path / f"archive-{int(archive)}", **world)
        seed(w)
        a = answers_for(w)
        outcomes.append(Outcome(w, act(w, a, archive), a))
    return outcomes[0], outcomes[1]


def merge_faults(way: Direction, hook: Hook) -> dict[str, Any]:
    """make_world keywords putting ``hook`` on the receiving backend's merge."""
    return {"btty_faults" if way is UP else "local_faults": {"merge": hook}}


def unsupported_at(*names: str) -> Hook:
    """A merge hook raising Unsupported when the staged item is one of ``names``."""
    return fail(Unsupported, when=lambda src, dst: os.path.basename(src) in names, message="cannot merge here")


def merges(w: World, way: Direction) -> list[str]:
    """The name of each item the receiving backend was asked to merge, in order."""
    return [os.path.basename(args[1]) for name, args in way.dst(w).log if name == "merge"]  # type: ignore[attr-defined]


def per_file_tops(w: World, way: Direction) -> set[str]:
    """The selected items (names in the remote home) that something went
    per file for: the --put destinations uploading, the --get sources
    downloading, leaving out the archive itself."""
    moved = puts_of(w.fake) if way is UP else gets_of(w.fake)
    return {os.path.relpath(p, w.home).split("/")[0] for p in moved if not p.endswith(".tgz")}


def selection(way: Direction, op: str, names: Sequence[str]) -> Callable[[World, Answers, bool], Report]:
    def act(w: World, a: Answers, archive: bool) -> Report:
        root = way.src_root(w)
        fn = getattr(engine, op)
        return fn(way.src(w), [f"{root}/{n}" for n in names], way.dst(w), way.dst_root(w), a, archive=archive)

    return act


def no_temporaries(w: World, systmp: str) -> None:
    assert os.listdir(w.rtmp) == []
    assert os.listdir(systmp) == []


OPS = pytest.mark.parametrize("op", ["copy", "move"])

# -- conflicts answered before, between and inside the deferred items --------------------------

ITEMS = ["p1", "f", "p2", "p3"]
"""Three directories and, between the first two, one selected file."""


def seed_conflicts(way: Direction) -> Callable[[World], None]:
    """Five conflicts, asked in this order: p1/x (in the item that merges),
    f (the selected file between), then p2's x and sub/z (inside the first
    item whose merge fails, in walk order) and p3/x (inside the item after
    it)."""

    def seed(w: World) -> None:
        src, dst = way.src_root(w), way.dst_root(w)
        for top in ("p1", "p2", "p3"):
            for rel in ("x", "sub/z"):
                write(f"{src}/{top}/{rel}", f"new {top}/{rel}".encode())
        write(f"{src}/f", b"new f")
        for rel in ("p1/x", "f", "p2/x", "p2/sub/z", "p3/x"):
            write(f"{dst}/{rel}", f"old {rel}".encode())

    return seed


O, OA, S, SA = Conflict.OVERWRITE, Conflict.OVERWRITE_ALL, Conflict.SKIP, Conflict.SKIP_ALL

ANSWER_CASES = [
    # (answers in order, whether the selected file f goes, questions asked)
    pytest.param([OA], True, 1, id="overwrite-all-in-the-merged-item"),
    pytest.param([SA], False, 1, id="skip-all-in-the-merged-item"),
    pytest.param([O, SA], False, 2, id="skip-all-on-the-file-between"),
    pytest.param([S, OA], True, 2, id="overwrite-all-on-the-file-between"),
    pytest.param([O, O, S, OA], True, 4, id="skip-then-overwrite-all-inside-the-deferred-item"),
    pytest.param([S, S, O, SA], False, 4, id="overwrite-then-skip-all-inside-the-deferred-item"),
    pytest.param([S, O, O, S, S], True, 5, id="each-answered-on-its-own"),
]


@DIRECTIONS
@OPS
@pytest.mark.parametrize(("queue", "f_goes", "asked"), ANSWER_CASES)
def test_a_later_merge_without_support_continues_per_file_with_the_answers_given(
    tmp_path: Path, way: Direction, op: str, queue: list[Conflict], f_goes: bool, asked: int, systmp: str
) -> None:
    """p1 (and f, when it goes) merge; p2's merge raises Unsupported; p2 and
    p3 then go per file. Every conflict is asked once, during the scan, and
    the per-file continuation replays those answers, *_ALL ones included."""
    off, on = pair(
        tmp_path,
        seed_conflicts(way),
        selection(way, op, ITEMS),
        lambda w: Answers(queue, conflict=Conflict.CANCEL),  # an extra question would cancel
        **merge_faults(way, unsupported_at("p2")),
    )
    assert not off.report.cancelled and len(off.answers.conflicts_asked) == asked
    assert merges(on.w, way) == (["p1", "f", "p2"] if f_goes else ["p1", "p2"])
    tops = per_file_tops(on.w, way)
    assert "p3" in tops and tops <= {"p2", "p3"}  # nothing merged goes again
    assert_same(off, on)
    no_temporaries(on.w, systmp)


@DIRECTIONS
@OPS
@pytest.mark.parametrize(
    ("fails", "merged", "per_file"),
    [
        pytest.param("p1", ["p1"], {"p1", "f", "p2", "p3"}, id="first-merge-whole-batch-per-file"),
        pytest.param("p3", ["p1", "f", "p2", "p3"], {"p3"}, id="last-merge-only-it-per-file"),
    ],
)
def test_which_merge_lacks_support_decides_what_goes_per_file(
    tmp_path: Path, way: Direction, op: str, fails: str, merged: list[str], per_file: set[str], systmp: str
) -> None:
    """With p1/x and f overwritten, then SKIP and OVERWRITE_ALL inside p2."""
    off, on = pair(
        tmp_path,
        seed_conflicts(way),
        selection(way, op, ITEMS),
        lambda w: Answers([O, O, S, OA], conflict=Conflict.CANCEL),
        **merge_faults(way, unsupported_at(fails)),
    )
    assert not off.report.cancelled
    assert merges(on.w, way) == merged
    assert per_file_tops(on.w, way) == per_file
    assert_same(off, on)
    no_temporaries(on.w, systmp)


# -- file/directory clashes answered before and inside the deferred items -----------------------


def seed_clashes(way: Direction) -> Callable[[World], None]:
    """Three clashes, asked in this order: p1/y (a directory where p1 has a
    file, in the item that merges), p2/y (the same, in the item whose merge
    fails) and p3/sub (a file where p3 has a directory, in the item after)."""

    def seed(w: World) -> None:
        src, dst = way.src_root(w), way.dst_root(w)
        for top in ("p1", "p2", "p3"):
            for rel in ("x", "y", "sub/z"):
                write(f"{src}/{top}/{rel}", f"new {top}/{rel}".encode())
        write(f"{dst}/p1/y/inside", b"a directory here")
        write(f"{dst}/p2/y/inside", b"a directory here")
        write(f"{dst}/p3/sub", b"a file here")

    return seed


@DIRECTIONS
@OPS
@pytest.mark.parametrize(
    ("errors", "asked"),
    [
        pytest.param([OnError.SKIP, OnError.SKIP, OnError.SKIP], 3, id="each-skipped"),
        pytest.param([OnError.SKIP, OnError.SKIP_ALL], 2, id="skip-all-inside-the-deferred-item"),
        pytest.param([OnError.SKIP_ALL], 1, id="skip-all-in-the-merged-item"),
    ],
)
def test_clash_answers_given_in_the_scan_replay_in_the_per_file_continuation(
    tmp_path: Path, way: Direction, op: str, errors: list[OnError], asked: int, systmp: str
) -> None:
    off, on = pair(
        tmp_path,
        seed_clashes(way),
        selection(way, op, ["p1", "p2", "p3"]),
        lambda w: Answers(errors=errors, error=OnError.ABORT),  # an extra question would abort
        **merge_faults(way, unsupported_at("p2")),
    )
    assert not off.report.cancelled and len(off.answers.errors_asked) == asked
    assert merges(on.w, way) == ["p1", "p2"]
    tops = per_file_tops(on.w, way)
    assert "p2" in tops and tops <= {"p2", "p3"}
    assert_same(off, on)
    no_temporaries(on.w, systmp)


# -- an earlier merge that failed ---------------------------------------------------------------


@DIRECTIONS
@OPS
def test_an_earlier_failed_merge_stays_failed_and_the_items_after_a_later_unsupported_one_go_per_file(
    tmp_path: Path, way: Direction, op: str, systmp: str
) -> None:
    """p1's merge fails (OperationFailed, answered SKIP): it stays failed
    and, for a move, keeps all of its sources. p2's merge then raises
    Unsupported, which is a later merge: p2 and p3 go per file and arrive."""
    reject = fail(OperationFailed, when=lambda src, dst: os.path.basename(src) == "p1")
    unsupported = unsupported_at("p2")

    def hook(*args: Any, **kwargs: Any) -> Any:
        return reject(*args, **kwargs) or unsupported(*args, **kwargs)

    w = make_world(tmp_path / "w", **merge_faults(way, hook))
    src, dst = way.src_root(w), way.dst_root(w)
    for top in ("p1", "p2", "p3"):
        for rel in ("x", "sub/z"):
            write(f"{src}/{top}/{rel}", f"new {top}/{rel}".encode())
    want = {top: tree(f"{src}/{top}") for top in ("p1", "p2", "p3")}
    answers = Answers(error=OnError.SKIP)
    report = selection(way, op, ["p1", "p2", "p3"])(w, answers, True)

    assert [(p, type(e)) for p, e in report.failed] == [(f"{src}/p1", OperationFailed)]
    assert [p for p, _ in answers.errors_asked] == [f"{src}/p1"]
    assert sorted(report.done) == [f"{src}/p2", f"{src}/p3"]
    assert report.skipped == [] and not report.cancelled
    assert merges(w, way) == ["p1", "p2"]
    assert per_file_tops(w, way) == {"p2", "p3"}
    assert not os.path.lexists(f"{dst}/p1")  # nothing of p1 merged
    assert tree(f"{dst}/p2") == want["p2"]
    assert tree(f"{dst}/p3") == want["p3"]
    if op == "move":
        assert sorted(os.listdir(src)) == ["p1"]
        assert tree(f"{src}/p1") == want["p1"]
    else:
        assert sorted(os.listdir(src)) == ["p1", "p2", "p3"]
    assert sorted(os.listdir(dst)) == ["p2", "p3"]
    no_temporaries(w, systmp)


# -- cancel during the per-file continuation --------------------------------------------------


def seed_three(way: Direction) -> Callable[[World], None]:
    def seed(w: World) -> None:
        src = way.src_root(w)
        for top in ("p1", "p2", "p3"):
            for rel in ("x", "y", "sub/z"):
                write(f"{src}/{top}/{rel}", f"new {top}/{rel}".encode())

    return seed


def arrived(way: Direction, w: World, top: str) -> list[str]:
    """The files of ``top`` (x, y, sub/z) that are at the destination."""
    dst = way.dst_root(w)
    return [rel for rel in ("x", "y", "sub/z") if os.path.isfile(f"{dst}/{top}/{rel}")]


@DIRECTIONS
@OPS
def test_cancel_before_the_deferred_item_starts_leaves_it_and_the_rest_untouched(
    tmp_path: Path, way: Direction, op: str, systmp: str
) -> None:
    """``cancelled()`` turns True once all of p1 has arrived: per file that is
    the poll before p2's first work item; in archive mode, the first poll
    of the per-file continuation after p2's merge raised Unsupported."""
    off, on = pair(
        tmp_path,
        seed_three(way),
        selection(way, op, ["p1", "p2", "p3"]),
        lambda w: Answers(cancel_when=lambda a: len(arrived(way, w, "p1")) == 3),
        **merge_faults(way, unsupported_at("p2")),
    )
    src, dst = way.src_root(on.w), way.dst_root(on.w)
    assert on.report.cancelled
    assert on.report.done == [f"{src}/p1"]
    assert on.report.failed == [] and on.report.skipped == []
    assert merges(on.w, way) == ["p1", "p2"]
    assert per_file_tops(on.w, way) == set()
    assert not os.path.lexists(f"{dst}/p2") and not os.path.lexists(f"{dst}/p3")
    if op == "move":
        assert sorted(os.listdir(src)) == ["p2", "p3"]
    assert_same(off, on)
    no_temporaries(on.w, systmp)


@DIRECTIONS
@OPS
def test_cancel_inside_the_deferred_item_stops_it_where_the_per_file_transfer_stops(
    tmp_path: Path, way: Direction, op: str, systmp: str
) -> None:
    """``cancelled()`` turns True once one file of p2 has arrived: p2 is not
    done, the file that went stays at the destination (and, for a move, is
    gone at the source), and nothing of p3 goes."""
    off, on = pair(
        tmp_path,
        seed_three(way),
        selection(way, op, ["p1", "p2", "p3"]),
        lambda w: Answers(cancel_when=lambda a: len(arrived(way, w, "p2")) > 0),
        **merge_faults(way, unsupported_at("p2")),
    )
    src, dst = way.src_root(on.w), way.dst_root(on.w)
    assert on.report.cancelled
    assert on.report.done == [f"{src}/p1"]
    assert on.report.failed == [] and on.report.skipped == []
    assert merges(on.w, way) == ["p1", "p2"]
    assert per_file_tops(on.w, way) == {"p2"}
    [went] = arrived(way, on.w, "p2")
    assert not os.path.lexists(f"{dst}/p3")
    if op == "move":
        stayed = [rel for rel in ("x", "y", "sub/z") if os.path.isfile(f"{src}/p2/{rel}")]
        assert sorted(stayed) == sorted({"x", "y", "sub/z"} - {went})
        assert tree(f"{src}/p3") == {
            "x": ("file", b"new p3/x"),
            "y": ("file", b"new p3/y"),
            "sub": ("dir", None),
            "sub/z": ("file", b"new p3/sub/z"),
        }
    assert_same(off, on)
    no_temporaries(on.w, systmp)


# -- a merge script too long to send, after one that fitted ---------------------------------------


@OPS
@pytest.mark.parametrize(
    ("queue", "asked", "sent"),
    [
        pytest.param([O, O, S], 3, {"f" * 70}, id="one-by-one"),
        pytest.param([OA], 1, {"f" * 70, "b"}, id="overwrite-all-on-the-first"),
    ],
)
def test_a_merge_script_too_long_after_one_that_fitted_sends_the_rest_per_file(
    tmp_path: Path,
    op: str,
    queue: list[Conflict],
    asked: int,
    sent: set[str],
    fake_cls: type[FakeBtty],
    systmp: str,
) -> None:
    """Upload of three files into a directory whose path is 200 bytes: "a"
    and "b" have merge scripts that fit, the 70-byte name's does not
    (btty_scripts raises Unsupported before sending it). "a" merges
    remotely; the long name and "b", after it, go per file; no second
    merge script is sent. archive=True must still end as archive=False."""
    long = "f" * 70
    names = ["a", long, "b"]
    token = ".twinfm-" + "0" * 16  # the staging name's length

    def into(w: World) -> str:
        pad = 200 - len(w.home) - 1
        assert pad > 0, "tmp_path is too long for this test"
        return f"{w.home}/{'d' * pad}"

    def seed(w: World) -> None:
        for n in names:
            write(f"{w.local}/{n}", f"new {n}".encode())
            write(f"{into(w)}/{n}", f"old {n}".encode())
        assert fits(merge_script, f"{into(w)}/{token}/a", f"{into(w)}/a")
        assert not fits(merge_script, f"{into(w)}/{token}/{long}", f"{into(w)}/{long}")

    def act(w: World, a: Answers, archive: bool) -> Report:
        fn = getattr(engine, op)
        return fn(w.lb, [f"{w.local}/{n}" for n in names], w.btty, into(w), a, archive=archive)

    off, on = pair(tmp_path, seed, act, lambda w: Answers(queue, conflict=Conflict.CANCEL), fake_cls=fake_cls)
    assert not off.report.cancelled and len(off.answers.conflicts_asked) == asked
    assert ops_of(on.w.fake) == ["u", "m"]  # one unpack, then only "a"'s merge
    assert {os.path.basename(p) for p in puts_of(on.w.fake) if not p.endswith(".tgz")} == sent
    assert f"{on.w.local}/{long}" in on.report.done
    assert_same(off, on)
    no_temporaries(on.w, systmp)
