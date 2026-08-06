"""Textual TUI for reviewing ODT documents.

Vim-style navigation over a read-only rendering of the document, with a
character-level visual mode for selecting the span a comment anchors to.

Performance model. The wrap layout (display line -> paragraph slice) is
computed once per resize or document change and cached in
ReviewApp.lines. The document pane is a line-API ScrollView that styles
and paints only the visible lines each frame, so keystrokes cost
O(viewport), not O(document).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape
from rich.segment import Segment
from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Size
from textual.reactive import Reactive
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static, TextArea

from sidenote.engine import Comment, Pos, TrackedChange, open_review
from sidenote.refcheck import (
    ReferenceCheck,
    RefEntry,
    find_reference_check,
    load_reference_check,
    open_pdf,
)

WORD_RE = re.compile(r"[\wÀ-ɏ]+")
# a pandoc citation key in the source, @smithFolateIntake2020
CITATION_RE = re.compile(r"@([A-Za-z0-9_][\w.:#$%&+?<>~/-]*)")
# vim's sentence rule. . ! or ? then any closing brackets or quotes,
# followed by whitespace or the end of the paragraph.
SENTENCE_END_RE = re.compile(r"[.!?][)\]\"'’”]*(?=\s|$)")
# f/F/t/T reversed, for ,
FIND_REVERSE = {"f": "F", "F": "f", "t": "T", "T": "t"}

STYLE_COMMENT = "underline yellow"
STYLE_SEARCH = "black on green"
STYLE_SEARCH_CURRENT = "black on orange1"
STYLE_SELECTION = "reverse"
STYLE_CURSOR = "black on bright_yellow"
STYLE_INSERTION = "green"
STYLE_DELETION = "underline red"

# reference-check status word -> colour in the reference overlay
STATUS_STYLES = {"OK": "green", "FIXED": "cyan", "WEAK": "yellow"}

# Side panel width in columns, and the range ctrl+left/right moves it in.
PANEL_WIDTH = 44
PANEL_MIN_WIDTH = 20
PANEL_STEP = 4
# columns the document pane keeps for itself when the panel grows
DOC_MIN_WIDTH = 30

HELP_TEXT = """\
[b]Movement[/b]
  j k h l        line down/up, char left/right
  w b e          word start forward/back, word end
  f F {ch}       jump to next/previous {ch} in the paragraph
  t T {ch}       stop just before/after it
  ; ,            repeat the last f/F/t/T, forward/back
  ( )            previous/next sentence
  0 $            start/end of display line
  { }            previous/next paragraph
  gg G           first/last paragraph
  ctrl+d ctrl+u  half page down/up
  ctrl+e ctrl+y  scroll view one line
  zz zt zb       cursor line to center/top/bottom

