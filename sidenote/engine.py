"""ODT document engine.

Loads an ODT file, exposes its paragraphs as plain text, and anchors
comments at exact character offsets using ODF's inline annotation model
(office:annotation paired with office:annotation-end for ranged comments).
Export to docx is delegated to headless LibreOffice, which maps named
annotation ranges to Word commentRangeStart/commentRangeEnd.

Positions are (paragraph_index, character_offset) pairs. Offsets index
into the plain text returned by para_texts(), where existing annotation
elements contribute zero width, so adding a comment never shifts the
offsets of the surrounding text.
"""

from __future__ import annotations

import getpass
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from odf import dc
from odf.element import Element, Node, Text
from odf.namespaces import DCNS, OFFICENS, TEXTNS, XMLNS
from odf.office import Annotation, AnnotationEnd
from odf.opendocument import load
from odf.text import ChangedRegion, P, S

P_QNAME = (TEXTNS, "p")
H_QNAME = (TEXTNS, "h")
S_QNAME = (TEXTNS, "s")
TAB_QNAME = (TEXTNS, "tab")
LINEBREAK_QNAME = (TEXTNS, "line-break")
ANNOTATION_QNAME = (OFFICENS, "annotation")
ANNOTATION_END_QNAME = (OFFICENS, "annotation-end")
TRACKED_CHANGES_QNAME = (TEXTNS, "tracked-changes")
CHANGE_QNAME = (TEXTNS, "change")
CHANGE_START_QNAME = (TEXTNS, "change-start")
CHANGE_END_QNAME = (TEXTNS, "change-end")
INSERTION_QNAME = (TEXTNS, "insertion")
DELETION_QNAME = (TEXTNS, "deletion")
CHANGE_INFO_QNAME = (OFFICENS, "change-info")

# zero-width marker elements collected with their text offsets
MARK_QNAMES = (
    ANNOTATION_QNAME,
    ANNOTATION_END_QNAME,
    CHANGE_QNAME,
    CHANGE_START_QNAME,
    CHANGE_END_QNAME,
)

Pos = tuple[int, int]


