"""btty_scripts: the remote script builders, and the parsers for what they print.

The parsers are fed hand-written records in the format the module docstring
specifies (@J JSON records with base64 names, @X with GNU base64's wrapped
paths, @K), including non-UTF-8 names that this Mac's filesystem cannot hold.
The scripts themselves are run with /bin/sh (or bash) through
``fake_btty.run_script`` in a temporary directory, never through btty.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import stat
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.backend.contract import DIR, FILE, LINK, Node, make_archive
from tests.backend.fake_btty import run_script, script_env, script_names, script_paths
from twinfm.backend.base import (
    AlreadyExists,
    DirectoryNotEmpty,
    FsError,
    IsADirectory,
    NotADirectory,
    NotFound,
    OperationFailed,
    PermissionDenied,
    Unsupported,
)
from twinfm.backend.btty_scripts import (
    COMMAND_BUDGET,
    EntryRecord,
    copy_script,
    delete_scripts,
    error_for,
    list_script,
    merge_script,
    mkdir_script,
    pack_list_script,
    pack_script,
    parse_output,
    rename_script,
    stat_script,
    to_entry,
    unpack_script,
    walk_script,
)

HEAD = "unalias -a 2>/dev/null"
WORKSPACE = "/mnt/home0/wst/workspaces/jake.blow.clearwateranalytics.com"

HOSTILE_PATHS = [
    f"{WORKSPACE}/with space",
    f"{WORKSPACE}/it's",
    f"{WORKSPACE}/new\nline",
    f"{WORKSPACE}/-leading-dash",
    f"{WORKSPACE}/star*?[ab]",
    f"{WORKSPACE}/$(touch pwned) `id` \"q\" ;|&",
    f"{WORKSPACE}/ünïcødé-日本語",
]

needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="permission bits do not bind root"
)


def b64(raw: bytes | str) -> str:
    return base64.b64encode(raw.encode() if isinstance(raw, str) else raw).decode()


def record(**fields: object) -> str:
    """An @J line; the defaults describe a regular file named ``f``."""
    data: dict[str, object] = {
        "name": b64("f"),
        "kind": "file",
        "size": 1,
        "mtime": 1.0,
        "mode": 0o644,
        "link_target": None,
        "target_kind": None,
    }
    data.update(fields)
    return "@J " + json.dumps(data)


def one_entry(line: str) -> EntryRecord:
    output = parse_output(line + "\n@K")
    assert output.noise == [], output.noise
    [entry] = output.entries
    return entry


Builder = Callable[[str], str]
ALL_BUILDERS: dict[str, Builder] = {
    "list": list_script,
    "walk": walk_script,
    "stat": stat_script,
    "mkdir": mkdir_script,
    "delete": lambda p: delete_scripts([p])[0][1],
    "delete-recursive": lambda p: delete_scripts([p], recursive=True)[0][1],
    "rename": lambda p: rename_script(p, p + "-2"),
    "copy": lambda p: copy_script(p, p + "-2"),
}


# -- builders ------------------------------------------------------------------------------


@pytest.mark.parametrize("builder", ALL_BUILDERS.values(), ids=ALL_BUILDERS.keys())
def test_every_script_drops_aliases_first_and_never_calls_exit(builder: Builder) -> None:
    # btty evals the script in its own interactive bash, where rm/mv/cp may be
    # aliased to prompt, and where `exit` would kill btty's shell.
    script = builder(f"{WORKSPACE}/project/file.txt")
    assert script.split("\n", 1)[0] == HEAD
    assert re.search(r"\bexit\b", script) is None


@pytest.mark.parametrize("builder", ALL_BUILDERS.values(), ids=ALL_BUILDERS.keys())
def test_every_script_fits_the_budget_for_a_realistic_path(builder: Builder) -> None:
    script = builder(f"{WORKSPACE}/project/{'n' * 100}.txt")
    assert len(script.encode("utf-8")) <= COMMAND_BUDGET


@pytest.mark.parametrize("builder", ALL_BUILDERS.values(), ids=ALL_BUILDERS.keys())
@pytest.mark.parametrize("path", ["relative/path", "", "/a\0b"], ids=["relative", "empty", "nul"])
def test_a_relative_or_nul_path_is_refused(builder: Builder, path: str) -> None:
    with pytest.raises(OperationFailed):
        builder(path)


@pytest.mark.parametrize("builder", ALL_BUILDERS.values(), ids=ALL_BUILDERS.keys())
def test_a_path_too_long_for_one_command_raises_operation_failed(builder: Builder) -> None:
    long = "/" + "/".join(["d" * 100] * 12)
    with pytest.raises(OperationFailed) as err:
        builder(long)
    assert "too long" in err.value.message


@pytest.mark.parametrize("builder", [list_script, walk_script, stat_script], ids=["l", "w", "s"])
def test_a_listing_path_may_use_exactly_the_budget_and_not_one_byte_more(builder: Builder) -> None:
    spare = COMMAND_BUDGET - len(builder("/").encode())
    fits = "/" + "a" * spare
    assert len(builder(fits).encode()) == COMMAND_BUDGET
    with pytest.raises(OperationFailed):
        builder(fits + "a")


@pytest.mark.parametrize("path", HOSTILE_PATHS, ids=repr)
def test_listing_scripts_pass_one_fixed_program_and_the_path_as_separate_words(path: str) -> None:
    programs = set()
    for op, builder in (("l", list_script), ("w", walk_script), ("s", stat_script)):
        words = shlex.split(builder(path).split("\n", 1)[1])
        assert len(words) == 6
        assert words[:3] == ["python3", "-I", "-c"]
        assert words[4:] == [op, path]
        assert path not in words[3]
        programs.add(words[3])
    assert len(programs) == 1
    assert programs == {shlex.split(list_script("/x").split("\n", 1)[1])[3]}


@pytest.mark.parametrize("path", HOSTILE_PATHS, ids=repr)
def test_every_mutation_script_names_exactly_its_paths(path: str) -> None:
    other = f"{WORKSPACE}/other 'one'"
    assert script_paths(mkdir_script(path)) == [path]
    assert script_paths(rename_script(path, other)) == [path, other]
    assert script_paths(copy_script(other, path, overwrite=True)) == [other, path]
    [(chunk, script)] = delete_scripts([path, other], recursive=True)
    assert chunk == [path, other]
    assert script_paths(script) == [path, other]


def test_a_path_is_data_to_the_shell_not_code(tmp_path: Path) -> None:
    # Run in root, so an injected `touch pwned` would leave root/pwned behind.
    root = tmp_path.resolve()
    name = "$(touch pwned) `touch pwned` ;touch pwned; '\"\n-x *"
    for script in (mkdir_script(f"{root}/{name}"), rename_script(f"{root}/{name}", f"{root}/{name}2")):
        status, output = run_script(script, cwd=str(root))
        assert status == 0 and "@K" in output, output
    assert sorted(os.listdir(root)) == [f"{name}2"]


class TestDeleteScripts:
    PATHS = [f"{WORKSPACE}/build/report {i:03d} - it's *final*.txt" for i in range(200)]

    @pytest.mark.parametrize("recursive", [False, True])
    def test_chunks_cover_every_path_in_order_within_the_budget(self, recursive: bool) -> None:
        chunks = delete_scripts(self.PATHS, recursive)
        assert 1 < len(chunks) < len(self.PATHS)
        assert [p for chunk, _ in chunks for p in chunk] == self.PATHS
        for chunk, script in chunks:
            assert chunk
            assert script_paths(script) == chunk
            assert len(script.encode("utf-8")) <= COMMAND_BUDGET

    def test_each_chunk_holds_as_many_paths_as_fit(self) -> None:
        chunks = delete_scripts(self.PATHS)
        for (_, script), (following, _) in zip(chunks, chunks[1:], strict=False):
            # One more path would have pushed the script over the budget.
            assert len(script.encode()) + 1 + len(shlex.quote(following[0]).encode()) > COMMAND_BUDGET

    def test_no_paths_make_no_scripts(self) -> None:
        assert delete_scripts([]) == []

    def test_one_path_too_long_raises_before_any_script_is_made(self) -> None:
        with pytest.raises(OperationFailed) as err:
            delete_scripts([f"{WORKSPACE}/ok", "/" + "x" * COMMAND_BUDGET, f"{WORKSPACE}/never"])
        assert err.value.path == "/" + "x" * COMMAND_BUDGET


# -- parsers: @J records ---------------------------------------------------------------------


def test_a_file_record_from_a_walk_becomes_an_entry_below_the_base() -> None:
    entry = to_entry(
        one_entry(record(name=b64("sub/dir/f.txt"), size=12, mtime=1700000000.5, mode=0o640)),
        "/base",
    )
    assert entry.name == "f.txt"
    assert entry.path == "/base/sub/dir/f.txt"
    assert (entry.kind, entry.size, entry.mtime, entry.mode) == ("file", 12, 1700000000.5, 0o640)
    assert (entry.link_target, entry.target_kind) == (None, None)


def test_a_symlink_record_keeps_its_link_text_and_target_kind() -> None:
    entry = to_entry(
        one_entry(
            record(
                name=b64("to-dir"),
                kind="symlink",
                size=7,
                mtime=1700000001,
                mode=0o777,
                link_target=b64("../té d"),
                target_kind="dir",
            )
        ),
        "/base",
    )
    assert (entry.kind, entry.size, entry.link_target, entry.target_kind) == (
        "symlink",
        7,
        "../té d",
        "dir",
    )
    assert entry.mtime == 1700000001.0 and isinstance(entry.mtime, float)


@pytest.mark.parametrize("target_kind", [None, "symlink", "nonsense", 3])
def test_a_symlink_s_target_kind_is_never_symlink_and_unknown_means_none(target_kind: object) -> None:
    entry = one_entry(record(kind="symlink", link_target=b64("x"), target_kind=target_kind))
    assert entry.target_kind is None


@pytest.mark.parametrize("kind", ["dir", "other"])
def test_a_dir_or_other_record_has_no_size_link_or_target(kind: str) -> None:
    entry = one_entry(record(kind=kind, size=4096, link_target=b64("x"), target_kind="file"))
    assert (entry.kind, entry.size, entry.link_target, entry.target_kind) == (kind, None, None, None)


def test_mode_keeps_only_the_permission_bits() -> None:
    assert one_entry(record(mode=stat.S_IFREG | 0o4755)).mode == 0o4755


def test_a_non_utf8_name_round_trips_exactly() -> None:
    raw = b"bad\xff\xfe name\nwith newline"
    entry = to_entry(one_entry(record(name=b64(raw))), "/base")
    assert entry.name.encode("utf-8", "surrogateescape") == raw
    assert entry.path.encode("utf-8", "surrogateescape") == b"/base/" + raw


def test_entries_listed_at_the_root_have_single_slash_paths() -> None:
    entry = to_entry(one_entry(record(name=b64("etc"), kind="dir")), "/")
    assert (entry.name, entry.path) == ("etc", "/etc")


@pytest.mark.parametrize(
    ("field", "value"),
    [("size", "12"), ("size", True), ("mtime", "yesterday"), ("mode", 420.5), ("mode", False)],
)
def test_a_wrongly_typed_optional_field_becomes_none(field: str, value: object) -> None:
    assert getattr(one_entry(record(**{field: value})), field) is None


@pytest.mark.parametrize(
    "line",
    [
        "@J",
        "@J not json",
        "@J [1, 2]",
        '@J {"kind": "file"}',
        '@J {"name": "Zg==", "kind": "weird"}',
        '@J {"name": "Zg==", "kind": ["file"]}',
        '@J {"name": 5, "kind": "file"}',
        '@J {"name": "!!not base64!!", "kind": "file"}',
    ],
)
def test_a_malformed_record_is_noise_not_an_entry(line: str) -> None:
    output = parse_output(f"{line}\n{record()}\n@K")
    assert [e.rel for e in output.entries] == ["f"]
    assert output.noise == [line.strip()]
    assert output.done


def test_output_with_pty_line_ends_and_btty_s_info_line_parses() -> None:
    text = f"btty: 6a35ed517a03 on brisk-host-0\r\n{record()}\r\n\r\nsome tool said this\r\n@K\r\n"
    output = parse_output(text)
    assert [e.rel for e in output.entries] == ["f"]
    assert output.noise == ["btty: 6a35ed517a03 on brisk-host-0", "some tool said this"]
    assert output.done and output.errors == []
    assert error_for(output, "/base", "btty", 0) is None


# -- parsers: @X and @K -------------------------------------------------------------------------


def test_an_error_path_wrapped_by_gnu_base64_is_joined_back() -> None:
    path = f"{WORKSPACE}/a rather long folder name/and a file name that wraps.txt"
    encoded = b64(path)
    assert len(encoded) > 76
    wrapped = " ".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))
    assert parse_output(f"@X ENOENT {wrapped}").errors == [("ENOENT", path)]


def test_an_error_without_a_path_is_about_the_operation_s_own_path() -> None:
    output = parse_output("@X EEXIST")
    assert output.errors == [("EEXIST", None)]
    err = error_for(output, "/base/x", "btty", 0)
    assert isinstance(err, AlreadyExists) and err.path == "/base/x" and err.backend_id == "btty"


def test_an_error_path_that_is_not_base64_still_reports_the_error() -> None:
    assert parse_output("@X EACCES ***").errors == [("EACCES", None)]


@pytest.mark.parametrize("line", ["@X", "@K now", "@Kay", "@j " + json.dumps({"name": "Zg==", "kind": "file"})])
def test_record_look_alikes_are_noise(line: str) -> None:
    output = parse_output(line)
    assert (output.entries, output.errors, output.done) == ([], [], False)
    assert output.noise == [line]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ENOENT", NotFound),
        ("EEXIST", AlreadyExists),
        ("ENOTDIR", NotADirectory),
        ("EISDIR", IsADirectory),
        ("ENOTEMPTY", DirectoryNotEmpty),
        ("EACCES", PermissionDenied),
        ("EPERM", PermissionDenied),
        ("EINVAL", OperationFailed),
        ("EIO", OperationFailed),
        ("ELOOP", OperationFailed),
        ("EXDEV", OperationFailed),  # merge across filesystems (base.py: OperationFailed)
    ],
)
def test_each_error_code_maps_to_its_fs_error_about_the_reported_path(
    code: str, expected: type[FsError]
) -> None:
    output = parse_output(f"[Errno 99] the tool's own words\n@X {code} {b64('/base/sub')}")
    err = error_for(output, "/base", "btty", 0)
    assert type(err) is expected
    assert err.path == "/base/sub"
    assert err.backend_id == "btty"


def test_einval_means_a_directory_into_itself() -> None:
    err = error_for(parse_output("@X EINVAL"), "/a", "btty", 0)
    assert isinstance(err, OperationFailed) and "into itself" in err.message


def test_an_unclassified_error_carries_the_noise_as_its_reason() -> None:
    err = error_for(parse_output("cp: /a/x: Input/output error\n@X EIO"), "/a", "btty", 1)
    assert isinstance(err, OperationFailed)
    assert "Input/output error" in err.message


def test_the_first_error_decides_and_beats_a_k() -> None:
    output = parse_output(f"@X ENOTEMPTY {b64('/a/1')}\n@X ENOENT {b64('/a/2')}\n@K")
    err = error_for(output, "/a", "btty", 0)
    assert type(err) is DirectoryNotEmpty and err.path == "/a/1"


def test_output_without_k_or_x_means_the_script_did_not_finish() -> None:
    err = error_for(parse_output("bash: line 1: python3: command not found"), "/a", "btty", 127)
    assert isinstance(err, OperationFailed)
    assert "did not finish" in err.message
    assert "127" in err.message
    assert "python3: command not found" in err.message


# -- the listing program, run for real with /bin/sh ------------------------------------------


def listing(script: str) -> tuple[list[EntryRecord], list[tuple[str, str | None]], bool]:
    status, output = run_script(script)
    parsed = parse_output(output)
    assert status == 0, output
    return parsed.entries, parsed.errors, parsed.done


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A directory holding one of each kind."""
    root = tmp_path.resolve() / "tree"
    root.mkdir()
    (root / "f").write_bytes(b"12345")
    os.chmod(root / "f", 0o640)
    (root / "d").mkdir()
    (root / "d" / "inner").write_bytes(b"i")
    os.symlink("f", root / "to-f")
    os.symlink(str(root / "d"), root / "to-d")
    os.symlink("missing", root / "dangling")
    os.symlink("loop-b", root / "loop-a")
    os.symlink("loop-a", root / "loop-b")
    os.mkfifo(root / "fifo")
    os.symlink("fifo", root / "to-fifo")
    return root


