"""The find line's rules (twinfm.ui.find), free of Textual: matching names
and telling a path from a search, and resolving a typed path.

A search matches names without regard to case (``casefold``), anywhere in
the name; ``*`` is any run of characters (empty included), ``?`` exactly one,
every other character itself. ``..`` is never a match. Text is a path when it
contains ``/``, is exactly ``~``, or starts with a registered ``KIND:``.
"""

from __future__ import annotations

import pytest

from tests.ui.conftest import LEFT_ORDER
from twinfm.ui.find import (
    PARENT,
    first_match,
    is_path,
    location_kind,
    match_indices,
    matches,
    resolve,
    step_match,
)

KINDS = ["local", "btty"]
"""What the registry offers today (local, then btty)."""

# LEFT_ORDER: ["..", ".git", "alpha", "Sub", ".hidden", "A.txt", "b.txt", "c.txt", "Zeta.md"]
#              0     1       2        3      4          5        6        7        8


def test_parent_is_the_dot_dot_entry() -> None:
    assert PARENT == ".."


# -- matches ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "name", "start"),
    [
        ("b", "b.txt", 0),
        ("b", "Sub", 2),
        ("txt", "A.txt", 2),
        ("b", "abab", 1),  # the leftmost match
        ("zeta", "Zeta.md", 0),
    ],
)
def test_matches_gives_the_start_of_the_leftmost_unanchored_match(query: str, name: str, start: int) -> None:
    assert matches(query, name) == start


@pytest.mark.parametrize(
    ("query", "name", "start"),
    [
        ("A", "alpha", 0),
        ("a", "A.txt", 0),
        ("ZETA.MD", "Zeta.md", 0),
        ("É", "café", 3),
        ("ДОМ", "мой дом", 4),
        ("ss", "Straße", 4),  # ß casefolds to ss: the position is in "strasse"
        ("STRASSE", "straße", 0),
    ],
)
def test_matches_ignores_case_by_casefold_including_unicode(query: str, name: str, start: int) -> None:
    assert matches(query, name) == start


@pytest.mark.parametrize(
    ("query", "name", "start"),
    [
        ("*.md", "Zeta.md", 0),
        ("*", "x", 0),
        ("a*", "a", 0),  # * matches the empty run
        ("a*c", "xxabc", 2),
        ("a*c", "ac", 0),
        ("z*md", "Zeta.md", 0),
        ("a?c", "abc", 0),
        ("?", "x", 0),
        ("b.?xt", "b.txt", 0),
    ],
)
def test_star_is_any_run_and_question_mark_exactly_one_character(query: str, name: str, start: int) -> None:
    assert matches(query, name) == start


@pytest.mark.parametrize(
    ("query", "name"),
    [
        ("a?c", "ac"),  # ? is exactly one character, not zero
        ("a??c", "abc"),
        ("*.md", "Zeta.txt"),
        ("xyz", "Zeta.md"),
    ],
)
def test_no_match_is_none(query: str, name: str) -> None:
    assert matches(query, name) is None


@pytest.mark.parametrize(
    ("query", "name", "start"),
    [
        ("[1]", "file [1].txt", 5),
        ("[", "a[b", 1),
        ("]", "a]b", 1),
        ("a.b", "a.b", 0),
        ("\\", "back\\slash", 4),
        ("a+", "a+b", 0),
        ("(x)", "f(x).py", 1),
        ("^a", "x^a", 1),
        ("$", "a$b", 1),
        ("a|b", "a|b", 0),
        ("{1}", "x{1}", 1),
    ],
)
def test_every_other_character_is_literal(query: str, name: str, start: int) -> None:
    assert matches(query, name) == start


@pytest.mark.parametrize(
    ("query", "name"),
    [
        ("[1]", "file1.txt"),  # not a character class
        ("[ab]", "a"),
        ("a.b", "axb"),  # . is not any character
        ("a+", "aa"),  # + is not a repetition
        ("^a", "a"),  # ^ is not an anchor
        ("a$", "a"),  # $ is not an anchor
        ("a|b", "b"),  # | is not alternation
        ("\\d", "1"),  # backslash escapes nothing
    ],
)
def test_regex_syntax_has_no_special_meaning(query: str, name: str) -> None:
    assert matches(query, name) is None


@pytest.mark.parametrize("name", ["abc", "", ".."])
def test_an_empty_query_matches_nothing(name: str) -> None:
    assert matches("", name) is None


