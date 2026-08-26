"""Docx engine tests.

Round-trip comments through real zip files on disk and assert that
everything the engine did not set out to change came through byte for
byte. That last part is the point of the engine, so most fixtures here
carry parts it must not touch.
"""

import shutil
import zipfile
from xml.etree import ElementTree

import pytest
from sidenote.cli import make_docx_sample
from sidenote.docx import DocxReview
from sidenote.engine import open_review

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types"><Default Extension="rels" ContentType="rels"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="doc"/></Types>'
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{PKG_NS}"><Relationship Id="rId1" Type="t" '
    'Target="header1.xml"/></Relationships>'
)


def build_docx(path, body, extra=None):
    """Write a docx whose body is the given XML, plus any extra parts."""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/'
        'wordprocessingDrawing" '
        'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", TYPES)
        z.writestr("_rels/.rels", "<Relationships/>")
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", RELS)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


def run(text, props=""):
    return f'<w:r>{props}<w:t xml:space="preserve">{text}</w:t></w:r>'


def para(*runs):
    return "<w:p>" + "".join(runs) + "</w:p>"


def parts(path):
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def assert_well_formed(path):
    for name, data in parts(path).items():
        if name.endswith(".xml") or name.endswith(".rels"):
            ElementTree.fromstring(data)


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "sample.docx"
    make_docx_sample(path)
    return path


def test_open_review_picks_the_docx_engine(sample):
    assert isinstance(open_review(sample), DocxReview)


def test_para_texts(sample):
    texts = DocxReview(sample).para_texts()
    assert len(texts) == 5
    assert texts[0] == "Sample manuscript"
    assert texts[1].startswith("Maternal folate intake")


def test_ranged_comment_round_trip(sample):
    review = DocxReview(sample)
    texts = review.para_texts()
    start = texts[1].index("folate")
    end = start + len("folate intake")
    review.add_comment(
        (1, start), (1, end), "Check the FFQ item wording", author="Richard"
    )
    review.save()

    reloaded = DocxReview(sample)
    assert reloaded.para_texts() == texts
    comments = reloaded.comments()
    assert len(comments) == 1
    c = comments[0]
    assert c.author == "Richard"
    assert c.text == "Check the FFQ item wording"
    assert (c.start, c.end) == ((1, start), (1, end))
    assert texts[1][c.start[1] : c.end[1]] == "folate intake"
    assert_well_formed(sample)


def test_mid_word_offsets_split_a_run(sample):
    review = DocxReview(sample)
    texts = review.para_texts()
    # inside the bold run of the sample, so the run must be cut twice
    review.add_comment((1, 10), (1, 15), "mid-word range", author="R")
    review.save()
    reloaded = DocxReview(sample)
    assert reloaded.para_texts() == texts
    c = reloaded.comments()[0]
    assert (c.start, c.end) == ((1, 10), (1, 15))
    # cut twice, so three sub-runs, each carrying the run properties
    assert parts(sample)["word/document.xml"].count(b"<w:b/>") == 3


def test_two_comments_in_one_run(sample):
    review = DocxReview(sample)
    texts = review.para_texts()
    review.add_comment((1, 10), (1, 15), "first", author="R")
    review.add_comment((1, 17), (1, 20), "second", author="R")
    review.save()
    reloaded = DocxReview(sample)
    assert reloaded.para_texts() == texts
    assert [(c.start, c.end) for c in reloaded.comments()] == [
        ((1, 10), (1, 15)),
        ((1, 17), (1, 20)),
    ]


def test_adjacent_ranges_keep_their_own_span(sample):
    review = DocxReview(sample)
    texts = review.para_texts()
    review.add_comment((3, 0), (3, 6), "first", author="R")
    review.add_comment((3, 6), (3, 10), "second", author="R")
    review.save()
    reloaded = DocxReview(sample)
    assert reloaded.para_texts() == texts
    spans = [(c.start, c.end) for c in reloaded.comments()]
    assert spans == [((3, 0), (3, 6)), ((3, 6), (3, 10))]


