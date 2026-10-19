"""Settings: defaults, the TOML config file, its location and command-line overrides.

The last section proves the conftest btty guard. ``Settings.btty_command``
defaults to the bare name ``btty``, and the guard is what makes that default
safe to construct and pass around under test.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import textwrap
from pathlib import Path

import pytest

import twinfm.config
from twinfm.config import (
    CONFIG_ENV,
    DEFAULT_REMOTE_HOME,
    ConfigError,
    Settings,
    config_path,
    load_settings,
)

# User decision D2: the default remote folder is the workspaces root, the parent of the
# user's own bscp directory (/mnt/home0/wst/workspaces/jake.blow.clearwateranalytics.com).
WORKSPACES_HOME = "/mnt/home0/wst/workspaces"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test from the user's real config: a fresh HOME, no $TWINFM_CONFIG."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    return home


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


# -- defaults ------------------------------------------------------------------------------


def test_without_a_config_file_the_defaults_are_the_workspaces_root_and_exec_mode() -> None:
    """[D1/D2] No config file and no flags: exec mode on, the workspaces root as home."""
    settings = load_settings()
    assert (settings.remote_home, settings.btty_exec) == (WORKSPACES_HOME, True)
    assert DEFAULT_REMOTE_HOME == WORKSPACES_HOME
    assert settings.btty_command == "btty"
    assert settings.btty_timeout == 120
    assert settings == Settings()


def test_an_explicit_path_that_does_not_exist_means_defaults(home: Path, tmp_path: Path) -> None:
    write(home / ".config/twinfm/config.toml", '[btty]\nexec = false\nhome = "/elsewhere"\n')
    assert load_settings(tmp_path / "missing.toml") == Settings()


# -- the config file ------------------------------------------------------------------------


def test_btty_table_turns_exec_off_and_sets_home(tmp_path: Path) -> None:
    """[D1] exec = false opts out of exec mode; [D2] home overrides the default, normalised."""
    path = write(tmp_path / "c.toml", '[btty]\nexec = false\nhome = "/srv/x/"\n')
    settings = load_settings(path)
    assert settings.btty_exec is False
    assert settings.remote_home == "/srv/x"
    assert (settings.btty_command, settings.btty_timeout) == ("btty", 120)


def test_btty_table_with_exec_true_spells_out_the_default(tmp_path: Path) -> None:
    """[D1] exec = true is what a file without exec already gives."""
    path = write(tmp_path / "c.toml", "[btty]\nexec = true\n")
    settings = load_settings(path)
    assert settings.btty_exec is True
    assert settings == Settings()


def test_every_btty_key_is_read(tmp_path: Path) -> None:
    path = write(
        tmp_path / "c.toml",
        """
        [btty]
        home = "/srv/work/"
        command = "/opt/bin/btty"
        exec = false
        timeout = 300
        """,
    )
    assert load_settings(str(path)) == Settings(
        remote_home="/srv/work", btty_command="/opt/bin/btty", btty_exec=False, btty_timeout=300
    )


def test_an_empty_file_and_other_tables_mean_defaults(tmp_path: Path) -> None:
    assert load_settings(write(tmp_path / "empty.toml", "")) == Settings()
    other = write(tmp_path / "other.toml", "[ui]\ntheme = 'dark'\n[[plugins]]\nname = 'x'\n")
    assert load_settings(other) == Settings()


@pytest.mark.parametrize(
    ("home_value", "expected"),
    [("/x/y/", "/x/y"), ("//x//y", "/x/y"), ("/x/./y/../z", "/x/z"), ("/", "/")],
)
def test_remote_home_is_normalised_without_a_trailing_slash(
    tmp_path: Path, home_value: str, expected: str
) -> None:
    path = write(tmp_path / "c.toml", f'[btty]\nhome = "{home_value}"\n')
    assert load_settings(path).remote_home == expected
    assert Settings(remote_home=home_value).remote_home == expected


@pytest.mark.parametrize(
    "text",
    [
        "[btty\nexec = true\n",
        "[btty]\nexec = yes\n",
        "[btty]\nexec = true\nexec = false\n",
        "this is not toml",
    ],
    ids=["unclosed-table", "bare-word", "duplicate-key", "prose"],
)
def test_a_malformed_file_raises_an_error_naming_the_file(tmp_path: Path, text: str) -> None:
    path = write(tmp_path / "broken.toml", text)
    with pytest.raises(ConfigError) as err:
        load_settings(path)
    assert str(path) in str(err.value)
    assert err.value.path == str(path)
    assert isinstance(err.value, ValueError)


def test_a_file_that_is_not_utf8_raises_an_error_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "latin1.toml"
    path.write_bytes(b'[btty]\nhome = "/caf\xe9"\n')
    with pytest.raises(ConfigError, match=str(path)):
        load_settings(path)


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ('exec = "yes"', "exec"),
        ("exec = 1", "exec"),
        ("timeout = 0", "timeout"),
        ("timeout = -5", "timeout"),
        ("timeout = true", "timeout"),
        ("timeout = 1.5", "timeout"),
        ('timeout = "120"', "timeout"),
        ('home = "relative/path"', "home"),
        ('home = ""', "home"),
        ("home = 5", "home"),
        ('command = ""', "command"),
        ('command = "   "', "command"),
        ("command = ['btty']", "command"),
    ],
)
def test_a_value_of_the_wrong_type_raises_an_error_naming_the_file_and_key(
    tmp_path: Path, line: str, field: str
) -> None:
    path = write(tmp_path / "c.toml", f"[btty]\n{line}\n")
    with pytest.raises(ConfigError) as err:
        load_settings(path)
    assert str(path) in str(err.value)
    assert field in err.value.reason


def test_an_unknown_btty_key_raises_an_error_naming_the_file_and_key(tmp_path: Path) -> None:
    path = write(tmp_path / "c.toml", "[btty]\nexecute = true\n")
    with pytest.raises(ConfigError) as err:
        load_settings(path)
    assert str(path) in str(err.value)
    assert "execute" in err.value.reason


@pytest.mark.parametrize("text", ["btty = 1\n", "btty = 'on'\n", "btty = [1, 2]\n"])
def test_btty_that_is_not_a_table_raises_an_error_naming_the_file(
    tmp_path: Path, text: str
) -> None:
    path = write(tmp_path / "c.toml", text)
    with pytest.raises(ConfigError, match=str(path)):
        load_settings(path)


def test_a_config_path_that_is_a_directory_raises_an_error_naming_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=str(tmp_path)):
        load_settings(tmp_path)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="permission bits do not bind root"
)
def test_an_unreadable_file_raises_an_error_naming_it(tmp_path: Path) -> None:
    path = write(tmp_path / "c.toml", "[btty]\nexec = true\n")
    path.chmod(0)
    try:
        with pytest.raises(ConfigError, match=str(path)):
            load_settings(path)
    finally:
        path.chmod(0o600)


# -- which file is read ----------------------------------------------------------------------


def test_the_default_location_is_under_the_home_directory(home: Path) -> None:
    write(home / ".config/twinfm/config.toml", "[btty]\ntimeout = 7\n")
    assert config_path() == str(home / ".config/twinfm/config.toml")
    assert load_settings().btty_timeout == 7


def test_the_environment_variable_beats_the_default_location(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(home / ".config/twinfm/config.toml", "[btty]\ntimeout = 7\n")
    env_file = write(tmp_path / "env.toml", "[btty]\ntimeout = 8\n")
    monkeypatch.setenv(CONFIG_ENV, str(env_file))
    assert config_path() == str(env_file)
    assert load_settings().btty_timeout == 8


def test_an_empty_environment_variable_is_ignored(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(home / ".config/twinfm/config.toml", "[btty]\ntimeout = 7\n")
    monkeypatch.setenv(CONFIG_ENV, "")
    assert load_settings().btty_timeout == 7


def test_an_explicit_path_beats_the_environment_variable(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(home / ".config/twinfm/config.toml", "[btty]\ntimeout = 7\n")
    monkeypatch.setenv(CONFIG_ENV, str(write(tmp_path / "env.toml", "[btty]\ntimeout = 8\n")))
    explicit = write(tmp_path / "explicit.toml", "[btty]\ntimeout = 9\n")
    assert config_path(explicit) == str(explicit)
    assert load_settings(explicit).btty_timeout == 9


def test_a_malformed_file_found_through_the_environment_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = write(tmp_path / "env.toml", "[btty\n")
    monkeypatch.setenv(CONFIG_ENV, str(env_file))
    with pytest.raises(ConfigError, match=str(env_file)):
        load_settings()


# -- overrides (command-line flags) ----------------------------------------------------------


def test_overrides_win_over_the_file_and_none_means_not_given(tmp_path: Path) -> None:
    path = write(tmp_path / "c.toml", '[btty]\nexec = false\nhome = "/from/file"\ntimeout = 30\n')
    settings = load_settings(
        path, overrides={"btty_exec": True, "remote_home": None, "btty_timeout": None}
    )
    assert settings.btty_exec is True
    assert settings.remote_home == "/from/file"
    assert settings.btty_timeout == 30


@pytest.mark.parametrize(
    ("file_exec", "override", "expected"),
    [("true", False, False), ("false", True, True), ("false", None, False), ("true", None, True)],
    ids=["no-btty-exec-beats-file-true", "btty-exec-beats-file-false", "file-false", "file-true"],
)
def test_the_exec_override_beats_the_file_in_both_directions(
    tmp_path: Path, file_exec: str, override: bool | None, expected: bool
) -> None:
    """[D1] --no-btty-exec beats exec = true, --btty-exec beats exec = false, and no flag
    (None) leaves the file's value standing rather than the default."""
    path = write(tmp_path / "c.toml", f"[btty]\nexec = {file_exec}\n")
    assert load_settings(path, overrides={"btty_exec": override}).btty_exec is expected


