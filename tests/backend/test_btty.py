"""BttyBackend: the shared contract in exec mode over the fake btty, the
transfer-only mode, argv shapes, error mapping, chunked deletes, hostile
names, btty's informational line, and the default runner.

Nothing here runs the real btty. A backend gets a FakeBtty
(tests/backend/fake_btty.py), which emulates btty over a directory below
tmp_path, or a ``Recorder``, which runs nothing at all. The default-runner
tests patch ``subprocess.run``.

Every test states ``btty_exec`` and ``remote_home`` explicitly, so it holds
whatever ``Settings()`` defaults to (decisions D1/D2), except the PATH-guard
test, which is about ``Settings()`` itself and holds in either mode.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.backend.contract import DIR, FILE, LINK, BackendContract, Node
from tests.backend.fake_btty import INFO_LINE, FakeBtty, run_script, script_paths
from twinfm.backend.base import (
    ALL_CAPS,
    AlreadyExists,
    Backend,
    BackendUnavailable,
    Cap,
    FsError,
    IsADirectory,
    NotFound,
    OperationFailed,
    PermissionDenied,
    Unsupported,
)
from twinfm.backend.btty import TRANSFER_CAPS, BttyBackend, open_btty, run_btty
from twinfm.backend.btty_scripts import COMMAND_BUDGET, mkdir_script
from twinfm.config import Settings

TIMEOUT = 37
"""A --timeout other than the default, so argv checks prove it is the configured one."""

BSCP_HOME = "/mnt/home0/wst/workspaces/jake.blow.clearwateranalytics.com"
"""The folder the user's bscp function copies into."""

CONTAINER_ID = INFO_LINE.split()[1]

needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="permission bits do not bind root"
)


class Recorder:
    """A runner that runs nothing: it records each argv and returns ``answer``."""

    def __init__(self, answer: tuple[int, str, str] = (0, "", "")) -> None:
        self.answer = answer
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self.answer


def exec_backend(remote: str, runner: Callable[[list[str]], tuple[int, str, str]], **kw: object) -> BttyBackend:
    settings = Settings(remote_home=remote, btty_exec=True, btty_timeout=TIMEOUT, **kw)  # type: ignore[arg-type]
    return BttyBackend(settings, runner=runner)


def transfer_backend(remote: str, runner: Callable[[list[str]], tuple[int, str, str]], **kw: object) -> BttyBackend:
    settings = Settings(remote_home=remote, btty_exec=False, btty_timeout=TIMEOUT, **kw)  # type: ignore[arg-type]
    return BttyBackend(settings, runner=runner)


def exec_argv(script: str, command: str = "btty") -> list[str]:
    return [command, "--yes", "--no-launch", "--allow-unrestricted", "--json", "--timeout", str(TIMEOUT), "--exec", script]


def action(argv: list[str]) -> str:
    """--put, --get or --exec: what one recorded btty argv asks for."""
    return argv[7] if len(argv) == 9 else argv[2]


def json_result(stdout: str, exit_code: int = 0) -> str:
    """btty's --json result line."""
    return json.dumps({"stdout": stdout, "stderr": "", "exit_code": exit_code}) + "\n"


def remote_tree(root: str) -> dict[str, Node]:
    """Every path below ``root`` (lstat, symlinks not followed) as contract nodes."""
    result: dict[str, Node] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                result[rel] = LINK(os.readlink(path))
            elif os.path.isdir(path):
                result[rel] = DIR
            else:
                result[rel] = FILE(Path(path).read_bytes())
    return result


def staging_dirs_gone(fake: FakeBtty) -> bool:
    """Whether every --out path's temporary directory has been removed."""
    return all(not os.path.exists(os.path.dirname(out)) for out, _ in fake.out_paths)


@pytest.fixture
def remote(tmp_path: Path) -> str:
    """The fake remote: an empty directory, resolved (macOS /var is a symlink)."""
    path = tmp_path.resolve() / "remote"
    path.mkdir()
    return str(path)


@pytest.fixture
def local(tmp_path: Path) -> Path:
    """An empty local directory for the local side of transfers."""
    path = tmp_path / "local"
    path.mkdir()
    return path


@pytest.fixture
def fake(remote: str) -> Iterator[FakeBtty]:
    fake = FakeBtty(remote, timeout=TIMEOUT)
    yield fake
    assert fake.rejected == [], "a command over COMMAND_BUDGET was sent to btty"


def write(path: Path | str, data: bytes) -> str:
    Path(path).write_bytes(data)
    return str(path)


# -- the shared contract, exec mode -------------------------------------------------------


class _BttyExecContract(BackendContract):
    """BackendContract on BttyBackend in exec mode over the fake; ``info`` is
    the stream btty's informational line arrives on."""

    info: str | None = "stderr"

    @pytest.fixture
    def root(self, tmp_path: Path) -> str:
        root = tmp_path.resolve() / "remote"
        root.mkdir()
        return str(root)

    @pytest.fixture
    def fake(self, root: str) -> Iterator[FakeBtty]:
        fake = FakeBtty(root, timeout=TIMEOUT, info=self.info)
        yield fake
        assert fake.rejected == [], "a command over COMMAND_BUDGET was sent to btty"

    @pytest.fixture
    def backend(self, root: str, fake: FakeBtty) -> BttyBackend:
        return exec_backend(root, fake)

    def seed_dir(self, backend: Backend, path: str) -> None:
        os.mkdir(path)

    def seed_file(self, backend: Backend, path: str, data: bytes) -> None:
        if os.path.islink(path):
            os.unlink(path)
        Path(path).write_bytes(data)

    def seed_symlink(self, backend: Backend, path: str, target: str) -> None:
        os.symlink(target, path)

    def observe(self, backend: Backend, root: str) -> dict[str, Node]:
        return remote_tree(root)

    def test_the_contract_runs_in_exec_mode_over_the_fake(
        self, backend: BttyBackend, fake: FakeBtty
    ) -> None:
        assert backend.caps == ALL_CAPS
        assert backend.runner is fake


