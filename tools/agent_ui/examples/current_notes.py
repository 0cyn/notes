"""Capture visible installed Notes widgets without changing the user's notes."""


def run(session):
    from bnnotes.src.ui.notebook import NotesWidget
    from PySide6.QtWidgets import QApplication

    result = []
    for widget in QApplication.allWidgets():
        if isinstance(widget, NotesWidget) and widget.isVisible():
            session.capture(widget, f'notes-{len(result)}')
            result.append(session.describe(widget))
    return result
