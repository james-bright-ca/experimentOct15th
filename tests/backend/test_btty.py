"""BttyBackend: the shared contract in exec mode over the fake btty, the
transfer-only mode, argv shapes, error mapping, chunked deletes, hostile
names, btty's informational line, the default runner, archives (pack, unpack,
merge: timeouts, the names file, a remote python3 without tar filters) and
the listing cache.

Nothing here runs the real btty. A backend gets a FakeBtty
(tests/backend/fake_btty.py), which emulates btty over a directory below
tmp_path, or a ``Recorder`` or ``StubListing``, which run nothing at all.
The default-runner tests patch ``subprocess.run``.

Every test states ``btty_exec`` and ``remote_home`` explicitly, so it holds
whatever ``Settings()`` defaults to (decisions D1/D2), except the PATH-guard
test, which is about ``Settings()`` itself and holds in either mode, and the
registry test, which is about ``Settings()``'s caps.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import tarfile
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.backend.contract import DIR, FILE, LINK, BackendContract, Node, make_archive
from tests.backend.fake_btty import (
    ARCHIVE_KINDS,
    EXEC_FLAGS,
    INFO_LINE,
    FakeBtty,
    run_script,
    script_kind,
    script_names,
    script_paths,
    settings_for,
)
from twinfm.backend import parse_location
from twinfm.backend.base import (
    ALL_CAPS,
    AlreadyExists,
    Backend,
    BackendUnavailable,
    Cap,
    FsError,
    IsADirectory,
    NotADirectory,
    NotFound,
    OperationFailed,
    PermissionDenied,
    Unsupported,
)
from twinfm.backend.btty import TRANSFER_CAPS, BttyBackend, open_btty, run_btty
from twinfm.backend.btty_scripts import (
    COMMAND_BUDGET,
    copy_script,
    delete_scripts,
    list_script,
    merge_script,
    mkdir_script,
    pack_list_script,
    pack_script,
    rename_script,
    stat_script,
    unpack_script,
    walk_script,
)
from twinfm.config import Settings

TIMEOUT = 37
"""A --timeout other than the default, so argv checks prove it is the configured one."""

ARCHIVE_TIMEOUT = 613
"""A btty_archive_timeout other than the default (900) and other than TIMEOUT."""

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


def _settings(remote: str, exec_mode: bool, kw: dict[str, object]) -> Settings:
    values: dict[str, object] = {
        "remote_home": remote,
        "btty_exec": exec_mode,
        "btty_timeout": TIMEOUT,
        "btty_archive_timeout": ARCHIVE_TIMEOUT,
        **kw,
    }
    return Settings(**values)  # type: ignore[arg-type]


def exec_backend(remote: str, runner: Callable[[list[str]], tuple[int, str, str]], **kw: object) -> BttyBackend:
    return BttyBackend(_settings(remote, True, kw), runner=runner)


def transfer_backend(remote: str, runner: Callable[[list[str]], tuple[int, str, str]], **kw: object) -> BttyBackend:
    return BttyBackend(_settings(remote, False, kw), runner=runner)


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
    fake = FakeBtty(remote, timeout=TIMEOUT, archive_timeout=ARCHIVE_TIMEOUT)
    yield fake
    assert fake.rejected == [], "a command over COMMAND_BUDGET was sent to btty"


def write(path: Path | str, data: bytes) -> str:
    Path(path).write_bytes(data)
    return str(path)


def members(path: str) -> dict[str, Node]:
    """The members of the gzip tar at ``path`` (a local file) as contract nodes."""
    result: dict[str, Node] = {}
    with tarfile.open(path, "r:gz") as tar:
        for info in tar.getmembers():
            if info.isdir():
                result[info.name] = DIR
            elif info.issym():
                result[info.name] = LINK(info.linkname)
            else:
                extracted = tar.extractfile(info)
                assert extracted is not None, info.name
                result[info.name] = FILE(extracted.read())
    return result


# -- the shared contract, exec mode -------------------------------------------------------


class _BttyExecContract(BackendContract):
    """BackendContract on BttyBackend in exec mode over the fake; ``info`` is
    the stream btty's informational line arrives on.

    The fake's root holds the contract root (the remote home) and, beside
    it, the remote_tmp directory, so that both lie inside the fake remote.
    Both timeouts differ from the defaults, and the fake checks each call's.
    """

    info: str | None = "stderr"

    @pytest.fixture
    def store(self, tmp_path: Path) -> str:
        """The fake remote's root."""
        store = tmp_path.resolve() / "remote"
        (store / "home").mkdir(parents=True)
        (store / "tmp").mkdir()
        return str(store)

    @pytest.fixture
    def root(self, store: str) -> str:
        return f"{store}/home"

    @pytest.fixture
    def fake(self, store: str) -> Iterator[FakeBtty]:
        fake = FakeBtty(store, timeout=TIMEOUT, archive_timeout=ARCHIVE_TIMEOUT, info=self.info)
        yield fake
        assert fake.rejected == [], "a command over COMMAND_BUDGET was sent to btty"
        assert "?" not in fake.kinds, "the fake was sent an argv it does not know"

    @pytest.fixture
    def backend(self, root: str, store: str, fake: FakeBtty) -> BttyBackend:
        return BttyBackend(settings_for(fake, root, f"{store}/tmp"), runner=fake)

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

    def test_the_archive_dir_is_remote_tmp_inside_the_fake_remote(
        self, backend: BttyBackend, store: str, root: str
    ) -> None:
        assert backend.archive_dir == f"{store}/tmp" == backend.settings.remote_tmp
        assert backend.archive_max_bytes == backend.settings.archive_max_bytes == 268435456
        assert backend.home() == root


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
            "pack",
            "pack-many-names",
            "unpack",
            "merge",
            "merge-into-itself",
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
            "pack": lambda: backend.pack(remote, ["f", "d"], f"{remote}/a.tgz"),
            "pack-many-names": lambda: backend.pack(remote, [f"n{i:04d}" * 8 for i in range(200)], new),
            "unpack": lambda: backend.unpack(f, new),
            "merge": lambda: backend.merge(d, new),
            "merge-into-itself": lambda: backend.merge(d, f"{d}/inside"),
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
            lambda: backend.pack("/top/a\0b", ["x"], "/top/a.tgz"),
            lambda: backend.unpack("/top/a.tgz", "/top/a\0b"),
            lambda: backend.merge("/top/a\0b", "/top/b"),
        ):
            with pytest.raises(OperationFailed):
                call()
        assert runner.calls == []

    @pytest.mark.parametrize(
        "call",
        [
            lambda b, long: b.pack(long, ["a"], "/top/a.tgz"),
            lambda b, long: b.pack("/top", ["a"], long),
            lambda b, long: b.pack(long, [f"name-{i:03d}" for i in range(200)], "/top/a.tgz"),
            lambda b, long: b.unpack(long, "/top/out"),
            lambda b, long: b.unpack("/top/a.tgz", long),
            lambda b, long: b.merge(long, "/top/elsewhere"),
            lambda b, long: b.merge("/top/elsewhere", long),
        ],
        ids=["pack-parent", "pack-archive", "pack-many-names", "unpack-archive", "unpack-dest",
             "merge-src", "merge-dst"],
    )
    def test_an_archive_path_too_long_for_one_command_raises_unsupported(
        self, call: Callable[[BttyBackend, str], object]
    ) -> None:
        # Unsupported, not OperationFailed: the caller then transfers per file.
        runner = Recorder()
        long = "/top/" + "/".join(["x" * 100] * 12)
        with pytest.raises(Unsupported) as err:
            call(exec_backend("/top", runner), long)
        assert "too long" in err.value.message
        assert runner.calls == []

    @pytest.mark.parametrize(
        "name",
        ["", ".", "..", "a/b", "/abs", "trailing/", "nul\0byte"],
        ids=["empty", "dot", "dot-dot", "slash", "absolute", "trailing-slash", "nul"],
    )
    def test_pack_refuses_a_name_that_is_not_a_single_component(self, name: str) -> None:
        runner = Recorder()
        with pytest.raises(OperationFailed):
            exec_backend("/top", runner).pack("/top/src", ["ok", name, "fine"], "/top/a.tgz")
        assert runner.calls == []

    @pytest.mark.parametrize(
        ("src", "dst"),
        [
            ("/top/s", "/top/s"),
            ("/top/s", "/top/s/inside"),
            ("/top/s", "/top/s/deep/er"),
            ("/top/s", "/top/x/../s/inside"),
            ("s", "s/inside"),
            ("/top/s/", "/top/s"),
            ("/", "/top/anything"),
        ],
    )
    def test_merge_into_itself_or_its_subtree_is_refused_before_anything_is_sent(
        self, src: str, dst: str
    ) -> None:
        runner = Recorder()
        with pytest.raises(OperationFailed):
            exec_backend("/top", runner).merge(src, dst)
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


