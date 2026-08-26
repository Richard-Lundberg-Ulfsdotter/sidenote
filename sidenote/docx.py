"""Word (docx) document engine.

Comments are written straight into the OOXML. The file is opened as a
zip, only `word/document.xml` and `word/comments.xml` are rewritten,
and every other part is copied through byte for byte. Inside
document.xml only the runs that need a marker are rebuilt, so a
manuscript that came from Word goes back to Word as the same file plus
the comments.

That is the whole reason this engine exists. The alternative, and what
sidenote did before, is to convert to ODT and export back through
headless LibreOffice, which re-serialises the entire document. On a
real 22-page manuscript that halved document.xml, dropped
commentsExtended, commentsIds, commentsExtensible, people, footnotes,
endnotes and webSettings, and turned two headers into eight headers and
eight footers. The comments survived and nothing else quite did.

Positions are (paragraph_index, character_offset) into the plain text
from `para_texts()`, the same model `OdtReview` uses. Comment markers
are zero-width in that text, so adding a comment never shifts the
offsets around it.
"""

from __future__ import annotations

import getpass
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sidenote.engine import Comment, Pos, TrackedChange

DOCUMENT_PART = "word/document.xml"
COMMENTS_PART = "word/comments.xml"
CONTENT_TYPES_PART = "[Content_Types].xml"
RELS_PART = "word/_rels/document.xml.rels"

# the Word parts that key threading and resolved state off a comment
# paragraph's w14:paraId. No row is ever added, only pruned on delete,
# so a stale row cannot outlive the comment it describes.
EXTENDED_PARTS = (
    "word/commentsExtended.xml",
    "word/commentsIds.xml",
    "word/commentsExtensible.xml",
)

COMMENTS_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
)
COMMENTS_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

EMPTY_COMMENTS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<w:comments xmlns:w="{W_NS}"></w:comments>'
).encode("utf-8")

# Attribute values may legally contain ">", so the tag pattern steps
# over quoted stretches rather than scanning to the first ">".
_TAG = re.compile(rb"<(/?)([A-Za-z_][\w.:-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>")
_ATTR = re.compile(rb"([A-Za-z_][\w.:-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")

_COMMENT_EL = re.compile(rb"<w:comment(?:\s[^>]*)?>.*?</w:comment>", re.S)
_TEXT_EL = re.compile(rb"<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:tab/>|<w:br/>", re.S)
_PARA_ID = re.compile(rb'w14:paraId="([0-9A-Fa-f]+)"')

_ENTITY = re.compile(r"&(?:#(\d+)|#[xX]([0-9a-fA-F]+)|(amp|lt|gt|quot|apos));")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}

# control characters XML 1.0 forbids outside tab and newline
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# elements standing in for one character of document text
_ATOMS = {
    b"w:tab": "\t",
    b"w:br": "\n",
    b"w:cr": "\n",
    b"w:noBreakHyphen": "-",
}

# property and layout subtrees, which hold no document text but do hold
# elements this walker would otherwise mistake for it (a w:tab in
# w:pPr/w:tabs, a w:ins marking an inserted paragraph mark)
_BLOCK_SKIP = frozenset(
    {
        b"w:pPr",
        b"w:rPr",
        b"w:sdtPr",
        b"w:sdtEndPr",
        b"w:tblPr",
        b"w:tblPrEx",
        b"w:trPr",
        b"w:tcPr",
        b"w:tblGrid",
        b"w:sectPr",
    }
)


def _unescape(raw: bytes) -> str:
    def one(m: re.Match[str]) -> str:
        if m.group(1):
            return chr(int(m.group(1)))
        if m.group(2):
            return chr(int(m.group(2), 16))
        return _NAMED[m.group(3)]

    return _ENTITY.sub(one, raw.decode("utf-8"))


