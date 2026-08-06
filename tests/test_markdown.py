"""Markdown engine and its sidecar comment file."""

import json

import pytest

from sidenote.engine import open_review
from sidenote.markdown import MarkdownReview, sidecar_path, split_blocks

SOURCE = """# Title

Maternal folate intake has been linked to offspring
neurodevelopment. The evidence for dose-response relations
remains inconsistent.

Intake was categorised in tertiles.

```r
x <- 1

y <- 2
```

The highest tertile showed a weak association.
"""


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "manuscript.md"
    path.write_text(SOURCE, encoding="utf-8")
    return path


def offset_of(review, para, needle):
    return review.para_texts()[para].index(needle)


# ----------------------------------------------------------------------
# Block splitting
# ----------------------------------------------------------------------


def test_blocks_split_on_blank_lines():
    assert split_blocks("one\n\ntwo\n\n\nthree") == ["one", "two", "three"]


def test_fenced_code_survives_its_blank_lines():
    blocks = split_blocks("intro\n\n```\na\n\nb\n```\n\nafter")
    assert blocks == ["intro", "```\na\n\nb\n```", "after"]


def test_hard_wrapped_paragraph_stays_one_block(doc):
    review = MarkdownReview(doc)
    assert "\n" in review.para_texts()[1]
    assert review.para_texts()[0] == "# Title"


# ----------------------------------------------------------------------
# Round trip
# ----------------------------------------------------------------------


def test_open_review_picks_the_markdown_engine(doc):
    assert isinstance(open_review(doc), MarkdownReview)


def test_markdown_file_is_never_modified(doc):
    before = doc.read_bytes()
    review = MarkdownReview(doc)
    off = offset_of(review, 1, "dose-response")
    review.add_comment((1, off), (1, off + 13), "define this")
    review.save()
    assert doc.read_bytes() == before


def test_comment_round_trips_through_the_sidecar(doc):
    review = MarkdownReview(doc)
    off = offset_of(review, 1, "dose-response")
    review.add_comment((1, off), (1, off + 13), "define this", author="Anna")
    review.save()

    reopened = MarkdownReview(doc)
    (comment,) = reopened.comments()
    assert comment.author == "Anna"
    assert comment.text == "define this"
    assert comment.start == (1, off)
    assert comment.end == (1, off + 13)
    assert not comment.orphan


def test_sidecar_sits_beside_the_document(doc):
    assert sidecar_path(doc).name == "manuscript.sidenote.json"
    review = MarkdownReview(doc)
    review.add_comment((0, 0), (0, 7), "title note")
    target = review.save()
    assert target == doc.parent / "manuscript.sidenote.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["file"] == "manuscript.md"
    assert payload["comments"][0]["quote"] == "# Title"


def test_point_comment_has_no_quote(doc):
    review = MarkdownReview(doc)
    review.add_comment((3, 0), (3, 0), "note on methods")
    review.save()
    (comment,) = MarkdownReview(doc).comments()
    assert comment.start == comment.end == (3, 0)


# ----------------------------------------------------------------------
# Re-anchoring
# ----------------------------------------------------------------------


def test_anchor_survives_paragraphs_inserted_above(doc):
    review = MarkdownReview(doc)
    off = offset_of(review, 1, "dose-response")
    review.add_comment((1, off), (1, off + 13), "define this")
    review.save()

    doc.write_text("# New\n\nAn added paragraph.\n\n" + SOURCE, encoding="utf-8")
    reopened = MarkdownReview(doc)
    (comment,) = reopened.comments()
    assert not comment.orphan
    assert comment.start[0] == 3
    texts = reopened.para_texts()
    assert texts[3][comment.start[1] : comment.end[1]] == "dose-response"


def test_anchor_survives_a_reworded_neighbour(doc):
    review = MarkdownReview(doc)
    off = offset_of(review, 1, "dose-response")
    review.add_comment((1, off), (1, off + 13), "define this")
    review.save()

    doc.write_text(
        SOURCE.replace(
            "Maternal folate intake has been linked to offspring\nneurodevelopment.",
            "Folate intake in pregnancy is associated with later outcomes.",
        ),
        encoding="utf-8",
    )
    reopened = MarkdownReview(doc)
    (comment,) = reopened.comments()
    assert not comment.orphan
    texts = reopened.para_texts()
    assert texts[comment.start[0]][comment.start[1] : comment.end[1]] == "dose-response"


def test_rewriting_the_anchored_text_orphans_the_comment(doc):
    review = MarkdownReview(doc)
    off = offset_of(review, 1, "dose-response")
    review.add_comment((1, off), (1, off + 13), "define this")
    review.save()

    doc.write_text(
        SOURCE.replace(
            "The evidence for dose-response relations\nremains inconsistent.",
            "Whether intake scales with effect is unsettled.",
        ),
        encoding="utf-8",
    )
    (comment,) = MarkdownReview(doc).comments()
    assert comment.orphan
    assert comment.text == "define this"


def test_comment_follows_a_replaced_phrase(doc):
    """Surrounding wording intact means the comment tracks the new text."""
    review = MarkdownReview(doc)
    off = offset_of(review, 1, "dose-response")
    review.add_comment((1, off), (1, off + 13), "define this")
    review.save()

    doc.write_text(SOURCE.replace("dose-response", "graded"), encoding="utf-8")
    reopened = MarkdownReview(doc)
    (comment,) = reopened.comments()
    assert not comment.orphan
    texts = reopened.para_texts()
    assert texts[comment.start[0]][comment.start[1] : comment.end[1]] == "graded"


