import time

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtTest import QTest

from src.markdown import utf16_length
from src.ui.search import (
    SearchNote,
    search_notes,
)


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate()


def search(page, text):
    page.query.setText(text)
    wait_for(lambda: not page._debounce.isActive() and page._jobs[0] is None)
    return page.results


def test_literal_case_insensitive_search_preserves_unicode_offsets_and_context():
    source = 'first\nsecond\n🙂 café NEEDLE and needle\nfourth\nfifth\nsixth'
    results = search_notes([SearchNote('a', 'First note', source, 4)], 'needle')
    assert results.matches == 2 and not results.truncated
    note = results.notes[0]
    assert note.note_id == 'a' and note.matches == 2
    assert len(note.snippets) == 1
    snippet = note.snippets[0]
    assert [line.number for line in snippet.lines] == [1, 2, 3, 4, 5]
    assert snippet.matches[0].start == utf16_length('first\nsecond\n🙂 café ')
    assert snippet.matches[0].line == 3
    assert snippet.lines[2].highlights == ((8, 14), (19, 25))
    encoded = source.encode('utf-16-le')
    assert [encoded[hit.start * 2:hit.end * 2].decode('utf-16-le')
            for hit in snippet.matches] == ['NEEDLE', 'needle']
    assert search_notes([SearchNote('a', '', source, 0)], 'needle',
                        case_sensitive=True).matches == 1
    note = SearchNote('b', '', 'a+b aaab a+b', 0)
    assert search_notes([note], 'a+b').matches == 2
    assert search_notes([note], '').notes == ()


def test_search_includes_closed_notes_and_click_opens_exact_unicode_match(notebook, data, qapp):
    widget = notebook()
    first = widget.current_note
    first.replace_source('# One\n\nNo match here')
    second = widget.new_note()
    second.replace_source('# Two\n\n🙂 context\n  NEEDLE here\nfollowing')
    note = second.session
    widget.close_note()
    widget.resize(850, 650)
    widget.show()
    widget.activateWindow()
    page = widget.open_find_all()
    writes = list(data.writes)
    results = search(page, 'needle')
    assert results.matches == 1 and results.notes[0].note_id == note.id
    assert note.id not in widget._pages
    qapp.processEvents()
    index = page.result_list.model().index(1, 0)
    rectangle = page.result_list.visualRect(index)
    assert rectangle.height() > 0
    QTest.mouseClick(page.result_list.viewport(), Qt.MouseButton.LeftButton, pos=rectangle.center())
    qapp.processEvents()
    assert widget.current_note.session is note
    assert widget.editor.textCursor().selectedText() == 'NEEDLE'
    assert widget.editor.hasFocus()
    assert data.writes == writes
    # The result replaced the home tab; Find in All Notes brings it back with the query.
    assert widget.home.page is None
    again = widget.open_find_all()
    assert widget.tabs.currentWidget() is widget.home.page and again.query.text() == 'needle'
    assert again.query.hasFocus()