[b]Comments[/b]
  v              visual mode (character-level selection)
  is as          select sentence, without/with trailing space
  iw aw          select word, without/with trailing space
  ip ap          select the whole paragraph
  c              comment on selection, or point note at cursor
  m              edit comment under cursor
  d              delete comment under cursor
  ] \\[            jump to next/previous comment
  s              open and focus comments sidebar / close it
  ctrl+left/right move the divider, resizing panel and text
  With the sidebar open, the cursor moving onto commented
  text highlights that comment in the panel.

[b]Sidebar (when focused)[/b]
  j k g G        move selection, first/last
  enter          jump to the comment in the document
  m d            edit/delete the selected comment
  /              filter comments by author or text
  n N            next/previous comment (wraps)
  * #            filter to selected comment's author, step through
  escape         clear the filter, then back to document
  tab            back to the document

[b]References[/b]
  r              what reference-check.md says about the citation
                 under the cursor, with its supporting quote
  n N            other keys of a grouped citation
  o              open the full-text pdf for the shown key
  j k            scroll the overlay, escape closes it

[b]Tracked changes[/b]
  S              open and focus changes panel / close it
  > <            jump to next/previous tracked change
  D              show/hide deleted text lines
  Insertions show green. Deleted text appears as red
  struck-through lines at the spot it was removed, and in
  the status bar when the cursor is on the red mark.

[b]Search[/b]
  /              search (smartcase)
  n N            next/previous match (wraps, count in status bar)
  * #            search word under cursor forward/backward

[b]Other[/b]
  X              export to docx
  escape         leave visual mode / clear search
  q              quit

Comments save to the ODT immediately. In the comment box,
enter inserts a newline, ctrl+s saves, escape cancels.
This help scrolls with j/k, escape or q closes it.\
"""


@dataclass
class Line:
    """One display line. Maps back to a slice of a source paragraph."""

    para: int
    start: int
    end: int


@dataclass
class DelLine:
    """A virtual display line showing deleted text at its position.

    Carries no document mapping. The cursor skips these lines, so
    they never participate in offsets or comment anchoring.
    """

    text: str


def wrap_offsets(text: str, width: int) -> list[tuple[int, int]]:
    """Greedy word wrap. Returns (start, end) slices into text."""
    width = max(width, 8)
    spans: list[tuple[int, int]] = []
    for seg_start, seg_text in _split_hard_lines(text):
        pos = 0
        while True:
            remaining = seg_text[pos:]
            if len(remaining) <= width:
                spans.append((seg_start + pos, seg_start + len(seg_text)))
                break
            cut = remaining.rfind(" ", 1, width + 1)
            cut = cut + 1 if cut > 0 else width
            spans.append((seg_start + pos, seg_start + pos + cut))
            pos += cut
    return spans or [(0, 0)]


def sentence_spans(text: str) -> list[tuple[int, int, int]]:
    """Split a paragraph into sentences.

    Returns (start, body_end, end) per sentence, covering the whole
    text. body_end stops at the terminator, end includes the trailing
    whitespace, which is the difference between `is` and `as`.
    """
    spans: list[tuple[int, int, int]] = []
    start = 0
    for m in SENTENCE_END_RE.finditer(text):
        if m.end() <= start:
            continue
        end = m.end()
        while end < len(text) and text[end].isspace():
            end += 1
        spans.append((start, m.end(), end))
        start = end
    if start < len(text):
        spans.append((start, len(text), len(text)))
    return spans or [(0, 0, 0)]


def sentence_at(text: str, offset: int) -> tuple[int, int, int]:
    """The sentence containing offset. Clamps past the end of the text."""
    spans = sentence_spans(text)
    for span in spans:
        if span[0] <= offset < span[2]:
            return span
    return spans[-1]


def word_object(text: str, offset: int, around: bool) -> tuple[int, int]:
    """vim iw/aw. The word at offset, or the run of non-word characters.

    `around` takes the trailing whitespace with it, or the leading
    whitespace when there is none to the right, as vim does.
    """
    for m in WORD_RE.finditer(text):
        if m.start() <= offset < m.end():
            lo, hi = m.start(), m.end()
            break
    else:
        lo, hi = offset, min(offset + 1, len(text))
        while lo > 0 and not WORD_RE.match(text[lo - 1]):
            lo -= 1
        while hi < len(text) and not WORD_RE.match(text[hi]):
            hi += 1
        return lo, hi
    if around:
        pad = hi
        while pad < len(text) and text[pad] in " \t":
            pad += 1
        if pad > hi:
            hi = pad
        else:
            while lo > 0 and text[lo - 1] in " \t":
                lo -= 1
    return lo, hi


def bracket_span(text: str, offset: int) -> tuple[int, int] | None:
    """The [...] group around offset, as (inner start, inner end).

    Used to treat `[@a; @b]` as one citation, so the cursor anywhere in
    the group reaches every key in it.
    """
    lo = text.rfind("[", 0, offset + 1)
    if lo == -1 or text.find("]", lo, offset) != -1:
        return None
    hi = text.find("]", offset)
    if hi == -1 or text.find("[", offset + 1, hi) != -1:
        return None
    return lo + 1, hi


def citations_at(text: str, offset: int) -> list[str]:
    """Citation keys of the citation at the cursor, that one first.

    Inside a bracketed group every key in the group comes along, so a
    multi-source citation can be stepped through without moving the
    cursor. Returns an empty list when the cursor is not on a citation.
    """
    hits = [
        (m.start(), m.end(), m.group(1).rstrip(".,;:"))
        for m in CITATION_RE.finditer(text)
    ]
    if not hits:
        return []
    at_cursor = next((h for h in hits if h[0] <= offset < h[1]), None)
    span = bracket_span(text, offset)
    group = [h for h in hits if span[0] <= h[0] < span[1]] if span else []
    if at_cursor is not None and at_cursor not in group:
        group = [at_cursor]
    if not group:
        return []
    keys = [h[2] for h in group]
    if at_cursor is not None and at_cursor[2] in keys:
        cut = keys.index(at_cursor[2])
        keys = keys[cut:] + keys[:cut]
    return keys


def _split_hard_lines(text: str) -> list[tuple[int, str]]:
    out = []
    start = 0
    for part in text.split("\n"):
        out.append((start, part))
        start += len(part) + 1
    return out


class CommentInput(ModalScreen[str | None]):
    """Multi-line comment editor. ctrl+s submits, escape cancels."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "submit", "save"),
    ]

    def __init__(
        self, anchor_preview: str, initial: str = "", byline: str = ""
    ):
        super().__init__()
        self.anchor_preview = anchor_preview
        self.initial = initial
        self.byline = byline

    def compose(self) -> ComposeResult:
        header = f"[b]Comment on[/b]\n{escape(self.anchor_preview)}"
        if self.byline:
            header += f"\n[dim]{escape(self.byline)}[/dim]"
        with Vertical(id="comment-dialog"):
            yield Static(header, id="comment-anchor")
            yield TextArea(self.initial, id="comment-text")
            yield Label("[dim]ctrl+s save · escape cancel[/dim]")

    def on_mount(self) -> None:
        area = self.query_one(TextArea)
        area.focus()
        area.move_cursor(area.document.end)

    def action_submit(self) -> None:
        self.dismiss(self.query_one(TextArea).text.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SearchInput(ModalScreen[str | None]):
    """Search prompt. Enter submits, escape cancels."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, placeholder: str = "search"):
        super().__init__()
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Input(placeholder=self.placeholder, id="search-text")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Key reference. Scrolls with j/k, escape or q closes."""

    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("q", "close", show=False),
        Binding("question_mark", "close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Static(HELP_TEXT)

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def on_key(self, event) -> None:
        scroll = self.query_one(VerticalScroll)
        if event.character == "j":
            scroll.scroll_to(y=int(scroll.scroll_offset.y) + 1, animate=False)
            event.stop()
        elif event.character == "k":
            scroll.scroll_to(y=int(scroll.scroll_offset.y) - 1, animate=False)
            event.stop()

    def action_close(self) -> None:
        self.dismiss(None)


class ReferenceScreen(ModalScreen[None]):
    """What the reference-check file says about a citation.

    Shows one key at a time, every row of the table that cites it, with
    `n`/`N` stepping through the other keys of a grouped citation and
    `o` opening the full text.
    """

    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("q", "close", show=False),
        Binding("o", "open_pdf", show=False),
    ]

    def __init__(self, check: ReferenceCheck, keys: list[str]):
        super().__init__()
        self.check = check
        self.keys = keys
        self.index = 0

    @property
    def key(self) -> str:
        return self.keys[self.index]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="reference-dialog"):
            yield Static(id="reference-body")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()
        self._refresh_body()

    def _refresh_body(self) -> None:
        scroll = self.query_one(VerticalScroll)
        title = "reference"
        if len(self.keys) > 1:
            title += f" {self.index + 1}/{len(self.keys)}"
        scroll.border_title = title
        scroll.scroll_to(y=0, animate=False)
        self.query_one("#reference-body", Static).update(self._body())

    def _body(self) -> str:
        entries = self.check.lookup(self.key)
        parts = [f"[b]{escape(self.key)}[/b]"]
        if entries:
            for entry in entries:
                parts.append(self._entry_text(entry))
        else:
            parts.append(
                f"[yellow]not listed in {escape(self.check.path.name)}[/yellow]"
            )
        parts.append(self._pdf_text())
        hints = ["o open pdf", "j k scroll", "escape close"]
        if len(self.keys) > 1:
            hints.insert(0, "n N other keys")
        parts.append("[dim]" + " · ".join(hints) + "[/dim]")
        return "\n\n".join(parts)

    def _entry_text(self, entry: RefEntry) -> str:
        head = []
        if entry.section:
            head.append(f"[dim]{escape(entry.section)}[/dim]")
        if entry.status:
            style = STATUS_STYLES.get(entry.status.upper(), "white")
            head.append(f"[{style}]{escape(entry.status)}[/{style}]")
        lines = [" · ".join(head)] if head else []
        if entry.statement:
            lines.append(escape(entry.statement))
        if entry.quote:
            # rendered as written, the cell already carries its own
            # quotation marks and sometimes a note after them
            lines.append(f"[dim]{escape(entry.quote)}[/dim]")
        return "\n".join(lines)

    def _pdf_text(self) -> str:
        pdf = self.check.pdf_for(self.key)
        if pdf is not None:
            return f"[dim]full text[/dim] {escape(str(pdf))}"
        expected = self.check.expected_pdf(self.key)
        where = f", looked in {escape(str(expected.parent))}" if expected else ""
        return f"[dim]no full text on disk{where}[/dim]"

    def on_key(self, event) -> None:
        scroll = self.query_one(VerticalScroll)
        ch = event.character
        if ch in ("j", "k"):
            step = 1 if ch == "j" else -1
            scroll.scroll_to(y=int(scroll.scroll_offset.y) + step, animate=False)
        elif ch in ("n", "N") and len(self.keys) > 1:
            step = 1 if ch == "n" else -1
            self.index = (self.index + step) % len(self.keys)
            self._refresh_body()
        else:
            return
        event.stop()

    def action_open_pdf(self) -> None:
        pdf = self.check.pdf_for(self.key)
        if pdf is None:
            self.app.notify(
                f"no full text for {self.key}", severity="warning"
            )
            return
        try:
            open_pdf(pdf)
        except OSError as exc:
            self.app.notify(f"cannot open {pdf.name}. {exc}", severity="error")
            return
        self.app.notify(f"opening {pdf.name}")

    def action_close(self) -> None:
        self.dismiss(None)


