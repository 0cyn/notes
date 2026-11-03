from __future__ import annotations

import sys
from collections import deque
from enum import Enum
from pathlib import Path
from time import perf_counter
from weakref import WeakSet

from PySide6.QtCore import QEvent, QObject, QPoint, QSignalBlocker, Qt, Signal, Slot
from PySide6.QtGui import QContextMenuEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QMessageBox,
    QSizePolicy,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from ..links import AddressAnchor, find_address_anchor, resolve_in_bv_link
from . import session as sessions
from .common import (
    NOTE_CONTENT_MARGIN,
    FindKeyFilter,
    IdleTask,
    Lease,
    NoteContextMenu,
    ToolbarButton,
    binary_view_id,
    keep_focus,
    live_action,
    live_actions,
    menu_text,
    single_shot,
    view_settings,
)
from .home import (
    FOLDER,
    FOLDERS,
    SEARCH,
    NotesHomeWidget,
    add_folder_menus,
    add_move_actions,
    create_folder,
)
from .notepad import NotepadWidget
from .onboarding import OnboardingWidget, mark_onboarded, onboarding_due
from .preferences import NotepadSettingsWidget, editor_preferences, toggle_author_bar
from .provider import HLILPreviewProvider
from .search import FindAllNotesWidget
from .session import notebook_for, recent_first


class NavigationSync:
    """Following view navigation into notes.

    Ignores echoes of our own navigation, retries arrivals that wait on an index, and
    doesn't recenter on the anchor it last moved to.
    """

    def __init__(self):
        self.enabled = False
        self._echo = None
        self.waiting = None    # (offset, function) awaiting an index
        self.anchor = None

    def reset(self, enabled=None):
        if enabled is not None:
            self.enabled = enabled
        self._echo = self.waiting = self.anchor = None

    def sent(self, offset):
        self._echo = int(offset)
        self.waiting = None

    def is_echo(self, offset) -> bool:
        """Whether `offset` is the echo of our own navigation; any other address ends it."""
        if self._echo == int(offset):
            return True
        self._echo = None
        return False

    def wait(self, offset, function):
        self.waiting = offset, function

    def left_note(self, note_id):
        if self.anchor is not None and self.anchor[0] != note_id:
            self.anchor = None


def new_note_button(parent, callback):
    button = ToolbarButton(parent, symbol="plus")
    button.setObjectName("bnnotes-new-note")
    button.setToolTip("New Note")
    button.setAccessibleName("New Note")
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    button.clicked.connect(callback)
    return button


def _tab_context(adapter, event, bar, container, *, viewport=None):
    if (event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease)
            and event.button() == Qt.MouseButton.RightButton):
        # Don't select on right press: context actions have their own target.
        event.accept()
        return True
    if event.type() != QEvent.Type.ContextMenu:
        return False
    surface = viewport if viewport is not None else bar
    if event.reason() == QContextMenuEvent.Reason.Keyboard:
        index = bar.currentIndex()
        rectangle = bar.tabRect(index) if index >= 0 else surface.rect()
        point = surface.mapToGlobal(rectangle.bottomLeft())
    else:
        point = event.globalPos()
        local = surface.mapFromGlobal(point)
        index = bar.tabAt(local) if surface.rect().contains(local) else -1
    page = container.widget(index) if 0 <= index < container.count() else None
    adapter.contextMenuRequested.emit(page, container, point)
    event.accept()
    return True


class _NoteTabs(QWidget):
    """The notebook's tab panes: Binary Ninja's split tabs, or one QTabWidget standalone."""

    currentChanged = Signal(object)
    tabClosed = Signal(object)
    newNoteRequested = Signal(object)
    contextMenuRequested = Signal(object, object, QPoint)
    orderChanged = Signal()
    supportsSplitting = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fixed = WeakSet()  # Pages whose tab may not close.
        QVBoxLayout(self).setContentsMargins(0, 0, 0, 0)

    def pages(self):
        return [page for container in self._containers() for page in self.pagesIn(container)]

    def setTabClosable(self, page, closable):
        (self._fixed.discard if closable else self._fixed.add)(page)
        self._show_close_button(page, closable)


