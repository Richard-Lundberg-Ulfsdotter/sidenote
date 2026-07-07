"""TUI smoke tests. Drives the app headlessly with Textual's pilot."""

import asyncio
import shutil

import pytest
from sidenote.cli import make_sample
from sidenote.engine import OdtReview
from sidenote.tui import ReviewApp, wrap_offsets

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
            assert app.comment_list == []
            await pilot.press("q")

    asyncio.run(scenario())
    assert OdtReview(sample).comments() == []


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
            # t opens and focuses the sidebar
            await pilot.press("t")
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
            await pilot.pause()
            assert [c.text for c in app.comment_list] == ["second"]
            # escape returns focus to the document, t closes the sidebar
            await pilot.press("escape")
            assert app.focused is app.doc_view
            await pilot.press("t")
            await pilot.pause()
            assert not app.sidebar.has_class("visible")
            await pilot.press("q")

    asyncio.run(scenario())
    assert [c.text for c in OdtReview(sample).comments()] == ["second"]


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
            await pilot.press("t")
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
            await pilot.press("t")
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
            await pilot.press("t")
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
            await pilot.press("t")
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
