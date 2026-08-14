"""Themes, and the document styles that belong to each background.

A Textual theme colours the chrome. The document pane paints its own
styles for comments, search, selection, tracked changes and markdown
markup, and those cannot be shared across backgrounds, an underlined
yellow comment is invisible on white paper. So a theme here is a pair,
a Textual `Theme` for the frame and a `DocStyles` for the pane.

`styles_for` looks the theme up by name first and falls back to its
background. The fallback sets are deliberately written in ANSI colour
names so an unknown theme borrows the terminal's own palette rather
than a foreign one, the named sets spell out hex because they are
matched to a specific background.

The default is `gruvbox-material`, the palette of the same-named nvim
colorscheme at `background = hard` and `foreground = material`, so a
manuscript looks the same here as in the editor it was written in.

The `word` theme is Word on paper, black on white with Word's title
blue for headings and its tracked-change colours. The font is the
terminal's own and no program can change it from inside, so Aptos is a
terminal profile setting, see the README.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from textual.theme import Theme

# gruvbox-material, dark, hard background, material foreground. Names
# are the colorscheme's own so the two can be diffed.
GBG_DIM = "#141617"
GBG0 = "#1d2021"
GBG1 = "#282828"
GBG2 = "#32302f"  # bg_statusline2, and the blurred selection
GBG3 = "#3c3836"  # what Visual fills with
GBG5 = "#504945"
GFG0 = "#d4be98"
GFG1 = "#ddc7a1"
GRED = "#ea6962"
GORANGE = "#e78a4e"
GYELLOW = "#d8a657"
GGREEN = "#a9b665"
GAQUA = "#89b482"
GBLUE = "#7daea3"
GPURPLE = "#d3869b"
GGREY0 = "#7c6f64"
GGREY1 = "#928374"

GRUVBOX_THEME = Theme(
    name="gruvbox-material",
    primary=GBLUE,
    secondary=GAQUA,
    accent=GYELLOW,
    foreground=GFG0,
    background=GBG0,
    surface=GBG1,
    panel=GBG2,
    warning=GORANGE,
    error=GRED,
    success=GGREEN,
    dark=True,
    variables={
        # nvim fills Visual with bg3 and leaves the foreground alone,
        # which is also what keeps the green and red words of a panel
        # entry legible where a saturated fill would swallow them
        "block-cursor-background": GBG3,
        "block-cursor-foreground": GFG1,
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": GBG2,
        "block-cursor-blurred-foreground": GFG0,
        "footer-key-foreground": GYELLOW,
        "input-selection-background": GBG5,
        "button-color-foreground": GBG0,
    },
)

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

# The groups nvim paints markdown with under gruvbox-material. Title is
# orange bold, @markup.quote links to Grey, @markup.raw to the string
# green, and the markers take the dimmer of the two greys.
GRUVBOX_MARKUP: Mapping[str, str] = {
    "marker": GGREY0,
    "heading": f"bold {GORANGE}",
    "strong": "bold",
    "emphasis": "italic",
    "strike": "strike",
    "code": GGREEN,
    "quote": f"italic {GGREY1}",
    "link": f"underline {GBLUE}",
    "cite": GPURPLE,
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

# nvim's own marks where it has one. Search is Search, the current hit
# is IncSearch, the selection is Visual. The cursor is a yellow block
# rather than nvim's reverse video, which here would be the selection
# again. A comment keeps the underline of DARK_STYLES because the two
# fills are already spoken for.
GRUVBOX_STYLES = DocStyles(
    comment=f"underline {GYELLOW}",
    search=f"{GBG0} on {GGREEN}",
    search_current=f"{GBG0} on {GRED}",
    selection=f"{GFG1} on {GBG3}",
    cursor=f"{GBG0} on {GYELLOW}",
    insertion=GGREEN,
    deletion=f"underline {GRED}",
    deleted_line=f"{GRED} strike",
    deleted_prefix=GRED,
    anchor=GYELLOW,
    markup=GRUVBOX_MARKUP,
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


# Themes whose pane styles are matched to that one background.
STYLES_BY_NAME: Mapping[str, DocStyles] = {
    "gruvbox-material": GRUVBOX_STYLES,
    "word": LIGHT_STYLES,
}


def styles_for(theme: Theme | None) -> DocStyles:
    """Pane styles for a theme, by name if it has its own, else by
    background, so every unlisted light theme still gets a readable set."""
    if theme is None:
        return DARK_STYLES
    named = STYLES_BY_NAME.get(theme.name)
    if named is not None:
        return named
    return DARK_STYLES if theme.dark else LIGHT_STYLES
