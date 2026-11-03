from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from weakref import WeakSet, ref

from PySide6.QtCore import QEvent, QObject, Qt, QThread
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QApplication, QMenu, QWidget
from shiboken6 import isValid

from ..links import binary_ninja_url
from .common import single_shot


class WidgetEvents(QObject):
    """Event filters on widgets only: app-wide Python filters break embedded shutdown."""

    def __init__(self, callback, *, parent, on_widget=None):
        super().__init__(parent)
        self._app = QApplication.instance()
        self._callback = callback
        self._on_widget = on_widget
        self._widgets = WeakSet()
        self._installed = True
        self._scan_timer = single_shot(self, self._scan)
        self._app.focusChanged.connect(self._focus_changed)
        self._app.focusWindowChanged.connect(self._scan)
        self._app.aboutToQuit.connect(self.uninstall)
        self._scan()

    def _watch(self, widget):
        if not self._installed or not isValid(widget) or widget in self._widgets:
            return
        self._widgets.add(widget)
        widget.installEventFilter(self)
        if self._on_widget is not None:
            self._on_widget(widget)

    def _watch_tree(self, widget):
        if not isinstance(widget, QWidget) or not isValid(widget):
            return
        self._watch(widget)
        for child in widget.findChildren(QWidget):
            self._watch(child)

    def _scan(self):
        if self._installed:
            for widget in self._app.allWidgets():
                self._watch(widget)

    def _focus_changed(self, _old, current):
        if current is not None and isValid(current):
            if current.window() not in self._widgets:
                self._watch_tree(current.window())
            else:
                self._watch_tree(current)

    def eventFilter(self, watched, event):
        if not self._installed:
            return False
        if event.type() == QEvent.Type.ChildPolished:
            # The child is now fully constructed; catch menus before Show.
            self._watch_tree(event.child())
        elif event.type() == QEvent.Type.ChildAdded:
            # Avoid wrapping a child while its native constructor is incomplete.
            self._scan_timer.start(0)
        return bool(self._callback(watched, event))

    def uninstall(self):
        if not self._installed:
            return
        self._installed = False
        self._scan_timer.stop()
        self._app.focusChanged.disconnect(self._focus_changed)
        self._app.focusWindowChanged.disconnect(self._scan)
        self._app.aboutToQuit.disconnect(self.uninstall)
        # Wrappers can leave the WeakSet while their widgets live; enumerate live widgets.
        for widget in self._app.allWidgets():
            widget.removeEventFilter(self)
        self._widgets.clear()


@dataclass(frozen=True)
class HostMenuContext:
    kind: str
    owner: QWidget
    binary_view: object
    addresses: tuple[int, ...]
    names: tuple[str, ...] = ()
    native_context: object = None

    @property
    def address(self):
        return self.addresses[0] if self.addresses else None


@dataclass(frozen=True)
class RootMenuAction:
    text: str
    callback: Callable[[HostMenuContext], None]
    enabled: Callable[[HostMenuContext], bool] | None = None
    kinds: tuple[str, ...] = ("linear", "graph", "symbols")
    after: tuple[str, ...] = ()
    action_name: str | None = None


def host_owner(owner):
    if not isinstance(owner, QWidget) or not isValid(owner):
        return None
    if owner.inherits("LinearView"):
        return "linear", owner
    if owner.inherits("DisassemblyView"):
        return "graph", owner
    if owner.inherits("ComponentTree") or owner.inherits("SymbolsView"):
        return "symbols", owner
    return None


def menu_owner(menu):
    """Identify only root popups belonging to the requested host surfaces."""
    owner = menu.parentWidget()
    return None if isinstance(owner, QMenu) else host_owner(owner)


def symbol_item_view(owner):
    return next((child for child in owner.findChildren(QAbstractItemView)
                 if child.inherits("ComponentTreeView") or child.inherits("SymbolList")), None)


