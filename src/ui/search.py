from __future__ import annotations

import math
import re
import sys
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from weakref import ref

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QPainter,
    QPalette,
    QPen,
    QTextCharFormat,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from ..markdown import utf16_length
from .common import NOTE_CONTENT_MARGIN, AppExecutor, FindKeyFilter, app_singleton, mix, single_shot
from .preferences import editor_preferences

CONTEXT_LINES = 2
MAX_CARD_LINES = 12
MAX_MATCHES = 2000


@dataclass(frozen=True)
class SearchNote:
    id: str
    title: str
    source: str
    revision: int


@dataclass(frozen=True)
class SearchMatch:
    start: int
    end: int
    line: int
    end_line: int


@dataclass(frozen=True)
class ContextLine:
    number: int
    text: str
    highlights: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class SearchSnippet:
    note_id: str
    title: str
    revision: int
    lines: tuple[ContextLine, ...]
    matches: tuple[SearchMatch, ...]


@dataclass(frozen=True)
class NoteResults:
    note_id: str
    title: str
    matches: int
    snippets: tuple[SearchSnippet, ...]


@dataclass(frozen=True)
class SearchResults:
    notes: tuple[NoteResults, ...] = ()
    matches: int = 0
    truncated: bool = False


def search_notes(notes, query, *, case_sensitive=False, limit=MAX_MATCHES):
    if not query or limit < 1:
        return SearchResults()
    pattern = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    results, count, truncated = [], 0, False
    for note in notes:
        matches = []
        previous, position = 0, 0
        for match in pattern.finditer(note.source):
            if count >= limit:
                truncated = True
                break
            position += utf16_length(note.source[previous:match.start()])
            end = position + utf16_length(match.group())
            matches.append((position, end, match.start(), match.end()))
            previous, position = match.end(), end
            count += 1
        if matches:
            rows = note.source.split('\n')
            py_starts, starts = [], []
            py_position, position = 0, 0
            for row in rows:
                py_starts.append(py_position)
                starts.append(position)
                py_position += len(row) + 1
                position += utf16_length(row) + 1
            found = tuple(SearchMatch(start, end, bisect_right(py_starts, py_start),
                                      bisect_right(py_starts, max(py_start, py_end - 1)))
                          for start, end, py_start, py_end in matches)
            windows = []
            for match in found:
                left = max(1, match.line - CONTEXT_LINES)
                right = min(len(rows), match.end_line + CONTEXT_LINES)
                if (windows and left <= windows[-1][1] + 1
                        and right - windows[-1][0] + 1 <= MAX_CARD_LINES):
                    windows[-1][1] = right
                    windows[-1][2].append(match)
                else:
                    windows.append([left, right, [match]])
            snippets = []
            for left, right, hits in windows:
                context = []
                for row in range(left - 1, right):
                    start = starts[row]
                    end = start + utf16_length(rows[row])
                    spans = tuple((max(hit.start, start) - start, min(hit.end, end) - start)
                                  for hit in found if hit.start < end and hit.end > start)
                    context.append(ContextLine(row + 1, rows[row], spans))
                snippets.append(SearchSnippet(note.id, note.title, note.revision,
                                               tuple(context), tuple(hits)))
            results.append(NoteResults(note.id, note.title, len(found), tuple(snippets)))
        if truncated:
            break
    return SearchResults(tuple(results), count, truncated)


