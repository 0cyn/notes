# Retained rendering and responsiveness

Notes favors retained RAM over repeated Python/Qt work. The source document remains
the editing/undo authority; the presentation is a prepared, indexed display list.

## Cold opening and tab selection

Opening a collection creates lightweight source records. Only the persisted open-note
list gets deferred tab pages; without saved state, just the first note opens. Only
the selected page constructs an editor, document and highlighter. Its header
is installed immediately; remaining saved headers are restored through the shared
GUI preparation queue, preserving the active page. Saving, permission changes,
search snapshots and closed-note navigation indexes operate on cached source strings.
They do not instantiate hidden editors or documents.

The selected note's source is editable immediately. Initial Markdown interpretation,
syntax lexing, table normalization and cell HTML preparation run in a persistent
helper **process**, not a Python parsing thread. Threaded parsing still competes for
the GUI's GIL; the large-document stress case exposed pauses even while the GUI's own
callbacks used little CPU time. The helper accepts source strings and returns data
over private pipes. It has no Qt or Binary Ninja dependency. In Binary Ninja it runs the
Python that Binary Ninja installs plugin dependencies with (its Python Path Override, else the
interpreter matching Python Interpreter or the bundled Python, with Binary Ninja's environment);
outside it, the current interpreter. If the helper can't start, fails, or lacks the plugin's
packages, preparation runs in the GUI process on the same background thread instead.

Responses are matched to their source snapshot; a late result cannot overwrite text
typed while preparation was pending. Superseded queued requests are canceled.
Prepared cell HTML has link slots that bind only to successfully resolved in-view
targets on the GUI thread. Unresolved links keep their original rendered labels.
Both light/dark variants are retained, avoiding parser imports during cold table display.

Cold document geometry uses a sparse paragraph-height prefix index. Offscreen prose
starts with an estimate; visible paragraphs are measured first. Updating a measured
height or finding a paragraph at a scroll position takes logarithmic work, rather
than rescanning every paragraph. Embeds use their known concrete height. Source-based
viewport anchors preserve the visible location as offscreen text is measured.

Formatting, paragraph preparation, native tab restoration, table measurements and
cell preparation share a GUI queue with a roughly 3 ms budget per turn. Individual
tasks yield between paragraphs/cells. Hidden views pause their presentation work.
Visible tables create their widgets on demand; native size-hint queries reuse model
measurements instead of constructing new documents. Cached cell documents are Qt-owned
so their timers/layouts are destroyed on the GUI thread.

## Frame path

1. Pixel scrollbars determine a document-space viewport rectangle.
2. Vertical interval indexes select visible paragraphs, embeds, code frames and tables.
   They use prefix maximum bottoms, so a tall block that starts far above the viewport
   remains visible and overlapping intervals are handled correctly.
3. QPainter replays retained drawing tiles. A warm tile is a `QImage` blit at the
   current device-pixel ratio. A cold tile immediately replays its already-recorded
   `QPicture`, without shaping text or waiting for rasterization.
4. Native text selection/caret, quote rules, hover, and sticky embed titles are
   painted as dynamic overlays. Selected paragraphs use their existing `QTextLayout`
   to preserve Qt's native selection and input behavior.

`common/retained.py` records sparse 1024 × 256 logical-pixel tiles during preparation.
Each tile keeps separate raster images for each device scale encountered. Its actual
allocation is cropped to occupied bounds; blank source space around an AST does not
require an enormous bitmap. QPictures used as native-document paint devices have an
explicit size: otherwise Qt can cull all text against a zero-sized device.

A shared GUI-thread raster queue works in roughly 3 ms event-loop slices (yielding
between tiles). It holds weak references and does not trigger continuous repaints.
The vector fallback supplies complete content while the raster queue catches up.

## Preparation and invalidation

- `PixelDocumentLayout` retains paragraph measurements and drawings. A paragraph key
  includes its text, formats, font, wrapping, palette and local presentation geometry.
  Edits rebuild affected paragraphs and update document offsets; unchanged paragraphs
  retain their shaped text and pictures even when their source positions move.
- Incremental application of parsed formats
  avoids `QSyntaxHighlighter.rehighlight()` for unchanged paragraphs, because Qt clears
  their text layouts even when their highlighting has not changed. Formatting updates
  are batched, coalescing layout notifications without adding source edits or undo steps.
- The main editor prepares code-frame/table bounds outside paint events. Painting does
  not walk every code block or reposition every table. Quote marker selections cover
  only visible source paragraphs rather than the entire note.
- Large HLIL layouts (over 128 native rows) are prepared by a background worker.
  It owns independent `QTextLayout`/`QPicture` value objects, using copied fonts,
  immutable HLIL snapshots and captured theme colors. They never access live editors,
  source documents, native views or theme/settings repositories. Results are delivered
  to the GUI thread; deleted owners are ignored. Preparation yields cooperatively
  to limit GIL contention. Shared futures survive one subscriber closing; closing or
  rebinding their owning provider cancels queued and running preparation cooperatively.
- Existing projections remain visible while replacements are prepared. First-time
  projections have a small preparation status until their layout is available.
- Table cells are resolved, measured and recorded before painting. Table paint only
  looks up prepared cells and draws their cached text plus dynamic embed overlays.
  Generated cell documents import a one-character object marker for HLIL, rather than
  importing thousands of HTML spans and immediately deleting them again.
- Provider caches share prepared large layouts across matching embeds/notes. Unchanged
  HLIL snapshots retain identity; derived token sets/text and hashes are cached. Font,
  width, labels, address visibility, theme and snapshot contents are cache dependencies.
  File rebinding clears the provider's prepared-layout cache.

## Background tabs

Hidden notes retain their projections, paragraph drawings and cell variants. Their
presentation/refresh timers stop. A single application focus observer dispatches only
to the old/new focused widget's editor ancestors, rather than refreshing every note.

Only visible links request periodic HLIL refreshes or symbol re-resolution. Background
table metrics are marked dirty and deferred until the note becomes visible. Completed
HLIL lookups invalidate tables referencing the changed URL, rather than every table
in every open note. Hot painting paths use synchronized preference values, not native
settings repository queries.

Native editing can still demand one paragraph's geometry immediately. Small active
edits retain the synchronous command/undo path; initial saved-note preparation does
not wait for the whole collection or document. Preparation budgets are cooperative,
not hard real-time guarantees for individual Qt/native calls.

## Verification and repeatable measurement

the test suite checks no preparation/resolution during repaint,
bounded scroll work, reuse after edits, hidden-tab inactivity, worker ownership and
delivery, and exact raster-vs-native text pixels at 1× and 2× scale. Existing interaction
tests continue to cover source preservation, undo, selection, navigation and reflow.
`tests/test_notebook.py` checks lazy opening, file switches during header restoration,
typing before parsing finishes, process isolation, cell-template link binding,
incremental height indexing and large-embed navigation/reveal after a cold load.

Measure **startup** separately from visiting/preparing every tab:

```sh
.venv/bin/python tools/benchmark_startup.py --notes 100 --paragraphs 30
.venv/bin/python tools/benchmark_startup.py --notes 100 --paragraphs 500 --switches 2
.venv/bin/python tools/benchmark_startup.py --notes 100 --paragraphs 8 --mode tables
```

This reports constructor time, readiness for input, first content paint, event-loop
heartbeat gaps and individual cold-tab selection times. It models an already-running
Qt host by warming platform font discovery, but does not pre-open Notes or warm its
parser/documents. Optional `--capture PATH` saves first/final offscreen images; omit
captures for timing runs. In-memory fixtures exclude native BNDB/analysis loading.

The steady rendering benchmark remains independent of tests and the host bridge:

```sh
.venv/bin/python tools/benchmark_rendering.py --notes 30 --paragraphs 30 --frames 120
.venv/bin/python tools/benchmark_rendering.py --notes 100 --paragraphs 30 --frames 120
.venv/bin/python tools/benchmark_rendering.py --mode hlil --notes 30 --paragraphs 8
.venv/bin/python tools/benchmark_rendering.py --mode tables --notes 30 --paragraphs 8
```

`--profile PATH` records cProfile data. Its `visit_all_tabs_seconds` deliberately includes
visiting every tab and waiting for the active note's preparation; it is not startup
latency. `--capture PATH` optionally saves a standalone,
offscreen visual fixture. The measured cycle includes scrollbar movement, event-loop
processing and an explicit repaint; it is not a display refresh-rate measurement.
Compare runs with the same profiling option. Fixtures use in-memory metadata and,
in HLIL modes, immutable synthetic 350-row snapshots; they do not measure native
analysis or BNDB loading time.

Current cold-opening measurements on the development machine (100 saved notes):

| Fixture | Constructor | Ready for input | First content paint | Cold tab median / max |
| --- | ---: | ---: | ---: | ---: |
| Prose, 30 paragraph groups/note | 4.19 ms | 12.24 ms | 20.31 ms | 2.62 / 3.84 ms |
| Prose, 500 paragraph groups/note | 9.36 ms | 19.58 ms | 33.02 ms | 5.02 / 5.24 ms |
| Tables with 350-row HLIL snapshots | 4.07 ms | 12.88 ms | 20.16 ms | 3.98 / 4.82 ms |

The larger prose fixture's event-loop heartbeat was 6.81 ms at p95 and 25.98 ms
maximum, including opening and tab switches. Source appears first; formatting and
embeds finish asynchronously. These are synthetic local measurements, not a guarantee
for every native theme, document or host workload.
