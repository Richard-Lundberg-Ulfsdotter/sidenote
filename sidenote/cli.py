"""Command line interface for sidenote.

sidenote FILE                 open the TUI (.odt, .docx or .md)
sidenote FILE --refcheck F    read reference rows from a given file
sidenote list FILE            list comments
sidenote check FILE           report orphaned comments, nonzero on any
sidenote changes FILE         list tracked changes
sidenote add FILE ...         add a comment non-interactively
sidenote export FILE          convert to docx via headless LibreOffice
sidenote sample FILE          write a small sample document for testing

A .docx argument is converted to a sibling .odt working copy first. An
existing working copy is reused when it is newer than the docx.

Markdown is reviewed in place. Comments go to a sidecar JSON file
beside the document and the markdown itself is never modified, so it
stays a plain build input. Because the document can be edited outside
sidenote, `check` is the hook-friendly way to catch a comment whose
anchor text has been rewritten away.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sidenote.engine import MARKDOWN_SUFFIXES, docx_to_odt, open_review

SUBCOMMANDS = ("view", "list", "check", "add", "changes", "export", "sample")


SAMPLE_PARAGRAPHS = (
    "Maternal folate intake during pregnancy has been linked to "
    "offspring neurodevelopment in several cohort studies. The "
    "evidence for dose-response relations remains inconsistent.",
    "We used data from the NorthPop birth cohort to examine "
    "self-reported folate intake in gestational week 20 and "
    "language development at 18 months.",
    "Intake was categorised in tertiles. Models were adjusted "
    "for maternal age, education, pre-pregnancy BMI, and "
    "smoking during pregnancy.",
    "The middle tertile showed no association. The highest "
    "tertile showed a weak positive association that did not "
    "survive adjustment for education.",
)


def make_markdown_sample(path: Path) -> None:
    body = "\n\n".join(("# Sample manuscript", *SAMPLE_PARAGRAPHS))
    path.write_text(body + "\n", encoding="utf-8")


def make_sample(path: Path) -> None:
    """Write a sample document. Format follows the file suffix."""
    if path.suffix.lower() in MARKDOWN_SUFFIXES:
        make_markdown_sample(path)
        return

    from odf.opendocument import OpenDocumentText
    from odf.style import Style, TextProperties
    from odf.text import H, P

    doc = OpenDocumentText()
    bold = Style(name="Bold", family="text")
    bold.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(bold)

    doc.text.addElement(H(outlinelevel=1, text="Sample manuscript"))
    for para in SAMPLE_PARAGRAPHS:
        doc.text.addElement(P(text=para))
    doc.save(str(path))


def cmd_list(review) -> None:
    texts = review.para_texts()
    comments = review.comments()
    if not comments:
        print("no comments")
        return
    for i, c in enumerate(comments, 1):
        sp, so = c.start
        ep, eo = c.end
        if c.end > c.start:
            anchor = (
                texts[sp][so:eo] if sp == ep else texts[sp][so:] + " [...]"
            )
            where = f"para {sp} [{so}-{eo}]" if sp == ep else f"para {sp}:{so} - para {ep}:{eo}"
        else:
            anchor = ""
            where = f"para {sp} @{so}"
        flag = " ORPHANED" if c.orphan else ""
        print(f"[{i}] {c.author} {c.date} ({where}){flag}")
        if anchor:
            print(f"    anchor: {anchor!r}")
        print(f"    {c.text}")


def cmd_check(review) -> int:
    """Report anchor health. Nonzero exit when a comment is orphaned."""
    status = getattr(review, "status", None)
    if status is None:
        print(f"{review.path.name}: anchors are stored in the document itself")
        return 0
    result = status()
    print(f"{review.path.name}: {result.summary()}")
    if not result.orphaned:
        return 0
    for i, c in enumerate(review.comments(), 1):
        if c.orphan:
            print(f"  [{i}] {c.author}: {c.text}")
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # bare file argument opens the TUI
    if argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "view")

    parser = argparse.ArgumentParser(prog="sidenote", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_view = sub.add_parser("view", help="open the TUI")
    p_view.add_argument("file", type=Path)
    p_view.add_argument("--author", help="comment author name")
    p_view.add_argument(
        "--refcheck",
        type=Path,
        help="reference-check.md to read for the r key "
        "(default: found beside the document)",
    )

    p_list = sub.add_parser("list", help="list comments")
    p_list.add_argument("file", type=Path)

    p_check = sub.add_parser("check", help="report orphaned comments")
    p_check.add_argument("file", type=Path)

    p_add = sub.add_parser("add", help="add a comment")
    p_add.add_argument("file", type=Path)
    p_add.add_argument("--para", type=int, required=True)
    p_add.add_argument("--start", type=int, required=True)
    p_add.add_argument("--end", type=int, help="end offset, exclusive (defaults to a point comment)")
    p_add.add_argument("--end-para", type=int, help="end paragraph if different from --para")
    p_add.add_argument("--text", required=True)
    p_add.add_argument("--author")

    p_changes = sub.add_parser("changes", help="list tracked changes")
    p_changes.add_argument("file", type=Path)

    p_export = sub.add_parser("export", help="convert to docx")
    p_export.add_argument("file", type=Path)
    p_export.add_argument("--outdir", type=Path)

    p_sample = sub.add_parser("sample", help="write a sample document")
    p_sample.add_argument("file", type=Path)

    args = parser.parse_args(argv)

    if args.command == "sample":
        make_sample(args.file)
        print(f"wrote {args.file}")
        return 0

    if not args.file.exists():
        print(f"file not found: {args.file}", file=sys.stderr)
        return 1

    if args.file.suffix.lower() == ".docx":
        odt = docx_to_odt(args.file)
        print(f"working copy {odt}")
        args.file = odt

    if args.command == "view":
        from sidenote.tui import ReviewApp

        ReviewApp(
            args.file, author=args.author, refcheck=args.refcheck
        ).run()
        return 0

    try:
        review = open_review(args.file)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.command == "list":
        cmd_list(review)
    elif args.command == "check":
        return cmd_check(review)
    elif args.command == "changes":
        changes = review.changes()
        if not changes:
            print("no tracked changes")
        for i, ch in enumerate(changes, 1):
            sign = "+" if ch.kind == "insertion" else "-"
            sp, so = ch.start
            print(f"[{i}] {sign} {ch.author} {ch.date} (para {sp} @{so})")
            print(f"    {ch.text}")
    elif args.command == "add":
        end_para = args.end_para if args.end_para is not None else args.para
        end_off = args.end if args.end is not None else args.start
        c = review.add_comment(
            (args.para, args.start), (end_para, end_off), args.text, args.author
        )
        review.save()
        kind = "ranged" if c.end > c.start else "point"
        print(f"added {kind} comment by {c.author}")
    elif args.command == "export":
        try:
            target = review.export_docx(args.outdir)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
