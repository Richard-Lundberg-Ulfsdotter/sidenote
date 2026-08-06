"""TUI smoke tests. Drives the app headlessly with Textual's pilot."""

import asyncio
import shutil

import pytest
from textual.widgets import Static

from sidenote.cli import make_sample
from sidenote.engine import OdtReview, open_review
from sidenote.tui import (
    DOC_MIN_WIDTH,
    PANEL_MIN_WIDTH,
    PANEL_STEP,
    PANEL_WIDTH,
    DelLine,
    Line,
    ReviewApp,
    sentence_at,
    sentence_spans,
    word_object,
    wrap_offsets,
)

needs_soffice = pytest.mark.skipif(
    shutil.which("soffice") is None, reason="LibreOffice not installed"
)


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "sample.odt"
    make_sample(path)
    return path


def test_wrap_offsets_lossless():
    text = "Maternal folate intake during pregnancy has been linked to outcomes."
    spans = wrap_offsets(text, 20)
    assert "".join(text[s:e] for s, e in spans) == text
    assert all(e - s <= 21 for s, e in spans)


def test_wrap_offsets_hard_breaks():
    text = "line one\nline two"
    spans = wrap_offsets(text, 40)
    assert [text[s:e] for s, e in spans] == ["line one", "line two"]


def test_select_and_comment_multiline(sample):
    async def scenario():
        app = ReviewApp(sample, author="Richard")
        async with app.run_test(size=(100, 30)) as pilot:
            # into paragraph 1, select eight characters, comment
            await pilot.press("j", "j", "v", *("l" * 7), "c")
            await pilot.pause()
            for ch in "check this":
                await pilot.press(ch)
            await pilot.press("enter")
            for ch in "second line":
                await pilot.press(ch)
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.anchor is None
            assert len(app.comment_list) == 1
            sel_len = app.comment_list[0].end[1] - app.comment_list[0].start[1]
            assert sel_len == 8
            await pilot.press("q")

    asyncio.run(scenario())
    comments = OdtReview(sample).comments()
    assert len(comments) == 1
    assert comments[0].text == "check this\nsecond line"
    assert comments[0].author == "Richard"


def test_edit_comment(sample):
    review = OdtReview(sample)
    review.add_comment((1, 9), (1, 22), "old text", author="R")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("right_square_bracket", "m")
            await pilot.pause()
            # the dialog names the comment's author
            assert app.screen.byline.startswith("comment by R")
            # append to the prefilled text
            for ch in " amended":
                await pilot.press(ch)
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.comment_list[0].text == "old text amended"
            await pilot.press("q")

    asyncio.run(scenario())
    assert OdtReview(sample).comments()[0].text == "old text amended"


def test_navigation_and_delete(sample):
    review = OdtReview(sample)
    review.add_comment((1, 9), (1, 22), "note", author="R")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("G")
            assert app.cur[0] == len(app.texts) - 1
            await pilot.press("g", "g")
            assert app.cur == (0, 0)
            await pilot.press("right_square_bracket")
            assert app.cur == (1, 9)
            await pilot.press("d")
            await pilot.pause()
            # the delete waits on the confirm, nothing is gone yet
            assert len(app.comment_list) == 1
            await pilot.press("y")
            await pilot.pause()
            assert app.comment_list == []
            await pilot.press("q")

    asyncio.run(scenario())
    assert OdtReview(sample).comments() == []


@pytest.mark.parametrize("key", ["n", "escape"])
def test_delete_can_be_called_off(sample, key):
    """A stray d must not cost a comment. Only y agrees."""
    review = OdtReview(sample)
    review.add_comment((1, 9), (1, 22), "note", author="R")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("right_square_bracket", "d")
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            assert [c.text for c in app.comment_list] == ["note"]
            await pilot.press("q")

    asyncio.run(scenario())
    assert [c.text for c in OdtReview(sample).comments()] == ["note"]