def test_cross_paragraph_comment(sample):
    review = DocxReview(sample)
    texts = review.para_texts()
    review.add_comment((1, 10), (2, 5), "spans two paragraphs", author="R")
    review.save()
    reloaded = DocxReview(sample)
    assert reloaded.para_texts() == texts
    c = reloaded.comments()[0]
    assert (c.start, c.end) == ((1, 10), (2, 5))


def test_point_comment(sample):
    review = DocxReview(sample)
    review.add_comment((3, 0), (3, 0), "general note", author="R")
    review.save()
    reloaded = DocxReview(sample)
    c = reloaded.comments()[0]
    assert c.start == c.end == (3, 0)
    document = parts(sample)["word/document.xml"]
    assert b"commentRangeStart" not in document


def test_multiple_comments_and_delete(sample):
    review = DocxReview(sample)
    texts = review.para_texts()
    review.add_comment((1, 0), (1, 8), "first", author="R")
    review.add_comment((2, 5), (2, 9), "second", author="R")
    review.save()

    reloaded = DocxReview(sample)
    comments = reloaded.comments()
    assert [c.text for c in comments] == ["first", "second"]
    assert len({c.name for c in comments}) == 2

    reloaded.delete_comment(comments[0])
    reloaded.save()
    final = DocxReview(sample)
    assert [c.text for c in final.comments()] == ["second"]
    assert final.para_texts() == texts
    document = parts(sample)["word/document.xml"]
    assert document.count(b"<w:commentRangeStart") == 1
    assert document.count(b"<w:commentReference") == 1


def test_delete_leaves_the_split_run_split(tmp_path):
    """Removing a comment removes its markers, not the run boundary.

    Re-joining runs would mean deciding two runs are interchangeable,
    which is Word's call to make on its own save, not this engine's.
    The text and the run properties are what have to come back.
    """
    body = para(run("Hello world", "<w:rPr><w:b/></w:rPr>"))
    path = build_docx(tmp_path / "split.docx", body)

    review = DocxReview(path)
    review.add_comment((0, 6), (0, 11), "note", author="R")
    review.save()
    review = DocxReview(path)
    review.delete_comment(review.comments()[0])
    review.save()

    document = parts(path)["word/document.xml"]
    assert b"comment" not in document
    assert DocxReview(path).para_texts() == ["Hello world"]
    assert DocxReview(path).comments() == []
    assert document.count(b"<w:b/>") == 2


def test_update_comment(sample):
    review = DocxReview(sample)
    review.add_comment((1, 9), (1, 22), "first draft", author="R")
    review.save()

    reloaded = DocxReview(sample)
    reloaded.update_comment(reloaded.comments()[0], "revised\nwith a second line")
    reloaded.save()

    final = DocxReview(sample).comments()[0]
    assert final.text == "revised\nwith a second line"
    assert final.author == "R"
    assert (final.start, final.end) == ((1, 9), (1, 22))


def test_only_the_two_parts_change(tmp_path):
    """Every part the engine has no business in comes through untouched."""
    extra = {
        "word/header1.xml": "<hdr/>",
        "word/footnotes.xml": "<notes/>",
        "word/settings.xml": "<settings/>",
        "word/media/image1.png": b"\x89PNG\r\n\x1a\nnot really",
        "docProps/app.xml": "<Properties/>",
    }
    path = build_docx(tmp_path / "rich.docx", para(run("Hello world")), extra)
    before = parts(path)

    review = DocxReview(path)
    review.add_comment((0, 0), (0, 5), "note", author="R")
    review.save()

    after = parts(path)
    assert set(after) == set(before) | {"word/comments.xml"}
    for name, data in before.items():
        if name in ("word/document.xml", "[Content_Types].xml"):
            continue
        if name == "word/_rels/document.xml.rels":
            continue
        assert after[name] == data, name


