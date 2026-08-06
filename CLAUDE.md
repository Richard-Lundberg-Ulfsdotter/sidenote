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
.venv/bin/sidenote /tmp/paper.md --refcheck ref-check.md  # r overlay
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
- Markdown comment names are positions, not identities. `save()` calls
  `_renumber()` after `_recapture()`, which sorts `_records` into
  document order and reassigns `cmt1..cmtN`, orphans last because
  their position is a fallback guess. So `cmt3` in the sidecar is the
  comment the sidebar numbers 3, which is the point, a reader of the
  JSON and a reader of the screen name the same comment. The cost is
  that inserting a comment renames every comment below it. ODT does
  NOT do this and should not. There `office:name` is the structural
  key pairing `office:annotation` with `office:annotation-end`, and
  point comments carry no name at all.
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
  the tracked-changes panel (`S`) is a second `SideList` instance.
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
- Multi-key motions. `pending_find` (`f`/`F`/`t`/`T`),
  `pending_object` (`a`/`i`), `pending_g` and `pending_z` hold the
  first key. `ReviewApp.on_key` sets them, but `resolve_pending()`
  consumes the second key and is called from `DocumentView.on_key`,
  not from `ReviewApp.on_key`. This matters. `event.stop()` in the
  app's `on_key` does NOT stop the app's own BINDINGS, so `fs` and
  `as` both jumped and toggled the sidebar. Only stopping the event on
  the focused widget keeps it from reaching them. Any future
  multi-key sequence has to resolve there too. A pending prefix
  swallows exactly one key whether or not it completes.
- `f`/`t` search the paragraph, not the display line, because the wrap
  moves with the pane width. `last_find` backs `;`/`,`, reversed
  through `FIND_REVERSE`.
- Text objects (`iw aw is as ip ap`) go through `_text_object`, which
  sets a visual selection rather than driving an operator, and always
  sets both ends even in visual mode. That is deliberately unlike vim,
  which extends the selection forward only. Selecting from the cursor
  onwards is already `vf.` or `v)`. `sentence_spans` is vim's rule,
  `.!?` plus closing brackets or quotes, then whitespace or end of
  paragraph. It returns `(start, body_end, end)`, the third field
  being what separates `as` from `is`.
- Reference overlay. `r` reads `reference-check.md` (found beside the
  document, up to `SEARCH_PARENTS` levels up, or `--refcheck` /
  `$SIDENOTE_REFCHECK`) and shows the table rows citing the key under
  the cursor. `refcheck.parse_entries` identifies columns by header
  name, not position, and indexes a row under every key it names, so
  the file's own layout can drift. The file is re-read on every `r`,
  it is small and hand-edited during review. `citations_at` returns
  the whole bracket group so `[@a; @b]` steps with `n`/`N`, the key
  under the cursor first. `o` shells out to `xdg-open`
  (`$SIDENOTE_PDF_VIEWER`) detached, PDFs named `<key>.pdf` in the
  directory the check file documents. Do not name a `ModalScreen`
  method `_render`, that is Textual's own and overriding it breaks
  painting with a bare `AttributeError` in the compositor.
- The panels are `s` (comments) and `S` (changes). They moved off
  `t`/`T` when those became the till motions, do not move them back.
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
- `docs/screenshot.svg` is a `save_screenshot()` export with the fake
  macOS window chrome stripped. Regenerate it with
  `.venv/bin/python tools/make_screenshot.py`, which rebuilds the
  sample document, adds the two pictured comments with a fixed date so
  the diff stays small, drives the TUI to the captured state, and does
  the stripping. The picture shows the footer, so any key change dates
  it. `strip_chrome` deletes the rounded background rect, the title
  `text`, the three traffic-light `circle`s and the `-title` style
  rule, sets the terminal group to `translate(0, 0)`, and measures the
  content box off the SVG itself (width from the per-line clip rects,
  height from the painted cell rects, which unlike the clip rects
  include the footer row). 1464x734 for 30 rows at 120 columns. The
  capture is 120 wide because the footer stopped fitting at 110 when
  `r` was added, and a truncated footer in the picture looks broken.
