"""Linear scans for `<tag ...>...</tag>` blocks in raw HTML.

The regex `<script\\b[^>]*>(.*?)</script\\s*>` is quadratic on unclosed tags: for each
`<script` that never closes it rescans to the end of the document, so 160 KB of bare
`<script>` tags took 15 s per pass -- synchronously, on the worker's event loop, with
every other domain stalled behind it and no way for the domain timeout to interrupt.
These scanners walk the document once. An unclosed block runs to the end of the
document, which is how a browser reads it too.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

_EMAILISH = re.compile(r"@[a-z0-9-]+\.[a-z]{2,}", re.I)


@dataclass(frozen=True)
class Block:
    start: int        # index of '<'
    body_start: int   # first index after the opening tag's '>'
    body_end: int     # index of the closing '</'
    end: int          # first index after the closing tag's '>'
    attrs: str        # the opening tag's attribute text


def iter_blocks(html: str, tag: str) -> Iterator[Block]:
    """Every `<tag ...>...</tag>` in document order, each found in one forward pass."""
    low = html.lower()
    open_needle = "<" + tag
    close_needle = "</" + tag
    pos = 0
    n = len(html)
    while True:
        start = low.find(open_needle, pos)
        if start < 0:
            return
        after = start + len(open_needle)
        # `<svg` must not match `<svgfoo`; the tag name ends at whitespace, '/' or '>'.
        if after < n and html[after] not in " \t\r\n/>":
            pos = after
            continue
        gt = html.find(">", after)
        if gt < 0:
            return
        body_start = gt + 1
        close = low.find(close_needle, body_start)
        if close < 0:
            yield Block(start, body_start, n, n, html[after:gt])
            return
        close_gt = html.find(">", close + len(close_needle))
        end = n if close_gt < 0 else close_gt + 1
        yield Block(start, body_start, close, end, html[after:gt])
        pos = end


def strip_blocks(
    html: str, tag: str, keep: Callable[[Block, str], bool] | None = None
) -> str:
    """Replace each block with a space, except those `keep(block, body)` says to leave."""
    parts: list[str] = []
    last = 0
    for b in iter_blocks(html, tag):
        parts.append(html[last:b.start])
        kept = keep is not None and keep(b, html[b.body_start:b.body_end])
        parts.append(html[b.start:b.end] if kept else " ")
        last = b.end
    if not parts:
        return html
    parts.append(html[last:])
    return "".join(parts)


def bodies(html: str, tag: str) -> list[str]:
    return [html[b.body_start:b.body_end] for b in iter_blocks(html, tag)]


def looks_emailish(body: str) -> bool:
    return "@" in body and _EMAILISH.search(body) is not None