def _soffice_convert(source: Path, to: str, out_dir: Path) -> Path:
    result = subprocess.run(
        [
            "soffice",
            "--headless",
            "--convert-to",
            to,
            "--outdir",
            str(out_dir),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    target = out_dir / (source.stem + f".{to}")
    if result.returncode != 0 or not target.exists():
        raise RuntimeError(
            f"soffice conversion failed. {result.stderr.strip() or result.stdout.strip()}"
        )
    return target


def docx_to_odt(path: str | Path, force: bool = False) -> Path:
    """Convert a docx to a sibling ODT working copy via headless LibreOffice.

    An existing sibling .odt is reused when it is at least as new as the
    docx, so comments already added to the working copy are not lost on
    reopening the same docx.
    """
    path = Path(path)
    target = path.with_suffix(".odt")
    if (
        target.exists()
        and not force
        and target.stat().st_mtime >= path.stat().st_mtime
    ):
        return target
    return _soffice_convert(path, "odt", path.parent)


@dataclass
class Comment:
    """A comment anchored in the document.

    start and end are (paragraph_index, offset) positions. A point
    comment has start == end. The node references are used internally
    for deletion.

    orphan marks a comment whose anchor text could not be found, which
    only happens for sidecar-backed formats where the document can be
    edited outside sidenote. It is always False for ODT, where the
    anchor is an element inside the document itself.
    """

    name: str | None
    author: str
    date: str
    text: str
    start: Pos
    end: Pos
    node: Element = field(repr=False, compare=False, default=None)
    end_node: Element | None = field(repr=False, compare=False, default=None)
    orphan: bool = False


@dataclass
class TrackedChange:
    """A tracked change read from the document, never written.

    Insertions span (start, end) in the body text. Deletions are a
    point (start == end) where the text used to be, with the removed
    text recovered from the tracked-changes metadata block.
    """

    kind: str
    author: str
    date: str
    text: str
    start: Pos
    end: Pos


@dataclass
class _Seg:
    """One leaf of a paragraph flattened to plain-text segments."""

    start: int
    length: int
    node: Node
    parent: Element


def _space_count(s_element: Element) -> int:
    c = s_element.getAttribute("c")
    return int(c) if c else 1


def _plain_text(el: Element) -> str:
    """Plain text of an element, skipping annotation bodies."""
    parts: list[str] = []
    for node in el.childNodes:
        if node.nodeType == Node.TEXT_NODE:
            parts.append(node.data)
        elif node.nodeType == Node.ELEMENT_NODE:
            q = node.qname
            if q == S_QNAME:
                parts.append(" " * _space_count(node))
            elif q == TAB_QNAME:
                parts.append("\t")
            elif q == LINEBREAK_QNAME:
                parts.append("\n")
            elif q in (ANNOTATION_QNAME, ANNOTATION_END_QNAME):
                continue
            else:
                parts.append(_plain_text(node))
    return "".join(parts)


def _flatten(el: Element, segs: list[_Seg], offset: int) -> int:
    """Collect the leaf nodes of a paragraph with their text offsets."""
    for node in el.childNodes:
        if node.nodeType == Node.TEXT_NODE:
            segs.append(_Seg(offset, len(node.data), node, el))
            offset += len(node.data)
        elif node.nodeType == Node.ELEMENT_NODE:
            q = node.qname
            if q == S_QNAME:
                n = _space_count(node)
                segs.append(_Seg(offset, n, node, el))
                offset += n
            elif q in (TAB_QNAME, LINEBREAK_QNAME):
                segs.append(_Seg(offset, 1, node, el))
                offset += 1
            elif q in (ANNOTATION_QNAME, ANNOTATION_END_QNAME):
                continue
            else:
                offset = _flatten(node, segs, offset)
    return offset


def _extract_range(texts: list[str], start: Pos, end: Pos) -> str:
    (p1, o1), (p2, o2) = start, end
    if p1 == p2:
        return texts[p1][o1:o2]
    parts = [texts[p1][o1:]]
    parts.extend(texts[i] for i in range(p1 + 1, p2))
    parts.append(texts[p2][:o2])
    return "\n".join(parts)


def _insert_after(parent: Element, ref: Node, new_node: Node) -> None:
    idx = parent.childNodes.index(ref)
    if idx + 1 < len(parent.childNodes):
        parent.insertBefore(new_node, parent.childNodes[idx + 1])
    else:
        parent.appendChild(new_node)


def _insert_at(paragraph: Element, offset: int, new_node: Element) -> None:
    """Insert new_node at a character offset, splitting leaves as needed."""
    segs: list[_Seg] = []
    total = _flatten(paragraph, segs, 0)
    if not 0 <= offset <= total:
        raise ValueError(f"offset {offset} outside paragraph of length {total}")
    for seg in segs:
        if seg.start == offset:
            seg.parent.insertBefore(new_node, seg.node)
            return
        if seg.start < offset < seg.start + seg.length:
            k = offset - seg.start
            if seg.node.nodeType == Node.TEXT_NODE:
                tail = Text(seg.node.data[k:])
                seg.node.data = seg.node.data[:k]
                _insert_after(seg.parent, seg.node, new_node)
                _insert_after(seg.parent, new_node, tail)
            elif seg.node.qname == S_QNAME:
                seg.node.setAttribute("c", str(k))
                tail = S(c=str(seg.length - k))
                _insert_after(seg.parent, seg.node, new_node)
                _insert_after(seg.parent, new_node, tail)
            else:
                # tab and line-break have length 1, an interior offset
                # cannot occur
                raise ValueError("cannot split element")
            return
    if segs:
        last = segs[-1]
        _insert_after(last.parent, last.node, new_node)
    else:
        paragraph.appendChild(new_node)


class OdtReview:
    """Read an ODT file, list and add comments, save, export to docx."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.doc = load(str(self.path))

    # ------------------------------------------------------------------
    # Document structure
    # ------------------------------------------------------------------

    @property
    def paragraphs(self) -> list[Element]:
        out: list[Element] = []
        self._collect_paragraphs(self.doc.text, out)
        return out

    def _collect_paragraphs(self, el: Element, out: list[Element]) -> None:
        for node in el.childNodes:
            if node.nodeType != Node.ELEMENT_NODE:
                continue
            # tracked-changes holds deleted/changed fragments as
            # text:p metadata, not document content
            if node.qname in (
                ANNOTATION_QNAME,
                ANNOTATION_END_QNAME,
                TRACKED_CHANGES_QNAME,
            ):
                continue
            if node.qname in (P_QNAME, H_QNAME):
                out.append(node)
            else:
                self._collect_paragraphs(node, out)

    def para_texts(self) -> list[str]:
        return [_plain_text(p) for p in self.paragraphs]

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def comments(self) -> list[Comment]:
        out: list[Comment] = []
        open_ranges: dict[str, Comment] = {}
        for para_idx, offset, node in self._marks():
            q = node.qname
            if q == ANNOTATION_QNAME:
                comment = self._read_annotation(node, (para_idx, offset))
                out.append(comment)
                if comment.name:
                    open_ranges[comment.name] = comment
            elif q == ANNOTATION_END_QNAME:
                name = node.getAttrNS(OFFICENS, "name")
                if name in open_ranges:
                    open_ranges[name].end = (para_idx, offset)
                    open_ranges[name].end_node = node
                    del open_ranges[name]
        return out

    def _marks(self) -> list[tuple[int, int, Element]]:
        """All zero-width markers as (paragraph, offset, element)."""
        marks: list[tuple[int, int, Element]] = []
        for i, par in enumerate(self.paragraphs):
            self._collect_marks(par, i, 0, marks)
        return marks

    def _collect_marks(
        self,
        el: Element,
        para_idx: int,
        offset: int,
        marks: list[tuple[int, int, Element]],
    ) -> int:
        for node in el.childNodes:
            if node.nodeType == Node.TEXT_NODE:
                offset += len(node.data)
            elif node.nodeType == Node.ELEMENT_NODE:
                q = node.qname
                if q == S_QNAME:
                    offset += _space_count(node)
                elif q in (TAB_QNAME, LINEBREAK_QNAME):
                    offset += 1
                elif q in MARK_QNAMES:
                    marks.append((para_idx, offset, node))
                else:
                    offset = self._collect_marks(node, para_idx, offset, marks)
        return offset

    # ------------------------------------------------------------------
    # Tracked changes (read only)
    # ------------------------------------------------------------------

    def changes(self) -> list[TrackedChange]:
        """Tracked changes in document order.

        Insertions come from inline change-start/end markers, their
        text read straight from the body. Deletions come from the
        point marker plus the removed text stored in the metadata
        block. Format-only changes are ignored.
        """
        regions = self._change_regions()
        texts = self.para_texts()
        out: list[TrackedChange] = []
        open_starts: dict[str, Pos] = {}
        for para_idx, offset, node in self._marks():
            q = node.qname
            if q == CHANGE_START_QNAME:
                cid = node.getAttrNS(TEXTNS, "change-id")
                open_starts[cid] = (para_idx, offset)
            elif q == CHANGE_END_QNAME:
                cid = node.getAttrNS(TEXTNS, "change-id")
                region = regions.get(cid)
                start = open_starts.pop(cid, None)
                if region is None or start is None:
                    continue
                if region["kind"] != "insertion":
                    continue
                end = (para_idx, offset)
                out.append(
                    TrackedChange(
                        "insertion",
                        region["author"],
                        region["date"],
                        _extract_range(texts, start, end),
                        start,
                        end,
                    )
                )
            elif q == CHANGE_QNAME:
                cid = node.getAttrNS(TEXTNS, "change-id")
                region = regions.get(cid)
                if region is None or region["kind"] != "deletion":
                    continue
                pos = (para_idx, offset)
                out.append(
                    TrackedChange(
                        "deletion",
                        region["author"],
                        region["date"],
                        region["text"],
                        pos,
                        pos,
                    )
                )
        out.sort(key=lambda c: c.start)
        return out

    def _change_regions(self) -> dict[str, dict]:
        """Metadata for each changed region, keyed by change id."""
        regions: dict[str, dict] = {}
        for region in self.doc.text.getElementsByType(ChangedRegion):
            rid = region.getAttrNS(TEXTNS, "id") or region.getAttrNS(
                XMLNS, "id"
            )
            if not rid:
                continue
            body = None
            kind = None
            for child in region.childNodes:
                if child.nodeType != Node.ELEMENT_NODE:
                    continue
                if child.qname == INSERTION_QNAME:
                    kind, body = "insertion", child
                elif child.qname == DELETION_QNAME:
                    kind, body = "deletion", child
            if body is None:
                continue
            author = date = ""
            deleted: list[str] = []
            for child in body.childNodes:
                if child.nodeType != Node.ELEMENT_NODE:
                    continue
                if child.qname == CHANGE_INFO_QNAME:
                    for info in child.childNodes:
                        if info.nodeType != Node.ELEMENT_NODE:
                            continue
                        if info.qname == (DCNS, "creator"):
                            author = _plain_text(info)
                        elif info.qname == (DCNS, "date"):
                            date = _plain_text(info)
                elif child.qname in (P_QNAME, H_QNAME):
                    deleted.append(_plain_text(child))
            regions[rid] = {
                "kind": kind,
                "author": author,
                "date": date,
                "text": "\n".join(deleted),
            }
        return regions

    def _read_annotation(self, node: Element, pos: Pos) -> Comment:
        creators = node.getElementsByType(dc.Creator)
        dates = node.getElementsByType(dc.Date)
        body = [
            _plain_text(c)
            for c in node.childNodes
            if c.nodeType == Node.ELEMENT_NODE and c.qname == P_QNAME
        ]
        return Comment(
            name=node.getAttrNS(OFFICENS, "name"),
            author=_plain_text(creators[0]) if creators else "",
            date=_plain_text(dates[0]) if dates else "",
            text="\n".join(body),
            start=pos,
            end=pos,
            node=node,
        )

    def add_comment(
        self,
        start: Pos,
        end: Pos,
        text: str,
        author: str | None = None,
        date: str | None = None,
    ) -> Comment:
        """Anchor a comment from start to end (end exclusive).

        start == end creates a point comment. Ranged comments get an
        office:name so LibreOffice exports them as Word comment ranges.
        date defaults to now and is passed explicitly only when putting
        a deleted comment back, which must not restamp it.
        """
        if end < start:
            start, end = end, start
        paras = self.paragraphs
        texts = [_plain_text(p) for p in paras]
        for para_idx, off in (start, end):
            if not 0 <= para_idx < len(paras):
                raise ValueError(f"paragraph {para_idx} out of range")
            if not 0 <= off <= len(texts[para_idx]):
                raise ValueError(
                    f"offset {off} outside paragraph {para_idx} "
                    f"of length {len(texts[para_idx])}"
                )
        author = author or os.environ.get("SIDENOTE_AUTHOR") or getpass.getuser()
        date = date or datetime.now().isoformat(timespec="seconds")
        ranged = end > start
        name = self._unique_name() if ranged else None

        ann = Annotation()
        if name:
            ann.setAttrNS(OFFICENS, "name", name)
        ann.appendChild(dc.Creator(text=author))
        ann.appendChild(dc.Date(text=date))
        for line in text.split("\n") or [""]:
            ann.appendChild(P(text=line))

        end_node = None
        if ranged:
            # insert the end marker first so the start insertion cannot
            # invalidate its offset (annotations have zero text width)
            end_node = AnnotationEnd(name=name)
            _insert_at(paras[end[0]], end[1], end_node)
        _insert_at(paras[start[0]], start[1], ann)

        return Comment(
            name=name,
            author=author,
            date=date,
            text=text,
            start=start,
            end=end if ranged else start,
            node=ann,
            end_node=end_node,
        )

    def update_comment(self, comment: Comment, text: str) -> None:
        """Replace a comment's text, refreshing its date."""
        node = comment.node
        stale = [
            c
            for c in node.childNodes
            if c.nodeType == Node.ELEMENT_NODE
            and c.qname in (P_QNAME, (DCNS, "date"))
        ]
        for child in stale:
            node.removeChild(child)
        date = datetime.now().isoformat(timespec="seconds")
        node.appendChild(dc.Date(text=date))
        for line in text.split("\n") or [""]:
            node.appendChild(P(text=line))
        comment.text = text
        comment.date = date

    def delete_comment(self, comment: Comment) -> None:
        comment.node.parentNode.removeChild(comment.node)
        if comment.end_node is not None:
            comment.end_node.parentNode.removeChild(comment.end_node)

    def _unique_name(self) -> str:
        existing = {c.name for c in self.comments() if c.name}
        n = 1
        while f"cmt{n}" in existing:
            n += 1
        return f"cmt{n}"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        self.doc.save(str(target))
        return target

    def export_docx(self, out_dir: str | Path | None = None) -> Path:
        """Convert to docx with headless LibreOffice, return the new path."""
        out_dir = Path(out_dir) if out_dir else self.path.parent
        return _soffice_convert(self.path, "docx", out_dir)


MARKDOWN_SUFFIXES = frozenset(
    {".md", ".markdown", ".mdown", ".mkd", ".qmd", ".rmd"}
)


def open_review(path: str | Path):
    """Open a document with the engine that fits its format.

    Markdown goes to `MarkdownReview`, which keeps comments in a
    sidecar file, everything else to `OdtReview`. Both expose the same
    interface, so callers do not branch on format.
    """
    path = Path(path)
    if path.suffix.lower() in MARKDOWN_SUFFIXES:
        from sidenote.markdown import MarkdownReview

        return MarkdownReview(path)
    return OdtReview(path)