def test_comments_part_is_created_and_registered(tmp_path):
    path = build_docx(tmp_path / "fresh.docx", para(run("Hello world")))
    assert "word/comments.xml" not in parts(path)

    review = DocxReview(path)
    review.add_comment((0, 6), (0, 11), "note", author="R")
    review.save()

    after = parts(path)
    assert b"note" in after["word/comments.xml"]
    assert b"/word/comments.xml" in after["[Content_Types].xml"]
    rels = after["word/_rels/document.xml.rels"]
    assert b'Target="comments.xml"' in rels
    # the existing relationship keeps its id and a fresh one is minted
    assert b'Id="rId1"' in rels and b'Id="rId2"' in rels
    assert_well_formed(path)
    assert DocxReview(path).comments()[0].text == "note"


def test_empty_self_closing_comments_root(tmp_path):
    """pandoc ships `<w:comments/>`, which has no closing tag to append to."""
    empty = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
        f'<w:comments xmlns:w="{W_NS}" xmlns:m="urn:math"/>'
    )
    path = build_docx(
        tmp_path / "pandoc.docx",
        para(run("Hello world")),
        {"word/comments.xml": empty},
    )
    review = DocxReview(path)
    assert review.comments() == []
    review.add_comment((0, 6), (0, 11), "note", author="R")
    review.save()

    after = parts(path)["word/comments.xml"]
    # the document's own namespace declarations are what a comment body
    # resolves against, so they have to survive
    assert b'xmlns:m="urn:math"' in after
    assert_well_formed(path)
    reloaded = DocxReview(path)
    assert reloaded.para_texts() == ["Hello world"]
    c = reloaded.comments()[0]
    assert (c.text, c.start, c.end) == ("note", (0, 6), (0, 11))


def test_an_unappendable_comments_part_leaves_the_body_alone(tmp_path):
    """The body is only touched once the comment is known to have a home."""
    path = build_docx(
        tmp_path / "broken.docx",
        para(run("Hello world")),
        {"word/comments.xml": "<w:comments-but-not-really/>"},
    )
    review = DocxReview(path)
    body = review.doc
    with pytest.raises(RuntimeError, match="</w:comments>"):
        review.add_comment((0, 6), (0, 11), "note", author="R")
    assert review.doc == body


def test_existing_comments_keep_their_ids(tmp_path):
    comments = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:comments xmlns:w="{W_NS}">'
        '<w:comment w:id="4" w:author="Sophia" w:date="2026-08-11T12:37:00Z">'
        '<w:p><w:r><w:t>theirs</w:t></w:r></w:p></w:comment></w:comments>'
    )
    body = (
        "<w:p>"
        '<w:commentRangeStart w:id="4"/>'
        + run("Hello")
        + '<w:commentRangeEnd w:id="4"/>'
        '<w:r><w:commentReference w:id="4"/></w:r>'
        + run(" world")
        + "</w:p>"
    )
    path = build_docx(
        tmp_path / "shared.docx", body, {"word/comments.xml": comments}
    )

    review = DocxReview(path)
    theirs = review.comments()
    assert len(theirs) == 1
    assert theirs[0].author == "Sophia"
    assert (theirs[0].start, theirs[0].end) == ((0, 0), (0, 5))

    review.add_comment((0, 6), (0, 11), "mine", author="R")
    review.save()

    final = DocxReview(path).comments()
    assert [(c.name, c.text) for c in final] == [("4", "theirs"), ("5", "mine")]
    assert DocxReview(path).para_texts() == ["Hello world"]


