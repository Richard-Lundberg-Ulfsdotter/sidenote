"""Markdown document engine.

Reads a Markdown file as plain source text and keeps comments in a
sidecar JSON file next to it, so the Markdown itself is never modified.
That matters when the file is a build input, a pandoc source, or simply
under version control where comment churn would pollute prose diffs.

Paragraphs are the blank-line separated blocks of the source, fenced
code blocks kept whole. Positions are (paragraph_index, offset) into
those blocks, the same model `OdtReview` uses, so the TUI and the CLI
talk to both engines through one interface.

Because the sidecar cannot move when the Markdown is edited outside
sidenote, each endpoint of a comment stores the surrounding text as
context and is relocated on load. An endpoint whose context has
vanished leaves the comment orphaned rather than dropping it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sidenote.engine import Comment, Pos, TrackedChange, default_author

SIDECAR_VERSION = 1

# characters of surrounding text stored to relocate an endpoint
CONTEXT = 40

# a lone side of context must be at least this long to relocate on its own
MIN_SOLO_CONTEXT = 8


def sidecar_path(path: str | Path) -> Path:
    path = Path(path)
    return path.with_name(path.stem + ".sidenote.json")


def digest(source: str) -> str:
    """Short content digest, stored to detect edits made elsewhere."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def split_blocks(source: str) -> list[str]:
    """Split Markdown into blank-line separated blocks.

    Fenced code blocks survive whole even when they contain blank
    lines, so a comment inside one keeps a stable paragraph index.
    """
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in source.split("\n"):
        stripped = line.lstrip()
        if fence is None:
            for marker in ("```", "~~~"):
                if stripped.startswith(marker):
                    fence = marker
                    break
            if not stripped and not fence:
                if current:
                    blocks.append("\n".join(current))
                    current = []
                continue
        elif stripped.startswith(fence):
            fence = None
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def hard_lines(text: str) -> list[tuple[int, str]]:
    """The text's own lines as (offset, line)."""
    out: list[tuple[int, str]] = []
    start = 0
    for line in text.split("\n"):
        out.append((start, line))
        start += len(line) + 1
    return out


# a pandoc citation key in the source, @smithFolateIntake2020. The
# capture group is the bare key, which is what the reference overlay
# looks up.
CITATION_RE = re.compile(r"@([A-Za-z0-9_][\w.:#$%&+?<>~/-]*)")

# One display span of the source, (start, end, kind). The kinds name
# what the run is, the frontend chooses the colours.
Span = tuple[int, int, str]

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6}[ \t]+)(.*)$")
_QUOTE_RE = re.compile(r"^(\s*(?:>[ \t]?)+)")
_BULLET_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])[ \t]+)")
# a line of nothing but one repeated rule character, so a thematic
# break or the underline of a setext heading
_RULE_RE = re.compile(r"^\s*([=*_-])(?:[ \t]*\1){2,}[ \t]*$")
# a pipe-table delimiter row, |---|:--:|
_TABLE_RULE_RE = re.compile(r"^[\s|:-]*-[\s|:-]*$")

