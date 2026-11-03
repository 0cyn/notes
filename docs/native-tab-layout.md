# Native note-tab alignment

Verified in Binary Ninja Ultimate 6.1.10832-dev on macOS, 2026-10-18.

## Horizontal inset

Each embedded `NotepadWidget` uses an 8 px content margin, but the native
`DockableTabBarWithCornerWidget` header defaults to 2 px left/right margins.
`NativeNoteTabs._configure_containers()` sets the header's horizontal margins to
the same `NOTE_CONTENT_MARGIN` used by the page. This aligns the actual tab bar
with the darker editor region in each split pane and in the sidebar.
Embedded note pages have no top content margin, so the editor's dark background
starts at the bottom of the tab header, directly against the native underline.
Standalone note components retain their normal top inset above their own toolbar.

## Unequal top spacing after splitting

`SplitTabWidget` gives its single corner widget to one leaf. The native headers
calculate their heights independently and bottom-align the tab bar. Our 28 px
New Note/menu controls, plus the native corner container's 2 px top inset, make
that leaf's header taller than a bare tab bar:

| Native configuration | Left header / tab top | Right header / tab top |
| --- | --- | --- |
| Two panes, no corner widget | 23 px / 0 px | 23 px / 0 px |
| Same panes, 28 px corner widget | 23 px / 0 px | 30 px / 7 px |

The original isolated native-widget investigation demonstrated a split-header
sizing inconsistency when only one pane has taller corner controls. Use the
`tools.agent_ui preview tabs` scene for current visual inspection.

The local workaround is in `src/ui/notebook.py`:

- Measure each header layout's natural `sizeHint().height()`.
- Give every header in that Notepad's split tree the largest minimum height.
- Reconfigure on native `layoutChanged` and after adding tabs/corner controls.
- Observe header `LayoutRequest` events to update when controls or fonts change.

With the current theme, all headers are 30 px and all tab bars have a 7 px top
offset. Native bottom alignment is preserved, and adjacent editors begin at the
same height. The height is measured, not a hard-coded 7 px pad. Reading layout
hints rather than the imposed widget minimum lets the headers shrink again.

The workaround is scoped to Notepad's native tab containers.

## Per-pane New Note buttons

The hamburger is the notebook's single native corner widget. Each
`DockableTabWidget` header owns a separate New Note button, inserted immediately
after its tab bar, followed by an expanding spacer. The bar uses a maximum
horizontal size policy so it fits its tabs while retaining native overflow
scrolling. The spacer consumes the remaining width and holds the hamburger at
the far right of whichever pane currently owns it.

This arrangement is reapplied on `layoutChanged`: native splitting can reset the
bar/corner stretch settings. Previously, New Note and the hamburger were bundled
in one corner widget; splitting both moved the only New Note button into one pane
and allowed the tab bar to expand, pushing New Note against the hamburger.

The button emits its owning container through `newNoteRequested`. `NotesWidget`
passes that target to `NativeNoteTabs.addTab`, so clicking a pane's + inserts and
selects the new note in that pane even when another pane was active. Enabled
state applies to all pane buttons, including newly created split headers.

The click handler resolves the button's owner from the current widget tree.
Native layout restoration can retain a header while replacing its original
`DockableTabWidget`; capturing that original container in a callback leaves a
deleted Qt wrapper and causes new notes to be created without visible tabs.
`tests/test_home.py` exercises header reparenting with both a surviving and
a destroyed original owner.

Native UI tests check each pane's creation target and recovery after transfer.
Use an agent tab preview to inspect long titles, nested splits, and alignment.

## Settings and other auxiliary tabs

The Settings page is tracked separately from note sessions. It uses the same tab
adapter and can be moved, split, and closed, while note-only actions are disabled
when it is selected. Closing it releases only the page; editor preferences are
user-level settings shared by all notebooks.

Native split closure can emit `currentChanged(None)` while another leaf still
has a selected tab. The adapter checks the live leaf selections before exposing
its current page and falls back to a surviving tab. This keeps note actions and
focus usable when the Settings pane is closed.

## Tab context menus

Right-click a note tab for New Note, Duplicate Note, Copy Note as
Markdown, Find in Note, closing actions, native splitting, and Delete Note.
Opening the menu targets the tab under the pointer without selecting it or loading
a deferred editor. Keyboard context-menu events target the selected tab.

Close Other Tabs, Close Tabs to the Left/Right, and Close All Tabs apply to the
clicked pane, in its current visual order. Settings and the Notes home page participate in
closing/splitting but do not offer note-only actions. Closing retains saved notes
and undo history; Delete Note uses the existing cross-surface deletion confirmation,
which identifies the target note. Bulk closes run in short batches and cancel
pending header restoration for the affected notes.

New and Duplicate place their result in the clicked pane. Duplicate copies the
exact Markdown into a new note with its own undo history and the current default
new-note MCP permission. Tab titles continue to follow the note's first H1 heading.

Split to the Right / Split Below move the clicked tab into a new native pane. They
are enabled when its source pane has another tab to leave behind. Tab/page and pane
ownership are reacquired when actions run, so a menu cannot accidentally act on a
different file or a stale pane after a transfer.

`tests/test_notebook.py` covers targeting, reordering, scoped closing, lazy pages,
shared undo, duplication, deletion and stale actions. `tests/test_home.py`
covers native viewport hit routing. The host UI suite exercises the same context
menu across real native split panes.

## Persisted open notes

Open-note IDs, their order, and the last selected note are saved through the
[per-view QSettings store](ui-state.md). A file without saved UI state opens only
its first note, or a fresh draft if its collection is empty. An explicitly empty
open-tab list stays empty. Saved notes outside the list remain available in the
hamburger menu and the home page.

Restored inactive notes retain deferred editors. The selected tab is installed
first for responsiveness; pending headers are inserted into their saved positions.
Reordering and native split transfers update the saved list. Sidebar and view/pane
surfaces keep separate state; open surfaces remain independently selectable.

## Stale width after dragging between panes

The native drag placeholder temporarily increases a tab bar's size hint. After a
drop the hint can shrink without invalidating the parent layout's cached width.
One observed bar remained 177 px wide even though its two tabs and current size
hint totaled 117 px, leaving a 60 px hole before New Note.

`NativeNoteTabs` coalesces a deferred geometry refresh after configuration and
mouse release/ungrab events on the native bar/viewport. On the next event-loop
turn, after native placeholder removal, it calls `bar.updateGeometry()` and
invalidates the header layout. Qt then sizes the bar from the final hint while
preserving native overflow scrolling. The refresh timer belongs to the adapter,
so it is destroyed with the notebook.

Validation includes native cross-pane reparenting in the UI tests and real
macOS pointer drags in the live UI. A clean application restart is used for
changes to these Qt event filters; replacing a running Python virtual callback
caused a PySide/Python callback crash during development.