def test_drawing_run_survives_a_split(tmp_path):
    drawing = (
        "<w:r><w:drawing><wp:inline><wp:docPr id=\"1\" name=\"Picture 1\"/>"
        "</wp:inline></w:drawing></w:r>"
    )
    body = para(run("before"), drawing, run("after"))
    path = build_docx(tmp_path / "picture.docx", body)

    review = DocxReview(path)
    assert review.para_texts() == ["beforeafter"]
    review.add_comment((0, 3), (0, 8), "across the picture", author="R")
    review.save()

    document = parts(path)["word/document.xml"]
    assert document.count(b"<w:drawing>") == 1
    assert b'name="Picture 1"' in document
    reloaded = DocxReview(path)
    assert reloaded.para_texts() == ["beforeafter"]
    assert (reloaded.comments()[0].start, reloaded.comments()[0].end) == (
        (0, 3),
        (0, 8),
    )


def test_text_box_is_not_document_text(tmp_path):
    """A paragraph inside a drawing belongs to the drawing, not the body."""
    body = para(
        run("Body text"),
        "<w:r><w:drawing><wp:inline><w:txbxContent>"
        + para(run("caption"))
        + "</w:txbxContent></wp:inline></w:drawing></w:r>",
    )
    path = build_docx(tmp_path / "textbox.docx", body)
    assert DocxReview(path).para_texts() == ["Body text"]


def test_table_cells_are_paragraphs(tmp_path):
    body = (
        para(run("Before the table"))
        + '<w:tbl><w:tblPr><w:tblStyle w:val="Grid"/></w:tblPr>'
        '<w:tblGrid><w:gridCol w:w="100"/></w:tblGrid><w:tr>'
        '<w:tc><w:tcPr><w:tcW w:w="100"/></w:tcPr>'
        + para(run("Cell one"))
        + "</w:tc><w:tc>"
        + para(run("Cell two"))
        + "</w:tc></w:tr></w:tbl>"
        + para(run("After"))
    )
    path = build_docx(tmp_path / "table.docx", body)
    review = DocxReview(path)
    texts = review.para_texts()
    assert texts == ["Before the table", "Cell one", "Cell two", "After"]
    review.add_comment((1, 5), (1, 8), "add the unit", author="R")
    review.save()
    reloaded = DocxReview(path)
    assert reloaded.para_texts() == texts
    assert (reloaded.comments()[0].start, reloaded.comments()[0].end) == (
        (1, 5),
        (1, 8),
    )


def test_comment_inside_a_hyperlink(tmp_path):
    body = para(
        run("See "),
        '<w:hyperlink r:id="rId5">' + run("the register page") + "</w:hyperlink>",
        run(" for details"),
    )
    path = build_docx(tmp_path / "link.docx", body)
    review = DocxReview(path)
    assert review.para_texts() == ["See the register page for details"]
    review.add_comment((0, 8), (0, 16), "wrong author field", author="R")
    review.save()
    document = parts(path)["word/document.xml"]
    # the markers stay inside the hyperlink, which keeps its target
    assert b'<w:hyperlink r:id="rId5">' in document
    assert document.index(b"commentRangeStart") > document.index(b"<w:hyperlink")
    assert document.index(b"commentRangeEnd") < document.index(b"</w:hyperlink>")
    reloaded = DocxReview(path)
    assert reloaded.para_texts() == ["See the register page for details"]
    c = reloaded.comments()[0]
    assert (c.start, c.end) == ((0, 8), (0, 16))


def test_tabs_and_breaks_are_characters(tmp_path):
    body = (
        "<w:p><w:r>"
        '<w:t xml:space="preserve">a</w:t><w:tab/>'
        '<w:t xml:space="preserve">b</w:t><w:br/>'
        '<w:t xml:space="preserve">c</w:t>'
        "</w:r></w:p>"
    )
    path = build_docx(tmp_path / "tabs.docx", body)
    review = DocxReview(path)
    assert review.para_texts() == ["a\tb\nc"]
    review.add_comment((0, 2), (0, 3), "the b", author="R")
    review.save()
    reloaded = DocxReview(path)
    assert reloaded.para_texts() == ["a\tb\nc"]
    c = reloaded.comments()[0]
    assert (c.start, c.end) == ((0, 2), (0, 3))
    assert parts(path)["word/document.xml"].count(b"<w:tab/>") == 1