class TestBttyBackendContract(_BttyExecContract):
    info = "stderr"


class TestBttyBackendContractInfoLineOnStdout(_BttyExecContract):
    info = "stdout"


def test_the_contract_suite_is_inherited_unchanged() -> None:
    contract_tests = {name for name in dir(BackendContract) if name.startswith("test_")}
    assert contract_tests
    for cls in (TestBttyBackendContract, TestBttyBackendContractInfoLineOnStdout):
        for name in contract_tests:
            assert getattr(cls, name) is getattr(BackendContract, name), (cls.__name__, name)


# -- transfer-only mode (btty_exec=False) --------------------------------------------------


class TestTransferOnly:
    def test_caps_are_exactly_upload_and_download(self, remote: str) -> None:
        backend = transfer_backend(remote, Recorder())
        assert backend.caps == frozenset({Cap.UPLOAD, Cap.DOWNLOAD}) == TRANSFER_CAPS

    @pytest.mark.parametrize(
        "operation",
        [
            "list_dir",
            "list_dir-missing",
            "walk",
            "stat",
            "exists",
            "mkdir",
            "delete",
            "delete-recursive",
            "delete-missing",
            "delete_many",
            "rename",
            "rename-missing",
            "copy_within",
        ],
    )
    def test_everything_but_transfers_raises_unsupported_without_calling_the_runner(
        self, remote: str, operation: str
    ) -> None:
        runner = Recorder()
        backend = transfer_backend(remote, runner)
        f, d, new = write(f"{remote}/f", b"x"), f"{remote}/d", f"{remote}/new"
        os.mkdir(d)
        calls: dict[str, Callable[[], object]] = {
            "list_dir": lambda: backend.list_dir(remote),
            "list_dir-missing": lambda: backend.list_dir(f"{remote}/missing"),
            "walk": lambda: backend.walk(remote),
            "stat": lambda: backend.stat(f),
            "exists": lambda: backend.exists(f),
            "mkdir": lambda: backend.mkdir(new),
            "delete": lambda: backend.delete(f),
            "delete-recursive": lambda: backend.delete(d, recursive=True),
            "delete-missing": lambda: backend.delete(f"{remote}/missing"),
            "delete_many": lambda: backend.delete_many([f, d]),
            "rename": lambda: backend.rename(f, new),
            "rename-missing": lambda: backend.rename(f"{remote}/missing", new),
            "copy_within": lambda: backend.copy_within(f, new),
        }
        with pytest.raises(Unsupported):
            calls[operation]()
        assert runner.calls == []
        assert remote_tree(remote) == {"f": FILE(b"x"), "d": DIR}

    @pytest.mark.parametrize("command", ["btty", "/opt/tools/btty-nightly"])
    def test_upload_with_overwrite_is_exactly_the_bscp_call(self, local: Path, command: str) -> None:
        runner = Recorder()
        backend = BttyBackend(
            Settings(remote_home=BSCP_HOME, btty_exec=False, btty_command=command), runner=runner
        )
        src = write(local / "a.txt", b"hello")
        backend.upload(src, "/r/a.txt", overwrite=True)
        assert runner.calls == [[command, "--yes", "--put", src, "--to", "/r/a.txt"]]

    def test_a_relative_destination_resolves_against_the_remote_home(self, local: Path) -> None:
        runner = Recorder()
        backend = BttyBackend(Settings(remote_home=BSCP_HOME, btty_exec=False), runner=runner)
        src = write(local / "a.txt", b"hello")
        backend.upload(src, "a.txt", overwrite=True)
        backend.upload(src, "sub/../b.txt", overwrite=True)
        assert runner.calls == [
            ["btty", "--yes", "--put", src, "--to", f"{BSCP_HOME}/a.txt"],
            ["btty", "--yes", "--put", src, "--to", f"{BSCP_HOME}/b.txt"],
        ]

    def test_upload_without_overwrite_raises_unsupported_and_sends_nothing(
        self, remote: str, local: Path
    ) -> None:
        runner = Recorder()
        backend = transfer_backend(remote, runner)
        with pytest.raises(Unsupported):
            backend.upload(write(local / "a.txt", b"x"), f"{remote}/a.txt")
        assert runner.calls == []

    def test_upload_creates_the_file_then_replaces_it_silently_as_bscp_does(
        self, remote: str, local: Path, fake: FakeBtty
    ) -> None:
        backend = transfer_backend(remote, fake)
        backend.upload(write(local / "v1", b"first"), f"{remote}/a.txt", overwrite=True)
        assert remote_tree(remote) == {"a.txt": FILE(b"first")}
        backend.upload(write(local / "v2", b"second"), f"{remote}/a.txt", overwrite=True)
        assert remote_tree(remote) == {"a.txt": FILE(b"second")}
        assert [argv[2] for argv in fake.calls] == ["--put", "--put"]

    def test_upload_into_a_missing_remote_folder_raises_not_found(
        self, remote: str, local: Path, fake: FakeBtty
    ) -> None:
        backend = transfer_backend(remote, fake)
        target = f"{remote}/missing/a.txt"
        with pytest.raises(NotFound) as err:
            backend.upload(write(local / "a.txt", b"x"), target, overwrite=True)
        assert err.value.path == target
        assert remote_tree(remote) == {}

    def test_download_lands_at_local_dest_through_an_out_path_that_never_existed(
        self, remote: str, local: Path, fake: FakeBtty
    ) -> None:
        write(f"{remote}/f", b"remote bytes")
        backend = transfer_backend(remote, fake)
        dest = local / "got"
        backend.download(f"{remote}/f", str(dest))
        assert dest.read_bytes() == b"remote bytes"
        assert sorted(os.listdir(local)) == ["got"]
        [(out, existed)] = fake.out_paths
        assert not existed
        assert out != str(dest)
        assert staging_dirs_gone(fake)
        assert [argv[2] for argv in fake.calls] == ["--get"]

    def test_a_failed_download_carries_btty_s_reason_and_never_stats(
        self, remote: str, local: Path, fake: FakeBtty
    ) -> None:
        backend = transfer_backend(remote, fake)
        dest = local / "got"
        with pytest.raises(FsError) as err:
            backend.download(f"{remote}/missing", str(dest))
        assert "cannot open" in err.value.message
        assert fake.of_kind("--exec") == []
        assert os.listdir(local) == []
        assert staging_dirs_gone(fake)

    def test_download_refuses_an_existing_local_destination_before_calling_btty(
        self, remote: str, local: Path
    ) -> None:
        runner = Recorder()
        backend = transfer_backend(remote, runner)
        dest = write(local / "existing", b"keep")
        with pytest.raises(AlreadyExists):
            backend.download(f"{remote}/f", dest)
        assert runner.calls == []
        assert Path(dest).read_bytes() == b"keep"