def test_a_btty_location_with_default_settings_has_every_cap_and_transfer_only_has_two() -> None:
    """[D1] parse_location("btty:", Settings()) builds a BttyBackend (with the
    default runner, which construction never calls) in exec mode: every Cap,
    ARCHIVE included. With exec off its caps are {UPLOAD, DOWNLOAD}."""
    backend, path = parse_location("btty:", Settings())
    assert isinstance(backend, BttyBackend)
    assert backend.runner is run_btty
    assert backend.caps == frozenset(Cap) == ALL_CAPS
    assert Cap.ARCHIVE in backend.caps
    assert path == "/mnt/home0/wst/workspaces"
    transfer, path = parse_location("btty:", Settings(btty_exec=False))
    assert isinstance(transfer, BttyBackend)
    assert transfer.caps == frozenset({Cap.UPLOAD, Cap.DOWNLOAD})
    assert transfer is not backend
    assert path == "/mnt/home0/wst/workspaces"


def test_open_btty_shares_one_backend_per_settings() -> None:
    settings = Settings(remote_home="/mnt/t19-shared", btty_exec=True)
    backend = open_btty(settings)
    assert open_btty(Settings(remote_home="/mnt/t19-shared", btty_exec=True)) is backend
    assert open_btty(Settings(remote_home="/mnt/t19-shared", btty_exec=False)) is not backend
    assert open_btty(Settings(remote_home="/mnt/t19-other", btty_exec=True)) is not backend


# -- archives: timeouts, the names file, a remote without tar filters ---------------------------


def packable(remote: str, count: int, folder: str = "src") -> tuple[str, list[str]]:
    """A folder below ``remote`` holding ``count`` files with names that need
    quoting, one unselected file and one subdirectory; (its path, the names)."""
    path = f"{remote}/{folder}"
    os.mkdir(path)
    names = [f"report {i:03d} - it's *final* $(x).txt" for i in range(count)]
    for name in names:
        write(f"{path}/{name}", name.encode())
    write(f"{path}/not selected", b"stays out")
    return path, names


class TestArchiveTimeouts:
    @pytest.mark.parametrize(
        ("timeout", "archive_timeout"),
        [(TIMEOUT, ARCHIVE_TIMEOUT), (None, None)],
        ids=["configured", "defaults-120-and-900"],
    )
    def test_pack_unpack_and_merge_carry_the_archive_timeout_and_every_other_exec_the_btty_timeout(
        self, remote: str, local: Path, timeout: int | None, archive_timeout: int | None
    ) -> None:
        fake = FakeBtty(remote)  # checks no timeout itself: this test reads every argv
        values: dict[str, object] = {"remote_home": remote, "btty_exec": True}
        if timeout is not None:
            values.update(btty_timeout=timeout, btty_archive_timeout=archive_timeout)
        backend = BttyBackend(Settings(**values), runner=fake)  # type: ignore[arg-type]
        expected_exec = str(timeout or 120)
        expected_archive = str(archive_timeout or 900)

        src, names = packable(remote, 150)
        backend.list_dir(src)
        backend.walk(src)
        backend.stat(f"{src}/not selected")
        backend.mkdir(f"{remote}/dest")
        backend.copy_within(f"{src}/not selected", f"{remote}/copied")
        backend.rename(f"{remote}/copied", f"{remote}/renamed")
        backend.upload(write(local / "up", b"up"), f"{remote}/up")
        backend.pack(src, names[:2], f"{remote}/few.tgz")
        backend.pack(src, names, f"{remote}/all.tgz")
        with pytest.raises(NotFound):
            backend.pack(f"{remote}/missing", ["x"], f"{remote}/failed.tgz")
        backend.unpack(f"{remote}/few.tgz", f"{remote}/stage")
        backend.merge(f"{remote}/stage", f"{remote}/dest")
        with pytest.raises(NotFound):
            backend.merge(f"{remote}/stage", f"{remote}/dest")
        with pytest.raises(NotFound):
            backend.unpack(f"{remote}/few.tgz", f"{remote}/stage")
        backend.delete(f"{remote}/renamed")
        backend.delete_many([f"{remote}/up", f"{remote}/all.tgz"])

        assert set(fake.kinds) >= {
            "list", "walk", "stat", "mkdir", "copy", "rename", "delete", "put",
            "pack", "pack-from-file", "unpack", "merge",
        }
        for argv, kind in zip(fake.calls, fake.kinds, strict=True):
            if kind in ("put", "get"):
                assert "--timeout" not in argv, argv
                continue
            body = argv[8].split("\n", 1)[1]
            archive = body.startswith("python3 -I -c ") and shlex.split(body)[4] in ("p", "f", "u", "m")
            assert (kind in ARCHIVE_KINDS) == archive, kind
            assert argv[5:7] == ["--timeout", expected_archive if archive else expected_exec], (kind, argv[:7])
        assert all(len(s.encode("utf-8")) <= COMMAND_BUDGET for s in fake.scripts)
        assert fake.rejected == []


