from __future__ import annotations

import json
import sys
from weakref import WeakSet

from PySide6.QtCore import (
    QDateTime,
    QFile,
    QLocale,
    QMimeData,
    QModelIndex,
    QPointF,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListView,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from ..storage import view_name
from .common import (
    BUTTON_SIZE,
    NoteContextMenu,
    menu_text,
    mix,
    single_shot,
    theme_icon_color,
)
from .search import FindAllNotesWidget
from .session import recency, recent_first

COMPACT_KEY = 'home/compactMode'
SECTION_KEY = 'home/section'
LAYOUT_KEY = 'home/sectionLayout'
COLLAPSED_KEY = 'home/collapsedFolders'
NEW_FOLDER = 'New Folder'
RECENT, FOLDERS, SEARCH = 0, 1, 2
SECTIONS = (RECENT, FOLDERS, SEARCH)
NOTE_ID = Qt.ItemDataRole.UserRole + 1
EDITED = Qt.ItemDataRole.UserRole + 2
KIND = Qt.ItemDataRole.UserRole + 11
ITEM_ID = Qt.ItemDataRole.UserRole + 12
FOLDER, NOTE = 'folder', 'note'
MIME_TYPE = 'application/x-bnnotes-items'
MENU_FOLDERS = 5
# Section names, as saved layouts know them; Folders shows as "Notes on <file>".
RECENT_NAME, FOLDERS_NAME, SEARCH_NAME = 'Recent Notes', 'Folders', 'Search'
# Binary Ninja's split-tab layout: Folders and Recent Notes over Search.
DEFAULT_LAYOUT = json.dumps({
    'type': 'vertical',
    'first': {'type': 'tabs', 'titles': [FOLDERS_NAME, RECENT_NAME], 'active': FOLDERS_NAME},
    'second': {'type': 'tabs', 'titles': [SEARCH_NAME], 'active': SEARCH_NAME},
    'sizes': [400, 400],
})
ROW_PADDING_X = 16
ROW_PADDING_Y = 9
LINE_GAP = 2
QUICK_OPEN_KEYS = tuple(Qt.Key(Qt.Key.Key_1.value + row) for row in range(9))
ICON_SIZE = 16
# Binary Ninja's Symbols-sidebar folder masks, tried in order.
CLOSED_FOLDER = tuple(f':/icons/images/{name}' for name in (
    'folder-closed.png', 'folder.png', 'folder-closed.svg', 'folder.svg'))
OPEN_FOLDER = tuple(f':/icons/images/{name}' for name in ('folder-open.png', 'folder-open.svg'))


def _resource(paths):
    return next((path for path in paths if QFile.exists(path)), None)


def _pixmap(draw, color, scale):
    pixmap = QPixmap(ICON_SIZE * scale, ICON_SIZE * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.scale(scale, scale)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 1.25)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    draw(painter)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def _draw_note(painter):
    painter.drawRoundedRect(QRectF(3.5, 1.5, 9, 13), 1.75, 1.75)
    for y in (5.25, 8, 10.75):
        painter.drawLine(QPointF(5.75, y), QPointF(10.25, y))


def _draw_folder(opened):
    def draw(painter):
        # The back, with its tab; an open folder's front flap tilts out over it.
        path = QPainterPath(QPointF(1.5, 12.5))
        for x, y in ((1.5, 3.5), (5.75, 3.5), (7.25, 5), (13, 5),
                     (13, 7.5) if opened else (13, 12.5)):
            path.lineTo(x, y)
        if opened:
            path.moveTo(1.5, 12.5)
            for x, y in ((3.75, 7.5), (14.75, 7.5), (12.5, 12.5)):
                path.lineTo(x, y)
        path.closeSubpath()
        painter.drawPath(path)
    return draw


def _masked(path, color):
    """A black-and-white mask image as `color` ink: Binary Ninja's renderer, else our own."""
    render = getattr(sys.modules.get('binaryninjaui'), 'pixmapForBWMaskIcon', None)
    if callable(render):
        pixmap = QPixmap()
        try:
            result = render(path, pixmap, color)
        except (TypeError, ValueError, RuntimeError):
            result = None
        pixmap = result if isinstance(result, QPixmap) else pixmap
        if not pixmap.isNull():
            return pixmap
    mask = QImage(path).convertToFormat(QImage.Format.Format_Grayscale8)
    if mask.isNull():
        return QPixmap()
    image = QImage(mask.size(), QImage.Format.Format_ARGB32)
    image.fill(color)
    image.setAlphaChannel(mask)
    return QPixmap.fromImage(image)


def note_icon(widget):
    icon = QIcon()
    color = theme_icon_color(widget, active=False)
    for scale in (1, 2):
        icon.addPixmap(_pixmap(_draw_note, color, scale))
    return icon


def folder_icon(widget):
    color = theme_icon_color(widget, active=False)
    closed, opened = _resource(CLOSED_FOLDER), _resource(OPEN_FOLDER)
    if closed is not None:
        closed_pixmap = _masked(closed, color)
        opened_pixmap = _masked(opened, color) if opened is not None else closed_pixmap
        if not closed_pixmap.isNull():
            icon = QIcon()
            icon.addPixmap(closed_pixmap, state=QIcon.State.Off)
            icon.addPixmap(opened_pixmap, state=QIcon.State.On)
            return icon
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(_pixmap(_draw_folder(False), color, scale), state=QIcon.State.Off)
        icon.addPixmap(_pixmap(_draw_folder(True), color, scale), state=QIcon.State.On)
    return icon


def ask_folder_name(parent, title, text=''):
    name, accepted = QInputDialog.getText(parent, title, 'Folder name:', text=text)
    name = name.strip() if accepted else ''
    return name or None


def create_folder(parent_widget, notebook, parent=None):
    """Prompt for a name and add a folder (inside `parent`, or at the top level)."""
    if notebook is None or notebook.load_failed:
        return None
    name = ask_folder_name(parent_widget, 'New Note Folder')
    return notebook.new_folder(name, parent) if name else None


def folders_by_path(notebook):
    return sorted(notebook.folders, key=lambda folder: notebook.folder_path(folder).casefold())


def add_move_actions(menu, parent_widget, notebook, note):
    """`Move to Folder ▸` (checked at the note's folder) and `Move to New Folder…`."""
    editable = notebook is not None and not notebook.load_failed
    move = menu.addMenu('Move to Folder')
    move.setEnabled(editable)
    current = notebook.note_folder(note) if notebook is not None else None
    unfiled = move.addAction('No Folder', lambda _checked=False: notebook.move_note(note, None))
    unfiled.setCheckable(True)
    unfiled.setChecked(current is None)
    folders = folders_by_path(notebook) if notebook is not None else []
    if folders:
        move.addSeparator()
    for folder in folders:
        action = move.addAction(
            menu_text(notebook.folder_path(folder)),
            lambda _checked=False, folder=folder: notebook.move_note(note, folder),
        )
        action.setCheckable(True)
        action.setChecked(current is not None and current.id == folder.id)

    def move_to_new():
        folder = create_folder(parent_widget, notebook)
        if folder is not None:
            notebook.move_note(note, folder)

    menu.addAction('Move to New Folder…', lambda _checked=False: move_to_new()).setEnabled(
        editable)
    return move


def folder_recency(notebook, folder, recent):
    """The most recent activity of any note in `folder`, including its subfolders."""
    notes = [note for note in notebook.saved_notes
             if (owner := notebook.note_folder(note)) is not None
             and notebook._contains(folder, owner)]
    return max((recency(note, recent) for note in notes), default=0)


def add_folder_menus(menu, notebook, recent, open_note, limit=MENU_FOLDERS):
    """Submenus for the `limit` most recently active top-level folders."""
    folders = sorted(notebook.child_folders(None),
                     key=lambda folder: -folder_recency(notebook, folder, recent))[:limit]

    def fill(submenu, folder):
        for child in notebook.child_folders(folder.id):
            fill(submenu.addMenu(menu_text(child.name)), child)
        for note in recent_first(notebook.folder_notes(folder), recent):
            submenu.addAction(menu_text(note.title),
                              lambda _checked=False, note=note: open_note(note))
        if submenu.isEmpty():
            submenu.addAction('Empty folder').setEnabled(False)

    for folder in folders:
        fill(menu.addMenu(menu_text(folder.name)), folder)
    return folders


class FolderModel(QStandardItemModel):
    """Folders, then unfiled notes. Drops and renames are requests; the notebook changes and
    the model is rebuilt from it."""

    moveRequested = Signal(list, object)  # [(kind, id)], folder id or None
    groupRequested = Signal(list, str)  # [(kind, id)] dropped on the note with this id
    renameRequested = Signal(str, str)  # folder id, name

    def build(self, notebook, recent, widget):
        self.clear()
        if notebook is None:
            return
        folder_item_icon, note_item_icon = folder_icon(widget), note_icon(widget)

        def add(parent, folder):
            for child in notebook.child_folders(folder.id if folder else None):
                item = QStandardItem(folder_item_icon, child.name)
                item.setData(FOLDER, KIND)
                item.setData(child.id, ITEM_ID)
                item.setToolTip(notebook.folder_path(child))
                parent.appendRow(item)
                add(item, child)
            for note in recent_first(notebook.folder_notes(folder), recent):
                item = QStandardItem(note_item_icon, note.title)
                item.setData(NOTE, KIND)
                item.setData(note.id, ITEM_ID)
                item.setToolTip(note.title)
                parent.appendRow(item)

        add(self.invisibleRootItem(), None)

    def flags(self, index):
        flags = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                 | Qt.ItemFlag.ItemIsDragEnabled)
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        # Notes take drops too: notes dropped on a note make a folder of them.
        flags |= Qt.ItemFlag.ItemIsDropEnabled
        if index.data(KIND) == FOLDER:
            # A note's name is its H1, so only folders rename in place.
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    @staticmethod
    def _items(data):
        if not data.hasFormat(MIME_TYPE):
            return None
        try:
            items = [tuple(item) for item in json.loads(bytes(data.data(MIME_TYPE)).decode())]
        except (ValueError, TypeError):
            return None
        return items if all(len(item) == 2 for item in items) else None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.EditRole and index.data(KIND) == FOLDER:
            name = str(value).strip()
            if not name:
                return False
            if name != index.data(Qt.ItemDataRole.DisplayRole):
                super().setData(index, name, role)
                self.renameRequested.emit(index.data(ITEM_ID), name)
            return True
        return super().setData(index, value, role)

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def supportedDragActions(self):
        return Qt.DropAction.MoveAction

    def mimeTypes(self):
        return [MIME_TYPE]

    def mimeData(self, indexes):
        items = list(dict.fromkeys((index.data(KIND), index.data(ITEM_ID))
                                   for index in indexes if index.isValid()))
        data = QMimeData()
        data.setData(MIME_TYPE, json.dumps(items).encode())
        return data

    def canDropMimeData(self, data, _action, row, _column, parent):
        items = self._items(data)
        if items is None:
            return False
        # A note dropped on itself is no move at all.
        return not (row < 0 and parent.data(KIND) == NOTE
                    and items == [(NOTE, parent.data(ITEM_ID))])

    def dropMimeData(self, data, action, row, column, parent):
        items = self._items(data)
        if items is None or not self.canDropMimeData(data, action, row, column, parent):
            return False
        if row < 0 and parent.data(KIND) == NOTE:
            # Onto a note: notes make a folder with it; folders go into its folder.
            self.groupRequested.emit(items, parent.data(ITEM_ID))
            return False
        # Between rows: into the folder holding them.
        while parent.isValid() and parent.data(KIND) != FOLDER:
            parent = parent.parent()
        target = parent.data(ITEM_ID) if parent.isValid() else None
        self.moveRequested.emit(items, target)
        return False


