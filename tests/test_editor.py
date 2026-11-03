from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QFontInfo,
    QKeySequence,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.markdown import serialize_table, utf16_length
from src.storage import METADATA_KEY
from src.ui.editor import CLAUDE_HUE, OPENAI_HUE, PEOPLE_HUES


def open_selection(notepad, qapp, text, selected, *, cell=False, backward=False):
    widget = notepad()
    widget.replace_source(serialize_table([["Header"], [text]]) if cell else text)
    widget.resize(900, 600)
    widget.show()
    qapp.processEvents()
    card = next(iter(widget.editor.table_cards.values())) if cell else None
    if card is not None:
        card.focus_cell(1, 0, edit=True)
    editor = card.cell_editor if card is not None else widget.editor
    editor.setFocus()
    start = utf16_length(text[:text.index(selected)]) if selected else 0
    end = start + utf16_length(selected)
    cursor = editor.textCursor()
    cursor.setPosition(end if backward else start)
    cursor.setPosition(start if backward else end, QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)
    return widget, editor, card


def open_menu_editor(notepad, qapp, text="🙂 words", *, surface="note"):
    widget = notepad()
    widget.replace_source(text if surface == "note" else serialize_table([
        ["Header", "Other"], [text, "untouched"],
    ]) + "\n\nAfter table")
    widget.resize(1000, 700)
    widget.show()
    qapp.processEvents()
    if surface == "note":
        target = widget.editor
        target.setFocus()
        target.selectAll()
    else:
        card = next(iter(widget.editor.table_cards.values()))
        card.focus_cell(1, 0, edit=surface == "cell")
        qapp.processEvents()
        target = card.cell_editor if surface == "cell" else card
        if surface == "cell":
            target.selectAll()
    return widget, target


def action(menu, *path):
    found = next(item for item in menu.actions() if item.text() == path[0])
    return action(found.menu(), *path[1:]) if len(path) > 1 else found


def trigger(target, *path):
    menu = target.create_context_menu()
    item = action(menu, *path)
    assert item.isEnabled()
    item.trigger()
    menu.deleteLater()


def format_at_position(editor, position):
    block = editor.document().findBlock(position)
    column = position - block.position()
    fmt = QTextCharFormat()
    fmt.setFont(editor.document().defaultFont())
    for run in block.layout().formats():
        if run.start <= column < run.start + run.length:
            fmt.merge(run.format)
    return fmt


def position_of(editor, text):
    cursor = editor.document().find(text)
    assert not cursor.isNull()
    return cursor.selectionStart()


def open_quote(notepad, qapp, source, *, compact=False):
    widget = notepad(compact=compact)
    widget.replace_source(source)
    widget.resize(800, 650)
    widget.show()
    qapp.processEvents()
    widget.editor.moveCursor(QTextCursor.MoveOperation.Start)
    widget.editor.setFocus()
    qapp.processEvents()
    return widget


def test_surfaces_share_document_undo_and_immediate_persistence(notepad, data):
    view = notepad()
    sidebar = notepad(SimpleNamespace(file=data.file), compact=True)
    source = "# Notes\n\nα 🙂 **bold**  \n"
    view.replace_source(source)
    assert sidebar.editor.document() is view.editor.document()
    assert sidebar.editor.toPlainText() == source
    # No event-loop tick or debounce timeout is necessary before a BNDB save.
    assert data.values[METADATA_KEY]['notes'][0]["text"] == source
    sidebar.editor.undo()
    assert view.editor.toPlainText() == ""
    assert data.values[METADATA_KEY]['notes'] == []
    view.editor.redo()
    assert sidebar.editor.toPlainText() == source


@pytest.mark.parametrize(("source", "expected"), [
    ("- item", "- item\n- "),
    ("- [x] done", "- [x] done\n- [ ] "),
])
def test_list_and_quote_continuation_respects_code(notepad, source, expected):
    widget = notepad()
    widget.replace_source(source)
    QTest.keyClick(widget.editor, Qt.Key.Key_Return)
    assert widget.editor.toPlainText() == expected


def test_inline_formatting_is_one_undoable_source_edit(notepad):
    widget = notepad()
    widget.replace_source("🙂 words")
    cursor = widget.editor.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    widget.editor.setTextCursor(cursor)
    widget.editor.wrap_selection("**")
    assert widget.editor.toPlainText() == "**🙂 words**"
    assert widget.editor.textCursor().selectedText() == "🙂 words"
    widget.editor.undo()
    assert widget.editor.toPlainText() == "🙂 words"


