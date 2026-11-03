from __future__ import annotations

from contextlib import contextmanager
from time import monotonic

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QIODevice,
    QObject,
    QSaveFile,
    QSignalBlocker,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QKeySequence, QPainter, QShortcut, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QBoxLayout,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QProgressDialog,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)
from shiboken6 import isValid

from ..export import embeds, export_markdown
from ..markdown import MarkdownParser
from .common import (
    NOTE_CONTENT_MARGIN,
    FindKeyFilter,
    Lease,
    OwnedMenu,
    ToolbarButton,
    single_shot,
)
from .editor import MarkdownEditor
from .highlighter import code_block_colors
from .search import create_filter_edit
from .tables import TableGridPicker, custom_table_size


class SearchDrawer(QWidget):
    DURATION = 170

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setObjectName('bnnotes-search-drawer')
        self.setAccessibleName('Find in note')
        self.expanded = False
        self._progress = 0.0
        self._laying_out = False
        self._base_margins = editor.viewportMargins()
        self._query = None
        self._controls = None
        self._animation = QVariantAnimation(self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._set_progress)
        self._animation.finished.connect(self._finished)
        self._layout_timer = single_shot(self, self._layout_panel)
        editor.installEventFilter(self)
        editor.viewport().installEventFilter(self)
        self.hide()

    def set_controls(self, query, controls):
        self._query, self._controls = query, controls
        self._layout_panel()

    def set_expanded(self, expanded):
        expanded = bool(expanded)
        if expanded == self.expanded:
            return
        self.expanded = expanded
        self._animation.stop()
        target = 1.0 if expanded else 0.0
        self._layout_panel()
        if expanded:
            self.show()
            self.raise_()
        if not self.editor.isVisible() or self._progress == target:
            self._set_progress(target)
            self._finished()
            return
        start = self._progress
        # Reversing mid-slide starts from the same pixel.
        with QSignalBlocker(self._animation):
            self._animation.setDuration(max(1, round(self.DURATION * abs(target - start))))
            self._animation.setStartValue(start)
            self._animation.setEndValue(target)
            self._animation.setCurrentTime(0)
        self._animation.start()

    def _set_progress(self, value):
        self._progress = float(value)
        self._layout_panel()

    def _finished(self):
        if not self.expanded:
            self.hide()

    def _layout_panel(self):
        if self._laying_out or not isValid(self.editor):
            return
        self._laying_out = True
        try:
            layout = self.layout()
            if layout is not None and self._controls is not None:
                margins = layout.contentsMargins()
                query_width = max(120, self._query.fontMetrics().horizontalAdvance(
                    'Search notes',
                ) + 50)
                required = (query_width + self._controls.sizeHint().width() + layout.spacing()
                            + margins.left() + margins.right())
                direction = (QBoxLayout.Direction.TopToBottom
                             if self.editor.viewport().width() < required
                             else QBoxLayout.Direction.LeftToRight)
                if layout.direction() != direction:
                    layout.setDirection(direction)
            height = max(1, layout.sizeHint().height() if layout is not None else 1)
            reveal = round(height * self._progress)
            margins = self.editor.viewportMargins()
            top = self._base_margins.top() + reveal
            if margins.top() != top:
                margins.setTop(top)
                self.editor.setViewportMargins(margins)
            viewport = self.editor.viewport().geometry()
            # The drawer's lower edge and the pushed-down viewport move together.
            self.setGeometry(viewport.left(), viewport.top() - height, viewport.width(), height)
            if not self.isHidden():
                self.raise_()
        finally:
            self._laying_out = False

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.editor.palette().base())
        divider = code_block_colors(self.editor.palette())[1]
        painter.fillRect(0, self.height() - 1, self.width(), 1, divider)
        painter.end()

    def event(self, event):
        result = super().event(event)
        if hasattr(self, '_layout_timer') and event.type() in (
            QEvent.Type.LayoutRequest, QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange,
        ):
            self._layout_timer.start(0)
        return result

    def eventFilter(self, watched, event):
        if not isValid(self.editor):
            return False
        kind = event.type()
        if watched is self.editor and kind == QEvent.Type.Hide:
            self._animation.stop()
            self._set_progress(1.0 if self.expanded else 0.0)
            self._finished()
        elif kind in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show,
                      QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._layout_panel()
            self.update()
        return False


