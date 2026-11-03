import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.markdown import MarkdownParser, cell_editor_text, new_table
from src.storage import METADATA_KEY
from src.ui.tables import cell_document

from .conftest import SOURCE, table_widget


def open_note(notepad, qapp, source):
    widget = notepad()
    widget.replace_source(source)
    widget.resize(950, 650)
    widget.show()
    qapp.processEvents()
    return widget, widget.session.tables.models[0]


def click_note(widget, point):
    viewport = widget.editor.viewport()
    assert viewport.rect().contains(point)
    receiver = viewport.childAt(point) or viewport
    QTest.mouseClick(receiver, Qt.MouseButton.LeftButton,
                     pos=receiver.mapFrom(viewport, point))


def type_prose(widget, qapp):
    assert widget.editor.hasFocus()
    # Let the parser run between keystrokes, as it does during real typing.
    for char in "Text after table":
        QTest.keyClicks(QApplication.focusWidget(), char)
        qapp.processEvents()
    assert widget.editor.hasFocus()
    assert widget.session.tables.models[0].rowCount() == 3
    assert new_table(3, 2) + "\n\nText after table" in widget.session.source


def open_table(notepad, qapp, suffix="", prefix=""):
    widget = notepad()
    widget.replace_source(prefix + new_table(3, 2) + suffix)
    widget.resize(1000, 700)
    widget.show()
    qapp.processEvents()
    card = next(iter(widget.editor.table_cards.values()))
    card.focus_cell(2, 1)
    qapp.processEvents()
    return widget, card


CELL = (
    "```c<br><br>int void(uint64_t weh)<br>{<br>    return 0;<br>}<br>"
    "```aasdf<br>asdfasdf<br>asdf"
)


MULTILINE_SOURCE = (
    "# asdf\n\n\n"
    "| asdf                                     | bababa |\n"
    "| ---------------------------------------- | ------ |\n"
    f"| {CELL} |        |\n"
    "|                                          |        | \n\n"
)


def open_cell(notepad, qapp):
    widget = notepad()
    widget.replace_source(MULTILINE_SOURCE)
    widget.resize(1000, 700)
    widget.show()
    qapp.processEvents()
    model = widget.session.tables.models[0]
    widget.editor._ensure_table_visible(model)
    card = next(iter(widget.editor.table_cards.values()))
    card.focus_cell(1, 0, edit=True)
    qapp.processEvents()
    return widget, model, card, card.cell_editor


def test_tables_are_rendered_inline_but_source_and_copy_remain_markdown(notepad, qapp):
    widget, model, card = table_widget(notepad, qapp)
    assert card.isVisible()
    assert widget.editor.toPlainText() == SOURCE
    assert model.rows[0] == ["**Name**", "Value"]
    assert model.source_markdown() == SOURCE[SOURCE.index("|"):SOURCE.index("\n\nAfter table")]
    document = cell_document("**bold** and *italic*", card.view.font(), 200)
    assert document.toPlainText() == "bold and italic"
    block = widget.editor.document().findBlockByNumber(model.table.start_line + 1)
    assert not block.isVisible()
    after = widget.editor.document().find("After table")
    rect = widget.editor._visual_line_rect(after.selectionStart())
    assert rect.top() >= card.geometry().bottom()
    widget.editor.selectAll()
    widget.editor.copy()
    assert QApplication.clipboard().text() == SOURCE
    card.copy_table()
    copied = MarkdownParser().parse(QApplication.clipboard().text()).tables[0]
    assert copied.rows == tuple(map(tuple, model.rows))


def test_cell_typing_is_saved_immediately_and_undoable_across_views(notepad, qapp, data):
    widget, model, card = table_widget(notepad, qapp)
    other = notepad(compact=True)
    other.resize(350, 620)
    other.show()
    qapp.processEvents()
    assert other.session.tables.models[0] is model
    card.focus_cell(1, 0, edit=True)
    qapp.processEvents()
    editor = card.cell_editor
    assert editor is not None
    editor.selectAll()
    QTest.keyClicks(editor, "**updated**")
    assert "**updated**" in data.values[METADATA_KEY]['notes'][0]["text"]
    qapp.processEvents()
    assert other.session.tables.models[0].rows[1][0] == "**updated**"
    card.undo()
    qapp.processEvents()
    assert widget.session.tables.models[0].rows[1][0] == "alpha"
    assert other.session.tables.models[0].rows[1][0] == "alpha"
    card.redo()
    qapp.processEvents()
    assert widget.session.tables.models[0].rows[1][0] == "**updated**"