def selected_symbols(owner):
    """Read actual selected rows; the main disassembly cursor may be elsewhere."""
    tree = symbol_item_view(owner)
    if tree is None or tree.model() is None or tree.selectionModel() is None:
        return (), ()
    model = tree.model()
    column = next((index for index in range(model.columnCount())
                   if str(model.headerData(index, Qt.Orientation.Horizontal)).casefold()
                   == "address"), None)
    if column is None:
        return (), ()
    indexes = tree.selectionModel().selectedRows(0)
    current = tree.currentIndex().siblingAtColumn(0)
    indexes.sort(key=lambda index: index != current)
    addresses, names = [], []
    for index in indexes:
        value = model.data(index.siblingAtColumn(column), Qt.ItemDataRole.DisplayRole)
        try:
            address = value if isinstance(value, int) else int(str(value), 16)
        except (ValueError, TypeError):
            continue  # Component folders do not have a symbol address.
        if 0 <= address < 1 << 64:
            addresses.append(address)
            names.append(str(model.data(index, Qt.ItemDataRole.DisplayRole) or ""))
    return tuple(addresses), tuple(names)


def host_menu_context(kind, owner):
    handler = owner.property("__UIActionHandler")
    native = handler.actionContext() if handler is not None else None
    data = getattr(native, "binaryView", None)
    if kind in ("linear", "graph"):
        address = getattr(native, "address", None)
        addresses = (int(address),) if address is not None else ()
        return HostMenuContext(kind, owner, data, addresses, native_context=native)

    addresses, names = selected_symbols(owner)
    # ComponentTree's actionContext is empty: use its frame's view, not its address.
    if data is None:
        ui = sys.modules.get("binaryninjaui")
        if ui is not None:
            frame = ui.UIContext.currentViewFrameForWidget(owner)
            if frame is not None:
                data = frame.getCurrentBinaryView()
    return HostMenuContext(kind, owner, data, addresses, names, native)


class RootContextMenus(QObject):
    """Adds root actions to native menus once the host has populated them."""

    def __init__(self, actions, *, context_provider=host_menu_context, action_dispatcher=None):
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("Root context menus require an initialized Qt application.")
        if QThread.currentThread() != app.thread():
            raise RuntimeError("Install root context menus on the Qt UI thread.")
        super().__init__(app)
        self.actions = tuple(actions)
        self.context_provider = context_provider
        self.action_dispatcher = action_dispatcher
        self._marker = f"bnnotes-root-menu-{id(self)}"
        self._menus = WeakSet()
        self._app = app
        self._installed = True
        self._events = WidgetEvents(self.eventFilter, parent=self)
        app.aboutToQuit.connect(self.uninstall)

    def _remove_actions(self, menu):
        for action in menu.actions():
            if action.property(self._marker):
                menu.removeAction(action)
                action.deleteLater()

    def eventFilter(self, watched, event):
        if (not self._installed or event.type() != QEvent.Type.Show
                or not isinstance(watched, QMenu)):
            return False
        match = menu_owner(watched)
        if match is None:
            return False
        self._remove_actions(watched)
        specs = [spec for spec in self.actions if match[0] in spec.kinds]
        if not specs:
            return False
        context = self.context_provider(*match)
        existing = watched.actions()
        first = existing[0] if existing else None
        prepended = False
        for spec in specs:
            before = first
            if spec.after:
                position = next((index for name in spec.after
                                 for index, item in enumerate(existing)
                                 if item.text().split("\t", 1)[0].replace("&", "") == name), None)
                if position is None:
                    continue
                before = existing[position + 1] if position + 1 < len(existing) else None
            else:
                prepended = True
            action = QAction(spec.text, watched)
            action.setProperty(self._marker, True)
            if spec.action_name is not None and self.action_dispatcher is not None:
                dispatcher = self.action_dispatcher
                action.setProperty("bnnotesUIAction", spec.action_name)
                action.setEnabled(dispatcher.is_valid(spec.action_name, context))
                shortcut = dispatcher.shortcut_text(spec.action_name)
                if shortcut:
                    # Display only: UIActionHandler owns the actual shortcut.
                    action.setText(f"{spec.text}\t{shortcut}")
                action.triggered.connect(
                    lambda _checked=False, name=spec.action_name, context=context:
                        self.action_dispatcher.execute(name, context)
                )
            else:
                action.setEnabled(spec.enabled(context) if spec.enabled is not None else True)
                # The context of the popup, not of whatever has focus when the action runs.
                action.triggered.connect(
                    lambda _checked=False, spec=spec, context=context: spec.callback(context)
                )
            watched.insertAction(before, action)
        if prepended and existing:
            separator = QAction(watched)
            separator.setSeparator(True)
            separator.setProperty(self._marker, True)
            watched.insertAction(first, separator)
        self._menus.add(watched)
        watched.adjustSize()
        return False  # Preserve the host's normal menu handling.

    def uninstall(self):
        if not self._installed:
            return
        if QThread.currentThread() != self._app.thread():
            raise RuntimeError("Uninstall root context menus on the Qt UI thread.")
        self._installed = False
        self._events.uninstall()
        self._app.aboutToQuit.disconnect(self.uninstall)
        for menu in self._menus:
            if isValid(menu):
                self._remove_actions(menu)
        self._menus.clear()