def test_the_remote_home_override_beats_the_file(tmp_path: Path) -> None:
    """[D2] --remote-home /cli/home wins even when the file sets home."""
    path = write(tmp_path / "c.toml", '[btty]\nhome = "/from/file"\n')
    settings = load_settings(path, overrides={"remote_home": "/cli/home", "btty_exec": None})
    assert settings.remote_home == "/cli/home"


def test_overrides_apply_without_a_config_file() -> None:
    settings = load_settings(overrides={"remote_home": "/cli/home/", "btty_exec": False})
    assert (settings.remote_home, settings.btty_exec) == ("/cli/home", False)


def test_an_unknown_override_raises_value_error_naming_it() -> None:
    with pytest.raises(ValueError, match="btty_exe"):
        load_settings(overrides={"btty_exe": True})


@pytest.mark.parametrize(
    ("name", "value"),
    [("btty_timeout", 0), ("btty_exec", "yes"), ("remote_home", "rel"), ("btty_command", "")],
)
def test_an_invalid_override_raises_value_error_naming_the_field(name: str, value: object) -> None:
    with pytest.raises(ValueError, match=name):
        load_settings(overrides={name: value})


# -- Settings itself ---------------------------------------------------------------------------


def test_settings_are_frozen_and_hashable() -> None:
    settings = Settings(btty_exec=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.btty_exec = True  # type: ignore[misc]
    assert hash(settings) == hash(Settings(btty_exec=False))
    assert len({settings, Settings(btty_exec=False), Settings()}) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("btty_timeout", 0),
        ("btty_timeout", True),
        ("btty_timeout", 2.5),
        ("btty_exec", 1),
        ("remote_home", "relative"),
        ("remote_home", None),
        ("btty_command", " "),
    ],
)
def test_invalid_settings_raise_value_error_naming_the_field(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        Settings(**{field: value})  # type: ignore[arg-type]


# -- the archive settings ([W2]: tmp, archive_timeout, archive_max_bytes) -----------------------


def test_the_archive_settings_default_to_tmp_900_seconds_and_256_mib() -> None:
    for settings in (Settings(), load_settings()):
        assert settings.remote_tmp == "/tmp"
        assert settings.btty_archive_timeout == 900
        assert settings.archive_max_bytes == 268435456 == 256 * 1024 * 1024


def test_the_archive_keys_are_read_from_the_btty_table(tmp_path: Path) -> None:
    path = write(
        tmp_path / "c.toml",
        """
        [btty]
        tmp = "/var/tmp/twinfm/"
        archive_timeout = 30
        archive_max_bytes = 1000
        """,
    )
    settings = load_settings(path)
    assert (settings.remote_tmp, settings.btty_archive_timeout, settings.archive_max_bytes) == (
        "/var/tmp/twinfm",
        30,
        1000,
    )
    # The other keys keep their defaults, and the archive keys do not touch them.
    assert (settings.remote_home, settings.btty_exec, settings.btty_timeout) == (WORKSPACES_HOME, True, 120)
    assert settings == Settings(remote_tmp="/var/tmp/twinfm", btty_archive_timeout=30, archive_max_bytes=1000)


def test_every_key_of_the_btty_table_together(tmp_path: Path) -> None:
    path = write(
        tmp_path / "c.toml",
        """
        [btty]
        home = "/srv/work/"
        command = "/opt/bin/btty"
        exec = false
        timeout = 300
        tmp = "/scratch/"
        archive_timeout = 1800
        archive_max_bytes = 1073741824
        """,
    )
    assert load_settings(path) == Settings(
        remote_home="/srv/work",
        btty_command="/opt/bin/btty",
        btty_exec=False,
        btty_timeout=300,
        remote_tmp="/scratch",
        btty_archive_timeout=1800,
        archive_max_bytes=1073741824,
    )


@pytest.mark.parametrize(
    ("tmp_value", "expected"),
    [("/tmp/", "/tmp"), ("//x//y", "/x/y"), ("/x/./y/../z", "/x/z"), ("/", "/")],
)
def test_remote_tmp_is_normalised_without_a_trailing_slash(
    tmp_path: Path, tmp_value: str, expected: str
) -> None:
    path = write(tmp_path / "c.toml", f'[btty]\ntmp = "{tmp_value}"\n')
    assert load_settings(path).remote_tmp == expected
    assert Settings(remote_tmp=tmp_value).remote_tmp == expected
    assert load_settings(overrides={"remote_tmp": tmp_value}).remote_tmp == expected


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ('tmp = "relative/tmp"', "tmp"),
        ('tmp = ""', "tmp"),
        ("tmp = 5", "tmp"),
        ("tmp = ['/tmp']", "tmp"),
        ("archive_timeout = 0", "archive_timeout"),
        ("archive_timeout = -1", "archive_timeout"),
        ("archive_timeout = true", "archive_timeout"),
        ("archive_timeout = 1.5", "archive_timeout"),
        ('archive_timeout = "900"', "archive_timeout"),
        ("archive_max_bytes = 0", "archive_max_bytes"),
        ("archive_max_bytes = -268435456", "archive_max_bytes"),
        ("archive_max_bytes = true", "archive_max_bytes"),
        ("archive_max_bytes = 2.5e8", "archive_max_bytes"),
        ('archive_max_bytes = "256MiB"', "archive_max_bytes"),
    ],
)
def test_an_invalid_archive_value_raises_an_error_naming_the_file_and_key(
    tmp_path: Path, line: str, key: str
) -> None:
    path = write(tmp_path / "c.toml", f"[btty]\n{line}\n")
    with pytest.raises(ConfigError) as err:
        load_settings(path)
    assert err.value.path == str(path)
    assert str(path) in str(err.value)
    assert re.search(rf"\b{key}\b", err.value.reason), err.value.reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("remote_tmp", "relative"),
        ("remote_tmp", None),
        ("remote_tmp", ""),
        ("btty_archive_timeout", 0),
        ("btty_archive_timeout", True),
        ("btty_archive_timeout", 2.5),
        ("archive_max_bytes", 0),
        ("archive_max_bytes", -1),
        ("archive_max_bytes", True),
        ("archive_max_bytes", 1.0),
    ],
)
def test_invalid_archive_settings_raise_value_error_naming_the_field(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        Settings(**{field: value})  # type: ignore[arg-type]
    if value is not None:  # an override of None means "not given"
        with pytest.raises(ValueError, match=field):
            load_settings(overrides={field: value})


def test_archive_overrides_win_over_the_file_and_none_means_not_given(tmp_path: Path) -> None:
    path = write(
        tmp_path / "c.toml", '[btty]\ntmp = "/from/file"\narchive_timeout = 30\narchive_max_bytes = 1000\n'
    )
    settings = load_settings(
        path,
        overrides={"remote_tmp": "/cli/tmp/", "btty_archive_timeout": None, "archive_max_bytes": 5},
    )
    assert (settings.remote_tmp, settings.btty_archive_timeout, settings.archive_max_bytes) == (
        "/cli/tmp",
        30,
        5,
    )


def test_settings_with_the_archive_fields_stay_frozen_and_hashable() -> None:
    settings = Settings(remote_tmp="/x/", btty_archive_timeout=5, archive_max_bytes=7)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.remote_tmp = "/y"  # type: ignore[misc]
    same = Settings(remote_tmp="/x", btty_archive_timeout=5, archive_max_bytes=7)
    assert settings == same and hash(settings) == hash(same)
    variants = {
        Settings(),
        settings,
        same,
        Settings(remote_tmp="/y"),
        Settings(btty_archive_timeout=6),
        Settings(archive_max_bytes=8),
    }
    assert len(variants) == 5


# -- the documented defaults (D1, D2) ------------------------------------------------------------


def _sample_btty_table(doc: str) -> str:
    """The indented ``[btty]`` sample table from the config module docstring."""
    lines = doc.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "[btty]")
    block = []
    for line in lines[start:]:
        if not line.strip():
            break
        block.append(line)
    return textwrap.dedent("\n".join(block)) + "\n"


