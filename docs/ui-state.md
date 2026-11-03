# Persistent per-view UI state

`src/ui/common.py` wraps `QSettings` with a shared
`PersistentViewSettings` instance per QApplication. It uses the native user store
for organization **0cyn**, application **bnnotes**.

One QSettings entry, **`binaryViews`**, holds a native QVariantMap of view identities
to settings dictionaries. For example:

```text
binaryViews = {
    "file:<canonical-path-sha256>": {
        "notebook.view": {
            "open_notes": ["note-id-1", "note-id-3"],
            "active_note": "note-id-3"
        },
        "notebook.sidebar": {
            "open_notes": [],
            "active_note": ""
        },
        "another-feature": { ... }
    }
}
```

New features can add fields to a view dictionary. `get()`/`view()` return copies;
`set()` and `remove()` change only the requested field and preserve other fields.
QSettings' memory cache updates immediately. Disk synchronization is coalesced and
flushed when a notebook closes or is destroyed and when the application quits.

## Identity

For Notes, the owning file identifies the view: Raw and analyzed BinaryViews share
the same collection and UI-state identity.

- Project-backed views use the project's UUID and project-file UUID. Moving the
  project or renaming the project entry preserves the identity.
- Other named files use a hash of the canonical, platform-normalized backing path.
  Different databases with the same display name do not collide.
- Unnamed buffers use FileMetadata's session ID in an in-memory map only. Session
  numbers are not durable IDs and are never written to QSettings.

The identity is refreshed when saving UI state, so a saved/renamed backing file can
receive the current tab snapshot. Note text and permissions are stored in the BNDB;
this UI store records note IDs, not Markdown content.

## Notebook restoration

`src/ui/notebook.py` owns the notebook field schema. It stores the open-note
list and last selected note independently for `notebook.view` (views/panes) and
`notebook.sidebar`. Within one kind, the latest surface update supplies the next
restoration snapshot; already-open surfaces retain their independent selection.

- With no saved state, open only the first note, or a new draft for an empty file.
- Preserve a deliberately empty open-note list.
- Ignore deleted/missing IDs, remove duplicates, and retain saved order.
- Restore the active note first and add the other headers incrementally, without
  constructing their editors.
- Keep the last note selection when an auxiliary Settings/Find tab is selected.
- Skip writes for unreadable note metadata so an error view cannot overwrite a
  previously valid tab snapshot.

Close-all, individual closing, creation, duplication, navigation, reordering and
file switching update the snapshot. Closing the surface flushes it without treating
widget teardown as a request to close all saved tabs. Auxiliary-tab contents and
the split-pane topology are not part of the open-note list.

Incoming address sync is separately controlled by **Settings → Navigation → Follow
View Navigation** (`bnnotes.navigation.followView`, default off). When off, restoring
a BNDB's code location cannot reopen a matching closed note or move a note's caret
or scroll position. Enabling it permits matching across the whole collection,
including closed notes. Disabling it also cancels pending navigation waiting for a
note index. Explicit Cmd/Ctrl-click navigation from a note remains available.

Tests use private QSettings files. They verify native/INI map round trips in a
separate process, restoration with new file-session IDs, order and selection,
explicitly empty state, deleted IDs, project identity, surface independence,
Save As, parent destruction, and preservation of extension fields.