class TestPackNamesFile:
    def test_pack_with_200_names_writes_one_archive_holding_all_of_them(
        self, remote: str, fake: FakeBtty
    ) -> None:
        src, names = packable(remote, 200)
        assert pack_script(src, names, f"{remote}/a.tgz") is None, "the names must not fit one command"
        backend = exec_backend(remote, fake)
        backend.pack(src, names, f"{remote}/a.tgz")

        assert members(f"{remote}/a.tgz") == {name: FILE(name.encode()) for name in names}
        assert sorted(os.listdir(remote)) == ["a.tgz", "src"], "the names file was left behind"
        assert fake.kinds == ["put", "pack-from-file"]
        [put, exec_] = fake.calls
        assert put[5] == f"{remote}/a.tgz.names"
        assert script_paths(exec_[8]) == [f"{remote}/a.tgz", src, f"{remote}/a.tgz.names"]
        assert all(len(s.encode("utf-8")) <= COMMAND_BUDGET for s in fake.scripts)
        assert fake.rejected == []

    def test_names_that_fit_go_inline_in_one_command(self, remote: str, fake: FakeBtty) -> None:
        src, names = packable(remote, 3)
        backend = exec_backend(remote, fake)
        backend.pack(src, names, f"{remote}/a.tgz")
        assert fake.kinds == ["pack"]
        assert script_names(fake.scripts[0]) == names
        assert members(f"{remote}/a.tgz") == {name: FILE(name.encode()) for name in names}

    def test_hostile_names_survive_the_names_file(self, remote: str, fake: FakeBtty) -> None:
        hostile = ["new\nline", "tab\there", " padded ", "back\\slash", "-leading", "ünïcødé-日本語", "$(echo pwned)"]
        src, names = packable(remote, 120)
        for name in hostile:
            write(f"{src}/{name}", name.encode())
        selection = names + hostile
        exec_backend(remote, fake).pack(src, selection, f"{remote}/a.tgz")
        assert fake.kinds == ["put", "pack-from-file"]
        assert members(f"{remote}/a.tgz") == {name: FILE(name.encode()) for name in selection}
        assert sorted(os.listdir(remote)) == ["a.tgz", "src"]

    @pytest.mark.parametrize(
        "failure",
        ["missing-name", "missing-parent", "existing-archive", "parent-is-a-file", "missing-archive-dir"],
    )
    def test_a_failed_pack_from_a_names_file_leaves_no_names_file_and_no_new_archive(
        self, remote: str, fake: FakeBtty, failure: str
    ) -> None:
        src, names = packable(remote, 200)
        archive = f"{remote}/a.tgz"
        parent = src
        expected: type[FsError] = NotFound
        if failure == "missing-name":
            names = [*names[:100], "missing", *names[100:]]
        elif failure == "missing-parent":
            parent = f"{remote}/missing"
        elif failure == "existing-archive":
            write(archive, b"precious")
            expected = AlreadyExists
        elif failure == "parent-is-a-file":
            parent = f"{src}/not selected"
            expected = NotADirectory
        else:
            archive = f"{remote}/missing/a.tgz"
        before = remote_tree(remote)
        with pytest.raises(expected) as err:
            exec_backend(remote, fake).pack(parent, names, archive)
        if failure == "missing-name":
            assert err.value.path == f"{src}/missing"
        elif failure == "existing-archive":
            assert err.value.path == archive
        elif failure != "missing-archive-dir":
            assert err.value.path == parent
        assert remote_tree(remote) == before
        if failure == "missing-archive-dir":
            assert fake.kinds == ["put"]  # the names file had nowhere to go
        else:
            assert fake.kinds[:2] == ["put", "pack-from-file"]

    def test_when_the_pack_command_never_runs_its_names_file_is_deleted(
        self, remote: str, fake: FakeBtty
    ) -> None:
        src, names = packable(remote, 200)
        before = remote_tree(remote)

        def runner(argv: list[str]) -> tuple[int, str, str]:
            if len(argv) == 9 and script_kind(argv[8]) == "pack-from-file":
                fake.respond(1, "", "btty: the connection dropped\n")  # nothing runs
            return fake(argv)

        with pytest.raises(BackendUnavailable):
            exec_backend(remote, runner).pack(src, names, f"{remote}/a.tgz")
        assert fake.kinds == ["put", "pack-from-file", "delete"]
        assert script_paths(fake.calls[2][8]) == [f"{remote}/a.tgz.names"]
        assert remote_tree(remote) == before


class TestArchiveRoundTrips:
    @pytest.mark.parametrize("layout", ["selected-names", "inside-a-selected-folder"])
    def test_hard_linked_files_survive_pack_then_unpack(
        self, remote: str, fake: FakeBtty, layout: str
    ) -> None:
        # tarfile.add stores the second path to an inode as a hard-link
        # member; pnpm stores and git checkouts are full of them.
        src = f"{remote}/src"
        os.mkdir(src)
        if layout == "selected-names":
            write(f"{src}/a", b"shared bytes")
            os.link(f"{src}/a", f"{src}/b")
            names, prefix = ["a", "b"], ""
        else:
            os.makedirs(f"{src}/pkg/sub")
            write(f"{src}/pkg/a", b"shared bytes")
            os.link(f"{src}/pkg/a", f"{src}/pkg/sub/b")
            names, prefix = ["pkg"], "pkg/"
        backend = exec_backend(remote, fake)
        backend.pack(src, names, f"{remote}/a.tgz")
        backend.unpack(f"{remote}/a.tgz", f"{remote}/out")
        out = remote_tree(f"{remote}/out")
        b = f"{prefix}sub/b" if prefix else "b"
        assert out[f"{prefix}a"] == FILE(b"shared bytes")
        assert out[b] == FILE(b"shared bytes")
        assert not os.path.exists(f"{remote}/a.tgz")

    def test_a_large_file_and_many_small_ones_round_trip_byte_for_byte(
        self, remote: str, fake: FakeBtty
    ) -> None:
        src = f"{remote}/src"
        os.makedirs(f"{src}/d")
        big = os.urandom(3 * 1024 * 1024)
        write(f"{src}/d/big.bin", big)
        for i in range(300):
            write(f"{src}/d/small-{i:03d}", f"{i}".encode())
        backend = exec_backend(remote, fake)
        backend.pack(src, ["d"], f"{remote}/a.tgz")
        backend.unpack(f"{remote}/a.tgz", f"{remote}/out")
        assert remote_tree(f"{remote}/out") == remote_tree(src)
        assert fake.count("pack", "unpack") == 2


