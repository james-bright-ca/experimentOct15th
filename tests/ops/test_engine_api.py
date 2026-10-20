"""[C0] The Engine protocol's archive keyword and Progress.stage.

Specification: ``twinfm.ops.model`` (Engine.copy / Engine.move take
``archive`` keyword-only, default False; Progress gains ``stage``, default
None, after the existing fields) and the [C0] criterion "callers that pass
neither behave exactly as before".
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

from tests.ops.support import Answers, leftovers, make_world, ops_of, puts_of, tree, write
from twinfm.ops import engine
from twinfm.ops.model import Engine, Progress


@pytest.mark.parametrize("owner", [engine, Engine], ids=["engine-module", "Engine-protocol"])
@pytest.mark.parametrize("name", ["copy", "move"])
def test_archive_is_keyword_only_and_defaults_to_false(owner: object, name: str) -> None:
    params = inspect.signature(getattr(owner, name)).parameters
    archive = params["archive"]
    assert archive.kind is inspect.Parameter.KEYWORD_ONLY
    assert archive.default is False
    positional = [
        p.name
        for p in params.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert positional[-5:] == ["src", "paths", "dst", "target", "cb"]


@pytest.mark.parametrize("name", ["copy", "move"])
def test_archive_cannot_be_passed_positionally(tmp_path: Path, name: str) -> None:
    world = make_world(tmp_path / "w")
    write(world.lpath("f"), b"x")
    before = tree(world.base)
    with pytest.raises(TypeError):
        getattr(engine, name)(world.lb, [world.lpath("f")], world.btty, world.home, Answers(), True)
    assert tree(world.base) == before
    assert world.fake.calls == []


def test_progress_stage_defaults_to_none_and_follows_the_existing_fields() -> None:
    assert [f.name for f in dataclasses.fields(Progress)] == [
        "op",
        "current",
        "items_done",
        "items_total",
        "bytes_done",
        "bytes_total",
        "stage",
    ]
    p = Progress("copy", "/a", 1, 3)
    assert (p.op, p.current, p.items_done, p.items_total) == ("copy", "/a", 1, 3)
    assert p.bytes_done is None and p.bytes_total is None and p.stage is None
    full = Progress("move", "/b", 2, 4, 10, 20)
    assert (full.bytes_done, full.bytes_total, full.stage) == (10, 20, None)
    assert Progress("copy", "/a", 0, 1, stage="Packing").stage == "Packing"


def test_progress_stays_frozen() -> None:
    p = Progress("copy", "/a", 0, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.stage = "Packing"  # type: ignore[misc]


@pytest.mark.parametrize("name", ["copy", "move"])
def test_omitting_archive_transfers_per_file_even_when_both_sides_could_archive(
    tmp_path: Path, name: str
) -> None:
    """Both stores claim Cap.ARCHIVE, so only the default False keeps this
    folder upload on the per-file path: one --put per file, no archive
    program, no .twinfm- temporary, and per-item Progress without a stage."""
    world = make_world(tmp_path / "w")
    for rel in ("proj/a", "proj/sub/b", "proj/sub/c"):
        write(world.lpath(rel), rel.encode())
    answers = Answers()
    report = getattr(engine, name)(world.lb, [world.lpath("proj")], world.btty, world.home, answers)
    assert report.ok and report.done == [world.lpath("proj")]
    assert tree(world.rpath("proj")) == {
        "a": ("file", b"proj/a"),
        "sub": ("dir", None),
        "sub/b": ("file", b"proj/sub/b"),
        "sub/c": ("file", b"proj/sub/c"),
    }
    assert ops_of(world.fake) == []
    assert sorted(puts_of(world.fake)) == sorted(
        world.rpath(rel) for rel in ("proj/a", "proj/sub/b", "proj/sub/c")
    )
    assert leftovers(world.base) == []
    assert answers.progress_seen and all(p.stage is None for p in answers.progress_seen)
    assert (name == "move") == (not Path(world.lpath("proj")).exists())
