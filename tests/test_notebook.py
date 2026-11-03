
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from src.storage import METADATA_KEY, NoteStorage
from src.ui.common import (
    NoteContextMenu,
    PersistentViewSettings,
)
from src.ui.home import RECENT

from .conftest import FakeView, file_view, wait_until


def tab_title(widget, page):
    tabs = widget.tabs.widget
    return tabs.tabText(tabs.indexOf(page))


def close_widget(widget, qapp):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def action(menu, label):
    return next(item for item in menu.actions() if item.text() == label)


def notes(widget, *texts):
    pages = []
    for text in texts:
        page = widget.current_note if not pages else widget.new_note()
        page.replace_source(text)
        pages.append(page)
    return pages


def whitelist(widget):
    menu = widget.build_menu()
    action = next(item for item in menu.actions() if item.text() == "MCP Whitelist")
    actions = menu.actions()
    index = actions.index(action)
    assert actions[index - 1].isSeparator() and actions[index + 1].isSeparator()
    return action.menu()


def test_add_notes_and_type_updates_collection_without_clobbering_other_notes(notebook, data, qapp):
    widget = notebook()
    first = widget.current_note
    first.replace_source("# First\n\nOriginal")
    QTest.mouseClick(widget.new_button, Qt.MouseButton.LeftButton)
    second = widget.current_note
    assert second is not first
    assert second.session.title == "Note 2"
    second.replace_source("# Second\n\nBody")
    assert [note["text"] for note in data.values[METADATA_KEY]['notes']] == [
        "# First\n\nOriginal", "# Second\n\nBody",
    ]
    assert first.editor.document() is not second.editor.document()
    qapp.processEvents()
    assert tab_title(widget, first) == "First"
    assert tab_title(widget, second) == "Second"
    second.editor.undo()
    assert second.session.source == ""
    assert first.session.source == "# First\n\nOriginal"
    assert [note["text"] for note in data.values[METADATA_KEY]['notes']] == [first.session.source]
    second.editor.redo()
    assert len(data.values[METADATA_KEY]['notes']) == 2


def test_first_h1_title_updates_live_and_ignores_code_and_other_heading_levels(notebook, qapp):
    widget = notebook()
    page = widget.current_note
    page.replace_source("## Not the title\n\n```\n# Code\n```\n\n# First 🙂\n\n# Later")
    qapp.processEvents()
    assert tab_title(widget, page) == "First 🙂"
    cursor = page.editor.document().find("First 🙂")
    page.editor.setTextCursor(cursor)
    QTest.keyClicks(page.editor, "Renamed")
    qapp.processEvents()
    assert tab_title(widget, page) == "Renamed"
    page.replace_source("## Only an H2")
    qapp.processEvents()
    assert tab_title(widget, page) == "Note 1"


def test_closed_note_reopens_from_modern_grouped_menu_with_undo_intact(notebook, qapp):
    widget = notebook()
    first = widget.current_note
    first.replace_source("# Saved\n\nOne")
    first.editor.insertPlainText(" two")
    note = first.session
    qapp.processEvents()
    assert widget.close_note()
    assert not widget._pages
    assert widget.notebook.notes == [note]
    menu = widget.build_menu()
    assert isinstance(menu, NoteContextMenu)
    actions = menu.actions()
    assert actions[0].text() == "New Note"
    assert [action.text() for action in actions[1:3]] == ['New Note Folder…', 'Import Markdown…']
    assert [index for index, action in enumerate(actions) if action.isSeparator()] == [
        3, 6, 8, 12, 14,
    ]
    assert actions[4].text() == "Saved"
    assert actions[5].text() == 'All Notes…'
    assert actions[-1].text() == "Notes by 0cyn"
    assert not actions[-1].isEnabled()
    actions[4].trigger()
    reopened = widget.current_note
    assert reopened is not first
    assert reopened.session is note
    reopened.editor.undo()
    assert note.source == "# Saved\n\nOne"