# -- match_indices -----------------------------------------------------------------------


def test_match_indices_lists_every_match_in_display_order() -> None:
    assert match_indices("t", LEFT_ORDER) == [1, 5, 6, 7, 8]
    assert match_indices("b", LEFT_ORDER) == [3, 6]
    assert match_indices("*.md", LEFT_ORDER) == [8]


@pytest.mark.parametrize("query", [".", "..", "*", "?", "?."])
def test_match_indices_never_includes_the_parent_entry(query: str) -> None:
    assert matches(query, PARENT) == 0, "precondition: the query would match '..'"
    assert 0 not in match_indices(query, LEFT_ORDER)


def test_match_indices_excludes_only_the_parent_entry_itself() -> None:
    assert match_indices("..", ["..", "..foo", "a..b"]) == [1, 2]


def test_match_indices_of_an_empty_query_or_no_match_is_empty() -> None:
    assert match_indices("", LEFT_ORDER) == []
    assert match_indices("xyz", LEFT_ORDER) == []
    assert match_indices("a", []) == []


# -- first_match ---------------------------------------------------------------------------


def test_first_match_prefers_a_match_at_the_start_over_an_earlier_substring_match() -> None:
    # "Sub" (index 3) contains "b", but "b.txt" (index 6) starts with it.
    assert first_match("b", LEFT_ORDER) == 6


def test_first_match_without_a_prefix_match_is_the_first_match_anywhere() -> None:
    # Nothing starts with "t"; ".git" is the first name containing it.
    assert first_match("t", LEFT_ORDER) == 1


@pytest.mark.parametrize(
    ("query", "index"),
    [("a", 2), ("A", 2), ("*.md", 8), ("A.t", 5), (".", 1), ("su", 3), ("z", 8)],
)
def test_first_match_in_the_test_tree(query: str, index: int) -> None:
    assert first_match(query, LEFT_ORDER) == index


@pytest.mark.parametrize("query", ["xyz", "", ".."])
def test_first_match_is_none_without_a_match(query: str) -> None:
    assert first_match(query, LEFT_ORDER) is None


def test_first_match_skips_the_parent_entry_even_when_it_would_match_at_the_start() -> None:
    assert first_match(".", ["..", "x.y"]) == 1
    assert first_match("*", ["..", "a"]) == 1
    assert first_match("..", [".."]) is None


def test_first_match_casefolds_unicode() -> None:
    assert first_match("É", ["café", "école"]) == 1
    assert first_match("STRASSE", ["Straße"]) == 0


# -- step_match ------------------------------------------------------------------------------


def test_step_down_goes_to_the_next_match_and_wraps_to_the_first() -> None:
    hits = [1, 5, 6, 7, 8]  # "t"
    seen = []
    index = 1
    for _ in hits:
        index = step_match("t", LEFT_ORDER, index, 1)
        seen.append(index)
    assert seen == [5, 6, 7, 8, 1]


def test_step_up_goes_to_the_previous_match_and_wraps_to_the_last() -> None:
    seen = []
    index = 1
    for _ in range(5):
        index = step_match("t", LEFT_ORDER, index, -1)
        seen.append(index)
    assert seen == [8, 7, 6, 5, 1]


@pytest.mark.parametrize("step", [1, -1])
def test_step_with_a_single_match_stays_on_it(step: int) -> None:
    assert step_match("alp", LEFT_ORDER, 2, step) == 2


@pytest.mark.parametrize(
    ("index", "step", "expected"),
    [
        (3, 1, 5),  # from "Sub" (not a match for "t") down to A.txt
        (3, -1, 1),  # and up to .git
        (0, 1, 1),  # from ".."
        (0, -1, 8),  # nothing before "..": wraps to the last match
        (4, -1, 1),
    ],
)
def test_step_from_a_row_that_is_not_a_match(index: int, step: int, expected: int) -> None:
    assert step_match("t", LEFT_ORDER, index, step) == expected


@pytest.mark.parametrize("step", [1, -1])
def test_step_with_a_single_match_from_another_row_goes_to_it(step: int) -> None:
    assert step_match("alp", LEFT_ORDER, 6, step) == 2


@pytest.mark.parametrize(("query", "step"), [("xyz", 1), ("xyz", -1), ("", 1), ("..", -1)])
def test_step_without_a_match_is_none(query: str, step: int) -> None:
    assert step_match(query, LEFT_ORDER, 3, step) is None