class _RoomyRows(QStyledItemDelegate):
    """Rows as tall as the Recent Notes list's compact rows."""

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), hint.height() + 10)

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        # The open-folder icon is for expanded folders with something in them.
        if not index.model().hasChildren(index):
            option.state &= ~QStyle.StateFlag.State_Open


class FolderTree(QTreeView):
    """Folders and notes, organized like files: select several, drag, rename, delete."""

    activatedNote = Signal(str)
    contextMenuWanted = Signal(object, object)  # QModelIndex, global position
    deleteWanted = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('bnnotes-home-folders')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHeaderHidden(True)
        self.setItemDelegate(_RoomyRows(self))
        self.setUniformRowHeights(True)
        # Qt's expand animation can't be shortened; folders open and close at once.
        self.setAnimated(False)
        # The platform's rename key (Return on macOS, F2 elsewhere) renames a folder.
        self.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setAutoExpandDelay(600)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda point: self.contextMenuWanted.emit(
                self.indexAt(point), self.viewport().mapToGlobal(point)))
        self.doubleClicked.connect(self._activate)

    def _activate(self, index):
        if index.isValid() and index.data(KIND) == NOTE:
            self.activatedNote.emit(index.data(ITEM_ID))

    def rows(self, parent=None):
        """Every index, depth first, in the order the tree shows them."""
        model, parent = self.model(), parent if parent is not None else QModelIndex()
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            yield index
            yield from self.rows(index)

    def selected_items(self):
        """The selected (kind, id) pairs, top to bottom."""
        selection = self.selectionModel()
        return [(index.data(KIND), index.data(ITEM_ID)) for index in self.rows()
                if selection.isSelected(index)]

    def select_only(self, index):
        """Make `index` current and the whole selection, whatever the selection mode."""
        selection = self.selectionModel()
        selection.setCurrentIndex(index, selection.SelectionFlag.ClearAndSelect)
        self.scrollTo(index)

    def select_items(self, items, current=None):
        selection = self.selectionModel()
        selection.clearSelection()
        wanted = set(items)
        for index in self.rows():
            if (index.data(KIND), index.data(ITEM_ID)) in wanted:
                selection.select(index, selection.SelectionFlag.Select)
        current = self.find(*current) if current is not None else QModelIndex()
        if current.isValid():
            selection.setCurrentIndex(current, selection.SelectionFlag.NoUpdate)
            self.scrollTo(current)

    def keyPressEvent(self, event):
        key, index = event.key(), self.currentIndex()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and index.data(KIND) == NOTE:
            for kind, identity in self.selected_items() or [(NOTE, index.data(ITEM_ID))]:
                if kind == NOTE:
                    self.activatedNote.emit(identity)
            event.accept()
            return
        if key == Qt.Key.Key_F2 and index.data(KIND) == FOLDER:
            self.edit(index)
            event.accept()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_items():
            self.deleteWanted.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def find(self, kind, identity, parent=None):
        return next((index for index in self.rows(parent)
                     if index.data(KIND) == kind and index.data(ITEM_ID) == identity),
                    QModelIndex())