class NativeNoteTabs(_NoteTabs):
    supportsSplitting = True

    def __init__(self, ui, parent=None):
        super().__init__(parent)
        self.ui = ui
        # The native collection must outlive every dockable container.
        self.collection = ui.DockableTabCollection()
        self.widget = ui.SplitTabWidget(self.collection)
        self.widget.setTabStyle(ui.GlobalAreaTabStyle())
        self._current = None
        self._new_note_enabled = True
        self._watched_bars = WeakSet()
        self._tab_geometry_timer = single_shot(self, self._refresh_tab_geometry)
        self.layout().addWidget(self.widget)
        self.widget.currentChanged.connect(self._current_changed)
        self.widget.tabClosed.connect(self.tabClosed.emit)
        self.widget.layoutChanged.connect(self._configure_containers)
        self.widget.layoutChanged.connect(self.orderChanged.emit)
        self._configure_containers()

    def _containers(self):
        """This notebook's panes, not tab widgets inside its pages (like the Notes page's)."""
        own = []
        for container in self.widget.findChildren(self.ui.DockableTabWidget):
            parent = container.parentWidget()
            while parent is not None and parent is not self.widget:
                if isinstance(parent, self.ui.DockableTabWidget):
                    break
                parent = parent.parentWidget()
            else:
                own.append(container)
        return own

    def _configure_containers(self):
        for container in self._containers():
            # Splits stay in this notebook, which owns the menu, leases and actions.
            container.setCanCreateNewWindow(False)
            bar = container.tabBar()
            if bar not in self._watched_bars and hasattr(bar, 'tabMoved'):
                bar.tabMoved.connect(self._tab_moved)
                self._watched_bars.add(bar)
            header = bar.parentWidget()
            layout = header.layout()
            margins = layout.contentsMargins()
            layout.setContentsMargins(
                NOTE_CONTENT_MARGIN, margins.top(), NOTE_CONTENT_MARGIN, margins.bottom(),
            )
            header.installEventFilter(self)
            bar.installEventFilter(self)
            bar.viewport().installEventFilter(self)
            button = header.findChild(ToolbarButton, "bnnotes-new-note")
            if button is None:
                button = new_note_button(header, self._request_new_note)
                layout.insertWidget(layout.indexOf(bar) + 1, button,
                                    alignment=Qt.AlignmentFlag.AlignBottom)
                space = QWidget(header)
                space.setObjectName("bnnotes-tab-space")
                space.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                layout.insertWidget(layout.indexOf(button) + 1, space, 1)
            button.setEnabled(self._new_note_enabled)
            # Keep each pane's + beside its tabs, with spare width after it.
            policy = bar.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Maximum)
            bar.setSizePolicy(policy)
            space = header.findChild(QWidget, "bnnotes-tab-space")
            for index in range(layout.count()):
                layout.setStretch(index, int(layout.itemAt(index).widget() is space))
        self._sync_header_heights()
        self._tab_geometry_timer.start(0)

    @Slot()
    def _request_new_note(self):
        # Restored layouts can reuse a header; find its current pane.
        button = self.sender()
        if isinstance(button, QWidget):
            for container in self._containers():
                if container.isAncestorOf(button):
                    self.newNoteRequested.emit(container)
                    return

    def _refresh_tab_geometry(self):
        # Layouts can keep a drag placeholder's size after the drop.
        for container in self._containers():
            bar = container.tabBar()
            header = bar.parentWidget()
            bar.updateGeometry()
            header.layout().invalidate()

    def currentContainer(self):
        containers = self._containers()
        current = self.currentWidget()
        return next((item for item in containers if item.currentWidget() is current),
                    next(iter(containers), None))

    def containerFor(self, page):
        if page is None:
            return self.currentContainer()
        return next((item for item in self._containers()
                     if item.indexOf(page) >= 0), None)

    def pagesIn(self, container):
        if (container is None or not isValid(container)
                or container not in self._containers()):
            return []
        return [container.widget(index) for index in range(container.count())]

    def _tab_moved(self, *_args):
        self.orderChanged.emit()

    def split(self, page, edge):
        container = self.containerFor(page)
        if container is None or container.count() < 2:
            return False
        container.splitTab.emit(container.indexOf(page), edge)
        self.selectWidget(page)
        return True

    def newButton(self):
        container = self.currentContainer()
        if container is not None:
            return container.tabBar().parentWidget().findChild(ToolbarButton, "bnnotes-new-note")
        return None

    def setNewNoteEnabled(self, enabled):
        self._new_note_enabled = enabled
        for button in self.widget.findChildren(ToolbarButton, "bnnotes-new-note"):
            button.setEnabled(enabled)

    def _sync_header_heights(self):
        headers = [container.tabBar().parentWidget()
                   for container in self._containers()]
        # Panes size headers independently; match them so tabs line up.
        height = max((header.layout().sizeHint().height() for header in headers), default=0)
        for header in headers:
            if header.minimumHeight() != height:
                header.setMinimumHeight(height)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.ContextMenu, QEvent.Type.MouseButtonPress,
                            QEvent.Type.MouseButtonRelease):
            for container in self._containers():
                if not container.isAncestorOf(watched):
                    continue
                bar = container.tabBar()
                if watched in (bar, bar.viewport(), bar.parentWidget()) and _tab_context(
                    self, event, bar, container, viewport=bar.viewport(),
                ):
                    return True
        if event.type() == QEvent.Type.LayoutRequest:
            self._sync_header_heights()
        elif event.type() in (QEvent.Type.MouseButtonRelease, QEvent.Type.UngrabMouse):
            # After native release handling has removed the drag placeholder.
            self._tab_geometry_timer.start(0)
        return super().eventFilter(watched, event)

    def _current_changed(self, page):
        self._current = page
        self._current = self.currentWidget()
        self.currentChanged.emit(self._current)

    def currentWidget(self):
        # Closing the active split can report None; keep a surviving pane's page.
        pages = [container.currentWidget()
                 for container in self._containers()]
        if self._current is not None and any(page is self._current for page in pages):
            return self._current
        return next((page for page in pages if page is not None), None)

    def addTab(self, page, title, *, target=None, index=None):
        if index is not None and target is not None:
            target.insertTab(index, page, title)
        elif target is not None:
            target.addTab(page, title)
        else:
            self.widget.addTab(page, title)
        self.widget.setCanCloseTab(page, True)
        self._configure_containers()

    def selectWidget(self, page):
        self.widget.selectWidget(page)

    def setTabText(self, page, title):
        for container in self._containers():
            index = container.indexOf(page)
            if index >= 0:
                container.setTabText(index, title)
                container.setTabToolTip(index, title)

    def closeTab(self, page):
        if page in self._fixed:
            return False
        return self.widget.closeTab(page)

    def removeTab(self, page):
        return self.widget.removeTab(page)

    def _show_close_button(self, page, closable):
        self.widget.setCanCloseTab(page, closable)

    def moveToFront(self, page, title):
        """Make `page` the first tab of its pane, keeping the selection."""
        container = self.containerFor(page)
        if container is None or container.indexOf(page) <= 0:
            return
        selected = self.currentWidget()
        closable = page not in self._fixed
        self.widget.removeTab(page)
        self.addTab(page, title, target=container, index=0)
        self.setTabClosable(page, closable)
        if selected is not None:
            self.selectWidget(selected)

    def setCornerWidget(self, widget):
        # Only the menu is shared. Per-pane + buttons belong to their headers.
        self.widget.setCornerWidget(widget, Qt.Corner.TopRightCorner)
        self._configure_containers()


class StandaloneNoteTabs(_NoteTabs):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.widget = QTabWidget(self)
        self.widget.setDocumentMode(True)
        self.widget.setTabsClosable(True)
        self.widget.setMovable(True)
        self.widget.tabBar().installEventFilter(self)
        self.widget.tabBar().tabMoved.connect(lambda *_args: self.orderChanged.emit())
        self.controls = QWidget(self)
        controls = QHBoxLayout(self.controls)
        controls.setContentsMargins(2, 0, 2, 0)
        controls.setSpacing(2)
        self._new_button = new_note_button(self.controls, lambda: self.newNoteRequested.emit(None))
        controls.addWidget(self._new_button)
        controls.addStretch(1)
        self.widget.setCornerWidget(self.controls, Qt.Corner.TopRightCorner)
        self.layout().addWidget(self.widget)
        self.widget.currentChanged.connect(
            lambda _index: self.currentChanged.emit(self.currentWidget())
        )
        self.widget.tabCloseRequested.connect(
            lambda index: self.closeTab(self.widget.widget(index))
        )

    def currentWidget(self):
        return self.widget.currentWidget()

    def currentContainer(self):
        return self.widget

    def containerFor(self, page):
        return self.widget if page is None or self.widget.indexOf(page) >= 0 else None

    def _containers(self):
        return [self.widget]

    def pagesIn(self, container):
        if container is not self.widget:
            return []
        return [self.widget.widget(index) for index in range(self.widget.count())]

    def eventFilter(self, watched, event):
        if watched is self.widget.tabBar() and _tab_context(
            self, event, watched, self.widget,
        ):
            return True
        return super().eventFilter(watched, event)

    def newButton(self):
        return self._new_button

    def setNewNoteEnabled(self, enabled):
        self._new_button.setEnabled(enabled)

    def addTab(self, page, title, *, target=None, index=None):
        # Native tabs call target.addTab(); reject anything that isn't a pane here too.
        if target is not None and target is not self.widget:
            raise TypeError(f'Not a tab pane: {target!r}')
        if index is None:
            self.widget.addTab(page, title)
        else:
            self.widget.insertTab(index, page, title)

    def selectWidget(self, page):
        self.widget.setCurrentWidget(page)

    def setTabText(self, page, title):
        index = self.widget.indexOf(page)
        self.widget.setTabText(index, title)
        self.widget.setTabToolTip(index, title)

    def closeTab(self, page):
        if page in self._fixed or self.widget.indexOf(page) < 0 or not page.close():
            return False
        self.removeTab(page)
        self.tabClosed.emit(page)
        page.deleteLater()
        return True

    def removeTab(self, page):
        index = self.widget.indexOf(page)
        if index < 0:
            return False
        self.widget.removeTab(index)
        return True

    def _show_close_button(self, page, closable):
        bar, index = self.widget.tabBar(), self.widget.indexOf(page)
        for side in (QTabBar.ButtonPosition.RightSide, QTabBar.ButtonPosition.LeftSide):
            if (button := bar.tabButton(index, side)) is not None:
                button.setVisible(closable)

    def moveToFront(self, page, _title):
        index = self.widget.indexOf(page)
        if index > 0:
            self.widget.tabBar().moveTab(index, 0)

    def setCornerWidget(self, widget):
        self.controls.layout().addWidget(widget)