def test_field_instructions_are_not_text(tmp_path):
    body = (
        "<w:p>"
        + run("See ")
        + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> REF _Ref1 </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        + run("Table 2")
        + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        + "</w:p>"
    )
    path = build_docx(tmp_path / "field.docx", body)
    review = DocxReview(path)
    assert review.para_texts() == ["See Table 2"]
    review.add_comment((0, 4), (0, 11), "add the unit", author="R")
    review.save()
    document = parts(path)["word/document.xml"]
    assert b"REF _Ref1" in document
    assert DocxReview(path).para_texts() == ["See Table 2"]


def test_escaped_text_survives(tmp_path):
    body = para(run("Fisher&apos;s exact test, p &lt; 0.05 &amp; n &gt; 30"))
    path = build_docx(tmp_path / "escapes.docx", body)
    review = DocxReview(path)
    plain = "Fisher's exact test, p < 0.05 & n > 30"
    assert review.para_texts() == [plain]
    # a span whose own text needs re-escaping when the run is split
    start = plain.index("<")
    review.add_comment((0, start), (0, start + 8), "state the test", author="R")
    review.save()
    reloaded = DocxReview(path)
    assert reloaded.para_texts() == [plain]
    c = reloaded.comments()[0]
    assert plain[c.start[1] : c.end[1]] == "< 0.05 &"


def test_comment_text_is_escaped(sample):
    review = DocxReview(sample)
    review.add_comment((0, 0), (0, 6), 'p < 0.05 & "yes" > no', author="R & Co")
    review.save()
    assert_well_formed(sample)
    c = DocxReview(sample).comments()[0]
    assert c.text == 'p < 0.05 & "yes" > no'
    assert c.author == "R & Co"


def test_tracked_changes(tmp_path):
    body = (
        "<w:p>"
        + run("Hello ")
        + '<w:ins w:id="1" w:author="Anna" w:date="2026-06-16T08:31:00Z">'
        + run("new words")
        + "</w:ins>"
        + run(" world")
        + "</w:p><w:p>"
        + run("Start ")
        + '<w:del w:id="2" w:author="Magnus" w:date="2026-06-17T09:00:00Z">'
        '<w:r><w:delText xml:space="preserve">old text</w:delText></w:r>'
        "</w:del>" + run("finish") + "</w:p>"
    )
    path = build_docx(tmp_path / "tracked.docx", body)
    review = DocxReview(path)
    assert review.para_texts() == ["Hello new words world", "Start finish"]

    changes = review.changes()
    assert len(changes) == 2
    ins, dele = changes
    assert ins.kind == "insertion"
    assert ins.author == "Anna"
    assert ins.text == "new words"
    assert (ins.start, ins.end) == ((0, 6), (0, 15))
    assert dele.kind == "deletion"
    assert dele.author == "Magnus"
    assert dele.text == "old text"
    assert dele.start == dele.end == (1, 6)


def test_changes_do_not_disturb_comments(tmp_path):
    body = (
        "<w:p>"
        + run("Hello ")
        + '<w:ins w:id="1" w:author="Anna" w:date="2026-06-16T08:31:00Z">'
        + run("new words")
        + "</w:ins>"
        + run(" world")
        + "</w:p>"
    )
    path = build_docx(tmp_path / "tracked.docx", body)
    review = DocxReview(path)
    review.add_comment((0, 6), (0, 15), "note on inserted text", author="R")
    review.save()
    reloaded = DocxReview(path)
    c = reloaded.comments()[0]
    assert (c.start, c.end) == ((0, 6), (0, 15))
    ins = reloaded.changes()[0]
    assert (ins.start, ins.end) == ((0, 6), (0, 15))
    assert reloaded.para_texts()[0] == "Hello new words world"