class _Sections(QWidget):
    currentChanged = Signal(int)
    layoutChanged = Signal()  # Sections were split, moved or rearranged.

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pages = []
        self.titles = []  # Each section's name, which saved layouts use.
        self._shown = {}  # Page: the title its tab shows instead of its name.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

    def _page_changed(self, page):
        if page in self.pages:
            self.currentChanged.emit(self.pages.index(page))

    def title(self, index):
        page = self.pages[index]
        return self._shown.get(page, self.titles[index])

    def setSectionTitle(self, index, title):
        """Show section `index` as `title`. Saved layouts keep using its name, so a layout
        saved with one file open restores with another."""
        page = self.pages[index]
        if title == self.titles[index]:
            self._shown.pop(page, None)
        else:
            self._shown[page] = title
        self._show_titles()

    def _show_titles(self):
        pass

    def _named(self, layout):
        """`layout` with shown titles replaced by section names."""
        names = {self._shown[page]: name for page, name in zip(self.pages, self.titles, strict=True)
                 if page in self._shown}
        if not names or not isinstance(layout, str):
            return layout
        try:
            tree = json.loads(layout)
        except ValueError:
            return layout

        def rename(node):
            if isinstance(node, dict):
                return {key: (names.get(value, value) if key == 'active'
                              else [names.get(title, title) for title in value]
                              if key == 'titles' and isinstance(value, list)
                              else rename(value))
                        for key, value in node.items()}
            return node

        return json.dumps(rename(tree))

    def saved_layout(self):
        """The arrangement as text for `restore_layout`, or None if there is nothing to keep."""
        return None

    def restore_layout(self, layout):
        return False


