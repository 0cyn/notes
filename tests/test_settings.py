import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QPlainTextEdit
from shiboken6 import isValid

from src.settings import MemorySettings, Scope
from src.ui.preferences import (
    EditorPreferences,
    NotepadSettingsWidget,
    editor_preferences,
    toggle_author_bar,
)


def action(menu, title):
    return next(item for item in menu.actions() if item.text() == title)


def test_preferences_reject_invalid_values(qapp):
    preferences = EditorPreferences(MemorySettings())
    assert preferences.get('font_size') == 0
    assert preferences.get('word_wrap') is True
    with pytest.raises(ValueError):
        preferences.set('font_size', 200)
    with pytest.raises(ValueError):
        preferences.set('word_wrap', 'false')
    with pytest.raises(ValueError):
        preferences.set('mcp_allow_delete', 'Sometimes')
    with pytest.raises(ValueError):
        preferences.set('mcp_allow_create', 1)


def test_scopes_inherit_override_reset_and_sync_external_writes(notebook, data, qapp):
    from types import SimpleNamespace

    from .conftest import FakeView

    data.file.project = SimpleNamespace(path='/project-a', name='Project A')
    sibling, unrelated = FakeView(), FakeView()
    sibling.file.project = data.file.project
    main, peer, other = notebook(), notebook(sibling), notebook(unrelated)
    settings = main.open_settings()
    page = next(iter(main._pages.values()))
    page.replace_source('Source and undo remain intact')
    before = list(data.writes)
    undo = page.session.document.availableUndoSteps()
    assert [settings.scope_tabs.tabText(i) for i in range(3)] == [
        'Application', 'Project: Project A', f'BinaryView: {data.file.display_name}',
    ]
    assert all(settings.scope_tabs.isTabEnabled(i) for i in range(3))
    settings.default_font.setChecked(False)
    settings.font_size.setValue(18)
    settings.scope_tabs.setCurrentIndex(1)
    assert settings.description.text() == 'Overrides Application settings.'
    assert settings.font_size.value() == 18
    assert settings.origins['font_size'].text() == 'Inherited from Application'
    settings.font_size.setValue(22)
    assert page.session.document.defaultFont().pointSizeF() == 22
    assert peer.current_note.session.document.defaultFont().pointSizeF() == 22
    assert other.current_note.session.document.defaultFont().pointSizeF() == 18
    settings.scope_tabs.setCurrentIndex(2)
    assert data.file.display_name not in settings.description.text()
    settings.font_size.setValue(26)
    settings.word_wrap.setChecked(False)
    assert page.session.document.defaultFont().pointSizeF() == 26
    assert peer.current_note.session.document.defaultFont().pointSizeF() == 22
    assert page.editor.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap
    assert peer.editor.lineWrapMode() == QPlainTextEdit.LineWrapMode.WidgetWidth
    settings.scope_tabs.setCurrentIndex(0)
    assert settings.font_size.value() == 18  # The selected scope, not the effective BV value.
    settings.font_size.setValue(20)
    assert page.session.document.defaultFont().pointSizeF() == 26
    assert other.current_note.session.document.defaultFont().pointSizeF() == 20
    settings.scope_tabs.setCurrentIndex(2)
    settings.reset_buttons['font_size'].click()
    assert settings.font_size.value() == 22
    assert page.session.document.defaultFont().pointSizeF() == 22
    assert not settings.reset_buttons['font_size'].isEnabled()
    hub = editor_preferences()
    hub.backend.write('font_size', 30, data, Scope.PROJECT)
    hub.synchronize()  # Same path used by the native-settings synchronization timer.
    assert settings.font_size.value() == 30
    assert peer.current_note.session.document.defaultFont().pointSizeF() == 30
    assert other.current_note.session.document.defaultFont().pointSizeF() == 20
    settings.restore_defaults.click()
    assert page.editor.lineWrapMode() == QPlainTextEdit.LineWrapMode.WidgetWidth
    assert data.writes == before
    assert page.session.document.availableUndoSteps() == undo


def test_toggle_author_bar_flips_it_where_it_is_decided(data, qapp):
    """The Toggle Author Bar command: the Application preference, unless the file overrides it."""
    application = editor_preferences()
    toggle_author_bar(application)
    assert application.get('show_authors', Scope.APPLICATION) is False
    toggle_author_bar(application)
    assert application.get('show_authors', Scope.APPLICATION) is True

    view = editor_preferences(data)
    view.set('show_authors', False, Scope.VIEW)
    toggle_author_bar(view)
    assert view.state('show_authors') == (True, Scope.DEFAULT)  # Back to inheriting.
    toggle_author_bar(view)
    assert view.get('show_authors', Scope.APPLICATION) is False  # No override: the default.
    assert view.effective('show_authors') is False


def test_settings_menu_opens_one_auxiliary_tab_without_creating_a_note(notebook, data, qapp):
    widget = notebook()
    note = widget.current_note
    note.replace_source('# Settings\n\nA note with the same title.')
    before = list(data.writes)
    action(widget.build_menu(), 'Settings').trigger()
    settings = widget.tabs.currentWidget()
    assert isinstance(settings, NotepadSettingsWidget)
    assert widget.current_note is None and widget.editor is None
    assert widget.tabs.widget.tabText(widget.tabs.widget.indexOf(settings)) == 'Settings'
    assert widget.open_settings() is settings
    assert widget.tabs.widget.count() == 2
    assert list(widget._pages.values()) == [note]
    assert widget.notebook.notes == [note.session]
    assert data.writes == before

    widget.tabs.selectWidget(note)
    assert widget.open_settings() is settings
    menu = widget.build_menu()
    for name in ('Find in Note…', 'Delete Note…'):
        assert not action(menu, name).isEnabled()
    action(menu, 'Close Tab').trigger()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(settings)
    assert widget.current_note is note
    assert data.writes == before
    reopened = widget.open_settings()
    assert isValid(reopened) and reopened is not settings