def test_orphan_keeps_its_stored_anchor_across_a_save(doc):
    review = MarkdownReview(doc)
    off = offset_of(review, 1, "dose-response")
    review.add_comment((1, off), (1, off + 13), "define this")
    review.add_comment((3, 0), (3, 0), "second")
    review.save()

    doc.write_text(
        SOURCE.replace(
            "The evidence for dose-response relations\nremains inconsistent.",
            "Whether intake scales with effect is unsettled.",
        ),
        encoding="utf-8",
    )
    reopened = MarkdownReview(doc)
    assert [c.orphan for c in reopened.comments()] == [True, False]
    reopened.save()

    # the orphan keeps the wording it was written against, so it can
    # still be re-found if the text comes back
    payload = json.loads(sidecar_path(doc).read_text(encoding="utf-8"))
    stored = {c["name"]: c for c in payload["comments"]}
    # cmt2 because renumbering puts orphans after the live comments
    assert stored["cmt2"]["quote"] == "dose-response"
    assert stored["cmt2"]["start"]["after"].startswith("dose-response")


def test_save_recaptures_moved_anchors(doc):
    review = MarkdownReview(doc)
    off = offset_of(review, 1, "dose-response")
    review.add_comment((1, off), (1, off + 13), "define this")
    review.save()

    doc.write_text("# New\n\nAn added paragraph.\n\n" + SOURCE, encoding="utf-8")
    MarkdownReview(doc).save()

    payload = json.loads(sidecar_path(doc).read_text(encoding="utf-8"))
    assert payload["comments"][0]["start"]["para"] == 3


# ----------------------------------------------------------------------
# Naming
# ----------------------------------------------------------------------


def test_names_run_top_to_bottom_whatever_the_order_written(doc):
    """cmtN is the Nth comment down, which is what the sidebar shows."""
    review = MarkdownReview(doc)
    review.add_comment((4, 0), (4, 0), "last")
    review.add_comment((2, 0), (2, 0), "middle")
    review.add_comment((1, 0), (1, 0), "first")
    review.save()

    payload = json.loads(sidecar_path(doc).read_text(encoding="utf-8"))
    assert [c["name"] for c in payload["comments"]] == ["cmt1", "cmt2", "cmt3"]
    assert [c["text"] for c in payload["comments"]] == ["first", "middle", "last"]
    assert [c.name for c in MarkdownReview(doc).comments()] == [
        "cmt1",
        "cmt2",
        "cmt3",
    ]


def test_deleting_a_comment_closes_the_gap_in_the_names(doc):
    review = MarkdownReview(doc)
    for para, text in ((1, "first"), (2, "middle"), (4, "last")):
        review.add_comment((para, 0), (para, 0), text)
    review.save()

    reopened = MarkdownReview(doc)
    middle = next(c for c in reopened.comments() if c.text == "middle")
    reopened.delete_comment(middle)
    reopened.save()

    payload = json.loads(sidecar_path(doc).read_text(encoding="utf-8"))
    assert [(c["name"], c["text"]) for c in payload["comments"]] == [
        ("cmt1", "first"),
        ("cmt2", "last"),
    ]


# ----------------------------------------------------------------------
# Status and edits made elsewhere
# ----------------------------------------------------------------------


def test_status_reports_edits_made_elsewhere(doc):
    review = MarkdownReview(doc)
    review.add_comment((0, 0), (0, 7), "title note")
    review.save()
    assert not MarkdownReview(doc).status().edited_elsewhere

    doc.write_text(SOURCE + "\nA new closing paragraph.\n", encoding="utf-8")
    status = MarkdownReview(doc).status()
    assert status.edited_elsewhere
    assert status.total == 1
    assert status.orphaned == 0


def test_status_counts_orphans(doc):
    review = MarkdownReview(doc)
    off = offset_of(review, 1, "dose-response")
    review.add_comment((1, off), (1, off + 13), "define this")
    review.save()
    doc.write_text(
        SOURCE.replace(
            "The evidence for dose-response relations\nremains inconsistent.",
            "Whether intake scales with effect is unsettled.",
        ),
        encoding="utf-8",
    )
    status = MarkdownReview(doc).status()
    assert status.total == 1
    assert status.orphaned == 1
    assert status.anchored == 0
    assert "orphaned" in status.summary()


def test_no_sidecar_means_no_comments(doc):
    review = MarkdownReview(doc)
    assert review.comments() == []
    assert not review.status().has_sidecar


# ----------------------------------------------------------------------
# Editing and deleting
# ----------------------------------------------------------------------


def test_update_and_delete(doc):
    review = MarkdownReview(doc)
    review.add_comment((0, 0), (0, 7), "first")
    review.add_comment((3, 0), (3, 0), "second")
    review.save()

    reopened = MarkdownReview(doc)
    first, second = reopened.comments()
    reopened.update_comment(first, "revised")
    reopened.delete_comment(second)
    reopened.save()

    (comment,) = MarkdownReview(doc).comments()
    assert comment.text == "revised"


def test_deleting_the_last_comment_leaves_an_empty_sidecar(doc):
    review = MarkdownReview(doc)
    review.add_comment((0, 0), (0, 7), "only")
    review.save()
    review.delete_comment(review.comments()[0])
    review.save()
    assert MarkdownReview(doc).comments() == []
    assert sidecar_path(doc).exists()


def test_offsets_outside_the_paragraph_are_rejected(doc):
    review = MarkdownReview(doc)
    with pytest.raises(ValueError):
        review.add_comment((0, 0), (0, 999), "too long")
    with pytest.raises(ValueError):
        review.add_comment((99, 0), (99, 1), "no such paragraph")


def test_no_tracked_changes_and_no_docx_export(doc):
    review = MarkdownReview(doc)
    assert review.changes() == []
    with pytest.raises(RuntimeError):
        review.export_docx()


def test_unreadable_sidecar_is_reported(doc):
    sidecar_path(doc).write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot read"):
        MarkdownReview(doc)
