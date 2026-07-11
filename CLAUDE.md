# sidenote — quick reference

Terminal reviewer for ODT files with character-level comments, written as
native ODF inline annotations. See README.md for usage and keys.

## Commands

```sh
.venv/bin/python -m pytest tests/ -q      # run tests (includes soffice export)
.venv/bin/sidenote sample /tmp/demo.odt # make a test document
.venv/bin/sidenote /tmp/demo.odt        # open the TUI
```

## Design notes

- Positions are `(paragraph_index, character_offset)` into the plain text
  from `OdtReview.para_texts()`. Annotations are zero-width in that text,
  so existing comments never shift offsets.
- `engine._insert_at` splits text nodes and `text:s` runs at the offset.
  Ranged comments insert the `office:annotation-end` marker before the
  start element so the second insertion cannot invalidate the first.
- odfpy quirk. `Annotation(name=...)` sets `draw:name`, which is wrong for
  ranged comments. Use `setAttrNS(OFFICENS, "name", ...)` instead.
  `AnnotationEnd(name=...)` maps correctly to `office:name`.
- Point comments (start == end) get no `office:name` and no end marker,
  matching how LibreOffice writes them.
- `engine.docx_to_odt` makes the sibling `.odt` working copy for docx
  input. It reuses the working copy when its mtime is at least the
  docx's, so comments in the copy survive reopening. `update_comment`
  replaces the annotation's `text:p` children and refreshes `dc:date`.
- docx export shells out to `soffice --headless --convert-to docx`.
  LibreOffice converts named annotation ranges to Word
  `commentRangeStart`/`commentRangeEnd` and splits runs as needed. The
  test suite asserts this end to end by unzipping the produced docx.
- The TUI never manipulates XML. It maps display lines back to engine
  positions (`ReviewApp.lines`) and reloads the document after every
  mutation. Keep new frontends on the same engine API.
- Performance model. `DocumentView` is a line-API `ScrollView`, its
  `render_line` styles only visible rows. The wrap layout is cached in
  `ReviewApp.lines` and rebuilt only by `rebuild_lines()` (resize,
  reload, sidebar toggle via resize). Keystrokes go through
  `_repaint()` which is O(viewport). Do NOT reintroduce a full-document
  `RichText` build per keypress, that was ~180 ms/key on a
  400-paragraph document, `_repaint` is ~0.2 ms.
- Key handling. Movement lives in `ReviewApp.on_key` (guarded when a
  modal is up or the document pane is not focused, ends with
  `event.stop()`), actions live in BINDINGS. `e` is vim word-end, docx
  export is `X`. Literal `[` in help text must be escaped as `\\[`,
  and all user-supplied strings (comment text, filenames, anchors) go
  through `rich.markup.escape` before landing in a Static, or markup
  parsing crashes.
- Tracked changes are read-only. `engine.changes()` pairs inline
  `text:change-start/end/change` markers (zero-width, found by the
  same `_collect_marks` walker as annotations) with author/date and
  deleted text from the `text:tracked-changes` block, which
  `_collect_paragraphs` skips. Insertions live in the body text,
  deletions only in the metadata block. Deleted text renders as
  `DelLine` virtual display lines in `ReviewApp.lines` (toggled with
  `D`). The cursor and all offset math skip anything that is not a
  `Line`, so the invariant holds. Format-only regions are ignored.
- The sidebar is a `SideList(ListView)`, fully keyboard-driven, and
  the tracked-changes panel (`T`) is a second `SideList` instance.
  The two panels are mutually exclusive.
  `m`/`d` resolve their target via `_target_comment()` (sidebar
  selection when focused, else document cursor). Rebuilds go through
  `_update_sidebar()` whose index set is deferred with
  `call_after_refresh` because ListView clear/extend are queued DOM
  operations.
- `render_line` strips must be padded to the pane width with the
  widget's `rich_style` (`adjust_cell_length`), otherwise the area
  after the text paints in the terminal default background.
- Pilot benchmarks. `pilot.press` has ~65 ms fixed harness overhead per
  key, measure app cost with direct calls (`_move_vertical` +
  `_repaint`) or subtract the floor from an ignored-key baseline.
- TUI testing works headlessly via Textual's pilot,
  `app.run_test()` + `pilot.press(...)`. Screenshots via
  `app.save_screenshot()` render text-run spacing slightly off after
  rsvg-convert, check the SVG text runs before chasing "missing" spaces.