class NativeSections(_Sections):
    """Binary Ninja's split tabs: sections can be rearranged and split like note tabs."""

    def __init__(self, ui, parent=None):
        super().__init__(parent)
        self.ui = ui
        # The collection must outlive its containers, as for the note tabs.
        self.collection = ui.DockableTabCollection()
        self.widget = ui.SplitTabWidget(self.collection)
        self.widget.setTabStyle(ui.GlobalAreaTabStyle())
        self._current = None
        self._watched = WeakSet()
        # Dragging a divider reports every step; keep the size it settles on.
        self._resized = single_shot(self, self.layoutChanged.emit, 250)
        self.widget.currentChanged.connect(self._native_changed)
        self.widget.layoutChanged.connect(self._layout_changed)
        self.layout().addWidget(self.widget)

    def _containers(self):
        return self.widget.findChildren(self.ui.DockableTabWidget)

    def _configure(self):
        # New panes from a split start with native defaults. The sections belong to the
        # home page, which is replaced and rebuilt, so they don't tear off into windows.
        for container in self._containers():
            container.setCanCreateNewWindow(False)
            if container not in self._watched:
                container.tabMoved.connect(lambda *_args: self.layoutChanged.emit())
                self._watched.add(container)
            self._show_titles(container)
            bar = container.tabBar()
            header = bar.parentWidget()
            layout = header.layout() if header is not None else None
            if layout is not None:
                # As tall as a note pane's header, whose + button sets its height.
                margins = layout.contentsMargins()
                header.setMinimumHeight(BUTTON_SIZE + margins.top() + margins.bottom())
                layout.setAlignment(bar, Qt.AlignmentFlag.AlignBottom)
        for splitter in self.widget.findChildren(QSplitter):
            if splitter not in self._watched:
                splitter.splitterMoved.connect(lambda *_args: self._resized.start())
                self._watched.add(splitter)
        for page in self.pages:
            self.widget.setCanCloseTab(page, False)

    def _show_titles(self, only=None, *, names=False):
        for container in (only,) if only is not None else self._containers():
            for page, name in zip(self.pages, self.titles, strict=True):
                index = container.indexOf(page)
                if index >= 0:
                    title = name if names else self._shown.get(page, name)
                    container.setTabText(index, title)
                    container.setTabToolTip(index, title)

    def _layout_changed(self):
        self._configure()
        self.layoutChanged.emit()

    def _native_changed(self, page):
        self._current = page
        self._page_changed(page)

    def addSection(self, page, title):
        self.pages.append(page)
        self.titles.append(title)
        self.widget.addTab(page, title)
        self._configure()

    def saved_layout(self):
        if not isValid(self.widget):
            return None
        layout = self._named(self.widget.savedLayoutString())
        # Mid-teardown or mid-drag layouts can be missing sections; keep the last whole one.
        return layout if layout and all(title in layout for title in self.titles) else None

    def restore_layout(self, layout):
        """Rearrange the sections as saved; a layout missing any of them is ignored."""
        if (not isinstance(layout, str) or not layout
                or not all(title in layout for title in self.titles)):
            return False
        current = self._current
        self._show_titles(names=True)  # Whether it matches tabs by name or by their text.
        self.widget.restoreLayoutString(layout)
        self._configure()
        if current in self.pages:
            self.widget.selectWidget(current)
        return True

    def currentIndex(self):
        page = self._current
        if page not in self.pages:
            page = next((container.currentWidget() for container in self._containers()), None)
        return self.pages.index(page) if page in self.pages else -1

    def setCurrentIndex(self, index):
        self._current = self.pages[index]
        self.widget.selectWidget(self.pages[index])


class StandaloneSections(_Sections):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.widget = QTabWidget(self)
        self.widget.setDocumentMode(True)
        self.widget.setMovable(True)
        self.widget.currentChanged.connect(lambda index: self._page_changed(
            self.widget.widget(index)))
        self.widget.tabBar().tabMoved.connect(lambda *_args: self.layoutChanged.emit())
        self.layout().addWidget(self.widget)

    def addSection(self, page, title):
        self.pages.append(page)
        self.titles.append(title)
        self.widget.addTab(page, title)

    def _show_titles(self):
        for page, name in zip(self.pages, self.titles, strict=True):
            index = self.widget.indexOf(page)
            title = self._shown.get(page, name)
            self.widget.setTabText(index, title)
            self.widget.setTabToolTip(index, title)

    def currentIndex(self):
        return self.pages.index(self.widget.currentWidget()) if self.pages else -1

    def setCurrentIndex(self, index):
        self.widget.setCurrentWidget(self.pages[index])


def create_sections(parent=None):
    ui = sys.modules.get('binaryninjaui')
    return NativeSections(ui, parent) if ui is not None else StandaloneSections(parent)


