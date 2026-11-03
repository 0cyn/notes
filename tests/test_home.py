
import json
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QSettings,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QSplitter,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from src.storage import METADATA_KEY, NoteStorage
from src.ui import home as note_icons
from src.ui import session as session_module
from src.ui.common import BUTTON_SIZE, PersistentViewSettings
from src.ui.home import (
    COLLAPSED_KEY,
    COMPACT_KEY,
    FOLDER,
    FOLDERS,
    ITEM_ID,
    LAYOUT_KEY,
    NOTE,
    RECENT,
    SEARCH,
    NotesHomeWidget,
)
from src.ui.notebook import (
    StandaloneNoteTabs,
)
from src.ui.search import SearchNote, search_notes

from .conftest import NOW, _action, _root, file_view


@pytest.fixture
def clock(monkeypatch):
    time = [NOW]
    monkeypatch.setattr(session_module, 'now', lambda: time[0])
    return time


def _titles(home):
    return [home.model.item(row).text() for row in range(home.model.rowCount())]


def _settle(qapp):
    for _ in range(3):
        qapp.processEvents()


def _row_center(home, row):
    return home.list.visualRect(home.model.index(row, 0)).center()


def _close_action(menu):
    return next(action for action in menu.actions() if action.text() == 'Close Tab')


class _LateRemovalTabs(StandaloneNoteTabs):
    """Like Binary Ninja's tabs: report a close while the page is still in its pane."""

    def closeTab(self, page):
        if self.widget.indexOf(page) < 0 or not page.close():
            return False
        self.tabClosed.emit(page)
        self.removeTab(page)
        page.deleteLater()
        return True


def _tree_rows(tree, parent=None, depth=0):
    model = tree.model()
    parent = parent if parent is not None else QModelIndex()
    rows = []
    for row in range(model.rowCount(parent)):
        index = model.index(row, 0, parent)
        rows.append('  ' * depth + index.data())
        rows.extend(_tree_rows(tree, index, depth + 1))
    return rows


def _home(notebook, data, qapp):
    data.values[METADATA_KEY] = _root()
    widget = notebook()
    widget.resize(800, 500)
    widget.show()
    home = widget.open_home()
    home.show_section(FOLDERS)
    _settle(qapp)
    return widget, home


@pytest.fixture
def answer(monkeypatch):
    """Answer the next folder-name prompts."""
    names = []
    monkeypatch.setattr(QInputDialog, 'getText',
                        lambda *_args, **_kwargs: (names.pop(0), True) if names else ('', False))
    return names


def _alpha(pixmap, x, y):
    return pixmap.toImage().pixelColor(x, y).alpha()


def test_editing_a_note_records_when_in_its_metadata(notebook, data, qapp, clock):
    widget = notebook()
    widget.current_note.replace_source('# First\n')
    assert data.values[METADATA_KEY]['notes'][0]['edited'] == NOW
    clock[0] += 60
    widget.current_note.editor.insertPlainText('more')
    assert data.values[METADATA_KEY]['notes'][0]['edited'] == NOW + 60
    assert NoteStorage(data).load()[0].edited == NOW + 60


def test_closing_the_last_tab_shows_the_home_page(notebook, tmp_path, qapp, clock):
    widget = notebook(file_view(tmp_path / 'home.bndb'))
    widget.show()
    assert widget.home.page is None
    assert widget.close_note()
    _settle(qapp)
    home = widget.home.page
    assert widget.tabs.currentWidget() is home
    assert widget.current_note is None
    assert _titles(home) == ['A', 'B', 'C']
    assert home.new_button.isEnabled()


def test_home_lists_the_most_recently_opened_or_edited_notes_first(
    notebook, tmp_path, qapp, clock,
):
    data = file_view(tmp_path / 'recent.bndb')
    for item, edited in zip(data.values[METADATA_KEY]['notes'], (NOW - 30, NOW - 10, NOW - 20),
                            strict=True):
        item['edited'] = edited
    widget = notebook(data)
    widget.close_note()
    home = widget.open_home()
    clock[0] = NOW - 15  # Opening A counts, though it was edited longest ago.
    widget.open_note(widget.notebook.note('a'))
    widget.tab_state.remember('a', {note.id for note in widget.notebook.notes})
    widget.close_note()
    home = widget.open_home()
    home.refresh()
    assert _titles(home) == ['B', 'A', 'C']
    assert 'Edited' in home.model.item(0).data(Qt.ItemDataRole.UserRole + 2)


