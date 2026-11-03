# Root context-menu injection

Verified in Binary Ninja Ultimate 6.1.10832-dev on macOS, 2026-10-18.

`src/ui/host.py` provides an opt-in Qt hook for adding actions directly to
the root context menus of Linear View, Graph View, and the Symbols sidebar. The notebook
plugin uses it to install **Copy BNURL to Function** in the Symbols copy-actions
group after **Copy Raw Name** (falling back to **Copy Name**). This action copies
a file-qualified URL whose `expr` is the one selected function's name, or its
start address when it has no name. It is disabled
for non-function or multiple-address selections and is scoped to Symbols menus.
Linear and Graph also get **Copy BNURL to Address** and **Copy BNURL to Function**
after **Copy Address**, within the existing copy group. These use the popup's
native address/function context and produce file-qualified URLs.

These production commands are native **UIAction** registrations, implemented in
`src/ui/host.py`, with stable identifiers:

```text
Notepad\Copy BNURL to Address
Notepad\Copy BNURL to Function
```

They are registered with `binaryninjaui.UIAction.registerAction()` and bound with
`UIActionHandler.bindAction()` on the relevant widgets. The controller binds
existing widgets immediately and new widgets on Show/FocusIn, so keyboard use
works before the first context menu is opened. Binary Ninja creates WidgetShortcut
bindings, so Symbols gets a handler on the actual focused `ComponentTreeView`,
with its parent bindings inherited. Its selection remains authoritative despite
an empty native action context. No default keybindings are assigned.

The Qt menu entries invoke `UIActionHandler.executeAction()` through the same
controller. A bounded dispatch context preserves the original popup selection.
Menu shortcut labels come from `UIAction.getKeyBinding()`; the QActions display
them without installing a second set of Qt shortcuts. The **Keybindings** command
palette entry opens Binary Ninja's keybinding editor.

## Runtime discovery

The relevant live widget relationships are:

```text
Linear context popup:
  QMenu → LinearView → QWidget → ViewFrame → ViewPane → …

Graph context popup:
  QMenu → DisassemblyView → DisassemblyContainer → …

Symbols context popup:
  QMenu → ComponentTree → SidebarWidgetAndHeader → …

Symbols item view:
  ComponentTreeView (Python sees QTreeView)
    → FilteredView → ComponentTree
```

Although the sidebar is titled **Symbols**, the installed build implements it
with `ComponentTree` and `ComponentTreeView`. Its Python wrappers are generic
`SidebarWidget` and `QTreeView` objects. `metaObject().className()` and
`QObject.inherits()` expose their concrete Qt class identities.

The hook uses widget-scoped event filters and examines `QEvent.Show` for
`QMenu` instances. At this point the host has populated its menu. It recognizes
the popup's immediate parent, then uses `QMenu.insertAction()` to add ordinary
`QAction` objects at the top level. The host still handles the event normally.
Submenus and unrelated widget menus do not match the owner check.

For reference, Binary Ninja also exposes `UIContextNotification.OnContextMenuCreated`
for Linear/Graph/Hex/Types/Stack views. The API explicitly excludes the other
menu-construction paths, which is why the Qt-level hook is useful for Symbols.

## Capturing the right context

**Linear/Graph View:** its `__UIActionHandler` dynamic property exposes the native
`UIActionHandler`. `actionContext()` supplies the selected address, BinaryView,
function, and token context. The callback receives this native context too.

**Symbols:** `ComponentTree.__UIActionHandler.actionContext()` was empty in the
tested build (`binaryView=None`, `address=0`). The selected address must come from
the item model instead:

1. Find the `ComponentTreeView` using its Qt class identity.
2. Locate the column whose header is `Address`.
3. Read the selection model's selected rows, putting the current row first.
4. Read their address and name using `Qt.DisplayRole`; skip component folders.
5. Obtain the BinaryView through `UIContext.currentViewFrameForWidget(owner)`.

This keeps the selected Symbols rows independent from the disassembly cursor.
In the live test, Linear was at `0x20005690`, while the selected Symbols row was
`_cd9660_blktooff` at `0x20004000`. The injected Symbols action received
`0x20004000`, and the correct project-backed BinaryView.

The `HostMenuContext` is captured when the menu appears. A callback therefore
operates on that popup's context rather than querying whichever widget has focus
later. It contains:

- `kind`: `"linear"`, `"graph"`, or `"symbols"`.
- `owner`: the native Qt widget that owns the popup.
- `binary_view`: the associated BinaryView.
- `addresses`: a tuple of integer addresses, supporting Symbols multiselection.
- `address`: the first address, or `None`.
- `names`: names of the selected Symbols rows.
- `native_context`: the native UIActionContext, when available.

## Using the hook

Construct and remove the hook on the UI thread, and retain a Python reference:

```python
import binaryninja
from bnnotes.src.ui.host import RootContextMenus, RootMenuAction

def clicked(context):
    print(context.kind, context.binary_view, context.addresses)

def install():
    global root_menu_hook
    root_menu_hook = RootContextMenus([
        RootMenuAction(
            "My root action",
            clicked,
            enabled=lambda context: context.binary_view is not None
                                    and context.address is not None,
        ),
    ])

binaryninja.execute_on_main_thread_and_wait(install)

# Remove the event filter and its items from menus that have already been built:
# binaryninja.execute_on_main_thread_and_wait(root_menu_hook.uninstall)
```

Pass multiple `RootMenuAction` values to insert a group of root actions. Each
popup refresh replaces the hook's previous items, avoiding duplicates. Existing
native actions are preserved. `uninstall()` removes only that hook's marked items.

Use `kinds=("symbols",)` to restrict an action to the Symbols popup. To insert
inside an existing group, specify `after=("Copy Raw Name", "Copy Name")`: the
first matching native label supplies the insertion point, without adding a
separator. If none of the requested labels is present, that action is omitted.
Actions without `after` are grouped at the top as in the example above.

For keybindable actions, give each spec an `action_name`, construct
`NativeUIActions(binaryninjaui, specs)`, and pass it to
`RootContextMenus(specs, action_dispatcher=controller)`. Retain both objects. The
example above uses menu-only callbacks; the built-in
BNURL commands use the registered UI-action route.

## Verification

`tests/test_host.py` checks root placement, callback targeting, native menu
preservation, submenu exclusion and repeated popup creation. Native visual
inspection uses the [agent UI framework](../tools/agent_ui/README.md); capture the
app window with the menu open or supply a targeted inspection script.