class TestWithoutTarFilter:
    """A remote python3 older than 3.12 (and 3.11.4) has no tarfile.tar_filter:
    pack and unpack raise Unsupported and change nothing, so that the caller
    transfers per file."""

    @pytest.fixture
    def old(self, remote: str) -> Iterator[FakeBtty]:
        fake = FakeBtty(remote, timeout=TIMEOUT, archive_timeout=ARCHIVE_TIMEOUT, tar_filter=False)
        yield fake
        assert fake.rejected == []

    def test_the_fake_remote_python3_really_lacks_the_filters(self) -> None:
        probe = "python3 -I -c 'import tarfile as T;print(hasattr(T, \"tar_filter\"), hasattr(T, \"data_filter\"))'"
        assert run_script(probe, tar_filter=False) == (0, "False False\n")
        assert run_script(probe) == (0, "True True\n")

    @pytest.mark.parametrize("count", [3, 200], ids=["inline", "names-file"])
    def test_pack_raises_unsupported_and_writes_nothing(
        self, remote: str, old: FakeBtty, count: int
    ) -> None:
        src, names = packable(remote, count)
        before = remote_tree(remote)
        with pytest.raises(Unsupported):
            exec_backend(remote, old).pack(src, names, f"{remote}/a.tgz")
        assert remote_tree(remote) == before
        assert old.count("pack", "pack-from-file") == 1

    def test_unpack_raises_unsupported_creates_no_dest_and_keeps_the_archive(
        self, remote: str, old: FakeBtty
    ) -> None:
        data = make_archive([("d", DIR), ("d/f", FILE(b"1"))])
        write(f"{remote}/a.tgz", data)
        os.mkdir(f"{remote}/parent")
        with pytest.raises(Unsupported):
            exec_backend(remote, old).unpack(f"{remote}/a.tgz", f"{remote}/parent/dest")
        assert remote_tree(remote) == {"a.tgz": FILE(data), "parent": DIR}

    def test_merge_needs_no_tar_filter(self, remote: str, old: FakeBtty) -> None:
        for rel in ("s", "s/sub", "t", "t/sub"):
            os.mkdir(f"{remote}/{rel}")
        write(f"{remote}/s/sub/new", b"n")
        write(f"{remote}/t/sub/old", b"o")
        exec_backend(remote, old).merge(f"{remote}/s", f"{remote}/t")
        assert remote_tree(remote) == {"t": DIR, "t/sub": DIR, "t/sub/new": FILE(b"n"), "t/sub/old": FILE(b"o")}

    def test_the_switch_can_change_between_calls(self, remote: str, old: FakeBtty) -> None:
        src, names = packable(remote, 2)
        backend = exec_backend(remote, old)
        with pytest.raises(Unsupported):
            backend.pack(src, names, f"{remote}/a.tgz")
        old.tar_filter = True
        backend.pack(src, names, f"{remote}/a.tgz")
        assert members(f"{remote}/a.tgz") == {name: FILE(name.encode()) for name in names}


# -- the listing cache ------------------------------------------------------------------------


class StubListing:
    """A runner that runs nothing: every list script gets one file entry named
    ``in-<basename>``; each listed path is recorded. Any other call fails the test."""

    def __init__(self) -> None:
        self.listed: list[str] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        assert len(argv) == 9 and script_kind(argv[8]) == "list", argv[:8]
        [path] = script_paths(argv[8])
        self.listed.append(path)
        name = "in-" + (posixpath.basename(path) or "root")
        record = {
            "name": base64.b64encode(name.encode()).decode(),
            "kind": "file", "size": 1, "mtime": 1.0, "mode": 0o644,
            "link_target": None, "target_kind": None,
        }
        return 0, json_result(f"@J {json.dumps(record)}\r\n@K\r\n"), INFO_LINE + "\r\n"


class DroppedAfter:
    """Runs every call on ``fake``, then, for calls of ``kinds``, reports a
    dropped connection: the remote did the work and the answer was lost."""

    def __init__(self, fake: FakeBtty, kinds: set[str]) -> None:
        self.fake, self.kinds = fake, kinds

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        answer = self.fake(argv)
        if self.fake.kinds[-1] in self.kinds:
            return 1, "", f"{INFO_LINE}\r\nbtty: the connection dropped\n"
        return answer


def listed(backend: BttyBackend, fake: FakeBtty, path: str) -> tuple[set[str] | type[FsError], int]:
    """(the names list_dir(path) gives, or the FsError class it raises; how many btty calls it made)."""
    before = len(fake.calls)
    try:
        result: set[str] | type[FsError] = {e.name for e in backend.list_dir(path)}
    except FsError as e:
        result = type(e)
    return result, len(fake.calls) - before


DISK = None
"""In a Mutation's ``after``: the names now on disk, whatever they are."""


@dataclass
class Mutation:
    """One mutating call and the listings it must drop (base.py's table).

    tree
        What is on the remote first (relative paths).
    act
        The call, given (backend, remote, local directory).
    after
        Each directory whose listing the call must drop, relative to the
        remote ("" for the remote itself), with what list_dir then gives: the
        names, the FsError class it raises, or DISK. Each is listed (and so
        cached) before the call, so it must exist then.
    behind
        Changes made behind the backend's back after the listings are cached
        and before the call, so that a dropped listing differs from the cached one.
    kind
        The fake's kind of the call that changes the remote.
    """

    kind: str
    tree: dict[str, Node]
    act: Callable[[BttyBackend, str, Path], object]
    after: dict[str, set[str] | type[FsError] | None]
    behind: Callable[[str], object] = field(default=lambda remote: None)
    raises: type[FsError] | None = None


ARCHIVE_OF_F = make_archive([("f", FILE(b"from the archive"))])