def function_bnurl(context):
    if context.binary_view is None or len(context.addresses) != 1:
        return None
    if context.kind == "symbols":
        function = context.binary_view.get_function_at(context.address)
    elif context.kind in ("linear", "graph"):
        function = getattr(context.native_context, "function", None)
        if function is None:
            function = context.binary_view.get_function_at(context.address)
        if function is None:
            functions = context.binary_view.get_functions_containing(context.address)
            function = next(iter(functions), None)
    else:
        return None
    if function is None:
        return None
    return binary_ninja_url(context.binary_view, function.name or f"{function.start:#x}")


def address_bnurl(context):
    if (context.kind not in ("linear", "graph") or context.binary_view is None
            or len(context.addresses) != 1
            or not context.binary_view.is_valid_offset(context.address)):
        return None
    return binary_ninja_url(context.binary_view, context.address)


def _copy_bnurl_action(text, resolver, *, kinds, after):
    def copy(context):
        url = resolver(context)
        if url is not None:
            QApplication.clipboard().setText(url)

    return RootMenuAction(
        text, copy, enabled=lambda context: resolver(context) is not None,
        kinds=kinds, after=after, action_name=f"Notepad\\{text}",
    )


def copy_function_bnurl_action():
    return _copy_bnurl_action(
        "Copy BNURL to Function", function_bnurl,
        kinds=("symbols",), after=("Copy Raw Name", "Copy Raw Names", "Copy Name", "Copy Names"),
    )


def copy_view_bnurl_actions():
    return [
        _copy_bnurl_action(text, resolver, kinds=("linear", "graph"),
                           after=("Copy Address", "Copy"))
        for text, resolver in (("Copy BNURL to Address", address_bnurl),
                               ("Copy BNURL to Function", function_bnurl))
    ]


@dataclass
class _Binding:
    owner: QWidget
    widget: QWidget
    handler: object
    names: tuple[str, ...]


