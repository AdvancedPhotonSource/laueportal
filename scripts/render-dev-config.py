"""Write the development settings (`make setup`, step 9): deploy/config/dev.toml.template with its placeholders
filled in, {{WORKSPACE}} with the development workspace and {{CHECKOUT}} with the checkout.

    python scripts/render-dev-config.py <workspace> <checkout> <output>

Both paths must be absolute. The output must not exist (it is the developer's copy to edit); it is written private
(0600). Standard library only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "config" / "dev.toml.template"


def toml_string_content(value: str) -> str:
    """``value`` as the inside of a TOML basic string (the placeholders sit inside double quotes)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render(workspace: Path, checkout: Path, template: Path = TEMPLATE) -> str:
    for path in (workspace, checkout):
        if not path.is_absolute():
            raise ValueError(f"{path}: not an absolute path")
    text = template.read_text()
    for name, value in (("WORKSPACE", workspace), ("CHECKOUT", checkout)):
        text = text.replace(f"{{{{{name}}}}}", toml_string_content(str(value)))
    if "{{" in text:
        raise ValueError(f"{template}: a placeholder other than {{{{WORKSPACE}}}} and {{{{CHECKOUT}}}}")
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: render-dev-config.py <workspace> <checkout> <output>", file=sys.stderr)
        return 2
    workspace, checkout, output = map(Path, argv)
    try:
        text = render(workspace, checkout)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except (ValueError, OSError) as error:
        print(f"render-dev-config.py: {error}", file=sys.stderr)
        return 1
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