def test_find_wraps_and_outline_navigates(notepad):
    widget = notepad()
    widget.replace_source("# One\n\nneedle\n\n## Two\n\nneedle")
    widget.session.highlighter.refresh()
    widget.find_edit.setText("needle")
    widget.find_next()
    assert widget.editor.textCursor().blockNumber() == 2
    widget.find_next()
    assert widget.editor.textCursor().blockNumber() == 6
    parent = widget.outline.topLevelItem(0)
    widget._navigate_heading(parent.child(0))
    assert widget.editor.textCursor().blockNumber() == 4


def test_write_failure_keeps_note_editable_and_retryable(notepad, data):
    widget = notepad()
    data.write_error = True
    widget.replace_source("unsaved text")
    assert widget.session.source == "unsaved text"
    assert "simulated write failure" in widget.status.text()
    data.write_error = False
    widget.session.persist()
    assert data.values[METADATA_KEY]['notes'][0]["text"] == "unsaved text"


def test_unsupported_metadata_opens_read_only(notepad, data):
    payload = {"version": 99, "text": "future note"}
    data.values[METADATA_KEY] = payload
    widget = notepad()
    widget.replace_source("do not overwrite")
    assert widget.editor.isReadOnly()
    assert data.values[METADATA_KEY] == payload
    assert not data.writes


@pytest.mark.parametrize("cell", [False, True])
@pytest.mark.parametrize("key, delimiter", [
    (QKeySequence.StandardKey.Bold, "**"), (QKeySequence.StandardKey.Italic, "*"),
])
def test_repeating_keyboard_format_toggles_off(notepad, qapp, cell, key, delimiter):
    _widget, editor, _card = open_selection(notepad, qapp, "words", "words", cell=cell)
    sequence = QKeySequence.keyBindings(key)[0]
    QTest.keySequence(editor, sequence)
    assert editor.toPlainText() == delimiter + "words" + delimiter
    QTest.keySequence(editor, sequence)
    assert editor.toPlainText() == "words"
    assert editor.textCursor().selectedText() == "words"


def test_context_menu_exposes_editing_actions_in_groups(notepad, qapp):
    _widget, editor = open_menu_editor(notepad, qapp)
    menu = editor.create_context_menu()
    assert [item.text() for item in menu.actions() if not item.isSeparator()] == [
        "Add link", "Add external link", "Format", "Paragraph", "Insert", "Author",
        "Cut", "Copy", "Paste", "Paste as plain text", "Select all", "Undo", "Redo",
    ]
    assert [item.text() for item in action(menu, "Format").menu().actions()
            if not item.isSeparator()] == [
        "Bold", "Italic", "Strikethrough", "Highlight", "Code", "Math", "Comment",
        "Clear formatting",
    ]