# -- exec mode: argv shapes and transfers ---------------------------------------------------


class TestExecMode:
    @pytest.mark.parametrize("timeout", [1, TIMEOUT, 3600])
    def test_every_exec_argv_carries_the_fixed_flags_and_the_configured_timeout(
        self, remote: str, local: Path, timeout: int
    ) -> None:
        command = "btty-under-test"
        fake = FakeBtty(remote, command=command, timeout=timeout)
        backend = BttyBackend(
            Settings(remote_home=remote, btty_exec=True, btty_timeout=timeout, btty_command=command),
            runner=fake,
        )
        write(f"{remote}/f", b"data")
        backend.list_dir(remote)
        backend.walk(remote)
        backend.stat(f"{remote}/f")
        assert not backend.exists(f"{remote}/missing")
        backend.mkdir(f"{remote}/d")
        backend.copy_within(f"{remote}/f", f"{remote}/g")
        backend.rename(f"{remote}/g", f"{remote}/d/g")
        backend.upload(write(local / "up", b"up"), f"{remote}/up")
        with pytest.raises(NotFound):
            backend.download(f"{remote}/missing", str(local / "got"))
        backend.delete(f"{remote}/d", recursive=True)
        backend.delete_many([f"{remote}/f", f"{remote}/up"])
        assert remote_tree(remote) == {}

        execs = fake.of_kind("--exec")
        assert len(execs) >= 10
        for argv in execs:
            assert argv[:8] == [
                command,
                "--yes",
                "--no-launch",
                "--allow-unrestricted",
                "--json",
                "--timeout",
                str(timeout),
                "--exec",
            ]
            assert len(argv) == 9
        assert all(argv[:2] == [command, "--yes"] for argv in fake.calls)

    def test_upload_checks_the_destination_then_sends_the_bscp_call(
        self, remote: str, local: Path, fake: FakeBtty
    ) -> None:
        backend = exec_backend(remote, fake)
        src = write(local / "a.txt", b"new")
        backend.upload(src, f"{remote}/a.txt")
        write(f"{remote}/b.txt", b"old")
        backend.upload(src, f"{remote}/b.txt", overwrite=True)
        assert [action(argv) for argv in fake.calls] == ["--exec", "--put", "--exec", "--put"]
        assert fake.of_kind("--put") == [
            ["btty", "--yes", "--put", src, "--to", f"{remote}/a.txt"],
            ["btty", "--yes", "--put", src, "--to", f"{remote}/b.txt"],
        ]
        assert remote_tree(remote) == {"a.txt": FILE(b"new"), "b.txt": FILE(b"new")}

    def test_upload_onto_a_symlink_to_a_directory_replaces_the_link_not_the_directory(
        self, remote: str, local: Path, fake: FakeBtty
    ) -> None:
        # btty --put moves its temporary file with `mv`, which would drop it
        # inside the directory a symlink points to.
        os.mkdir(f"{remote}/d")
        os.symlink("d", f"{remote}/to-d")
        backend = exec_backend(remote, fake)
        backend.upload(write(local / "src", b"new"), f"{remote}/to-d", overwrite=True)
        assert remote_tree(remote) == {"d": DIR, "to-d": FILE(b"new")}

    def test_upload_to_the_root_is_refused_before_anything_is_sent(
        self, remote: str, local: Path
    ) -> None:
        runner = Recorder()
        with pytest.raises(IsADirectory):
            exec_backend(remote, runner).upload(write(local / "src", b"x"), "/", overwrite=True)
        assert runner.calls == []

    def test_upload_into_a_missing_remote_folder_raises_not_found(
        self, remote: str, local: Path, fake: FakeBtty
    ) -> None:
        backend = exec_backend(remote, fake)
        with pytest.raises(NotFound):
            backend.upload(write(local / "src", b"x"), f"{remote}/missing/a.txt", overwrite=True)
        assert remote_tree(remote) == {}

    def test_downloads_never_name_an_existing_out_path_and_land_at_local_dest(
        self, remote: str, local: Path, fake: FakeBtty
    ) -> None:
        backend = exec_backend(remote, fake)
        for i in range(3):
            write(f"{remote}/f{i}", f"content {i}".encode())
            backend.download(f"{remote}/f{i}", str(local / f"got{i}"))
        assert {name: (local / name).read_bytes() for name in os.listdir(local)} == {
            f"got{i}": f"content {i}".encode() for i in range(3)
        }
        assert len(fake.out_paths) == 3
        assert all(not existed for _, existed in fake.out_paths)
        assert len({out for out, _ in fake.out_paths}) == 3
        assert staging_dirs_gone(fake)

    @pytest.mark.parametrize(
        ("seed", "expected"),
        [("missing", NotFound), ("dir", IsADirectory)],
    )
    def test_a_failed_download_is_explained_by_a_stat(
        self, remote: str, local: Path, fake: FakeBtty, seed: str, expected: type[FsError]
    ) -> None:
        if seed == "dir":
            os.mkdir(f"{remote}/target")
        backend = exec_backend(remote, fake)
        with pytest.raises(expected) as err:
            backend.download(f"{remote}/target", str(local / "got"))
        assert err.value.path == f"{remote}/target"
        assert [action(argv) for argv in fake.calls] == ["--get", "--exec"]
        assert os.listdir(local) == []
        assert staging_dirs_gone(fake)

    def test_relative_paths_resolve_against_the_remote_home(
        self, remote: str, fake: FakeBtty
    ) -> None:
        os.mkdir(f"{remote}/sub")
        write(f"{remote}/sub/f", b"x")
        backend = exec_backend(remote, fake)
        assert backend.home() == remote
        assert [(e.name, e.path) for e in backend.list_dir("sub")] == [("f", f"{remote}/sub/f")]
        assert backend.stat("sub/../sub/f").path == f"{remote}/sub/f"

    @needs_non_root
    def test_an_unreadable_folder_raises_permission_denied(
        self, remote: str, fake: FakeBtty
    ) -> None:
        locked = f"{remote}/locked"
        os.mkdir(locked)
        os.mkdir(f"{locked}/inner")
        os.chmod(locked, 0)
        try:
            backend = exec_backend(remote, fake)
            with pytest.raises(PermissionDenied) as err:
                backend.list_dir(locked)
            assert err.value.path == locked
            with pytest.raises(PermissionDenied) as err:
                backend.walk(remote)
            assert err.value.path == locked
        finally:
            os.chmod(locked, 0o755)

    def test_a_remote_without_python3_cannot_list(self, remote: str, fake: FakeBtty) -> None:
        fake.respond(127, json_result("bash: line 1: python3: command not found\r\n", 127))
        with pytest.raises(OperationFailed) as err:
            exec_backend(remote, fake).list_dir(remote)
        assert "python3: command not found" in err.value.message
        assert "127" in err.value.message


