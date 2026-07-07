"""Engine tests. Round-trip comments through a real ODT file on disk."""

import shutil
import zipfile

import pytest
from sidenote.cli import make_sample
from sidenote.engine import OdtReview, docx_to_odt

needs_soffice = pytest.mark.skipif(
    shutil.which("soffice") is None, reason="LibreOffice not installed"
)


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "sample.odt"
    make_sample(path)
    return path


def test_para_texts(sample):
    review = OdtReview(sample)
    texts = review.para_texts()
    assert len(texts) == 5
    assert texts[0] == "Sample manuscript"
    assert texts[1].startswith("Maternal folate intake")


def test_ranged_comment_round_trip(sample):
    review = OdtReview(sample)
    texts = review.para_texts()
    # comment on "folate intake" inside paragraph 1, mid-word boundaries
    start = texts[1].index("folate")
    end = start + len("folate intake")
    review.add_comment((1, start), (1, end), "Check the FFQ item wording", author="Richard")
    review.save()

    reloaded = OdtReview(sample)
    # text must be unchanged by the insertion
    assert reloaded.para_texts() == texts
    comments = reloaded.comments()
    assert len(comments) == 1
    c = comments[0]
    assert c.author == "Richard"
    assert c.text == "Check the FFQ item wording"
    assert c.start == (1, start)
    assert c.end == (1, end)
    anchor = reloaded.para_texts()[1][c.start[1]:c.end[1]]
    assert anchor == "folate intake"


def test_mid_word_offsets(sample):
    review = OdtReview(sample)
    texts = review.para_texts()
    # deliberately split inside words, char-level anchoring
    review.add_comment((2, 3), (2, 17), "mid-word range", author="R")
    review.save()
    reloaded = OdtReview(sample)
    assert reloaded.para_texts() == texts
    c = reloaded.comments()[0]
    assert (c.start, c.end) == ((2, 3), (2, 17))


def test_cross_paragraph_comment(sample):
    review = OdtReview(sample)
    texts = review.para_texts()
    review.add_comment((1, 10), (2, 5), "spans two paragraphs", author="R")
    review.save()
    reloaded = OdtReview(sample)
    assert reloaded.para_texts() == texts
    c = reloaded.comments()[0]
    assert c.start == (1, 10)
    assert c.end == (2, 5)


def test_point_comment(sample):
    review = OdtReview(sample)
    review.add_comment((3, 0), (3, 0), "general note", author="R")
    review.save()
    reloaded = OdtReview(sample)
    c = reloaded.comments()[0]
    assert c.start == c.end == (3, 0)
    assert c.name is None


def test_multiple_comments_and_delete(sample):
    review = OdtReview(sample)
    texts = review.para_texts()
    review.add_comment((1, 0), (1, 8), "first", author="R")
    review.add_comment((2, 5), (2, 9), "second", author="R")
    review.save()

    reloaded = OdtReview(sample)
    comments = reloaded.comments()
    assert [c.text for c in comments] == ["first", "second"]
    assert len({c.name for c in comments}) == 2

    reloaded.delete_comment(comments[0])
    reloaded.save()
    final = OdtReview(sample)
    assert [c.text for c in final.comments()] == ["second"]
    assert final.para_texts() == texts


def test_update_comment(sample):
    review = OdtReview(sample)
    review.add_comment((1, 9), (1, 22), "first draft", author="R")
    review.save()

    reloaded = OdtReview(sample)
    c = reloaded.comments()[0]
    reloaded.update_comment(c, "revised\nwith a second line")
    reloaded.save()

    final = OdtReview(sample).comments()[0]
    assert final.text == "revised\nwith a second line"
    assert final.author == "R"
    assert (final.start, final.end) == ((1, 9), (1, 22))


@needs_soffice
def test_docx_to_odt_round_trip(sample, tmp_path):
    review = OdtReview(sample)
    texts = review.para_texts()
    review.add_comment((1, 9), (1, 22), "survives round trip", author="R")
    review.save()
    docx = review.export_docx(tmp_path / "roundtrip")

    odt = docx_to_odt(docx)
    assert odt == docx.with_suffix(".odt")
    back = OdtReview(odt)
    assert back.para_texts() == texts
    comments = back.comments()
    assert len(comments) == 1
    assert comments[0].text == "survives round trip"
    assert (comments[0].start, comments[0].end) == ((1, 9), (1, 22))

    # a newer working copy is reused, not clobbered
    back.add_comment((2, 0), (2, 4), "second", author="R")
    back.save()
    assert docx_to_odt(docx) == odt
    assert len(OdtReview(odt).comments()) == 2


@needs_soffice
def test_docx_export_carries_comment(sample, tmp_path):
    review = OdtReview(sample)
    texts = review.para_texts()
    start = texts[1].index("cohort studies")
    review.add_comment(
        (1, start), (1, start + len("cohort studies")), "cite the reviews", author="R"
    )
    review.save()
    docx = OdtReview(sample).export_docx(tmp_path / "out")
    with zipfile.ZipFile(docx) as z:
        comments_xml = z.read("word/comments.xml").decode("utf-8")
        document_xml = z.read("word/document.xml").decode("utf-8")
    assert "cite the reviews" in comments_xml
    assert "commentRangeStart" in document_xml
    assert "commentRangeEnd" in document_xml
