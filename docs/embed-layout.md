# Embed geometry and interaction

The main Markdown editor uses `PixelTextEdit` (`QTextEdit`) with
`PixelDocumentLayout`. The source is still the same shared `QTextDocument`, with
native cursors and undo. Highlighting hides the link's source glyphs and reserves
its width; the document layout reserves the embed's actual height.

`QPlainTextEdit` scrolls by visual source lines. Increasing a source line's bounding
rectangle cannot make its scrollbar traverse a 5,000-pixel embed. The pixel layout
therefore supplies absolute paragraph rectangles, document dimensions, painting and
hit testing through Qt's document-layout API. Cell editors fit their full source
inside an already pixel-scrolling table and keep their plain-text layout.

Each paragraph's `QTextLayout.position()` is in document coordinates. Painting must
not add that origin a second time; native cursor rectangles and painted glyphs must
stay aligned. the test suite inspects actual glyph pixels at the hit
rectangle after scrolling to protect this contract.

## Projection changes

Layout changes preserve a source-backed viewport anchor per editor. As with
browser scroll anchoring and CodeMirror, the anchor is the line at the top of the
viewport, not the caret: an embed expanding or collapsing below it pushes later
text down without scrolling, and only size changes above it move the scrollbar.
The anchor is re-captured when the view scrolls, never merely because the caret
moved. After keyboard input, a reflow that pushes the caret out of view scrolls
just far enough to show it again, until the next wheel, mouse or scrollbar input.

Projections never change under a held mouse button. While an ordinary press places
the caret or drags a selection, embeds keep their current form; the projection is
applied after release. Otherwise leaving a revealed link would re-expand its embed
mid-press, slide the text under the pointer and extend the selection to it.

Clicking an embed to edit it pins the text around it instead: the lines above it
when its top is on screen, otherwise the lines below it, otherwise (an embed
taller than the view) its source under the pointer. Cell editors apply the same
choice through their table and outer note scroll areas after queued row-height
reflow.

## Follow gestures

Editors and tables use keyboard focus policy and explicitly focus ordinary clicks.
A Cmd/Ctrl follow click is handled before focus or selection changes, consuming the
press, motion and release. Cursor/anchor, preferred column and scrollbar snapshots
cover synchronous navigation and queued echoes. A new input gesture cancels pending
restoration, and an actual source edit prevents stale state from being restored.

A right-click on an embed never moves the caret, so the embed stays projected while
its menu is open. `ui/links/embed_editing.py` builds the embed commands; their source
rewrites (`src/markdown.py`) use the label, destination and title ranges the parser
records on each `MarkdownLink`, and run as one source edit. **Set title** pins the
view around the embed before editing, exactly like a reveal click. An active cell
editor keeps its right presses: passed on, the table view would take focus and
close the editor before the menu opened.

## Labels

`CodePreviewLayout` gives a labelled embed a one-line title strip above its code and
function signature; an unlabelled embed has none. The strip's height comes from the
label font alone, so it is reserved before the code is laid out and only moves the
code down: wrapping, addresses and the signature are unchanged. The embed widens
to the title's natural width, capped at the available width, and longer titles are
elided. The strip is opaque. When the embed's top is scrolled out of the visible
area (including parent clipping in table cells), the strip sticks to the visible
top until the embed's bottom pushes it up, hiding the rows passing under it. Source
Markdown and copy operations retain the original label.

An accent spine runs down the frame's inner left edge, one filled shape whose width
says what it is beside: full (`SPINE_WIDTH`) beside code, half beside a function
signature header, and none beside the link's title. Width changes ease across two
quadratic curves starting `SPINE_EASE` before a section boundary, so the edge
leaves and rejoins the vertical smoothly and the spine grows out of the title.
A stuck title keeps its zero width, so the spine curves in below it.

Scrolling invalidates the overlay region instead of repainting only the exposed
bottom strip. Code rows and address rows are indexed during preparation and recorded
into retained tiles; repainting visits only visible tiles, even for very large blocks.
See [retained rendering](rendering-performance.md) for caches, worker preparation,
invalidation and performance measurements.

## Block alignment

Block-level content follows the box-alignment convention of Notion, Obsidian and
GitHub: every block's box starts on the paragraph's text column, and its content
starts `BLOCK_INSET` inside the box (`ui/common/appearance.py`). The column is the
document margin (`DOCUMENT_MARGIN`) plus any quote indentation.

- Fenced code frames span the column. `MarkdownDocumentLayout` re-wraps code lines
  `BLOCK_INSET` in from both sides, using the same per-line positions as quote
  indentation, so the source, caret and hit testing stay native.
- Embeds are laid out with `CODE_PADDING_X = BLOCK_INSET` and may be as wide as the
  column.
- A table card puts its grid on the column. Its left rail (`LEFT_RAIL`, the 24px
  document margin) holds the 18px row handles and table menu in the margin. Its right
  rail, the same width, holds the vertical scroll indicator and add-column control in
  the right margin, so a table wider than the column ends on the column's right edge.
- Below a table the layout leaves one prose line blank (`TABLE_MARGIN` plus
  `table_exit_height`), unless a plain text or blank line follows directly (including
  the note's final line, or the blank line between adjacent tables): that line is
  already somewhere to click and type (`MarkdownDocumentLayout.table_room`). Headings,
  quotes, code, rules and embeds keep the room.
  A click in the room starts a paragraph after the table, opening an empty line when
  the table runs straight into other content such as a code fence.

## Tables and the note caret

A table has a current cell only while focus is inside it. When focus leaves (to the
note, a dialog, another window) its selection is cleared, so an unfocused table looks
and behaves like any other block. While a cell has focus, the note's caret sits
(undrawn) at the start of that cell's Markdown source. Focus returning to the table
without choosing a cell, after a window switch for example, resumes at that caret.
The grid consumes every navigation key: Up from the first row and Down from the last
leave the table like prose lines do, and keys with nowhere to go never fall through to
the editor underneath, which would move a caret the user cannot see.

## GUI lifetime

Notebook sessions and their documents/highlighters are owned by Qt. Surface and
note-view leases schedule final deletion on the GUI thread after the last user
of a session is gone. Python garbage collection can run on an HLIL worker while
allocating a large snapshot; it must not destroy GUI timers there. The layout
cache stores source positions, not long-lived `QTextBlock` wrappers.

Behavioral coverage is in `tests/test_embeds.py`, the test suite,
and the test suite. Visual inspection uses the separate agent
bridge with offscreen fixtures.