def test_shift_enter_creates_a_markdown_source_line(notepad):
    widget = notepad()
    widget.replace_source("```c\nint main(void)\n{")
    QTest.keyClick(widget.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(widget.editor, Qt.Key.Key_Tab)
    QTest.keyClicks(widget.editor, "return 0;")
    QTest.qWait(150)
    assert "\u2028" not in widget.editor.document().toRawText()
    start = position_of(widget.editor, "return")
    assert QFontInfo(format_at_position(widget.editor, start).font()).fixedPitch()


@pytest.mark.parametrize("source, expected", [
    ("> first", "> first\n> "),
    ("> first\nlazy", "> first\nlazy\n> "),
])
def test_enter_continues_quotes_and_exits_one_level_at_a_time(notepad, qapp, source, expected):
    widget = open_quote(notepad, qapp, source)
    editor = widget.editor
    editor.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    qapp.processEvents()
    assert widget.session.source == expected




def authored_note(notepad, qapp):
    from .conftest import FakeView

    view = FakeView()
    view.values[METADATA_KEY] = {'version': 2, 'notes': [
        {'id': 'n', 'text': 'mine\n\nagent one\nagent two\n\nmine', 'agents': [
            {'line': 2, 'count': 2, 'name': 'Claude'}]}]}
    widget = notepad(view)
    widget.resize(800, 500)
    widget.show()
    qapp.processEvents()
    return widget, view


def test_people_edits_mark_lines_human_and_undo_restores_agents(notepad, qapp):
    widget, view = authored_note(notepad, qapp)
    note, editor = widget.session, widget.editor
    editor.setFocus()
    cursor = editor.textCursor()
    cursor.setPosition(len('mine\n\nagent one'))
    editor.setTextCursor(cursor)
    QTest.keyClick(editor, Qt.Key.Key_Return)  # A new line after an agent line is a person's.
    QTest.keyClicks(editor, 'added')
    qapp.processEvents()  # Edits are attributed on the next event-loop turn.
    assert note.agents == ((2, 1, 'Claude'), (4, 1, 'Claude'))
    cursor.setPosition(len('mine\n\nagent'))
    editor.setTextCursor(cursor)
    QTest.keyClicks(editor, '!')  # Editing an agent line makes it a person's.
    qapp.processEvents()
    assert note.agents == ((4, 1, 'Claude'),)
    assert view.values[METADATA_KEY]['notes'][0]['agents'] == [
        {'line': 4, 'count': 1, 'name': 'Claude'}]
    note.document.undo()
    assert note.agents == ((2, 1, 'Claude'), (4, 1, 'Claude'))


def test_people_editing_an_agent_line_share_it_and_its_bar_hands_off(notepad, qapp,
                                                                     monkeypatch):
    monkeypatch.setattr('src.storage.getpass.getuser', lambda: 'ada')
    widget, view = authored_note(notepad, qapp)
    note, editor, gutter = widget.session, widget.editor, widget.editor.author_gutter
    editor.setFocus()
    cursor = editor.textCursor()
    cursor.setPosition(len('mine\n\nagent'))
    editor.setTextCursor(cursor)
    QTest.keyClicks(editor, '!')
    qapp.processEvents()
    assert note.agents == ((3, 1, 'Claude'),)
    assert view.values[METADATA_KEY]['notes'][0]['earlier'] == [
        {'line': 2, 'count': 1, 'writers': [{'agent': 'Claude'}]}]
    QTest.keyClicks(editor, '?')  # More of the same writer's edits change nothing.
    qapp.processEvents()
    assert len(note.earlier) == 1
    # The paragraph keeps one bar: Claude, handing off to ada in the edited line, and back.
    bar = gutter.runs()[1]
    assert bar.label == 'Claude & ada'
    assert [hue for _top, _bottom, hue in bar.sections] == [CLAUDE_HUE, PEOPLE_HUES[0],
                                                             CLAUDE_HUE]
    image = gutter.grab().toImage()
    x = gutter.LEFT + 1

    def hue(section):
        top, bottom, _hue = bar.sections[section]
        return image.pixelColor(x, round((top + bottom) / 2)).hsvHue()

    assert 15 <= hue(0) <= 45 and 200 <= hue(1) <= 230 and 15 <= hue(2) <= 45
    note.document.undo()
    assert note.earlier == () and note.agents == ((2, 2, 'Claude'),)


def test_splitting_a_line_keeps_its_writers(notepad, qapp):
    widget, _view = authored_note(notepad, qapp)
    note, editor = widget.session, widget.editor
    editor.setFocus()
    for position in (len('mine\n\n'), len('mine\n\n\nagent')):  # Line start, then middle.
        cursor = editor.textCursor()
        cursor.setPosition(position)
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        qapp.processEvents()
    assert note.source == 'mine\n\n\nagent\n one\nagent two\n\nmine'
    assert note.agents == ((2, 4, 'Claude'),) and note.earlier == ()


def test_author_menu_marks_the_paragraph_and_names_agents(notepad, qapp, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    widget, view = authored_note(notepad, qapp)
    editor, note = widget.editor, widget.session
    cursor = editor.textCursor()
    cursor.setPosition(len('mine\n\nage'))
    editor.setTextCursor(cursor)
    assert action(editor.create_context_menu(), 'Author', 'Claude').isChecked()
    trigger(editor, 'Author', 'Human')
    assert note.agents == ()
    monkeypatch.setattr(QInputDialog, 'getText', lambda *_args, **_kwargs: ('GPT-6', True))
    trigger(editor, 'Author', 'Agent Name…')
    assert note.agents == ((2, 2, 'GPT-6'),)
    note.document.undo()
    assert note.agents == ()


def test_author_bar_follows_the_setting_and_slides_out_writers(notepad, qapp, monkeypatch):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    monkeypatch.setattr('src.storage.getpass.getuser', lambda: 'ada')
    widget, _view = authored_note(notepad, qapp)
    editor, gutter = widget.editor, widget.editor.author_gutter
    assert gutter.isVisible() and editor.viewportMargins().left() == gutter.WIDTH
    runs = gutter.runs()
    blue = PEOPLE_HUES[0]
    assert [run.writers for run in runs] == [(('ada', blue),), (('Claude', CLAUDE_HUE),),
                                             (('ada', blue),)]
    middle = (runs[1].top + runs[1].bottom) / 2
    QApplication.sendEvent(gutter, QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(5, middle), QPointF(5, middle),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    gutter.repaint()
    assert gutter.tag.isVisible() and gutter.tag.text == 'Claude'
    widget.session.set_author(range(6), None)  # One writer: the paragraphs' bars link up.
    assert len(gutter.runs()) == 1
    widget.session.notebook.preferences.set('show_authors', False)
    assert not gutter.isVisible() and editor.viewportMargins().left() == 0


def test_enterprise_lines_record_their_writer(notepad, qapp, monkeypatch):
    monkeypatch.setattr('src.ui.session.enterprise_user', lambda: 'cynder')
    widget, view = authored_note(notepad, qapp)
    note, editor = widget.session, widget.editor
    editor.setFocus()
    cursor = editor.textCursor()
    cursor.setPosition(0)
    editor.setTextCursor(cursor)
    QTest.keyClicks(editor, 'x')
    note.replace_text(len(note.source), len(note.source), '\nfrom MCP', author='Claude')
    assert note.users == ((0, 1, 'cynder'), (6, 1, 'cynder'))
    assert view.values[METADATA_KEY]['notes'][0]['users'] == [
        {'line': 0, 'count': 1, 'name': 'cynder'}, {'line': 6, 'count': 1, 'name': 'cynder'}]
    runs = editor.author_gutter.runs()
    assert runs[0].label == 'cynder' and runs[-1].writers[-1] == ("cynder's Claude", CLAUDE_HUE)


def test_enter_always_starts_a_line_on_tagged_and_stored_notes(notepad, qapp, monkeypatch):
    # Qt's own Enter resets an empty line's block format (author tags) instead of adding a line,
    # and a stored note's progressive layout was queried mid-edit at the end of the note.
    monkeypatch.setattr('src.ui.session.enterprise_user', lambda: 'cynder')
    widget, _view = authored_note(notepad, qapp)
    editor, note = widget.editor, widget.session
    editor.setFocus()
    cursor = editor.textCursor()
    cursor.setPosition(len('mine'))
    editor.setTextCursor(cursor)
    for _ in range(3):
        QTest.keyClick(editor, Qt.Key.Key_Return)
        qapp.processEvents()
    assert note.source.startswith('mine\n\n\n\n\nagent one')
    for _ in range(3):
        QTest.keyClick(editor, Qt.Key.Key_Backspace)
    cursor.movePosition(QTextCursor.MoveOperation.End)
    editor.setTextCursor(cursor)
    for _ in range(3):
        QTest.keyClick(editor, Qt.Key.Key_Return)
    assert note.source.endswith('mine\n\n\n')


def test_agents_are_colored_by_vendor():
    from src.ui.editor import AGENT_HUE, OPENAI_HUE, agent_hue

    for name in ('Claude Opus 5.5', 'claude-sonnet-5-5', 'Anthropic agent', 'Haiku'):
        assert agent_hue(name) == CLAUDE_HUE, name
    for name in ('GPT-6', 'gpt-4o', 'OpenAI Codex', 'ChatGPT', 'o3-mini', 'o1', 'Astra',
                 'terra-2', 'Luna mini', 'Sol'):
        assert agent_hue(name) == OPENAI_HUE, name
    for name in ('AI', 'Gemini 3', 'binjad', 'Llama', 'Cosmo', 'Solver', 'Lunatic'):
        assert agent_hue(name) == AGENT_HUE, name


def test_people_take_primary_colors_in_turn(notepad, qapp, monkeypatch):
    from .conftest import FakeView

    monkeypatch.setattr('src.ui.session.enterprise_user', lambda: 'cynder')
    monkeypatch.setattr('src.storage.getpass.getuser', lambda: 'cynder')
    view = FakeView()
    view.values[METADATA_KEY] = {'version': 2, 'notes': [
        {'id': 'n', 'text': 'mine\n\nbob\n\nalice\n\nold\n\nagent',
         'users': [{'line': 0, 'count': 1, 'name': 'cynder'},
                   {'line': 2, 'count': 1, 'name': 'bob'},
                   {'line': 4, 'count': 1, 'name': 'alice'},
                   {'line': 8, 'count': 1, 'name': 'cynder'}],
         'agents': [{'line': 8, 'count': 1, 'name': 'GPT-6'}]}]}
    widget = notepad(view)
    widget.resize(800, 500)
    widget.show()
    qapp.processEvents()
    runs = widget.editor.author_gutter.runs()
    # You first; everyone else by name. A line with no user recorded is yours too.
    assert [run.writers for run in runs] == [
        (('cynder', PEOPLE_HUES[0]),), (('bob', PEOPLE_HUES[2]),),
        (('alice', PEOPLE_HUES[1]),), (('cynder', PEOPLE_HUES[0]),),
        (("cynder's GPT-6", OPENAI_HUE),)]