def test_the_config_docstrings_no_longer_call_exec_an_opt_in() -> None:
    """[D1/D2] Exec mode is the default; transfer-only is the opt-out."""
    for name, doc in [("module", twinfm.config.__doc__), ("Settings", Settings.__doc__)]:
        assert doc is not None
        assert "opt-in" not in doc.lower(), f"the {name} docstring still calls exec an opt-in"


def test_the_sample_btty_table_in_the_module_docstring_shows_the_defaults(tmp_path: Path) -> None:
    """[D1/D2] The sample [btty] table is a valid config that spells out the defaults:
    exec mode on and home the workspaces root."""
    assert twinfm.config.__doc__ is not None
    sample = _sample_btty_table(twinfm.config.__doc__)
    settings = load_settings(write(tmp_path / "sample.toml", sample))
    assert settings.remote_home == WORKSPACES_HOME
    assert settings.btty_exec is True
    assert settings == Settings()


def test_the_settings_docstring_shows_the_default_home() -> None:
    """[D2] The Settings docstring names /mnt/home0/wst/workspaces itself, not a folder in it."""
    assert Settings.__doc__ is not None
    assert re.search(re.escape(WORKSPACES_HOME) + r"(?![\w/])", Settings.__doc__), Settings.__doc__


# -- the btty guard (tests/conftest.py) ----------------------------------------------------------