class NativeUIActions(QObject):
    """Binds registered actions to every Linear, Graph and Symbols action handler."""

    def __init__(self, ui, actions):
        app = QApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RuntimeError("Install native UI actions on the Qt UI thread.")
        super().__init__(app)
        self.ui = ui
        self.actions = tuple(spec for spec in actions if spec.action_name is not None)
        self._app = app
        self._owners = {}
        self._menu_contexts = []
        self._installed = True
        for name in dict.fromkeys(spec.action_name for spec in self.actions):
            if not ui.UIAction.isActionRegistered(name):
                ui.UIAction.registerAction(name)
        self._events = WidgetEvents(self.eventFilter, parent=self, on_widget=self.bind_owner)
        app.aboutToQuit.connect(self.uninstall)

    def bind_owner(self, owner):
        if not self._installed:
            return None
        match = host_owner(owner)
        if match is None:
            return None
        kind, _owner = match
        key = id(owner)
        existing = self._owners.get(key)
        if existing is not None and isValid(existing.widget):
            return existing.handler
        widget = symbol_item_view(owner) if kind == "symbols" else owner
        if widget is None:
            return None
        handler = widget.property("__UIActionHandler")
        if handler is None and kind == "symbols":
            # Shortcuts are per widget: Symbols keys belong on the focused item view.
            handler = self.ui.UIActionHandler()
            widget._bnnotes_action_handler = handler
            handler.setupActionHandler(widget)
        if handler is None:
            return None
        names = []
        owner_ref = ref(owner)
        for spec in self.actions:
            if kind not in spec.kinds:
                continue
            handler.bindAction(spec.action_name, self.ui.UIAction(
                lambda native, spec=spec, kind=kind, owner_ref=owner_ref:
                    self._activate(spec, kind, owner_ref(), native),
                lambda native, spec=spec, kind=kind, owner_ref=owner_ref:
                    self._valid(spec, kind, owner_ref(), native),
            ))
            names.append(spec.action_name)
        self._owners[key] = _Binding(owner, widget, handler, tuple(names))
        owner.destroyed.connect(lambda *_args, key=key: self._owners.pop(key, None))
        return handler

    def eventFilter(self, watched, event):
        if (event.type() in (QEvent.Type.Show, QEvent.Type.FocusIn)
                and isinstance(watched, QWidget)):
            owner = watched
            while owner is not None and isValid(owner):
                if host_owner(owner) is not None:
                    self.bind_owner(owner)
                    break
                owner = owner.parentWidget()
        return False

    def _context(self, name, kind, owner, native):
        if not self._installed or owner is None or not isValid(owner):
            return None
        if self._menu_contexts:
            active_name, context = self._menu_contexts[-1]
            if active_name == name and context.owner is owner:
                return context
        if kind == "symbols" or getattr(native, "binaryView", None) is None:
            return host_menu_context(kind, owner)
        return HostMenuContext(kind, owner, native.binaryView, (int(native.address),),
                               native_context=native)

    def _valid(self, spec, kind, owner, native):
        context = self._context(spec.action_name, kind, owner, native)
        return context is not None and (spec.enabled is None or spec.enabled(context))

    def _activate(self, spec, kind, owner, native):
        context = self._context(spec.action_name, kind, owner, native)
        if context is not None and (spec.enabled is None or spec.enabled(context)):
            spec.callback(context)

    @contextmanager
    def _menu_dispatch(self, name, context):
        native = context.native_context
        if native is None or context.kind == "symbols":
            native = self.ui.UIActionContext()
            native.widget = context.owner
            native.context = self.ui.UIContext.contextForWidget(context.owner)
            native.binaryView = context.binary_view
            native.address = context.address if context.address is not None else 0
            if context.binary_view is not None and len(context.addresses) == 1:
                native.function = context.binary_view.get_function_at(context.address)
        self._menu_contexts.append((name, context))
        try:
            yield native
        finally:
            self._menu_contexts.pop()

    def is_valid(self, name, context):
        handler = self.bind_owner(context.owner)
        if handler is None:
            return False
        with self._menu_dispatch(name, context) as native:
            return handler.isValidAction(name, native)

    def execute(self, name, context):
        handler = self.bind_owner(context.owner)
        if handler is not None:
            with self._menu_dispatch(name, context) as native:
                handler.executeAction(name, native)

    def shortcut_text(self, name):
        return ", ".join(sequence.toString(QKeySequence.SequenceFormat.NativeText)
                         for sequence in self.ui.UIAction.getKeyBinding(name)
                         if not sequence.isEmpty())

    def uninstall(self):
        if not self._installed:
            return
        if QThread.currentThread() != self._app.thread():
            raise RuntimeError("Uninstall native UI actions on the Qt UI thread.")
        self._installed = False
        self._events.uninstall()
        self._app.aboutToQuit.disconnect(self.uninstall)
        for binding in list(self._owners.values()):
            if isValid(binding.widget):
                for name in binding.names:
                    binding.handler.unbindAction(name)
        self._owners.clear()