class TestRefusedBeforeAnythingIsSent:
    """Calls that must fail before reaching btty. They use a Recorder, which
    runs nothing, so a broken backend cannot do damage."""

    @pytest.mark.parametrize(
        ("home", "call"),
        [
            ("/top", lambda b: b.delete("/")),
            ("/top", lambda b: b.delete("/", recursive=True)),
            ("/top", lambda b: b.delete("..")),
            ("/top", lambda b: b.delete("//", recursive=True)),
            ("/top", lambda b: b.delete_many(["/top/a", "/top/b", "/"], recursive=True)),
        ],
        ids=["root", "root-recursive", "dotdot-to-root", "double-slash", "root-in-a-batch"],
    )
    def test_deleting_the_root_is_refused(self, home: str, call: Callable[[BttyBackend], object]) -> None:
        runner = Recorder()
        with pytest.raises(OperationFailed):
            call(exec_backend(home, runner))
        assert runner.calls == []

    @pytest.mark.parametrize(
        "call",
        [
            lambda b, long: b.list_dir(long),
            lambda b, long: b.walk(long),
            lambda b, long: b.stat(long),
            lambda b, long: b.mkdir(long),
            lambda b, long: b.rename(long, long + "-2"),
            lambda b, long: b.copy_within(long, long + "-2"),
            lambda b, long: b.delete(long),
            lambda b, long: b.delete_many(["/top/ok", long, "/top/never"]),
        ],
        ids=["list_dir", "walk", "stat", "mkdir", "rename", "copy_within", "delete", "delete_many"],
    )
    def test_a_path_too_long_for_one_command_raises_operation_failed(
        self, call: Callable[[BttyBackend, str], object]
    ) -> None:
        runner = Recorder()
        long = "/top/" + "/".join(["x" * 100] * 12)  # longer than any one command may be
        with pytest.raises(OperationFailed) as err:
            call(exec_backend("/top", runner), long)
        assert "too long" in err.value.message
        assert runner.calls == []

    def test_a_nul_byte_in_a_path_raises_operation_failed(self) -> None:
        runner = Recorder()
        backend = exec_backend("/top", runner)
        for call in (
            lambda: backend.list_dir("/top/a\0b"),
            lambda: backend.delete("/top/a\0b"),
            lambda: backend.rename("/top/a", "/top/a\0b"),
        ):
            with pytest.raises(OperationFailed):
                call()
        assert runner.calls == []


# -- chunked deletes --------------------------------------------------------------------------