def create_note_tabs(parent):
    ui = sys.modules.get("binaryninjaui")
    if ui is not None:
        return NativeNoteTabs(ui, parent)
    return StandaloneNoteTabs(parent)


class TabMenus(QObject):
    def __init__(self, notebook):
        super().__init__(notebook)
        self.owner = notebook
        self.menu = None
        self._closing = deque()
        self._close_book = None
        self._close_selected = None
        self._close_task = IdleTask(self)
        self._close_task.timeout.connect(self._close_batch)

    def reset(self):
        if self.menu is not None and isValid(self.menu):
            self.menu.close()
            self.menu.deleteLater()
        self.menu = None
        self._close_task.stop()
        self._closing.clear()
        self._close_selected = None

    def _valid(self, page):
        if page is None or not isValid(page):
            return False
        if isinstance(page, NotepadWidget):
            return (self.owner.notebook is page.session.notebook
                    and self.owner._pages.get(page.session.id) is page)
        return page in (self.owner._settings_page, self.owner.home.page)

    def _container(self, page, container):
        if page is not None:
            return self.owner.tabs.containerFor(page) if self._valid(page) else None
        return container if container is not None and isValid(container) else None

    def _pages(self, page, container):
        container = self._container(page, container)
        pages = self.owner.tabs.pagesIn(container)
        if container is not None and container is self.owner.tabs.currentContainer():
            pages.extend(item for item in self.owner.tab_restore.pending if item not in pages)
        return pages

    def show(self, page, container, position):
        if self.menu is not None and isValid(self.menu):
            self.menu.close()
            self.menu.deleteLater()
        self.menu = self.build(page, container)
        self.menu.popup(position)

    def build(self, page, container=None):
        owner, book = self.owner, self.owner.notebook
        container = self._container(page, container)
        menu = NoteContextMenu(owner)
        menu.setObjectName('bnnotes-tab-context-menu')
        editable = book is not None and not book.load_failed
        note_page = self._valid(page) and isinstance(page, NotepadWidget)
        pages = self._pages(page, container)

        def guarded(callback):
            return lambda _checked=False: (callback() if owner.notebook is book
                                            and (self._valid(page) if page is not None else
                                                 self._container(page, container) is not None)
                                            else None)

        def add(label, callback, enabled=True, tooltip=None):
            action = menu.addAction(label, guarded(callback))
            action.setEnabled(enabled)
            if tooltip:
                action.setToolTip(tooltip)
            return action

        add('New Note', lambda: owner.new_note(target=self._container(page, container)),
            editable and container is not None)
        if note_page:
            menu.addSeparator()
            add_move_actions(menu, owner, book, page.session)
            menu.addSeparator()
            add('Duplicate Note', lambda: self.duplicate(page), editable)
            add('Copy Note as Markdown',
                lambda: QApplication.clipboard().setText(page.session.source),
                bool(page.session.source))
            add('Export Note as Markdown…', lambda: owner.export_markdown(page))
            add('Find in Note…', lambda: self.find(page))
        menu.addSeparator()
        if page is not None:
            add('Close Tab', lambda: self.close_many([page]), owner.can_close(page))
            index = pages.index(page) if page in pages else -1
            for label, mode, enabled in (
                ('Close Other Tabs', 'others', len(pages) > 1),
                ('Close Tabs to the Left', 'left', index > 0),
                ('Close Tabs to the Right', 'right', 0 <= index < len(pages) - 1),
            ):
                add(label, lambda mode=mode: self.close_group(page, container, mode), enabled,
                    'Applies to this pane. Saved notes remain in the Notes menu.')
        add('Close All Tabs', lambda: self.close_group(page, container, 'all'), bool(pages),
            'Close every tab in this pane. Saved notes remain in the Notes menu.')
        if page is not None and owner.tabs.supportsSplitting:
            menu.addSeparator()
            add('Split to the Right', lambda: owner.tabs.split(page, Qt.Edge.RightEdge),
                len(owner.tabs.pagesIn(container)) > 1,
                'Move this tab into a new pane to the right.')
            add('Split Below', lambda: owner.tabs.split(page, Qt.Edge.BottomEdge),
                len(owner.tabs.pagesIn(container)) > 1, 'Move this tab into a new pane below.')
        if note_page:
            menu.addSeparator()
            add('Delete Note…', lambda: owner.delete_note(page), editable)
        return menu

    def find(self, page):
        self.owner.tabs.selectWidget(page)
        page.materialize()
        page.show_find()

    def duplicate(self, page):
        note = self.owner.notebook.new_note(page.session.source)
        if note is not None:
            self.owner.open_note(note, target=self.owner.tabs.containerFor(page))

    def close_group(self, page, container, mode):
        pages = self._pages(page, container)
        if mode != 'all' and page not in pages:
            return
        if mode == 'others':
            victims = [item for item in pages if item is not page]
        elif mode == 'left':
            victims = pages[:pages.index(page)]
        elif mode == 'right':
            victims = pages[pages.index(page) + 1:]
        else:
            victims = pages
        if mode != 'all' and self.owner.tabs.currentWidget() in victims:
            self.owner.tabs.selectWidget(page)
        self.close_many(victims)

    def close_many(self, pages):
        owner = self.owner
        owner.tab_restore.pause()
        pending = [page for page in pages if page in owner.tab_restore]
        visible = [page for page in reversed(pages) if page not in pending]
        self._closing = deque(pending + visible)
        self._close_book = owner.notebook
        selected = owner.tabs.currentWidget()
        self._close_selected = selected if selected not in pages else None
        self._close_batch()

    def _close_batch(self):
        owner = self.owner
        if owner.notebook is not self._close_book:
            self._closing.clear()
            return
        deadline = perf_counter() + .003
        while self._closing:
            page = self._closing.popleft()
            if not self._valid(page):
                continue
            if not owner.close_page(page):
                self._closing.clear()
                break
            if perf_counter() >= deadline:
                break
        if (self._valid(self._close_selected)
                and owner.tabs.currentWidget() is not self._close_selected):
            owner.tabs.selectWidget(self._close_selected)
        if self._closing:
            self._close_task.start()
        else:
            owner.tab_restore.resume()


