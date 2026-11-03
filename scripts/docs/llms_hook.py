"""Publish machine-readable copies of the guide alongside the rendered site.

An agent reading documentation wants the prose, not the navigation shell, the
search worker and the theme's CSS. Three artifacts come out of every build:

``/llms.txt``
    An index in the `llms.txt <https://llmstxt.org/>`_ shape: what this site is,
    then every page as a link with a one-line summary, in navigation order. Small
    enough to read first and decide what else to fetch.

``/llms-full.txt``
    Every page concatenated, each under its own heading and canonical URL. One
    fetch for the whole manual when the reader has the context budget for it.

``<any page>.md``
    The Markdown source of that page, at the page's own URL plus ``.md``. Lets a
    reader that already found a page take the clean copy instead of parsing HTML.

All three are generated, never hand-maintained, so they cannot drift from the
pages. Nothing here changes the rendered site.
"""
from __future__ import annotations

import re
from pathlib import Path

# Pages are written for people, so they open with an H1 and a sentence or two
# saying who does this and when. That sentence is the summary; nothing separate
# has to be maintained for the index.
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_FIGURE = re.compile(r"<figure markdown>.*?</figure>", re.DOTALL)
_FIGCAPTION = re.compile(r"<figcaption>(.*?)</figcaption>", re.DOTALL)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)(?:\{[^}]*\})?")
_ADMONITION_MARKER = re.compile(r"^!!!\s+\w+(?:\s+\"[^\"]*\")?\s*$", re.MULTILINE)

_nav_pages: list = []


def on_nav(nav, config, files):
    """Remember reading order. The nav is the author's ordering; a directory
    walk is not, and an index that lists Troubleshooting before Getting Started
    is worse than no index."""
    global _nav_pages
    _nav_pages = [page for page in nav.pages if page.file.src_path.endswith(".md")]
    return nav


def _title(markdown: str, fallback: str) -> str:
    found = _H1.search(markdown)
    return found.group(1) if found else fallback


def _summary(markdown: str) -> str:
    """The page's opening sentence, flattened to one line.

    Skips the H1 and anything that is not prose: a figure, an admonition
    marker, a table row, a list bullet. Stops at the first blank line after
    prose starts, so this is the lede paragraph rather than the whole page.
    """
    body = _FIGURE.sub("", markdown)
    body = _ADMONITION_MARKER.sub("", body)
    collected: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            if collected:
                break
            continue
        # A bullet needs the space: `**Pending Order** is the one screen…` is
        # prose that happens to open in bold, and dropping it took the summary
        # from the middle of the paragraph.
        is_structure = (
            line.startswith(("#", "|", ">", "<", "```", "==="))
            or re.match(r"^[-*+]\s", line)
            or re.match(r"^\d+\.\s", line)
        )
        if is_structure:
            if collected:
                break
            continue
        collected.append(line)
    text = " ".join(collected)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # links -> their text
    text = re.sub(r"[*`]", "", text)                        # bold, code ticks
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 300:
        cut = text[:300].rsplit(". ", 1)[0]
        text = (cut + ".") if cut else text[:300].rstrip() + "…"
    return text


def _page_url(config, page) -> str:
    return config["site_url"].rstrip("/") + "/" + page.url


def on_post_build(config):
    docs_dir = Path(config["docs_dir"])
    site_dir = Path(config["site_dir"])
    site_url = config["site_url"].rstrip("/")

    entries = []
    for page in _nav_pages:
        source = docs_dir / page.file.src_path
        if not source.is_file():
            continue
        markdown = source.read_text(encoding="utf-8")
        entries.append({
            "page": page,
            "src": page.file.src_path,
            "url": _page_url(config, page),
            "title": _title(markdown, page.title or page.file.src_path),
            "summary": _summary(markdown),
            "markdown": markdown,
        })

        # The page's own Markdown at its URL plus ".md": guide/selling/pos/ ->
        # guide/selling/pos.md, and a section index such as extensions/ ->
        # extensions.md. The site root keeps index.md.
        stem = page.url.rstrip("/")
        target = site_dir / (f"{stem}.md" if stem else "index.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")

    _write_index(site_dir, site_url, config, entries)
    _write_full(site_dir, site_url, config, entries)


