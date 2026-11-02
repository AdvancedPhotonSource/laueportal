#!/usr/bin/env python3
"""`make check`'s docs link check: every relative link in a Markdown file of the repository resolves.

    check-doc-links.py [--unpublished PATH]...

A link resolves when its target (relative to the file, or to the repository's root when it starts with `/`) is a file
or folder of the repository (tracked, or new and not ignored) and its `#fragment`, if any, is a heading of the target
Markdown file (GitHub's anchors). Inline links, images, reference definitions and HTML `href` and `src` attributes are
read, paragraph by paragraph, so a link whose text or target wraps onto the next line counts; code blocks, code spans
(a span may wrap too) and HTML comments are not. Links with a scheme (`https:`, `mailto:`) are not checked.

--unpublished PATH (repeatable): a file, or a folder when PATH ends with `/`, that stays out of the published tree:
its own Markdown files are not checked, and a link from any other file to it, or to anything under it, fails.

Exits 1 and lists every failing link as FILE:LINE: TARGET: REASON (LINE is where the target starts). Standard library
only.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

INLINE = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*(<[^>\n]*>|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)(?:\s+[\"'(][^)]*)?\)")
REFERENCE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^>\n]*>|\S+)", re.MULTILINE)
TAG = re.compile(r"<[A-Za-z][A-Za-z0-9-]*\b[^>]*>")
ATTRIBUTE = re.compile(r"\s(?:href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))", re.IGNORECASE)
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
BLOCK_START = re.compile(r"^\s*(?:#{1,6}(?:\s|$)|[-+*]\s|\d{1,9}[.)]\s|\||>)")
HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def repository_files(root: Path) -> set[str]:
    """Tracked files and new files Git does not ignore, those that exist (a deleted file is gone)."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return {name for name in out.split("\0") if name and (root / name).is_file()}


def prose_lines(text: str) -> Iterator[tuple[int, str]]:
    """(line number, text) of every line outside a fenced code block (a fence may be indented, as in a list item)."""
    fence = None
    for number, line in enumerate(text.split("\n"), 1):
        opened = FENCE.match(line)
        if fence:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            continue
        if opened:
            fence = opened.group(1)
            continue
        yield number, line


def paragraphs(text: str) -> Iterator[tuple[int, str]]:
    """(first line number, text) of every block of prose: consecutive lines outside fences, joined with their line
    breaks, split at blank lines and where a heading, a list item, a table row or a quote starts."""
    first, block, last = 0, [], 0
    for number, line in prose_lines(text):
        if block and (number != last + 1 or not line.strip() or BLOCK_START.match(line)):
            yield first, "\n".join(block)
            block = []
        if line.strip():
            if not block:
                first = number
            block.append(line)
        last = number
    if block:
        yield first, "\n".join(block)


def blank_code_spans(text: str) -> str:
    """The text with each code span (a run of backticks up to the next run of the same length, across line breaks)
    and each HTML comment replaced by spaces, its line breaks kept."""

    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    text = COMMENT.sub(blank, text)
    out, at = [], 0
    for run in re.finditer(r"(?<!\\)`+", text):
        if run.start() < at:
            continue
        close = next((m for m in re.finditer(r"`+", text[run.end() :]) if len(m.group(0)) == len(run.group(0))), None)
        if close is None:
            continue
        end = run.end() + close.end()
        out.append(text[at : run.start()])
        out.append(re.sub(r"[^\n]", " ", text[run.start() : end]))
        at = end
    out.append(text[at:])
    return "".join(out)


def links(text: str) -> Iterator[tuple[int, str]]:
    """(line number, target) of every link of a Markdown text, outside code."""
    for first, block in paragraphs(text):
        prose = blank_code_spans(block)
        found = [(m.start(1), m.group(1)) for m in INLINE.finditer(prose)]
        found += [(m.start(1), m.group(1)) for m in REFERENCE.finditer(prose)]
        for tag in TAG.finditer(prose):
            for attribute in ATTRIBUTE.finditer(tag.group(0)):
                group = next(g for g in (1, 2, 3) if attribute.group(g) is not None)
                found.append((tag.start() + attribute.start(group), attribute.group(group)))
        for offset, raw in sorted(found):
            yield first + prose.count("\n", 0, offset), raw[1:-1] if raw.startswith("<") else raw


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: its text, lower case, without punctuation, spaces as hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    found = set()
    for _, line in prose_lines(path.read_text(encoding="utf-8")):
        match = HEADING.match(line)
        if not match:
            continue
        base = slug(match.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def under(name: str, unpublished: list[str]) -> bool:
    return any(name == p.rstrip("/") or (p.endswith("/") and name.startswith(p)) for p in unpublished)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--unpublished", action="append", default=[], metavar="PATH")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    files = repository_files(root)
    folders = {posixpath.dirname(f) for f in files}
    folders |= {posixpath.dirname(d) for d in list(folders)}
    while True:
        more = {posixpath.dirname(d) for d in folders} - folders
        if not more:
            break
        folders |= more
    unpublished = [p.removeprefix("./") for p in args.unpublished]
    failures = []
    checked = 0
    for source in sorted(f for f in files if f.endswith(".md") and not under(f, unpublished)):
        checked += 1
        for number, target in links((root / source).read_text(encoding="utf-8")):
            if not target or SCHEME.match(target):
                continue
            path, _, fragment = target.partition("#")
            if path.startswith("/"):
                name = posixpath.normpath(path.lstrip("/"))
            elif path:
                name = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
            else:
                name = source
            problem = None
            if name.startswith("../") or name == "..":
                problem = "outside the repository"
            elif under(name, unpublished) or (name in folders and under(name + "/", unpublished)):
                problem = "not published"
            elif name != "." and name not in files and name not in folders:
                problem = "no such file or folder"
            elif fragment and name.endswith(".md") and fragment not in anchors(root / name):
                problem = f"no heading #{fragment}"
            if problem:
                failures.append(f"{source}:{number}: {target}: {problem}")
    if failures:
        print("check-doc-links: links that do not resolve:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"check-doc-links: every relative link resolves ({checked} Markdown files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
