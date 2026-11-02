"""Text the API accepts and echoes (``laueportal.core.text``): NUL and lone surrogates found and escaped, values
from a request shortened for messages."""

from __future__ import annotations

import json

import pytest

from laueportal.core.text import QUOTED_LIMIT, escaped, first_invalid, invalid_strings, invalid_text, quoted, shortened


@pytest.mark.parametrize(
    ("text", "index"),
    [
        ("plain", None),
        ("", None),
        ("emoji \U0001f600 and accents é", None),  # a joined surrogate pair is one character
        ("a\x00b", 1),
        ("\ud800", 0),
        ("ab\udfff", 2),
        ("x\ud83d", 1),  # the first half of a pair, alone
        ("é\udcfc\x00", 1),  # a surrogate escape of the file system (Latin-1 ü), then a NUL
    ],
)
def test_first_invalid_finds_a_nul_or_a_lone_surrogate(text: str, index: int | None) -> None:
    assert first_invalid(text) == index


def test_json_joins_an_escaped_pair_so_only_lone_surrogates_are_left() -> None:
    assert first_invalid(json.loads('"\\ud83d\\ude00"')) is None
    assert first_invalid(json.loads('"\\ude00\\ud83d"')) == 0  # the halves in the wrong order are two lone ones


def test_escaped_writes_each_nul_and_lone_surrogate_as_its_json_escape() -> None:
    assert escaped("a\x00b\ud800c\udfff\U0001f600") == "a\\u0000b\\ud800c\\udfff\U0001f600"
    assert escaped("plain") == "plain"
    escaped("x\ud800").encode("utf-8")  # it can be sent


def test_shortened_keeps_short_texts_and_marks_a_cut() -> None:
    assert shortened("abc", 3) == "abc"
    assert shortened("abcd", 3) == "ab…"
    assert len(shortened("x" * 10_000, 50)) == 50


def test_quoted_is_a_short_repr_whatever_the_value() -> None:
    assert quoted("bogus") == "'bogus'"
    assert quoted(5) == "5" and quoted(None) == "None" and quoted(True) == "True"
    assert quoted("a\ud800\x00") == "'a\\ud800\\x00'"
    for value in ("x" * 100_000, list(range(100_000)), {str(i): [i] * 100 for i in range(1000)}, 10**4000):
        assert len(quoted(value)) <= QUOTED_LIMIT
    assert quoted("x" * 1000, 300).startswith("'xxx") and len(quoted("x" * 1000, 300)) == 300
    deep: list = []
    for _ in range(100_000):  # deeper than repr() could go
        deep = [deep]
    assert quoted(deep) == "[[[...]]]"


def test_invalid_text_says_what_and_where() -> None:
    assert invalid_text("fine") is None
    assert invalid_text("ab\x00") == "Text cannot hold a NUL character (\\u0000, at character 3)"
    assert invalid_text("\ud800", "The name") == (
        "The name must be valid Unicode: a lone surrogate (\\ud800) at character 1 is half of a character"
    )


def test_invalid_strings_finds_every_one_in_document_order_with_names_escaped() -> None:
    document = {
        "a": "fine",
        "b": ["ok", "x\x00", {"c": "\ud800"}],
        "bad\ud800name": "its value is not looked at: \x00",
        "d": {"e": 1, "f": None, "g": True, "h": 2.5},
        "last": "\udfff",
    }
    found = list(invalid_strings(document, ["query", "filter"]))
    assert [loc for loc, _ in found] == [
        ["query", "filter", "b", 1],
        ["query", "filter", "b", 2, "c"],
        ["query", "filter", "bad\\ud800name"],
        ["query", "filter", "last"],
    ]
    assert found[2][1].startswith("The name must be valid Unicode")
    assert list(invalid_strings("x\x00")) == [([], "Text cannot hold a NUL character (\\u0000, at character 2)")]
    assert list(invalid_strings({"fine": ["text", 1, None]})) == []


def test_invalid_strings_walks_any_depth_json_produced() -> None:
    depth = 8000
    document = json.loads("[" * depth + '"\\u0000"' + "]" * depth)
    [(loc, _)] = list(invalid_strings(document))
    assert loc == [0] * depth
