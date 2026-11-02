"""Entity tags for what the API derives from files: the results viewer's summary and products;
and the version of stored values a client edits (``value_tag``: the scan catalog's).

Such a response carries a weak ``ETag`` computed from everything it depends on (the source file's identity: its
real path, size, modification and change times; the geometry file's; the request's parameters) and the code that
computes it (``versions``), and ``Cache-Control: private, no-cache``: the browser keeps it but asks again before
every use, sending the tag in ``If-None-Match``, and gets a bodiless 304 while nothing changed. There is no ``max-age``, so
nothing stale is served after a conversion writes ``output.h5``, an XML is chosen or a geometry file is edited
(an identity changes), or after a deployment with other code (the versions change). FastAPI-free:
``core.api.revalidated`` applies it to a response; an endpoint that builds its own ``Response`` gives it
``headers(tag)``.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache

from laueportal.core import provenance

REVALIDATE = "private, no-cache"
"""The ``Cache-Control`` of a tagged response: stored by the browser, revalidated before every use."""


@cache
def versions() -> tuple[str, str]:
    """The code that computes a tagged response: a digest of the portal's source and lauelab's pinned commit
    (``core.provenance``). Made once per process (the app makes it at startup), so it is the same in every API
    process of one deployment and changes with any change to either."""

    return provenance.portal_identity(), provenance.lauelab_identity()


def entity_tag(*parts: object) -> str:
    """A weak entity tag (``W/"<hex>"``) for ``parts``, JSON-encoded (anything else as its ``str``), and the
    ``versions`` of the code, so a deployment that computes differently never matches an old tag."""

    return _digest([*versions(), *parts])


def value_tag(*parts: object) -> str:
    """A weak entity tag of stored values alone (a record's version, as the scan catalog's ``version``): the same
    digest as ``entity_tag`` without the code's ``versions``, so only a change to the values changes it, never a
    deployment."""

    return _digest(list(parts))


def _digest(parts: list[object]) -> str:
    text = json.dumps(parts, default=str, separators=(",", ":"), sort_keys=True)
    return f'W/"{hashlib.blake2b(text.encode(), digest_size=16).hexdigest()}"'


def _opaque(tag: str) -> str:
    tag = tag.strip()
    return tag[2:] if tag.startswith("W/") else tag


def matches(if_none_match: str | None, tag: str) -> bool:
    """Whether an ``If-None-Match`` header holds ``tag``: weak comparison (RFC 9110 §13.1.2), ``*`` matches."""

    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    wanted = _opaque(tag)
    return any(_opaque(candidate) == wanted for candidate in if_none_match.split(","))


def headers(tag: str) -> dict[str, str]:
    """The headers of a response tagged ``tag`` (also of its 304)."""

    return {"ETag": tag, "Cache-Control": REVALIDATE}
