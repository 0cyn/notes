import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QContextMenuEvent, QTextCursor
from PySide6.QtTest import QTest

from src.links import ILMode
from src.storage import METADATA_KEY
from src.ui.common import NoteContextMenu
from src.ui.provider import HLILPreviewProvider
from src.ui.tables import cell_html

from .conftest import (
    Function,
    LinkView,
    SnapshotFunction,
    _action,
    file_url,
    wait_until,
)


def entries(editor):
    return list(editor._links.rectangles())


def open_preview(notebook, data, source):
    widget = notebook(data)
    widget.current_note.replace_source(source)
    widget.resize(900, 420)
    widget.show()
    widget.activateWindow()
    cursor = widget.editor.textCursor()
    cursor.setPosition(0)
    widget.editor.setTextCursor(cursor)
    widget.editor.setFocus()
    QCoreApplication.processEvents()
    return widget


@pytest.fixture
def multiline_view(preview_view):
    address = preview_view.start + 4
    preview_view.functions[0].statement.rendered_lines = [
        SimpleNamespace(address=address, tokens=[
            SimpleNamespace(text='        ', type=33),
            SimpleNamespace(text='int32_t', type=19),
            SimpleNamespace(text=' result =', type=0),
        ]),
        SimpleNamespace(address=address, tokens=[
            SimpleNamespace(text='            ', type=33),
            SimpleNamespace(text='UDFGetVnode', type=64),
            SimpleNamespace(text='(mount, objectId: 2, nodeIdType: kUDFNodeIdNormal,', type=0),
        ]),
        SimpleNamespace(address=address, tokens=[
            SimpleNamespace(text='                parentVnode: nullptr, &componentName, ', type=0),
            SimpleNamespace(text='vnodeOut: arg2, directoryEntryOffset: 0, ', type=0),
            SimpleNamespace(text='fileEntryLocation: nullptr, lockNode: true)', type=0),
        ]),
    ]
    return preview_view


def _embed_point(editor):
    entry, rectangle = entries(editor)[0]
    return (rectangle.topLeft() + entry.code_rectangle.center()).toPoint()


def _note(notebook, data, source):
    widget = open_preview(notebook, data, source)
    editor = widget.editor
    editor.moveCursor(QTextCursor.MoveOperation.End)  # Not inside a link at the start.
    # Only HLIL resolves in the test double; other modes embed their placeholder.
    wait_until(lambda: len(entries(editor)) == 1 and not entries(editor)[0][0].preview.text
               .startswith('Loading'))
    return widget, editor


URL = 'binaryninja://?expr=first+4'


def _right_click(editor, point):
    viewport = editor.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, pos=point)
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, pos=point)
    QCoreApplication.sendEvent(viewport, QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, point, viewport.mapToGlobal(point)))
    QCoreApplication.processEvents()


@pytest.fixture
def menus(monkeypatch):
    opened = []
    monkeypatch.setattr(NoteContextMenu, 'exec', lambda self, *_args: opened.append(self))
    return opened


@pytest.fixture
def preview_data(tmp_path):
    data = LinkView(tmp_path / 'settings-test.bndb')
    data.functions[0] = Function(data.start)
    return data


class ModeView(LinkView):
    """A link-resolving raw view whose first function serves every mode."""

    def __init__(self, filename):
        super().__init__(filename)
        self.functions[0] = SnapshotFunction(self.start)


@pytest.fixture
def mode_view(tmp_path):
    return ModeView(tmp_path / 'modes.bndb')


def test_inactive_link_collapses_and_reveals_exact_source_for_editing(
    notebook, preview_view, qapp,
):
    url = file_url(preview_view, 'first+4')
    source = f'🙂 Before [**target**]({url}) after\n\nTail'
    widget = open_preview(notebook, preview_view, source)
    editor = widget.editor
    writes = list(preview_view.writes)
    undo = editor.document().availableUndoSteps()
    wait_until(lambda: len(entries(editor)) == 1 and entries(editor)[0][0].preview.available)
    entry, rectangle = entries(editor)[0]
    assert entry.label == 'target'
    assert entry.preview.text == 'result = worker(arg1)'
    assert entry.layout.text() == 'target'
    assert entry.code.text == f'{preview_view.start + 4:x}\t    result = worker(arg1)'
    assert editor.document().begin().layout().lineCount() == 1
    assert editor.document().toPlainText() == source
    assert editor.document().availableUndoSteps() == undo
    assert preview_view.writes == writes

    QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton, pos=rectangle.center().toPoint())
    qapp.processEvents()
    assert not entries(editor)
    assert entry.link.start <= editor.textCursor().position() < entry.link.end
    assert editor.toPlainText() == source
    assert preview_view.writes == writes

    cursor = editor.textCursor()
    cursor.setPosition(editor.document().characterCount() - 1)
    editor.setTextCursor(cursor)
    wait_until(lambda: len(entries(editor)) == 1)
    assert preview_view.values[METADATA_KEY]['notes'][0]['text'] == source


