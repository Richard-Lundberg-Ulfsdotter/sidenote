"""Reference-check lookup.

A manuscript in this workflow often carries a hand-maintained
`reference-check.md` beside it, one pipe-table row per citation, mapping
a citation key to the sentence it supports, a verbatim quote from the
cited full text, and a status. This module reads that file so the TUI
can show the row behind the citation under the cursor and open the
full-text PDF from there.

The parser identifies columns by their header names rather than by
position, so the table can carry extra columns and the sections can
differ between manuscripts. A file it cannot make sense of yields no
entries rather than an error, the feature is an aid and never blocks
reviewing.

The directory holding the full texts is read from the file itself when
it documents one (a line mentioning `<key>.pdf`), then from
`$SIDENOTE_REFERENCES`, then from `~/research/references`.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REFERENCE_CHECK_NAME = "reference-check.md"

# how far up from the document to look for the reference-check file
SEARCH_PARENTS = 2

DEFAULT_REFERENCES_DIR = Path("~/research/references")

# a citation key as pandoc writes it, optionally with its leading @
_KEY_RE = re.compile(r"@?([A-Za-z][\w.:#$%&+?<>~/-]*\d{4}[a-z]?)\b")

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")

# a pipe-table separator row, |:---|---:| and friends
_SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|?\s*$")

# the documented full-text location, e.g. `/home/me/references/<key>.pdf`
_PDF_DIR_RE = re.compile(r"([~/][^\s`'\"]*?)/<key>\.pdf")

# header cell substring -> field name
_COLUMNS = {
    "statement": "statement",
    "reference": "key",
    "quote": "quote",
    "status": "status",
}


@dataclass
class RefEntry:
    """One row of the reference-check table."""

    key: str
    section: str = ""
    statement: str = ""
    quote: str = ""
    status: str = ""


@dataclass
class ReferenceCheck:
    """The parsed file, indexed by citation key."""

    path: Path
    entries: dict[str, list[RefEntry]] = field(default_factory=dict)
    pdf_dir: Path | None = None

    def lookup(self, key: str) -> list[RefEntry]:
        """Every row citing this key, in file order."""
        return self.entries.get(key, [])

    def pdf_for(self, key: str) -> Path | None:
        """The full text for a key, or None when it is not on disk."""
        if self.pdf_dir is None:
            return None
        candidate = self.pdf_dir / f"{key}.pdf"
        return candidate if candidate.is_file() else None

    def expected_pdf(self, key: str) -> Path | None:
        """Where the full text would be, whether or not it exists."""
        return None if self.pdf_dir is None else self.pdf_dir / f"{key}.pdf"


def find_reference_check(doc_path: str | Path) -> Path | None:
    """Locate the reference-check file for a document.

    Looks beside the document first, then in its parent directories, so
    a manuscript in `manuscript/` finds a check file kept one level up.
    """
    env = os.environ.get("SIDENOTE_REFCHECK")
    if env:
        path = Path(env).expanduser()
        return path if path.is_file() else None
    folder = Path(doc_path).expanduser().resolve().parent
    for parent in (folder, *list(folder.parents)[:SEARCH_PARENTS]):
        candidate = parent / REFERENCE_CHECK_NAME
        if candidate.is_file():
            return candidate
    return None


def references_dir(source: str = "") -> Path | None:
    """Where the full-text PDFs live.

    The reference-check file documents its own corpus, so that wins.
    `$SIDENOTE_REFERENCES` overrides the fallback for anyone whose
    corpus sits elsewhere.
    """
    match = _PDF_DIR_RE.search(source)
    if match:
        folder = Path(match.group(1)).expanduser()
        if folder.is_dir():
            return folder
    env = os.environ.get("SIDENOTE_REFERENCES")
    if env:
        folder = Path(env).expanduser()
        return folder if folder.is_dir() else None
    folder = DEFAULT_REFERENCES_DIR.expanduser()
    return folder if folder.is_dir() else None


def load_reference_check(path: str | Path) -> ReferenceCheck:
    """Parse a reference-check file. Unparsable tables yield no entries."""
    path = Path(path)
    source = path.read_text(encoding="utf-8")
    return ReferenceCheck(
        path=path,
        entries=parse_entries(source),
        pdf_dir=references_dir(source),
    )


def parse_entries(source: str) -> dict[str, list[RefEntry]]:
    """Index every table row of the file by the citation keys it names.

    A row citing two keys is indexed under both. Rows outside a table
    with a recognisable reference column are ignored, which is what
    keeps the prose sections of the file out of the results.
    """
    entries: dict[str, list[RefEntry]] = {}
    section = ""
    columns: dict[str, int] | None = None
    for line in source.split("\n"):
        stripped = line.strip()
        heading = _HEADING_RE.match(stripped)
        if heading:
            section = heading.group(1)
            columns = None
            continue
        if not stripped.startswith("|"):
            columns = None
            continue
        if _SEPARATOR_RE.match(stripped):
            continue
        cells = _split_row(stripped)
        if columns is None:
            columns = _header_columns(cells)
            continue
        if "key" not in columns:
            continue
        row = {name: _cell(cells, i) for name, i in columns.items()}
        for key in _KEY_RE.findall(row.get("key", "")):
            entries.setdefault(key, []).append(
                RefEntry(
                    key=key,
                    section=section,
                    statement=row.get("statement", ""),
                    quote=row.get("quote", ""),
                    status=row.get("status", ""),
                )
            )
    return entries


def open_pdf(path: str | Path) -> None:
    """Hand a PDF to the desktop viewer, detached from the TUI.

    `$SIDENOTE_PDF_VIEWER` replaces `xdg-open` for anyone who wants a
    particular reader. Output goes nowhere, a viewer writing to the
    terminal would corrupt the display.
    """
    viewer = os.environ.get("SIDENOTE_PDF_VIEWER") or "xdg-open"
    subprocess.Popen(
        [viewer, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _header_columns(cells: list[str]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for i, cell in enumerate(cells):
        low = cell.lower()
        for needle, name in _COLUMNS.items():
            if needle in low and name not in columns:
                columns[name] = i
    return columns


def _cell(cells: list[str], index: int) -> str:
    return cells[index].strip("` ") if index < len(cells) else ""