def test_step_never_lands_on_the_parent_entry() -> None:
    names = ["..", "a.txt"]
    assert step_match(".", names, 1, 1) == 1
    assert step_match(".", names, 1, -1) == 1


# -- location_kind and is_path -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("btty:/x", "btty"),
        ("btty:", "btty"),
        ("btty:rel/path", "btty"),
        ("local:/tmp", "local"),
        ("local:x", "local"),
    ],
)
def test_location_kind_is_the_registered_kind_before_the_colon(text: str, kind: str) -> None:
    assert location_kind(text, KINDS) == kind


@pytest.mark.parametrize("text", ["btty", "bttyx:/a", "xbtty:/a", "/btty:x", "./btty:x", "BTTY:/x", "memory:/x", ""])
def test_location_kind_is_none_without_a_registered_kind_prefix(text: str) -> None:
    assert location_kind(text, KINDS) is None


def test_location_kind_takes_any_iterable_of_kinds() -> None:
    assert location_kind("btty:/x", iter(KINDS)) == "btty"
    assert location_kind("btty:/x", []) is None


@pytest.mark.parametrize("text", ["~", "/", "/tmp", "a/b", "Sub/", "../", "~/x", "./btty:x", "~foo/x"])
def test_text_with_a_slash_or_a_lone_tilde_is_a_path(text: str) -> None:
    assert is_path(text, KINDS)


@pytest.mark.parametrize("text", ["~foo", ".", "..", "", "b", "*.md", "x~", "~~", "a:b", "file [1].txt"])
def test_other_text_is_a_search(text: str) -> None:
    assert not is_path(text, KINDS)


def test_kind_prefix_is_a_path_only_when_that_kind_is_registered() -> None:
    assert is_path("btty:x", ["local", "btty"])
    assert is_path("btty:", ["btty"])
    assert not is_path("btty:x", ["local"])
    assert not is_path("btty:x", [])
    assert is_path("local:x", ["local"])


# -- resolve -----------------------------------------------------------------------------------


@pytest.mark.parametrize("home", ["/home/me", "/Users/someone", "/"])
def test_tilde_is_the_home_it_is_given(home: str) -> None:
    assert resolve("~", "/base/dir", home) == home


@pytest.mark.parametrize(("home", "expected"), [("/home/me", "/home/me/x"), ("/remote", "/remote/x"), ("/", "/x")])
def test_tilde_slash_rest_lies_under_the_home(home: str, expected: str) -> None:
    assert resolve("~/x", "/base/dir", home) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("~/", "/home/me"),
        ("~//x", "/home/me/x"),
        ("~/x/", "/home/me/x"),
        ("~/../x", "/home/x"),
        ("~/a/./b", "/home/me/a/b"),
    ],
)
def test_paths_under_the_home_are_normalised(text: str, expected: str) -> None:
    assert resolve(text, "/base/dir", "/home/me") == expected


def test_a_home_given_with_a_trailing_slash_comes_out_normalised() -> None:
    assert resolve("~", "/base", "/home/me/") == "/home/me"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/tmp", "/tmp"),
        ("/tmp/", "/tmp"),
        ("//tmp//x/", "/tmp/x"),
        ("/", "/"),
        ("//", "/"),
        ("///", "/"),
        ("/..", "/"),
        ("/../..", "/"),
        ("/a/../b", "/b"),
        ("/a/./b", "/a/b"),
    ],
)
def test_a_leading_slash_is_absolute_and_normalised(text: str, expected: str) -> None:
    assert resolve(text, "/base/dir", "/home/me") == expected


@pytest.mark.parametrize(
    ("text", "base", "expected"),
    [
        ("Sub/", "/L", "/L/Sub"),
        ("Sub//inner/", "/L", "/L/Sub/inner"),
        ("../", "/L/M", "/L"),
        ("..//", "/L/M", "/L"),
        ("../../..", "/L", "/"),
        ("./x", "/L", "/L/x"),
        ("a/b", "/", "/a/b"),
        ("./btty:x", "/L", "/L/btty:x"),
        ("~foo/x", "/L", "/L/~foo/x"),  # ~foo is a name, not a home
    ],
)
def test_other_text_is_relative_to_the_base(text: str, base: str, expected: str) -> None:
    assert resolve(text, base, "/home/me") == expected