def test_collapsed_control_click_follows_address_and_selection_copies_markdown(
    notebook, preview_view, qapp,
):
    source = 'Before [target](binaryninja://?expr=first+4) after'
    widget = open_preview(notebook, preview_view, source)
    editor = widget.editor
    wait_until(lambda: bool(entries(editor)))
    received = []
    widget.addressActivated.connect(received.append)
    entry, rectangle = entries(editor)[0]
    QTest.mouseClick(editor.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier, rectangle.center().toPoint())
    qapp.processEvents()
    assert received == [preview_view.start + 4]
    assert entries(editor)
    editor.selectAll()
    qapp.processEvents()
    assert not entries(editor)
    editor.copy()
    assert qapp.clipboard().text() == source
    assert editor.toPlainText() == source


def test_other_file_external_and_code_links_stay_source_text(notebook, preview_view):
    source = ('Before [local](binaryninja://?expr=first+4) '
              '[web](https://example.com) [other](binaryninja:///other.bndb?expr=first)\n'
              '`[code](binaryninja://?expr=first)`')
    widget = open_preview(notebook, preview_view, source)
    wait_until(lambda: len(entries(widget.editor)) == 1)
    assert entries(widget.editor)[0][0].label == 'local'
    assert widget.editor.toPlainText() == source


def test_table_preview_escapes_hlil_and_keeps_note_source(notebook, preview_view, qapp):
    function = preview_view.functions[0]
    function.statement.text = 'if (x < 4 && y > 0)'
    url = 'binaryninja://?expr=first+4'
    source = f'| Name |\n| --- |\n| [target]({url}) |\n'
    widget = open_preview(notebook, preview_view, source)
    source = widget.current_note.session.source
    model = widget.current_note.session.tables.models[0]
    card = widget.editor.table_cards[model.identity]
    card.link_previews(model.rows[1][0], load=True)
    provider = widget._link_previews
    wait_until(lambda: provider.request(url).available)
    previews = card.link_previews(model.rows[1][0], load=False)
    html = cell_html(model.rows[1][0], link_previews=previews)
    assert 'if (x &lt; 4 &amp;&amp; y &gt; 0)' in html
    assert 'bnnotes-hlil-preview:' in html
    assert widget.current_note.session.source == source
    card.focus_cell(1, 0, edit=True)
    qapp.processEvents()
    assert card.cell_editor.toPlainText() == f'[target]({url})'
    assert 'if (x' not in card.cell_editor.toPlainText()


def test_provider_runs_off_ui_thread_and_discards_results_from_a_previous_view(preview_view, qapp):
    function = preview_view.functions[0]
    function.release = threading.Event()
    provider = HLILPreviewProvider(preview_view)
    try:
        assert provider.request('binaryninja://?expr=first+4').text == 'Loading HLIL…'
        assert function.started.wait(1)
        assert len(function.threads) == 1
        assert function.threads[0] != threading.get_ident()
        other = LinkView(preview_view.file.filename + '.other')
        other.functions[0] = Function(other.start)
        other.functions[0].statement.text = 'different = other_binary()'
        provider.set_data(other)
        provider.request('binaryninja://?expr=first+4')
        wait_until(lambda: provider.request('binaryninja://?expr=first+4').available)
        function.release.set()
        QTest.qWait(100)
        assert provider.request('binaryninja://?expr=first+4').text == 'different = other_binary()'
    finally:
        function.release.set()
        provider.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_menu_commands_rewrite_the_link_as_single_undo_steps_without_revealing_it(
    notebook, preview_view, qapp, menus,
):
    source = f'Before\n\n[target]({URL}) after\n\nTail'
    widget, editor = _note(notebook, preview_view, source)
    undo = editor.document().availableUndoSteps()
    _right_click(editor, _embed_point(editor))
    mlil = next(action for action in _action(menus[-1], 'Select IL mode').menu().actions()
                if action.text() == 'MLIL')
    mlil.trigger()
    assert editor.toPlainText() == source.replace(f'{URL})', f'{URL} "mlil")')
    assert editor.document().availableUndoSteps() == undo + 1
    wait_until(lambda: len(entries(editor)) == 1
               and entries(editor)[0][0].preview.il_mode is ILMode.MLIL)

    _right_click(editor, _embed_point(editor))
    assert [action.text() for action in _action(menus[-1], 'Select IL mode').menu().actions()
            if action.isChecked()] == ['MLIL']
    _action(menus[-1], 'Embed whole AST').trigger()
    assert editor.toPlainText() == source.replace('[target', '[!target').replace(
        f'{URL})', f'{URL} "mlil")')
    wait_until(lambda: len(entries(editor)) == 1 and entries(editor)[0][0].link.expand_block)
    editor.undo()
    editor.undo()
    assert editor.toPlainText() == source


@pytest.mark.parametrize('modifier', [Qt.KeyboardModifier.NoModifier,
                                      Qt.KeyboardModifier.ShiftModifier])
@pytest.mark.parametrize('inside', ['[tar', f'[target]({URL[:12]}'])
def test_enter_inside_link_source_leaves_it_for_the_next_line(
    notebook, preview_view, qapp, modifier, inside,
):
    source = f'Before\n\n- [target]({URL} "mlil") after\nNext line\n\nTail'
    widget, editor = _note(notebook, preview_view, source)
    cursor = editor.textCursor()
    cursor.setPosition(source.index(inside) + len(inside))
    editor.setTextCursor(cursor)
    QTest.keyClick(editor, Qt.Key.Key_Return, modifier)
    assert editor.toPlainText() == source
    assert editor.textCursor().position() == source.index('Next line')
    wait_until(lambda: len(entries(editor)) == 1)  # Collapsed again.


