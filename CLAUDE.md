# sidenote — quick reference

Terminal reviewer for docx, ODT and Markdown files with character-level
comments. docx comments are native Word comments spliced into the OOXML,
ODT comments are native ODF inline annotations, Markdown comments live in
a sidecar JSON file. See README.md for usage and keys.

## Commands

```sh
.venv/bin/python -m pytest tests/ -q      # run tests (no LibreOffice needed)
.venv/bin/sidenote sample /tmp/demo.odt # make a test document
.venv/bin/sidenote sample /tmp/demo.docx # the Word equivalent
.venv/bin/sidenote sample /tmp/demo.md  # the markdown equivalent
.venv/bin/sidenote /tmp/demo.odt        # open the TUI
.venv/bin/sidenote check /tmp/demo.md   # orphan report, nonzero on any
.venv/bin/sidenote /tmp/paper.md --refcheck ref-check.md  # r overlay
.venv/bin/sidenote /tmp/demo.md --theme word  # black on white
```

## Design notes

- Positions are `(paragraph_index, character_offset)` into the plain text
  from `para_texts()`. Annotations are zero-width in that text,
  so existing comments never shift offsets.
- `engine.open_review()` returns `OdtReview`, `DocxReview` or
  `MarkdownReview` by file suffix. All three expose the same interface
  (`para_texts`, `comments`, `add_comment`, `update_comment`,
  `delete_comment`, `changes`, `save`), so no caller branches on
  format. Add a format by adding a class with that
  interface, not by special-casing the TUI.
- `engine._insert_at` splits text nodes and `text:s` runs at the offset.
  Ranged comments insert the `office:annotation-end` marker before the
  start element so the second insertion cannot invalidate the first.
- odfpy quirk. `Annotation(name=...)` sets `draw:name`, which is wrong for
  ranged comments. Use `setAttrNS(OFFICENS, "name", ...)` instead.
  `AnnotationEnd(name=...)` maps correctly to `office:name`.
- Point comments (start == end) get no `office:name` and no end marker,
  matching how LibreOffice writes them.
- `OdtReview.update_comment` replaces the annotation's `text:p` children
  and refreshes `dc:date`.
- Nothing converts between formats and nothing shells out. `soffice`,
  `_soffice_convert`, `docx_to_odt`, `export_docx` and the `X` key are
  all gone, removed once `DocxReview` landed and the ODT round trip had
  no reason left to exist. Do not bring any of it back. Measured on the
  2021-011 manuscript, that round trip halved `document.xml`, dropped
  `commentsExtended`, `commentsIds`, `commentsExtensible`, `people`,
  `footnotes`, `endnotes` and `webSettings`, and turned two headers into
  eight headers and eight footers. `DocxReview` changes two parts and
  leaves the other 34 byte-identical, which
  `test_a_real_word_file_keeps_its_parts` asserts against that same file
  when it is present. Handing an ODT to a Word user is LibreOffice's
  job, not sidenote's.
- `docx.py`. One `_Walker` pass over `document.xml` bytes produces
  paragraphs, runs, comment marks and tracked changes. Regex over XML
  is the deliberate choice, not laziness. An ElementTree round trip
  rewrites namespace prefixes and reorders attributes across the whole
  part, which is exactly the byte churn the engine exists to avoid.
  `_TAG` steps over quoted attribute values rather than scanning to
  the first `>`, because a `>` inside an attribute value is legal.
- Every direct child of a `w:r` is recorded in `_Run.children`, with
  `text=None` for anything zero-width. That is what lets `_rebuild`
  re-emit a split run from the original bytes and keep a drawing, a
  field char or a footnote reference that happened to sit in it. Only
  the one `w:t` actually being cut is rebuilt.
- A comment marker is a run-level element, so it cannot go inside a
  run. `_para_edits` therefore sorts each wanted offset into one of
  two cases, between runs (a plain insertion) or inside one (split the
  run around it). The `at_byte.pop(run.start, ...)` line is not
  optional. Two comments can want the same byte, one ending at a run's
  first character and one starting inside it, and two edits at the
  same position would overlap.
- Skipping. `_BLOCK_SKIP` holds property subtrees, which is what keeps
  a `w:tab` in `w:pPr/w:tabs` out of the text and a `w:ins` marking an
  inserted paragraph mark out of `changes()`. Drawings, pictures and
  embedded objects are skipped as opaque run children, which is why a
  text box's paragraphs are not document paragraphs. `w:delText` is
  zero-width in the flat text and feeds `changes()` instead, matching
  how ODT keeps deletions in metadata.
