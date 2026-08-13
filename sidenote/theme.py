"""Themes, and the document styles that belong to each background.

A Textual theme colours the chrome. The document pane paints its own
styles for comments, search, selection, tracked changes and markdown
markup, and those cannot be shared across backgrounds, an underlined
yellow comment is invisible on white paper. So a theme here is a pair,
a Textual `Theme` for the frame and a `DocStyles` for the pane, picked
by whether the active theme is dark.

The `word` theme is Word on paper, black on white with Word's title
blue for headings and its tracked-change colours. The font is the
terminal's own and no program can change it from inside, so Aptos is a
terminal profile setting, see the README.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from textual.theme import Theme

# One blue means selected, in the pane and in the panels alike. Word's
# lighter accent, dark enough to read as a fill on white paper and
# still light enough to carry the green and red words of a panel entry.
SELECTED = "#9dc3e6"
SELECTED_BLURRED = "#cfe2f3"

WORD_THEME = Theme(
    name="word",
    primary="#2b579a",  # the Word title bar
    secondary="#41729f",
    accent="#0563c1",  # hyperlink blue
    foreground="#000000",
    background="#ffffff",
    surface="#f3f2f1",  # the ribbon
    panel="#e1dfdd",
    warning="#bf8f00",
    error="#c00000",
    success="#107c10",
    dark=False,
    variables={
        # the selected panel entry. A saturated blue would swallow the
        # green and red words in it, so the fill carries black text
        "block-cursor-background": SELECTED,
        "block-cursor-foreground": "#000000",
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": SELECTED_BLURRED,
        "block-cursor-blurred-foreground": "#000000",
        "footer-key-foreground": "#2b579a",
        "input-selection-background": SELECTED,
        "button-color-foreground": "#ffffff",
    },
)

# markdown kind -> style, keyed by what `markdown.markup_spans` returns.
# A kind left out of the mapping is simply not styled.
DARK_MARKUP: Mapping[str, str] = {
    "marker": "dim",
    "heading": "bold bright_blue",
    "strong": "bold",
    "emphasis": "italic",
    "strike": "strike",
    "code": "bright_cyan",
    "quote": "italic bright_black",
    "link": "underline bright_blue",
    "cite": "bright_magenta",
}

LIGHT_MARKUP: Mapping[str, str] = {
    "marker": "#a6a6a6",
    "heading": "bold #2b579a",
    "strong": "bold",
    "emphasis": "italic",
    "strike": "strike",
    "code": "#a31515",
    "quote": "italic #595959",
    "link": "underline #0563c1",
    "cite": "#7030a0",
}


@dataclass(frozen=True)
class DocStyles:
    """Rich styles for the document pane and the sidebar's anchor text."""

    comment: str
    search: str
    search_current: str
    selection: str
    cursor: str
    insertion: str
    deletion: str
    deleted_line: str
    deleted_prefix: str
    anchor: str
    markup: Mapping[str, str]


DARK_STYLES = DocStyles(
    comment="underline yellow",
    search="black on green",
    search_current="black on orange1",
    selection="reverse",
    cursor="black on bright_yellow",
    insertion="green",
    deletion="underline red",
    deleted_line="red strike",
    deleted_prefix="red",
    anchor="yellow",
    markup=DARK_MARKUP,
)

# Word's own marks. Commented text takes a pale yellow fill, tracked
# insertions are underlined in the author colour, deletions struck in
# red, and the cursor is a block in the title blue.
LIGHT_STYLES = DocStyles(
    comment="black on #ffe9a8",
    search="black on #b6d7a8",
    search_current="black on #f6b26b",
    selection=f"black on {SELECTED}",
    cursor="white on #2b579a",
    insertion="underline #0563c1",
    deletion="underline #c00000",
    deleted_line="#c00000 strike",
    deleted_prefix="#c00000",
    anchor="#7f6000",
    markup=LIGHT_MARKUP,
)


def styles_for(theme: Theme | None) -> DocStyles:
    """Pane styles for a theme, chosen by its background, not its name."""
    return DARK_STYLES if theme is None or theme.dark else LIGHT_STYLES