def _escape(text: str) -> str:
    text = _CONTROL.sub("", text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(text: str) -> str:
    return _escape(text).replace('"', "&quot;")


def _attrs(raw: bytes) -> dict[bytes, str]:
    return {
        m.group(1): _unescape(m.group(2) if m.group(2) is not None else m.group(3))
        for m in _ATTR.finditer(raw)
    }


def _text_element(text: str) -> bytes:
    return (
        b'<w:t xml:space="preserve">'
        + _escape(text).encode("utf-8")
        + b"</w:t>"
    )


@dataclass
class _Child:
    """One direct child of a run, with the text it contributes.

    text is None for anything zero-width, which includes drawings,
    field characters and deleted text. Those are re-emitted from their
    original bytes when a run is split, never rebuilt.
    """

    start: int
    end: int
    text: str | None


@dataclass
class _Run:
    start: int
    end: int
    open_tag: bytes
    rpr: bytes = b""
    children: list[_Child] = field(default_factory=list)
    flat_start: int = 0
    flat_len: int = 0


@dataclass
class _Mark:
    """A comment marker in the body, with the bytes that carry it."""

    kind: str  # "start", "end" or "ref"
    cid: str
    offset: int
    start: int
    end: int


@dataclass
class _Para:
    start: int
    end: int
    content_start: int
    content_end: int
    text: str = ""
    runs: list[_Run] = field(default_factory=list)
    marks: list[_Mark] = field(default_factory=list)


class _Walker:
    """One pass over document.xml, collecting paragraphs and changes.

    Everything the engine needs comes out of this walk. Paragraphs are
    the w:p elements that are not inside a drawing or an embedded
    object, in document order, table cells included. A run's children
    are recorded whole so that splitting a run for a marker cannot drop
    an image or a field.
    """

    def __init__(self, data: bytes):
        self.data = data
        self.paras: list[_Para] = []
        self.changes: list[TrackedChange] = []
        self.depth = 0
        self.skip_at: int | None = None
        self.skip_kind = ""
        self.skip_start = 0
        self.text_content = 0
        self.para: _Para | None = None
        self.para_depth = -1
        self.offset = 0
        self.parts: list[str] = []
        self.run: _Run | None = None
        self.run_depth = -1
        self.ref_mark: _Mark | None = None
        self.ins: list[tuple[int, str, str, int]] = []
        self.dele: list[list] = []
        self.pending: list[tuple] = []
        self._walk()

    def _walk(self) -> None:
        for m in _TAG.finditer(self.data):
            name = m.group(2)
            if m.group(1):
                self.depth -= 1
                self._close(name, m)
            else:
                self._open(name, m.group(3), m)
                if m.group(3).endswith(b"/"):
                    self._close(name, m)
                else:
                    self.depth += 1

    def _open(self, name: bytes, attrs: bytes, m: re.Match[bytes]) -> None:
        if self.skip_at is not None:
            return
        d = self.depth
        if self.run is not None and d == self.run_depth + 1:
            self._open_run_child(name, m, d)
            return
        if name in _BLOCK_SKIP:
            self.skip_at, self.skip_kind, self.skip_start = d, "none", m.start()
            return
        if name == b"w:p":
            if self.para is None:
                self.para = _Para(m.start(), 0, m.end(), 0)
                self.para_depth = d
                self.offset = 0
                self.parts = []
                self.pending = []
            return
        if self.para is None:
            return
        if name == b"w:r":
            self.run = _Run(m.start(), 0, self.data[m.start() : m.end()])
            self.run.flat_start = self.offset
            self.run_depth = d
            self.ref_mark = None
        elif name in (b"w:commentRangeStart", b"w:commentRangeEnd"):
            cid = _attrs(attrs).get(b"w:id")
            if cid is not None:
                kind = "start" if name.endswith(b"Start") else "end"
                self.para.marks.append(
                    _Mark(kind, cid, self.offset, m.start(), m.end())
                )
        elif name == b"w:ins":
            a = _attrs(attrs)
            self.ins.append(
                (d, a.get(b"w:author", ""), a.get(b"w:date", ""), self.offset)
            )
        elif name == b"w:del":
            a = _attrs(attrs)
            self.dele.append(
                [d, a.get(b"w:author", ""), a.get(b"w:date", ""), self.offset, []]
            )

    def _open_run_child(self, name: bytes, m: re.Match[bytes], d: int) -> None:
        """Every child of a run is claimed here, then closed in one place."""
        if name == b"w:rPr":
            kind = "rpr"
        elif name in (b"w:t", b"w:delText"):
            kind = name.decode()
            self.text_content = m.end()
        elif name in _ATOMS:
            kind = "atom"
        else:
            kind = "opaque"
            if name == b"w:commentReference":
                cid = _attrs(m.group(3)).get(b"w:id")
                if cid is not None and self.para is not None:
                    self.ref_mark = _Mark(
                        "ref", cid, self.offset, m.start(), m.end()
                    )
                    self.para.marks.append(self.ref_mark)
        self.skip_at, self.skip_kind, self.skip_start = d, kind, m.start()

    def _close(self, name: bytes, m: re.Match[bytes]) -> None:
        d = self.depth
        if self.skip_at is not None:
            if self.skip_at == d:
                self._close_skipped(name, m)
            return
        if self.run is not None and d == self.run_depth:
            self._close_run(m)
            return
        if self.para is None:
            return
        if name == b"w:ins" and self.ins and self.ins[-1][0] == d:
            _, author, date, start = self.ins.pop()
            if self.offset > start:
                self.pending.append(
                    ("insertion", author, date, start, self.offset, None)
                )
        elif name == b"w:del" and self.dele and self.dele[-1][0] == d:
            _, author, date, start, chunks = self.dele.pop()
            if chunks:
                self.pending.append(
                    ("deletion", author, date, start, start, "".join(chunks))
                )
        elif name == b"w:p" and d == self.para_depth:
            self._close_para(m)

    def _close_skipped(self, name: bytes, m: re.Match[bytes]) -> None:
        kind, start = self.skip_kind, self.skip_start
        self.skip_at = None
        if kind == "none":
            return
        run = self.run
        if run is None:
            return
        if kind == "rpr":
            run.rpr = self.data[start : m.end()]
            return
        if kind == "atom":
            text = _ATOMS[name]
        elif kind == "w:t":
            text = _unescape(self.data[self.text_content : m.start()])
        else:
            if kind == "w:delText" and self.dele:
                self.dele[-1][4].append(
                    _unescape(self.data[self.text_content : m.start()])
                )
            run.children.append(_Child(start, m.end(), None))
            return
        run.children.append(_Child(start, m.end(), text))
        self.parts.append(text)
        self.offset += len(text)

    def _close_run(self, m: re.Match[bytes]) -> None:
        run = self.run
        run.end = m.end()
        run.flat_len = self.offset - run.flat_start
        # Word wraps a lone commentReference in its own run, and that
        # whole run is what a delete has to take away
        if (
            self.ref_mark is not None
            and run.flat_len == 0
            and len(run.children) == 1
        ):
            self.ref_mark.start, self.ref_mark.end = run.start, run.end
        self.para.runs.append(run)
        self.run = None
        self.ref_mark = None

    def _close_para(self, m: re.Match[bytes]) -> None:
        para = self.para
        para.end = m.end()
        para.content_end = m.start()
        para.text = "".join(self.parts)
        index = len(self.paras)
        self.paras.append(para)
        for kind, author, date, start, end, text in self.pending:
            self.changes.append(
                TrackedChange(
                    kind,
                    author,
                    date,
                    text if text is not None else para.text[start:end],
                    (index, start),
                    (index, end),
                )
            )
        self.para = None
        self.pending = []


@dataclass
class _Record:
    """A comment as it sits in comments.xml."""

    cid: str
    author: str
    date: str
    text: str
    start: int
    end: int


def _parse_comments(data: bytes) -> list[_Record]:
    out: list[_Record] = []
    for m in _COMMENT_EL.finditer(data):
        blob = m.group(0)
        head = blob[: blob.index(b">") + 1]
        a = _attrs(head)
        cid = a.get(b"w:id")
        if cid is None:
            continue
        out.append(
            _Record(
                cid,
                a.get(b"w:author", ""),
                a.get(b"w:date", ""),
                _comment_text(blob),
                m.start(),
                m.end(),
            )
        )
    return out


def _comment_text(blob: bytes) -> str:
    """The prose of one w:comment, one line per paragraph."""
    chunks = blob.split(b"</w:p>")[:-1] or [blob]
    lines: list[str] = []
    for chunk in chunks:
        parts: list[str] = []
        for m in _TEXT_EL.finditer(chunk):
            if m.group(1) is not None:
                parts.append(_unescape(m.group(1)))
            elif m.group(0) == b"<w:tab/>":
                parts.append("\t")
            else:
                parts.append("\n")
        lines.append("".join(parts))
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _initials(author: str) -> str:
    letters = [word[0] for word in re.split(r"[\s.-]+", author) if word]
    return "".join(letters[:3]).upper()


class DocxReview:
    """Read a docx, list and add comments, save it as a docx.

    Mirrors the `OdtReview` interface. There is no working copy and no
    conversion step, so `export_docx` only writes the document out,
    possibly to another directory.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            names = set(z.namelist())
            if DOCUMENT_PART not in names:
                raise RuntimeError(f"{self.path.name} has no {DOCUMENT_PART}")
            self.doc = z.read(DOCUMENT_PART)
            self.com = z.read(COMMENTS_PART) if COMMENTS_PART in names else None
            self.types = z.read(CONTENT_TYPES_PART)
            self.rels = z.read(RELS_PART) if RELS_PART in names else b""
            self.extended = {
                name: z.read(name) for name in EXTENDED_PARTS if name in names
            }
        self._index()

    def _index(self) -> None:
        walker = _Walker(self.doc)
        self.paras = walker.paras
        self._changes = sorted(walker.changes, key=lambda c: c.start)
        self._records = _parse_comments(self.com) if self.com else []

    def para_texts(self) -> list[str]:
        return [p.text for p in self.paras]

    def changes(self) -> list[TrackedChange]:
        return list(self._changes)

    # ------------------------------------------------------------------
    # reading comments

    def comments(self) -> list[Comment]:
        """Comments paired with their body markers, in document order.

        A comment whose markers are missing from the body is reported
        as an orphan at the top of the document rather than dropped,
        because it is still in comments.xml and still saved.
        """
        starts: dict[str, Pos] = {}
        ends: dict[str, Pos] = {}
        refs: dict[str, Pos] = {}
        for index, para in enumerate(self.paras):
            for mark in para.marks:
                where = {"start": starts, "end": ends, "ref": refs}[mark.kind]
                where.setdefault(mark.cid, (index, mark.offset))

        out: list[Comment] = []
        for record in self._records:
            start = starts.get(record.cid)
            end = ends.get(record.cid)
            ref = refs.get(record.cid)
            if start is None:
                start = end = ref
            elif end is None:
                end = ref or start
            orphan = start is None
            if orphan:
                start = end = (0, 0)
            if end < start:
                start, end = end, start
            out.append(
                Comment(
                    name=record.cid,
                    author=record.author,
                    date=record.date,
                    text=record.text,
                    start=start,
                    end=end,
                    node=record.cid,
                    orphan=orphan,
                )
            )
        out.sort(key=lambda c: (c.orphan, c.start))
        return out

    # ------------------------------------------------------------------
    # writing comments

    def add_comment(
        self,
        start: Pos,
        end: Pos,
        text: str,
        author: str | None = None,
        date: str | None = None,
    ) -> Comment:
        """Anchor a comment from start to end, as `OdtReview.add_comment`.

        A ranged comment gets commentRangeStart and commentRangeEnd
        around the span plus a reference run at the end, which is what
        Word writes. A point comment gets the reference run alone.
        """
        if end < start:
            start, end = end, start
        texts = self.para_texts()
        for para_idx, off in (start, end):
            if not 0 <= para_idx < len(texts):
                raise ValueError(f"paragraph {para_idx} out of range")
            if not 0 <= off <= len(texts[para_idx]):
                raise ValueError(
                    f"offset {off} outside paragraph {para_idx} "
                    f"of length {len(texts[para_idx])}"
                )
        author = author or os.environ.get("SIDENOTE_AUTHOR") or getpass.getuser()
        date = date or datetime.now().isoformat(timespec="seconds")
        self._ensure_comments_part()
        # resolved before the body is touched, so a comments part this
        # engine cannot append to leaves the document unmodified
        tail = self.com.rfind(b"</w:comments>")
        if tail < 0:
            raise RuntimeError(f"{COMMENTS_PART} has no </w:comments>")
        cid = self._next_id()
        ranged = end > start
        reference = (
            b'<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
            b'<w:commentReference w:id="%s"/></w:r>' % cid.encode()
        )
        if ranged:
            marks = [
                (start[0], start[1], b'<w:commentRangeStart w:id="%s"/>' % cid.encode()),
                (
                    end[0],
                    end[1],
                    b'<w:commentRangeEnd w:id="%s"/>' % cid.encode() + reference,
                ),
            ]
        else:
            marks = [(start[0], start[1], reference)]
        self._apply(marks)
        element = self._comment_element(cid, author, date, text)
        self.com = self.com[:tail] + element + self.com[tail:]
        self._index()
        return Comment(
            name=cid,
            author=author,
            date=date,
            text=text,
            start=start,
            end=end if ranged else start,
            node=cid,
        )

    def update_comment(self, comment: Comment, text: str) -> None:
        """Replace a comment's text, refreshing its date."""
        record = self._record_for(comment)
        date = datetime.now().isoformat(timespec="seconds")
        element = self._comment_element(record.cid, record.author, date, text)
        self.com = self.com[: record.start] + element + self.com[record.end :]
        self._index()
        comment.text = text
        comment.date = date

    def delete_comment(self, comment: Comment) -> None:
        record = self._record_for(comment)
        spans = [
            (mark.start, mark.end)
            for para in self.paras
            for mark in para.marks
            if mark.cid == record.cid
        ]
        for begin, stop in sorted(spans, reverse=True):
            self.doc = self.doc[:begin] + self.doc[stop:]
        self._prune_extended(self.com[record.start : record.end])
        self.com = self.com[: record.start] + self.com[record.end :]
        self._index()

    def _record_for(self, comment: Comment) -> _Record:
        for record in self._records:
            if record.cid == comment.name:
                return record
        raise ValueError(f"comment {comment.name!r} is not in {COMMENTS_PART}")

    def _next_id(self) -> str:
        used = {int(r.cid) for r in self._records if r.cid.isdigit()}
        used |= {
            int(mark.cid)
            for para in self.paras
            for mark in para.marks
            if mark.cid.isdigit()
        }
        return str(max(used) + 1 if used else 0)

    def _comment_element(
        self, cid: str, author: str, date: str, text: str
    ) -> bytes:
        head = (
            f'<w:comment w:id="{_attr(cid)}" w:author="{_attr(author)}" '
            f'w:date="{_attr(date)}" w:initials="{_attr(_initials(author))}">'
        )
        body: list[str] = []
        for n, line in enumerate(text.split("\n") or [""]):
            ref = (
                '<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
                "<w:annotationRef/></w:r>"
                if n == 0
                else ""
            )
            body.append(
                '<w:p><w:pPr><w:pStyle w:val="CommentText"/></w:pPr>'
                f"{ref}<w:r><w:t xml:space=\"preserve\">{_escape(line)}"
                "</w:t></w:r></w:p>"
            )
        return (head + "".join(body) + "</w:comment>").encode("utf-8")

    def _prune_extended(self, element: bytes) -> None:
        """Drop the Word-only rows keyed off a deleted comment's paraIds."""
        para_ids = set(_PARA_ID.findall(element))
        if not para_ids:
            return
        durable: set[bytes] = set()
        ids_part = self.extended.get("word/commentsIds.xml", b"")
        for m in re.finditer(rb"<w16cid:commentId\s[^>]*/>", ids_part):
            attrs = _attrs(m.group(0))
            if attrs.get(b"w16cid:paraId", "").encode() in para_ids:
                durable.add(attrs.get(b"w16cid:durableId", "").encode())
        for name, data in list(self.extended.items()):
            pruned = _drop_rows(data, para_ids, durable)
            if pruned != data:
                self.extended[name] = pruned

    # ------------------------------------------------------------------
    # splicing the body

    def _apply(self, marks: list[tuple[int, int, bytes]]) -> None:
        """Insert marker bytes at (paragraph, offset) positions."""
        wanted: dict[int, dict[int, bytes]] = {}
        for para_idx, off, blob in marks:
            slot = wanted.setdefault(para_idx, {})
            slot[off] = slot.get(off, b"") + blob
        edits: list[tuple[int, int, bytes]] = []
        for para_idx, offsets in wanted.items():
            edits.extend(self._para_edits(self.paras[para_idx], offsets))
        edits.sort(key=lambda e: -e[0])
        for a, b in zip(edits, edits[1:]):
            if b[1] > a[0]:
                raise RuntimeError("overlapping edits in one paragraph")
        for begin, stop, blob in edits:
            self.doc = self.doc[:begin] + blob + self.doc[stop:]

    def _para_edits(
        self, para: _Para, offsets: dict[int, bytes]
    ) -> list[tuple[int, int, bytes]]:
        """Turn wanted offsets into byte edits, splitting runs as needed.

        A marker is a run-level element, so it cannot go inside a run.
        An offset that lands between runs becomes a plain insertion, one
        that lands inside a run makes that run split in two around it.
        """
        at_byte: dict[int, bytes] = {}
        run_cuts: dict[int, dict[int, bytes]] = {}
        for off in sorted(offsets):
            blob = offsets[off]
            index = next(
                (
                    i
                    for i, run in enumerate(para.runs)
                    if run.flat_start < off < run.flat_start + run.flat_len
                ),
                None,
            )
            if index is None:
                pos = next(
                    (run.start for run in para.runs if run.flat_start >= off),
                    para.content_end,
                )
                at_byte[pos] = at_byte.get(pos, b"") + blob
            else:
                cuts = run_cuts.setdefault(index, {})
                cuts[off - para.runs[index].flat_start] = blob

        edits: list[tuple[int, int, bytes]] = []
        for index, cuts in run_cuts.items():
            run = para.runs[index]
            # a marker landing exactly where this run starts belongs in
            # front of the rebuilt run, not in a separate edit that
            # would overlap it
            lead = at_byte.pop(run.start, b"")
            edits.append((run.start, run.end, lead + self._rebuild(run, cuts)))
        for pos, blob in at_byte.items():
            edits.append((pos, pos, blob))
        return edits

    def _rebuild(self, run: _Run, cuts: dict[int, bytes]) -> bytes:
        """Re-emit one run as sub-runs with the markers between them.

        Children other than the one being cut are copied from their
        original bytes, so run properties, images and field characters
        come through untouched.
        """
        out: list[bytes] = []
        frag: list[bytes] = []

        def flush() -> None:
            if frag:
                out.append(
                    run.open_tag + run.rpr + b"".join(frag) + b"</w:r>"
                )
                frag.clear()

        pending = sorted(cuts.items())
        pos = 0
        for child in run.children:
            length = len(child.text) if child.text is not None else 0
            while pending and pending[0][0] <= pos:
                flush()
                out.append(pending.pop(0)[1])
            if length and pending and pos < pending[0][0] < pos + length:
                local = 0
                while pending and pos < pending[0][0] < pos + length:
                    cut, blob = pending.pop(0)
                    frag.append(_text_element(child.text[local : cut - pos]))
                    flush()
                    out.append(blob)
                    local = cut - pos
                frag.append(_text_element(child.text[local:]))
            else:
                frag.append(self.doc[child.start : child.end])
            pos += length
        while pending:
            flush()
            out.append(pending.pop(0)[1])
        flush()
        return b"".join(out)

    # ------------------------------------------------------------------
    # the comments part

    def _ensure_comments_part(self) -> None:
        """Make word/comments.xml exist, be appendable, and be registered.

        A document that has never carried a comment may still ship the
        part with an empty self-closing root, which pandoc writes and
        which has no closing tag to append to. Both registration guards
        are idempotent, so a part that is already declared is left as
        it is.
        """
        if self.com is None:
            self.com = EMPTY_COMMENTS
        elif b"</w:comments>" not in self.com:
            self.com = _open_empty_root(self.com)
        if COMMENTS_TYPE.encode() not in self.types:
            tail = self.types.rfind(b"</Types>")
            if tail < 0:
                raise RuntimeError(f"{CONTENT_TYPES_PART} has no </Types>")
            override = (
                f'<Override PartName="/{COMMENTS_PART}" '
                f'ContentType="{COMMENTS_TYPE}"/>'
            ).encode()
            self.types = self.types[:tail] + override + self.types[tail:]
        if COMMENTS_REL.encode() not in self.rels:
            tail = self.rels.rfind(b"</Relationships>")
            if tail < 0:
                raise RuntimeError(f"{RELS_PART} has no </Relationships>")
            used = [int(n) for n in re.findall(rb'Id="rId(\d+)"', self.rels)]
            rel = (
                f'<Relationship Id="rId{max(used) + 1 if used else 1}" '
                f'Type="{COMMENTS_REL}" Target="comments.xml"/>'
            ).encode()
            self.rels = self.rels[:tail] + rel + self.rels[tail:]

    # ------------------------------------------------------------------
    # output

    def save(self, path: str | Path | None = None) -> Path:
        """Rewrite the zip, copying every part this engine did not touch.

        The new file is built beside the target and moved into place, so
        an interrupted save leaves the document as it was.
        """
        target = Path(path) if path else self.path
        changed = {
            DOCUMENT_PART: self.doc,
            COMMENTS_PART: self.com,
            CONTENT_TYPES_PART: self.types,
            RELS_PART: self.rels,
            **self.extended,
        }
        temp = target.with_name(target.name + ".sidenote-tmp")
        try:
            with zipfile.ZipFile(self.path) as zin, zipfile.ZipFile(
                temp, "w", zipfile.ZIP_DEFLATED
            ) as zout:
                seen = set()
                for item in zin.infolist():
                    seen.add(item.filename)
                    data = changed.get(item.filename)
                    zout.writestr(
                        item,
                        zin.read(item.filename) if data is None else data,
                    )
                if COMMENTS_PART not in seen and self.com is not None:
                    info = zipfile.ZipInfo(
                        COMMENTS_PART, date_time=(1980, 1, 1, 0, 0, 0)
                    )
                    info.compress_type = zipfile.ZIP_DEFLATED
                    zout.writestr(info, self.com)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        return target

    def export_docx(self, out_dir: str | Path | None = None) -> Path:
        """Write the document out. It is already a docx, so this saves."""
        out_dir = Path(out_dir) if out_dir else self.path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        return self.save(out_dir / self.path.name)


def _open_empty_root(data: bytes) -> bytes:
    """Turn `<w:comments .../>` into an open and close pair.

    The namespace declarations are kept exactly as they are. They are
    what a comment body's markup is resolved against, and a document
    that ships this root usually declares more of them than sidenote
    would.
    """
    # the lookahead is what stops this matching a longer tag name that
    # merely starts with "w:comments"
    m = re.search(
        rb"<w:comments(?=[\s/>])((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>", data
    )
    if m is None or not m.group(1).endswith(b"/"):
        raise RuntimeError(f"{COMMENTS_PART} has no </w:comments>")
    head = b"<w:comments" + m.group(1)[:-1].rstrip() + b"></w:comments>"
    return data[: m.start()] + head + data[m.end() :]


def _drop_rows(data: bytes, para_ids: set[bytes], durable: set[bytes]) -> bytes:
    """Remove the rows of a Word comment side-table naming a dead comment."""
    row = re.compile(rb"<(w15|w16cid|w16cex):[A-Za-z]+\s[^>]*/>")

    def keep(m: re.Match[bytes]) -> bytes:
        attrs = _attrs(m.group(0))
        for key, pool in (
            (b"w15:paraId", para_ids),
            (b"w16cid:paraId", para_ids),
            (b"w16cex:durableId", durable),
        ):
            value = attrs.get(key)
            if value is not None and value.encode() in pool:
                return b""
        return m.group(0)

    return row.sub(keep, data)