def test_a_click_selects_and_a_double_click_replaces_the_home_tab(notebook, tmp_path, qapp):
    widget = notebook(file_view(tmp_path / 'replace.bndb'))
    widget.resize(800, 500)
    widget.show()
    first = widget.current_note
    home = widget.open_home()
    assert widget.tabs.pages() == [home, first]  # The home tab leads its pane.
    _settle(qapp)
    row = _titles(home).index('B')
    viewport = home.list.viewport()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=_row_center(home, row))
    _settle(qapp)
    assert widget.tabs.currentWidget() is home
    assert home.list.selectionModel().isSelected(home.model.index(row, 0))
    QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, pos=_row_center(home, row))
    _settle(qapp)
    opened = widget.current_note
    assert opened.session.title == 'B'
    assert widget.home.page is None
    assert widget.tabs.pages() == [opened, first]  # In the home tab's place.
    assert QApplication.focusWidget() is opened.editor


@pytest.mark.parametrize('key, title', [(Qt.Key.Key_1, 'A'), (Qt.Key.Key_3, 'C')])
def test_command_number_opens_that_entry(notebook, tmp_path, qapp, key, title):
    widget = notebook(file_view(tmp_path / 'keys.bndb'))
    widget.show()
    widget.close_note()
    _settle(qapp)
    home = widget.home.page
    home.setFocus()
    focused = QApplication.focusWidget()
    assert home.isAncestorOf(focused)
    # Claimed before any host shortcut can take it.
    override = QKeyEvent(QEvent.Type.ShortcutOverride, key, Qt.KeyboardModifier.ControlModifier)
    override.ignore()
    QApplication.sendEvent(focused, override)
    assert override.isAccepted()
    QTest.keyClick(focused, Qt.Key.Key_9, Qt.KeyboardModifier.ControlModifier)  # No 9th note.
    _settle(qapp)
    assert widget.home.page is home
    QTest.keyClick(focused, key, Qt.KeyboardModifier.ControlModifier)
    _settle(qapp)
    assert widget.current_note.session.title == title
    assert widget.home.page is None


def test_the_home_tab_cannot_close_while_it_is_the_only_tab(notebook, qapp):
    widget = notebook()
    widget.close_note()
    _settle(qapp)
    home = widget.home.page
    assert not widget.can_close(home)
    assert not widget.close_tab() and not widget.tabs.closeTab(home)
    assert not _close_action(widget.build_menu()).isEnabled()
    assert not _close_action(widget.tab_menus.build(home)).isEnabled()
    bar = widget.tabs.widget.tabBar()
    buttons = [bar.tabButton(0, side) for side in (QTabBar.ButtonPosition.RightSide,
                                                    QTabBar.ButtonPosition.LeftSide)]
    assert not any(button is not None and button.isVisibleTo(bar) for button in buttons)
    assert widget.tabs.pages() == [home]

    # With a note beside it, it closes like any tab.
    note = widget.open_note(widget.notebook.default_note(), focus=False)
    widget.tabs.selectWidget(home)
    assert widget.can_close(home) and _close_action(widget.build_menu()).isEnabled()
    assert widget.close_tab()
    _settle(qapp)
    assert widget.tabs.pages() == [note] and widget.home.page is None


def test_the_home_tab_stays_first_in_its_pane(notebook, tmp_path, qapp):
    widget = notebook(file_view(tmp_path / 'first.bndb'))
    first = widget.current_note
    home = widget.open_home()
    second = widget.open_note(widget.notebook.note('b'), focus=False)
    assert widget.tabs.pages() == [home, first, second]
    widget.tabs.widget.tabBar().moveTab(0, 2)  # Dragged to the end.
    _settle(qapp)
    assert widget.tabs.pages() == [home, first, second]
    assert widget.tabs.currentWidget() is second