class TabRestore(QObject):
    """Pages waiting for a tab header, inserted in 3 ms slices after a file opens.

    `inserting` is true while a header is added; that selection isn't the user's.
    """

    finished = Signal()

    def __init__(self, parent, tabs, order):
        super().__init__(parent)
        self._tabs = tabs
        self._order = order  # () -> every page, in notebook order
        self._queue = []
        self.inserting = False
        self._task = IdleTask(self)
        self._task.timeout.connect(self._insert_slice)

    def __bool__(self):
        return bool(self._queue)

    def __contains__(self, page):
        return page in self._queue

    @property
    def pending(self) -> tuple:
        return tuple(self._queue)

    def defer(self, page):
        if page not in self._queue:
            self._queue.append(page)

    def resume(self):
        if self._queue:
            self._task.start()

    def pause(self):
        self._task.stop()

    def clear(self) -> list:
        """Drop every queued page without adding headers; returns them."""
        self._task.stop()
        dropped, self._queue = self._queue, []
        return dropped

    def discard(self, page) -> bool:
        if page not in self._queue:
            return False
        self._queue.remove(page)
        return True

    def insert(self, page):
        """Add `page`'s header now, in notebook order, keeping the current selection."""
        if not self.discard(page):
            return
        tabs = self._tabs
        selected = tabs.currentWidget()
        self.inserting = True
        try:
            with page.materialization_held():
                target = tabs.currentContainer()
                ordered = self._order()
                following = ordered[ordered.index(page) + 1:]
                before = next((item for item in following
                               if tabs.containerFor(item) is target and item not in self._queue),
                              None)
                index = (target.indexOf(before)
                         if before is not None and target is not None else None)
                tabs.addTab(page, page.session.title, target=target, index=index)
                if selected is not None:
                    tabs.selectWidget(selected)
        finally:
            self.inserting = False
        if selected is None and page.isVisible():
            page.materialize()

    def insert_next(self):
        if self._queue:
            self.insert(self._queue[0])

    def _insert_slice(self):
        deadline = perf_counter() + .003
        while self._queue:
            self.insert_next()
            if perf_counter() >= deadline:
                break
        if self._queue:
            self._task.start()
        else:
            self.finished.emit()


class TabState:
    """A notebook's open tabs and recently opened notes, per file, in persisted UI settings.

    `alive` turns False when the widget is closed or destroyed, so the tabs it closes while
    tearing down aren't recorded as the user's choice.
    """

    def __init__(self, store, surface):
        self.store = store
        self.field = f'notebook.{surface}'
        self.view_id = None
        self.active = None
        self.alive = True

    def bind(self, data):
        self.view_id = binary_view_id(data) if data is not None else None
        self.active = None

    def refresh_id(self, data):
        try:
            self.view_id = binary_view_id(data)
        except (RuntimeError, OSError, ValueError):
            pass  # File teardown can outlive the view wrapper; keep the last identity.

    def read(self):
        """(open note ids, active id), or None when nothing valid is saved."""
        value = self.store.get(self.view_id, self.field)
        identities = value.get('open_notes') if isinstance(value, dict) else None
        if (not isinstance(identities, list)
                or any(not isinstance(item, str) or not item for item in identities)):
            return None
        identities = tuple(dict.fromkeys(identities))
        active = value.get('active_note')
        return identities, active if active in identities else None

    def write(self, identities, current=None):
        if current in identities:
            self.active = current
        elif self.active not in identities:
            self.active = next(iter(identities), None)
        value = self.store.get(self.view_id, self.field, {})
        value = value if isinstance(value, dict) else {}
        value.update(open_notes=list(identities), active_note=self.active or '')
        self.store.set(self.view_id, self.field, value)

    def recent(self):
        """When each note was last opened here: {note id: seconds since the epoch}."""
        value = self.store.get(self.view_id, 'recent_notes', {}) if self.view_id else {}
        return {key: item for key, item in value.items()
                if isinstance(key, str) and type(item) is int} if isinstance(value, dict) else {}

    def remember(self, note_id, known):
        recent = {key: item for key, item in self.recent().items() if key in known}
        recent[note_id] = sessions.now()
        self.store.set(self.view_id, 'recent_notes', recent)

    def flush(self):
        if isValid(self.store):
            self.store.sync()

    def close(self, *_args):
        self.alive = False
        self.flush()


