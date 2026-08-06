"""Regenerate docs/screenshot.svg.

Builds the sample document, adds the two comments the picture shows,
drives the TUI to the state it was captured in, and strips the fake
macOS window chrome that save_screenshot draws around the terminal.

    .venv/bin/python tools/make_screenshot.py

Run it whenever a key, the status bar or the footer changes.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import tempfile
from pathlib import Path

# the picture shows the default theme, whatever the shell asks for
os.environ.pop("TEXTUAL_THEME", None)

from sidenote.cli import make_sample
from sidenote.engine import OdtReview
from sidenote.tui import ReviewApp

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshot.svg"

# 110x30 cells. The document pane keeps 110 - PANEL_WIDTH columns.
SIZE = (110, 30)
# fixed so regenerating does not churn the dates in the diff
DATE = "2026-07-07T09:14:00"
# (paragraph, start, end, author, text) for the two sidebar entries
SHOWN_COMMENTS = [
    (1, 9, 22, "Richard", "check the FFQ item wording against NNR 2023"),
    (3, 26, 34, "Anna", "tertiles or quartiles? justify the cut-offs"),
]
# the visual selection runs from the head of this display line to here
CURSOR = (1, 87)


def build_document(path: Path) -> None:
    make_sample(path)
    review = OdtReview(path)
    for para, start, end, author, text in SHOWN_COMMENTS:
        comment = review.add_comment(
            (para, start), (para, end), text, author=author
        )
        for child in comment.node.childNodes:
            if child.qname[1] == "date":
                child.firstChild.data = DATE
    review.save()


def capture(path: Path) -> tuple[str, str]:
    """Drive the TUI to the pictured state. Returns the SVG and its bg."""

    async def scenario() -> tuple[str, str]:
        app = ReviewApp(path)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("s")  # open the comments sidebar
            await pilot.pause()
            await pilot.press("escape")  # focus back on the document
            await pilot.pause()
            # visual selection from the head of the cursor's display line
            app.cur = CURSOR
            app.anchor = (CURSOR[0], app.lines[app._cursor_line()].start)
            app._repaint()
            await pilot.pause()
            return app.export_screenshot(), app.screen.background_colors[1].hex

    return asyncio.run(scenario())


def strip_chrome(svg: str, background: str) -> str:
    """Drop the window frame so the SVG is just the terminal content.

    save_screenshot draws a rounded background rect, a title and three
    traffic lights around the content, and offsets the terminal group to
    make room. Removing them means the viewBox, the clip rect and the
    group transform all have to come back to the content box. The width
    comes from the per-line clip rects, the height from the painted cell
    rects, which unlike the clip rects also cover the footer row.
    """
    lines = re.findall(
        r'<rect x="0" y="[\d.]+" width="(\d+)" height="[\d.]+"/>', svg
    )
    cells = re.findall(
        r'<rect fill="#[0-9a-f]+" x="[\d.]+" y="([\d.]+)" '
        r'width="[\d.]+" height="([\d.]+)" shape-rendering="crispEdges"/>',
        svg,
    )
    if not lines or not cells:
        raise SystemExit("could not measure the terminal content box")
    width = int(lines[0])
    height = math.ceil(max(float(y) + float(h) for y, h in cells))

    old = re.search(
        r'<clipPath id="(terminal-\d+)-clip-terminal">\s*'
        r'(<rect x="0" y="0" width="[\d.]+" height="[\d.]+" />)',
        svg,
    )
    if old is None:
        raise SystemExit("could not find the terminal clip rect")
    prefix = old.group(1)

    svg = re.sub(
        r'viewBox="[^"]*"', f'viewBox="0 0 {width} {height}"', svg, count=1
    )
    svg = re.sub(r"\s*\.%s-title \{[^}]*\}\n" % prefix, "", svg, count=1)
    svg = svg.replace(
        old.group(2),
        f'<rect x="0" y="0" width="{width}" height="{height}" />',
        1,
    )
    # the frame rect, the title and the traffic lights, up to the group
    svg = re.sub(
        r'<rect fill="#292929".*?<g transform="translate\([^)]*\)" '
        r'clip-path="url\(#%s-clip-terminal\)">' % prefix,
        f'<rect fill="{background}" x="0" y="0" width="{width}" '
        f'height="{height}"/>\n\n    <g transform="translate(0, 0)" '
        f'clip-path="url(#{prefix}-clip-terminal)">',
        svg,
        count=1,
        flags=re.DOTALL,
    )
    return svg


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shot.odt"
        build_document(path)
        svg, background = capture(path)
    OUT.write_text(strip_chrome(svg, background), encoding="utf-8")
    print(f"wrote {OUT} ({len(svg)} bytes before stripping)")


if __name__ == "__main__":
    main()