GUARD_CONFTEST = Path(__file__).with_name("conftest.py")


def test_the_configured_btty_command_resolves_to_the_guard_shim(btty_guard: object) -> None:
    shim = shutil.which(Settings().btty_command)
    assert shim == str(btty_guard.shim)  # type: ignore[attr-defined]
    assert os.environ["PATH"].split(os.pathsep)[0] == str(btty_guard.bin_dir)  # type: ignore[attr-defined]


def test_running_btty_in_a_test_fails_that_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[A] A test that executes btty through subprocess fails because of the guard,
    even when the code under test swallows the error; a decoy standing in for the
    real btty earlier on PATH is never reached."""
    pytester.makeconftest(GUARD_CONFTEST.read_text())
    decoy_dir = pytester.mkdir("decoy-bin")
    marker = pytester.path / "decoy-ran"
    decoy = decoy_dir / "btty"
    decoy.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    decoy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{decoy_dir}{os.pathsep}{os.environ['PATH']}")
    pytester.makepyfile(
        test_inner="""
        import os
        import shutil
        import subprocess

        import pytest

        from twinfm.config import Settings


        def default_runner(argv):
            # The shape of BttyBackend's default runner: argv in, stdin closed.
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            return done.returncode, done.stdout, done.stderr


        def test_argv_through_the_default_runner_shape():
            code, out, err = default_runner(
                [Settings().btty_command, "--yes", "--put", "a.txt", "--to", "/r/a.txt"]
            )
            assert code == 97
            assert "btty test guard" in err


        def test_shell_string():
            subprocess.run("btty --version", shell=True, capture_output=True)


        def test_error_swallowed_by_the_code_under_test():
            with pytest.raises(subprocess.CalledProcessError):
                subprocess.run(["btty", "--exec", "true"], check=True, capture_output=True)


        def test_os_system():
            os.system("btty --help >/dev/null 2>&1")


        @pytest.fixture
        def runs_btty_in_setup():
            subprocess.run(["btty", "--yes"], capture_output=True)


        def test_btty_in_a_fixture(runs_btty_in_setup):
            pass


        def test_which_finds_the_shim_not_the_decoy():
            assert "decoy-bin" not in shutil.which("btty")


        def test_that_never_runs_btty():
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rA", "-vv")
    result.assert_outcomes(passed=2, failed=4, errors=1)
    result.stdout.fnmatch_lines_random(
        [
            "FAILED test_inner.py::test_argv_through_the_default_runner_shape - *btty test guard*",
            "FAILED test_inner.py::test_shell_string - *btty test guard*",
            "FAILED test_inner.py::test_error_swallowed_by_the_code_under_test - *btty test guard*",
            "FAILED test_inner.py::test_os_system - *btty test guard*",
            "ERROR test_inner.py::test_btty_in_a_fixture - *btty test guard*during setup*",
            "PASSED test_inner.py::test_which_finds_the_shim_not_the_decoy",
            "PASSED test_inner.py::test_that_never_runs_btty",
            "btty test guard: 5 btty execution(s) blocked",
        ]
    )
    assert not marker.exists(), "the decoy btty ran: the guard did not come first on PATH"


def test_btty_run_outside_any_test_fails_the_session(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(GUARD_CONFTEST.read_text())
    pytester.makepyfile(
        test_import_time="""
        import subprocess

        subprocess.run(["btty", "--yes", "--get", "/r/a", "--out", "-"], capture_output=True)


        def test_passes():
            pass
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*btty test guard: btty ran outside any test*", "*arg: --get*"])


def test_a_clean_session_reports_zero_blocked_executions(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(GUARD_CONFTEST.read_text())
    pytester.makepyfile(test_clean="def test_ok():\n    pass\n")
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["btty test guard: 0 btty execution(s) blocked"])
