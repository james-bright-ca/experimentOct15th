"""Backend registry: which kinds of store exist, and how a location spec
(command-line argument or typed location) becomes a (Backend, path) pair.

The contract every backend implements is in ``twinfm.backend.base``.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Callable

from twinfm.backend.base import Backend
from twinfm.backend.btty import open_btty
from twinfm.backend.local import LocalBackend
from twinfm.config import Settings

__all__ = [
    "BTTY_PREFIX",
    "REGISTRY",
    "BackendFactory",
    "UnknownBackendError",
    "available",
    "open_backend",
    "parse_location",
]

BackendFactory = Callable[[Settings], Backend]

BTTY_PREFIX = "btty:"


class UnknownBackendError(LookupError):
    """No backend is registered under ``kind``; the message names it."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(
            f"backend {kind!r} is not available (available: {', '.join(available())})"
        )


_LOCAL = LocalBackend()


def _open_local(settings: Settings) -> Backend:
    return _LOCAL


REGISTRY: dict[str, BackendFactory] = {"local": _open_local}
"""Backend kind -> factory. A factory builds (or returns) the backend for the
given Settings without doing I/O. The engine treats two locations as the same
store only when their backends are the same object, so a factory returns one
shared instance for one store: "local" always returns the same LocalBackend."""

REGISTRY["btty"] = open_btty


def available() -> list[str]:
    """The registered backend kinds, in registration order."""
    return list(REGISTRY)


def open_backend(kind: str, settings: Settings) -> Backend:
    """The backend for ``kind``; UnknownBackendError if it is not registered."""
    factory = REGISTRY.get(kind)
    if factory is None:
        raise UnknownBackendError(kind)
    return factory(settings)


def _normalised(path: str) -> str:
    # normpath keeps a POSIX leading "//"; backend paths start with exactly one "/".
    return "/" + posixpath.normpath(path).lstrip("/")


def parse_location(spec: str, settings: Settings) -> tuple[Backend, str]:
    """Turn a location spec into ``(backend, absolute normalised path)``.

    ``btty:`` selects the btty backend at ``settings.remote_home``;
    ``btty:/abs/path`` at that path; ``btty:rel/path`` resolves against the
    remote home. Anything else is a local path made absolute against the
    current directory (write ``./btty:x`` for a local name starting with
    ``btty:``). Raises UnknownBackendError naming the kind when it is not
    registered.
    """
    if spec.startswith(BTTY_PREFIX):
        backend = open_backend("btty", settings)
        rest = spec[len(BTTY_PREFIX) :]
        return backend, _normalised(posixpath.join(settings.remote_home, rest))
    return open_backend("local", settings), _normalised(os.path.abspath(spec))