def test_find_in_all_notes_is_the_home_pages_search_section(notebook, tmp_path, qapp):
    widget = notebook(file_view(tmp_path / 'search.bndb'))
    widget.resize(800, 500)
    widget.show()
    widget.activateWindow()
    first = widget.current_note
    home = widget.open_home()
    home.show_section(RECENT)
    _settle(qapp)
    # Cmd/Ctrl+F on the home page switches to its Search section.
    QTest.keyClick(home.list, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert home.section == SEARCH and home.search.query.hasFocus()
    home.show_section(RECENT)
    # Find in All Notes reuses the home tab rather than adding one.
    search = widget.open_find_all()
    assert search is home.search and widget.tabs.pages() == [home, first]
    assert home.section == SEARCH and search.query.hasFocus()
    search.query.setText('B')
    _settle(qapp)
    assert notebook(file_view(tmp_path / 'other.bndb')).open_home().section == SEARCH

    # A result opens in the home tab's place, as a note opened from Recent Notes does.
    note = widget.notebook.saved_notes[1]
    snippet = next(snippet for result in search_notes(
        [SearchNote(note.id, note.title, note.source, note.source_revision)], note.title).notes
        for snippet in result.snippets)
    home.resultActivated.emit(snippet, snippet.matches[0])
    _settle(qapp)
    opened = widget.current_note
    assert opened.session is note and widget.home.page is None
    assert widget.tabs.pages() == [opened, first]
    assert widget.open_find_all().query.text() == 'B'


class _FakePane(QWidget):
    """A native DockableTabWidget: one pane of section tabs under a header with its bar."""

    tabMoved = Signal(int, int)

    def __init__(self, parent):
        super().__init__(parent)
        self.pages, self.new_windows, self.tips, self.owner = [], None, {}, None
        self.header = QWidget(self)
        QHBoxLayout(self.header).setContentsMargins(4, 2, 4, 0)
        self.bar = QTabBar(self.header)
        self.header.layout().addWidget(self.bar)

    def tabBar(self):
        return self.bar

    def setCanCreateNewWindow(self, allowed):
        self.new_windows = allowed

    def setCanSplit(self, allowed):
        raise AssertionError('home sections keep native splitting')

    def currentWidget(self):
        return self.pages[0] if self.pages else None

    def indexOf(self, page):
        return self.pages.index(page) if page in self.pages else -1

    def setTabText(self, index, title):
        # Saved layouts name tabs by their text, the stricter of the ways native could.
        self.owner.titles[self.pages[index]] = title

    def setTabToolTip(self, index, tip):
        self.tips[self.pages[index]] = tip


class _FakeSplitTabs(QWidget):
    """Just enough of SplitTabWidget to split panes and save and restore their layout, in
    Binary Ninja's format."""

    currentChanged = Signal(object)
    layoutChanged = Signal()

    def __init__(self, _collection):
        super().__init__()
        QVBoxLayout(self)
        self.splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.layout().addWidget(self.splitter)
        self.panes, self.titles, self.closable = [], {}, {}
        self._add_pane()

    def _add_pane(self):
        pane = _FakePane(self.splitter)
        pane.owner = self
        self.splitter.addWidget(pane)
        self.panes.append(pane)
        return pane

    def setTabStyle(self, _style):
        pass

    def addTab(self, page, title):
        self._place(page, self.panes[0])
        self.titles[page] = title

    def setCanCloseTab(self, page, closable):
        self.closable[page] = closable

    def selectWidget(self, page):
        self.currentChanged.emit(page)

    def _place(self, page, pane):
        for other in self.panes:
            if page in other.pages:
                other.pages.remove(page)
        pane.pages.append(page)
        page.setParent(pane)

    def split(self, page):
        """Drag `page` to a new pane below, as dropping it on a pane's lower edge does."""
        self._place(page, self._add_pane())
        self.layoutChanged.emit()

    def arrangement(self):
        return [[self.titles[page] for page in pane.pages] for pane in self.panes]

    def savedLayoutString(self):
        def tabs(pane):
            titles = [self.titles[page] for page in pane.pages]
            return {'type': 'tabs', 'titles': titles, 'active': titles[0]}

        if len(self.panes) == 1:
            return json.dumps(tabs(self.panes[0]))
        return json.dumps({'type': 'vertical', 'first': tabs(self.panes[0]),
                           'second': tabs(self.panes[1]), 'sizes': self.splitter.sizes()})

    def restoreLayoutString(self, layout):
        layout = json.loads(layout)
        pages = {title: page for page, title in self.titles.items()}
        for page in pages.values():
            page.setParent(self)  # Off the old panes before they go.
        for pane in self.panes:
            pane.deleteLater()
            pane.setParent(None)
        self.panes = []
        for node in (layout['first'], layout['second']) if 'first' in layout else (layout,):
            pane = self._add_pane()
            for title in node['titles']:
                self._place(pages[title], pane)
        if 'sizes' in layout:
            self.splitter.setSizes(layout['sizes'])
        self.layoutChanged.emit()


@pytest.fixture
def native_store(monkeypatch, tmp_path, qapp):
    ui = SimpleNamespace(DockableTabCollection=object, SplitTabWidget=_FakeSplitTabs,
                         GlobalAreaTabStyle=object, DockableTabWidget=_FakePane)
    monkeypatch.setitem(sys.modules, 'binaryninjaui', ui)
    return PersistentViewSettings(QSettings(str(tmp_path / 'state.ini'),
                                            QSettings.Format.IniFormat), qapp)


def test_home_sections_start_with_search_below_the_lists(native_store, qapp):
    home = NotesHomeWidget(store=native_store)
    tabs = home.sections.widget
    assert tabs.arrangement() == [['Folders', 'Recent Notes'], ['Search']]
    assert home.section == FOLDERS
    assert all(tabs.closable[page] is False for page in home.sections.pages)
    # Section tab bars are as tall as a note pane's, whose + button sets its height.
    header = tabs.panes[0].header
    margins = header.layout().contentsMargins()
    assert header.minimumHeight() == BUTTON_SIZE + margins.top() + margins.bottom()
    assert header.layout().itemAt(0).alignment() == Qt.AlignmentFlag.AlignBottom
    home.deleteLater()


def test_home_sections_split_and_keep_their_layout(native_store, qapp):
    store = native_store
    store.set_app_value(LAYOUT_KEY, json.dumps(
        {'type': 'tabs', 'titles': ['Recent Notes', 'Folders', 'Search'], 'active': 'Search'}))
    home = NotesHomeWidget(store=store)
    tabs = home.sections.widget
    assert tabs.arrangement() == [['Recent Notes', 'Folders', 'Search']]
    tabs.split(home.search)
    # The new pane keeps native splitting and stays inside the home page.
    assert [pane.new_windows for pane in tabs.panes] == [False, False]
    layout = [['Recent Notes', 'Folders'], ['Search']]
    assert tabs.arrangement() == layout
    assert json.loads(store.app_value(LAYOUT_KEY))['type'] == 'vertical'
    tabs.panes[0].pages.reverse()
    tabs.panes[0].tabMoved.emit(0, 1)
    assert json.loads(store.app_value(LAYOUT_KEY))['first']['titles'] == [
        'Folders', 'Recent Notes']

    # Dragging the divider is saved once it settles.
    tabs.resize(400, 600)
    tabs.show()
    qapp.processEvents()
    tabs.splitter.setSizes([150, 400])
    tabs.splitter.splitterMoved.emit(150, 1)
    sizes = tabs.splitter.sizes()
    QTest.qWait(400)
    assert json.loads(store.app_value(LAYOUT_KEY))['sizes'] == sizes
    home.deleteLater()

    again = NotesHomeWidget(store=store)
    assert again.sections.widget.arrangement() == [['Folders', 'Recent Notes'], ['Search']]
    assert again.search.parentWidget() is again.sections.widget.panes[1]
    again.deleteLater()
    # A layout without every section (say, from before a section existed) is ignored.
    store.set_app_value(LAYOUT_KEY, json.dumps({'type': 'tabs', 'titles': ['Recent Notes']}))
    fresh = NotesHomeWidget(store=store)
    assert fresh.sections.widget.arrangement() == [['Folders', 'Recent Notes'], ['Search']]
    fresh.deleteLater()


def test_folders_section_is_named_for_the_file(native_store, data, qapp):
    """It reads "Notes on <file>", as Settings names the BinaryView, but saved layouts keep
    calling it Folders so they restore with any file."""
    store = native_store
    home = NotesHomeWidget(store=store)
    tabs = home.sections.widget
    home.bind(session_module.notebook_for(data))
    assert tabs.arrangement() == [['Notes on sample.bin', 'Recent Notes'], ['Search']]
    assert tabs.panes[0].tips[home.tree] == 'Notes on sample.bin'
    assert home.sections.title(FOLDERS) == 'Notes on sample.bin'

    tabs.panes[0].pages.reverse()
    tabs.panes[0].tabMoved.emit(0, 1)
    saved = json.loads(store.app_value(LAYOUT_KEY))
    assert saved['first']['titles'] == ['Recent Notes', 'Folders']
    assert saved['first']['active'] == 'Recent Notes'

    home.sections.restore_layout(store.app_value(LAYOUT_KEY))
    assert tabs.arrangement() == [['Recent Notes', 'Notes on sample.bin'], ['Search']]
    data.file.display_name = ''
    home.bind(session_module.notebook_for(data))
    assert home.sections.title(FOLDERS) == 'Notes on Untitled'
    home.bind(None)
    assert tabs.arrangement() == [['Recent Notes', 'Folders'], ['Search']]
    home.deleteLater()


@pytest.mark.parametrize('base', ['#1a1a1a', '#ffffff'])
def test_alternate_rows_are_a_shade_darker(base):
    stripe = note_icons.zebra_stripe(QColor(base))
    assert stripe.lightness() < QColor(base).lightness()
    assert QColor(base).lightness() - stripe.lightness() < 12


def test_the_lists_share_the_search_sections_background(notebook, tmp_path, qapp):
    widget = notebook(file_view(tmp_path / 'colors.bndb'))
    palette = QPalette(widget.palette())
    palette.setColor(QPalette.ColorRole.Window, QColor('#2f2f2f'))
    palette.setColor(QPalette.ColorRole.Base, QColor('#1a1a1a'))
    widget.setPalette(palette)
    widget.resize(800, 500)
    widget.show()
    home = widget.open_home()
    _settle(qapp)
    # From the first paint: the page color here showed until the host's style replaced it.
    image = home.list.viewport().grab().toImage()
    row = home.list.visualRect(home.model.index(0, 0))
    assert image.pixelColor(4, row.center().y()) == QColor('#1a1a1a')
    for view in (home.list, home.tree, home.search):
        assert view.palette().color(QPalette.ColorRole.Base) == QColor('#1a1a1a')


def test_compact_mode_is_one_line_per_note_and_remembered(notebook, tmp_path, qapp):
    widget = notebook(file_view(tmp_path / 'compact.bndb'))
    widget.resize(800, 500)
    widget.show()
    home = widget.open_home()
    _settle(qapp)
    tall = home.list.visualRect(home.model.index(0, 0)).height()
    compact = next(action for action in home.build_menu().actions()
                   if action.text() == 'Compact Mode')
    assert compact.isCheckable() and not compact.isChecked()
    compact.trigger()
    _settle(qapp)
    assert home.compact
    short = home.list.visualRect(home.model.index(0, 0)).height()
    line = home.list.fontMetrics().height()
    assert short < tall and short < 2 * line
    assert widget.tab_state.store.app_value(COMPACT_KEY) is True

    other = notebook(file_view(tmp_path / 'other.bndb'))
    assert other.open_home().compact
    menu = other.home.page.build_menu()
    assert next(action for action in menu.actions() if action.text() == 'Compact Mode').isChecked()


def test_hamburger_lists_the_five_most_recent_notes_then_all_notes(notebook, data, qapp, clock):
    clock[0] = NOW + 100  # Opening N1 now makes it the most recent.
    data.values[METADATA_KEY] = {'version': 2, 'notes': [
        {'id': f'n{index}', 'text': f'# N{index}\n', 'edited': NOW + index} for index in range(7)
    ]}
    widget = notebook()  # Shows N0, which counts as opening it.
    clock[0] += 1
    widget.open_note(widget.notebook.note('n1'))
    widget.tab_state.remember('n1', {note.id for note in widget.notebook.notes})
    texts = [action.text() for action in widget.build_menu().actions()]
    start = texts.index('All Notes…') - 5
    assert texts[start - 1] == ''  # The group's separator.
    assert texts[start:start + 6] == ['N1', 'N0', 'N6', 'N5', 'N4', 'All Notes…']
    next(action for action in widget.build_menu().actions()
         if action.text() == 'All Notes…').trigger()
    home = widget.tabs.currentWidget()
    assert isinstance(home, NotesHomeWidget)
    assert len(_titles(home)) == 7 and _titles(home)[:3] == ['N1', 'N0', 'N6']


def test_home_appears_when_the_host_reports_the_last_close_before_removing_it(notebook, qapp):
    widget = notebook(tab_factory=_LateRemovalTabs)
    widget.show()
    assert widget.close_note()
    _settle(qapp)
    assert isinstance(widget.tabs.currentWidget(), NotesHomeWidget)
    assert widget.tabs.pages() == [widget.home.page]


def test_folder_operations_persist_and_keep_notes_when_a_folder_is_deleted(notebook, data):
    data.values[METADATA_KEY] = _root()
    book = notebook().notebook
    a, c = book.note('a'), book.note('c')
    fs, iso, kernel = (book.folder(identity) for identity in ('fs', 'iso', 'k'))
    assert book.folder_path(iso) == 'Filesystems / ISO 9660'
    assert book.move_note(c, kernel) and c.folder == 'k'
    assert data.values[METADATA_KEY]['notes'][2]['folder'] == 'k'
    assert book.rename_folder(kernel, ' Kernel notes ')
    assert book.folder('k').name == 'Kernel notes'
    assert not book.move_folder(fs, iso)  # Never into its own subfolder.
    assert book.move_folder(iso, None) and book.folder('iso').parent is None
    assert book.move_folder(book.folder('iso'), book.folder('k'))
    assert book.delete_folder(book.folder('k'))
    # Its note and subfolder move up to the top level; nothing is lost.
    assert c.folder is None and book.folder('iso').parent is None
    assert book.move_note(a, None) and 'folder' not in data.values[METADATA_KEY]['notes'][0]
    assert [folder['id'] for folder in data.values[METADATA_KEY]['folders']] == ['fs', 'iso']
    for folder in list(book.folders):
        assert book.delete_folder(folder)
    assert 'folders' not in data.values[METADATA_KEY]


def test_the_folders_tree_nests_folders_then_notes(notebook, data, qapp):
    _widget, home = _home(notebook, data, qapp)
    assert _tree_rows(home.tree) == [
        'Filesystems', '  ISO 9660', '    B', '  A', 'Kernel', 'C']


@pytest.mark.parametrize('item, target, moved', [
    ((NOTE, 'c'), (FOLDER, 'k'), lambda book: book.note('c').folder == 'k'),
    ((NOTE, 'a'), None, lambda book: book.note('a').folder is None),
])
def test_dragging_items_onto_folders(notebook, data, qapp, item, target, moved):
    widget, home = _home(notebook, data, qapp)
    model = home.folder_model
    source = home.tree.find(*item)
    mime = model.mimeData([source])
    parent = home.tree.find(*target) if target is not None else QModelIndex()
    assert model.canDropMimeData(mime, Qt.DropAction.MoveAction, -1, -1, parent)
    # The view never removes rows itself; the tree is rebuilt from the notebook.
    assert not model.dropMimeData(mime, Qt.DropAction.MoveAction, -1, -1, parent)
    _settle(qapp)
    assert moved(widget.notebook)
    assert home.tree.currentIndex().data(ITEM_ID) == item[1]


def _select(home, *items):
    home.tree.select_items(items, items[0])


def _texts(menu):
    return [action.text() for action in menu.actions()]


def test_several_items_drag_together_in_one_write(notebook, data, qapp, monkeypatch):
    widget, home = _home(notebook, data, qapp)
    book = widget.notebook
    assert home.tree.selectionMode() == home.tree.SelectionMode.ExtendedSelection
    _select(home, (FOLDER, 'fs'), (NOTE, 'a'), (NOTE, 'c'))
    writes = []
    save = book._save_notes
    monkeypatch.setattr(book, '_save_notes', lambda *args, **kwargs: (
        writes.append(1), save(*args, **kwargs))[1])
    model = home.folder_model
    mime = model.mimeData(home.tree.selectedIndexes())
    model.dropMimeData(mime, Qt.DropAction.MoveAction, -1, -1, home.tree.find(FOLDER, 'k'))
    _settle(qapp)
    assert len(writes) == 1
    # A, inside the moving Filesystems, goes along with it rather than out of it.
    assert book.folder('fs').parent == 'k' and book.note('a').folder == 'fs'
    assert book.note('c').folder == 'k'
    assert _tree_rows(home.tree) == [
        'Kernel', '  Filesystems', '    ISO 9660', '      B', '    A', '  C']
    assert home.tree.selected_items() == [(FOLDER, 'fs'), (NOTE, 'a'), (NOTE, 'c')]
    # Never into itself: nothing moves.
    mime = model.mimeData([home.tree.find(FOLDER, 'fs'), home.tree.find(NOTE, 'c')])
    model.dropMimeData(mime, Qt.DropAction.MoveAction, -1, -1, home.tree.find(FOLDER, 'iso'))
    _settle(qapp)
    assert book.folder('fs').parent == 'k' and book.note('c').folder == 'k'


def test_notes_dropped_on_a_note_make_a_folder_of_them(notebook, data, qapp):
    widget, home = _home(notebook, data, qapp)
    book, model, tree = widget.notebook, home.folder_model, home.tree
    assert not tree.isAnimated()  # Folders open and close at once.
    b = tree.find(NOTE, 'b')
    mime = model.mimeData([tree.find(NOTE, 'b')])
    assert not model.canDropMimeData(mime, Qt.DropAction.MoveAction, -1, -1, b)
    mime = model.mimeData([tree.find(NOTE, 'a'), tree.find(NOTE, 'c')])
    assert model.canDropMimeData(mime, Qt.DropAction.MoveAction, -1, -1, b)
    assert not model.dropMimeData(mime, Qt.DropAction.MoveAction, -1, -1, b)
    _settle(qapp)
    group = book.note_folder(book.note('b'))
    # Where the note was, holding it and the dropped notes, and named in place.
    assert group.name == 'New Folder' and group.parent == 'iso'
    assert {note.id for note in book.folder_notes(group)} == {'a', 'b', 'c'}
    assert tree.state() == tree.State.EditingState
    assert tree.currentIndex().data(ITEM_ID) == group.id
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Escape)
    _settle(qapp)
    # A folder dropped on a note goes into that note's folder instead.
    mime = model.mimeData([tree.find(FOLDER, 'k')])
    model.dropMimeData(mime, Qt.DropAction.MoveAction, -1, -1, tree.find(NOTE, 'a'))
    _settle(qapp)
    assert book.folder('k').parent == group.id and book.note_folder(book.note('a')) is group
    # Between rows, a drop goes into the folder holding them.
    mime = model.mimeData([tree.find(NOTE, 'a')])
    model.dropMimeData(mime, Qt.DropAction.MoveAction, 0, 0, tree.find(FOLDER, 'fs'))
    _settle(qapp)
    assert book.note('a').folder == 'fs'


