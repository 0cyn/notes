# Sidebar layout investigation

Investigated on 2026-10-18 in Binary Ninja Ultimate 6.1.10832-dev on macOS.
Status: reproduced and traced; implementation work is described below.

## Reproduction

The initial live file had an empty note, which did not expose wrapping problems.
An in-app probe created two native `NotesWidget` instances sharing an in-memory
metadata view: a wide main editor and a compact sidebar. It populated ordinary
prose, a blockquote, a table, and several long-titled notes. The probe used the
initialized Binary Ninja UI and its real `SplitTabWidget` bindings.

The local probe and results are in `.test-artifacts/ui-automation/`:

- `probe_sidebar.py`: in-app reproduction, with cleanup of its temporary dialog.
- `native-sidebar-report.json`: measured geometry and tab-scroll state.
- `native-both-visible-sidebar.png`: clipped paragraphs and quote text.
- `native-main-hidden-sidebar.png`: clipping persists with the main editor hidden.
- `native-short-sidebar-main.png`: a short sidebar also collapses the main table.
- `native-many-tabs-sidebar.png`: the active tab is outside the visible strip.
- `native-many-tabs-ensured-sidebar.png`: the native ensure-visible API corrects it.

## 1. Shared document layout breaks independent wrapping

With both native editors visible, the main text viewport was 636 logical pixels
wide and the sidebar's was 290. Both used paragraph lines 604 pixels wide. The
sidebar therefore acquired a 340-pixel horizontal scroll range for ordinary prose.

Hiding the main widget retained those same paragraph widths. Narrowing the sidebar
to a 230-pixel viewport still left its paragraph lines at 604 pixels and increased
the horizontal scroll range to 400 pixels. Resizing the sidebar does not fix it.

An independent offscreen Qt reproduction showed the same issue: a 304-pixel
sidebar viewport inherited 768-pixel paragraph lines from a wider editor.

### Cause

- `src/ui/session.py`: `NoteSession` owns one `QTextDocument` and one
  `MarkdownDocumentLayout` for the note.
- `src/ui/editor.py`, `MarkdownEditor.bind_session`: every surface installs that
  exact document using `setDocument(session.document)`.
- Wrapping, `QTextBlock` layouts, line positions, visibility, and custom spacing
  state consequently belong to a shared document. Each editor cannot independently
  lay it out to its own viewport width.

The effect extends beyond prose: wrapped quotes, code-frame geometry, table source
folding, cursor geometry, and vertical scroll positions all depend on those lines.

## 2. Table viewport limits leak between surfaces

In the native probe, a 13-row table initially displayed at 472 pixels high.
Reducing the compact widget to 300 pixels high produced a sidebar text viewport
of 183 pixels. The table's display height then became **105 pixels in both views**,
even though the main text viewport remained 610 pixels high.

### Cause

- `src/ui/tables.py`, `TableRegistry.height_limit`, chooses the minimum of all
  registered editor height limits.
- `TableModel.display_height` uses that one shared limit.
- `TableRegistry._geometry_changed` installs that height in the shared session
  document layout.
- `MarkdownEditor._table_rect` and its table cards consume the shared height.

Active cell-editor heights are likewise held in the shared table model. Source
values and column widths can be shared intentionally; available viewport space
and temporary editor geometry need per-surface ownership.

## 3. A renamed active tab can be completely offscreen

With five notes and long H1-derived titles in a 320-pixel compact widget:

```text
Current tab:       A long investigation title 4 (index 4)
Tab viewport:      248 pixels wide
Current tab rect:  x=510, width=168
Horizontal scroll: 83 of 513
```

The editor showed note 4 while its selected tab was outside the visible strip.
The visible tabs were earlier notes, making the active-note state misleading.

Calling the native `DockableTabBar.ensureCurrentTabVisible()` changed the scroll
position to 513 and moved the selected tab to x=80, fully inside the viewport.

### Cause and localized fix

`src/ui/notebook.py`, `NativeNoteTabs.setTabText`, updates labels and tooltips
without ensuring that the current tab remains visible after its width changes.

Schedule an ensure-visible pass after title and tab-layout changes, when corner
controls and tab widths have settled. Cover initial selection, long-title updates,
resizing, split containers, and reopening from the saved-note menu.

## Required ownership change

Keep a canonical note source and undo history in `NoteSession`, and give each
editor its own presentation document/layout:

```text
Shared NoteSession
  Markdown source, note identity, persistence, undo/redo, parser analysis
  Table source models and intentionally shared column-width preferences
      |
      +-- main editor presentation document/layout and table viewport geometry
      +-- sidebar presentation document/layout and table viewport geometry
      +-- pane editor presentation document/layout and table viewport geometry
```

Per-editor document changes must be routed back to the canonical source as
UTF-16-positioned edits. Canonical changes must propagate to the other documents
without feedback loops or duplicate metadata writes. Use incremental cursor-based
updates rather than resetting the entire document on each keystroke, so selections,
cursor positions, and active cell editing survive synchronization. Undo/redo in
every surface must route to the canonical history.

Move wrapping, block visibility, spacing caches, table display-height limits, and
active cell-editor geometry into that per-editor presentation layer. Keep parsed
source ranges, table values, alignment, and persistence note-scoped.

Forcing the shared layout to the narrowest width would make the main view wrap to
the sidebar's width. It would leave the underlying ownership problem in place.

## Regression coverage needed

The existing suite covers shared edits/undo and individual-view wrapping, but does
not check simultaneous unequal-width layouts. Some tests explicitly assert shared
`QTextDocument` identity; those assertions need to become behavioral checks for
shared source and undo with independent layout.

1. Show the same note simultaneously at wide and narrow widths. Assert prose and
   quotes fit each editor, with different wrap counts where appropriate.
2. Resize, hide, close, and reopen either surface. Verify the other's wrapping,
   cursor hit testing, and scroll behavior remain correct.
3. Show the same tall table in a short sidebar and tall main view. Assert independent
   display heights, including active multiline cell editing.
4. Type and format in either view, undo in the other, and save immediately. Assert
   exact synchronized Markdown and a single shared undo operation.
5. Exercise UTF-16 ranges, IME input, table separator repair, and selections across
   synchronized presentation documents.
6. During an agent UI inspection, rename the selected tab to a long title, narrow
   its container, and reopen it from the menu. Assert its tab rectangle is visible.