def test_closing_an_empty_note_discards_it_and_renumbers_untitled_notes(notebook, data, qapp):
    widget = notebook()
    first = widget.current_note
    second = widget.new_note()
    second.replace_source("body")
    assert second.session.title == "Note 2"
    discarded = first.session
    discarded_status = []
    discarded.statusChanged.connect(lambda: discarded_status.append(True))
    widget.tabs.closeTab(first)
    qapp.processEvents()
    assert len(widget.notebook.notes) == 1
    assert tab_title(widget, second) == "Note 1"
    assert [item["text"] for item in data.values[METADATA_KEY]['notes']] == ["body"]
    # A disposed empty note must not receive later notebook status broadcasts;
    # its forwarded signal becomes invalid when Qt destroys the note session.
    assert not discarded_status
    second.replace_source("")
    widget.close_note()
    assert not widget.notebook.notes
    assert data.values[METADATA_KEY]['notes'] == []
    menu = widget.build_menu()
    assert sum(action.isSeparator() for action in menu.actions()) == 5
    assert not menu.actions()[-1].isEnabled()
    assert widget.new_note().session.title == "Note 1"
    assert not discarded_status


def test_switching_files_restores_open_notes_and_never_mixes_collections(notebook, data, qapp):

    other_data = FakeView()
    widget = notebook()
    first = widget.current_note
    first.replace_source("first file")
    first_id = first.session.id
    second = widget.new_note()
    second.replace_source("second note")
    second_id = second.session.id
    widget.tabs.closeTab(first)
    widget.set_data(other_data)
    widget.current_note.replace_source("other file")
    assert [item['text'] for item in data.values[METADATA_KEY]['notes']] == [
        'first file', 'second note',
    ]
    widget.set_data(data)
    qapp.processEvents()
    assert list(widget._pages) == [second_id]
    assert widget.current_note.session.id == second_id
    assert widget.notebook.note(first_id).source == "first file"
    assert [item["text"] for item in other_data.values[METADATA_KEY]['notes']] == ["other file"]


def test_invalid_collection_is_preserved_and_cannot_create_notes(notebook, data):
    invalid = {'version': 2, 'notes': [{'id': 'invalid', 'text': 123}]}
    data.values[METADATA_KEY] = invalid
    widget = notebook()
    assert widget.current_note.editor.isReadOnly()
    assert not widget.new_button.isEnabled()
    assert not widget.build_menu().actions()[0].isEnabled()
    assert widget.new_note() is None
    assert not next(action for action in widget.build_menu().actions()
                    if action.text() == "Delete Note…").isEnabled()
    assert not widget.delete_note()
    assert data.values[METADATA_KEY] is invalid
    assert not data.writes


def test_delete_note_removes_its_identity_from_all_surfaces_and_storage(
    notebook, data, qapp, monkeypatch,
):
    widget = notebook()
    first = widget.current_note
    first.replace_source("# Same\n\nDelete this note")
    note = first.session
    second = widget.new_note()
    second.replace_source("# Same\n\nKeep this note")
    second.editor.insertPlainText("!")
    sidebar = notebook(compact=True)
    sidebar.open_note(second.session)
    sidebar.open_note(note)
    other_menu = sidebar.build_menu()
    stale_action = sidebar._note_actions[note.id]
    widget.open_note(note)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    action = next(action for action in widget.build_menu().actions()
                  if action.text() == "Delete Note…")
    assert action.isEnabled()
    action.trigger()
    qapp.processEvents()

    for surface in (widget, sidebar):
        assert note.id not in surface._pages
        assert surface.notebook.note(note.id) is None
        assert surface.current_note.session is second.session
        assert surface.open_note(note) is None
    assert not stale_action.isVisible()
    assert not stale_action.isEnabled()
    stale_action.trigger()
    assert sidebar.current_note.session is second.session
    assert len([action for action in other_menu.actions() if action.isCheckable()
                and action.isVisible() and action.text() != 'Show Author Bar']) == 1
    assert [entry.id for entry in NoteStorage(data).load()] == [second.session.id]
    assert not note._views
    second.editor.undo()
    assert second.session.source == "# Same\n\nKeep this note"