def test_the_folder_menu_acts_on_the_selection(notebook, data, qapp):
    _widget, home = _home(notebook, data, qapp)
    _select(home, (NOTE, 'a'), (NOTE, 'c'))
    menu = home.build_folder_menu(home.tree.find(NOTE, 'c'))
    assert _texts(menu) == ['Open 2 Notes', '', 'New Folder',
                            'New Folder with Selection (2 Items)', 'Move to Folder', '',
                            'Delete 2 Notes…']
    # Right-clicking outside the selection selects that item alone, as in a file browser.
    menu = home.build_folder_menu(home.tree.find(FOLDER, 'fs'))
    assert home.tree.selected_items() == [(FOLDER, 'fs')]
    assert _texts(menu) == ['New Folder', 'New Folder with Selection (1 Item)',
                            'Move to Folder', '', 'Rename', 'Delete Folder']
    # A folder can't go into itself or anything inside it.
    assert _texts(_action(menu, 'Move to Folder').menu()) == ['No Folder', '', 'Kernel']
    assert _action(_action(menu, 'Move to Folder').menu(), 'No Folder').isChecked()
    assert _texts(home.build_folder_menu(QModelIndex())) == ['New Folder']
    assert home.tree.selected_items() == []


def test_new_folder_with_selection_holds_it_and_renames_in_place(notebook, data, qapp):
    widget, home = _home(notebook, data, qapp)
    book = widget.notebook
    book.new_folder('New Folder', book.folder('fs'))
    _select(home, (NOTE, 'a'), (FOLDER, 'iso'))
    _action(home.build_folder_menu(home.tree.find(NOTE, 'a')),
            'New Folder with Selection (2 Items)').trigger()
    _settle(qapp)
    created = book.note_folder(book.note('a'))
    # Beside the first item, named apart from the folder already there.
    assert created.name == 'New Folder 2' and created.parent == 'fs'
    assert book.folder('iso').parent == created.id
    assert home.tree.state() == home.tree.State.EditingState
    assert home.tree.currentIndex().data(ITEM_ID) == created.id
    editor = QApplication.focusWidget()
    editor.setText('Volume')
    QTest.keyClick(editor, Qt.Key.Key_Return)
    _settle(qapp)
    assert book.folder(created.id).name == 'Volume'
    # An empty name keeps the old one.
    index = home.tree.find(FOLDER, created.id)
    assert not home.folder_model.setData(index, '  ')
    QTest.keyClick(home.tree, Qt.Key.Key_F2)
    assert home.tree.state() == home.tree.State.EditingState


