"""Settings: defaults, an optional TOML config file, and command-line overrides.

The config file holds two tables::

    [btty]
    home = "/mnt/home0/wst/workspaces"       # remote home directory (absolute)
    command = "btty"                         # the btty executable
    exec = true                              # btty --exec --allow-unrestricted; false: transfer-only
    timeout = 120                            # seconds, passed to btty --timeout
    tmp = "/tmp"                             # remote directory for archives of remote selections
    archive_timeout = 900                    # seconds, btty --timeout for pack, unpack and merge
    archive_max_bytes = 268435456            # a larger batch is transferred per file

    [viewer]
    command = "less -R"                      # F3's viewer, split as a shell would

Every key is optional. Other top-level tables are ignored.
The ``[btty]`` values shown are the defaults: exec mode is on unless
``exec = false``, which leaves only single-file upload and download.
``[viewer] command`` has no default: without it F3 runs $PAGER, else
``less -R``, else ``more``. The file's path is always added as the last
argument; no shell is involved.
"""

from __future__ import annotations

import os
import posixpath
import shlex
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CONFIG_ENV",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_REMOTE_HOME",
    "ConfigError",
    "Settings",
    "config_path",
    "load_settings",
]

DEFAULT_REMOTE_HOME = "/mnt/home0/wst/workspaces"
CONFIG_ENV = "TWINFM_CONFIG"
DEFAULT_CONFIG_PATH = "~/.config/twinfm/config.toml"


def _remote_home(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"must be an absolute path, got {value!r}")
    # normpath drops the trailing slash but keeps a POSIX leading "//".
    return "/" + posixpath.normpath(value).lstrip("/")


def _command(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"must be a non-empty string, got {value!r}")
    return value


def _viewer_command(value: object) -> str | None:
    if value is None:
        return None
    try:
        words = shlex.split(value) if isinstance(value, str) else []
    except ValueError:  # an unclosed quote
        words = []
    if not words:
        raise ValueError(f"must be a command of at least one word, got {value!r}")
    return value


def _exec(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"must be true or false, got {value!r}")
    return value


def _timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"must be a positive whole number of seconds, got {value!r}")
    return value


def _byte_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"must be a positive whole number of bytes, got {value!r}")
    return value


_FIELDS: dict[str, Callable[[object], Any]] = {
    "remote_home": _remote_home,
    "btty_command": _command,
    "btty_exec": _exec,
    "btty_timeout": _timeout,
    "remote_tmp": _remote_home,
    "btty_archive_timeout": _timeout,
    "archive_max_bytes": _byte_count,
    "viewer_command": _viewer_command,
}

# config-file table -> its key -> Settings field
_FILE_KEYS = {
    "btty": {
        "home": "remote_home",
        "command": "btty_command",
        "exec": "btty_exec",
        "timeout": "btty_timeout",
        "tmp": "remote_tmp",
        "archive_timeout": "btty_archive_timeout",
        "archive_max_bytes": "archive_max_bytes",
    },
    "viewer": {
        "command": "viewer_command",
    },
}


class ConfigError(ValueError):
    """The config file cannot be read or is malformed. ``str()`` names the file."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, validated settings (hashable).

    remote_home
        Absolute remote directory the btty panel opens at and resolves relative
        remote paths against; stored without a trailing slash.
        Default ``/mnt/home0/wst/workspaces``; ``home`` in ``[btty]`` or
        ``--remote-home`` changes it.
    btty_command
        The btty executable, first element of every btty argv.
    btty_exec
        On by default: ``btty --exec --allow-unrestricted`` (browse, delete, rename,
        mkdir, folders). False means transfer-only: single-file put/get.
        ``exec = false`` in ``[btty]`` or ``--no-btty-exec`` turns exec mode off.
    btty_timeout
        Seconds passed to ``btty --timeout`` for exec commands other than
        pack, unpack and merge.
    remote_tmp
        Absolute remote directory where archive mode packs a remote selection
        before downloading it (the selection's own directory may be
        read-only); stored normalised, without a trailing slash. The archive
        is only written, downloaded and deleted there, never renamed into the
        workspace (another filesystem). Default ``/tmp``; ``tmp`` in ``[btty]``.
    btty_archive_timeout
        Seconds passed to ``btty --timeout`` for the archive commands (pack,
        unpack, merge), which can run far longer than the others. Default 900;
        ``archive_timeout`` in ``[btty]``.
    archive_max_bytes
        The largest batch moved as one archive: when the selection's total
        size or the packed archive is larger, the batch is transferred per
        file instead. btty holds a whole transfer in memory, hence the
        default of 268435456 (256 MiB); ``archive_max_bytes`` in ``[btty]``.
    viewer_command
        The command F3 views a local file with, split as a shell would, the
        file's absolute path added as its last argument. Default None: F3
        runs $PAGER, else ``less -R``, else ``more``; ``command`` in
        ``[viewer]``.

    Invalid values raise ValueError naming the field.
    """

    remote_home: str = DEFAULT_REMOTE_HOME
    btty_command: str = "btty"
    btty_exec: bool = True
    btty_timeout: int = 120
    remote_tmp: str = "/tmp"
    btty_archive_timeout: int = 900
    archive_max_bytes: int = 268435456
    viewer_command: str | None = None

    def __post_init__(self) -> None:
        for name, check in _FIELDS.items():
            try:
                value = check(getattr(self, name))
            except ValueError as e:
                raise ValueError(f"{name} {e}") from None
            object.__setattr__(self, name, value)


def config_path(path: str | os.PathLike[str] | None = None) -> str:
    """The config file to read: ``path`` if given, else ``$TWINFM_CONFIG`` if
    set and non-empty, else ``~/.config/twinfm/config.toml``."""
    if path is not None:
        return os.fspath(path)
    return os.environ.get(CONFIG_ENV) or os.path.expanduser(DEFAULT_CONFIG_PATH)


def load_settings(
    path: str | os.PathLike[str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Settings:
    """Build Settings from defaults, the config file, then ``overrides``.

    The file is chosen by ``config_path(path)``; a missing file means
    defaults. An unreadable or malformed file (bad TOML, ``btty`` or ``viewer``
    not a table, an unknown key in ``[btty]`` or ``[viewer]``, a value of the
    wrong type) raises ConfigError naming the file. ``overrides`` (from command-line flags) maps
    Settings field names to values and wins over the file; a None value means
    "not given". An unknown override name, or an invalid override value,
    raises ValueError.
    """
    file = config_path(path)
    try:
        with open(file, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(file, f"invalid TOML: {e}") from e
    except OSError as e:
        raise ConfigError(file, e.strerror or str(e)) from e

    values: dict[str, Any] = {}
    for section, keys in _FILE_KEYS.items():
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise ConfigError(file, f"[{section}] must be a table")
        for key, value in table.items():
            name = keys.get(key)
            if name is None:
                known = ", ".join(keys)
                raise ConfigError(file, f"unknown key {key!r} in [{section}] (known: {known})")
            try:
                values[name] = _FIELDS[name](value)
            except ValueError as e:
                raise ConfigError(file, f"[{section}] {key} {e}") from None

    for name, value in (overrides or {}).items():
        if name not in _FIELDS:
            raise ValueError(f"unknown setting {name!r}")
        if value is not None:
            values[name] = value
    return Settings(**values)
