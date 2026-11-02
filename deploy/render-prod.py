"""Render the production configuration from this checkout's templates (docs/runbooks/deploy.md).

    python deploy/render-prod.py [--output-dir <folder>] [--force] ROOT=... RUNS_ROOT=... API_PORT=... \\
        HOST_NAME=... REMOTE_ENABLED=true|false

writes into <ROOT>/config (the installation's config folder, which supervisord.conf names), filling in every
{{NAME}} placeholder of the templates:

    deploy/supervisor/prod.conf.template -> supervisord.conf
    deploy/config/prod.toml.template     -> prod.toml

A placeholder without a value, a value no template uses, or a value that does not suit its place (a relative
path or one ending in '/', a port out of range, a host name with a scheme or port) is refused,
and nothing is written. The files are private (0600, the folder 0700), written whole (a temporary file renamed),
and an existing one is replaced only with --force (prod.toml is edited after rendering). --output-dir renders
elsewhere, to compare a new commit's templates with the installed files (docs/runbooks/deploy.md, "Upgrade"): the
files still name <ROOT>'s paths. The output folder must lie outside the checkout. Nothing here is secret.
Standard library only; prints the files it wrote.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
TEMPLATES = {
    "supervisord.conf": CHECKOUT / "deploy/supervisor/prod.conf.template",
    "prod.toml": CHECKOUT / "deploy/config/prod.toml.template",
}
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_PATH = re.compile(r"/[A-Za-z0-9._/+-]*")
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


class RenderError(ValueError):
    """A value is missing, unused or unsuitable, or an output cannot be written."""


def _path(value: str) -> str | None:
    if (
        not _PATH.fullmatch(value)
        or value.endswith("/")  # "/" too: <ROOT>/config would be //config
        or "//" in value
        or "/../" in f"{value}/"
        or "/./" in f"{value}/"
    ):
        return "must be an absolute path of letters, digits and ._+- (no space, quote, '..', '.' or final '/')"
    return None


def _port(value: str) -> str | None:
    return None if value.isdigit() and 1 <= int(value) <= 65535 else "must be a port, 1-65535"


CHECKS = {
    "ROOT": _path,
    "RUNS_ROOT": _path,
    "API_PORT": _port,
    "HOST_NAME": lambda value: None if _HOST.fullmatch(value) else "must be a host name (no scheme, port or path)",
    "REMOTE_ENABLED": lambda value: None if value in ("true", "false") else "must be true or false",
}
"""How each placeholder's value is checked: a value goes into a TOML string or number and a supervisor command as
it is."""


def check_values(values: dict[str, str]) -> None:
    """``RenderError`` naming every problem with ``values`` against the templates' placeholders."""
    wanted = {name for template in TEMPLATES.values() for name in PLACEHOLDER.findall(template.read_text())}
    problems = [f"{name}: no value given" for name in sorted(wanted - values.keys())]
    problems += [f"{name}: no template uses it" for name in sorted(values.keys() - wanted)]
    for name in sorted(wanted & values.keys()):
        check = CHECKS.get(name)
        reason = "has no check in deploy/render-prod.py" if check is None else check(values[name])
        if reason is not None:
            problems.append(f"{name}: {reason}")
    if problems:
        raise RenderError("; ".join(problems))


def render(values: dict[str, str], output_dir: Path | None = None, *, force: bool = False) -> list[Path]:
    """Write the two files into ``output_dir``, by default ``<ROOT>/config`` (made 0700 if missing); their paths.
    ``RenderError`` before anything is written when a value or an existing file is in the way."""
    check_values(values)
    output_dir = (Path(values["ROOT"]) / "config" if output_dir is None else output_dir).absolute()
    if output_dir.resolve().is_relative_to(CHECKOUT):
        raise RenderError(f"{output_dir}: render outside the checkout ({CHECKOUT})")
    targets = {output_dir / name: template for name, template in TEMPLATES.items()}
    existing = [str(target) for target in targets if target.exists()]
    if existing and not force:
        raise RenderError(f"{', '.join(existing)}: exists (--force replaces it)")
    rendered = {
        target: PLACEHOLDER.sub(lambda match: values[match[1]], template.read_text())
        for target, template in targets.items()
    }
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for target, text in rendered.items():
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=output_dir)  # created 0600
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(text)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    return list(rendered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output-dir", type=Path, help="where to write (default <ROOT>/config; elsewhere: to compare with it)"
    )
    parser.add_argument("--force", action="store_true", help="replace existing files")
    parser.add_argument("values", nargs="*", metavar="NAME=value", help="a placeholder's value")
    args = parser.parse_args(argv)
    values: dict[str, str] = {}
    for item in args.values:
        name, equals, value = item.partition("=")
        if not equals or not PLACEHOLDER.fullmatch(f"{{{{{name}}}}}"):
            parser.error(f"not NAME=value: {item!r}")
        values[name] = value
    try:
        written = render(values, args.output_dir, force=args.force)
    except (RenderError, OSError) as error:
        print(f"render-prod.py: {error}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    installed = Path(values["ROOT"]) / "config"
    if written and written[0].parent != installed:
        print(f"render-prod.py: not the installed files ({installed}): compare them (diff -u)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