def test_delete_removes_folders_and_confirms_notes(notebook, data, qapp, monkeypatch):
    widget, home = _home(notebook, data, qapp)
    book = widget.notebook
    answers = []
    monkeypatch.setattr(QMessageBox, 'question',
                        lambda *_args, **_kwargs: answers.pop(0))
    _select(home, (FOLDER, 'iso'))
    QTest.keyClick(home.tree, Qt.Key.Key_Delete)
    # Its note moves up; nothing asked, nothing lost.
    assert book.folder('iso') is None and book.note('b').folder == 'fs'
    _settle(qapp)
    _select(home, (NOTE, 'b'), (FOLDER, 'k'))
    answers.append(QMessageBox.StandardButton.No)
    QTest.keyClick(home.tree, Qt.Key.Key_Backspace)
    assert book.note('b') is not None and book.folder('k') is not None
    answers.append(QMessageBox.StandardButton.Yes)
    _action(home.build_folder_menu(home.tree.find(NOTE, 'b')), 'Delete 2 Items…').trigger()
    assert book.note('b') is None and book.folder('k') is None


def test_collapsed_folders_stay_collapsed(notebook, data, qapp):
    widget, home = _home(notebook, data, qapp)
    home.tree.collapse(home.tree.find(FOLDER, 'fs'))
    assert json.loads(widget.tab_state.store.app_value(COLLAPSED_KEY)) == ['fs']
    again = NotesHomeWidget(store=widget.tab_state.store)
    again.bind(widget.notebook)
    assert _tree_rows(again.tree)[:2] == ['Filesystems', '  ISO 9660']
    assert not again.tree.isExpanded(again.tree.find(FOLDER, 'fs'))
    assert again.tree.isExpanded(again.tree.find(FOLDER, 'iso'))
    again.tree.expand(again.tree.find(FOLDER, 'fs'))
    assert json.loads(widget.tab_state.store.app_value(COLLAPSED_KEY)) == []