def test_the_program_lists_each_kind_with_lstat_semantics(tree: Path) -> None:
    entries, errors, done = listing(list_script(str(tree)))
    assert done and errors == []
    got = {e.rel: e for e in entries}
    assert set(got) == {"f", "d", "to-f", "to-d", "dangling", "loop-a", "loop-b", "fifo", "to-fifo"}
    described = {
        rel: (e.kind, e.link_target, e.target_kind) for rel, e in got.items()
    }
    assert described == {
        "f": ("file", None, None),
        "d": ("dir", None, None),
        "to-f": ("symlink", "f", "file"),
        "to-d": ("symlink", str(tree / "d"), "dir"),
        "dangling": ("symlink", "missing", None),
        "loop-a": ("symlink", "loop-b", None),
        "loop-b": ("symlink", "loop-a", None),
        "fifo": ("other", None, None),
        "to-fifo": ("symlink", "fifo", "other"),
    }
    st = os.lstat(tree / "f")
    assert (got["f"].size, got["f"].mode, got["f"].mtime) == (5, 0o640, st.st_mtime)
    assert got["to-f"].size == len("f")
    assert got["d"].size is None and got["fifo"].size is None
    assert got["d"].mode == stat.S_IMODE(os.lstat(tree / "d").st_mode)


def test_the_program_lists_hostile_names_exactly(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    names = ["with space", "it's", "new\nline", "-leading-dash", "star*", "ünïcødé-日本語", "\\back", " padded "]
    for name in names:
        (root / name).write_bytes(b"x")
    entries, _, done = listing(list_script(str(root)))
    assert done
    assert sorted(e.rel for e in entries) == sorted(names)


def test_a_listing_follows_a_symlink_to_a_directory(tree: Path) -> None:
    entries, _, done = listing(list_script(str(tree / "to-d")))
    assert done and [(e.rel, e.kind) for e in entries] == [("inner", "file")]


def test_a_walk_puts_parents_first_sorts_siblings_and_never_descends_symlinks(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    for rel in ["b", "b/c", "a-dir"]:
        (root / rel).mkdir()
    for rel in ["b/c/f", "b/e", "a", "a-dir/x"]:
        (root / rel).write_bytes(b"1")
    os.symlink("b", root / "link-to-b")
    entries, errors, done = listing(walk_script(str(root)))
    assert done and errors == []
    assert [e.rel for e in entries] == ["a", "a-dir", "a-dir/x", "b", "b/c", "b/c/f", "b/e", "link-to-b"]
    entries, _, _ = listing(walk_script(str(root / "link-to-b")))
    assert [e.rel for e in entries] == ["c", "c/f", "e"]


def test_a_stat_describes_the_path_itself(tree: Path) -> None:
    [entry], _, done = listing(stat_script(str(tree / "to-d")))
    assert done
    assert (entry.rel, entry.kind, entry.link_target, entry.target_kind) == (
        "",
        "symlink",
        str(tree / "d"),
        "dir",
    )
    [entry], _, _ = listing(stat_script(str(tree / "dangling")))
    assert (entry.kind, entry.target_kind) == ("symlink", None)


@pytest.mark.parametrize(
    ("build", "rel", "code"),
    [
        (stat_script, "missing", "ENOENT"),
        (stat_script, "f/below-a-file", "ENOENT"),
        (list_script, "missing", "ENOENT"),
        (list_script, "f", "ENOTDIR"),
        (walk_script, "missing", "ENOENT"),
        (list_script, "loop-a", "ELOOP"),
    ],
)
def test_the_program_reports_failures_about_the_path(
    tree: Path, build: Builder, rel: str, code: str
) -> None:
    path = str(tree / rel)
    entries, errors, done = listing(build(path))
    assert (entries, done) == ([], False)
    assert errors == [(code, path)]


@needs_non_root
def test_an_unreadable_directory_ends_a_walk_with_eacces_about_it(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "a").write_bytes(b"1")
    (root / "locked").mkdir()
    (root / "locked" / "x").write_bytes(b"1")
    os.chmod(root / "locked", 0)
    try:
        entries, errors, done = listing(walk_script(str(root)))
        assert not done
        assert [e.rel for e in entries] == ["a", "locked"]
        assert errors == [("EACCES", str(root / "locked"))]
        err = error_for(parse_output(run_script(list_script(str(root / "locked")))[1]), "/x", "btty", 0)
        assert isinstance(err, PermissionDenied) and err.path == str(root / "locked")
    finally:
        os.chmod(root / "locked", 0o755)


# -- the mutation scripts, run for real ----------------------------------------------------


_HOSTILE_SHELL = """\
shopt -s expand_aliases
alias rm='echo ALIASED rm'
alias mv='echo ALIASED mv'
alias cp='echo ALIASED cp'
alias mkdir='echo ALIASED mkdir'
alias rmdir='echo ALIASED rmdir'
alias ls='echo ALIASED ls'
alias python3='echo ALIASED python3'
cd() { echo HIJACKED cd; }
"""


def run_in_hostile_bash(script: str) -> tuple[int, str]:
    """Run ``script`` as btty does, with ``eval`` in a bash whose rm/mv/cp/...
    are aliases and whose cd is a function."""
    done = subprocess.run(
        ["/bin/bash", "-c", _HOSTILE_SHELL + 'eval "$1"', "bash", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=script_env(),
        cwd="/",
        check=False,
    )
    return done.returncode, done.stdout.decode("utf-8", "replace")


def test_the_hostile_shell_really_aliases_the_tools() -> None:
    _, output = run_in_hostile_bash("rm -rf /nonexistent-t19; cd /; python3 -V")
    assert "ALIASED rm" in output and "HIJACKED cd" in output
    assert "ALIASED python3" in output


def test_mutation_scripts_ignore_aliases_and_shell_functions(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "src").mkdir()
    (root / "src" / "f").write_bytes(b"1")
    (root / "gone").write_bytes(b"x")
    (root / "full").mkdir()
    (root / "full" / "x").write_bytes(b"x")
    steps = [
        (mkdir_script(f"{root}/new"), "@K"),
        (copy_script(f"{root}/src", f"{root}/copy"), "@K"),
        (rename_script(f"{root}/copy", f"{root}/moved"), "@K"),
        (rename_script(f"{root}/src", f"{root}/src/inside"), "@X EINVAL"),
        (delete_scripts([f"{root}/gone"])[0][1], "@K"),
        (delete_scripts([f"{root}/full"])[0][1], "@X ENOTEMPTY"),
        (delete_scripts([f"{root}/full"], recursive=True)[0][1], "@K"),
    ]
    for script, expected in steps:
        status, output = run_in_hostile_bash(script)
        assert "ALIASED" not in output and "HIJACKED" not in output, output
        assert expected in output, (script, output)
    assert sorted(os.listdir(root)) == ["moved", "new", "src"]
    assert (root / "moved" / "f").read_bytes() == b"1"


def test_an_error_path_survives_gnu_base64_wrapping_in_a_real_script(tmp_path: Path) -> None:
    # GNU base64 wraps its output at 76 columns; BSD base64 (macOS) does not.
    bin_dir = tmp_path / "gnu-bin"
    bin_dir.mkdir()
    (bin_dir / "base64").write_text(
        "#!/bin/sh\nout=$(command -p base64 \"$@\") || exit\nprintf '%s\\n' \"$out\" | fold -w 76\n"
    )
    os.chmod(bin_dir / "base64", 0o755)
    missing = f"{tmp_path.resolve()}/a rather long folder name/that does not exist/file name.txt"
    script = rename_script(missing, missing + " 2")
    status, output = run_script(f"PATH={shlex.quote(str(bin_dir))}:$PATH\n{script}")
    assert any(len(line) == 76 for line in output.split()), output
    assert parse_output(output).errors == [("ENOENT", missing)]


@needs_non_root
def test_a_failed_copy_leaves_no_temporary_sibling(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "src").mkdir()
    (root / "src" / "readable").write_bytes(b"1")
    (root / "src" / "secret").write_bytes(b"2")
    os.chmod(root / "src" / "secret", 0)
    try:
        _, output = run_script(copy_script(f"{root}/src", f"{root}/copy"))
        err = error_for(parse_output(output), f"{root}/src", "btty", 0)
        assert isinstance(err, OperationFailed)
        assert sorted(os.listdir(root)) == ["src"]
    finally:
        os.chmod(root / "src" / "secret", 0o644)


# -- the archive scripts: builders -----------------------------------------------------------

ArchiveBuilder = Callable[[str], "str | None"]
ARCHIVE_BUILDERS: dict[str, ArchiveBuilder] = {
    "pack": lambda p: pack_script(p, ["one", "two words"], p + ".tgz"),
    "pack-from-file": lambda p: pack_list_script(p, p + ".tgz.names", p + ".tgz"),
    "unpack": lambda p: unpack_script(p + ".tgz", p + "-out"),
    "merge": lambda p: merge_script(p, p + "-merged"),
}


def words_of(script: str | None) -> list[str]:
    """The words of a python3 script's command line, after the alias line."""
    assert script is not None
    head, body = script.split("\n", 1)
    assert head == HEAD
    return shlex.split(body)


@pytest.mark.parametrize("builder", ARCHIVE_BUILDERS.values(), ids=ARCHIVE_BUILDERS.keys())
def test_every_archive_script_drops_aliases_first_and_never_calls_exit(builder: ArchiveBuilder) -> None:
    script = builder(f"{WORKSPACE}/project/folder")
    assert script is not None
    assert script.split("\n", 1)[0] == HEAD
    assert re.search(r"\bexit\b", script) is None


_FOLDER = f"{WORKSPACE}/project/{'n' * 100}"
_STAGING = f"{_FOLDER}/.twinfm-0123456789abcdef"
_IN_TMP = "/tmp/.twinfm-0123456789abcdef.tgz"
REALISTIC_ARCHIVE_SCRIPTS: dict[str, Callable[[], str | None]] = {
    # Downloading: the selection's folder is packed into remote_tmp.
    "pack": lambda: pack_script(_FOLDER, ["src", "README.md", "build output"], _IN_TMP),
    "pack-from-file": lambda: pack_list_script(_FOLDER, _IN_TMP + ".names", _IN_TMP),
    # Uploading: the archive and the staging directory sit in the target folder.
    "unpack": lambda: unpack_script(_STAGING + ".tgz", _STAGING),
    "merge": lambda: merge_script(_STAGING + "/src", f"{_FOLDER}/src"),
}


@pytest.mark.parametrize(
    "build", REALISTIC_ARCHIVE_SCRIPTS.values(), ids=REALISTIC_ARCHIVE_SCRIPTS.keys()
)
def test_every_archive_script_fits_the_budget_for_the_paths_archive_mode_uses(
    build: Callable[[], str | None],
) -> None:
    # A 100-character folder name in the user's workspace, and the staging
    # and archive names of archive mode.
    script = build()
    assert script is not None
    assert len(script.encode("utf-8")) <= COMMAND_BUDGET


@pytest.mark.parametrize("builder", ARCHIVE_BUILDERS.values(), ids=ARCHIVE_BUILDERS.keys())
def test_every_archive_script_leaves_room_for_about_500_bytes_of_paths(builder: ArchiveBuilder) -> None:
    # The module docstring: "the archive programs leave about 500 bytes for
    # their quoted paths". Each builder here names up to three paths derived
    # from one, so 140 bytes each stays within that.
    script = builder("/" + "p" * 139)
    assert script is not None
    assert len(script.encode("utf-8")) <= COMMAND_BUDGET


@pytest.mark.parametrize("builder", ARCHIVE_BUILDERS.values(), ids=ARCHIVE_BUILDERS.keys())
@pytest.mark.parametrize("path", ["relative/path", "", "/a\0b"], ids=["relative", "empty", "nul"])
def test_an_archive_script_refuses_a_relative_or_nul_path(builder: ArchiveBuilder, path: str) -> None:
    with pytest.raises(OperationFailed):
        builder(path)


@pytest.mark.parametrize(
    "builder",
    [ARCHIVE_BUILDERS["pack-from-file"], ARCHIVE_BUILDERS["unpack"], ARCHIVE_BUILDERS["merge"]],
    ids=["pack-from-file", "unpack", "merge"],
)
def test_an_archive_script_too_long_for_one_command_raises_unsupported(builder: ArchiveBuilder) -> None:
    # Unsupported, not OperationFailed: the caller then transfers per file.
    long = "/" + "/".join(["d" * 100] * 12)
    with pytest.raises(Unsupported) as err:
        builder(long)
    assert "too long" in err.value.message


def test_an_unpack_script_may_use_exactly_the_budget_and_not_one_byte_more() -> None:
    spare = COMMAND_BUDGET - len(unpack_script("/a", "/b").encode())
    fits = "/b" + "b" * spare
    assert len(unpack_script("/a", fits).encode()) == COMMAND_BUDGET
    with pytest.raises(Unsupported):
        unpack_script("/a", fits + "b")


def test_pack_names_go_inline_up_to_the_budget_and_otherwise_pack_script_returns_none() -> None:
    parent, archive = f"{WORKSPACE}/src", f"{WORKSPACE}/a.tgz"
    base = pack_script(parent, ["a"], archive)
    assert base is not None
    name = "a" * (1 + COMMAND_BUDGET - len(base.encode()))
    exact = pack_script(parent, [name], archive)
    assert exact is not None and len(exact.encode()) == COMMAND_BUDGET
    assert pack_script(parent, [name + "a"], archive) is None
    many = [f"report {i:03d} - it's *final*.txt" for i in range(200)]
    assert pack_script(parent, many, archive) is None
    listed = pack_list_script(parent, archive + ".names", archive)
    assert len(listed.encode()) <= COMMAND_BUDGET


@pytest.mark.parametrize("path", HOSTILE_PATHS, ids=repr)
def test_archive_scripts_pass_one_fixed_program_and_each_path_and_name_as_its_own_word(path: str) -> None:
    other = f"{WORKSPACE}/other 'one'"
    names = ["it's", "new\nline", "-dash", "$(x) *", " padded "]
    pack = pack_script(path, names, other)
    words = {
        "p": words_of(pack),
        "f": words_of(pack_list_script(path, other + ".names", other)),
        "u": words_of(unpack_script(other, path)),
        "m": words_of(merge_script(path, other)),
    }
    assert words["p"][4:] == ["p", other, path, *names]
    assert words["f"][4:] == ["f", other, path, other + ".names"]
    assert words["u"][4:] == ["u", other, path]
    assert words["m"][4:] == ["m", path, other]
    for op, these in words.items():
        assert these[:3] == ["python3", "-I", "-c"], op
        assert path not in these[3] and other not in these[3], op
    assert words["p"][3] == words["f"][3], "pack has one program, whatever the op"
    listing = words_of(list_script("/x"))[3]
    assert len({words["p"][3], words["u"][3], words["m"][3], listing}) == 4
    assert pack is not None
    assert script_paths(pack) == [other, path] and script_names(pack) == names


def test_nofilter_maps_to_unsupported_about_the_operation_s_path() -> None:
    # What the programs print without tarfile.tar_filter: the code and an empty path.
    output = parse_output("@X NOFILTER \n")
    assert output.errors == [("NOFILTER", None)]
    err = error_for(output, "/base/a.tgz", "btty", 0)
    assert type(err) is Unsupported
    assert err.path == "/base/a.tgz" and err.backend_id == "btty"


# -- the archive programs, run for real with /bin/sh --------------------------------------------


def outcome(script: str | None, *, tar_filter: bool = True) -> tuple[list[tuple[str, str | None]], bool]:
    """(the @X records, whether @K was printed) of running ``script``; it must exit 0."""
    assert script is not None
    status, output = run_script(script, tar_filter=tar_filter)
    assert status == 0, output
    parsed = parse_output(output)
    return parsed.errors, parsed.done


def files_below(root: Path) -> dict[str, Node]:
    """Every path below ``root`` (lstat) as contract nodes."""
    result: dict[str, Node] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            rel = str(path.relative_to(root))
            if path.is_symlink():
                result[rel] = LINK(os.readlink(path))
            elif path.is_dir():
                result[rel] = DIR
            else:
                result[rel] = FILE(path.read_bytes()) if path.is_file() else ("other", None)
    return result


def tar_members(path: Path) -> dict[str, tuple[Node, int, int, int, str, str, int]]:
    """name -> (node, mode, uid, gid, uname, gname, mtime) for each member."""
    result = {}
    with tarfile.open(path, "r:gz") as tar:
        for info in tar.getmembers():
            if info.isdir():
                node = DIR
            elif info.issym():
                node = LINK(info.linkname)
            else:
                extracted = tar.extractfile(info)
                assert extracted is not None, info.name
                node = FILE(extracted.read())
            result[info.name] = (node, info.mode, info.uid, info.gid, info.uname, info.gname, int(info.mtime))
    return result


@pytest.fixture
def area(tmp_path: Path) -> Path:
    """A resolved scratch directory (error paths are compared literally)."""
    root = tmp_path.resolve() / "area"
    root.mkdir()
    return root


def test_the_pack_program_stores_the_selection_relative_to_the_parent_without_owners_or_special_files(
    area: Path,
) -> None:
    src = area / "src"
    (src / "d").mkdir(parents=True)
    (src / "f").write_bytes(b"one")
    os.chmod(src / "f", 0o640)
    os.utime(src / "f", (1_500_000_000, 1_500_000_000))
    (src / "d" / "g").write_bytes(b"two")
    os.mkfifo(src / "d" / "fifo")
    os.mkfifo(src / "fifo")
    os.symlink("f", src / "link")
    (src / "unselected").write_bytes(b"no")
    archive = area / "a.tgz"
    assert outcome(pack_script(str(src), ["f", "d", "link", "fifo"], str(archive))) == ([], True)
    packed = tar_members(archive)
    assert {name: value[0] for name, value in packed.items()} == {
        "f": FILE(b"one"), "d": DIR, "d/g": FILE(b"two"), "link": LINK("f"),
    }
    assert all(value[2:6] == (0, 0, "", "") for value in packed.values())
    assert packed["f"][1] == 0o640 and packed["f"][6] == 1_500_000_000
    assert (archive.read_bytes()[:2]) == b"\x1f\x8b"


@pytest.mark.parametrize(
    "failure", ["existing-archive", "missing-parent", "parent-is-a-file", "missing-name", "missing-archive-dir"]
)
def test_the_pack_program_reports_each_failure_about_the_path_at_fault_and_leaves_no_archive(
    area: Path, failure: str
) -> None:
    src = area / "src"
    src.mkdir()
    (src / "f").write_bytes(b"1")
    parent, names, archive = src, ["f"], area / "a.tgz"
    if failure == "existing-archive":
        archive.write_bytes(b"precious")
        expected = ("EEXIST", archive)
    elif failure == "missing-parent":
        parent = area / "missing"
        expected = ("ENOENT", parent)
    elif failure == "parent-is-a-file":
        parent = src / "f"
        expected = ("ENOTDIR", parent)
    elif failure == "missing-name":
        names = ["f", "missing"]
        expected = ("ENOENT", src / "missing")
    else:
        archive = area / "missing" / "a.tgz"
        expected = ("ENOENT", archive)
    before = files_below(area)
    assert outcome(pack_script(str(parent), names, str(archive))) == ([(expected[0], str(expected[1]))], False)
    assert files_below(area) == before


@pytest.mark.parametrize("case", ["packs", "missing-parent", "no-tar-filter"])
def test_the_pack_program_reads_the_names_file_and_removes_it_before_anything_else(
    area: Path, case: str
) -> None:
    src = area / "src"
    src.mkdir()
    names = [f"report {i:03d} - it's *final*.txt" for i in range(150)] + ["new\nline", "ü"]
    for name in names:
        (src / name).write_bytes(name.encode())
    names_file, archive = area / "a.tgz.names", area / "a.tgz"
    names_file.write_bytes(b"\0".join(n.encode() for n in names))
    parent = area / "missing" if case == "missing-parent" else src
    errors, done = outcome(
        pack_list_script(str(parent), str(names_file), str(archive)), tar_filter=case != "no-tar-filter"
    )
    assert not names_file.exists()
    if case == "packs":
        assert (errors, done) == ([], True)
        assert {name: value[0] for name, value in tar_members(archive).items()} == {
            name: FILE(name.encode()) for name in names
        }
    else:
        code = "ENOENT" if case == "missing-parent" else "NOFILTER"
        assert [c for c, _ in errors] == [code] and not done
        assert not archive.exists()


def test_without_tar_filter_the_pack_program_writes_nothing(area: Path) -> None:
    (area / "src").mkdir()
    (area / "src" / "f").write_bytes(b"1")
    before = files_below(area)
    script = pack_script(str(area / "src"), ["f"], str(area / "a.tgz"))
    assert outcome(script, tar_filter=False) == ([("NOFILTER", None)], False)
    assert files_below(area) == before


def test_the_unpack_program_extracts_into_a_new_directory_and_removes_the_archive(area: Path) -> None:
    archive = area / "a.tgz"
    archive.write_bytes(make_archive([("d", DIR), ("d/f", FILE(b"1")), ("d/rel", LINK("f")), ("g", FILE(b"2"))]))
    (area / "parent").mkdir()
    assert outcome(unpack_script(str(archive), str(area / "parent" / "dest"))) == ([], True)
    assert files_below(area) == {
        "parent": DIR, "parent/dest": DIR, "parent/dest/d": DIR, "parent/dest/d/f": FILE(b"1"),
        "parent/dest/d/rel": LINK("f"), "parent/dest/g": FILE(b"2"),
    }


@pytest.mark.parametrize(
    "failure",
    ["existing-dest", "missing-dest-parent", "missing-archive", "not-gzip", "absolute-member", "dot-dot-member",
     "no-tar-filter"],
)
def test_the_unpack_program_reports_each_failure_keeps_the_archive_and_leaves_no_dest(
    area: Path, failure: str
) -> None:
    archive, dest = area / "a.tgz", area / "dest"
    members: list[tuple[str, Node]] = [("ok", FILE(b"extracted first"))]
    if failure == "absolute-member":
        members.append((str(area / "escaped"), FILE(b"x")))
    elif failure == "dot-dot-member":
        members.append(("../escaped", FILE(b"x")))
    data = b"not a gzip-compressed tar\n" * 8 if failure == "not-gzip" else make_archive(members)
    if failure != "missing-archive":
        archive.write_bytes(data)
    if failure == "existing-dest":
        dest.mkdir()
        (dest / "mine").write_bytes(b"kept")
    elif failure == "missing-dest-parent":
        dest = area / "missing" / "dest"
    expected = {
        "existing-dest": ("EEXIST", str(dest)),
        "missing-dest-parent": ("ENOENT", str(dest)),
        "missing-archive": ("ENOENT", str(archive)),
        "not-gzip": ("EIO", str(archive)),
        "absolute-member": ("EIO", str(archive)),
        "dot-dot-member": ("EIO", str(archive)),
        "no-tar-filter": ("NOFILTER", None),
    }[failure]
    before = files_below(area)
    errors, done = outcome(unpack_script(str(archive), str(dest)), tar_filter=failure != "no-tar-filter")
    assert (errors, done) == ([expected], False)
    assert files_below(area) == before


def test_the_merge_program_renames_merges_directories_and_replaces_non_directories(area: Path) -> None:
    for rel in ("s", "s/sub", "t", "t/sub", "d"):
        (area / rel).mkdir()
    (area / "s" / "sub" / "new").write_bytes(b"new")
    (area / "s" / "file").write_bytes(b"replaces")
    (area / "s" / "link").write_bytes(b"replaces the link")
    (area / "t" / "file").write_bytes(b"old")
    (area / "t" / "sub" / "kept").write_bytes(b"kept")
    os.symlink("../d", area / "t" / "link")
    assert outcome(merge_script(str(area / "s"), str(area / "t"))) == ([], True)
    assert files_below(area) == {
        "d": DIR, "t": DIR, "t/sub": DIR, "t/sub/new": FILE(b"new"), "t/sub/kept": FILE(b"kept"),
        "t/file": FILE(b"replaces"), "t/link": FILE(b"replaces the link"),
    }


@pytest.mark.parametrize(
    ("at_src", "at_dst", "code"),
    [("file", "dir", "EISDIR"), ("dir", "file", "ENOTDIR")],
)
def test_the_merge_program_reports_a_clash_about_the_deepest_destination_path(
    area: Path, at_src: str, at_dst: str, code: str
) -> None:
    for rel in ("s", "s/sub", "t", "t/sub"):
        (area / rel).mkdir()
    for side, kind in (("s", at_src), ("t", at_dst)):
        target = area / side / "sub" / "c"
        if kind == "dir":
            target.mkdir()
            (target / "x").write_bytes(b"x")
        else:
            target.write_bytes(side.encode())
    before = files_below(area)
    assert outcome(merge_script(str(area / "s"), str(area / "t"))) == ([(code, str(area / "t" / "sub" / "c"))], False)
    assert files_below(area) == before


def test_the_merge_program_needs_no_tar_filter(area: Path) -> None:
    (area / "s").mkdir()
    (area / "s" / "f").write_bytes(b"1")
    assert outcome(merge_script(str(area / "s"), str(area / "t")), tar_filter=False) == ([], True)
    assert files_below(area) == {"t": DIR, "t/f": FILE(b"1")}


def test_archive_scripts_ignore_a_python3_alias(area: Path) -> None:
    (area / "src").mkdir()
    (area / "src" / "f").write_bytes(b"1")
    (area / "t").mkdir()
    steps = [
        (pack_script(str(area / "src"), ["f"], str(area / "a.tgz")), "@K"),
        (unpack_script(str(area / "a.tgz"), str(area / "stage")), "@K"),
        (merge_script(str(area / "stage"), str(area / "t")), "@K"),
    ]
    for script, expected in steps:
        assert script is not None
        _, output = run_in_hostile_bash(script)
        assert "ALIASED" not in output and "HIJACKED" not in output, output
        assert expected in output, output
    assert files_below(area) == {"src": DIR, "src/f": FILE(b"1"), "t": DIR, "t/f": FILE(b"1")}