class TestBulkDelete:
    @staticmethod
    def make_files(remote: str, count: int) -> tuple[Path, list[str], list[str]]:
        folder = Path(remote, "jake.blow.clearwateranalytics.com", "build output")
        folder.mkdir(parents=True)
        names = [f"report {i:03d} - it's *final*.txt" for i in range(count)]
        for name in names:
            (folder / name).write_bytes(b"x")
        (folder / "keep me").write_bytes(b"k")
        return folder, names, [str(folder / name) for name in names]

    def test_deleting_200_files_takes_several_commands_each_within_the_budget(
        self, remote: str, fake: FakeBtty
    ) -> None:
        folder, _, paths = self.make_files(remote, 200)
        exec_backend(remote, fake).delete_many(paths)
        assert os.listdir(folder) == ["keep me"]
        scripts = fake.scripts
        assert 1 < len(scripts) < 200
        assert all(len(script.encode("utf-8")) <= COMMAND_BUDGET for script in scripts)
        assert [path for script in scripts for path in script_paths(script)] == paths
        assert fake.rejected == []

    def test_a_bulk_delete_stops_at_the_first_failure_and_keeps_what_follows(
        self, remote: str, fake: FakeBtty
    ) -> None:
        folder, names, paths = self.make_files(remote, 200)
        os.unlink(paths[150])
        with pytest.raises(NotFound) as err:
            exec_backend(remote, fake).delete_many(paths)
        assert err.value.path == paths[150]
        assert sorted(os.listdir(folder)) == sorted(["keep me", *names[151:]])
        assert paths[150] in script_paths(fake.scripts[-1])

    def test_a_bulk_delete_of_nothing_sends_nothing(self, remote: str) -> None:
        runner = Recorder()
        exec_backend(remote, runner).delete_many([])
        assert runner.calls == []

    def test_the_fake_rejects_a_command_over_the_budget_without_running_it(
        self, remote: str
    ) -> None:
        fake = FakeBtty(remote, timeout=TIMEOUT)
        target = f"{remote}/made"
        script = mkdir_script(target)
        over = script + " " * (COMMAND_BUDGET + 1 - len(script.encode()))
        code, _, stderr = fake(exec_argv(over))
        assert code == 1
        assert "over the" in stderr
        assert fake.rejected == [over]
        assert not os.path.exists(target)
        at_budget = script + " " * (COMMAND_BUDGET - len(script.encode()))
        code, stdout, _ = fake(exec_argv(at_budget))
        assert code == 0 and "@K" in stdout
        assert os.path.isdir(target)

    def test_a_rejected_command_raises_operation_failed(self, remote: str, fake: FakeBtty) -> None:
        fake.respond(1, "", "btty: that command makes a 3600 byte line, over the 3500 limit -- it is typed\n")
        with pytest.raises(OperationFailed) as err:
            exec_backend(remote, fake).mkdir(f"{remote}/d")
        assert "over the 3500 limit" in err.value.message


# -- hostile names ------------------------------------------------------------------------------

HOSTILE = [
    "with space",
    "it's",
    "new\nline",
    "-leading-dash",
    "star*",
    "ünïcødé-日本語",
    "-rf * 'q' $(x)\n ü",
]
DECOYS = {"with", "space", "it", "s", "line", "star", "starfish", "-rf", "decoy"}
"""Names that word splitting, globbing or option parsing would hit by mistake."""


@pytest.mark.parametrize("name", HOSTILE, ids=repr)
def test_a_hostile_name_round_trips_through_list_rename_and_delete(
    remote: str, fake: FakeBtty, name: str
) -> None:
    for decoy in DECOYS:
        write(f"{remote}/{decoy}", b"decoy")
    write(f"{remote}/{name}", b"payload")
    backend = exec_backend(remote, fake)

    listed = {e.name: e for e in backend.list_dir(remote)}
    assert set(listed) == DECOYS | {name}
    assert (listed[name].path, listed[name].kind, listed[name].size) == (
        f"{remote}/{name}",
        "file",
        7,
    )

    renamed = f"{name}.renamed"
    backend.rename(f"{remote}/{name}", f"{remote}/{renamed}")
    assert {e.name for e in backend.list_dir(remote)} == DECOYS | {renamed}
    assert Path(remote, renamed).read_bytes() == b"payload"

    backend.delete(f"{remote}/{renamed}")
    assert set(os.listdir(remote)) == DECOYS
    assert all(Path(remote, decoy).read_bytes() == b"decoy" for decoy in DECOYS)
    assert all(len(argv) == 9 for argv in fake.of_kind("--exec"))


def test_hostile_names_survive_a_bulk_delete_and_a_recursive_one(remote: str, fake: FakeBtty) -> None:
    for decoy in DECOYS:
        write(f"{remote}/{decoy}", b"decoy")
    folder = f"{remote}/-x it's *\n"
    os.mkdir(folder)
    for name in HOSTILE:
        write(f"{remote}/{name}", b"x")
        write(f"{folder}/{name}", b"x")
    backend = exec_backend(remote, fake)
    assert sorted(e.name for e in backend.list_dir(folder)) == sorted(HOSTILE)
    backend.delete_many([f"{remote}/{name}" for name in HOSTILE])
    backend.delete(folder, recursive=True)
    assert set(os.listdir(remote)) == DECOYS