MUTATIONS: dict[str, Mutation] = {
    "mkdir": Mutation(
        "mkdir", {"a": DIR}, lambda b, r, l: b.mkdir(f"{r}/a/new"), {"a": {"new"}}
    ),
    "delete": Mutation(
        "delete", {"a": DIR, "a/f": FILE(b"1"), "a/g": FILE(b"2")},
        lambda b, r, l: b.delete(f"{r}/a/f"), {"a": {"g"}},
    ),
    "delete-recursive": Mutation(
        "delete",
        {"a": DIR, "a/sub": DIR, "a/sub/deep": DIR, "a/sub/deep/g": FILE(b"g"), "a/keep": FILE(b"k")},
        lambda b, r, l: b.delete(f"{r}/a/sub", recursive=True),
        {"a": {"keep"}, "a/sub": NotFound, "a/sub/deep": NotFound},
    ),
    "delete_many": Mutation(
        "delete",
        {"a": DIR, "a/f": FILE(b"1"), "b": DIR, "b/h": FILE(b"2"), "b/keep": FILE(b"k")},
        lambda b, r, l: b.delete_many([f"{r}/a/f", f"{r}/b/h"]),
        {"a": set(), "b": {"keep"}},
    ),
    "delete_many-recursive": Mutation(
        "delete",
        {"a": DIR, "a/sub": DIR, "a/sub/x": FILE(b"x"), "b": DIR, "b/sub": DIR, "b/sub/y": FILE(b"y")},
        lambda b, r, l: b.delete_many([f"{r}/a/sub", f"{r}/b/sub"], recursive=True),
        {"a": set(), "a/sub": NotFound, "b": set(), "b/sub": NotFound},
    ),
    "delete_many-stopped-by-a-failure": Mutation(
        "delete",
        {"a": DIR, "a/f": FILE(b"1"), "a/g": FILE(b"2")},
        lambda b, r, l: b.delete_many([f"{r}/a/f", f"{r}/a/missing", f"{r}/a/g"]),
        {"a": {"g"}},
        raises=NotFound,
    ),
    "rename": Mutation(
        "rename",
        {"a": DIR, "a/sub": DIR, "a/sub/deep": DIR, "a/sub/deep/g": FILE(b"g"), "b": DIR},
        lambda b, r, l: b.rename(f"{r}/a/sub", f"{r}/b/sub"),
        {"a": set(), "a/sub": NotFound, "a/sub/deep": NotFound, "b": {"sub"}},
    ),
    "rename-onto-a-stale-listing": Mutation(
        "rename",
        {"a": DIR, "a/f": FILE(b"f"), "b": DIR, "b/t": DIR, "b/t/old": FILE(b"o")},
        lambda b, r, l: b.rename(f"{r}/a/f", f"{r}/b/t"),
        {"b/t": NotADirectory},
        behind=lambda r: shutil.rmtree(f"{r}/b/t"),
    ),
    "copy_within": Mutation(
        "copy",
        {"a": DIR, "a/sub": DIR, "a/sub/x": FILE(b"x"), "b": DIR},
        lambda b, r, l: b.copy_within(f"{r}/a/sub", f"{r}/b/copy"),
        {"b": {"copy"}},
    ),
    "copy_within-onto-a-stale-listing": Mutation(
        "copy",
        {"a": DIR, "a/sub": DIR, "a/sub/x": FILE(b"x"), "b": DIR, "b/copy": DIR, "b/copy/old": FILE(b"o")},
        lambda b, r, l: b.copy_within(f"{r}/a/sub", f"{r}/b/copy"),
        {"b/copy": {"x"}},
        behind=lambda r: shutil.rmtree(f"{r}/b/copy"),
    ),
    "upload": Mutation(
        "put", {"a": DIR},
        lambda b, r, l: b.upload(write(l / "up", b"up"), f"{r}/a/up"), {"a": {"up"}},
    ),
    "upload-overwrite": Mutation(
        "put", {"a": DIR, "a/up": FILE(b"old")},
        lambda b, r, l: b.upload(write(l / "up", b"new"), f"{r}/a/up", overwrite=True), {"a": {"up"}},
    ),
    "pack": Mutation(
        "pack", {"a": DIR, "a/f": FILE(b"f"), "b": DIR},
        lambda b, r, l: b.pack(f"{r}/a", ["f"], f"{r}/b/x.tgz"), {"b": {"x.tgz"}},
    ),
    "pack-from-a-names-file": Mutation(
        "pack-from-file",
        {"a": DIR, "b": DIR, **{f"a/{'n' * 40}{i:03d}": FILE(b"n") for i in range(60)}},
        lambda b, r, l: b.pack(f"{r}/a", sorted(os.listdir(f"{r}/a")), f"{r}/b/x.tgz"),
        {"b": {"x.tgz"}},
    ),
    "pack-stopped-by-a-failure": Mutation(
        "pack", {"a": DIR, "a/f": FILE(b"f"), "b": DIR},
        lambda b, r, l: b.pack(f"{r}/a", ["f", "missing"], f"{r}/b/x.tgz"),
        {"b": {"behind"}},
        behind=lambda r: write(f"{r}/b/behind", b"seen only after a fresh listing"),
        raises=NotFound,
    ),
    "unpack": Mutation(
        "unpack", {"a": DIR, "a/x.tgz": FILE(ARCHIVE_OF_F), "b": DIR},
        lambda b, r, l: b.unpack(f"{r}/a/x.tgz", f"{r}/b/out"), {"a": set(), "b": {"out"}},
    ),
    "unpack-onto-a-stale-listing": Mutation(
        "unpack", {"a": DIR, "a/x.tgz": FILE(ARCHIVE_OF_F), "b": DIR, "b/out": DIR, "b/out/old": FILE(b"o")},
        lambda b, r, l: b.unpack(f"{r}/a/x.tgz", f"{r}/b/out"),
        {"b/out": {"f"}},
        behind=lambda r: shutil.rmtree(f"{r}/b/out"),
    ),
    "merge": Mutation(
        "merge",
        {"a": DIR, "a/sub": DIR, "a/sub/deep": DIR, "a/sub/deep/g": FILE(b"g"),
         "b": DIR, "b/sub": DIR, "b/sub/other": FILE(b"o")},
        lambda b, r, l: b.merge(f"{r}/a/sub", f"{r}/b/sub"),
        {"a": set(), "a/sub": NotFound, "a/sub/deep": NotFound, "b": {"sub"}, "b/sub": {"deep", "other"}},
    ),
    "merge-to-a-free-path": Mutation(
        "merge", {"a": DIR, "a/sub": DIR, "a/sub/x": FILE(b"x"), "b": DIR},
        lambda b, r, l: b.merge(f"{r}/a/sub", f"{r}/b/moved"),
        {"a": set(), "a/sub": NotFound, "b": {"moved"}},
    ),
    "merge-stopped-by-a-clash": Mutation(
        "merge",
        {"s": DIR, "s/a": FILE(b"a"), "s/b": FILE(b"b"), "s/clash": DIR, "s/clash/x": FILE(b"x"),
         "t": DIR, "t/clash": FILE(b"old")},
        lambda b, r, l: b.merge(f"{r}/s", f"{r}/t"),
        {"s": DISK, "t": DISK},
        raises=NotADirectory,
    ),
}