def _section_of(src_path: str) -> str:
    parts = src_path.split("/")
    if parts[0] == "guide":
        # The user guide is one nav tab; group its pages by the guide's own sections.
        head = parts[1] if len(parts) > 2 else "guide"
    else:
        head = parts[0]
    if head.endswith(".md"):
        # Pages at the docs root belong to different nav tabs.
        return {
            "self-hosting.md": "Operations",
            "deploy-with-ai.md": "Operations",
            "extensions-runbook.md": "Operations",
            "versions.md": "Operations",
            "data-service.md": "Developers",
            "testing.md": "Developers",
        }.get(head, "Start here")
    return {
        "guide": "User guide",
        "getting-started": "Getting started",
        "selling": "Selling",
        "inventory": "Inventory",
        "compliance": "Compliance",
        "channels": "Online channels",
        "distributors": "Distributors",
        "reports": "Reports",
        "settings": "Settings",
        "reference": "Reference",
        "developers": "Developers",
        "extensions": "Extensions",
        "ai": "For AI agents",
    }.get(head, head.replace("-", " ").capitalize())


def _write_index(site_dir: Path, site_url: str, config, entries) -> None:
    lines = [
        f"# {config['site_name']}",
        "",
        f"> {config['site_description']}",
        "",
        "OpenGunstore is an open-source (GPLv3) point of sale and back office for "
        "US federal firearms licensees, built on Frappe and ERPNext v16. These docs "
        "have three parts: the user guide (selling at the counter, receiving "
        "inventory, the acquisition-and-disposition record, Form 4473 and NICS, the "
        "WooCommerce and GunBroker channels, distributors, reports and settings), "
        "operations (self-hosting, the extensions runbook), and the developer docs "
        "(architecture, building extensions, the MCP server, testing). Source: https://github.com/Old-Steel-Arsenal/opengunstore",
        "",
        "Every page is also available as Markdown at its own URL plus `.md`. "
        f"All of it as one file is at {site_url}/llms-full.txt. The HTTP API is "
        f"described by {site_url}/api/openapi.json.",
        "",
        "Anything describing a firearm transfer, the bound book, Form 4473 or a "
        "background check states a legal duty as well as a workflow. Read those "
        "pages as constraints, not as options.",
        "",
    ]
    # One heading per section, in the order the nav first reaches it.
    sections: dict[str, list[str]] = {}
    for entry in entries:
        summary = f": {entry['summary']}" if entry["summary"] else ""
        sections.setdefault(_section_of(entry["src"]), []).append(
            f"- [{entry['title']}]({entry['url']}){summary}")
    for section, items in sections.items():
        lines += [f"## {section}", "", *items, ""]
    (site_dir / "llms.txt").write_text("\n".join(lines), encoding="utf-8")


def _write_full(site_dir: Path, site_url: str, config, entries) -> None:
    lines = [
        f"# {config['site_name']}",
        "",
        f"> {config['site_description']}",
        "",
        f"The complete manual, {len(entries)} pages in reading order. Generated "
        f"from {site_url}/ on every build; the index is at {site_url}/llms.txt.",
        "",
        "---",
        "",
    ]
    for entry in entries:
        lines += [
            f"<!-- source: {entry['src']} -->",
            f"# {entry['title']}",
            "",
            f"Canonical URL: {entry['url']}",
            "",
        ]
        # Demote the page's own H1: it is already the heading above, and two of
        # them in one file makes the outline lie about the structure.
        body = _H1.sub(lambda m: f"## {m.group(1)}", entry["markdown"], count=1)
        body = re.sub(r"^#(?=\s)", "##", body, flags=re.MULTILINE)
        body = _screenshots_as_text(body)
        lines += [body.strip(), "", "---", ""]
    (site_dir / "llms-full.txt").write_text("\n".join(lines), encoding="utf-8")


def _screenshots_as_text(markdown: str) -> str:
    """One line of text per screenshot. llms-full.txt sits at the docs root, where
    the pages' relative image paths point nowhere, and the figure HTML is noise;
    the caption (or the alt text when there is none) is what a reader can use."""
    def figure(m):
        found = _FIGCAPTION.search(m.group(0)) or _IMAGE.search(m.group(0))
        return f"*Screenshot: {found.group(1).strip()}*" if found else ""

    body = _FIGURE.sub(figure, markdown)
    return _IMAGE.sub(lambda m: f"*Screenshot: {m.group(1).strip()}*", body)


assert _screenshots_as_text(
    '<figure markdown>\n![Alt](../screenshots/a.webp){ .screenshot }\n'
    "<figcaption>The caption.</figcaption>\n</figure>\n\n![Bare](../screenshots/b.webp)"
) == "*Screenshot: The caption.*\n\n*Screenshot: Bare*"