- Word comment ids are integers and are the structural key, like ODT's
  `office:name`. They are never renumbered. Markdown's positional
  renaming is a Markdown thing, do not bring it here.
- `_ensure_comments_part` handles three states, not two. The part can
  be missing, present and appendable, or present as an empty
  self-closing `<w:comments/>` root with no closing tag to append to.
  pandoc writes that third one and it is what `manuscript_test.docx`
  ships. `_open_empty_root` reopens it, keeping the declarations
  because a comment body's markup resolves against them.
  `add_comment` resolves the `</w:comments>` position before it calls
  `_apply`, so a part it cannot append to leaves the body unmodified
  instead of stranding markers with no comment. `_open_empty_root`
  needs its `(?=[\s/>])` lookahead, `<w:comments` is a prefix of other
  tag names and without it the regex happily rewrote
  `<w:comments-but-not-really/>` into mismatched tags.
- The comment mutations in the TUI (`action_comment`,
  `action_edit_comment`, `action_delete_comment`, `_step`) each wrap
  the engine call and the save in one try and report through
  `_failed`. Textual turns an uncaught exception in a modal callback
  into a full-screen traceback over the review, which reads as a hang
  and loses the cursor, the selection and the search. Undo is only
  recorded after the save returns, so a failed mutation leaves no step
  to walk back.
- New comments carry no `w14:paraId`, so no row is written to
  `commentsExtended`, `commentsIds` or `commentsExtensible`. Absent is
  the defined "unthreaded, unresolved" case and Word fills it in on
  first reply. Writing one would mean holding a uniqueness invariant
  across every edit for no gain. Deleting a comment does prune rows
  keyed off the paraIds in the comment being removed, so a stale row
  cannot outlive its comment. Confirmed in Word for the web on
  2026-08-26, on a pandoc-produced docx whose comments part was an
  empty self-closing root. Desktop Word is still untried.
- Deleting a comment cuts its marker bytes out but does not re-join the
  run they split. Deciding two adjacent runs are interchangeable is
  Word's call on its own save, not this engine's, and the residue is
  137 bytes on a 489 KB `document.xml`. Do not add run merging to make
  a diff prettier.
- `save()` builds the new zip beside the target and `os.replace`s it,
  so an interrupted save leaves the document as it was. Untouched
  parts are copied with their original `ZipInfo`, which keeps their
  timestamps and compression.
- The TUI never manipulates XML. It maps display lines back to engine
  positions (`ReviewApp.lines`) and reloads the document after every
  mutation. Keep new frontends on the same engine API.
- Markdown. `markdown.split_blocks` makes paragraphs from blank-line
  separated blocks, fenced code kept whole, source shown as written
  rather than rendered so offsets match the file the user edits. The
  `.md` is opened read-only and never written, everything goes to
  `<stem>.sidenote.json`.
- Markdown styling (`M`). `markdown.markup_spans` returns
  `(start, end, kind)` over one block, kinds not colours, so the
  engine layer stays free of the palette. It styles the source where
  it stands and never removes or reorders a character. That is the
  whole design constraint. Concealing `**` would need a display
  column to source offset map for the cursor, the wrap, the search
  hits and every comment range, and the cursor would vanish when it
  landed on a hidden character. Dimming the markers costs none of
  that, so `M` is a `_repaint()` and not a `rebuild_lines()`, and a
  comment cannot tell which way it is set. Spans go first in
  `_para_styles` so a comment or the cursor paints over them, they
  are cached per paragraph in `_markup_cache` because a wrapped
  paragraph asks once per visible line per frame, and the cache is
  cleared by a reload or a theme change, not by a resize. Later
  spans win where they overlap, which is what puts a marker-grey `#`
  inside a blue heading line. `_INLINE` is ordered by priority and a
  match overlapping a claimed region is skipped, which is what keeps
  emphasis out of a code span and leaves `snake_case` alone.