class TestListingCache:
    def test_two_listings_of_one_directory_run_btty_once(self, remote: str, fake: FakeBtty) -> None:
        os.mkdir(f"{remote}/sub")
        write(f"{remote}/sub/f", b"1")
        backend = exec_backend(remote, fake)
        first = backend.list_dir(f"{remote}/sub")
        for spelling in (f"{remote}/sub", "sub", f"{remote}/sub/", f"{remote}/./sub", f"{remote}/x/../sub"):
            assert backend.list_dir(spelling) == first, spelling
        assert fake.count("list") == 1
        assert [e.name for e in first] == ["f"]

    def test_stat_walk_and_exists_ask_btty_every_time_and_see_outside_changes(
        self, remote: str, fake: FakeBtty
    ) -> None:
        write(f"{remote}/f", b"1")
        backend = exec_backend(remote, fake)
        assert {e.name for e in backend.list_dir(remote)} == {"f"}
        write(f"{remote}/g", b"made by an editor")
        for _ in range(2):
            assert {e.name for e in backend.walk(remote)} == {"f", "g"}
            assert backend.stat(f"{remote}/g").size == len(b"made by an editor")
            assert backend.exists(f"{remote}/g")
        assert (fake.count("walk"), fake.count("stat")) == (2, 4)
        # The listing stays as cached until something invalidates it.
        assert {e.name for e in backend.list_dir(remote)} == {"f"}
        assert fake.count("list") == 1

    @pytest.mark.parametrize("outcome", ["returned", "dropped-after-the-work"])
    @pytest.mark.parametrize("mutation", MUTATIONS.values(), ids=MUTATIONS.keys())
    def test_each_mutation_drops_the_listings_it_may_have_changed(
        self, remote: str, local: Path, fake: FakeBtty, mutation: Mutation, outcome: str
    ) -> None:
        for rel in sorted(mutation.tree, key=lambda r: (r.count("/"), r)):
            kind, value = mutation.tree[rel]
            path = f"{remote}/{rel}"
            if kind == "dir":
                os.mkdir(path)
            else:
                assert isinstance(value, bytes)
                write(path, value)
        dropped = outcome != "returned"
        runner = DroppedAfter(fake, {mutation.kind}) if dropped else fake
        backend = exec_backend(remote, runner)
        for rel in mutation.after:
            result, calls = listed(backend, fake, f"{remote}/{rel}".rstrip("/"))
            assert isinstance(result, set) and calls == 1, (rel, result)
        mutation.behind(remote)

        raises = BackendUnavailable if dropped else mutation.raises
        if raises is None:
            mutation.act(backend, remote, local)
        else:
            with pytest.raises(raises):
                mutation.act(backend, remote, local)
        assert fake.count(mutation.kind) >= 1

        for rel, expected in mutation.after.items():
            path = f"{remote}/{rel}".rstrip("/")
            result, calls = listed(backend, fake, path)
            assert calls == 1, f"the listing of {rel!r} was not dropped"
            if expected is DISK:
                expected = set(os.listdir(path))
            assert result == expected, rel

    def test_a_listing_still_cached_after_a_mutation_elsewhere_is_served_from_the_cache(
        self, remote: str, fake: FakeBtty
    ) -> None:
        # btty.py: every mutation drops "the listings it may have changed"; the
        # user asked for a cache updated only when something changed. A
        # listing the call cannot have changed survives it.
        os.mkdir(f"{remote}/a")
        os.mkdir(f"{remote}/elsewhere")
        backend = exec_backend(remote, fake)
        backend.list_dir(f"{remote}/elsewhere")
        backend.mkdir(f"{remote}/a/new")
        _, calls = listed(backend, fake, f"{remote}/elsewhere")
        assert calls == 0

    @pytest.mark.parametrize("name", HOSTILE, ids=repr)
    def test_hostile_directory_names_are_cache_keys_like_any_other(
        self, remote: str, fake: FakeBtty, name: str
    ) -> None:
        folder = f"{remote}/{name}"
        os.mkdir(folder)
        os.mkdir(f"{folder}/below")
        backend = exec_backend(remote, fake)
        assert listed(backend, fake, folder) == ({"below"}, 1)
        assert listed(backend, fake, f"{folder}/below") == (set(), 1)
        assert listed(backend, fake, folder) == ({"below"}, 0)
        backend.mkdir(f"{folder}/new")
        assert listed(backend, fake, folder) == ({"below", "new"}, 1)
        write(f"{folder}/below/outside", b"x")
        backend.invalidate(folder)
        assert listed(backend, fake, f"{folder}/below") == ({"outside"}, 1)

    # -- invalidate -------------------------------------------------------------------------

    @staticmethod
    def tree_of_listings(remote: str) -> list[str]:
        dirs = ["d", "d/e", "d/e/f", "db", "d b", "x"]
        for rel in dirs:
            os.mkdir(f"{remote}/{rel}")
        return [remote, *(f"{remote}/{rel}" for rel in dirs)]

    def test_invalidate_of_a_directory_drops_it_and_everything_below_and_keeps_its_parent(
        self, remote: str, fake: FakeBtty
    ) -> None:
        paths = self.tree_of_listings(remote)
        backend = exec_backend(remote, fake)
        for path in paths:
            backend.list_dir(path)
        assert backend.invalidate(f"{remote}/d") is None
        again = {path: listed(backend, fake, path)[1] for path in paths}
        assert again == {
            remote: 0,  # the parent is kept
            f"{remote}/d": 1,
            f"{remote}/d/e": 1,
            f"{remote}/d/e/f": 1,
            f"{remote}/db": 0,  # shares a prefix, but is not below d
            f"{remote}/d b": 0,
            f"{remote}/x": 0,
        }

    @pytest.mark.parametrize("spelling", ["d", "{remote}/d/", "{remote}/x/../d", "{remote}//d"])
    def test_invalidate_normalises_its_path_and_resolves_it_against_the_home(
        self, remote: str, fake: FakeBtty, spelling: str
    ) -> None:
        paths = self.tree_of_listings(remote)
        backend = exec_backend(remote, fake)
        for path in paths:
            backend.list_dir(path)
        backend.invalidate(spelling.format(remote=remote))
        assert listed(backend, fake, f"{remote}/d")[1] == 1
        assert listed(backend, fake, f"{remote}/d/e")[1] == 1
        assert listed(backend, fake, f"{remote}/x")[1] == 0

    @pytest.mark.parametrize("everything", [None, "/"], ids=["no-path", "root"])
    def test_invalidate_without_a_path_or_of_the_root_drops_every_listing(
        self, remote: str, fake: FakeBtty, everything: str | None
    ) -> None:
        paths = self.tree_of_listings(remote)
        backend = exec_backend(remote, fake)
        for path in paths:
            backend.list_dir(path)
        if everything is None:
            backend.invalidate()
        else:
            backend.invalidate(everything)
        assert {path: listed(backend, fake, path)[1] for path in paths} == dict.fromkeys(paths, 1)

    @pytest.mark.parametrize("exec_mode", [True, False], ids=["exec", "transfer-only"])
    def test_invalidate_never_raises_and_never_calls_btty(self, exec_mode: bool) -> None:
        runner = Recorder()
        backend = (exec_backend if exec_mode else transfer_backend)("/top", runner)
        for path in (None, "/", "/top", "/top/a\0b", "", "relative/dir", "..", "/nowhere/at/all"):
            assert backend.invalidate(path) is None, path
        assert backend.invalidate() is None
        assert runner.calls == []

    # -- concurrency ------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "interference",
        ["mkdir-in-it", "delete-in-it", "invalidate-it", "invalidate-all"],
    )
    def test_a_listing_that_overlaps_an_invalidation_is_not_kept(
        self, remote: str, fake: FakeBtty, interference: str
    ) -> None:
        os.mkdir(f"{remote}/d")
        write(f"{remote}/d/old", b"o")
        fetched, release = threading.Event(), threading.Event()

        def runner(argv: list[str]) -> tuple[int, str, str]:
            answer = fake(argv)
            if fake.kinds[-1] == "list" and not fetched.is_set():
                fetched.set()  # the listing has read the remote: hold its answer back
                assert release.wait(30), "the main thread never released the listing"
            return answer

        backend = exec_backend(remote, runner)
        results: list[set[str]] = []
        worker = threading.Thread(target=lambda: results.append({e.name for e in backend.list_dir(f"{remote}/d")}))
        worker.start()
        try:
            assert fetched.wait(30), "the listing never reached btty"
            if interference == "mkdir-in-it":
                backend.mkdir(f"{remote}/d/new")
            elif interference == "delete-in-it":
                backend.delete(f"{remote}/d/old")
            elif interference == "invalidate-it":
                write(f"{remote}/d/new", b"made by an editor")
                backend.invalidate(f"{remote}/d")
            else:
                write(f"{remote}/d/new", b"made by an editor")
                backend.invalidate()
        finally:
            release.set()
            worker.join(30)
        assert not worker.is_alive()
        assert results == [{"old"}]  # it started before the change
        expected = set() if interference == "delete-in-it" else {"old", "new"}
        assert listed(backend, fake, f"{remote}/d") == (expected, 1)

    def test_a_listing_made_while_a_mutation_runs_is_dropped_when_the_mutation_ends(
        self, remote: str, fake: FakeBtty
    ) -> None:
        os.mkdir(f"{remote}/d")
        changed, release = threading.Event(), threading.Event()

        def runner(argv: list[str]) -> tuple[int, str, str]:
            answer = fake(argv)
            if fake.kinds[-1] == "mkdir":
                changed.set()  # the remote has changed; the call has not returned yet
                assert release.wait(30)
            return answer

        backend = exec_backend(remote, runner)
        worker = threading.Thread(target=backend.mkdir, args=(f"{remote}/d/new",))
        worker.start()
        try:
            assert changed.wait(30)
            assert listed(backend, fake, f"{remote}/d") == ({"new"}, 1)
            write(f"{remote}/d/later", b"made by an editor while the mkdir call was out")
        finally:
            release.set()
            worker.join(30)
        assert not worker.is_alive()
        assert listed(backend, fake, f"{remote}/d") == ({"new", "later"}, 1)

    def test_listings_from_many_threads_agree_and_stay_consistent(self, remote: str, fake: FakeBtty) -> None:
        for i in range(4):
            os.mkdir(f"{remote}/d{i}")
            write(f"{remote}/d{i}/f", b"1")
        backend = exec_backend(remote, fake)
        errors: list[BaseException] = []

        def work(i: int) -> None:
            try:
                for _ in range(5):
                    assert {e.name for e in backend.list_dir(f"{remote}/d{i % 4}")} >= {"f"}
                    backend.invalidate(f"{remote}/d{(i + 1) % 4}")
            except BaseException as e:  # noqa: BLE001 - reported below
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == []
        backend.invalidate()
        for i in range(4):
            assert listed(backend, fake, f"{remote}/d{i}") == ({"f"}, 1)

    # -- size and copies --------------------------------------------------------------------

    def test_the_256_most_recently_listed_directories_stay_cached(self) -> None:
        stub = StubListing()
        backend = exec_backend("/top", stub)
        paths = [f"/top/d{i:03d}" for i in range(256)]
        for path in paths:
            backend.list_dir(path)
        for path in reversed(paths):
            assert [e.name for e in backend.list_dir(path)] == ["in-" + posixpath.basename(path)]
        assert stub.listed == paths

    def test_after_257_directories_the_first_one_listed_is_asked_again(self) -> None:
        stub = StubListing()
        backend = exec_backend("/top", stub)
        paths = [f"/top/d{i:03d}" for i in range(257)]
        for path in paths:
            backend.list_dir(path)
        backend.list_dir(paths[0])
        assert stub.listed == [*paths, paths[0]]

    def test_the_least_recently_used_listing_goes_first(self) -> None:
        stub = StubListing()
        backend = exec_backend("/top", stub)
        paths = [f"/top/d{i:03d}" for i in range(256)]
        for path in paths:
            backend.list_dir(path)
        backend.list_dir(paths[0])  # a hit: now the most recently used
        backend.list_dir("/top/one-more")  # evicts paths[1], the least recently used
        backend.list_dir(paths[0])
        assert stub.listed == [*paths, "/top/one-more"]
        backend.list_dir(paths[1])
        assert stub.listed == [*paths, "/top/one-more", paths[1]]

    def test_changing_a_returned_list_changes_nothing_cached(self, remote: str, fake: FakeBtty) -> None:
        write(f"{remote}/a", b"1")
        write(f"{remote}/b", b"2")
        backend = exec_backend(remote, fake)
        first = backend.list_dir(remote)
        names = sorted(e.name for e in first)
        first.clear()
        second = backend.list_dir(remote)
        assert sorted(e.name for e in second) == names
        second.append(second[0])
        second.reverse()
        third = backend.list_dir(remote)
        assert sorted(e.name for e in third) == names
        assert third is not second
        assert fake.count("list") == 1

    def test_a_failed_listing_is_not_cached(self, remote: str, fake: FakeBtty) -> None:
        backend = exec_backend(remote, fake)
        with pytest.raises(NotFound):
            backend.list_dir(f"{remote}/later")
        os.mkdir(f"{remote}/later")
        assert listed(backend, fake, f"{remote}/later") == (set(), 1)


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

    def test_it_knows_the_kind_and_the_paths_of_every_script(self) -> None:
        a, b = "/r/it's a\npath", "/r/-other *"
        scripts = {
            "list": (list_script(a), [a]),
            "walk": (walk_script(a), [a]),
            "stat": (stat_script(a), [a]),
            "mkdir": (mkdir_script(a), [a]),
            "delete": (delete_scripts([a, b], recursive=True)[0][1], [a, b]),
            "rename": (rename_script(a, b), [a, b]),
            "copy": (copy_script(a, b, overwrite=True), [a, b]),
            "pack": (pack_script(a, ["x y", "z"], b), [b, a]),
            "pack-from-file": (pack_list_script(a, b + ".names", b), [b, a, b + ".names"]),
            "unpack": (unpack_script(a, b), [a, b]),
            "merge": (merge_script(a, b), [a, b]),
        }
        for kind, (script, paths) in scripts.items():
            assert script is not None
            assert script_kind(script) == kind
            assert script_paths(script) == paths, kind
        assert script_names(pack_script(a, ["x y", "z"], b) or "") == ["x y", "z"]
        assert script_names(list_script(a)) == []

    def test_it_checks_the_archive_timeout_for_archive_scripts_and_the_other_for_the_rest(
        self, remote: str
    ) -> None:
        fake = FakeBtty(remote, timeout=TIMEOUT, archive_timeout=ARCHIVE_TIMEOUT)
        merge = merge_script(f"{remote}/a", f"{remote}/b")
        listing = list_script(remote)

        def argv(script: str, timeout: int) -> list[str]:
            return ["btty", *EXEC_FLAGS, str(timeout), "--exec", script]

        with pytest.raises(AssertionError, match="merge --timeout 37 != archive timeout 613"):
            fake(argv(merge, TIMEOUT))
        with pytest.raises(AssertionError, match="--timeout 613 != 37"):
            fake(argv(listing, ARCHIVE_TIMEOUT))
        assert fake(argv(listing, TIMEOUT))[0] == 0
        code, stdout, _ = fake(argv(merge, ARCHIVE_TIMEOUT))
        assert "@X ENOENT" in stdout and code == 0
        assert fake.kinds == ["?", "?", "list", "merge"]
        assert fake.count("list", "merge") == 2
        assert fake.calls_of("merge") == [argv(merge, ARCHIVE_TIMEOUT)]

    @pytest.mark.parametrize(
        "script",
        [
            lambda r, out: pack_script(r, ["x"], out),
            lambda r, out: pack_script(out, ["x"], f"{r}/a.tgz"),
            lambda r, out: pack_list_script(r, out, f"{r}/a.tgz"),
            lambda r, out: unpack_script(f"{r}/a.tgz", out),
            lambda r, out: unpack_script(out, f"{r}/dest"),
            lambda r, out: merge_script(f"{r}/s", out),
            lambda r, out: merge_script(out, f"{r}/t"),
        ],
        ids=["pack-archive", "pack-parent", "pack-names-file", "unpack-dest", "unpack-archive",
             "merge-dst", "merge-src"],
    )
    def test_it_refuses_an_archive_path_outside_its_root_before_running_anything(
        self, remote: str, tmp_path: Path, script: Callable[[str, str], str | None]
    ) -> None:
        outside = tmp_path.resolve() / "outside"
        outside.mkdir()
        write(outside / "a.tgz", make_archive([("x", FILE(b"x"))]))
        for rel in ("s", "t"):
            os.mkdir(f"{remote}/{rel}")
        write(f"{remote}/x", b"x")
        write(f"{remote}/a.tgz", make_archive([("x", FILE(b"x"))]))
        before_outside, before_remote = remote_tree(str(outside)), remote_tree(remote)
        text = script(remote, f"{outside}/a.tgz")
        assert text is not None
        fake = FakeBtty(remote)
        with pytest.raises(AssertionError, match="outside the fake remote"):
            fake(exec_argv(text))
        assert remote_tree(str(outside)) == before_outside
        assert remote_tree(remote) == before_remote

    @pytest.mark.parametrize("name", ["../escaped", "/abs", "a/b", "..", "."])
    def test_it_refuses_a_pack_name_that_could_leave_the_parent(
        self, remote: str, local: Path, name: str
    ) -> None:
        os.mkdir(f"{remote}/src")
        fake = FakeBtty(remote)
        inline = pack_script(f"{remote}/src", ["ok", name], f"{remote}/a.tgz")
        assert inline is not None
        with pytest.raises(AssertionError, match="single path component"):
            fake(exec_argv(inline))
        write(f"{remote}/a.tgz.names", b"ok\0" + name.encode() + b"\0")
        listed_names = pack_list_script(f"{remote}/src", f"{remote}/a.tgz.names", f"{remote}/a.tgz")
        with pytest.raises(AssertionError, match="single path component"):
            fake(exec_argv(listed_names))
        assert remote_tree(remote) == {"src": DIR, "a.tgz.names": FILE(b"ok\0" + name.encode() + b"\0")}

    def test_settings_for_matches_the_fake_and_keeps_both_directories_inside_it(
        self, remote: str, tmp_path: Path
    ) -> None:
        fake = FakeBtty(remote, command="btty-x", timeout=TIMEOUT, archive_timeout=ARCHIVE_TIMEOUT)
        settings = settings_for(fake, f"{remote}/home", f"{remote}/tmp")
        assert settings == Settings(
            remote_home=f"{remote}/home",
            remote_tmp=f"{remote}/tmp",
            btty_command="btty-x",
            btty_exec=True,
            btty_timeout=TIMEOUT,
            btty_archive_timeout=ARCHIVE_TIMEOUT,
        )
        unchecked = settings_for(FakeBtty(remote), remote, remote, archive_max_bytes=10)
        assert (unchecked.btty_timeout, unchecked.btty_archive_timeout) == (120, 900)
        assert unchecked.archive_max_bytes == 10
        with pytest.raises(AssertionError, match="outside the fake remote"):
            settings_for(fake, remote, "/tmp")
        with pytest.raises(AssertionError, match="outside the fake remote"):
            settings_for(fake, str(tmp_path.resolve()), f"{remote}/tmp")

