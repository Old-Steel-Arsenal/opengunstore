#!/usr/bin/env python3
"""Lint the user guide (docs/guide) for the house style in docs/guide/STYLE.md.

Plain Python, no dependencies, so CI runs it next to the API-docs check:

    python3 scripts/docs/guide_lint.py            # every page, exit 1 on a finding
    python3 scripts/docs/guide_lint.py --words    # also print each page's word count

Rules (each finding prints as path:line: rule: detail):

paths       A Desk address (/app/... or /desk/...). Staff open pages from the
            sidebar or Settings, never from a typed address.
doctype     An ERPNext document type name, "doctype", or "ERPNext"/"Frappe" in
            plain text on a staff page. A name the reader must find on screen is
            allowed in bold (**Sales Invoice**), because then it is a label.
history     Design rationale or release history ("That is deliberate", "since
            version ...") on any page.
paragraph   A paragraph or list item longer than MAX_PARAGRAPH characters.
words       A staff page over STAFF_WORD_BUDGET words.
images      A file in docs/guide/screenshots that no page uses. Screenshots are
            generated from a shot list, so a stale one is deleted, not kept.

Pages under admin/ are written for the administrator and the bookkeeper: they
skip the doctype and words rules, not the others.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "guide"
MAX_PARAGRAPH = 350
STAFF_WORD_BUDGET = 1600
# Not prose pages: the style guide itself, and the glossary, which is a list of
# definitions read one entry at a time.
SKIP = {"STYLE.md"}
WORD_EXEMPT = {"help/glossary.md"}
ADMIN_PREFIX = "admin/"

PATHS = re.compile(r"(?<![\w.])/(?:app|desk)/")
DOCTYPES = re.compile(
    r"\b(?:Sales Order|Sales Invoice|Payment Entry|Delivery Note|Stock Reconciliation"
    r"|Stock Entry|Journal Entry|GL Entry|POS Closing Entry|POS Opening Entry"
    r"|doctypes?|DocTypes?|ERPNext|Frappe)\b"
)
HISTORY = re.compile(
    r"\b(?:that is deliberate|this is deliberate|is deliberate|on purpose"
    r"|since version|since v?\d+\.\d+|in earlier versions|older versions|used to be)\b",
    re.IGNORECASE,
)
BOLD = re.compile(r"\*\*[^*]+\*\*")
CODE_SPAN = re.compile(r"`[^`]*`")
LINK_TARGET = re.compile(r"\]\([^)]*\)")
HTML_TAG = re.compile(r"<[^>]+>")
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)(?:\{[^}]*\})?")
ATTR = re.compile(r"\{[^}]*\}")
LIST_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d+\.)\s+")


def _prose(text: str) -> str:
    """The words a reader reads: no link targets, tags, images or attribute lists."""
    text = IMAGE.sub("", text)
    text = LINK_TARGET.sub("]", text)
    text = HTML_TAG.sub("", text)
    return ATTR.sub("", text)


def blocks(lines: list[str]):
    """Yield (first line number, kind, text) for every paragraph and list item.

    Fenced code, tables, headings, figures and admonition markers are skipped;
    an admonition's indented body counts as paragraphs.
    """
    in_fence = False
    current: list[str] = []
    start = 0
    kind = "paragraph"

    def flush():
        nonlocal current
        if current:
            yield start, kind, " ".join(s.strip() for s in current)
        current = []

    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            yield from flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        structural = (
            not stripped
            or stripped.startswith(("#", "|", "<", "!!!", "???", "===", "---"))
            or stripped.startswith("![")
        )
        if structural:
            yield from flush()
            continue
        item = LIST_ITEM.match(line)
        if item:
            yield from flush()
            start, kind = number, "list item"
            current = [line[item.end():]]
            continue
        if not current:
            start, kind = number, "paragraph"
        current.append(line)
    yield from flush()


def word_count(lines: list[str]) -> int:
    total = 0
    in_fence = False
    for line in lines:
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(("<figure", "</figure", "![")):
            continue
        total += len(re.findall(r"[A-Za-z0-9][\w'’.-]*", _prose(line)))
    return total


def lint_page(path: Path) -> tuple[list[str], int]:
    rel = path.relative_to(GUIDE).as_posix()
    admin = rel.startswith(ADMIN_PREFIX)
    lines = path.read_text(encoding="utf-8").splitlines()
    found: list[str] = []

    in_fence = False
    for number, line in enumerate(lines, 1):
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
        if PATHS.search(LINK_TARGET.sub("]", line)):
            found.append(f"{rel}:{number}: paths: Desk address in the text")
        if in_fence:
            continue
        plain = LINK_TARGET.sub("]", line)
        if HISTORY.search(plain):
            found.append(f"{rel}:{number}: history: {HISTORY.search(plain).group(0)!r}")
        if not admin:
            unbolded = CODE_SPAN.sub("", BOLD.sub("", plain))
            hit = DOCTYPES.search(unbolded)
            if hit:
                found.append(f"{rel}:{number}: doctype: {hit.group(0)!r} in plain text")

    for number, kind, text in blocks(lines):
        size = len(_prose(text))
        if size > MAX_PARAGRAPH:
            found.append(f"{rel}:{number}: paragraph: {kind} of {size} characters (max {MAX_PARAGRAPH})")

    words = word_count(lines)
    if not admin and rel not in WORD_EXEMPT and words > STAFF_WORD_BUDGET:
        found.append(f"{rel}:1: words: {words} words (staff budget {STAFF_WORD_BUDGET})")
    return found, words


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--words", action="store_true", help="print each page's word count")
    args = parser.parse_args()

    findings: list[str] = []
    for path in sorted(GUIDE.rglob("*.md")):
        if path.name in SKIP:
            continue
        page_findings, words = lint_page(path)
        findings.extend(page_findings)
        if args.words:
            print(f"{words:6d}  {path.relative_to(GUIDE).as_posix()}")
    used = "".join(p.read_text(encoding="utf-8") for p in GUIDE.rglob("*.md") if p.name not in SKIP)
    for shot in sorted((GUIDE / "screenshots").iterdir()):
        if f"screenshots/{shot.name}" not in used:
            findings.append(f"screenshots/{shot.name}:1: images: no page uses it")
    for line in findings:
        print(line)
    if findings:
        print(f"guide_lint: {len(findings)} finding(s); see docs/guide/STYLE.md", file=sys.stderr)
        return 1
    print("guide_lint: clean", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
