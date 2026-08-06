# sidenote — quick reference

Terminal reviewer for ODT and Markdown files with character-level comments.
ODT comments are native ODF inline annotations, Markdown comments live in a
sidecar JSON file. See README.md for usage and keys.

## Commands

```sh
.venv/bin/python -m pytest tests/ -q      # run tests (includes soffice export)
.venv/bin/sidenote sample /tmp/demo.odt # make a test document
.venv/bin/sidenote sample /tmp/demo.md  # the markdown equivalent
.venv/bin/sidenote /tmp/demo.odt        # open the TUI
.venv/bin/sidenote check /tmp/demo.md   # orphan report, nonzero on any
```

## Design notes

- Positions are `(paragraph_index, character_offset)` into the plain text
  from `para_texts()`. Annotations are zero-width in that text,
  so existing comments never shift offsets.
- `engine.open_review()` returns `OdtReview` or `MarkdownReview` by file
  suffix. Both expose the same interface (`para_texts`, `comments`,
  `add_comment`, `update_comment`, `delete_comment`, `changes`, `save`,
  `export_docx`), so no caller branches on format. Add a format by adding
  a class with that interface, not by special-casing the TUI.
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
- Markdown. `markdown.split_blocks` makes paragraphs from blank-line
  separated blocks, fenced code kept whole, source shown as written
  rather than rendered so offsets match the file the user edits. The
  `.md` is opened read-only and never written, everything goes to
  `<stem>.sidenote.json`.
- Markdown anchoring. The sidecar cannot move when the document is
  edited elsewhere, so each endpoint stores `before`/`after` context
  (`CONTEXT` = 40 chars) and is relocated by `_Anchor.locate` on load.
  Exact position first, then a search for `before + after`, then one
  side alone if it is at least `MIN_SOLO_CONTEXT` long. A phrase
  replaced in place therefore carries its comment across, which
  surprised two tests during development, see
  `test_comment_follows_a_replaced_phrase`. Only losing both sides
  orphans a comment. Orphans keep their stored anchor (`_recapture`
  skips them) so they can re-attach if the text returns.
- `Comment.orphan` is always False for ODT, where the anchor is an
  element in the document. Only sidecar formats can orphan.
- The sidecar stores a digest of the source. `status()` reports
  totals, orphans, and whether the document moved since the last save.
  The TUI notifies on mount via `_report_anchor_health`, `sidenote
  check` prints the same and exits 1 on any orphan for use in hooks.
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
  selection when focused, else document cursor).
  `_highlight_comment_at_cursor()` runs from `_repaint()` so the
  sidebar selection follows the document cursor. It sets
  `sidebar.index` directly (not deferred) because the list is already
  mounted at that point, and does nothing when the cursor is off any
  comment or the comment is filtered out. Rebuilds go through
  `_update_sidebar()` whose index set is deferred with
  `call_after_refresh` because ListView clear/extend are queued DOM
  operations. `ctrl+left`/`ctrl+right` move the divider via
  `_move_divider`, which writes `styles.width` on both panels (the
  shared `panel_width`) and lets `DocumentView.on_resize` rewrap. The
  CSS literal `44` must stay in sync with `PANEL_WIDTH`.
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
