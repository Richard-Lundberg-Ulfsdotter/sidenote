"""Reference-check parsing and citation detection."""

import pytest

from sidenote.refcheck import (
    find_reference_check,
    load_reference_check,
    parse_entries,
    references_dir,
)
from sidenote.tui import bracket_span, citations_at

CHECK = """\
# Reference check — Paper 2a

Every citation in `manuscript.md` mapped to the sentence it supports.

- Full texts: `{refs}/<key>.pdf`, greppable mirror at `references/md/<key>.md`
- Status values are `OK`, `FIXED`, `WEAK`

## 1. Introduction

| Statement                        | Reference                    | Supporting quote                  | Status |
|:---------------------------------|:-----------------------------|:----------------------------------|:-------|
| Maternal diet influences health  | jouanneNutrientRequirements2021 | "critical for her health"      | OK     |
| Fiber and lower GDM risk         | zhangDietaryFiberIntake2006  | "26% (95% CI 9-49) reduction"     | FIXED  |
| Energy cutoffs are standard      | liMaternalDietary2024a; yangDietaryProtein2022 | "less than 500 kcal/day" | OK |

## 2. Materials and Methods

| Statement                | Reference                       | Supporting quote        | Status |
|:-------------------------|:--------------------------------|:------------------------|:-------|
| Fiber >= 3 g/MJ          | jouanneNutrientRequirements2021 | "at least 3 g/MJ"       | WEAK   |
"""


@pytest.fixture
def check_file(tmp_path):
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "zhangDietaryFiberIntake2006.pdf").write_bytes(b"%PDF-1.4\n")
    path = tmp_path / "manuscript" / "reference-check.md"
    path.parent.mkdir()
    path.write_text(CHECK.format(refs=refs), encoding="utf-8")
    return path


def test_parses_rows_by_column_name(check_file):
    entries = parse_entries(check_file.read_text())
    (row,) = entries["zhangDietaryFiberIntake2006"]
    assert row.section == "1. Introduction"
    assert row.statement == "Fiber and lower GDM risk"
    assert row.quote == '"26% (95% CI 9-49) reduction"'
    assert row.status == "FIXED"


def test_a_key_cited_twice_keeps_both_rows(check_file):
    entries = parse_entries(check_file.read_text())
    rows = entries["jouanneNutrientRequirements2021"]
    assert [r.section for r in rows] == ["1. Introduction", "2. Materials and Methods"]
    assert [r.status for r in rows] == ["OK", "WEAK"]


def test_a_row_citing_two_keys_is_indexed_under_both(check_file):
    entries = parse_entries(check_file.read_text())
    for key in ("liMaternalDietary2024a", "yangDietaryProtein2022"):
        (row,) = entries[key]
        assert row.statement == "Energy cutoffs are standard"


def test_prose_and_bullets_are_not_rows(check_file):
    entries = parse_entries(check_file.read_text())
    assert "manuscript.md" not in entries
    assert len(entries) == 4


def test_pdf_directory_comes_from_the_file(check_file, tmp_path):
    check = load_reference_check(check_file)
    assert check.pdf_dir == tmp_path / "references"
    assert check.pdf_for("zhangDietaryFiberIntake2006").is_file()
    # a key with no file still reports where it looked
    assert check.pdf_for("jouanneNutrientRequirements2021") is None
    assert check.expected_pdf("jouanneNutrientRequirements2021").parent == (
        tmp_path / "references"
    )


def test_pdf_directory_falls_back_to_the_environment(tmp_path, monkeypatch):
    refs = tmp_path / "corpus"
    refs.mkdir()
    monkeypatch.setenv("SIDENOTE_REFERENCES", str(refs))
    assert references_dir("no path documented here") == refs


def test_found_beside_the_document_and_one_level_up(check_file, monkeypatch):
    monkeypatch.delenv("SIDENOTE_REFCHECK", raising=False)
    manuscript = check_file.parent / "manuscript.md"
    manuscript.write_text("text\n", encoding="utf-8")
    assert find_reference_check(manuscript) == check_file
    nested = check_file.parent / "drafts"
    nested.mkdir()
    deep = nested / "manuscript.md"
    deep.write_text("text\n", encoding="utf-8")
    assert find_reference_check(deep) == check_file


def test_missing_reference_check_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("SIDENOTE_REFCHECK", raising=False)
    lone = tmp_path / "lonely" / "manuscript.md"
    lone.parent.mkdir()
    lone.write_text("text\n", encoding="utf-8")
    assert find_reference_check(lone) is None


def test_unparsable_file_yields_no_entries():
    assert parse_entries("# Notes\n\nJust prose, no tables.\n") == {}


# ----------------------------------------------------------------------
# Citation detection in the document
# ----------------------------------------------------------------------


def test_citation_under_the_cursor():
    text = "Diet matters [@jouanneNutrient2021] in pregnancy."
    off = text.index("jouanne")
    assert citations_at(text, off) == ["jouanneNutrient2021"]
    assert citations_at(text, text.index("pregnancy")) == []


def test_grouped_citation_yields_every_key_cursor_first():
    text = "Two sources [@barkerFetal1990; @lucasFetalOrigins1999] agree."
    assert citations_at(text, text.index("barker")) == [
        "barkerFetal1990",
        "lucasFetalOrigins1999",
    ]
    assert citations_at(text, text.index("lucas")) == [
        "lucasFetalOrigins1999",
        "barkerFetal1990",
    ]
    # on the semicolon, still inside the group
    assert len(citations_at(text, text.index(";"))) == 2


def test_bare_citation_without_brackets():
    text = "As @smithFolate2020 showed, intake was low."
    assert citations_at(text, text.index("smith")) == ["smithFolate2020"]


def test_bracket_span_ignores_a_closed_group():
    text = "[@a2020] and then text"
    assert bracket_span(text, text.index("then")) is None


def test_trailing_punctuation_is_not_part_of_the_key():
    text = "See @smithFolate2020."
    assert citations_at(text, text.index("smith")) == ["smithFolate2020"]
