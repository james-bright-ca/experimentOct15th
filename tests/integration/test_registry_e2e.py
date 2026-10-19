"""End to end, the location spec ``btty:`` from settings to backend:
parse_location returns a BttyBackend at the configured remote home, and two
calls with the same Settings (also equal Settings built twice from one
config file) return the same backend object, so both panels share one store
and one listing cache. Nothing here runs btty: building a backend does no
I/O, and no method that would call the runner is used."""

from __future__ import annotations

from pathlib import Path

from twinfm.backend import parse_location
from twinfm.backend.btty import BttyBackend
from twinfm.config import DEFAULT_REMOTE_HOME, Settings, load_settings


def test_btty_spec_is_a_btty_backend_at_the_configured_remote_home_shared_between_calls() -> None:
    settings = Settings(remote_home="/srv/e2e/registry-home", btty_exec=True)
    first, path = parse_location("btty:", settings)
    second, again = parse_location("btty:", settings)

    assert isinstance(first, BttyBackend)
    assert path == again == "/srv/e2e/registry-home"
    assert first.home() == "/srv/e2e/registry-home"
    assert second is first, "one Settings, one backend object"

    below, sub = parse_location("btty:project/src", settings)
    assert below is first
    assert sub == "/srv/e2e/registry-home/project/src"


def test_settings_loaded_twice_from_one_config_file_give_the_same_backend(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[btty]\nhome = "/srv/e2e/from-file/"\n')

    one = load_settings(config)
    two = load_settings(config)
    assert one == two and one is not two
    first, path = parse_location("btty:", one)
    second, _ = parse_location("btty:", two)

    assert path == "/srv/e2e/from-file"
    assert second is first
    assert first.settings.btty_exec is True, "exec mode is the default"


def test_different_settings_give_different_backends() -> None:
    exec_backend, _ = parse_location("btty:", Settings(remote_home="/srv/e2e/split"))
    transfer_backend, _ = parse_location("btty:", Settings(remote_home="/srv/e2e/split", btty_exec=False))
    assert exec_backend is not transfer_backend


def test_the_default_settings_open_btty_at_the_workspaces_folder() -> None:
    backend, path = parse_location("btty:", Settings())
    assert isinstance(backend, BttyBackend)
    assert path == DEFAULT_REMOTE_HOME == "/mnt/home0/wst/workspaces"
    user, user_path = parse_location("btty:jake.blow.clearwateranalytics.com", Settings())
    assert user is backend
    assert user_path == "/mnt/home0/wst/workspaces/jake.blow.clearwateranalytics.com"