class SideList(ListView):
    """Sidebar list of comments with vim-style selection keys."""

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "first", show=False),
        Binding("G", "last", show=False),
    ]

    def action_first(self) -> None:
        if len(self):
            self.index = 0

    def action_last(self) -> None:
        if len(self):
            self.index = len(self) - 1


class DocumentView(ScrollView):
    """Line-API document pane. Paints only the visible lines."""

    can_focus = True

    def on_key(self, event) -> None:
        """Resolve pending multi-key sequences before the bindings run.

        The second key of `fs` or `as` must not also fire the `s`
        binding. Stopping the event in `ReviewApp.on_key` is too late,
        the app's own BINDINGS still fire, so the sequences resolve
        here on the focused widget where stopping the event keeps it
        from reaching them. Everything else bubbles up untouched.
        """
        if self.app.resolve_pending(event):
            event.stop()
            event.prevent_default()

    def on_resize(self) -> None:
        self.app.rebuild_lines()

    def render_line(self, y: int) -> Strip:
        app = self.app
        base = self.rich_style
        row = y + int(self.scroll_offset.y)
        if row >= len(app.lines) or app.lines[row] is None:
            return Strip.blank(self.size.width, base)
        text = app.render_doc_line(row)
        text.stylize_before(base)
        segments = [Segment(" ", base), *text.render(app.console, end="")]
        return Strip(segments).adjust_cell_length(self.size.width, base)