class HomeTab(QObject):
    """The notebook's Notes home page, which behaves like a browser's new-tab page.

    It leads its pane, can't close while it is the only tab, is replaced in place by what
    is opened from it, and reappears whenever the notebook has no tabs.
    """

    TITLE = 'Notes'

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.page = None
        self.query = ''        # The Search section's query, kept while the page is closed.
        self._closed = []      # Pages reported closed since the last empty check.
        self._moving = False   # Reinserting the page at the front is not a close.
        self._empty_check = single_shot(self, self._show_if_empty)
        self._to_front = single_shot(self, self._move_to_front)

    def open(self, target=None, *, focus=True):
        owner, tabs = self.owner, self.owner.tabs
        if self.page is None:
            self.page = NotesHomeWidget(owner, store=owner.tab_state.store)
            self.page.bind(owner.notebook, owner.tab_state.recent)
            # Queued: opening closes the page whose click is still being handled.
            queued = Qt.ConnectionType.QueuedConnection
            self.page.noteActivated.connect(self._open_note, queued)
            self.page.newNoteRequested.connect(
                lambda: self._replace(lambda pane, index: owner.new_note(pane, index=index)),
                queued)
            self.page.resultActivated.connect(self._open_result, queued)
            self.page.search.query.setText(self.query)
            tabs.addTab(self.page, self.TITLE, index=0,
                        target=target if target is not None else tabs.currentContainer())
        tabs.selectWidget(self.page)
        self.refresh()
        if focus:
            self.page.setFocus()
        return self.page

    def bind(self, notebook):
        if self.page is not None:
            self.page.bind(notebook, self.owner.tab_state.recent)

    def _open_note(self, identity):
        notebook = self.owner.notebook
        note = notebook.note(identity) if notebook is not None else None
        if note is None:
            if self.page is not None:
                self.page.refresh()
            return None
        return self._replace(
            lambda pane, index: self.owner.open_note(note, target=pane, index=index))

    def _open_result(self, snippet, match):
        return self._replace(lambda pane, index: self.owner._open_search_result(
            snippet, match, target=pane, index=index))

    def _replace(self, open_page):
        """Open a page where the home tab is, then close the home tab."""
        tabs, home = self.owner.tabs, self.page
        pane = tabs.containerFor(home) if home is not None else None
        page = open_page(pane, pane.indexOf(home) if pane is not None else None)
        if page is not None and home is not None and self.page is home:
            self.refresh()
            tabs.closeTab(home)
            tabs.selectWidget(page)
        return page

    def alone(self):
        """Whether the home tab is the only page, counting notes still waiting for a header."""
        return (self.page is not None and not self.owner.tab_restore
                and all(page is self.page for page in self.owner.tabs.pages() if isValid(page)))

    def can_close(self, page):
        return page is not self.page or not self.alone()

    def refresh(self):
        if self.page is None or not isValid(self.page):
            return
        tabs = self.owner.tabs
        tabs.setTabClosable(self.page, not self.alone())
        pane = tabs.containerFor(self.page)
        if pane is not None and pane.indexOf(self.page) > 0:
            self._to_front.start()  # After a drag, once the tab bar has finished moving tabs.

    def _move_to_front(self):
        if self.page is not None and isValid(self.page):
            self._moving = True
            try:
                self.owner.tabs.moveToFront(self.page, self.TITLE)
            finally:
                self._moving = False

    def tab_closed(self, page):
        """Note a closed tab; returns False for the home page's own move to the front."""
        if page is self.page:
            if self._moving:
                return False
            self.query = self.page.search.query.text()
            self.page.bind(None)
            self.page = None
        if self.owner._binding is Binding.BOUND:
            # Checked later: native tabs report a close before removing the page.
            self._closed.append(page)
            self._empty_check.start()
        return True

    def _show_if_empty(self):
        closed, self._closed = self._closed, []
        owner = self.owner
        if owner._binding is not Binding.BOUND:
            return

        def remaining():
            return [page for page in owner.tabs.pages()
                    if page is not None and isValid(page) and all(page is not c for c in closed)]

        if not remaining():
            owner.tab_restore.insert_next()
        if not remaining():
            self.open(focus=False)


# Saved notes listed in the hamburger menu; All Notes… opens the rest.
MENU_RECENT_NOTES = 5


class Binding(Enum):
    """Which file's notes the widget shows; SWITCHING only inside `set_data`."""
    UNBOUND = 'unbound'
    SWITCHING = 'switching'
    BOUND = 'bound'