def create_filter_edit(owner, *, in_note=False, parent=None):
    parent = owner if parent is None else parent
    ui = sys.modules.get('binaryninjaui')
    if ui is None or not hasattr(ui, 'FilterEdit'):
        query = QLineEdit(parent)
        query.setClearButtonEnabled(True)
        query.setMinimumHeight(30)
        return query, None

    class Target(ui.FilterTarget):
        def __init__(self):
            super().__init__()
            self._owner = ref(owner)

        def owner(self):
            widget = self._owner()
            return widget if widget is not None and isValid(widget) else None

        def setFilter(self, _text, _options=None):
            widget = self.owner()
            if widget is not None and not in_note and hasattr(widget, 'status'):
                widget.refresh()

        def scrollToFirstItem(self):
            widget = self.owner()
            if widget is not None:
                if in_note:
                    widget.editor.ensureCursorVisible()
                else:
                    widget.result_list.scrollToTop()

        def ensureSelection(self):
            widget = self.owner()
            if widget is None or in_note:
                # A note always has a cursor; Enter advances it in activateSelection.
                return
            view = widget.result_list
            current = view.currentIndex()
            rows = view.results_model.rows
            if current.isValid() and isinstance(rows[current.row()], SearchSnippet):
                return
            for index, row in enumerate(rows):
                if isinstance(row, SearchSnippet):
                    view.setCurrentIndex(view.model().index(index, 0))
                    break

        def scrollToCurrentItem(self):
            self.ensureSelection()
            widget = self.owner()
            if widget is not None:
                if in_note:
                    widget.editor.show_source_cursor(widget.editor.textCursor(), focus_table=False)
                else:
                    widget.result_list.scrollTo(widget.result_list.currentIndex())

        def activateSelection(self):
            self.ensureSelection()
            widget = self.owner()
            if widget is not None:
                if in_note:
                    widget.find_next()
                    return
                index = widget.result_list.currentIndex()
                if index.isValid():
                    widget.result_list._activate(index)
                else:
                    widget._open_first()

        def closeFilter(self):
            widget = self.owner()
            if widget is not None:
                if in_note:
                    widget.hide_find()
                else:
                    widget.result_list.setFocus()

    target = Target()
    query = ui.FilterEdit(target)
    query.setParent(parent)
    # FilterEdit keeps a raw pointer to its target; keep the Python object alive.
    query._bnnotes_filter_target = target
    query.showRegexToggle(False)
    return query, lambda: bool(query.getFilterOptions() & ui.FilterOption.CaseSensitiveOption)


class SearchResultsModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = ()
        self.line_number_digits = 1

    def set_results(self, results):
        self.beginResetModel()
        self.rows = tuple(row for note in results.notes for row in (note, *note.snippets))
        last = max((snippet.lines[-1].number for note in results.notes
                    for snippet in note.snippets), default=1)
        self.line_number_digits = len(str(last))
        self.endResetModel()

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role == Qt.ItemDataRole.DisplayRole:
            return row.title if isinstance(row, NoteResults) else '\n'.join(
                line.text for line in row.lines
            )
        if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleTextRole):
            if isinstance(row, NoteResults):
                return f'{row.title}: {row.matches} matches'
            return f'Open {row.title} at line {row.matches[0].line}'
        return None

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsEnabled
        if index.isValid() and isinstance(self.rows[index.row()], SearchSnippet):
            flags |= Qt.ItemFlag.ItemIsSelectable
        return flags


class SearchTheme:
    def __init__(self, palette):
        self.base, self.text = palette.base().color(), palette.text().color()
        self.surface = mix(self.base, self.text, 0.035)
        self.border = mix(self.base, self.text, 0.17)
        self.muted = mix(self.surface, self.text, 0.55)
        self.accent = palette.highlight().color()
        if self.base.lightness() < 128 and self.accent.lightness() < 140:
            self.accent = self.accent.lighter(160)
        self.line = mix(self.surface, self.accent, 0.13)
        self.match = mix(self.surface, self.accent, 0.36)


def monospace_font(font):
    result = QFont(font)
    system = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    result.setFamilies([system] if QFontDatabase.isFixedPitch(system)
                       else ['Menlo', 'Consolas', 'DejaVu Sans Mono', 'monospace'])
    result.setStyleHint(QFont.StyleHint.Monospace)
    result.setWeight(QFont.Weight.Normal)
    return result


@dataclass
class RenderedLine:
    number: int
    layout: QTextLayout
    top: float
    height: float
    matched: bool