- Themes. `theme.py` pairs a Textual `Theme` for the chrome with a
  `DocStyles` for the pane, because the pane paints its own marks and
  an underlined yellow comment is invisible on white. `styles_for`
  checks `STYLES_BY_NAME` first and falls back to `theme.dark`, so a
  theme sidenote ships gets marks matched to its exact background and
  every other one still gets a readable set. The fallback sets stay in
  ANSI colour names on purpose, an unknown theme should borrow the
  terminal's palette rather than a foreign one. `DARK_STYLES` is now
  only ever the fallback, do not tune it to any one background.
  `ReviewApp.watch_theme` reloads `doc_styles`, and it runs before the
  widgets exist on the first pass, hence the guard on `self.lines`.
  The sidebar is rebuilt there too, its items carry markup baked in at
  build time. `gruvbox-material` and `word` are registered in
  `__init__` but applied in `on_mount`, setting `theme` earlier
  refreshes the CSS of an app that has not started, and a class-level
  default has to name a theme Textual already knows, hence
  `BOOTSTRAP_THEME` (Textual's own `gruvbox`, the nearest thing it
  knows to the default) and `TEXTUAL_THEME` read into `wanted_theme`
  instead of into the reactive. The default palette is the nvim
  colorscheme at `background = hard`, `foreground = material`, lifted
  from `gruvbox_material#get_palette`, so it can be re-diffed against
  the plugin. Aptos is a terminal profile setting, no program can
  change the font from inside.
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
  element in the document. Markdown orphans when its stored context no
  longer matches. docx orphans only in the pathological case of a
  `w:comment` with no marker anywhere in the body, which is a broken
  file. It is surfaced rather than dropped so the comment can be read
  or deleted, and it is still written back on save either way.
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
- Reload (`R`). `action_reload` opens a second review before touching
  any state, so an unreadable or empty file leaves the review as it
  was, then hands it to `_reload_document`, the same path every
  mutation takes. The extra work is what a mutation does not need,
  the text can now be shorter (`_clamp_cursor`), a visual selection
  spans wording that may be gone, and `search_matches` are stale
  offsets. That last one is why searches keep `search_pattern` and not
  only `search_query`, `*` matches whole words where `/` matches
  anywhere, so re-running from the words alone would quietly change
  what a live search means. Reload does not save. Writing the sidecar
  would rebase every anchor onto the new text and make successive
  external edits survive better, but it turns a read key into a write
  and a mid-save truncation into data loss, so the anchors stay as
  stored until a comment change saves them. Empty is refused rather
  than shown because nothing in the offset math has a paragraph 0 to
  work with.
- Performance model. `DocumentView` is a line-API `ScrollView`, its
  `render_line` styles only visible rows. The wrap layout is cached in
  `ReviewApp.lines` and rebuilt only by `rebuild_lines()` (resize,
  reload, sidebar toggle via resize). Keystrokes go through
  `_repaint()` which is O(viewport). Do NOT reintroduce a full-document
  `RichText` build per keypress, that was ~180 ms/key on a
  400-paragraph document, `_repaint` is ~0.2 ms.
- Key handling. Movement lives in `ReviewApp.on_key` (guarded when a
  modal is up or the document pane is not focused, ends with
  `event.stop()`), actions live in BINDINGS. `e` is vim word-end. The
  display toggles `D` and `M` sit in `on_key`
  rather than in BINDINGS on purpose. A shown binding widens the
  footer, and the footer is in `docs/screenshot.svg` at the narrowest
  width that still fits it. Literal `[` in help text must be escaped as `\\[`,
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
- Undo. `u` and `ctrl+r` walk `ReviewApp.undo_stack`/`redo_stack` of
  `Edit(label, before, after)`, each side a `Snapshot` of one comment
  or None. Add is `(None, after)`, delete is `(before, None)`, edit is
  both, so `_restore(target, current)` serves both directions and only
  `_step` knows which way it is going. Snapshots cannot hold a
  `Comment` because every mutation reloads the document, so
  `_comment_matching` re-finds it by span plus text. A snapshot that
  no longer matches refuses the undo rather than guessing.
  `add_comment` takes an optional `date` on both engines purely so a
  restored comment keeps its original timestamp instead of being
  restamped as new. `update_comment` deliberately does not, undoing an
  edit is itself an edit. The stack is in-memory and per-session,
  which is why `d` also confirms, a stray keystroke you never noticed
  is not one you will undo.
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
  width tracks the footer, which stopped fitting at 110 when `r` was
  added and at 120 when `u` was, went to 130 and came back to 120 when
  the docx export key went, and a truncated footer in the picture looks
  broken. Check the footer after any binding change and rerun the tool,
  120 is the current minimum that still shows `? help`.
- Comment style. Document unevenly. A hard constraint or a decision
  that looks like a bug earns a paragraph, a helper whose name says
  what it does earns nothing. Do not add a docstring to every
  function, and do not restate the signature in prose. State a fact
  once, at the interface, and let the other engine point at it rather
  than repeating the sentence, `MarkdownReview.add_comment` refers to
  `OdtReview.add_comment` for exactly this reason. Rationale that runs
  past a couple of lines belongs in this file, not in the code. No
  dashed banner separators outside `tui.py`, the files are small
  enough to grep.