def wordmark_font(base):
    """The UI typeface in bold, which every platform has without bundling a font."""
    font = QFont(base)
    font.setWeight(QFont.Weight.Bold)
    font.setPixelSize(28)
    return font


def quick_open_row(event):
    """The row Cmd/Ctrl+1-9 opens (Qt's Control is Cmd on macOS), or None."""
    if event.modifiers() != Qt.KeyboardModifier.ControlModifier:
        return None
    return QUICK_OPEN_KEYS.index(event.key()) if event.key() in QUICK_OPEN_KEYS else None


def quick_open_hint(row):
    return QKeySequence(Qt.KeyboardModifier.ControlModifier | QUICK_OPEN_KEYS[row]).toString(
        QKeySequence.SequenceFormat.NativeText)


def edited_label(seconds, now=None):
    if seconds is None:
        return 'No edit time saved'
    locale = QLocale()
    edited = QDateTime.fromSecsSinceEpoch(seconds).toLocalTime()
    now = (QDateTime.fromSecsSinceEpoch(now).toLocalTime() if now is not None
           else QDateTime.currentDateTime())
    days = edited.date().daysTo(now.date())
    time = locale.toString(edited.time(), QLocale.FormatType.ShortFormat)
    if days == 0:
        return f'Edited today at {time}'
    if days == 1:
        return f'Edited yesterday at {time}'
    pattern = 'MMM d' if edited.date().year() == now.date().year() else 'MMM d, yyyy'
    return f'Edited {locale.toString(edited.date(), pattern)} at {time}'


class _WideButton(QPushButton):
    """Long where there is room, shrinking toward its label in a narrow sidebar."""

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(max(hint.width(), 220), hint.height())


def _true(value):
    return value is True or str(value).lower() in ('true', '1')


def _json_list(value):
    try:
        items = json.loads(value) if isinstance(value, str) else []
    except ValueError:
        return []
    return [item for item in items if isinstance(item, str)] if isinstance(items, list) else []


def zebra_stripe(base):
    """Alternate rows: a little darker than `base`, in dark and light themes alike."""
    return mix(base, QColor(0, 0, 0), 0.2 if base.lightness() < 128 else 0.035)


class _NoteRows(QStyledItemDelegate):
    def __init__(self, parent):
        super().__init__(parent)
        self.compact = False

    def sizeHint(self, option, _index):
        lines = 1 if self.compact else 2
        height = QFontMetricsF(option.font).height()
        return QSize(0, round(lines * height + (lines - 1) * LINE_GAP + 2 * self.padding_y))

    @property
    def padding_y(self):
        return ROW_PADDING_Y - 2 if self.compact else ROW_PADDING_Y

    def paint(self, painter, option, index):
        # The theme's Base, as behind Folders and Search; alternate rows a shade darker.
        palette = option.palette
        base = palette.color(QPalette.ColorRole.Base)
        text = palette.color(QPalette.ColorRole.Text)
        rect = QRectF(option.rect)
        state = option.state
        if state & QStyle.StateFlag.State_Selected:
            focused = self.parent().hasFocus()
            background = mix(base, palette.color(QPalette.ColorRole.Highlight),
                             0.55 if focused else 0.3)
        elif state & QStyle.StateFlag.State_MouseOver:
            background = mix(base, text, 0.07)
        elif index.row() % 2:
            background = zebra_stripe(base)
        else:
            background = base
        painter.save()
        painter.fillRect(rect, background)
        metrics = QFontMetricsF(option.font)
        line = metrics.height()
        x, width = rect.left() + ROW_PADDING_X, rect.width() - 2 * ROW_PADDING_X
        top = rect.top() + self.padding_y
        painter.setFont(option.font)
        if index.row() < len(QUICK_OPEN_KEYS):
            hint = quick_open_hint(index.row())
            painter.setPen(mix(base, text, 0.55))
            painter.drawText(QRectF(x, rect.top(), width, rect.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, hint)
            width -= metrics.horizontalAdvance(hint) + ROW_PADDING_X
        painter.setPen(text)
        title = metrics.elidedText(index.data(Qt.ItemDataRole.DisplayRole),
                                   Qt.TextElideMode.ElideRight, width)
        painter.drawText(QRectF(x, top, width, line), Qt.AlignmentFlag.AlignVCenter, title)
        if not self.compact:
            painter.setPen(mix(base, text, 0.55))
            edited = metrics.elidedText(index.data(EDITED), Qt.TextElideMode.ElideRight, width)
            painter.drawText(QRectF(x, top + line + LINE_GAP, width, line),
                             Qt.AlignmentFlag.AlignVCenter, edited)
        painter.restore()


class NoteList(QListView):
    activatedNote = Signal(str)
    contextMenuWanted = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('bnnotes-home-list')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda point: self.contextMenuWanted.emit(self.viewport().mapToGlobal(point)),
        )
        self.rows = _NoteRows(self)
        self.setItemDelegate(self.rows)
        # A click only selects, as in Binary Ninja's welcome screen.
        self.doubleClicked.connect(self._activate)

    def _activate(self, index):
        if index.isValid():
            self.activatedNote.emit(index.data(NOTE_ID))

    def open_row(self, row):
        self._activate(self.model().index(row, 0))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._activate(self.currentIndex())
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if not self.currentIndex().isValid() and self.model().rowCount():
            self.setCurrentIndex(self.model().index(0, 0))

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.model() is not None and self.model().rowCount():
            return
        painter = QPainter(self.viewport())
        palette = self.palette()
        painter.setPen(mix(palette.color(QPalette.ColorRole.Base),
                           palette.color(QPalette.ColorRole.Text), 0.5))
        painter.drawText(self.viewport().rect().adjusted(0, 24, 0, 0),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                         'No saved notes yet')
        painter.end()


