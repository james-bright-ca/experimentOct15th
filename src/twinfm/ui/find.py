"""The find line's rules, free of Textual: matching names and resolving
typed paths.

Text in the find line is either a search or a path. A search matches names
without regard to case (``casefold``), anywhere in the name: ``*`` stands for
any run of characters, ``?`` for exactly one, and every other character for
itself. ``..`` is never a match. A path is text that contains ``/``, is
exactly ``~``, or starts with a registered ``KIND:``; ``~foo``, ``.`` and
``..`` stay searches.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache

__all__ = [
    "PARENT",
    "first_match",
    "is_path",
    "location_kind",
    "match_indices",
    "matches",
    "resolve",
    "step_match",
]

PARENT = ".."


@lru_cache(maxsize=64)
def _pattern(query: str) -> re.Pattern[str] | None:
    if not query:
        return None
    parts = (".*" if c == "*" else "." if c == "?" else re.escape(c) for c in query.casefold())
    return re.compile("".join(parts), re.DOTALL)


def matches(query: str, name: str) -> int | None:
    """Where the leftmost match of ``query`` starts in ``name``, both
    casefolded; None when there is none or ``query`` is empty."""
    pattern = _pattern(query)
    found = pattern.search(name.casefold()) if pattern is not None else None
    return found.start() if found is not None else None


def match_indices(query: str, names: Sequence[str]) -> list[int]:
    """The indices of the names ``query`` matches, in order; never ``..``."""
    return [i for i, name in enumerate(names) if name != PARENT and matches(query, name) is not None]


def first_match(query: str, names: Sequence[str]) -> int | None:
    """The first name the match starts at the beginning of, else the first
    match anywhere, else None."""
    first: int | None = None
    for i, name in enumerate(names):
        if name == PARENT:
            continue
        start = matches(query, name)
        if start == 0:
            return i
        if start is not None and first is None:
            first = i
    return first


def step_match(query: str, names: Sequence[str], index: int, step: int) -> int | None:
    """The next match after ``index`` (``step`` 1) or the previous one before
    it (``step`` -1), wrapping around; ``index`` itself when it is the only
    match, None when nothing matches. ``index`` need not be a match."""
    hits = match_indices(query, names)
    if not hits:
        return None
    if step > 0:
        return next((i for i in hits if i > index), hits[0])
    return next((i for i in reversed(hits) if i < index), hits[-1])


def location_kind(text: str, kinds: Iterable[str]) -> str | None:
    """The kind ``text`` starts with as ``KIND:``, or None."""
    return next((kind for kind in kinds if text.startswith(kind + ":")), None)


def is_path(text: str, kinds: Iterable[str]) -> bool:
    """Whether ``text`` is a path to go to rather than a search."""
    return "/" in text or text == "~" or location_kind(text, kinds) is not None


def resolve(text: str, base: str, home: str) -> str:
    """The absolute, normalised path ``text`` (without ``KIND:``) names: ``~``
    is ``home`` and ``~/rest`` lies under it, a leading ``/`` is absolute, and
    anything else is relative to ``base``. ``..`` above ``/`` stays at ``/``."""
    if text == "~":
        path = home
    elif text.startswith("~/"):
        path = posixpath.join(home, text[2:].lstrip("/"))
    else:
        path = posixpath.join(base, text)
    # normpath keeps a POSIX leading "//"; backend paths start with exactly one "/".
    return "/" + posixpath.normpath(path).lstrip("/")