# -- btty's informational line ------------------------------------------------------------------


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
class TestInfoLine:
    """``btty: <container-id> on <host>`` comes first on every call, --json
    included, on stdout or stderr. It is never data, never an error by itself
    and never part of an error message."""

    @staticmethod
    def watched(fake: FakeBtty) -> tuple[list[tuple[int, str, str]], Callable[[list[str]], tuple[int, str, str]]]:
        seen: list[tuple[int, str, str]] = []

        def runner(argv: list[str]) -> tuple[int, str, str]:
            seen.append(fake(argv))
            return seen[-1]

        return seen, runner

    @staticmethod
    def carried(seen: list[tuple[int, str, str]], stream: str) -> bool:
        index = 1 if stream == "stdout" else 2
        return bool(seen) and all(answer[index].startswith(INFO_LINE) for answer in seen)

    def test_exec_results_parse_and_the_line_never_becomes_an_entry(
        self, remote: str, stream: str
    ) -> None:
        fake = FakeBtty(remote, timeout=TIMEOUT, info=stream)
        seen, runner = self.watched(fake)
        os.mkdir(f"{remote}/d")
        write(f"{remote}/d/x", b"12")
        write(f"{remote}/a", b"1")
        backend = exec_backend(remote, runner)
        assert {e.name for e in backend.list_dir(remote)} == {"a", "d"}
        assert sorted(e.path for e in backend.walk(remote)) == [f"{remote}/a", f"{remote}/d", f"{remote}/d/x"]
        assert backend.stat(f"{remote}/d/x").size == 2
        backend.mkdir(f"{remote}/e")
        backend.rename(f"{remote}/a", f"{remote}/e/a")
        backend.copy_within(f"{remote}/d", f"{remote}/d2")
        backend.delete(f"{remote}/d", recursive=True)
        assert remote_tree(remote) == {"e": DIR, "e/a": FILE(b"1"), "d2": DIR, "d2/x": FILE(b"12")}
        assert self.carried(seen, stream)

    @pytest.mark.parametrize("exec_mode", [False, True], ids=["transfer-only", "exec"])
    def test_puts_and_gets_succeed_and_the_line_is_not_data(
        self, remote: str, local: Path, stream: str, exec_mode: bool
    ) -> None:
        fake = FakeBtty(remote, timeout=TIMEOUT, info=stream)
        seen, runner = self.watched(fake)
        backend = (exec_backend if exec_mode else transfer_backend)(remote, runner)
        data = bytes(range(256)) * 4
        backend.upload(write(local / "src", data), f"{remote}/up", overwrite=True)
        assert Path(remote, "up").read_bytes() == data
        backend.download(f"{remote}/up", str(local / "got"))
        assert (local / "got").read_bytes() == data
        assert self.carried(seen, stream)

    def test_the_line_on_the_same_line_as_the_result_still_parses(
        self, remote: str, stream: str
    ) -> None:
        fake = FakeBtty(remote, timeout=TIMEOUT, info=None)
        write(f"{remote}/a", b"1")

        def runner(argv: list[str]) -> tuple[int, str, str]:
            code, out, err = fake(argv)
            if stream == "stdout":
                return code, INFO_LINE + out, err
            return code, out, INFO_LINE + err

        assert [e.name for e in exec_backend(remote, runner).list_dir(remote)] == ["a"]

    def test_text_around_the_result_object_is_skipped(self, remote: str, stream: str) -> None:
        record = json.dumps(
            {"name": "YQ==", "kind": "file", "size": 1, "mtime": 1, "mode": 420,
             "link_target": None, "target_kind": None}
        )
        noise = f"{INFO_LINE}\r\n{{not json\r\n"
        payload = noise + json_result(f"@J {record}\r\n@K\r\n") + "trailing {\n"
        answer = (0, payload, "") if stream == "stdout" else (0, "", payload)
        entries = exec_backend(remote, Recorder(answer)).list_dir(remote)
        assert [(e.name, e.kind, e.size) for e in entries] == [("a", "file", 1)]

    @pytest.mark.parametrize("action", ["exec", "put", "get"])
    def test_the_line_never_appears_in_an_error_message(
        self, remote: str, local: Path, stream: str, action: str
    ) -> None:
        fake = FakeBtty(remote, timeout=TIMEOUT, info=stream)
        exec_mode = action == "exec"
        backend = (exec_backend if exec_mode else transfer_backend)(remote, fake)
        src = write(local / "src", b"x")
        attempts: list[Callable[[], object]] = {
            "exec": [
                lambda: backend.list_dir(f"{remote}/missing"),
                lambda: backend.mkdir(f"{remote}/missing/new"),
            ],
            "put": [lambda: backend.upload(src, f"{remote}/missing/a", overwrite=True)],
            "get": [lambda: backend.download(f"{remote}/missing", str(local / "got"))],
        }[action]
        for attempt in attempts:
            with pytest.raises(FsError) as err:
                attempt()
            assert CONTAINER_ID not in err.value.message
            assert CONTAINER_ID not in str(err.value)

        fake.respond(1, "", "btty: the frobnicator is jammed\n")
        with pytest.raises(OperationFailed) as err:
            attempts[0]()
        assert err.value.message == "the frobnicator is jammed"

    def test_the_line_alone_is_never_the_reason_for_a_failure(
        self, remote: str, local: Path, stream: str
    ) -> None:
        fake = FakeBtty(remote, timeout=TIMEOUT, info=stream)
        backend = transfer_backend(remote, fake)
        fake.respond(1)
        with pytest.raises(OperationFailed) as err:
            backend.upload(write(local / "src", b"x"), f"{remote}/a", overwrite=True)
        assert CONTAINER_ID not in str(err.value)
        assert err.value.message

    @pytest.mark.parametrize("exec_mode", [False, True], ids=["put", "exec"])
    def test_a_failure_without_a_btty_line_is_reported_without_the_info_line(
        self, remote: str, local: Path, stream: str, exec_mode: bool
    ) -> None:
        # e.g. node itself failing (the user's NODE_OPTIONS problem): no `btty: ` reason line.
        fake = FakeBtty(remote, timeout=TIMEOUT, info=stream)
        fake.respond(1, "", "node:internal/modules/cjs/loader:1228\n  throw err;\nError: Cannot find module 'x'\n")
        backend = (exec_backend if exec_mode else transfer_backend)(remote, fake)
        with pytest.raises(OperationFailed) as err:
            if exec_mode:
                backend.list_dir(remote)
            else:
                backend.upload(write(local / "src", b"x"), f"{remote}/a", overwrite=True)
        assert "Cannot find module" in err.value.message
        assert CONTAINER_ID not in str(err.value)

    def test_the_line_alone_with_success_is_a_successful_transfer(
        self, remote: str, local: Path, stream: str
    ) -> None:
        fake = FakeBtty(remote, timeout=TIMEOUT, info=stream)
        fake.respond(0)
        transfer_backend(remote, fake).upload(write(local / "src", b"x"), f"{remote}/a", overwrite=True)
        assert fake.of_kind("--put")


# -- error mapping ------------------------------------------------------------------------------

BTTY_FAILURES = [
    ("btty: no containers found\n", BackendUnavailable),
    ("btty: no Beacon IDE container found\n  start one at: https://example.invalid/launch\n", BackendUnavailable),
    ("btty: timed out after 37s -- the command was interrupted\n", BackendUnavailable),
    ("btty: the connection dropped\n", BackendUnavailable),
    ("btty: refusing to run in 6a35ed517a03: that is a Beacon IDE container\n", Unsupported),
    ("btty: --exec requires --user in a sandbox: an exec without -u runs as root\n", Unsupported),
    ("btty: that command is not valid shell syntax, so nothing was run:\n  oops\n", OperationFailed),
    ("btty: the frobnicator is jammed\n", OperationFailed),
]