class NotesHomeWidget(QWidget):
    noteActivated = Signal(str)
    newNoteRequested = Signal()
    resultActivated = Signal(object, object)  # SearchSnippet, SearchMatch

    def __init__(self, parent=None, *, store):
        super().__init__(parent)
        self.setObjectName('bnnotes-home')
        self.setAutoFillBackground(True)
        self._store = store
        self._notebook = None
        self._recent = dict
        self._stale = True

        self.wordmark = QLabel('Notes', self)
        self.wordmark.setObjectName('bnnotes-home-wordmark')
        self.wordmark.setFont(wordmark_font(self.font()))
        self.new_button = _WideButton('New Note', self)
        self.new_button.setObjectName('bnnotes-home-new-note')
        self.new_button.setMinimumWidth(110)
        self.new_button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.new_button.setFixedHeight(30)
        self.new_button.clicked.connect(self.newNoteRequested.emit)
        self.new_button.installEventFilter(self)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.wordmark, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        header.addWidget(self.new_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.panel = QFrame(self)
        self.panel.setObjectName('bnnotes-home-panel')
        self.panel.setAutoFillBackground(True)
        panel = QVBoxLayout(self.panel)
        panel.setContentsMargins(0, 0, 0, 0)
        panel.setSpacing(0)
        self.sections = create_sections(self.panel)

        self.list = NoteList(self.panel)
        self.list.activatedNote.connect(self.noteActivated.emit)
        self.list.contextMenuWanted.connect(
            lambda position: self._popup(self.build_menu(), position))
        self.model = QStandardItemModel(self)
        self.list.setModel(self.model)
        self.list.installEventFilter(self)
        self.tree = FolderTree(self.panel)
        self.folder_model = FolderModel(self)
        self.tree.setModel(self.folder_model)
        self.tree.activatedNote.connect(self.noteActivated.emit)
        self.tree.contextMenuWanted.connect(
            lambda index, position: self._popup(self.build_folder_menu(index), position))
        self.tree.installEventFilter(self)
        self.tree.deleteWanted.connect(lambda: self.delete_items(self.tree.selected_items()))
        # Queued: a drop or an edit must finish before the tree is rebuilt under it.
        queued = Qt.ConnectionType.QueuedConnection
        self.folder_model.moveRequested.connect(self._move_items, queued)
        self.folder_model.renameRequested.connect(self._rename_folder, queued)
        self.folder_model.groupRequested.connect(self._group_items, queued)
        self._collapsed = set(_json_list(self._store.app_value(COLLAPSED_KEY, None)))
        self.tree.collapsed.connect(lambda index: self._set_collapsed(index, True))
        self.tree.expanded.connect(lambda index: self._set_collapsed(index, False))
        self.search = FindAllNotesWidget(self.panel)
        self.search.resultActivated.connect(self.resultActivated.emit)
        self.sections.addSection(self.list, RECENT_NAME)
        self.sections.addSection(self.tree, FOLDERS_NAME)
        self.sections.addSection(self.search, SEARCH_NAME)
        if not self.sections.restore_layout(self._store.app_value(LAYOUT_KEY, None)):
            self.sections.restore_layout(DEFAULT_LAYOUT)
        self.sections.layoutChanged.connect(self._save_layout)
        self.sections.currentChanged.connect(self._section_changed)
        panel.addWidget(self.sections, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 24)
        layout.setSpacing(18)
        layout.addLayout(header)
        layout.addWidget(self.panel, 1)
        self._menu = None
        self._apply_palette()
        section = str(self._store.app_value(SECTION_KEY, FOLDERS))
        self.show_section(next((known for known in SECTIONS if str(known) == section), FOLDERS),
                          focus=False)

    @property
    def section(self):
        return self.sections.currentIndex()

    def show_section(self, section, *, focus=True):
        """Show Recent Notes, the file's notes (Folders) or Search; the choice is remembered
        application-wide."""
        self.sections.setCurrentIndex(section)
        self._section_changed(section)
        if focus and section == SEARCH:
            self.search.focus_query()
        elif focus:
            self.focusProxy().setFocus()

    def _save_layout(self):
        layout = self.sections.saved_layout()
        if layout is not None:
            self._store.set_app_value(LAYOUT_KEY, layout)

    def _section_changed(self, section):
        self.setFocusProxy({FOLDERS: self.tree, SEARCH: self.search}.get(section, self.list))
        self._store.set_app_value(SECTION_KEY, section)

    @property
    def compact(self):
        return self.list.rows.compact

    def set_compact(self, compact):
        self._store.set_app_value(COMPACT_KEY, bool(compact))
        self._apply_compact()

    def _apply_compact(self):
        compact = _true(self._store.app_value(COMPACT_KEY, False))
        if compact != self.list.rows.compact:
            self.list.rows.compact = compact
            # Uniform rows cache one size; reset the view to measure again.
            self.list.reset()

    def bind(self, notebook, recent=dict):
        if self._notebook is not None and isValid(self._notebook):
            self._notebook.notesChanged.disconnect(self._changed)
            self._notebook.noteChanged.disconnect(self._changed)
            self._notebook.foldersChanged.disconnect(self._changed)
        self._notebook, self._recent = notebook, recent
        if notebook is not None:
            notebook.notesChanged.connect(self._changed)
            notebook.noteChanged.connect(self._changed)
            notebook.foldersChanged.connect(self._changed)
        self.sections.setSectionTitle(
            FOLDERS, f'Notes on {view_name(notebook.storage.data)}' if notebook is not None
            else FOLDERS_NAME)
        editable = notebook is not None and not notebook.load_failed
        self.new_button.setEnabled(editable)
        self.search.bind_notebook(notebook)
        self.refresh()

    def _changed(self, *_args):
        if self.isVisible():
            self.refresh()
        else:
            self._stale = True

    def notes(self):
        """Saved notes, most recently opened or edited first."""
        notebook = self._notebook
        if notebook is None:
            return []
        return recent_first(notebook.saved_notes, self._recent())

    def refresh(self):
        self._stale = False
        self._apply_compact()
        current = self.list.currentIndex().data(NOTE_ID)
        self.model.clear()
        for note in self.notes():
            item = QStandardItem(note.title)
            item.setData(note.id, NOTE_ID)
            item.setData(edited_label(note.edited), EDITED)
            item.setToolTip(note.title)
            self.model.appendRow(item)
            if note.id == current:
                self.list.setCurrentIndex(item.index())
        self._refresh_tree()

    def _refresh_tree(self):
        current = self.tree.currentIndex()
        current = (current.data(KIND), current.data(ITEM_ID)) if current.isValid() else None
        selected = self.tree.selected_items()
        collapsed = set(self._collapsed)
        self.folder_model.build(self._notebook, self._recent() if self._notebook else {},
                                self.tree)
        self.tree.expandAll()
        for identity in collapsed:
            index = self.tree.find(FOLDER, identity)
            if index.isValid():
                self.tree.collapse(index)
        self._collapsed = collapsed
        self.tree.select_items(selected, current)

    def _set_collapsed(self, index, collapsed):
        identity = index.data(ITEM_ID)
        if (identity in self._collapsed) == collapsed:
            return
        (self._collapsed.add if collapsed else self._collapsed.discard)(identity)
        # Application-wide, like the layout; folder ids are unique across files.
        self._store.set_app_value(COLLAPSED_KEY, json.dumps(sorted(self._collapsed)))

    def _items(self, items):
        """The notes and folders named by (kind, id) pairs that still exist."""
        notebook = self._notebook
        if notebook is None:
            return [], []
        notes = [note for kind, identity in items
                 if kind == NOTE and (note := notebook.note(identity)) is not None]
        folders = [folder for kind, identity in items
                   if kind == FOLDER and (folder := notebook.folder(identity)) is not None]
        return notes, folders

    def _editable(self):
        return self._notebook is not None and not self._notebook.load_failed

    def new_folder(self, parent=None, items=()):
        """Add a folder named New Folder in `parent`, holding `items`, and rename it in place."""
        notebook = self._notebook
        if not self._editable():
            return None
        folder = notebook.new_folder(notebook.unique_folder_name(NEW_FOLDER, parent), parent)
        if folder is None:
            return None
        notes, folders = self._items(items)
        if notes or folders:
            notebook.move_items(notes, folders, folder)
        self.show_section(FOLDERS)
        self.refresh()
        index = self.tree.find(FOLDER, folder.id)
        if index.isValid():
            self.tree.select_only(index)
            self.tree.edit(index)
        return folder

    def _select(self, kind, identity):
        index = self.tree.find(kind, identity)
        if index.isValid():
            self.tree.select_only(index)

    def _rename_folder(self, identity, name):
        notebook = self._notebook
        folder = notebook.folder(identity) if notebook is not None else None
        if folder is not None and not notebook.rename_folder(folder, name):
            self._refresh_tree()  # Put the old name back.

    def _move_items(self, items, folder_id):
        notebook = self._notebook
        if not self._editable():
            return
        target = notebook.folder(folder_id) if folder_id is not None else None
        if folder_id is not None and target is None:
            return
        notes, folders = self._items(items)
        notebook.move_items(notes, folders, target)
        if target is not None:
            self._collapsed.discard(target.id)
        self._refresh_tree()
        if items:
            self.tree.select_items(items, items[0])

    def _group_items(self, items, note_id):
        """Notes dropped on a note: a new folder, where that note is, holding all of them."""
        notebook = self._notebook
        note = notebook.note(note_id) if notebook is not None else None
        if note is None or not self._editable():
            return None
        if any(kind != NOTE for kind, _identity in items):
            owner = notebook.note_folder(note)
            self._move_items(items, owner.id if owner is not None else None)
            return None
        group = [(NOTE, note_id)] + [item for item in items if item != (NOTE, note_id)]
        return self.new_folder(notebook.note_folder(note), group)

    def delete_items(self, items):
        """Delete folders (their contents move up) and, once confirmed, notes."""
        notebook = self._notebook
        notes, folders = self._items(items)
        if not self._editable() or not notes and not folders:
            return False
        if notes:
            what = f'“{notes[0].title}”' if len(notes) == 1 else f'{len(notes)} notes'
            answer = QMessageBox.question(
                self, 'Delete Note' if len(notes) == 1 else 'Delete Notes',
                f'Permanently delete {what} from this file?\n\n'
                'Deleted notes are removed from every open Notepad view. This cannot be undone.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes or notebook is not self._notebook:
                return False
        for folder in folders:
            if (current := notebook.folder(folder.id)) is not None:
                notebook.delete_folder(current)
        for note in notes:
            if note in notebook.notes:
                notebook.delete_note(note)
        return True

    def _menu_selection(self, index):
        """Right-clicking an unselected item selects it alone, as in a file browser."""
        selection = self.tree.selectionModel()
        if not index.isValid():
            self.tree.clearSelection()
        elif not selection.isSelected(index):
            self.tree.select_only(index)
        return self.tree.selected_items()

    def build_folder_menu(self, index):
        """The Folders tree's menu for the selection, after a right-click at `index`."""
        menu = NoteContextMenu(self)
        notebook, editable = self._notebook, self._editable()
        items = self._menu_selection(index)
        notes, folders = self._items(items)
        if notes:
            menu.addAction('Open' if len(notes) == 1 else f'Open {len(notes)} Notes',
                           lambda _checked=False: [self.noteActivated.emit(note.id)
                                                   for note in notes])
            menu.addSeparator()
        inside = folders[0] if len(items) == 1 and folders else None
        menu.addAction(NEW_FOLDER, lambda _checked=False: self.new_folder(inside)).setEnabled(
            editable)
        if items:
            count = f'{len(items)} Item' + ('s' if len(items) > 1 else '')
            # Where the first item is, as Finder makes it beside the selection.
            kind, identity = items[0]
            owner = (notebook.note_folder(notebook.note(identity)) if kind == NOTE
                     else notebook.folder(notebook.folder(identity).parent))
            menu.addAction(f'New Folder with Selection ({count})',
                           lambda _checked=False: self.new_folder(owner, items)).setEnabled(
                editable)
            self._add_move_menu(menu, notes, folders).setEnabled(editable)
            menu.addSeparator()
            if len(items) == 1 and folders:
                menu.addAction('Rename', lambda _checked=False: self.tree.edit(
                    self.tree.find(FOLDER, folders[0].id))).setEnabled(editable)
            noun = ('Note' if notes else 'Folder') if not notes or not folders else 'Item'
            label = f'Delete {noun}' if len(items) == 1 else f'Delete {len(items)} {noun}s'
            menu.addAction(label + ('…' if notes else ''),
                           lambda _checked=False: self.delete_items(items)).setEnabled(editable)
        return menu

    def _add_move_menu(self, menu, notes, folders):
        """`Move to Folder ▸` any folder the selection can go into, checked where it all is."""
        notebook = self._notebook
        move = menu.addMenu('Move to Folder')
        places = {note.folder if notebook.note_folder(note) is not None else None
                  for note in notes} | {folder.parent for folder in folders}
        here = next(iter(places)) if len(places) == 1 else False

        def add(text, target):
            action = move.addAction(text, lambda _checked=False: self._move_items(
                [(NOTE, note.id) for note in notes] + [(FOLDER, folder.id) for folder in folders],
                target.id if target is not None else None))
            action.setCheckable(True)
            action.setChecked(here == (target.id if target is not None else None))

        add('No Folder', None)
        targets = [folder for folder in folders_by_path(notebook)
                   if not any(notebook._contains(moving, folder) for moving in folders)]
        if targets:
            move.addSeparator()
        for folder in targets:
            add(menu_text(notebook.folder_path(folder)), folder)
        return move

    def _popup(self, menu, position):
        if self._menu is not None and isValid(self._menu):
            self._menu.deleteLater()
        self._menu = menu
        menu.popup(position)

    def showEvent(self, event):
        super().showEvent(event)
        if self._stale or self.compact != _true(self._store.app_value(COMPACT_KEY, False)):
            self.refresh()

    def build_menu(self):
        menu = NoteContextMenu(self)
        compact = menu.addAction('Compact Mode')
        compact.setCheckable(True)
        compact.setChecked(self.compact)
        compact.toggled.connect(self.set_compact)
        return menu

    def eventFilter(self, watched, event):
        # Cmd/Ctrl+1-9 belong to the page while it has focus, ahead of host shortcuts.
        kind = event.type()
        if (kind in (event.Type.ShortcutOverride, event.Type.KeyPress)
                and (row := quick_open_row(event)) is not None):
            event.accept()
            if kind == event.Type.KeyPress:
                self.list.open_row(row)
            return True
        return super().eventFilter(watched, event)

    def contextMenuEvent(self, event):
        self._popup(self.build_menu(), event.globalPos())
        event.accept()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (event.Type.PaletteChange, event.Type.ApplicationPaletteChange):
            self._apply_palette()
        elif event.type() in (event.Type.FontChange, event.Type.ApplicationFontChange):
            self.wordmark.setFont(wordmark_font(self.font()))

    def _apply_palette(self):
        palette = self.palette()
        window = palette.color(QPalette.ColorRole.Window)
        # The sections sit on the page itself: a tinted panel showed as an outline around
        # their tab bars, beside them and in the divider between split panes.
        panel = QPalette(self.panel.palette())
        panel.setColor(QPalette.ColorRole.Window, window)
        if self.panel.palette() != panel:
            self.panel.setPalette(panel)
        # The list and tree keep the theme's own Base, as Search does: overriding it showed
        # the lighter page color until Binary Ninja's style replaced it after the first paint.