class NotepadWidget(QWidget):
    """One note in a notebook tab: its editor, heading outline, find drawer and status.

    The editor is built on first show (`materialize`), so restoring many tabs is cheap.
    """

    def __init__(self, session, parent=None, *, compact=False, on_editor=None):
        super().__init__(parent)
        self.setObjectName("bnnotes-notepad")
        self.setMinimumWidth(240)
        self.session = session
        self._compact = compact
        self._on_editor = on_editor
        self._editor = None
        self._show_held = False
        self._lease = Lease(self)
        self._lease.take(session)
        self._preferences = session.notebook.preferences
        self._preferences.changed.connect(self._apply_preference)
        self.hide()  # Not yet in a tab stack.

    @property
    def editor(self):
        self.materialize()
        return self._editor

    @property
    def outline_button(self):
        return self.editor.outline_button

    @property
    def materialized(self):
        return self._editor is not None

    def materialize(self):
        if self._editor is not None:
            return
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(NOTE_CONTENT_MARGIN, 0, NOTE_CONTENT_MARGIN, NOTE_CONTENT_MARGIN)
        layout.setSpacing(6)
        self._editor = editor = MarkdownEditor(self)
        if self._on_editor is not None:
            self._on_editor(self)
        self._build_table_menu()
        self._build_find_drawer()
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(editor)
        self.outline = QTreeWidget(self.splitter)
        self.outline.setHeaderHidden(True)
        self.outline.setMinimumWidth(100)
        self.outline.setIndentation(12)
        self.outline.setAccessibleName("Note heading outline")
        self.outline.itemActivated.connect(self._navigate_heading)
        self.outline.itemClicked.connect(self._navigate_heading)
        self.splitter.addWidget(self.outline)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setSizes([700, 180])
        editor.outline_button.setChecked(not self._compact)
        self.outline.setVisible(editor.outline_button.isChecked())
        editor.outline_button.toggled.connect(self.outline.setVisible)
        layout.addWidget(self.splitter, 1)
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self._find_keys = FindKeyFilter(self, self.show_find, lambda: True)
        for key, callback in ((QKeySequence.StandardKey.FindNext, self.find_next),
                              (QKeySequence.StandardKey.FindPrevious,
                               lambda: self.find_next(backward=True)),
                              (Qt.Key.Key_Escape, self.hide_find)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
        self._find_escape = shortcut
        self._find_escape.setEnabled(False)
        self.setFocusProxy(editor)
        layout.activate()  # Bind at the real viewport size to avoid cold layouts.
        editor.bind_session(self.session)
        editor.setPlaceholderText("# Analysis notes\n\nStart writing Markdown…")
        self.session.statusChanged.connect(self._update_status)
        self.session.highlighter.analysisChanged.connect(self._analysis_changed)
        self._analysis_changed(self.session.highlighter.analysis)
        self.editor.table_button.setEnabled(not self.session.load_failed)
        self._apply_preference('word_wrap')
        self._apply_preference('show_authors')

    def _build_table_menu(self):
        button = self._editor.table_button
        self.table_menu = OwnedMenu(button)
        self.table_menu.setTitle("Insert table")
        self.table_picker = TableGridPicker(self.table_menu)
        picker = QWidgetAction(self.table_menu)
        picker.setDefaultWidget(self.table_picker)
        self.table_menu.addAction(picker)
        self.table_menu.addSeparator()
        self.table_menu.addAction("Custom size…", self._custom_table)
        self.table_picker.chosen.connect(self._insert_table)
        button.setMenu(self.table_menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

    def _build_find_drawer(self):
        self.find_bar = SearchDrawer(self._editor)
        layout = QHBoxLayout(self.find_bar)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(8, 6, 8, 7)
        layout.setSpacing(4)
        self.find_edit, self._find_case_sensitive = create_filter_edit(
            self, in_note=True, parent=self.find_bar)
        self.find_edit.setPlaceholderText("Find in note…")
        self.find_edit.setAccessibleName('Find in note')
        self.find_edit.setMinimumWidth(0)
        policy = self.find_edit.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self.find_edit.setSizePolicy(policy)
        if self._find_case_sensitive is None:
            self.find_edit.returnPressed.connect(self.find_next)
        layout.addWidget(self.find_edit, 1)
        self.find_controls = QWidget(self.find_bar)
        buttons = QHBoxLayout(self.find_controls)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(4)
        for text, tooltip, callback in (
                ("↑", "Previous match", lambda: self.find_next(backward=True)),
                ("↓", "Next match", self.find_next), ("×", "Close find", self.hide_find)):
            button = ToolbarButton(self)
            button.setText(text)
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addWidget(self.find_controls, alignment=Qt.AlignmentFlag.AlignRight)
        self.find_bar.set_controls(self.find_edit, self.find_controls)

    @contextmanager
    def materialization_held(self):
        """Don't build the editor for a show that only lasts while a tab is inserted."""
        held, self._show_held = self._show_held, True
        try:
            yield
        finally:
            self._show_held = held

    def showEvent(self, event):
        if not self._show_held:
            self.materialize()
        super().showEvent(event)

    def release_note(self):
        self._lease.release()

    def _apply_preference(self, name):
        if self.materialized and name == 'show_authors':
            self.editor.set_author_bar(self._preferences.effective(name))
        if self.materialized and name == 'word_wrap':
            self.editor.setLineWrapMode(
                MarkdownEditor.LineWrapMode.WidgetWidth if self._preferences.effective(name)
                else MarkdownEditor.LineWrapMode.NoWrap,
            )

    def _insert_table(self, columns, rows):
        self.table_menu.close()
        self.editor.insert_table(rows, columns)

    def _custom_table(self):
        dimensions = custom_table_size(self)
        if dimensions is not None:
            self._insert_table(*dimensions)

    def _analysis_changed(self, analysis):
        self.outline.clear()
        parents: list[tuple[int, QTreeWidgetItem]] = []
        for heading in analysis.headings:
            while parents and parents[-1][0] >= heading.level:
                parents.pop()
            parent = parents[-1][1] if parents else self.outline.invisibleRootItem()
            item = QTreeWidgetItem(parent, [heading.title or "Untitled section"])
            item.setData(0, Qt.ItemDataRole.UserRole, heading.position)
            item.setToolTip(0, f"Line {heading.line + 1}")
            parents.append((heading.level, item))
        self.outline.expandAll()
        self._update_status()

    def _navigate_heading(self, item, _column=0):
        cursor = QTextCursor(self.editor.document())
        position = item.data(0, Qt.ItemDataRole.UserRole)
        cursor.setPosition(min(position, self.editor.document().characterCount() - 1))
        self.editor.setTextCursor(cursor)
        self.editor.centerCursor()
        self.editor.setFocus()

    def _update_status(self):
        session = self.session
        if session.error:
            message = session.error + ("" if session.load_failed
                                       else " Export the note as Markdown to keep a copy.")
        else:
            message = session.highlighter.warning
        self.status.setText(message)
        self.status.setToolTip(message)
        self.status.setVisible(bool(message))

    def show_find(self):
        self.find_bar.set_expanded(True)
        self._find_escape.setEnabled(True)
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def hide_find(self):
        if self.find_bar.expanded:
            self.find_bar.set_expanded(False)
            self._find_escape.setEnabled(False)
            card = self.editor.active_table
            (card.view if card is not None else self.editor).setFocus()

    def find_next(self, _checked=False, *, backward=False):
        query = self.find_edit.text()
        if not query:
            self.show_find()
            return
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        if self._find_case_sensitive is not None and self._find_case_sensitive():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        document = self.editor.document()
        cursor = document.find(query, self.editor.textCursor(), flags)
        if cursor.isNull():
            cursor = document.find(query, document.characterCount() - 1 if backward else 0, flags)
        if cursor.isNull():
            self.find_edit.setToolTip("No matches")
        else:
            self.editor.show_source_cursor(cursor)
            self.find_edit.setToolTip("")

    def replace_source(self, source: str):
        """Replace the whole note as one undo step."""
        if self.editor.isReadOnly():
            return
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(source)
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)

    def export_markdown(self, previews=None):
        """Save the note as a Markdown file; with `previews` (the notebook's embed provider)
        and the setting on, each embed is written as a code block of its code."""
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Note", self.session.title + ".md", "Markdown (*.md)")
        if not filename:
            return
        source = self.session.source
        if previews is None or not self._preferences.effective('export_embeds_as_code'):
            self._write_export(filename, source)
            return
        export = EmbedExport(source, previews,
                             show_addresses=self._preferences.effective('hlil_show_addresses'),
                             parent=self)
        export.done.connect(lambda text: self._write_export(filename, text))

    def _write_export(self, filename, text):
        output = QSaveFile(filename)
        encoded = text.encode("utf-8")
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            QMessageBox.warning(self, "Export Note", output.errorString())
        elif output.write(encoded) != len(encoded):
            output.cancelWriting()
            QMessageBox.warning(self, "Export Note", output.errorString())
        elif not output.commit():
            QMessageBox.warning(self, "Export Note", output.errorString())

    def changeEvent(self, event):
        super().changeEvent(event)
        if (event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)
                and self.materialized):
            self.session.highlighter.set_palette(self.palette())