class ReviewApp(App):
    """sidenote. Vim keys, v selects, c comments, X exports docx."""

    TITLE = "sidenote"

    # Gruvbox by default. TEXTUAL_THEME still wins if the user sets it.
    theme: Reactive[str] = Reactive(os.environ.get("TEXTUAL_THEME") or "gruvbox")

    # the panel width literal must match PANEL_WIDTH, ctrl+left/right
    # overrides it per widget from there
    CSS = """
    DocumentView { width: 3fr; }
    #sidebar, #changes { width: 44; border: round $primary; padding: 0 1; display: none; }
    #sidebar.visible, #changes.visible { display: block; }
    #sidebar ListItem, #changes ListItem { height: auto; padding: 0 1; }
    #statusbar { dock: top; height: 1; background: $primary-background; padding: 0 1; }
    CommentInput, SearchInput, HelpScreen, ReferenceScreen { align: center middle; }
    #comment-dialog {
        width: 80%; max-width: 120; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    #comment-text { height: 12; margin: 1 0; }
    #comment-anchor { color: $text-muted; max-height: 10; }
    #search-dialog {
        width: 50; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    #help-dialog {
        width: 60; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    #reference-dialog {
        width: 80%; max-width: 100; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("v", "visual", "visual"),
        Binding("c", "comment", "comment"),
        Binding("m", "edit_comment", "edit"),
        Binding("d", "delete_comment", "delete"),
        Binding("slash", "search", "search"),
        Binding("s", "toggle_sidebar", "comments"),
        Binding("S", "toggle_changes", "changes"),
        Binding("r", "reference", "reference"),
        Binding("X", "export", "docx"),
        Binding("question_mark", "help", "help"),
        Binding("escape", "clear_transient", show=False),
        Binding("ctrl+left", "widen_panel", show=False),
        Binding("ctrl+right", "narrow_panel", show=False),
    ]

    def __init__(
        self,
        path: str | Path,
        author: str | None = None,
        refcheck: str | Path | None = None,
    ):
        super().__init__()
        self.path = Path(path)
        self.author = author
        self.refcheck_path = Path(refcheck) if refcheck else None
        self.review = open_review(self.path)
        self.texts: list[str] = self.review.para_texts()
        self.comment_list: list[Comment] = self.review.comments()
        self.doc_changes: list[TrackedChange] = self.review.changes()
        self.show_deletions = True
        self.cur: Pos = (0, 0)
        self.anchor: Pos | None = None
        self.goal_col = 0
        self.pending_g = False
        self.pending_z = False
        self.pending_find = ""
        self.pending_object = ""
        self.last_find: tuple[str, str] | None = None
        self.lines: list[Line | None] = []
        self._para_line_start: list[int] = []
        self.search_query = ""
        self.search_matches: list[Pos] = []
        self.sidebar_filter = ""
        self._sidebar_indices: list[int] = []
        self.panel_width = PANEL_WIDTH

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="statusbar")
        with Horizontal():
            yield DocumentView(id="doc")
            yield SideList(id="sidebar")
            yield SideList(id="changes")
        yield Footer()

    def on_mount(self) -> None:
        self.doc_view = self.query_one("#doc", DocumentView)
        self.status_widget = self.query_one("#statusbar", Static)
        self.sidebar = self.query_one("#sidebar", SideList)
        self.sidebar.border_title = "comments"
        self.changes_panel = self.query_one("#changes", SideList)
        self.changes_panel.border_title = "changes"
        self.doc_view.focus()
        self.rebuild_lines()
        self._update_sidebar()
        self._report_anchor_health()

    def _report_anchor_health(self) -> None:
        """Warn on open when sidecar anchors no longer fit the document."""
        status = getattr(self.review, "status", None)
        if status is None:
            return
        result = status()
        if result.orphaned:
            self.notify(
                f"{result.orphaned} of {result.total} comments lost their "
                "anchor text and are shown at their old position",
                severity="warning",
                timeout=8,
            )
        elif result.edited_elsewhere:
            self.notify(
                f"document edited since last save, {result.total} "
                "comments re-anchored"
            )

    # ------------------------------------------------------------------
    # Wrap cache and rendering
    # ------------------------------------------------------------------

    def _wrap_width(self) -> int:
        width = self.doc_view.scrollable_content_region.width or 80
        # one column of left padding, one of right slack
        return max(width - 2, 20)

    def rebuild_lines(self) -> None:
        """Recompute the wrap layout. Only needed on resize or reload."""
        width = self._wrap_width()
        self.lines = []
        self._para_line_start = []
        for i, text in enumerate(self.texts):
            self._para_line_start.append(len(self.lines))
            # render an empty paragraph as one space so the cursor shows
            render_text = text if text else " "
            deletions = (
                [
                    ch
                    for ch in self.doc_changes
                    if ch.kind == "deletion" and ch.start[0] == i
                ]
                if self.show_deletions
                else []
            )
            spans = wrap_offsets(render_text, width)
            for start, end in spans:
                self.lines.append(Line(i, start, end))
                last = end == len(render_text)
                for ch in deletions:
                    off = ch.start[1]
                    if start <= off < end or (last and off >= end):
                        del_text = ch.text if ch.text.strip() else "(whitespace)"
                        for ds, de in wrap_offsets(del_text, max(width - 2, 8)):
                            self.lines.append(DelLine(del_text[ds:de]))
            self.lines.append(None)
        self.doc_view.virtual_size = Size(width, len(self.lines))
        self._repaint()

    def _repaint(self) -> None:
        """Per-keystroke refresh. Repaints visible lines only."""
        self._update_statusbar()
        self._scroll_cursor_into_view()
        self._highlight_comment_at_cursor()
        self.doc_view.refresh()

    def render_doc_line(self, row: int) -> RichText:
        """Styled text for one display line, called for visible rows."""
        line = self.lines[row]
        if isinstance(line, DelLine):
            out = RichText("- " + line.text, no_wrap=True)
            out.stylize("red strike", 2)
            out.stylize("red", 0, 2)
            return out
        text = self.texts[line.para] or " "
        out = RichText(
            text[line.start:line.end].replace("\t", " "), no_wrap=True
        )
        for lo, hi, style in self._para_styles(line.para):
            lo2, hi2 = max(lo, line.start), min(hi, line.end)
            if lo2 < hi2:
                out.stylize(style, lo2 - line.start, hi2 - line.start)
        return out

    def _selection(self) -> tuple[Pos, Pos] | None:
        """Current selection as engine positions, end exclusive."""
        if self.anchor is None:
            return None
        a, b = sorted([self.anchor, self.cur])
        return a, (b[0], min(b[1] + 1, len(self.texts[b[0]])))

    def _para_styles(self, para: int) -> list[tuple[int, int, str]]:
        """Style ranges over one paragraph's text, cursor applied last."""
        n = len(self.texts[para])
        ranges: list[tuple[int, int, str]] = []
        for ch in self.doc_changes:
            if ch.kind == "insertion":
                s, e = ch.start, ch.end
                if s[0] <= para <= e[0]:
                    lo = s[1] if s[0] == para else 0
                    hi = e[1] if e[0] == para else n
                    ranges.append((lo, min(hi, n), STYLE_INSERTION))
            elif ch.start[0] == para and n:
                o = min(ch.start[1], n - 1)
                ranges.append((o, o + 1, STYLE_DELETION))
        for c in self.comment_list:
            s, e = c.start, max(c.end, (c.start[0], c.start[1] + 1))
            if s[0] <= para <= e[0]:
                lo = s[1] if s[0] == para else 0
                hi = e[1] if e[0] == para else n
                ranges.append((lo, min(hi, n), STYLE_COMMENT))
        if self.search_query:
            qlen = len(self.search_query)
            for mp, mo in self.search_matches:
                if mp == para:
                    style = (
                        STYLE_SEARCH_CURRENT
                        if (mp, mo) == self.cur
                        else STYLE_SEARCH
                    )
                    ranges.append((mo, min(mo + qlen, n), style))
        sel = self._selection()
        if sel:
            s, e = sel
            if s[0] <= para <= e[0]:
                lo = s[1] if s[0] == para else 0
                hi = e[1] if e[0] == para else n
                ranges.append((lo, min(hi, n), STYLE_SELECTION))
        if self.cur[0] == para:
            ranges.append((self.cur[1], self.cur[1] + 1, STYLE_CURSOR))
        return ranges

    def _update_statusbar(self) -> None:
        self.status_widget.update(self._status_text())

    def _status_text(self) -> str:
        mode = "VISUAL" if self.anchor is not None else "NORMAL"
        p, o = self.cur
        search = ""
        if self.search_query:
            at = [
                i
                for i, m in enumerate(self.search_matches, 1)
                if m == self.cur
            ]
            pos = at[0] if at else "-"
            search = (
                f" · /{escape(self.search_query)} "
                f"{pos}/{len(self.search_matches)}"
            )
        change = ""
        ch = self._change_at_cursor()
        if ch is not None:
            sign = "+" if ch.kind == "insertion" else "-"
            date = f" {ch.date[:10]}" if ch.date else ""
            change = f" · {sign} {escape(ch.author)}{date}"
            if ch.kind == "deletion" and ch.text:
                excerpt = (
                    ch.text if len(ch.text) <= 40 else ch.text[:37] + "..."
                )
                change += f" '{escape(excerpt)}'"
        name = self.path.name
        if len(name) > 28:
            name = name[:27] + "\u2026"
        return (
            f"[b]{escape(name)}[/b] · {mode} · "
            f"para {p + 1}/{len(self.texts)} char {o} · "
            f"comments {len(self.comment_list)}{search}{change}"
        )

    @staticmethod
    def _matches_filter(c: Comment, needle: str) -> bool:
        return needle in (c.author or "").lower() or needle in c.text.lower()

    def _update_sidebar(self, keep_index: int | None = None) -> None:
        needle = self.sidebar_filter.lower()
        self._sidebar_indices = [
            i
            for i, c in enumerate(self.comment_list)
            if not needle or self._matches_filter(c, needle)
        ]
        items: list[ListItem] = []
        for i in self._sidebar_indices:
            c = self.comment_list[i]
            sp, so = c.start
            ep, eo = c.end
            if c.end > c.start:
                anchor = (
                    self.texts[sp][so:eo]
                    if sp == ep
                    else self.texts[sp][so:so + 30] + " [...]"
                )
                anchor = anchor if len(anchor) <= 40 else anchor[:37] + "..."
                head = f"[b]{i + 1}.[/b] [yellow]{escape(anchor)}[/yellow]"
            else:
                head = f"[b]{i + 1}.[/b] [dim]para {sp + 1} note[/dim]"
            if c.orphan:
                head += " [red]orphaned[/red]"
            date = c.date[:10] if c.date else ""
            items.append(
                ListItem(
                    Static(
                        f"{head}\n[dim]{escape(c.author)} {date}[/dim]\n"
                        f"{escape(c.text)}"
                    )
                )
            )
        if needle:
            self.sidebar.border_title = (
                f"comments · {escape(self.sidebar_filter)} "
                f"({len(items)}/{len(self.comment_list)})"
            )
        else:
            self.sidebar.border_title = "comments"
        self.sidebar.clear()
        if items:
            self.sidebar.extend(items)
            index = min(keep_index or 0, len(items) - 1)
            self.call_after_refresh(self._set_sidebar_index, index)
        elif self.focused is self.sidebar and not needle:
            self.doc_view.focus()

    def _set_sidebar_index(self, index: int) -> None:
        if len(self.sidebar):
            self.sidebar.index = min(index, len(self.sidebar) - 1)

    def _sync_sidebar_index(self) -> None:
        """Point the sidebar selection at the comment at or after the cursor."""
        if not self._sidebar_indices:
            return
        after = [
            pos
            for pos, i in enumerate(self._sidebar_indices)
            if self.comment_list[i].start >= self.cur
        ]
        self.call_after_refresh(self._set_sidebar_index, after[0] if after else 0)

    def _highlight_comment_at_cursor(self) -> None:
        """Select the sidebar entry for the comment the cursor sits in.

        Runs on every cursor move, so it stays O(comments) and touches
        the ListView only when the selection actually changes. When the
        cursor is not inside any comment, or the comment is hidden by
        the filter, the previous selection stands.
        """
        if not self.sidebar.has_class("visible"):
            return
        idx = self._comment_index_at_cursor()
        if idx is None or idx not in self._sidebar_indices:
            return
        pos = self._sidebar_indices.index(idx)
        if pos < len(self.sidebar) and self.sidebar.index != pos:
            self.sidebar.index = pos

    def _sidebar_comment(self) -> Comment | None:
        """Comment behind the current sidebar selection, filter-aware."""
        idx = self.sidebar.index
        if idx is None or idx >= len(self._sidebar_indices):
            return None
        return self.comment_list[self._sidebar_indices[idx]]

    def _update_changes_panel(self) -> None:
        items: list[ListItem] = []
        for i, ch in enumerate(self.doc_changes, 1):
            if ch.kind == "insertion":
                head = f"[b]{i}.[/b] [green]+ inserted[/green]"
            else:
                head = f"[b]{i}.[/b] [red]- deleted[/red]"
            date = ch.date[:10] if ch.date else ""
            text = ch.text if ch.text.strip() else (
                "(whitespace)" if ch.text else "(empty)"
            )
            text = text if len(text) <= 120 else text[:117] + "..."
            items.append(
                ListItem(
                    Static(
                        f"{head}\n[dim]{escape(ch.author)} {date}[/dim]\n"
                        f"{escape(text)}"
                    )
                )
            )
        self.changes_panel.clear()
        if items:
            self.changes_panel.extend(items)
            self.call_after_refresh(self._set_changes_index, 0)

    def _set_changes_index(self, index: int) -> None:
        if len(self.changes_panel):
            self.changes_panel.index = min(index, len(self.changes_panel) - 1)

    def _sync_changes_index(self) -> None:
        if not self.doc_changes:
            return
        after = [
            i for i, ch in enumerate(self.doc_changes) if ch.start >= self.cur
        ]
        self.call_after_refresh(self._set_changes_index, after[0] if after else 0)

    def _change_at_cursor(self) -> TrackedChange | None:
        for ch in self.doc_changes:
            if ch.kind == "insertion" and ch.start <= self.cur < ch.end:
                return ch
            if ch.kind == "deletion" and ch.start == self.cur:
                return ch
        return None

    def _cursor_line(self) -> int:
        """Display line of the cursor. Scans only the cursor's paragraph."""
        p, o = self.cur
        idx = self._para_line_start[p] if p < len(self._para_line_start) else 0
        for j in range(idx, len(self.lines)):
            line = self.lines[j]
            if line is None:
                break
            if not isinstance(line, Line):
                continue
            if line.para != p:
                break
            idx = j
            if line.start <= o < max(line.end, line.start + 1):
                return j
        return idx

    def _scroll_cursor_into_view(self) -> None:
        y = self._cursor_line()
        top = int(self.doc_view.scroll_offset.y)
        height = self.doc_view.size.height or 24
        if y < top:
            self.doc_view.scroll_to(y=y, animate=False)
        elif y >= top + height:
            self.doc_view.scroll_to(y=y - height + 1, animate=False)

    # ------------------------------------------------------------------
    # Movement
    # ------------------------------------------------------------------

    def _para_len(self, i: int) -> int:
        return len(self.texts[i])

    def _max_off(self, i: int) -> int:
        return max(self._para_len(i) - 1, 0)

    def _move_horizontal(self, delta: int) -> None:
        p, o = self.cur
        o += delta
        while o < 0:
            if p == 0:
                o = 0
                break
            p -= 1
            o += self._max_off(p) + 1
        while o > self._max_off(p):
            if p == len(self.texts) - 1:
                o = self._max_off(p)
                break
            o -= self._max_off(p) + 1
            p += 1
        self.cur = (p, o)
        self.goal_col = self._col_in_line()

    def _col_in_line(self) -> int:
        line = self.lines[self._cursor_line()]
        return self.cur[1] - line.start if line else 0

    def _move_vertical(self, delta: int) -> None:
        idx = self._cursor_line()
        step = 1 if delta > 0 else -1
        for _ in range(abs(delta)):
            j = idx + step
            while 0 <= j < len(self.lines) and not isinstance(
                self.lines[j], Line
            ):
                j += step
            if not 0 <= j < len(self.lines):
                break
            idx = j
        line = self.lines[idx]
        if not isinstance(line, Line):
            return
        span = max(line.end - line.start - 1, 0)
        self.cur = (line.para, line.start + min(self.goal_col, span))

    def _word_jump(self, forward: bool) -> None:
        p, o = self.cur
        if forward:
            for i in range(p, len(self.texts)):
                min_start = o + 1 if i == p else 0
                for w in WORD_RE.finditer(self.texts[i]):
                    if w.start() >= min_start:
                        self.cur = (i, w.start())
                        return
        else:
            for i in range(p, -1, -1):
                limit = o if i == p else self._para_len(i) + 1
                m = [w for w in WORD_RE.finditer(self.texts[i]) if w.start() < limit]
                if m:
                    self.cur = (i, m[-1].start())
                    return

    def _word_end_jump(self) -> None:
        p, o = self.cur
        for i in range(p, len(self.texts)):
            for w in WORD_RE.finditer(self.texts[i]):
                last = w.end() - 1
                if i > p or last > o:
                    self.cur = (i, last)
                    return

    def _find_char(self, cmd: str, target: str) -> None:
        """vim f/F/t/T. Searches within the paragraph, as vim does a line."""
        p, o = self.cur
        text = self.texts[p]
        if cmd in ("f", "t"):
            idx = text.find(target, o + (2 if cmd == "t" else 1))
            hit = idx - 1 if cmd == "t" else idx
        else:
            stop = o - (1 if cmd == "T" else 0)
            idx = text.rfind(target, 0, max(stop, 0))
            hit = idx + 1 if cmd == "T" else idx
        if idx == -1:
            self.notify(
                f"{escape(target)!r} not found in this paragraph",
                severity="warning",
            )
            return
        self.cur = (p, hit)
        self.goal_col = self._col_in_line()

    def _repeat_find(self, reverse: bool) -> None:
        """vim ; and , repeat the last f/F/t/T, optionally flipped."""
        if self.last_find is None:
            return
        cmd, target = self.last_find
        self._find_char(FIND_REVERSE[cmd] if reverse else cmd, target)

    def _sentence_jump(self, forward: bool) -> None:
        """vim ( and ). Paragraph boundaries also end a sentence."""
        p, o = self.cur
        span = range(p, len(self.texts)) if forward else range(p, -1, -1)
        for i in span:
            starts = [s for s, _, _ in sentence_spans(self.texts[i])]
            if forward:
                hits = [s for s in starts if i > p or s > o]
            else:
                hits = [s for s in starts if i < p or s < o]
            if hits:
                target = hits[0] if forward else hits[-1]
                self.cur = (i, min(target, self._max_off(i)))
                self.goal_col = self._col_in_line()
                return

    def _text_object(self, kind: str, around: bool) -> None:
        """vim iw/aw, is/as, ip/ap. Selects the object into visual mode.

        Always takes the whole object, both ends of it, in visual mode
        too. Extending only forwards from the cursor is what `vf.`
        already does.
        """
        p, o = self.cur
        text = self.texts[p]
        if kind == "p":
            lo, hi = 0, len(text)
        elif kind == "s":
            start, body_end, end = sentence_at(text, o)
            lo, hi = start, end if around else body_end
        else:
            lo, hi = word_object(text, o, around)
        self.anchor = (p, lo)
        self.cur = (p, min(max(hi - 1, 0), self._max_off(p)))
        self.goal_col = self._col_in_line()

    def _para_jump(self, forward: bool) -> None:
        p, o = self.cur
        if forward:
            self.cur = (min(p + 1, len(self.texts) - 1), 0)
        else:
            self.cur = (p, 0) if o > 0 else (max(p - 1, 0), 0)
        self.goal_col = 0

    def _jump_to(self, positions: list[Pos], forward: bool) -> None:
        """Move to the nearest position in the given direction, wrapping."""
        if not positions:
            return
        positions = sorted(positions)
        if forward:
            nxt = [s for s in positions if s > self.cur]
            self.cur = nxt[0] if nxt else positions[0]
        else:
            prev = [s for s in positions if s < self.cur]
            self.cur = prev[-1] if prev else positions[-1]
        self.cur = (self.cur[0], min(self.cur[1], self._max_off(self.cur[0])))

    def _sidebar_key(self, event) -> None:
        """Search-style navigation inside the sidebar list.

        n/N cycle through the (filtered) comments with wraparound, and
        */# filter to the selected comment's author and move to that
        author's next/previous comment. Other keys fall through to the
        ListView bindings (j, k, g, G, enter).
        """
        ch = event.character
        count = len(self.sidebar)
        if count == 0 or ch not in ("n", "N", "*", "#"):
            return
        idx = self.sidebar.index or 0
        if ch == "n":
            self.sidebar.index = (idx + 1) % count
        elif ch == "N":
            self.sidebar.index = (idx - 1) % count
        else:
            target = self._sidebar_comment()
            if target is None or not target.author:
                self.notify("no author on selected comment", severity="warning")
                return
            needle = target.author.lower()
            filtered = [
                i
                for i, c in enumerate(self.comment_list)
                if self._matches_filter(c, needle)
            ]
            pos = filtered.index(self._sidebar_indices[idx])
            step = 1 if ch == "*" else -1
            self.sidebar_filter = target.author
            self._update_sidebar(keep_index=(pos + step) % len(filtered))
        event.stop()

    def _changes_key(self, event) -> None:
        """n/N cycle through the changes list with wraparound."""
        ch = event.character
        count = len(self.changes_panel)
        if count == 0 or ch not in ("n", "N"):
            return
        idx = self.changes_panel.index or 0
        step = 1 if ch == "n" else -1
        self.changes_panel.index = (idx + step) % count
        event.stop()

    def _search_word_under_cursor(self, forward: bool) -> None:
        """Vim * and #. Whole-word search for the word at the cursor."""
        p, o = self.cur
        word = None
        for m in WORD_RE.finditer(self.texts[p]):
            if m.start() <= o < m.end():
                word = m.group()
                break
        if word is None:
            self.notify("no word under cursor", severity="warning")
            return
        self.search_query = word
        pattern = re.compile(
            rf"(?<![\wÀ-ɏ]){re.escape(word)}(?![\wÀ-ɏ])"
        )
        self.search_matches = [
            (i, m.start())
            for i, t in enumerate(self.texts)
            for m in pattern.finditer(t)
        ]
        self._jump_to(self.search_matches, forward=forward)

    def _scroll_view(self, delta: int) -> None:
        self.doc_view.scroll_to(
            y=int(self.doc_view.scroll_offset.y) + delta, animate=False
        )

    def resolve_pending(self, event) -> bool:
        """Consume the second key of a multi-key sequence.

        Called from `DocumentView.on_key` so the key never reaches the
        app BINDINGS. Returns True when a sequence was pending, which
        swallows the key whether or not it completed the sequence, the
        way vim treats any prefix.
        """
        if len(self.screen_stack) > 1:
            return False
        ch = event.character
        if self.pending_g:
            self.pending_g = False
            if ch == "g":
                self.cur = (0, 0)
                self.goal_col = 0
                self._repaint()
            return True
        if self.pending_z:
            self.pending_z = False
            height = self.doc_view.size.height or 24
            y = self._cursor_line()
            targets = {"z": y - height // 2, "t": y, "b": y - height + 1}
            if ch in targets:
                self.doc_view.scroll_to(y=max(targets[ch], 0), animate=False)
            return True
        if self.pending_find:
            cmd, self.pending_find = self.pending_find, ""
            if ch and event.key != "escape":
                self.last_find = (cmd, ch)
                self._find_char(cmd, ch)
                self._repaint()
            return True
        if self.pending_object:
            scope, self.pending_object = self.pending_object, ""
            if ch in ("w", "s", "p"):
                self._text_object(ch, around=scope == "a")
                self._repaint()
            return True
        return False

    def on_key(self, event) -> None:
        if len(self.screen_stack) > 1:
            return
        if self.focused is self.sidebar:
            self._sidebar_key(event)
            return
        if self.focused is self.changes_panel:
            self._changes_key(event)
            return
        if self.focused is not self.doc_view:
            return
        key = event.key
        ch = event.character
        if key == "ctrl+e":
            self._scroll_view(1)
            event.stop()
            return
        elif key == "ctrl+y":
            self._scroll_view(-1)
            event.stop()
            return
        half_page = max((self.doc_view.size.height or 24) // 2, 1)
        if ch == "j" or key == "down":
            self._move_vertical(1)
        elif ch == "k" or key == "up":
            self._move_vertical(-1)
        elif ch == "l" or key == "right":
            self._move_horizontal(1)
        elif ch == "h" or key == "left":
            self._move_horizontal(-1)
        elif ch == "w":
            self._word_jump(forward=True)
        elif ch == "b":
            self._word_jump(forward=False)
        elif ch == "e":
            self._word_end_jump()
        elif ch in FIND_REVERSE:
            self.pending_find = ch
            event.stop()
            return
        elif ch in ("a", "i"):
            self.pending_object = ch
            event.stop()
            return
        elif ch == ";":
            self._repeat_find(reverse=False)
        elif ch == ",":
            self._repeat_find(reverse=True)
        elif ch == "(":
            self._sentence_jump(forward=False)
        elif ch == ")":
            self._sentence_jump(forward=True)
        elif ch == "0":
            line = self.lines[self._cursor_line()]
            if line:
                self.cur = (line.para, line.start)
                self.goal_col = 0
        elif ch == "$":
            line = self.lines[self._cursor_line()]
            if line:
                self.cur = (line.para, max(line.end - 1, line.start))
                self.goal_col = self._para_len(line.para)
        elif ch == "{":
            self._para_jump(forward=False)
        elif ch == "}":
            self._para_jump(forward=True)
        elif ch == "g":
            self.pending_g = True
            event.stop()
            return
        elif ch == "z":
            self.pending_z = True
            event.stop()
            return
        elif ch == "D":
            self.show_deletions = not self.show_deletions
            self.rebuild_lines()
            state = "shown" if self.show_deletions else "hidden"
            self.notify(f"deleted text {state}")
            event.stop()
            return
        elif ch == "G":
            self.cur = (len(self.texts) - 1, 0)
            self.goal_col = 0
        elif key == "ctrl+d":
            self._move_vertical(half_page)
        elif key == "ctrl+u":
            self._move_vertical(-half_page)
        elif ch == "]":
            self._jump_to([c.start for c in self.comment_list], forward=True)
        elif ch == "[":
            self._jump_to([c.start for c in self.comment_list], forward=False)
        elif ch == ">":
            self._jump_to([c.start for c in self.doc_changes], forward=True)
        elif ch == "<":
            self._jump_to([c.start for c in self.doc_changes], forward=False)
        elif ch == "n":
            self._jump_to(self.search_matches, forward=True)
        elif ch == "N":
            self._jump_to(self.search_matches, forward=False)
        elif ch == "*":
            self._search_word_under_cursor(forward=True)
        elif ch == "#":
            self._search_word_under_cursor(forward=False)
        else:
            return
        self._repaint()
        event.stop()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_visual(self) -> None:
        self.anchor = None if self.anchor is not None else self.cur
        self._repaint()

    def action_clear_transient(self) -> None:
        if self.focused is self.changes_panel:
            self.doc_view.focus()
            return
        if self.focused is self.sidebar:
            if self.sidebar_filter:
                self.sidebar_filter = ""
                self._update_sidebar()
            else:
                self.doc_view.focus()
            return
        if self.anchor is not None:
            self.anchor = None
        elif self.search_query:
            self.search_query = ""
            self.search_matches = []
        else:
            return
        self._repaint()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter in a side panel jumps to the entry's anchor."""
        if event.list_view is self.changes_panel:
            idx = self.changes_panel.index
            if idx is None or idx >= len(self.doc_changes):
                return
            start = self.doc_changes[idx].start
        else:
            target = self._sidebar_comment()
            if target is None:
                return
            start = target.start
        self.cur = (start[0], min(start[1], self._max_off(start[0])))
        self.goal_col = 0
        self.doc_view.focus()
        self._repaint()

    def _comments_at_cursor(self) -> list[Comment]:
        return [c for c in self.comment_list if self._covers_cursor(c)]

    def _comment_index_at_cursor(self) -> int | None:
        """Index into comment_list of the first comment under the cursor."""
        for i, c in enumerate(self.comment_list):
            if self._covers_cursor(c):
                return i
        return None

    def _covers_cursor(self, c: Comment) -> bool:
        return c.start <= self.cur < c.end or c.start == self.cur

    def _target_comment(self) -> Comment | None:
        """Comment to act on. Sidebar selection when focused, else cursor."""
        if self.focused is self.sidebar:
            return self._sidebar_comment()
        here = self._comments_at_cursor()
        return here[0] if here else None

    def _anchor_preview(self, start: Pos, end: Pos) -> str:
        """Full selected text for the comment dialog, capped for huge spans."""
        sp, so = start
        ep, eo = end
        if end <= start:
            return f"(note at paragraph {sp + 1})"
        if sp == ep:
            text = self.texts[sp][so:eo]
        else:
            parts = [self.texts[sp][so:]]
            parts.extend(self.texts[i] for i in range(sp + 1, ep))
            parts.append(self.texts[ep][:eo])
            text = "\n".join(parts)
        return text if len(text) <= 600 else text[:600] + " […]"

    def _after_mutation(self, message: str) -> None:
        keep = self.sidebar.index if self.focused is self.sidebar else None
        self.review = open_review(self.path)
        self.texts = self.review.para_texts()
        self.comment_list = self.review.comments()
        self.doc_changes = self.review.changes()
        self.rebuild_lines()
        self._update_sidebar(keep_index=keep)
        self.notify(message)

    def action_comment(self) -> None:
        sel = self._selection()
        start, end = sel if sel else (self.cur, self.cur)
        preview = self._anchor_preview(start, end)

        def on_result(text: str | None) -> None:
            if not text:
                return
            self.review.add_comment(start, end, text, author=self.author)
            self.review.save()
            self.anchor = None
            self._after_mutation("comment saved")

        self.push_screen(CommentInput(preview), on_result)

    def action_edit_comment(self) -> None:
        target = self._target_comment()
        if target is None:
            self.notify("no comment under cursor", severity="warning")
            return
        preview = self._anchor_preview(target.start, target.end)

        def on_result(text: str | None) -> None:
            if not text or text == target.text:
                return
            self.review.update_comment(target, text)
            self.review.save()
            self._after_mutation("comment updated")

        byline = f"comment by {target.author}" if target.author else ""
        if byline and target.date:
            byline += f", {target.date[:10]}"
        self.push_screen(
            CommentInput(preview, initial=target.text, byline=byline), on_result
        )

    def action_delete_comment(self) -> None:
        target = self._target_comment()
        if target is None:
            self.notify("no comment under cursor", severity="warning")
            return
        self.review.delete_comment(target)
        self.review.save()
        self._after_mutation("comment deleted")

    def action_search(self) -> None:
        if self.focused is self.sidebar:
            self._sidebar_filter_prompt()
            return

        def on_result(query: str | None) -> None:
            if not query:
                return
            self.search_query = query
            flags = re.IGNORECASE if query.islower() else 0
            self.search_matches = [
                (i, m.start())
                for i, t in enumerate(self.texts)
                for m in re.finditer(re.escape(query), t, flags)
            ]
            if not self.search_matches:
                self.notify(f"no matches for {query!r}", severity="warning")
                self.search_query = ""
                return
            self._jump_to(self.search_matches, forward=True)
            self._repaint()

        self.push_screen(SearchInput(), on_result)

    def _sidebar_filter_prompt(self) -> None:
        def on_result(query: str | None) -> None:
            if not query:
                return
            matches = [
                c
                for c in self.comment_list
                if self._matches_filter(c, query.lower())
            ]
            if not matches:
                self.notify(f"no comments matching {query!r}", severity="warning")
                return
            self.sidebar_filter = query
            self._update_sidebar()
            self.sidebar.focus()

        self.push_screen(SearchInput(placeholder="author or text"), on_result)

    def action_toggle_sidebar(self) -> None:
        # the width change triggers DocumentView.on_resize -> rebuild
        if self.sidebar.has_class("visible"):
            self.sidebar.remove_class("visible")
            if self.sidebar_filter:
                self.sidebar_filter = ""
                self._update_sidebar()
            self.doc_view.focus()
        else:
            self.changes_panel.remove_class("visible")
            self.sidebar.add_class("visible")
            self._sync_sidebar_index()
            self.sidebar.focus()

    def action_toggle_changes(self) -> None:
        if self.changes_panel.has_class("visible"):
            self.changes_panel.remove_class("visible")
            self.doc_view.focus()
        else:
            if self.sidebar.has_class("visible"):
                self.sidebar.remove_class("visible")
                if self.sidebar_filter:
                    self.sidebar_filter = ""
                    self._update_sidebar()
            self.changes_panel.add_class("visible")
            self._update_changes_panel()
            self._sync_changes_index()
            self.changes_panel.focus()

    def action_widen_panel(self) -> None:
        self._move_divider(PANEL_STEP)

    def action_narrow_panel(self) -> None:
        self._move_divider(-PANEL_STEP)

    def _move_divider(self, delta: int) -> None:
        """Move the split between the document and the side panel.

        Both panels share one width so the divider stays where the user
        put it when switching between comments and changes. The width
        change triggers DocumentView.on_resize -> rebuild_lines.
        """
        if not (
            self.sidebar.has_class("visible")
            or self.changes_panel.has_class("visible")
        ):
            return
        widest = max(PANEL_MIN_WIDTH, (self.size.width or 80) - DOC_MIN_WIDTH)
        width = max(PANEL_MIN_WIDTH, min(self.panel_width + delta, widest))
        if width == self.panel_width:
            return
        self.panel_width = width
        self.sidebar.styles.width = width
        self.changes_panel.styles.width = width

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_reference(self) -> None:
        """Show what the reference-check file says about this citation."""
        keys = citations_at(self.texts[self.cur[0]], self.cur[1])
        if not keys:
            self.notify("no citation under cursor", severity="warning")
            return
        check = self._load_refcheck()
        if check is None:
            self.notify(
                "no reference-check.md beside the document, "
                "pass --refcheck to point at one",
                severity="warning",
            )
            return
        self.push_screen(ReferenceScreen(check, keys))

    def _load_refcheck(self) -> ReferenceCheck | None:
        """Read the reference-check file, freshly each time.

        It is small and hand-edited while reviewing, so re-reading picks
        up a row added in another window without reopening the document.
        """
        path = self.refcheck_path or find_reference_check(self.path)
        if path is None or not path.is_file():
            return None
        try:
            return load_reference_check(path)
        except OSError as exc:
            self.notify(f"cannot read {path.name}. {exc}", severity="error")
            return None

    def action_export(self) -> None:
        self.notify("exporting to docx...")

        def do_export() -> None:
            try:
                target = self.review.export_docx()
                self.call_from_thread(self.notify, f"wrote {target}")
            except Exception as exc:
                self.call_from_thread(
                    self.notify, f"export failed. {exc}", severity="error"
                )

        self.run_worker(do_export, thread=True)