class SnippetLayout:
    def __init__(self, snippet, width, font, theme, digits):
        self.header_font = QFont(font)
        self.header_font.setPointSizeF(max(9, font.pointSizeF() - 1))
        self.header_height = math.ceil(QFontMetricsF(self.header_font).height()) + 16
        self.font = monospace_font(font)
        metrics = QFontMetricsF(self.font)
        self.gutter = math.ceil(metrics.horizontalAdvance('9' * digits)) + 24
        self.text_left = self.gutter + 12
        self.lines = []
        top = self.header_height
        for row in snippet.lines:
            layout = QTextLayout(row.text, self.font)
            option = QTextOption()
            option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            option.setTabStopDistance(metrics.horizontalAdvance(' ') * 4)
            layout.setTextOption(option)
            formats = []
            for start, end in row.highlights:
                span = QTextLayout.FormatRange()
                span.start, span.length = start, end - start
                fmt = QTextCharFormat()
                fmt.setBackground(theme.match)
                fmt.setForeground(theme.text)
                fmt.setFontWeight(QFont.Weight.DemiBold)
                span.format = fmt
                formats.append(span)
            layout.setFormats(formats)
            layout.beginLayout()
            height = 0.0
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(max(16, width - self.text_left - 12))
                line.setPosition(QPointF(0, height))
                height += line.height()
            layout.endLayout()
            height = max(metrics.height(), height) + 6
            self.lines.append(RenderedLine(row.number, layout, top, height, bool(row.highlights)))
            top += height
        self.height = math.ceil(top + 8)


class SearchResultDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self._layouts = OrderedDict()

    def clear(self):
        self._layouts.clear()

    def layout_for(self, snippet):
        width = max(80, self.view.viewport().width() - 16)
        font, palette = self.view.font(), self.view.palette()
        digits = self.view.results_model.line_number_digits
        key = snippet, width, font.toString(), palette.cacheKey(), digits
        layout = self._layouts.get(key)
        if layout is None:
            layout = SnippetLayout(snippet, width, font, SearchTheme(palette), digits)
            self._layouts[key] = layout
            if len(self._layouts) > 128:
                self._layouts.popitem(last=False)
        else:
            self._layouts.move_to_end(key)
        return layout

    def sizeHint(self, option, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        height = (math.ceil(QFontMetricsF(self.view.font()).height()) + 24
                  if isinstance(item, NoteResults) else self.layout_for(item).height + 12)
        return QSize(self.view.viewport().width(), height)

    def paint(self, painter, option, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        theme = SearchTheme(option.palette)
        painter.save()
        painter.setClipRect(option.rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if isinstance(item, NoteResults):
            font = QFont(self.view.font())
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            metrics = QFontMetricsF(font)
            count = f'{item.matches} match' + ('es' if item.matches != 1 else '')
            count_width = metrics.horizontalAdvance(count) + 20
            bounds = QRectF(option.rect).adjusted(10, 8, -10, 0)
            title = metrics.elidedText(item.title, Qt.TextElideMode.ElideRight,
                                       max(16, bounds.width() - count_width))
            painter.setPen(theme.text)
            painter.drawText(bounds, Qt.AlignmentFlag.AlignVCenter, title)
            painter.setPen(theme.muted)
            painter.drawText(bounds, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             count)
        else:
            layout = self.layout_for(item)
            card = QRectF(option.rect).adjusted(8, 4, -8, -8)
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            hot = bool(option.state & QStyle.StateFlag.State_MouseOver)
            painter.setPen(QPen(theme.accent if selected or hot else theme.border, 1))
            painter.setBrush(theme.surface)
            painter.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            painter.translate(card.topLeft())
            painter.setFont(layout.header_font)
            painter.setPen(theme.muted)
            first, last = item.lines[0].number, item.lines[-1].number
            caption = f'Line {first}' if first == last else f'Lines {first}–{last}'
            painter.drawText(QRectF(12, 0, card.width() - 50, layout.header_height),
                             Qt.AlignmentFlag.AlignVCenter, caption)
            painter.setPen(theme.accent if selected or hot else theme.muted)
            painter.drawText(QRectF(card.width() - 34, 0, 22, layout.header_height),
                             Qt.AlignmentFlag.AlignCenter, '↗')
            painter.setFont(layout.font)
            for row in layout.lines:
                if row.matched:
                    painter.fillRect(QRectF(1, row.top, card.width() - 2, row.height), theme.line)
                    painter.fillRect(QRectF(1, row.top, 2, row.height), theme.accent)
                painter.setPen(theme.accent if row.matched else theme.muted)
                metrics = QFontMetricsF(layout.font)
                baseline = row.top + 3 + (row.layout.lineAt(0).ascent()
                                          if row.layout.lineCount() else metrics.ascent())
                number = str(row.number)
                painter.drawText(QPointF(layout.gutter - 10 - metrics.horizontalAdvance(number),
                                         baseline), number)
                painter.setPen(theme.text)
                row.layout.draw(painter, QPointF(layout.text_left, row.top + 3))
            painter.setPen(QPen(mix(theme.surface, theme.text, 0.09), 1))
            painter.drawLine(QPointF(layout.gutter, layout.header_height),
                             QPointF(layout.gutter, layout.height - 8))
        painter.restore()


class SearchResultsView(QListView):
    resultActivated = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName('Matches in all notes')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(32)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMouseTracking(True)
        self.results_model = SearchResultsModel(self)
        self.setModel(self.results_model)
        self.delegate = SearchResultDelegate(self)
        self.setItemDelegate(self.delegate)
        self._click_point = None
        self.clicked.connect(self._clicked)
        self.activated.connect(self._activate)

    def set_results(self, results):
        self.delegate.clear()
        self.results_model.set_results(results)

    def mouseReleaseEvent(self, event):
        self._click_point = event.position().toPoint()
        super().mouseReleaseEvent(event)
        self._click_point = None

    def _clicked(self, index):
        self._activate(index, self._click_point)

    def _activate(self, index, point=None):
        snippet = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(snippet, SearchSnippet):
            return
        match = snippet.matches[0]
        if point is not None:
            y = point.y() - self.visualRect(index).top() - 4
            layout = self.delegate.layout_for(snippet)
            row = next((row for row in layout.lines if row.top <= y < row.top + row.height), None)
            if row is not None:
                match = min(snippet.matches, key=lambda hit: abs(hit.line - row.number))
        self.resultActivated.emit(snippet, match)

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, 'delegate') and event.type() in (
            QEvent.Type.FontChange, QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
        ):
            self.delegate.clear()
            self.scheduleDelayedItemsLayout()
            self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'delegate'):
            # A new scrollbar narrows the viewport; recompute wrapped heights.
            self.delegate.clear()
            self.scheduleDelayedItemsLayout()


def search_pool():
    return app_singleton('_bnnotes_search_pool', lambda app: AppExecutor(app, 1, 'bnnotes-search'))


def _cancel(jobs):
    if jobs[0] is not None:
        jobs[0].cancel()
        jobs[0] = None


class FindAllNotesWidget(QWidget):
    resultActivated = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('bnnotes-find-all')
        self.setAccessibleName('Find in all notes')
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)
        self.notebook = None
        self.results = SearchResults()
        self._jobs = [None]
        jobs = self._jobs
        self.destroyed.connect(lambda *_args: _cancel(jobs))
        self._debounce = single_shot(self, self._search, 120)
        self._poll = QTimer(self)
        self._poll.setInterval(30)
        self._poll.timeout.connect(self._collect)
        self._preferences = editor_preferences()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(NOTE_CONTENT_MARGIN, 8, NOTE_CONTENT_MARGIN, 8)
        layout.setSpacing(10)
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.query, self._native_case_sensitive = create_filter_edit(self)
        self.query.setPlaceholderText('Search all notes…')
        self.query.setAccessibleName('Search all notes')
        search_row.addWidget(self.query, 1)
        self.match_case = None
        if self._native_case_sensitive is None:
            self.match_case = QToolButton(self)
            self.match_case.setText('Aa')
            self.match_case.setCheckable(True)
            self.match_case.setToolTip('Match case')
            self.match_case.setAccessibleName('Match case')
            self.match_case.setFixedSize(32, 30)
            search_row.addWidget(self.match_case)
            self.match_case.toggled.connect(self.refresh)
            self.query.returnPressed.connect(self._open_first)
        else:
            self.query.optionsChanged.connect(self.refresh)
        layout.addLayout(search_row)
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.stack = QStackedWidget(self)
        self.result_list = SearchResultsView(self.stack)
        self.stack.addWidget(self.result_list)
        self.empty = QLabel(self.stack)
        self.empty.setTextFormat(Qt.TextFormat.PlainText)
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContentsMargins(16, 30, 16, 30)
        self.stack.addWidget(self.empty)
        layout.addWidget(self.stack, 1)
        self.query.textChanged.connect(self.refresh)
        self.result_list.resultActivated.connect(self.resultActivated.emit)
        self._find_keys = FindKeyFilter(self, self.focus_query, self.query.isEnabled)
        self.setFocusProxy(self.query)
        self._preferences.changed.connect(self._apply_preferences)
        self._apply_preferences('font_size')
        self.bind_notebook(None)

    def _apply_preferences(self, name):
        if name != 'font_size':
            return
        font = self._preferences.font()
        self.result_list.setFont(font)
        if self._native_case_sensitive is None:
            self.query.setFont(font)

    def bind_notebook(self, notebook):
        self._preferences.changed.disconnect(self._apply_preferences)
        self._preferences = notebook.preferences if notebook is not None else editor_preferences()
        self._preferences.changed.connect(self._apply_preferences)
        self._apply_preferences('font_size')
        if self.notebook is not None:
            self.notebook.noteChanged.disconnect(self.refresh)
            self.notebook.notesChanged.disconnect(self.refresh)
        self.notebook = notebook
        if notebook is not None:
            notebook.noteChanged.connect(self.refresh)
            notebook.notesChanged.connect(self.refresh)
        self.refresh()

    def focus_query(self):
        self.query.setFocus()
        self.query.selectAll()

    def _message(self, text):
        self.empty.setText(text)
        self.stack.setCurrentWidget(self.empty)

    def refresh(self, *_args):
        self._debounce.stop()
        self._poll.stop()
        _cancel(self._jobs)
        self.results = SearchResults()
        self.result_list.set_results(self.results)
        available = self.notebook is not None and not self.notebook.load_failed
        self.query.setEnabled(available)
        if self.match_case is not None:
            self.match_case.setEnabled(available)
        if not available:
            self.status.setText('')
            self._message('Open a binary to search its notes.' if self.notebook is None
                          else 'Notes could not be loaded for this file.')
            return
        if not self.query.text():
            count = len(self.notebook.notes)
            self.status.setText(f'{count} note' + ('s' if count != 1 else '') + ' available')
            self._message('Type to search across your notes.\n'
                          'Results include matching lines and nearby context.')
            return
        self.status.setText('Searching…')
        self._message('Searching notes…')
        self._debounce.start()

    def _search(self):
        if self.notebook is None or not self.query.text():
            return
        pool = search_pool()
        if pool.closed:
            return
        notes = tuple(SearchNote(note.id, note.title, note.source, note.source_revision)
                      for note in self.notebook.notes)
        case_sensitive = (self._native_case_sensitive() if self._native_case_sensitive is not None
                          else self.match_case.isChecked())
        self._jobs[0] = pool.executor.submit(search_notes, notes, self.query.text(),
                                             case_sensitive=case_sensitive)
        self._poll.start()

    def _collect(self):
        future = self._jobs[0]
        if future is None or not future.done():
            return
        self._jobs[0] = None
        self._poll.stop()
        if future.cancelled():
            return
        try:
            self.results = future.result()
        except Exception:
            self.status.setText('Search could not be completed')
            self._message('Try searching again.')
            return
        self.result_list.set_results(self.results)
        count, notes = self.results.matches, len(self.results.notes)
        summary = f'{count:,}' + ('+' if self.results.truncated else '')
        summary += ' match' + ('es' if count != 1 else '')
        summary += f' in {notes} note' + ('s' if notes != 1 else '')
        if self.results.truncated:
            summary += ' · Result limit reached; refine your search.'
        self.status.setText(summary)
        if count:
            self.stack.setCurrentWidget(self.result_list)
        else:
            self._message(f'No matches for “{self.query.text()}”.\nTry another word or phrase.')

    def _open_first(self):
        if self.results.notes:
            snippet = self.results.notes[0].snippets[0]
            self.resultActivated.emit(snippet, snippet.matches[0])
        elif self._debounce.isActive():
            self._debounce.stop()
            self._search()