class NotesWidget(QWidget):
    addressActivated = Signal(object)

    def __init__(self, data=None, parent=None, *, compact=False, tab_factory=create_note_tabs,
                 state_store=None):
        super().__init__(parent)
        self.setObjectName("bnnotes-notebook")
        # Native tabs use the Window palette; match it around them.
        self.setAutoFillBackground(True)
        self.setMinimumWidth(240)
        self.notebook = None
        self._lease = Lease(self)
        self._data = None
        self._link_previews = HLILPreviewProvider(parent=self)
        self.navigation = NavigationSync()
        self._compact = compact
        self.tab_state = TabState(state_store if state_store is not None else view_settings(),
                                  'sidebar' if compact else 'view')
        self.destroyed.connect(self.tab_state.close)
        keep_focus(self)
        QApplication.instance().aboutToQuit.connect(self._flush_open_state)
        self._pages = {}
        self._binding = Binding.UNBOUND
        self._settings_page = None
        self.onboarding = None  # The welcome tour, while it is open.
        self.home = HomeTab(self)
        self._menu = None
        self._note_actions = {}
        self._whitelist_actions = {}
        self._view_whitelist_action = None
        self._find_keys = FindKeyFilter(self, self.show_find,
                                        lambda: self.current_note is not None
                                        or self.home.page is not None
                                        and self.tabs.currentWidget() is self.home.page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.tabs = tab_factory(self)
        layout.addWidget(self.tabs)
        self.tab_restore = TabRestore(self, self.tabs, lambda: list(self._pages.values()))
        self.tab_restore.finished.connect(self._save_open_state)
        self.tabs.currentChanged.connect(self._current_changed)
        self.tabs.tabClosed.connect(self._tab_closed)
        self.tabs.newNoteRequested.connect(self.new_note)
        self.tab_menus = TabMenus(self)
        self.tabs.contextMenuRequested.connect(self.tab_menus.show)
        self.tabs.orderChanged.connect(self._save_open_state)
        self.tabs.orderChanged.connect(self.home.refresh)

        self.menu_button = ToolbarButton(self, symbol="menu")
        self.menu_button.setToolTip("Notes")
        self.menu_button.setAccessibleName("Notes menu")
        self.menu_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.menu_button.clicked.connect(self._show_menu)
        self.tabs.setCornerWidget(self.menu_button)
        self.set_data(data)

    @property
    def new_button(self):
        return self.tabs.newButton()

    @property
    def current_note(self):
        page = self.tabs.currentWidget()
        return page if isinstance(page, NotepadWidget) else None

    @property
    def editor(self):
        page = self.current_note
        return page.editor if page is not None else None

    def set_data(self, data):
        if (data is not None and self.notebook is not None
                and self.notebook.storage.session_id == data.file.session_id):
            self._data = data
            self._link_previews.set_data(data)
            self._save_open_state()
            return
        self._save_open_state()
        self._binding = Binding.SWITCHING
        self._data = data
        self._link_previews.set_data(data)
        self.tab_menus.reset()
        headerless = set(self.tab_restore.clear())
        auxiliary = self.tabs.currentWidget() if self._auxiliary_selected() else None
        self.navigation.reset(enabled=False)
        if self._menu is not None and isValid(self._menu):
            self._menu.close()
            self._menu.deleteLater()
            self._menu = None
        self._note_actions = {}
        if self.notebook is not None:
            self._whitelist_actions = {}
            self._view_whitelist_action = None
            for signal, slot in self._notebook_connections():
                signal.disconnect(slot)
        for page in self._pages.values():
            if page not in headerless:
                self.tabs.removeTab(page)
            page.release_note()
            page.deleteLater()
        self._pages.clear()
        self.notebook = notebook_for(data) if data is not None else None
        self._lease.take(self.notebook)
        self.tab_state.bind(data)
        if self.notebook is not None:
            for signal, slot in self._notebook_connections():
                signal.connect(slot)
            self.navigation.reset(enabled=self.notebook.preferences.effective(
                'follow_view_navigation'))
            if self.onboarding is None and onboarding_due(self.tab_state.store):
                mark_onboarded(self.tab_state.store)
                self._show_onboarding()
            elif self.onboarding is None:
                self._restore_tabs()
            self.tab_restore.resume()
        if self._settings_page is not None:
            self._settings_page.bind_preferences(self._settings_preferences())
        self.home.bind(self.notebook)
        if auxiliary is not None:
            self.tabs.selectWidget(auxiliary)
        self._binding = Binding.BOUND if self.notebook is not None else Binding.UNBOUND
        if self._binding is Binding.BOUND and self.tabs.currentWidget() is None:
            self.home.open(focus=False)
        self._notes_changed()
        self._current_changed(self.tabs.currentWidget())

    def _restore_tabs(self, *, default=True):
        """Open the file's saved tabs, or with `default` its first note when none are saved."""
        saved = self.tab_state.read()
        fallback = [self.notebook.default_note()] if default else []
        if saved is None or self.notebook.load_failed:
            notes, active = fallback, None
        else:
            identities, active = saved
            notes = [note for identity in identities
                     if (note := self.notebook.note(identity)) is not None]
            if identities and not self.notebook.notes:
                notes = fallback
        self.tab_state.active = active
        for note in notes:
            self.open_note(note, select=False)
        selected = self._pages.get(active) if active is not None else None
        if selected is not None or notes:
            selected = selected or self._pages[notes[0].id]
            self.tab_restore.insert(selected)
            self.tabs.selectWidget(selected)

    def _show_onboarding(self):
        """Show the welcome tour as the notebook's only tab, in place of its notes."""
        page = OnboardingWidget(self, preferences=editor_preferences(), data=self._data)
        # Queued: finishing closes the page whose button is still being handled.
        page.finished.connect(self.finish_onboarding, Qt.ConnectionType.QueuedConnection)
        self.onboarding = page
        self.tabs.addTab(page, OnboardingWidget.TITLE, target=self.tabs.currentContainer())
        self.tabs.selectWidget(page)
        return page

    def open_onboarding(self):
        """Show the welcome tour (Notepad → Open Onboarding), beside any open notes."""
        page = self.onboarding if self.onboarding is not None else self._show_onboarding()
        self.tabs.selectWidget(page)
        page.setFocus()
        return page

    def finish_onboarding(self):
        """Replace the welcome tour with a new note, restoring any saved tabs behind it."""
        page = self.onboarding
        if page is None or not isValid(page):
            return
        self.onboarding = None
        self._leave_onboarding(self.tabs.containerFor(page))
        self.tabs.closeTab(page)

    def _leave_onboarding(self, target=None):
        if self._binding is not Binding.BOUND:
            return
        self._restore_tabs(default=False)
        self.tab_restore.resume()
        # Straight into a new note, ready to type in; the home page if notes can't be added.
        if self.new_note(target) is None:
            self.home.open(target)
        self._save_open_state()

    def _notebook_connections(self):
        notebook = self.notebook
        return ((notebook.notesChanged, self._notes_changed),
                (notebook.noteChanged, self._note_changed),
                (notebook.lastNoteDeleted, self._last_note_deleted),
                (notebook.statusChanged, self._refresh_whitelist_checks),
                (notebook.indexReady, self._index_ready),
                (notebook.preferences.changed, self._navigation_preference_changed))

    def _save_open_state(self):
        state = self.tab_state
        # The tour stands in for the saved tabs until it closes; don't save it over them.
        if (not state.alive or self._binding is not Binding.BOUND or self.onboarding is not None
                or self.tab_restore.inserting or self.notebook.load_failed or not isValid(self)):
            return
        state.refresh_id(self._data)
        if state.view_id is None:
            return
        pages = list(self._pages.values()) if self.tab_restore else list(self.tabs.pages())
        pages.extend(page for page in self._pages.values() if page not in pages)
        identities = tuple(page.session.id for page in pages
                           if isinstance(page, NotepadWidget) and isValid(page)
                           and self._pages.get(page.session.id) is page
                           and page.session in self.notebook.notes)
        current = self.current_note
        state.write(identities, current.session.id if current is not None else None)

    def _flush_open_state(self):
        self._save_open_state()
        self.tab_state.flush()

    def hideEvent(self, event):
        self._save_open_state()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._flush_open_state()
        super().closeEvent(event)
        if event.isAccepted():
            self.tab_state.alive = False

    def showEvent(self, event):
        self.tab_state.alive = True
        super().showEvent(event)
        self._save_open_state()

    def toggle_author_bar(self):
        toggle_author_bar(self._settings_preferences())

    def open_settings(self):
        if self._settings_page is None:
            self._settings_page = NotepadSettingsWidget(
                self, preferences=self._settings_preferences(),
            )
            self.tabs.addTab(self._settings_page, 'Settings', target=self.tabs.currentContainer())
        self.tabs.selectWidget(self._settings_page)
        self._settings_page.setFocus()
        return self._settings_page

    def _settings_preferences(self):
        return self.notebook.preferences if self.notebook is not None else editor_preferences()

    def _auxiliary_selected(self, *, home=True):
        """Whether a page that isn't a note is selected (Settings, the tour, the home page)."""
        page = self.tabs.currentWidget()
        pages = (self._settings_page, self.onboarding) + ((self.home.page,) if home else ())
        return page is not None and page in pages

    def _searching(self):
        """Whether the home page is selected and showing its Search section."""
        page = self.home.page
        return (page is not None and self.tabs.currentWidget() is page
                and page.section == SEARCH)

    def open_home(self, target=None, *, focus=True):
        """Show the Notes home page, adding its tab to `target` (default: the current pane)."""
        return self.home.open(target, focus=focus)

    def can_close(self, page):
        """Every tab can close, except the home tab while nothing else is open."""
        return self.home.can_close(page)

    def open_find_all(self):
        """Show the home page's Search section with its query focused; returns that section."""
        if self.notebook is None or self.notebook.load_failed:
            return None
        page = self.home.open(focus=False)
        page.show_section(SEARCH)
        return page.search

    def _open_search_result(self, snippet, match, *, target=None, index=None):
        """Open a search result's note with the match selected; None if the result is stale."""
        if self.notebook is None:
            return None
        note = self.notebook.note(snippet.note_id)
        if (note is None or note.source_revision != snippet.revision
                or not 0 <= match.start < match.end < note.document.characterCount()):
            if self.home.page is not None:
                self.home.page.search.refresh()
            return None
        page = self.open_note(note, target=target, index=index)
        cursor = QTextCursor(note.document)
        cursor.setPosition(match.start)
        cursor.setPosition(match.end, QTextCursor.MoveMode.KeepAnchor)
        page.editor.show_source_cursor(cursor)
        return page

    def close_tab(self):
        page = self.tabs.currentWidget()
        return self.tabs.closeTab(page) if page is not None and self.can_close(page) else False

    def new_folder(self, parent=None):
        """Prompt for a folder name and add it, showing it in the home page's Folders."""
        folder = create_folder(self, self.notebook, parent)
        if folder is not None and self.home.page is not None:
            self.home.page.show_section(FOLDERS, focus=False)
            self.home.page._select(FOLDER, folder.id)
        return folder

    def new_note(self, target=None, *, index=None):
        if self.notebook is None or self.notebook.load_failed:
            return None
        return self.open_note(self.notebook.new_note(), target=target, index=index)

    def open_note(self, note, *, focus=True, target=None, select=True, index=None):
        if self.notebook is None or note not in self.notebook.notes:
            return None
        page = self._pages.get(note.id)
        if page is None:
            page = NotepadWidget(note, self, compact=self._compact,
                                 on_editor=self._connect_editor)
            page.setObjectName(f"bnnotes-note-{note.id}")
            self._pages[note.id] = page
            if not select:
                self.tab_restore.defer(page)
                self._save_open_state()
                return page
            with page.materialization_held():
                self.tabs.addTab(page, note.title, target=target, index=index)
        elif not select:
            self._save_open_state()
            return page
        self.tab_restore.insert(page)
        self.tabs.selectWidget(page)
        if page.isVisible():
            page.materialize()
        if focus and self._binding is Binding.BOUND:
            page.editor.setFocus()
        self._save_open_state()
        return page

    def close_page(self, page):
        """Close a page's tab, or drop it from the restore queue if it has no header yet."""
        if page in self.tab_restore:
            if not page.close():
                return False
            self._drop_headerless(page)
            return True
        return self.tabs.closeTab(page)

    def _drop_headerless(self, page):
        self.tab_restore.discard(page)
        self._tab_closed(page)
        page.deleteLater()

    def _connect_editor(self, page):
        page.editor.resolve_link = self.resolve_link
        page.editor.set_link_provider(self._link_previews)
        page.editor.addressActivated.connect(self._follow_address)

    def resolve_link(self, url):
        return resolve_in_bv_link(self._data, url)

    def _follow_address(self, offset):
        # Navigation echoes back through this notebook and Sync; ignore it until it changes.
        self.navigation.sent(offset)
        self.addressActivated.emit(offset)

    def navigate_to_address(self, offset, *, function=None):
        navigation = self.navigation
        if not navigation.enabled:
            navigation.waiting = None
            return False
        if self._auxiliary_selected(home=False) or self._searching():
            return False
        if self.notebook is None or self._data is None:
            return False
        if navigation.is_echo(offset):
            return True
        anchors, resolved, pending = [], {}, False
        for note in self.notebook.notes:
            if 'binaryninja:' not in note.source:
                continue
            analysis = note.navigation_analysis()
            if analysis is None:
                pending = True
                continue
            for link in analysis.links:
                if link.url not in resolved:
                    resolved[link.url] = self.resolve_link(link.url)
                address = resolved[link.url]
                if address is not None:
                    anchors.append(AddressAnchor(note.id, link.start, link.end, address, link.url))
        if pending:
            navigation.wait(offset, function)
            return False
        navigation.waiting = None
        current = self.current_note
        anchor = find_address_anchor(
            self._data, anchors, int(offset),
            preferred_note=current.session.id if current is not None else None,
            function=function,
        )
        if anchor is None:
            navigation.anchor = None
            return False
        note = self.notebook.note(anchor.note_id)
        key = anchor.note_id, anchor.position, anchor.address, note.source_revision
        if current is not None and current.session is note and navigation.anchor == key:
            return True
        focused = QApplication.focusWidget()
        restore_focus = focused is not None and not self.isAncestorOf(focused)
        page = self.open_note(note, focus=False)
        cursor = QTextCursor(page.editor.document())
        cursor.setPosition(anchor.position)
        page.editor.show_source_cursor(cursor, focus_table=False)
        if page.session.tables.at_position(anchor.position) is None:
            page.editor.centerCursor()
        navigation.anchor = key
        if restore_focus:
            focused.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def _navigation_preference_changed(self, name):
        if name != 'follow_view_navigation' or self.notebook is None:
            return
        enabled = self.notebook.preferences.effective(name)
        if enabled != self.navigation.enabled:
            self.navigation.reset(enabled=enabled)

    def _index_ready(self, _note):
        if self.navigation.waiting is not None:
            offset, function = self.navigation.waiting
            self.navigate_to_address(offset, function=function)

    def close_note(self):
        if self.current_note is not None:
            return self.tabs.closeTab(self.current_note)
        return False

    def delete_note(self, page=None):
        page, notebook = page or self.current_note, self.notebook
        if page is None or notebook is None or notebook.load_failed:
            return False
        note = page.session
        if note.notebook is not notebook or note not in notebook.notes:
            return False
        was_current = page is self.tabs.currentWidget()
        answer = QMessageBox.question(
            self, "Delete Note",
            f'Permanently delete “{note.title}” from this file?\n\n'
            "It will be removed from every open Notepad view. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if (answer != QMessageBox.StandardButton.Yes or self.notebook is not notebook
                or note not in notebook.notes or not notebook.delete_note(note)):
            return False
        if was_current and self.notebook is notebook and self.current_note is not None:
            self.current_note.editor.setFocus()
        return True

    def _last_note_deleted(self):
        auxiliary = self.tabs.currentWidget() if self._auxiliary_selected() else None
        # The first surface uses the + handler; the others open that same new note.
        if not self.notebook.notes:
            self.new_note()
        else:
            self.open_note(self.notebook.default_note())
        if auxiliary is not None:
            self.tabs.selectWidget(auxiliary)

    def _tab_closed(self, page):
        if not self.home.tab_closed(page):
            return
        if page is self._settings_page:
            self._settings_page = None
        elif page is self.onboarding:
            self.onboarding = None
            self._leave_onboarding()
        elif isinstance(page, NotepadWidget):
            self._pages.pop(page.session.id, None)
            page.release_note()
        if self.tabs.currentWidget() is None:
            self.tab_restore.insert_next()
        self._current_changed(self.tabs.currentWidget())

    def _current_changed(self, page):
        self.home.refresh()
        if self.tab_restore.inserting:
            return
        self.navigation.left_note(page.session.id if isinstance(page, NotepadWidget) else None)
        if (isinstance(page, NotepadWidget) and self._binding is Binding.BOUND
                and self.tab_state.view_id is not None and not self.notebook.load_failed):
            self.tab_state.remember(page.session.id, {note.id for note in self.notebook.notes})
        self.setFocusProxy(page if page is not None else self.new_button)
        self._refresh_menu_checks()
        self._save_open_state()

    def _notes_changed(self):
        editable = self.notebook is not None and not self.notebook.load_failed
        self.tabs.setNewNoteEnabled(editable)
        if self._binding is Binding.BOUND:
            for identity, page in list(self._pages.items()):
                if self.notebook.note(identity) is None:
                    if page in self.tab_restore:
                        self._drop_headerless(page)
                    else:
                        self.tabs.closeTab(page)
            for identity, action in live_actions(self._note_actions):
                if self.notebook.note(identity) is None:
                    action.setEnabled(False)
                    action.setVisible(False)
                    del self._note_actions[identity]
            for note in self.notebook.notes:
                self._note_changed(note)

    def _note_changed(self, note):
        page = self._pages.get(note.id)
        if page is not None:
            self.tabs.setTabText(page, note.title)
        action = live_action(self._note_actions.get(note.id))
        if action is not None:
            self._note_actions[note.id] = action
            action.setText(menu_text(note.title))
        else:
            self._note_actions.pop(note.id, None)
        self._refresh_whitelist_checks()

    def _refresh_whitelist_checks(self):
        if self.notebook is None:
            return
        for identity, action in live_actions(self._whitelist_actions):
            note = self.notebook.note(identity)
            saved = note is not None and note.source != ''
            action.setVisible(saved)
            if saved:
                action.setText(menu_text(note.title))
                with QSignalBlocker(action):
                    action.setChecked(note.mcp_allow is True)
        self._view_whitelist_action = live_action(self._view_whitelist_action)
        if self._view_whitelist_action is not None:
            with QSignalBlocker(self._view_whitelist_action):
                self._view_whitelist_action.setChecked(self.notebook.storage.mcp_allow is not False)

    def _refresh_menu_checks(self):
        current = self.current_note
        for identity, action in live_actions(self._note_actions):
            action.setChecked(current is not None and current.session.id == identity)

    def build_menu(self):
        menu = NoteContextMenu(self)
        editable = self.notebook is not None and not self.notebook.load_failed
        new = menu.addAction("New Note", lambda: self.new_note())
        new.setEnabled(editable)
        menu.addAction('New Note Folder…', lambda _checked=False: self.new_folder()).setEnabled(
            editable)
        menu.addAction('Import Markdown…', self.import_markdown).setEnabled(editable)
        self._note_actions = {}
        notes = self.notebook.saved_notes if self.notebook is not None else []
        menu.addSeparator()
        # The most recent few; the home page lists them all.
        for note in recent_first(notes, self.tab_state.recent())[:MENU_RECENT_NOTES]:
            action = menu.addAction(
                menu_text(note.title),
                lambda _checked=False, note=note: self.open_note(note),
            )
            action.setCheckable(True)
            self._note_actions[note.id] = action
        # A lambda: triggered(checked) must not reach open_home's `target`.
        menu.addAction('All Notes…', lambda: self.open_home()).setEnabled(
            self.notebook is not None)
        if self.notebook is not None and self.notebook.folders:
            menu.addSeparator()
            add_folder_menus(menu, self.notebook, self.tab_state.recent(), self.open_note)
        menu.addSeparator()
        menu.addAction('Find in All Notes', self.open_find_all).setEnabled(editable)
        menu.addSeparator()
        page = self.current_note
        menu.addAction("Find in Note…", self.show_find).setEnabled(page is not None)
        if self._auxiliary_selected():
            menu.addAction("Close Tab", self.close_tab).setEnabled(
                self.can_close(self.tabs.currentWidget()))
        else:
            menu.addAction("Close Note", self.close_note).setEnabled(page is not None)
        menu.addAction("Delete Note…", self.delete_note).setEnabled(page is not None and editable)
        if self.notebook is not None and self.notebook.error and not self.notebook.load_failed:
            menu.addAction("Retry Metadata Write", self.notebook.persist)
        menu.addSeparator()
        self._whitelist_actions = {}
        whitelist = menu.addMenu("MCP Whitelist")
        whitelist.setEnabled(editable)
        view_action = whitelist.addAction('Allow Agents on This View')
        self._view_whitelist_action = view_action
        view_action.setCheckable(True)
        view_action.setToolTip('The root mcp-allow flag. Individual note flags remain independent.')
        if editable:
            notebook = self.notebook
            view_action.triggered.connect(lambda value: notebook.set_mcp_allow(value))
        whitelist.addSeparator()
        for note in notes:
            action = whitelist.addAction(menu_text(note.title))
            action.setCheckable(True)
            action.triggered.connect(
                lambda value, note=note, book=self.notebook: book.set_mcp_allow(value, note),
            )
            self._whitelist_actions[note.id] = action
        if not notes:
            whitelist.addAction('No saved notes').setEnabled(False)
        self._refresh_whitelist_checks()
        menu.addSeparator()
        authors = menu.addAction("Show Author Bar", self.toggle_author_bar)
        authors.setCheckable(True)
        authors.setChecked(self._settings_preferences().get('show_authors'))
        menu.addAction("Settings", self.open_settings)
        menu.addAction("Notes by 0cyn").setEnabled(False)
        self._refresh_menu_checks()
        note_actions, whitelist_actions = self._note_actions, self._whitelist_actions
        menu.destroyed.connect(lambda *_: (note_actions.clear(), whitelist_actions.clear()))
        return menu

    def _show_menu(self):
        if self._menu is not None and isValid(self._menu):
            self._menu.deleteLater()
        self._menu = self.build_menu()
        origin = self.menu_button.mapToGlobal(QPoint(self.menu_button.width(),
                                                    self.menu_button.height()))
        origin.setX(origin.x() - self._menu.sizeHint().width())
        self._menu.popup(origin)

    def import_markdown(self):
        if self.notebook is None or self.notebook.load_failed:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Import Note", "", "Markdown (*.md *.markdown);;All files (*)")
        if not filename:
            return
        try:
            source = Path(filename).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            QMessageBox.warning(self, "Import Note", str(error))
            return
        self.open_note(self.notebook.new_note(source))

    def export_markdown(self, page=None):
        page = page or self.current_note
        if page is not None:
            page.export_markdown(self._link_previews)

    def show_find(self):
        focused = QApplication.focusWidget()
        # Prefer the focused note; current-tab notifications can lag behind focus.
        while focused is not None and focused is not self:
            if isinstance(focused, FindAllNotesWidget) and self.isAncestorOf(focused):
                focused.focus_query()
                return
            if isinstance(focused, NotesHomeWidget) and self.isAncestorOf(focused):
                focused.show_section(SEARCH)
                return
            if isinstance(focused, NotepadWidget) and self.isAncestorOf(focused):
                focused.show_find()
                return
            focused = focused.parentWidget()
        if self.current_note is not None:
            self.current_note.show_find()
        elif self.home.page is not None and self.tabs.currentWidget() is self.home.page:
            self.home.page.show_section(SEARCH)