class EmbedExport(QObject):
    """A note's Markdown with its embeds as code: looks their code up off the UI thread,
    then emits `done`. Shows a cancellable progress dialog if that takes a while."""

    done = Signal(str)
    PROGRESS_DELAY = 0.4

    def __init__(self, source, previews, *, show_addresses, parent):
        super().__init__(parent)
        self.source, self.show_addresses = source, show_addresses
        try:
            self.analysis = MarkdownParser().parse(source)
        except (ImportError, OSError):
            self.analysis = None  # Without the parser there are no embeds: export as written.
        self.embeds = embeds(self.analysis, previews.data) if self.analysis is not None else []
        self.futures = previews.snapshots({item.key for item in self.embeds})
        self.started = monotonic()
        self.dialog = None
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    def _poll(self):
        finished = sum(future.done() for future in self.futures.values())
        if finished == len(self.futures):
            self._finish()
            return
        if self.dialog is None and monotonic() - self.started > self.PROGRESS_DELAY:
            self.dialog = QProgressDialog("Exporting embedded code…", "Cancel", 0,
                                          len(self.futures), self.parent())
            self.dialog.setWindowTitle("Export Note")
            self.dialog.setWindowModality(Qt.WindowModality.WindowModal)
            self.dialog.canceled.connect(self.cancel)
            self.dialog.show()
        if self.dialog is not None:
            self.dialog.setValue(finished)

    def _close(self):
        self.timer.stop()
        if self.dialog is not None:
            self.dialog.canceled.disconnect(self.cancel)
            self.dialog.close()
            self.dialog.deleteLater()
        self.deleteLater()

    def cancel(self):
        for future in self.futures.values():
            future.cancel()
        self._close()

    def _finish(self):
        previews = {}
        for item in self.embeds:
            future = self.futures.get(item.key)
            if future is not None and not future.cancelled() and future.exception() is None:
                previews[item.link] = future.result()
        self._close()
        text = (export_markdown(self.source, self.analysis, previews,
                                show_addresses=self.show_addresses)
                if self.analysis is not None else self.source)
        self.done.emit(text)
