from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget
from shiboken6 import delete

from .conftest import wait_until


def _focus(qapp, widget):
    widget.window().show()
    widget.window().activateWindow()
    widget.setFocus()
    wait_until(lambda: QApplication.focusWidget() is widget)


def test_deleting_a_focused_notes_widget_hands_focus_to_a_live_one(notebook, qapp):
    """Binary Ninja's menu bar acts on the last focused widget and ignores focus moving to
    nothing, so a deleted focused widget must not leave focus there (it crashed on ⌘-keys)."""
    widget = notebook()
    widget.resize(800, 600)
    page = widget.current_note
    field = QLineEdit(page)
    field.show()
    _focus(qapp, field)

    delete(field)  # As a page, tour or card is: focused, then gone.
    assert QApplication.focusWidget() is None
    wait_until(lambda: QApplication.focusWidget() is not None)
    focused = QApplication.focusWidget()
    assert focused is widget or widget.isAncestorOf(focused)

    # Losing focus to a whole closed surface lands on what's left of the window.
    host = QWidget()
    QVBoxLayout(host).addWidget(other := notebook(parent=host))
    host.resize(800, 600)
    _focus(qapp, other.current_note.editor)
    delete(other)
    wait_until(lambda: QApplication.focusWidget() is not None)
    assert QApplication.focusWidget() is host
    delete(host)


def test_focus_lost_outside_notes_is_left_alone(notebook, qapp):
    notebook()  # The keeper exists.
    window = QWidget()
    field = QLineEdit(window)
    QVBoxLayout(window).addWidget(field)
    _focus(qapp, field)
    delete(field)
    for _ in range(3):
        qapp.processEvents()
    assert QApplication.focusWidget() is None
    delete(window)