def test_reopen_restores_exact_open_ids_order_and_active_note(notebook, qapp, tmp_path):
    path = tmp_path / 'analysis.bndb'
    data = file_view(path)
    widget = notebook(data)
    assert list(widget._pages) == ['a']
    first = widget.current_note
    third = widget.open_note(widget.notebook.note('c'))
    widget.open_note(widget.notebook.note('b'))
    widget.tabs.closeTab(first)
    widget.tabs.widget.tabBar().moveTab(1, 0)  # b, c
    widget.tabs.selectWidget(third)
    widget.open_settings()  # The last selected note remains the restoration target.
    state_file = widget.tab_state.store.backend.fileName()
    close_widget(widget, qapp)
    fresh_store = PersistentViewSettings(QSettings(state_file, QSettings.IniFormat), qapp)
    reopened = notebook(file_view(path), state_store=fresh_store)
    assert list(reopened._pages) == ['b', 'c']
    assert reopened.current_note.session.id == 'c'
    wait_until(lambda: not reopened.tab_restore)
    assert [page.session.id for page in reopened.tabs.pages()] == ['b', 'c']
    assert not reopened.notebook.note('a').materialized
    assert not reopened.notebook.note('b').materialized


@pytest.mark.parametrize('label, expected', [
    ('Close Tabs to the Left', ['A', 'D']),
    ('Close Tabs to the Right', ['B', 'C', 'A']),
])
def test_directional_closing_uses_reordered_tab_positions(notebook, qapp, label, expected):
    widget = notebook()
    pages = notes(widget, '# A', '# B', '# C', '# D')
    widget.tabs.widget.tabBar().moveTab(0, 2)  # B, C, A, D
    action(widget.tab_menus.build(pages[0]), label).trigger()
    wait_until(lambda: not widget.tab_menus._closing)
    actual = widget.tabs.pagesIn(widget.tabs.currentContainer())
    assert [page.session.source.removeprefix('# ') for page in actual] == expected
    assert len(widget.notebook.notes) == 4


def test_duplicate_cold_note_uses_new_id_default_permission_and_independent_undo(notebook, data):
    source = '# Duplicate\n\nOriginal **source** 🙂'
    data.values[METADATA_KEY] = {'version': 2, 'notes': [
        {'id': 'original', 'text': source, 'mcp-allow': True},
    ]}
    widget = notebook()
    original = widget.current_note
    assert not original.session.materialized
    action(widget.tab_menus.build(original), 'Duplicate Note').trigger()
    duplicate = widget.current_note
    assert duplicate is not original
    assert duplicate.session.id != original.session.id
    assert duplicate.session.source == source
    assert duplicate.session.mcp_allow is False
    assert not original.session.materialized
    duplicate.editor.insertPlainText('changed')
    duplicate.editor.undo()
    assert duplicate.session.source == original.session.source == source


def test_navigation_opens_closed_matching_note_without_stealing_focus_or_writing(
    notebook, link_view, qapp,
):
    widget = notebook(link_view)
    widget.notebook.preferences.set('follow_view_navigation', True)
    first = widget.current_note
    first.replace_source('# First\n\n[first](binaryninja://?expr=first)')
    second = widget.new_note()
    second.replace_source('# Second\n\n' + 'Context line\n' * 70
                          + '[second](binaryninja://?expr=second)')
    target = second.session
    widget.close_note()
    widget.resize(700, 350)
    widget.show()
    outside = QLineEdit('Disassembly focus stand-in')
    outside.show()
    outside.activateWindow()
    outside.setFocus()
    qapp.processEvents()
    writes = len(link_view.writes)
    try:
        assert widget.navigate_to_address(link_view.start + 0x1004)
        assert widget.current_note.session is target
        assert QApplication.focusWidget() is outside
        assert widget.editor.textCursor().position() == target.highlighter.analysis.links[0].start
        assert widget.editor.verticalScrollBar().value() > 0
        assert len(link_view.writes) == writes
        assert not widget.navigate_to_address(link_view.start + 0x2000)
        assert widget.current_note.session is target
        # Navigation leaves the home page, but not while it is searching.
        widget.open_find_all()
        assert not widget.navigate_to_address(link_view.start + 0x1004)
        assert widget.tabs.currentWidget() is widget.home.page
        widget.home.page.show_section(RECENT, focus=False)
        assert widget.navigate_to_address(link_view.start + 0x1004)
        assert widget.current_note.session is target
    finally:
        outside.close()
        outside.deleteLater()



def test_hamburger_toggles_the_author_bar(notebook, qapp):
    widget = notebook()
    preferences = widget._settings_preferences()
    action = next(item for item in widget.build_menu().actions()
                  if item.text() == 'Show Author Bar')
    assert action.isChecked()
    action.trigger()
    assert preferences.get('show_authors') is False