# Inline patterns as (pattern, body group, kind). Everything in the
# match outside the body group is markup, which includes a link's URL.
# Order is priority, a match overlapping one already taken is skipped,
# so emphasis cannot start inside a code span and `_` in snake_case is
# left alone by the lookarounds.
_INLINE = (
    (re.compile(r"(`+)([^`]+?)\1"), 2, "code"),
    (
        re.compile(r"(!?\[)([^\]\n]*)(\]\()([^)\s]+)((?:[ \t]+\"[^\"]*\")?\))"),
        2,
        "link",
    ),
    (re.compile(r"(<)([a-z][\w+.-]*:[^>\s]+)(>)"), 2, "link"),
    (re.compile(r"(~~)(?=\S)(.+?)(?<=\S)(~~)"), 2, "strike"),
    (re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1"), 2, "strong"),
    (
        re.compile(r"(?<![\w*])(\*|_)(?=[^\s*_])([^*_]+?)(?<=[^\s*_])\1(?![\w*])"),
        2,
        "emphasis",
    ),
    (CITATION_RE, 0, "cite"),
)


def markup_spans(block: str) -> list[Span]:
    """Where the markdown markup and the prose it marks up sit.

    Display only. Nothing is removed or reordered, the spans style the
    source where it stands, so a rendered view and a raw view share one
    set of offsets and comment anchors are indifferent to which is on.

    Later spans win where they overlap, which is how a heading's `#`
    ends up markup-grey inside an otherwise blue heading line.
    """
    # empty spans come from a blank line in a code block or a heading
    # with nothing after the hashes, and mean nothing to a renderer
    return [span for span in _scan_block(block) if span[0] < span[1]]


def _scan_block(block: str) -> list[Span]:
    lines = hard_lines(block)
    spans: list[Span] = []
    if lines and _FENCE_RE.match(lines[0][1]):
        # split_blocks keeps a fenced block whole, so the whole
        # paragraph is code apart from the fences themselves
        for off, line in lines:
            kind = "marker" if _FENCE_RE.match(line) else "code"
            spans.append((off, off + len(line), kind))
        return spans

    claimed = bytearray(len(block))
    # the previous line's prose, which a setext underline turns into a
    # heading after the fact
    previous: tuple[int, int] | None = None
    for off, line in lines:
        end = off + len(line)
        if _RULE_RE.match(line):
            spans.append((off, end, "marker"))
            if previous is not None and line.lstrip()[0] in "=-":
                spans.append((*previous, "heading"))
            previous = None
            continue
        if "|" in line and _TABLE_RULE_RE.match(line):
            spans.append((off, end, "marker"))
            previous = None
            continue

        lo = off
        quote = _QUOTE_RE.match(line)
        if quote:
            lo = off + quote.end()
            spans.append((off, lo, "marker"))
            spans.append((lo, end, "quote"))
        rest = block[lo:end]
        heading = _HEADING_RE.match(rest)
        if heading:
            spans.append((lo, lo + heading.end(1), "marker"))
            lo += heading.end(1)
            spans.append((lo, end, "heading"))
        else:
            bullet = _BULLET_RE.match(rest)
            if bullet:
                spans.append((lo, lo + bullet.end(1), "marker"))
                lo += bullet.end(1)
        for i, ch in enumerate(block[lo:end], lo):
            if ch == "|":
                spans.append((i, i + 1, "marker"))
        _scan_inline(block, lo, end, claimed, spans)
        previous = (lo, end)
    return spans


def _scan_inline(
    block: str, lo: int, hi: int, claimed: bytearray, spans: list[Span]
) -> None:
    for pattern, group, kind in _INLINE:
        for m in pattern.finditer(block, lo, hi):
            start, stop = m.span()
            if any(claimed[start:stop]):
                continue
            claimed[start:stop] = b"\x01" * (stop - start)
            body_start, body_stop = m.span(group)
            spans.append((body_start, body_stop, kind))
            if start < body_start:
                spans.append((start, body_start, "marker"))
            if body_stop < stop:
                spans.append((body_stop, stop, "marker"))


def _find_all(text: str, needle: str) -> list[int]:
    out: list[int] = []
    i = text.find(needle)
    while i != -1:
        out.append(i)
        i = text.find(needle, i + 1)
    return out


@dataclass
class _Anchor:
    """One endpoint of a comment plus the text that surrounds it."""

    para: int
    off: int
    before: str
    after: str

    @classmethod
    def capture(cls, texts: list[str], pos: Pos) -> "_Anchor":
        para, off = pos
        text = texts[para] if 0 <= para < len(texts) else ""
        return cls(
            para=para,
            off=off,
            before=text[max(0, off - CONTEXT) : off],
            after=text[off : off + CONTEXT],
        )

    def to_dict(self) -> dict:
        return {
            "para": self.para,
            "off": self.off,
            "before": self.before,
            "after": self.after,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "_Anchor":
        return cls(
            para=int(data.get("para", 0)),
            off=int(data.get("off", 0)),
            before=data.get("before", ""),
            after=data.get("after", ""),
        )

    def fits(self, texts: list[str], para: int, off: int) -> bool:
        if not 0 <= para < len(texts):
            return False
        text = texts[para]
        if not 0 <= off <= len(text):
            return False
        return (
            text[max(0, off - len(self.before)) : off] == self.before
            and text[off : off + len(self.after)] == self.after
        )

    def locate(self, texts: list[str]) -> Pos | None:
        """Find where this endpoint sits now, or None if it is gone."""
        if self.fits(texts, self.para, self.off):
            return (self.para, self.off)

        key = self.before + self.after
        if not key:
            # an endpoint in an empty paragraph carries no context
            if 0 <= self.para < len(texts) and not texts[self.para]:
                return (self.para, 0)
            return None

        candidates = [
            (i, j + len(self.before))
            for i, text in enumerate(texts)
            for j in _find_all(text, key)
        ]
        if not candidates:
            # the text on one side survived, settle for that
            for side, is_before in ((self.before, True), (self.after, False)):
                if len(side) < MIN_SOLO_CONTEXT:
                    continue
                candidates = [
                    (i, j + len(side) if is_before else j)
                    for i, text in enumerate(texts)
                    for j in _find_all(text, side)
                ]
                if candidates:
                    break
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda c: (abs(c[0] - self.para), abs(c[1] - self.off)),
        )


@dataclass
class _Record:
    """A stored comment, independent of where it currently anchors."""

    name: str
    author: str
    date: str
    text: str
    start: _Anchor
    end: _Anchor
    quote: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "author": self.author,
            "date": self.date,
            "text": self.text,
            "quote": self.quote,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "_Record":
        start = _Anchor.from_dict(data.get("start", {}))
        end_data = data.get("end")
        return cls(
            name=data.get("name", ""),
            author=data.get("author", ""),
            date=data.get("date", ""),
            text=data.get("text", ""),
            quote=data.get("quote", ""),
            start=start,
            end=_Anchor.from_dict(end_data) if end_data else start,
        )


def _extract_range(texts: list[str], start: Pos, end: Pos) -> str:
    (p1, o1), (p2, o2) = start, end
    if p1 == p2:
        return texts[p1][o1:o2]
    parts = [texts[p1][o1:]]
    parts.extend(texts[i] for i in range(p1 + 1, p2))
    parts.append(texts[p2][:o2])
    return "\n".join(parts)


@dataclass
class ReviewStatus:
    """What `sidenote check` and the TUI banner report on open."""

    total: int
    orphaned: int
    edited_elsewhere: bool
    has_sidecar: bool

    @property
    def anchored(self) -> int:
        return self.total - self.orphaned

    def summary(self) -> str:
        if not self.has_sidecar:
            return "no comments yet"
        if not self.total:
            return "no comments"
        parts = [f"{self.total} comment{'s' if self.total != 1 else ''}"]
        if self.orphaned:
            parts.append(f"{self.orphaned} orphaned")
        if self.edited_elsewhere:
            parts.append("document edited since last save")
        return ", ".join(parts)


class MarkdownReview:
    """Read a Markdown file, list and add comments, save the sidecar.

    Mirrors the `OdtReview` interface. Tracked changes do not exist for
    Markdown and `changes()` is always empty.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sidecar = sidecar_path(self.path)
        self.source = self.path.read_text(encoding="utf-8")
        self.digest = digest(self.source)
        self.stored_digest = ""
        self._texts = split_blocks(self.source)
        self._records = self._load_records()

    @property
    def edited_elsewhere(self) -> bool:
        return bool(
            self._records and self.stored_digest and self.stored_digest != self.digest
        )

    def status(self) -> "ReviewStatus":
        """Counts for the open-time and `sidenote check` reports."""
        comments = self.comments()
        orphaned = [c for c in comments if c.orphan]
        return ReviewStatus(
            total=len(comments),
            orphaned=len(orphaned),
            edited_elsewhere=self.edited_elsewhere,
            has_sidecar=self.sidecar.exists(),
        )

    def para_texts(self) -> list[str]:
        return list(self._texts)

    def _load_records(self) -> list[_Record]:
        if not self.sidecar.exists():
            return []
        try:
            data = json.loads(self.sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"cannot read {self.sidecar.name}. {exc}") from exc
        if isinstance(data, dict):
            self.stored_digest = data.get("digest", "")
            raw = data.get("comments", [])
        else:
            raw = data
        return [_Record.from_dict(item) for item in raw]

    def comments(self) -> list[Comment]:
        """Comments relocated against the current text, in document order."""
        out: list[Comment] = []
        for record in self._records:
            start = record.start.locate(self._texts)
            end = (
                start
                if record.end is record.start
                else record.end.locate(self._texts)
            )
            orphan = start is None or end is None
            if orphan:
                fallback = self._clamp((record.start.para, record.start.off))
                start = start or fallback
                end = end or start
            if end < start:
                start, end = end, start
            out.append(
                Comment(
                    name=record.name,
                    author=record.author,
                    date=record.date,
                    text=record.text,
                    start=start,
                    end=end,
                    node=record,
                    orphan=orphan,
                )
            )
        out.sort(key=lambda c: c.start)
        return out

    def _clamp(self, pos: Pos) -> Pos:
        if not self._texts:
            return (0, 0)
        para = min(max(pos[0], 0), len(self._texts) - 1)
        return (para, min(max(pos[1], 0), len(self._texts[para])))

    def add_comment(
        self,
        start: Pos,
        end: Pos,
        text: str,
        author: str | None = None,
        date: str | None = None,
    ) -> Comment:
        """Anchor a comment from start to end, as `OdtReview.add_comment`."""
        if end < start:
            start, end = end, start
        for para, off in (start, end):
            if not 0 <= para < len(self._texts):
                raise ValueError(f"paragraph {para} out of range")
            if not 0 <= off <= len(self._texts[para]):
                raise ValueError(
                    f"offset {off} outside paragraph {para} "
                    f"of length {len(self._texts[para])}"
                )
        author = author or default_author()
        date = date or datetime.now().isoformat(timespec="seconds")
        ranged = end > start
        record = _Record(
            name=self._unique_name(),
            author=author,
            date=date,
            text=text,
            start=_Anchor.capture(self._texts, start),
            end=_Anchor.capture(self._texts, end),
            quote=_extract_range(self._texts, start, end) if ranged else "",
        )
        self._records.append(record)
        return Comment(
            name=record.name,
            author=author,
            date=date,
            text=text,
            start=start,
            end=end if ranged else start,
            node=record,
        )

    def update_comment(self, comment: Comment, text: str) -> None:
        record = self._record_for(comment)
        record.text = text
        record.date = datetime.now().isoformat(timespec="seconds")
        comment.text = text
        comment.date = record.date

    def delete_comment(self, comment: Comment) -> None:
        self._records.remove(self._record_for(comment))

    def _record_for(self, comment: Comment) -> _Record:
        if isinstance(comment.node, _Record) and comment.node in self._records:
            return comment.node
        for record in self._records:
            if record.name == comment.name:
                return record
        raise ValueError(f"comment {comment.name!r} is not in {self.sidecar.name}")

    def _unique_name(self) -> str:
        existing = {r.name for r in self._records}
        n = 1
        while f"cmt{n}" in existing:
            n += 1
        return f"cmt{n}"

    def changes(self) -> list[TrackedChange]:
        return []

    def save(self, path: str | Path | None = None) -> Path:
        """Write the sidecar. The Markdown file is never touched.

        Anchors are re-captured against the current text first, so a
        comment that was relocated on load is stored at the place it
        actually sits now, then the records are renumbered into
        document order.
        """
        self._recapture()
        self._renumber()
        target = Path(path) if path else self.sidecar
        payload = {
            "version": SIDECAR_VERSION,
            "file": self.path.name,
            "digest": self.digest,
            "comments": [r.to_dict() for r in self._records],
        }
        self.stored_digest = self.digest
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return target

    def _recapture(self) -> None:
        for comment in self.comments():
            if comment.orphan:
                continue
            record = self._record_for(comment)
            record.start = _Anchor.capture(self._texts, comment.start)
            record.end = _Anchor.capture(self._texts, comment.end)
            if comment.end > comment.start:
                record.quote = _extract_range(
                    self._texts, comment.start, comment.end
                )

    def _renumber(self) -> None:
        """Name the records cmt1..cmtN in document order, orphans last.

        The sidebar numbers comments by position, so tying the stored
        name to the same order leaves one number that means the same
        thing on screen and in the sidecar. Orphans go last because
        their position is a fallback guess, not a real anchor, and
        would otherwise renumber the live comments around them.
        """
        ordered = self.comments()
        records = [
            self._record_for(c) for c in ordered if not c.orphan
        ] + [self._record_for(c) for c in ordered if c.orphan]
        for n, record in enumerate(records, 1):
            record.name = f"cmt{n}"
        self._records = records
