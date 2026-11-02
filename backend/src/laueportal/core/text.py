"""Text the API accepts and echoes: no NUL, no lone surrogate, and nothing unbounded.

JSON can spell any UTF-16 code unit with an escape, so a request can carry a **lone surrogate** (``"\\ud800"``,
half of a surrogate pair without the other half; ``JSON.stringify`` writes one for a pasted broken emoji) or a
**NUL** (``"\\u0000"``, and ``%00`` in a URL). Neither is text: UTF-8 cannot encode a lone surrogate, so SQLite,
the file system and the JSON answer fail on it, and SQLite's ``LIKE`` and the file system's names stop at a NUL.
The API refuses both with a 422 at its two decoding steps, through ``invalid_text``:

- ``core.api.check_request_text``, a dependency of every route: the path and query parameters and the JSON body;
- ``core.paging``: the JSON inside a grid's ``filter`` parameter (its field names and values).

What a problem echoes from a request goes through ``quoted`` (a short ``repr``) or ``escaped`` and ``shortened``;
``core.problems.sendable`` bounds and escapes every problem once more before it is sent.
"""

from __future__ import annotations

import re
import reprlib
from collections.abc import Iterator, Sequence
from typing import Any

INVALID_TEXT = "invalid_text"
"""The ``code`` of a problem with a NUL or a lone surrogate."""

_INVALID = re.compile(r"[\x00\ud800-\udfff]")
"""A NUL or a surrogate. ``json.loads`` joins an escaped pair into one character, so any surrogate left is lone."""

QUOTED_LIMIT = 80
"""The longest ``quoted`` value, in characters."""

_REPR = reprlib.Repr(
    maxlevel=2,
    maxtuple=4,
    maxlist=4,
    maxarray=4,
    maxdict=4,
    maxset=4,
    maxfrozenset=4,
    maxdeque=4,
    maxlong=40,
    maxstring=60,
    maxother=60,
)


def first_invalid(text: str) -> int | None:
    """The index of the first NUL or lone surrogate in ``text``, or None when it is all text."""

    match = _INVALID.search(text)
    return None if match is None else match.start()


def escaped(text: str) -> str:
    """``text`` with every NUL and lone surrogate written as its JSON escape (``\\u0000``, ``\\ud800``)."""

    return _INVALID.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def shortened(text: str, limit: int) -> str:
    """``text``, or its first ``limit - 1`` characters and "…" when it is longer than ``limit``."""

    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def quoted(value: Any, limit: int = QUOTED_LIMIT) -> str:
    """A value from a request for a message: its ``repr`` (a string's NUL and lone surrogates come out as escapes),
    at most ``limit`` characters and, for a list or an object, two levels deep."""

    text = repr(value[:limit]) if isinstance(value, str) else _REPR.repr(value)
    return shortened(escaped(text), limit)


def invalid_text(text: str, subject: str = "Text") -> str | None:
    """Why ``text`` is refused (its first NUL or lone surrogate and where it is), or None when it is all text.
    ``subject`` starts the message ("Text", "The name")."""

    index = first_invalid(text)
    if index is None:
        return None
    character = text[index]
    if character == "\x00":
        return f"{subject} cannot hold a NUL character (\\u0000, at character {index + 1})"
    return (
        f"{subject} must be valid Unicode: a lone surrogate (\\u{ord(character):04x}) at character {index + 1} is "
        "half of a character"
    )


def invalid_strings(value: Any, loc: Sequence[str | int] = ()) -> Iterator[tuple[list[str | int], str]]:
    """``(loc, message)`` for every string in a decoded JSON value (an object's names included) that holds a NUL or
    a lone surrogate, in document order. ``loc`` is the value's own location; an object member's name is shown
    escaped. The walk is iterative, so any depth ``json.loads`` produced is fine."""

    stack: list[tuple[list[str | int], Any]] = [(list(loc), value)]
    while stack:
        where, item = stack.pop()
        if isinstance(item, _InvalidName):
            yield where, item.message
        elif isinstance(item, str):
            reason = invalid_text(item)
            if reason is not None:
                yield where, reason
        elif isinstance(item, dict):
            members: list[tuple[list[str | int], Any]] = []
            for name, member in item.items():
                reason = invalid_text(name, "The name") if isinstance(name, str) else None
                if reason is None:
                    members.append(([*where, name], member))
                else:  # the member's value is not looked at: its name is already refused
                    members.append(([*where, escaped(name)], _InvalidName(reason)))
            stack.extend(reversed(members))
        elif isinstance(item, list):
            stack.extend(reversed([([*where, index], member) for index, member in enumerate(item)]))


class _InvalidName:
    """A refused object member name, kept in ``invalid_strings``'s stack so its problem comes in document order."""

    def __init__(self, message: str) -> None:
        self.message = message
