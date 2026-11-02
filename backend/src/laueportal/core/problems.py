"""Problem details (RFC 9457): the error type services raise. This module never imports FastAPI.

Raise ``ProblemError`` from services and endpoints, e.g. ``ProblemError(404, "Scan not found", "No scan 12")``.
``core.errors`` turns it into an ``application/problem+json`` response, through ``sendable``: whatever a problem echoes
from a request, its answer stays small and can always be sent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel

from laueportal.core.text import escaped, shortened

MAX_PROBLEM_ITEMS = 20
"""The most ``errors`` a problem response lists; its ``detail`` then says how many there were."""
MAX_TEXT_LENGTH = 1000
"""The longest ``title`` or ``detail`` sent, in characters (a longer one ends with "…")."""
MAX_MESSAGE_LENGTH = 500
"""The longest ``errors[].message`` sent."""
MAX_LOC_PARTS = 16
"""The most parts of an ``errors[].loc`` sent (a deeper one ends with "…")."""
MAX_LOC_PART_LENGTH = 64
"""The longest name in an ``errors[].loc`` sent."""


class ProblemItem(BaseModel):
    """One problem with one input, located by ``loc``."""

    loc: list[str | int]
    message: str
    code: str
    severity: Literal["error", "warning"] = "error"


class Problem(BaseModel):
    """An RFC 9457 problem details body."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[ProblemItem] | None = None


class ProblemError(Exception):
    """An error the API reports as problem+json, e.g. ``ProblemError(404, "Scan not found", "No scan 12")``."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        errors: Iterable[ProblemItem | Mapping[str, Any]] | None = None,
        *,
        problem_type: str = "about:blank",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        items = None if errors is None else [ProblemItem.model_validate(item) for item in errors]
        self.problem = Problem(type=problem_type, title=title, status=status, detail=detail, errors=items)
        self.headers = dict(headers or {})
        super().__init__(f"{status} {title}" + (f": {detail}" if detail else ""))

    @property
    def status(self) -> int:
        return self.problem.status


REQUIRED = "Required"
"""The message of a field left blank: pydantic's ``missing``, or a required form field's blank value."""

_FIELD_MESSAGES: dict[str, str] = {
    "missing": REQUIRED,
    "int_parsing": "Must be a whole number",
    "int_type": "Must be a whole number",
    "int_from_float": "Must not have a fractional part",
    "float_parsing": "Must be a number",
    "float_type": "Must be a number",
    "finite_number": "Must be a finite number",
    "bool_parsing": "Must be true or false",
    "bool_type": "Must be true or false",
    "string_type": "Must be text",
    "list_type": "Must be a list",
    "greater_than": "Must be greater than {gt}",
    "greater_than_equal": "Must be at least {ge}",
    "less_than": "Must be less than {lt}",
    "less_than_equal": "Must be at most {le}",
    "literal_error": "Must be {expected}",
    "enum": "Must be {expected}",
}
"""Pydantic's error types a form field can produce, in plain words (``{name}`` is the error's ``ctx`` item)."""

_LENGTHS = {
    "too_short": ("at least", "min_length", "entry", "entries"),
    "too_long": ("at most", "max_length", "entry", "entries"),
    "string_too_short": ("at least", "min_length", "character", "characters"),
    "string_too_long": ("at most", "max_length", "character", "characters"),
}


def _ctx_text(value: Any) -> str:
    """A constraint's value as written: a whole float without its ``.0`` (a float field's ``le=1`` is ``1.0``)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def field_message(error: Mapping[str, Any]) -> str:
    """A pydantic error (``type``, ``msg``, ``ctx``) as a form shows it under its field: short, and never naming
    the field, whose label the form's summary adds once (``"Required"``, ``"Must be a whole number"``, ``"Must be
    greater than 0"``, ``"Must have at least one entry"``). A model's own ``ValueError`` keeps its wording; any other
    type keeps pydantic's, its "Input should" made "Must"."""

    code = str(error.get("type", ""))
    ctx: Mapping[str, Any] = error.get("ctx") or {}
    if code == "value_error" and ctx.get("error") is not None:
        return str(ctx["error"])
    if code in _LENGTHS and ctx.get(_LENGTHS[code][1]) is not None:
        bound, key, one, many = _LENGTHS[code]
        count = ctx[key]
        return f"Must have {bound} {'one' if count == 1 else f'{count:,}'} {one if count == 1 else many}"
    template = _FIELD_MESSAGES.get(code)
    if template is not None:
        try:
            return template.format_map({name: _ctx_text(value) for name, value in ctx.items()})
        except KeyError:  # a constraint without its ctx: pydantic's own wording below
            pass
    message = str(error.get("msg") or "Invalid value")
    for prefix in ("Input should ", "Value should "):
        if message.startswith(prefix):
            return f"Must {message.removeprefix(prefix)}"
    return message


def problems_detail(count: int) -> str:
    """A validation problem's ``detail``: ``"1 problem"``, ``"3 problems"``."""

    return f"{count} problem{'' if count == 1 else 's'}"


def _text(value: str, limit: int) -> str:
    return shortened(escaped(value), limit)


def _loc(loc: Sequence[str | int]) -> list[str | int]:
    parts: list[str | int] = [
        part if isinstance(part, int) else _text(part, MAX_LOC_PART_LENGTH) for part in loc[:MAX_LOC_PARTS]
    ]
    if len(loc) > MAX_LOC_PARTS:
        parts[-1] = "…"
    return parts


def bounded(item: ProblemItem) -> ProblemItem:
    """``item`` as it is sent: its message and ``loc`` escaped and bounded (``MAX_MESSAGE_LENGTH``,
    ``MAX_LOC_PARTS`` parts of at most ``MAX_LOC_PART_LENGTH`` characters, its code too). A validate endpoint's
    issues go through it as a problem's errors do (``sendable``)."""

    return ProblemItem(
        loc=_loc(item.loc),
        message=_text(item.message, MAX_MESSAGE_LENGTH),
        code=_text(item.code, MAX_LOC_PART_LENGTH),
        severity=item.severity,
    )


def sendable(problem: Problem) -> Problem:
    """``problem`` as it is sent: every text with its NUL and lone surrogates escaped (UTF-8 cannot encode a lone
    surrogate) and bounded, at most ``MAX_PROBLEM_ITEMS`` errors (the ``detail`` says how many there were), each
    ``loc`` at most ``MAX_LOC_PARTS`` parts. The answer's size never depends on the request's."""

    errors = problem.errors
    detail = None if problem.detail is None else _text(problem.detail, MAX_TEXT_LENGTH)
    if errors is not None and len(errors) > MAX_PROBLEM_ITEMS:
        listed = f"the first {MAX_PROBLEM_ITEMS} of {len(errors)} are listed"
        detail = f"{detail} ({listed})" if detail else f"{listed[0].upper()}{listed[1:]}."
        errors = errors[:MAX_PROBLEM_ITEMS]
    return problem.model_copy(  # a subclass's extension members (a 409's ``created``) are the server's own: kept
        update={
            "type": _text(problem.type, MAX_TEXT_LENGTH),
            "title": _text(problem.title, MAX_TEXT_LENGTH),
            "detail": detail,
            "instance": None if problem.instance is None else _text(problem.instance, MAX_TEXT_LENGTH),
            "errors": None if errors is None else [bounded(item) for item in errors],
        }
    )
