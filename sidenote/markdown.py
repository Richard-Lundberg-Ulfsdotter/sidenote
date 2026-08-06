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

import getpass
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sidenote.engine import Comment, Pos, TrackedChange

SIDECAR_VERSION = 1

# characters of surrounding text stored to relocate an endpoint
CONTEXT = 40

# a lone side of context must be at least this long to relocate on its own
MIN_SOLO_CONTEXT = 8


def sidecar_path(path: str | Path) -> Path:
    """Path of the comment file belonging to a Markdown document."""
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
    Markdown and `changes()` is always empty. There is no docx export.
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
        """True when the document changed since the sidecar was written."""
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

    # ------------------------------------------------------------------
    # Document structure
    # ------------------------------------------------------------------

    def para_texts(self) -> list[str]:
        return list(self._texts)

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

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
    ) -> Comment:
        """Anchor a comment from start to end (end exclusive)."""
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
        author = author or os.environ.get("SIDENOTE_AUTHOR") or getpass.getuser()
        date = datetime.now().isoformat(timespec="seconds")
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
        """Replace a comment's text, refreshing its date."""
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

    # ------------------------------------------------------------------
    # Tracked changes
    # ------------------------------------------------------------------

    def changes(self) -> list[TrackedChange]:
        """Markdown carries no tracked changes."""
        return []

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

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

    def export_docx(self, out_dir: str | Path | None = None) -> Path:
        raise RuntimeError(
            "markdown review has no docx export. "
            "Comments live in the sidecar file next to the document."
        )