@pytest.mark.parametrize("info", ["stderr", "stdout", None])
@pytest.mark.parametrize("action", ["exec", "put", "get"])
@pytest.mark.parametrize(
    ("stderr", "expected"),
    BTTY_FAILURES,
    ids=["no-containers", "no-ide", "timed-out", "dropped", "refusing", "requires-user", "syntax", "unknown"],
)
def test_btty_failures_map_to_the_documented_error(
    remote: str, local: Path, info: str | None, action: str, stderr: str, expected: type[FsError]
) -> None:
    fake = FakeBtty(remote, timeout=TIMEOUT, info=info)
    fake.respond(1, "", stderr)
    src = write(local / "src", b"x")
    with pytest.raises(FsError) as err:
        if action == "exec":
            exec_backend(remote, fake).list_dir(remote)
        elif action == "put":
            transfer_backend(remote, fake).upload(src, f"{remote}/a", overwrite=True)
        else:
            transfer_backend(remote, fake).download(f"{remote}/a", str(local / "got"))
    assert type(err.value) is expected
    reason = stderr.removeprefix("btty: ").split("\n")[0]
    assert reason in err.value.message
    assert INFO_LINE not in err.value.message
    assert len(fake.calls) == 1


def test_exit_1_with_no_containers_found_raises_backend_unavailable(remote: str, local: Path) -> None:
    runner = Recorder((1, "", "btty: no containers found\n"))
    for backend in (exec_backend(remote, runner), transfer_backend(remote, runner)):
        with pytest.raises(BackendUnavailable):
            backend.upload(write(local / "src", b"x"), f"{remote}/a", overwrite=True)


def test_an_unrecognised_failure_raises_operation_failed_carrying_the_message(remote: str, local: Path) -> None:
    runner = Recorder((1, "", "btty: something nobody has seen before\n  with a second line\n"))
    with pytest.raises(OperationFailed) as err:
        transfer_backend(remote, runner).upload(write(local / "src", b"x"), f"{remote}/a", overwrite=True)
    assert type(err.value) is OperationFailed
    assert err.value.message == "something nobody has seen before with a second line"
    assert err.value.backend_id == "btty"


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("no such directory: {parent}", NotFound),
        ("wrote -1 of 3 bytes to {path}", OperationFailed),
        ("the remote went quiet for 60s", BackendUnavailable),
        ("the connection dropped", BackendUnavailable),
        ("cut short after 10 of 20 bytes", BackendUnavailable),
    ],
)
@pytest.mark.parametrize("word", ["token", "connection", "timed out", "no such directory", "refusing to run in"])
def test_words_in_the_remote_path_do_not_decide_the_class(
    remote: str, local: Path, word: str, detail: str, expected: type[FsError]
) -> None:
    parent = f"{remote}/{word}"
    path = f"{parent}/a.txt"
    runner = Recorder((1, "", f"{INFO_LINE}\r\nbtty: {path}: {detail.format(parent=parent, path=path)}\n"))
    with pytest.raises(FsError) as err:
        transfer_backend(remote, runner).upload(write(local / "src", b"x"), path, overwrite=True)
    assert type(err.value) is expected
    assert err.value.path == path


def test_exec_with_no_result_object_and_exit_0_raises_operation_failed(remote: str) -> None:
    runner = Recorder((0, f"{INFO_LINE}\r\n", ""))
    with pytest.raises(OperationFailed) as err:
        exec_backend(remote, runner).list_dir(remote)
    assert CONTAINER_ID not in str(err.value)


# -- the default runner (subprocess.run patched; nothing is spawned) --------------------------