def test_row_column_operations_and_alignment_preserve_surrounding_text(notepad, qapp):
    widget, model, _ = table_widget(notepad, qapp)
    model.insert_row(1)
    model.insert_column(1)
    model.set_cell(1, 1, "**new** | 🙂")
    model.set_alignment([1], "center")
    qapp.processEvents()
    model = widget.session.tables.models[0]
    assert (model.rowCount(), model.columnCount()) == (4, 3)
    assert model.rows[1][1] == "**new** \\| 🙂"
    assert model.alignments[1] == "center"
    model.delete_row(2)
    model.delete_column(0)
    qapp.processEvents()
    assert widget.editor.toPlainText().startswith("# Notes\n\n")
    assert widget.editor.toPlainText().endswith("\n\nAfter table")
    assert (model.rowCount(), model.columnCount()) == (3, 2)


def test_paste_markdown_and_tsv_into_cells_expands_the_grid(notepad, qapp):
    widget, model, card = table_widget(notepad, qapp)
    card.focus_cell(2, 1)
    QApplication.clipboard().setText("one\ttwo\nthree\tfour")
    card.paste()
    qapp.processEvents()
    assert (model.rowCount(), model.columnCount()) == (4, 3)
    assert model.rows[2][1:] == ["one", "two"]
    assert model.rows[3][1:] == ["three", "four"]
    card.focus_cell(0, 0)
    QApplication.clipboard().setText("| New | Header |\n| --- | --- |\n| **bold** | `code` |")
    card.paste()
    qapp.processEvents()
    assert model.rows[1][:2] == ["**bold**", "`code`"]
    assert "After table" in widget.editor.toPlainText()


def test_tab_from_last_cell_adds_a_row_and_escape_leaves_table(notepad, qapp):
    widget, model, card = table_widget(notepad, qapp)
    card.focus_cell(2, 1, edit=True)
    qapp.processEvents()
    QTest.keyClick(card.cell_editor, Qt.Key.Key_Tab)
    qapp.processEvents()
    assert model.rowCount() == 4
    assert card.view.currentIndex().row() == 3
    assert card.view.currentIndex().column() == 0
    # Finish editing, then Escape from the grid returns to the note after it.
    if card.cell_editor is not None:
        QTest.keyClick(card.cell_editor, Qt.Key.Key_Escape)
    QTest.keyClick(card.view, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert widget.editor.hasFocus()
    assert widget.editor.textCursor().position() > model.end


def test_grid_picker_creates_the_requested_dimensions_in_one_undo(notepad, qapp):
    widget = notepad()
    widget.resize(1000, 620)
    widget.show()
    picker = widget.table_picker
    point = QPoint(picker.MARGIN + picker.CELL * 2 + 4, picker.TOP + picker.CELL * 3 + 4)
    menu_visible = []

    def choose_size():
        menu_visible.append(widget.table_menu.isVisible())
        QTest.mouseMove(picker, point)
        QTest.mouseRelease(picker, Qt.MouseButton.LeftButton, pos=point)

    QTimer.singleShot(0, choose_size)
    QTest.mouseClick(widget.editor.table_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert menu_visible == [True]
    model = widget.session.tables.models[0]
    assert (model.rowCount(), model.columnCount()) == (4, 3)
    widget.session.document.undo()
    qapp.processEvents()
    assert widget.session.source == ""
    assert not widget.editor.table_cards


def test_table_only_note_has_owned_separators_and_writable_edge_paragraphs(notepad, qapp):
    table = new_table(3, 2)
    widget, model = open_note(notepad, qapp, table)
    assert widget.session.source == "\n\n" + table + "\n\n"
    assert model.start == 2
    assert model.render_start == 1
    assert model.render_end == model.end + 1
    assert model.before_position == 0
    assert model.after_position == widget.session.document.characterCount() - 1
    assert model.source_markdown() == table
    assert not widget.session.document.findBlock(model.start).isVisible()
    assert not widget.session.document.findBlock(model.render_end).isVisible()
    assert widget.session.document.findBlock(model.before_position).isVisible()
    assert widget.session.document.findBlock(model.after_position).isVisible()


@pytest.mark.parametrize("suffix", ["", "\n"])
def test_click_below_final_table_allows_typing_in_note(notepad, qapp, suffix):
    widget, card = open_table(notepad, qapp, suffix)
    point = QPoint(40, card.geometry().bottom() + 12)
    click_note(widget, point)
    qapp.processEvents()
    source = widget.session.source
    click_note(widget, point)
    qapp.processEvents()
    assert widget.session.source == source
    type_prose(widget, qapp)


def test_shift_enter_inserts_newline_into_the_same_cell(notepad, qapp):
    _widget, model, card, editor = open_cell(notepad, qapp)
    editor.selectAll()
    QTest.keyClicks(editor, "begin")
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    qapp.processEvents()
    assert card.cell_editor is editor
    assert card.current_cell() == (1, 0)
    assert editor.toPlainText() == "begin\n"
    assert "\u2028" not in editor.document().toRawText()
    assert cell_editor_text(model.rows[1][0]) == "begin\n"