def test_vim_motions(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            # e jumps to end of first word ("Sample" ends at offset 5)
            await pilot.press("e")
            assert app.cur == (0, 5)
            # } and { jump between paragraphs
            await pilot.press("}")
            assert app.cur == (1, 0)
            await pilot.press("}")
            assert app.cur == (2, 0)
            # { at a paragraph start goes to the previous paragraph
            await pilot.press("{")
            assert app.cur == (1, 0)
            # { from mid-paragraph goes to its start first
            await pilot.press("w", "w")
            assert app.cur[1] > 0
            await pilot.press("{")
            assert app.cur == (1, 0)
            # ctrl+e / ctrl+y scroll without moving the cursor
            before = app.cur
            await pilot.press("ctrl+e", "ctrl+y")
            assert app.cur == before
            await pilot.press("q")

    asyncio.run(scenario())


def test_sentence_spans_and_word_object():
    text = "One sentence here. A second one! And a third?"
    spans = sentence_spans(text)
    assert [text[s:b] for s, b, _ in spans] == [
        "One sentence here.",
        "A second one!",
        "And a third?",
    ]
    # the trailing space belongs to `as`, not `is`
    assert text[spans[0][0]:spans[0][2]] == "One sentence here. "
    assert sentence_at(text, 5) == spans[0]
    assert sentence_at(text, 20) == spans[1]
    # a paragraph with no terminator is one sentence
    assert sentence_spans("no full stop") == [(0, 12, 12)]
    assert sentence_spans("") == [(0, 0, 0)]

    assert word_object(text, 5, around=False) == (4, 12)  # "sentence"
    assert word_object(text, 5, around=True) == (4, 13)  # plus its space
    # no space to the right, so `aw` takes the one on the left
    assert text[39:44] == "third"
    assert word_object(text, 43, around=True) == (38, 44)


def test_find_char_motions(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("}")
            text = app.texts[1]
            first = text.index("f")
            second = text.index("f", first + 1)
            # f jumps onto the character, in this paragraph only
            await pilot.press("f", "f")
            assert app.cur == (1, first)
            # ; repeats, , goes back
            await pilot.press(";")
            assert app.cur == (1, second)
            await pilot.press(",")
            assert app.cur == (1, first)
            # t stops one short of the next one
            await pilot.press("t", "f")
            assert app.cur == (1, second - 1)
            # F searches backwards, T stops one after
            await pilot.press("F", "f")
            assert app.cur == (1, first)
            # $ stops at the end of the display line, T searches back from there
            await pilot.press("$")
            eol = app.cur[1]
            await pilot.press("T", "f")
            assert app.cur == (1, text.rfind("f", 0, eol - 1) + 1)
            # a character that is not in the paragraph leaves the cursor
            before = app.cur
            await pilot.press("f", "~")
            assert app.cur == before
            await pilot.press("q")

    asyncio.run(scenario())


def test_sentence_motions(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("}")
            second = sentence_spans(app.texts[1])[1][0]
            await pilot.press(")")
            assert app.cur == (1, second)
            # from the last sentence, ) crosses into the next paragraph
            await pilot.press(")")
            assert app.cur == (2, 0)
            # ( walks back the same way
            await pilot.press("(")
            assert app.cur == (1, second)
            # mid-sentence, ( goes to the start of that sentence first
            await pilot.press("w", "w")
            await pilot.press("(")
            assert app.cur == (1, second)
            await pilot.press("(")
            assert app.cur == (1, 0)
            await pilot.press("q")

    asyncio.run(scenario())


def test_text_objects_select_for_commenting(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("}", "w", "w")
            start, body_end, end = sentence_at(app.texts[1], app.cur[1])
            # is selects the sentence, as takes the trailing space too
            await pilot.press("i", "s")
            assert app.anchor == (1, start)
            assert app.cur == (1, body_end - 1)
            await pilot.press("escape")
            await pilot.press("a", "s")
            assert app.cur == (1, end - 1)
            await pilot.press("escape")
            # iw and ip work the same way
            await pilot.press("i", "w")
            lo, hi = word_object(app.texts[1], app.cur[1], around=False)
            assert app.anchor == (1, lo)
            await pilot.press("escape")
            await pilot.press("i", "p")
            assert app.anchor == (1, 0)
            assert app.cur == (1, len(app.texts[1]) - 1)
            await pilot.press("escape")
            # a sentence object then c comments on exactly that sentence
            await pilot.press("{")
            await pilot.press("i", "s", "c")
            await pilot.pause()
            for ch in "unclear":
                await pilot.press(ch)
            await pilot.press("ctrl+s")
            await pilot.pause()
            comment = app.comment_list[0]
            assert comment.start == (1, 0)
            assert app.texts[1][comment.start[1]:comment.end[1]] == (
                app.texts[1][:body_end]
            )
            await pilot.press("q")

    asyncio.run(scenario())


def test_visual_mode_selects_the_whole_object(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            # from the middle of the sentence, both ends move to it
            await pilot.press("}", "w", "w", "w", "v", "a", "s")
            start, _, end = sentence_spans(app.texts[1])[0]
            assert app.anchor == (1, start)
            assert app.cur == (1, end - 1)
            await pilot.press("q")

    asyncio.run(scenario())


def test_pending_keys_do_not_fire_their_bindings(sample):
    """The second key of a sequence must not also run its binding."""

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            # `as` would otherwise open the comments sidebar with s
            await pilot.press("}", "v", "a", "s")
            await pilot.pause()
            assert not app.sidebar.has_class("visible")
            # so would jumping to an s with f
            await pilot.press("escape", "{", "f", "s")
            await pilot.pause()
            assert not app.sidebar.has_class("visible")
            assert app.texts[1][app.cur[1]] == "s"
            # and S, the changes panel, as an f target
            await pilot.press("f", "S")
            await pilot.pause()
            assert not app.changes_panel.has_class("visible")
            # r, the reference overlay, must not open as an f target
            await pilot.press("f", "r")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            await pilot.press("q")

    asyncio.run(scenario())


def test_search(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("slash")
            await pilot.pause()
            for ch in "folate":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.search_matches) == 2
            first = app.cur
            assert app.texts[first[0]][first[1]:first[1] + 6] == "folate"
            await pilot.press("n")
            second = app.cur
            assert second != first
            await pilot.press("n")  # wraps around
            assert app.cur == first
            await pilot.press("N")
            assert app.cur == second
            # escape clears the search
            await pilot.press("escape")
            assert app.search_query == ""
            await pilot.press("q")

    asyncio.run(scenario())


def test_star_search_word_under_cursor(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            # put the cursor on "folate" in paragraph 1
            await pilot.press("slash")
            await pilot.pause()
            for ch in "folate":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            first = app.cur
            # * finds the next whole-word occurrence
            await pilot.press("*")
            assert app.search_query == "folate"
            assert app.cur != first
            second = app.cur
            assert app.texts[second[0]][second[1]:second[1] + 6] == "folate"
            # # goes back to the first
            await pilot.press("#")
            assert app.cur == first
            # n keeps working on the * results
            await pilot.press("n")
            assert app.cur == second
            await pilot.press("q")

    asyncio.run(scenario())


def test_star_word_boundaries(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            # "in" appears inside many words but as a word fewer times
            await pilot.press("slash")
            await pilot.pause()
            for ch in "in gestational":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("*")  # word under cursor is "in"
            assert app.search_query == "in"
            for mp, mo in app.search_matches:
                t = app.texts[mp]
                assert t[mo:mo + 2] == "in"
                after = t[mo + 2:mo + 3]
                assert not after.isalnum()  # no "intake", "inconsistent"
            await pilot.press("q")

    asyncio.run(scenario())


@needs_soffice
def test_export_binding(sample, tmp_path):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("X")
            await app.workers.wait_for_complete()
            await pilot.press("q")

    asyncio.run(scenario())
    assert sample.with_suffix(".docx").exists()


def test_help_screen(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("question_mark")
            await pilot.pause()
            assert len(app.screen_stack) > 1
            # j scrolls the help, it does not close it or move the cursor
            await pilot.press("j")
            await pilot.pause()
            assert len(app.screen_stack) > 1
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert app.cur == (0, 0)
            await pilot.press("q")

    asyncio.run(scenario())


def test_sidebar_keyboard(sample):
    review = OdtReview(sample)
    review.add_comment((1, 0), (1, 8), "first", author="R")
    review.add_comment((2, 3), (2, 7), "second", author="R")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            # s opens and focuses the sidebar
            await pilot.press("s")
            await pilot.pause()
            await pilot.pause()
            assert app.focused is app.sidebar
            assert app.sidebar.index == 0
            # j moves the selection, enter jumps to the anchor
            await pilot.press("j")
            assert app.sidebar.index == 1
            await pilot.press("enter")
            await pilot.pause()
            assert app.cur == (2, 3)
            assert app.focused is app.doc_view
            # tab returns to the sidebar, d deletes the selected comment
            await pilot.press("tab")
            assert app.focused is app.sidebar
            await pilot.press("g", "d")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            await pilot.pause()
            assert [c.text for c in app.comment_list] == ["second"]
            # escape returns focus to the document, s closes the sidebar
            await pilot.press("escape")
            assert app.focused is app.doc_view
            await pilot.press("s")
            await pilot.pause()
            assert not app.sidebar.has_class("visible")
            await pilot.press("q")

    asyncio.run(scenario())
    assert [c.text for c in OdtReview(sample).comments()] == ["second"]


def test_sidebar_follows_the_cursor(sample):
    review = OdtReview(sample)
    review.add_comment((1, 0), (1, 8), "first", author="R")
    review.add_comment((2, 3), (2, 7), "second", author="R")
    review.add_comment((3, 0), (3, 5), "third", author="R")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            await pilot.pause()
            await pilot.press("escape")
            assert app.focused is app.doc_view
            # ] walks the comments, the sidebar selection walks with it
            await pilot.press("]")
            assert app.cur == (1, 0)
            assert app.sidebar.index == 0
            await pilot.press("]")
            assert app.cur == (2, 3)
            assert app.sidebar.index == 1
            await pilot.press("]")
            assert app.sidebar.index == 2
            await pilot.press("[")
            assert app.sidebar.index == 1
            # inside the span counts, not just its first character
            await pilot.press("l")
            assert app.cur == (2, 4)
            assert app.sidebar.index == 1
            # off any comment the last selection stands
            await pilot.press("G")
            assert app.cur == (4, 0)
            assert app.sidebar.index == 1
            await pilot.press("q")

    asyncio.run(scenario())


def test_ctrl_arrows_move_the_divider(sample):
    review = OdtReview(sample)
    review.add_comment((1, 0), (1, 8), "first", author="R")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            # closed panel, the divider does not move
            await pilot.press("ctrl+left")
            assert app.panel_width == PANEL_WIDTH
            await pilot.press("s")
            await pilot.pause()
            await pilot.pause()
            doc_width = app.doc_view.size.width
            # ctrl+left grows the panel, the document gives up the columns
            await pilot.press("ctrl+left")
            await pilot.pause()
            await pilot.pause()
            assert app.panel_width == PANEL_WIDTH + PANEL_STEP
            assert app.sidebar.outer_size.width == PANEL_WIDTH + PANEL_STEP
            assert app.doc_view.size.width == doc_width - PANEL_STEP
            # the wrap layout follows the new document width
            assert app.lines[0].end <= app.doc_view.size.width
            # ctrl+right hands them back
            await pilot.press("ctrl+right")
            await pilot.pause()
            await pilot.pause()
            assert app.panel_width == PANEL_WIDTH
            assert app.doc_view.size.width == doc_width
            # the panel stops before it starves the document
            for _ in range(30):
                await pilot.press("ctrl+left")
            await pilot.pause()
            assert app.panel_width == 100 - DOC_MIN_WIDTH
            assert app.doc_view.size.width >= DOC_MIN_WIDTH
            # and it stops at the minimum going the other way
            for _ in range(30):
                await pilot.press("ctrl+right")
            await pilot.pause()
            assert app.panel_width == PANEL_MIN_WIDTH
            # the changes panel keeps the width the user chose
            await pilot.press("escape")
            await pilot.press("S")
            await pilot.pause()
            await pilot.pause()
            assert app.changes_panel.outer_size.width == PANEL_MIN_WIDTH
            await pilot.press("q")

    asyncio.run(scenario())


def test_comment_dialog_shows_full_anchor(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(120, 40)) as pilot:
            # select from inside paragraph 1 across into paragraph 2
            await pilot.press("}", "w", "v", "}", "e", "c")
            await pilot.pause()
            preview = app.screen.anchor_preview
            # full text of both fragments, joined across the break
            assert preview.startswith("folate intake during pregnancy")
            assert "remains inconsistent." in preview
            assert preview.endswith("\nWe")
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(scenario())


def test_sidebar_author_filter(sample):
    review = OdtReview(sample)
    review.add_comment((1, 0), (1, 8), "richard's note", author="Richard")
    review.add_comment((2, 0), (2, 4), "anna's note", author="Anna")
    review.add_comment((3, 0), (3, 6), "anna again", author="Anna")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            await pilot.pause()
            assert len(app.sidebar) == 3
            # filter by author, case-insensitive
            await pilot.press("slash")
            await pilot.pause()
            for ch in "anna":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert len(app.sidebar) == 2
            assert "2/3" in str(app.sidebar.border_title)
            # enter jumps to the first Anna comment, not Richard's
            await pilot.press("enter")
            await pilot.pause()
            assert app.cur == (2, 0)
            # back in the sidebar, d deletes the selected Anna comment
            await pilot.press("tab", "d")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            await pilot.pause()
            assert [c.author for c in app.comment_list] == ["Richard", "Anna"]
            assert len(app.sidebar) == 1
            # escape clears the filter and shows all again
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()
            assert len(app.sidebar) == 2
            assert app.focused is app.sidebar
            # next escape leaves the sidebar
            await pilot.press("escape")
            assert app.focused is app.doc_view
            await pilot.press("q")

    asyncio.run(scenario())


def test_sidebar_filter_matches_text(sample):
    review = OdtReview(sample)
    review.add_comment((1, 0), (1, 8), "check the FFQ wording", author="Richard")
    review.add_comment((2, 0), (2, 4), "needs enrolment years", author="Anna")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            await pilot.pause()
            # a word from the comment body matches, regardless of author
            await pilot.press("slash")
            await pilot.pause()
            for ch in "FFQ":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert len(app.sidebar) == 1
            await pilot.press("enter")
            await pilot.pause()
            assert app.cur == (1, 0)
            await pilot.press("q")

    asyncio.run(scenario())


def test_sidebar_search_commands(sample):
    review = OdtReview(sample)
    review.add_comment((1, 0), (1, 5), "first note", author="Richard")
    review.add_comment((2, 0), (2, 5), "second note", author="Anna")
    review.add_comment((3, 0), (3, 5), "third note", author="Richard")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            await pilot.pause()
            assert app.sidebar.index == 0
            # n cycles forward with wraparound, N backward
            await pilot.press("n", "n")
            assert app.sidebar.index == 2
            await pilot.press("n")
            assert app.sidebar.index == 0
            await pilot.press("N")
            assert app.sidebar.index == 2
            # * on Richard's second comment filters to Richard and wraps
            # to his first
            await pilot.press("asterisk")
            await pilot.pause()
            await pilot.pause()
            assert app.sidebar_filter == "Richard"
            assert len(app.sidebar) == 2
            assert app.sidebar.index == 0
            assert [
                app.comment_list[i].author for i in app._sidebar_indices
            ] == ["Richard", "Richard"]
            # escape clears the filter, all three visible again
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()
            assert len(app.sidebar) == 3
            await pilot.press("q")

    asyncio.run(scenario())


def test_sidebar_filter_no_match(sample):
    review = OdtReview(sample)
    review.add_comment((1, 0), (1, 8), "note", author="Richard")
    review.save()

    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            for ch in "nobody":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            # filter rejected, list unchanged
            assert app.sidebar_filter == ""
            assert len(app.sidebar) == 1
            await pilot.press("q")

    asyncio.run(scenario())


def test_z_view_positioning(sample):
    async def scenario():
        app = ReviewApp(sample)
        async with app.run_test(size=(100, 10)) as pilot:
            await pilot.press("G")
            before = app.cur
            await pilot.press("z", "t")
            await pilot.pause()
            # cursor unchanged, its line scrolled as far up as possible
            assert app.cur == before
            top = int(app.doc_view.scroll_offset.y)
            assert top > 0
            assert top <= app._cursor_line() < top + app.doc_view.size.height
            await pilot.press("z", "z")
            await pilot.pause()
            assert app.cur == before
            await pilot.press("q")

    asyncio.run(scenario())


def test_tracked_changes_in_tui(tracked_sample):
    async def scenario():
        app = ReviewApp(tracked_sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # insertion range styled in paragraph 0
            styles = app._para_styles(0)
            assert (6, 15, "green") in styles
            # deletion point marked in paragraph 1
            styles = app._para_styles(1)
            assert any(s == "underline red" for _, _, s in styles)
            # > and < jump between changes with wraparound
            await pilot.press(">")
            assert app.cur == (0, 6)
            assert app._change_at_cursor().author == "Anna"
            await pilot.press(">")
            assert app.cur == (1, 6)
            assert app._change_at_cursor().author == "Magnus"
            await pilot.press(">")
            assert app.cur == (0, 6)
            await pilot.press("q")

    asyncio.run(scenario())


def test_changes_panel(tracked_sample):
    async def scenario():
        app = ReviewApp(tracked_sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("S")
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            assert app.focused is app.changes_panel
            assert len(app.changes_panel) == 2
            # jump to the deletion from the panel
            await pilot.press("j", "enter")
            await pilot.pause()
            assert app.cur == (1, 6)
            assert app.focused is app.doc_view
            # panels are mutually exclusive
            await pilot.press("S")
            await pilot.pause()
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert not app.changes_panel.has_class("visible")
            assert app.sidebar.has_class("visible")
            await pilot.press("escape", "q")

    asyncio.run(scenario())


def test_deleted_text_lines(tracked_sample):
    async def scenario():
        app = ReviewApp(tracked_sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # the deleted text renders as a virtual line after the
            # display line containing the deletion point
            del_lines = [l for l in app.lines if isinstance(l, DelLine)]
            assert [l.text for l in del_lines] == ["old text"]
            row = next(
                i for i, l in enumerate(app.lines) if isinstance(l, DelLine)
            )
            rendered = app.render_doc_line(row)
            assert rendered.plain == "- old text"
            # the cursor can never land on a virtual line
            for _ in range(10):
                await pilot.press("j")
            assert isinstance(app.lines[app._cursor_line()], Line)
            # D hides the deleted text, and shows it again
            await pilot.press("D")
            assert not any(isinstance(l, DelLine) for l in app.lines)
            await pilot.press("D")
            assert any(isinstance(l, DelLine) for l in app.lines)
            await pilot.press("q")

    asyncio.run(scenario())


def test_deletion_status_excerpt(tracked_sample):
    async def scenario():
        app = ReviewApp(tracked_sample)
        async with app.run_test(size=(100, 30)) as pilot:
            # jump to the deletion point, status names author and text
            await pilot.press(">", ">")
            assert app.cur == (1, 6)
            ch = app._change_at_cursor()
            assert ch.kind == "deletion"
            assert ch.text == "old text"
            await pilot.press("q")

    asyncio.run(scenario())


def test_status_bar_survives_long_filename(tmp_path):
    long_name = (
        "Comparison of ASQ at different ages with the developmental "
        "assessment at the Child Healthcare Center.odt"
    )
    path = tmp_path / long_name
    make_sample(path)

    async def scenario():
        app = ReviewApp(path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            status = app._status_text()
            # the filename is truncated so the live info stays visible
            assert "NORMAL" in status
            assert "para 1/5" in status
            assert "comments 0" in status
            assert long_name not in status
            await pilot.press("q")

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# Markdown documents
# ----------------------------------------------------------------------


@pytest.fixture
def md_sample(tmp_path):
    path = tmp_path / "sample.md"
    make_sample(path)
    return path


def test_markdown_comment_leaves_the_document_untouched(md_sample):
    before = md_sample.read_bytes()

    async def scenario():
        app = ReviewApp(md_sample, author="Richard")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("j", "j", "v", *("l" * 7), "c")
            await pilot.pause()
            for ch in "check this":
                await pilot.press(ch)
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert len(app.comment_list) == 1
            await pilot.press("q")

    asyncio.run(scenario())
    assert md_sample.read_bytes() == before
    review = open_review(md_sample)
    (comment,) = review.comments()
    assert comment.text == "check this"
    assert comment.author == "Richard"
    assert not comment.orphan


def test_markdown_orphan_shows_in_the_sidebar(md_sample):
    review = open_review(md_sample)
    off = review.para_texts()[1].index("neurodevelopment")
    review.add_comment((1, off), (1, off + 16), "spell out", author="R")
    review.save()
    # rewriting the whole paragraph removes the anchor and its context,
    # replacing the word alone would just carry the comment across
    md_sample.write_text(
        md_sample.read_text().replace(
            review.para_texts()[1],
            "Folate status in pregnancy has been studied for decades.",
        ),
        encoding="utf-8",
    )

    async def scenario():
        app = ReviewApp(md_sample)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.comment_list[0].orphan
            await pilot.press("q")

    asyncio.run(scenario())


def test_markdown_export_fails_cleanly(md_sample):
    review = open_review(md_sample)
    with pytest.raises(RuntimeError, match="no docx export"):
        review.export_docx()


# ----------------------------------------------------------------------
# Reference overlay
# ----------------------------------------------------------------------


REFERENCE_CHECK = """\
# Reference check

- Full texts: `{refs}/<key>.pdf`

## 1. Introduction

| Statement                | Reference                                | Supporting quote        | Status |
|:-------------------------|:-----------------------------------------|:------------------------|:-------|
| Folate and neural tubes  | bjorkeFolate2023                         | "prevents most cases"   | OK     |
| Early insults last       | barkerFetal1990; lucasFetalOrigins1999   | "lifetime consequences" | FIXED  |
"""


@pytest.fixture
def cited(tmp_path):
    """A markdown document with citations and a reference-check beside it."""
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "bjorkeFolate2023.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "reference-check.md").write_text(
        REFERENCE_CHECK.format(refs=refs), encoding="utf-8"
    )
    path = tmp_path / "manuscript.md"
    path.write_text(
        "# Paper\n\n"
        "Folate reduces neural tube defect risk [@bjorkeFolate2023].\n\n"
        "Early insults have lifetime consequences "
        "[@barkerFetal1990; @lucasFetalOrigins1999].\n",
        encoding="utf-8",
    )
    return path


def _overlay_text(app) -> str:
    return str(app.screen.query_one("#reference-body", Static).content)


def test_reference_overlay_shows_the_row(cited, monkeypatch):
    monkeypatch.delenv("SIDENOTE_REFCHECK", raising=False)

    async def scenario():
        app = ReviewApp(cited)
        async with app.run_test(size=(100, 30)) as pilot:
            app.cur = (1, app.texts[1].index("bjorke"))
            await pilot.press("r")
            await pilot.pause()
            assert len(app.screen_stack) > 1
            body = _overlay_text(app)
            assert "bjorkeFolate2023" in body
            assert "Folate and neural tubes" in body
            assert "prevents most cases" in body
            assert "1. Introduction" in body
            # the full text is on disk, so its path is offered
            assert "bjorkeFolate2023.pdf" in body
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            await pilot.press("q")

    asyncio.run(scenario())


def test_reference_overlay_steps_through_a_grouped_citation(cited, monkeypatch):
    monkeypatch.delenv("SIDENOTE_REFCHECK", raising=False)

    async def scenario():
        app = ReviewApp(cited)
        async with app.run_test(size=(100, 30)) as pilot:
            app.cur = (2, app.texts[2].index("barker"))
            await pilot.press("r")
            await pilot.pause()
            assert "barkerFetal1990" in _overlay_text(app)
            await pilot.press("n")
            await pilot.pause()
            body = _overlay_text(app)
            assert "lucasFetalOrigins1999" in body
            # the row is shared, so the statement comes along
            assert "Early insults last" in body
            # no pdf for this one, and it says where it looked
            assert "no full text on disk" in body
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(scenario())


def test_reference_key_off_a_citation_does_nothing(cited, monkeypatch):
    monkeypatch.delenv("SIDENOTE_REFCHECK", raising=False)

    async def scenario():
        app = ReviewApp(cited)
        async with app.run_test(size=(100, 30)) as pilot:
            app.cur = (0, 0)
            await pilot.press("r")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            await pilot.press("q")

    asyncio.run(scenario())


def test_reference_overlay_opens_the_pdf(cited, monkeypatch, tmp_path):
    monkeypatch.delenv("SIDENOTE_REFCHECK", raising=False)
    log = tmp_path / "opened.txt"
    viewer = tmp_path / "fake-viewer"
    viewer.write_text(f'#!/bin/sh\necho "$1" >> {log}\n', encoding="utf-8")
    viewer.chmod(0o755)
    monkeypatch.setenv("SIDENOTE_PDF_VIEWER", str(viewer))

    async def scenario():
        app = ReviewApp(cited)
        async with app.run_test(size=(100, 30)) as pilot:
            app.cur = (1, app.texts[1].index("bjorke"))
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause(0.3)
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(scenario())
    assert log.exists()
    assert log.read_text().strip().endswith("bjorkeFolate2023.pdf")


def test_reference_key_without_a_check_file(md_sample, monkeypatch):
    """A document with no reference-check file just says so."""
    monkeypatch.delenv("SIDENOTE_REFCHECK", raising=False)
    md_sample.write_text(
        "Folate matters [@smithFolate2020] here.\n", encoding="utf-8"
    )

    async def scenario():
        app = ReviewApp(md_sample)
        async with app.run_test(size=(100, 30)) as pilot:
            app.cur = (0, app.texts[0].index("smith"))
            await pilot.press("r")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            await pilot.press("q")

    asyncio.run(scenario())
