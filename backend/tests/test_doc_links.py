"""``make check``'s docs link check (``scripts/check-doc-links.py``), run on a throwaway Git repository."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

INDEX = """\
# Index

## A `code` heading

## Twice

## Twice

Resolving: [a](other.md), [b](other.md#section-one), [c](#a-code-heading), [d](#twice-1), [e](sub/),
[f](https://example.org/missing.md), [g](other.md "a title"), ![h](img.png), [i][good], <a href="other.md">j</a>,
<img alt="k" src="img.png">, [a link whose text
wraps](other.md), [l](
sub/readme.md#sub), `[in a span](nowhere.md)` and <!-- [in a comment](nowhere.md) -->.

[good]: other.md

- a list item

  ```
  [in an indented fence](nowhere.md)
  ```

[missing](missing.md)

[anchor](other.md#nope) and [case](other.md#Section-One) and [duplicate](#twice-2)

[outside](../outside.md), [unpublished folder](private/), [under it](private/plan.md), [file](secret.md)

[bad]: gone.md

[a link whose text
wraps](wrapped.md)

[a target on the next line](
next-line.md)

A code span that `wraps
across lines` before [a real link](after-span.md) and `another span`.

<a href="html-href.md">a</a> <img src='html-src.png'>
"""


def test_the_docs_link_check_reads_links_across_line_breaks_and_html_and_skips_code(tmp_path: Path) -> None:
    """Every broken relative link fails, listed at the line where its target starts: a missing file, a missing or
    wrong-case anchor (``-1`` for a repeated heading, a code span in a heading), a target outside the repository or
    in an unpublished file or folder, a reference definition, link text or a target that wraps onto the next line, a
    link after a code span that wraps, and HTML ``href`` and ``src``. Links in code spans, comments and fences
    (indented in a list item too), links with a scheme and the unpublished files' own links are not checked."""
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "check-doc-links.py", checkout / "scripts" / "check-doc-links.py")
    files = {
        "index.md": INDEX,
        "other.md": "# Other\n\n## Section one\n",
        "sub/readme.md": "# Sub\n",
        "img.png": "",
        "private/plan.md": "[broken](nowhere.md)\n",
        "secret.md": "[broken](nowhere.md)\n",
    }
    for name, text in files.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    subprocess.run(["git", "init", "-q", str(checkout)], capture_output=True, timeout=60, check=True)
    command = [sys.executable, str(checkout / "scripts" / "check-doc-links.py")]
    command += ["--unpublished", "private/", "--unpublished", "secret.md"]

    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)

    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "check-doc-links: links that do not resolve:",
        "index.md:23: missing.md: no such file or folder",
        "index.md:25: other.md#nope: no heading #nope",
        "index.md:25: other.md#Section-One: no heading #Section-One",
        "index.md:25: #twice-2: no heading #twice-2",
        "index.md:27: ../outside.md: outside the repository",
        "index.md:27: private/: not published",
        "index.md:27: private/plan.md: not published",
        "index.md:27: secret.md: not published",
        "index.md:29: gone.md: no such file or folder",
        "index.md:32: wrapped.md: no such file or folder",
        "index.md:35: next-line.md: no such file or folder",
        "index.md:38: after-span.md: no such file or folder",
        "index.md:40: html-href.md: no such file or folder",
        "index.md:40: html-src.png: no such file or folder",
    ]
    (checkout / "index.md").write_text(INDEX.split("[missing]")[0])
    clean = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    assert (clean.returncode, clean.stdout, clean.stderr) == (
        0,
        "check-doc-links: every relative link resolves (3 Markdown files)\n",
        "",
    )