Answer = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _ok(argv: list[str]) -> subprocess.CompletedProcess[str]:
    if "--exec" in argv:  # a stat that finds nothing, as btty --exec --json reports it
        return subprocess.CompletedProcess(argv, 0, json_result("@X ENOENT\r\n"), "")
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replaces subprocess.run with a recorder; Popen fails the test if used."""
    spawn = SimpleNamespace(calls=[], answer=_ok)

    def record(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = list(args[0] if args else kwargs["args"])  # type: ignore[call-overload]
        spawn.calls.append(SimpleNamespace(argv=argv, kwargs=kwargs, environ=dict(os.environ)))
        return spawn.answer(argv)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the default runner must go through the patched subprocess.run")

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return spawn


def without_node_options(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k != "NODE_OPTIONS"}


class TestDefaultRunner:
    def test_a_backend_without_an_injected_runner_uses_run_btty(self) -> None:
        settings = Settings(remote_home=BSCP_HOME, btty_exec=False)
        assert BttyBackend(settings).runner is run_btty
        assert open_btty(settings).runner is run_btty

    @pytest.mark.parametrize("node_options", ["--foo", None], ids=["set", "unset"])
    def test_the_child_gets_an_empty_node_options_and_the_rest_of_the_environment(
        self,
        spawn: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        local: Path,
        node_options: str | None,
    ) -> None:
        if node_options is None:
            monkeypatch.delenv("NODE_OPTIONS", raising=False)
        else:
            monkeypatch.setenv("NODE_OPTIONS", node_options)
        before = dict(os.environ)
        src = write(local / "a.txt", b"x")

        BttyBackend(Settings(remote_home=BSCP_HOME, btty_exec=False)).upload(src, "/r/a.txt", overwrite=True)

        [call] = spawn.calls
        assert call.argv == ["btty", "--yes", "--put", src, "--to", "/r/a.txt"]
        env = call.kwargs["env"]
        assert env["NODE_OPTIONS"] == ""
        assert without_node_options(env) == without_node_options(call.environ)
        assert env["PATH"] == call.environ["PATH"]
        assert call.kwargs.get("stdin") is subprocess.DEVNULL
        assert dict(os.environ) == before
        assert os.environ.get("NODE_OPTIONS") == node_options

    def test_the_runner_returns_the_exit_code_and_both_streams(self, spawn: SimpleNamespace) -> None:
        spawn.answer = lambda argv: subprocess.CompletedProcess(argv, 3, "out text", "err text")
        assert run_btty(["btty", "--yes", "--get", "/r/x", "--out", "/nowhere/x"]) == (3, "out text", "err text")

    def test_a_btty_that_cannot_be_started_makes_the_backend_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, local: Path
    ) -> None:
        def missing(*args: object, **kwargs: object) -> None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "btty")

        monkeypatch.setattr(subprocess, "run", missing)
        backend = BttyBackend(Settings(remote_home=BSCP_HOME, btty_exec=False))
        with pytest.raises(BackendUnavailable) as err:
            backend.upload(write(local / "a.txt", b"x"), "/r/a.txt", overwrite=True)
        assert "cannot run" in err.value.message

    def test_settings_default_reaches_btty_only_through_path_where_the_guard_shim_answers(
        self, spawn: SimpleNamespace, btty_guard: object, local: Path
    ) -> None:
        # Settings() as it is, exec mode or not: the default command is found
        # through PATH, and under pytest PATH leads to the conftest guard shim.
        backend = BttyBackend(Settings())
        assert backend.runner is run_btty
        backend.upload(write(local / "a.txt", b"x"), "/r/a.txt", overwrite=True)

        assert spawn.calls
        assert spawn.calls[-1].argv[2] == "--put"
        shim = btty_guard.shim  # type: ignore[attr-defined]
        for call in spawn.calls:
            command = call.argv[0]
            assert command == "btty"
            assert os.sep not in command
            search = call.kwargs["env"]["PATH"]
            assert search == call.environ["PATH"]
            assert search.split(os.pathsep)[0] == str(btty_guard.bin_dir)  # type: ignore[attr-defined]
            found = shutil.which(command, path=search)
            assert found is not None and os.path.samefile(found, shim)


def test_open_btty_shares_one_backend_per_settings() -> None:
    settings = Settings(remote_home="/mnt/t19-shared", btty_exec=True)
    backend = open_btty(settings)
    assert open_btty(Settings(remote_home="/mnt/t19-shared", btty_exec=True)) is backend
    assert open_btty(Settings(remote_home="/mnt/t19-shared", btty_exec=False)) is not backend
    assert open_btty(Settings(remote_home="/mnt/t19-other", btty_exec=True)) is not backend


# -- the fake itself ------------------------------------------------------------------------------


class TestFakeBtty:
    def test_it_refuses_a_remote_path_outside_its_root_before_running_anything(
        self, remote: str, tmp_path: Path
    ) -> None:
        outside = str(tmp_path.resolve() / "outside")
        fake = FakeBtty(remote, timeout=TIMEOUT)
        with pytest.raises(AssertionError, match="outside the fake remote"):
            fake(exec_argv(mkdir_script(outside)))
        with pytest.raises(AssertionError, match="outside the fake remote"):
            fake(exec_argv(mkdir_script(f"{remote}/../outside")))
        assert not os.path.exists(outside)

    @pytest.mark.parametrize(
        "argv",
        [
            ["btty", "--put", "/l", "--to", "/r"],
            ["other", "--yes", "--put", "/l", "--to", "/r"],
            ["btty", "--yes", "--no-launch", "--json", "--timeout", "37", "--exec", "echo @K"],
            ["btty", "--yes", "--no-launch", "--allow-unrestricted", "--json", "--timeout", "0", "--exec", "x"],
        ],
        ids=["no-yes", "wrong-command", "no-allow-unrestricted", "zero-timeout"],
    )
    def test_it_rejects_a_malformed_argv(self, remote: str, argv: list[str]) -> None:
        with pytest.raises(AssertionError, match="fake btty"):
            FakeBtty(remote)(argv)

    def test_its_remote_shell_finds_python3_and_only_the_guard_shim_as_btty(
        self, remote: str, btty_guard: object
    ) -> None:
        status, output = run_script("command -v btty; python3 -I -c 'print(40 + 2)'")
        lines = output.split()
        assert status == 0
        assert os.path.samefile(lines[0], btty_guard.shim)  # type: ignore[attr-defined]
        assert lines[1] == "42"

    @pytest.mark.parametrize("info", ["stdout", "stderr"])
    def test_it_prints_the_info_line_first_on_the_chosen_stream(self, remote: str, info: str) -> None:
        fake = FakeBtty(remote, timeout=TIMEOUT, info=info)
        code, stdout, stderr = fake(exec_argv(mkdir_script(f"{remote}/d")))
        assert code == 0
        first, other = (stdout, stderr) if info == "stdout" else (stderr, stdout)
        assert first.startswith(INFO_LINE + "\r\n")
        assert INFO_LINE not in other
        result = json.loads((stdout if info == "stderr" else stdout.split("\r\n", 1)[1]))
        assert result == {"stdout": "@K\r\n", "stderr": "", "exit_code": 0}

    def test_its_put_moves_into_a_directory_as_btty_s_mv_does(self, remote: str, local: Path) -> None:
        os.mkdir(f"{remote}/d")
        code, _, _ = FakeBtty(remote)(["btty", "--yes", "--put", write(local / "s", b"x"), "--to", f"{remote}/d"])
        assert code == 0
        assert remote_tree(remote) == {"d": DIR, "d/d.btty-tmp": FILE(b"x")}

    def test_its_get_refuses_an_existing_out_file(self, remote: str, local: Path) -> None:
        write(f"{remote}/f", b"remote")
        out = write(local / "out", b"local")
        code, _, stderr = FakeBtty(remote)(["btty", "--yes", "--get", f"{remote}/f", "--out", out])
        assert code == 1 and "already exists" in stderr
        assert Path(out).read_bytes() == b"local"