def test_comment_without_markers_is_an_orphan(tmp_path):
    comments = (
        f'<w:comments xmlns:w="{W_NS}">'
        '<w:comment w:id="1" w:author="Ghost" w:date="2026-01-01T00:00:00Z">'
        "<w:p><w:r><w:t>lost</w:t></w:r></w:p></w:comment></w:comments>"
    )
    path = build_docx(
        tmp_path / "ghost.docx",
        para(run("Hello")),
        {"word/comments.xml": comments},
    )
    c = DocxReview(path).comments()[0]
    assert c.orphan
    assert c.text == "lost"
    # still saved, so it can be read or deleted rather than silently lost
    review = DocxReview(path)
    review.save()
    assert DocxReview(path).comments()[0].orphan


def test_delete_prunes_the_word_side_tables(tmp_path):
    comments = (
        f'<w:comments xmlns:w="{W_NS}" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
        '<w:comment w:id="1" w:author="Sophia" w:date="2026-01-01T00:00:00Z">'
        '<w:p w14:paraId="0A0B0C0D"><w:r><w:t>theirs</w:t></w:r></w:p>'
        "</w:comment></w:comments>"
    )
    extended = (
        '<w15:commentsEx xmlns:w15="http://schemas.microsoft.com/office/word/'
        '2012/wordml"><w15:commentEx w15:paraId="0A0B0C0D" w15:done="0"/>'
        '<w15:commentEx w15:paraId="11111111" w15:done="0"/></w15:commentsEx>'
    )
    body = (
        "<w:p>"
        '<w:commentRangeStart w:id="1"/>'
        + run("Hello")
        + '<w:commentRangeEnd w:id="1"/>'
        '<w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    path = build_docx(
        tmp_path / "threaded.docx",
        body,
        {
            "word/comments.xml": comments,
            "word/commentsExtended.xml": extended,
        },
    )
    review = DocxReview(path)
    review.delete_comment(review.comments()[0])
    review.save()

    after = parts(path)["word/commentsExtended.xml"]
    assert b"0A0B0C0D" not in after
    assert b"11111111" in after
    assert DocxReview(path).comments() == []
    assert DocxReview(path).para_texts() == ["Hello"]


def test_save_to_another_path(sample, tmp_path):
    """save(path) writes a copy and leaves the opened file alone."""
    review = DocxReview(sample)
    review.add_comment((1, 9), (1, 22), "carried over", author="R")
    target = review.save(tmp_path / "copy.docx")
    assert target == tmp_path / "copy.docx"
    assert DocxReview(target).comments()[0].text == "carried over"
    assert DocxReview(sample).comments() == []


def test_save_leaves_no_temporary_file(sample):
    review = DocxReview(sample)
    review.add_comment((0, 0), (0, 6), "note", author="R")
    review.save()
    assert [p.name for p in sample.parent.iterdir()] == ["sample.docx"]


def test_a_real_word_file_keeps_its_parts(tmp_path):
    """The manuscript this engine was written for, if it is still there."""
    source = (
        "/home/richard/research/extracts/ut/projekt/"
        "2021-011-merga-terefe-environmental-pollutants-allergy/"
        "5-manuscript/Chemicals_manuscript_draft_20260818.docx"
    )
    if not zipfile.is_zipfile(source):
        pytest.skip("manuscript not available")
    path = tmp_path / "manuscript.docx"
    shutil.copy(source, path)
    before = parts(path)

    review = DocxReview(path)
    texts = review.para_texts()
    theirs = review.comments()
    assert theirs, "the manuscript should carry the coauthor's comments"
    review.add_comment((28, 0), (28, 10), "note", author="R")
    review.save()

    after = parts(path)
    assert set(after) == set(before)
    untouched = [n for n in before if after[n] == before[n]]
    assert set(before) - set(untouched) == {
        "word/document.xml",
        "word/comments.xml",
    }
    reloaded = DocxReview(path)
    assert reloaded.para_texts() == texts
    assert len(reloaded.comments()) == len(theirs) + 1