def test_a_note_tab_moves_to_a_folder_or_a_new_one(notebook, data, answer):
    data.values[METADATA_KEY] = _root()
    widget = notebook()
    page = widget.open_note(widget.notebook.note('c'))
    menu = widget.tab_menus.build(page)
    move = _action(menu, 'Move to Folder').menu()
    assert [action.text() for action in move.actions()] == [
        'No Folder', '', 'Filesystems', 'Filesystems / ISO 9660', 'Kernel']
    assert _action(move, 'No Folder').isChecked()
    _action(move, 'Filesystems / ISO 9660').trigger()
    assert page.session.folder == 'iso'
    move = _action(widget.tab_menus.build(page), 'Move to Folder').menu()
    assert _action(move, 'Filesystems / ISO 9660').isChecked()
    answer.append('Triage')
    _action(widget.tab_menus.build(page), 'Move to New Folder…').trigger()
    assert widget.notebook.note_folder(page.session).name == 'Triage'
    # Cancelling the prompt leaves the note where it is.
    _action(widget.tab_menus.build(page), 'Move to New Folder…').trigger()
    assert widget.notebook.note_folder(page.session).name == 'Triage'


def test_folder_masks_are_inked_white_on_black(tmp_path):
    mask = QImage(8, 8, QImage.Format.Format_RGB32)
    mask.fill(Qt.GlobalColor.black)
    for x in range(4):
        for y in range(8):
            mask.setPixelColor(x, y, QColor('white'))
    path = str(tmp_path / 'folder.png')
    mask.save(path)
    pixmap = note_icons._masked(path, QColor('#ff8000'))
    assert _alpha(pixmap, 1, 1) == 255 and _alpha(pixmap, 6, 1) == 0
    assert pixmap.toImage().pixelColor(1, 1).name() == '#ff8000'
