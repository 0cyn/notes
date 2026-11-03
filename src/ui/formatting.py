from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from functools import lru_cache

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QTextCursor, QTextDocument
from PySide6.QtWidgets import QApplication, QMenu, QTextEdit

from ..markdown import MarkdownParser, cell_editor_text, source_line_positions, utf16_length
from .common import mix, single_shot, watch_focus
from .highlighter import code_block_colors
from .layout import QUOTE_INDENT, QUOTE_VERTICAL_PADDING


@lru_cache(maxsize=1)
def _inline_parser():
    return MarkdownParser()


def _selected_formats(formats, start, end, *, role=None, contained=False):
    by_span = {(item.start, item.end): item for item in formats}
    by_content = {(item.content_start, item.content_end): item for item in formats}
    for item in formats:
        if role is not None and item.role != role:
            continue
        if contained and start <= item.start and item.end <= end:
            yield item
            continue
        core_start, core_end = item.content_start, item.content_end
        while (nested := by_span.get((core_start, core_end))) is not None:
            core_start, core_end = nested.content_start, nested.content_end
        outer_start, outer_end = item.start, item.end
        while (parent := by_content.get((outer_start, outer_end))) is not None:
            outer_start, outer_end = parent.start, parent.end
        if outer_start <= start <= core_start and core_end <= end <= outer_end:
            yield item


def _format_markers(source, start, end, delimiter, analysis=None):
    """Parser-paired markers of `style` around the selection, at any nesting depth."""
    if analysis is None:
        try:
            analysis = _inline_parser().parse_inline(source)
        except (ImportError, RuntimeError, TypeError):
            # Preserve basic source editing when optional grammars are missing.
            return _literal_markers(source, start, end, delimiter)
    role = {
        "*": "emphasis", "_": "emphasis", "**": "strong", "__": "strong",
        "`": "code", "~~": "strike", "==": "highlight", "$": "math", "%%": "comment",
    }.get(delimiter)
    markers = []
    if role is not None:
        for item in _selected_formats(analysis.inline_formats, start, end, role=role):
            markers.extend(((item.start, item.content_start), (item.content_end, item.end)))
    if markers:
        # Remove every matching wrapper, including redundant nesting.
        return sorted(set(markers), reverse=True)
    # An empty pair at the caret has no parsed node yet.
    selected = source.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")
    if start == end or selected == delimiter * 2:
        return _literal_markers(source, start, end, delimiter)
    return []


def _marker_run(source, position, marker):
    left, right = position, position
    while left > 0 and source[left - 1] == marker:
        left -= 1
    while right < len(source) and source[right] == marker:
        right += 1
    return left, right


def _escaped(source, position):
    left = position
    while left > 0 and source[left - 1] == "\\":
        left -= 1
    return (position - left) % 2 != 0


def _literal_wrappers(source, start, end, delimiter):
    """Find matching markers just outside, or included in, the selection."""
    width = len(delimiter)
    for left, right in ((start - width, end), (start, end - width)):
        if not 0 <= left <= left + width <= right <= len(source) - width:
            continue
        if source[left:left + width] != delimiter or source[right:right + width] != delimiter:
            continue
        opening = _marker_run(source, left, delimiter[0])
        closing = _marker_run(source, right, delimiter[0])
        if _escaped(source, opening[0]) or _escaped(source, right):
            continue
        if left + width != right:
            # One '*' doesn't wrap '**bold**'; '***' holds both styles, removable separately.
            lengths = (opening[1] - opening[0], closing[1] - closing[0])
            if delimiter in ("*", "_") and any(length % 2 == 0 for length in lengths):
                continue
            if delimiter[0] == "`" and any(length != width for length in lengths):
                continue
        return left, right
    return None


def _literal_markers(source, start, end, delimiter):
    encoded = source.encode("utf-16-le")
    first = len(encoded[:start * 2].decode("utf-16-le"))
    last = len(encoded[:end * 2].decode("utf-16-le"))
    markers = _literal_wrappers(source, first, last, delimiter)
    if markers is None:
        return []
    width = utf16_length(delimiter)
    return [(position, position + width)
            for position in (utf16_length(source[:index]) for index in reversed(markers))]


def toggle_inline_text(source, delimiter):
    """Apply the same toggle to a whole selected grid cell."""
    markers = _format_markers(source, 0, utf16_length(source), delimiter)
    if not markers:
        return delimiter + source + delimiter
    encoded = source.encode("utf-16-le")
    for left, right in markers:
        encoded = encoded[:left * 2] + encoded[right * 2:]
    return encoded.decode("utf-16-le")


def toggle_inline_selection(cursor, delimiter, analysis=None):
    """Edit only the markers, keeping the selected text and its direction."""
    source = cursor.document().toRawText()
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    backward = cursor.position() < cursor.anchor()
    markers = _format_markers(source, start, end, delimiter, analysis)
    width = utf16_length(delimiter)
    cursor.beginEditBlock()
    if markers:
        for left, right in markers:
            cursor.setPosition(left)
            cursor.setPosition(right, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            start -= max(0, min(start, right) - left)
            end -= max(0, min(end, right) - left)
    else:
        cursor.setPosition(end)
        cursor.insertText(delimiter)
        cursor.setPosition(start)
        cursor.insertText(delimiter)
        start, end = start + width, end + width
    cursor.endEditBlock()
    cursor.setPosition(end if backward else start)
    cursor.setPosition(start if backward else end, QTextCursor.MoveMode.KeepAnchor)
    return cursor


def selection_has_format(cursor, delimiter, analysis=None):
    return bool(_format_markers(cursor.document().toPlainText(), cursor.selectionStart(),
                                cursor.selectionEnd(), delimiter, analysis))


def remove_source_ranges(cursor, ranges):
    """Remove a union of source ranges while preserving the surviving selection."""
    merged = []
    for left, right in sorted(ranges):
        if left >= right:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(right, merged[-1][1])
        else:
            merged.append((left, right))
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    backward = cursor.position() < cursor.anchor()
    for left, right in reversed(merged):
        cursor.setPosition(left)
        cursor.setPosition(right, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        start -= max(0, min(start, right) - left)
        end -= max(0, min(end, right) - left)
    cursor.setPosition(end if backward else start)
    cursor.setPosition(start if backward else end, QTextCursor.MoveMode.KeepAnchor)
    return cursor


def clear_inline_selection(cursor, analysis=None):
    if analysis is None:
        try:
            analysis = _inline_parser().parse(cursor.document().toPlainText())
        except (ImportError, RuntimeError, TypeError):
            return cursor
    ranges = []
    for item in _selected_formats(analysis.inline_formats, cursor.selectionStart(),
                                  cursor.selectionEnd(), contained=True):
        ranges.extend(((item.start, item.content_start), (item.content_end, item.end)))
    return remove_source_ranges(cursor, ranges)


def continue_quote(editor, analysis):
    cursor = editor.textCursor()
    if cursor.hasSelection():
        return False
    row = analysis.line_at_position(cursor.position())
    quote = next((item for item in analysis.quote_lines if item.line == row), None)
    if quote is None or cursor.position() != quote.end:
        return False
    source = editor.toPlainText().encode("utf-16-le")
    body = source[quote.content_start * 2:quote.end * 2].decode("utf-16-le")
    if body.strip():
        prefix = source[quote.start * 2:quote.content_start * 2].decode("utf-16-le")
        prefix += "> " * max(0, quote.depth - len(quote.markers))
        cursor.insertText("\n" + prefix)
    elif quote.markers:
        cursor.setPosition(quote.markers[-1][0])
        cursor.setPosition(quote.end, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
    else:
        return False
    editor.setTextCursor(cursor)
    return True


class QuotePresentation(QObject):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.highlighter = None
        self._analysis = None
        self._quotes = {}
        self._ordered = ()
        self._starts = ()
        self._selection_key = None
        self._timer = single_shot(self, self.refresh)
        editor.installEventFilter(self)
        editor.cursorPositionChanged.connect(self.refresh)
        editor.selectionChanged.connect(self.refresh)
        editor.updateRequest.connect(self.refresh)
        watch_focus(editor, self.refresh)

    def bind(self, highlighter):
        if self.highlighter is not None:
            self.highlighter.analysisChanged.disconnect(self.refresh)
        self.highlighter = highlighter
        if highlighter is not None:
            highlighter.analysisChanged.connect(self.refresh)
        self.refresh()

    @property
    def analysis(self):
        if (self.highlighter is not None and self.highlighter.analysis_is_current
                and not self.highlighter.warning):
            return self.highlighter.analysis
        return None

    def active_line(self):
        analysis = self.analysis
        if analysis is None or self.editor.isReadOnly():
            return None
        editing = self.editor.hasFocus()
        popup = QApplication.activePopupWidget()
        if popup is not None:
            while isinstance(popup.parentWidget(), QMenu):
                popup = popup.parentWidget()
            editing = editing or popup.parentWidget() is self.editor
        return analysis.line_at_position(self.editor.textCursor().position()) if editing else None

    def refresh(self, *_args):
        if not self.editor.isVisible():
            return
        analysis, active = self.analysis, self.active_line()
        if analysis is not self._analysis:
            self._analysis = analysis
            self._ordered = tuple(quote for quote in analysis.quote_lines if not any(
                table.start_position <= quote.start <= table.end_position
                for table in analysis.tables
            )) if analysis is not None else ()
            self._quotes = {quote.line: quote for quote in self._ordered}
            self._starts = tuple(quote.start for quote in self._ordered)
        editor = self.editor
        layout = editor.document().documentLayout()
        if hasattr(layout, 'block_at_y'):
            first = editor.firstVisibleBlock().position()
            last = layout.block_at_y(editor.verticalScrollBar().value()
                                     + editor.viewport().height())
            candidates = self._ordered[max(0, bisect_right(self._starts, first) - 1):
                                       bisect_left(self._starts, last.position() + last.length())]
        else:
            candidates = self._ordered
        key = id(analysis), active, tuple(quote.start for quote in candidates)
        if key == self._selection_key:
            return
        self._selection_key = key
        selections = []
        if analysis is not None:
            for quote in candidates:
                if quote.line == active:
                    continue
                for left, right in quote.markers:
                    selection = QTextEdit.ExtraSelection()
                    selection.cursor = QTextCursor(self.editor.document())
                    selection.cursor.setPosition(left)
                    selection.cursor.setPosition(right, QTextCursor.MoveMode.KeepAnchor)
                    selection.format.setForeground(QColor(Qt.GlobalColor.transparent))
                    selections.append(selection)
        self.editor.setExtraSelections(selections)
        self.editor.viewport().update()

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() in (
            QEvent.Type.FocusIn, QEvent.Type.FocusOut, QEvent.Type.Show,
        ):
            self._timer.start(0)
        return False

    def visual_lines(self):
        analysis, active = self.analysis, self.active_line()
        if analysis is None or not analysis.quote_lines:
            return
        quotes = self._quotes
        editor = self.editor
        viewport = editor.viewport().rect()
        block = editor.firstVisibleBlock()
        while block.isValid():
            if not block.isVisible():
                block = block.next()
                continue
            bounds = editor.blockBoundingGeometry(block).translated(editor.contentOffset())
            if bounds.top() > viewport.bottom():
                break
            layout = block.layout()
            for index in range(layout.lineCount()):
                line = layout.lineAt(index)
                position = block.position() + line.textStart()
                row = analysis.line_at_position(position)
                quote = quotes.get(row)
                if quote is None or row == active:
                    continue
                top = bounds.top() + line.y()
                bottom = top + line.height()
                if row - 1 not in quotes and position == quote.start:
                    top -= QUOTE_VERTICAL_PADDING
                if row + 1 not in quotes and position + line.textLength() >= quote.end:
                    bottom += QUOTE_VERTICAL_PADDING
                if bottom < viewport.top() or top > viewport.bottom():
                    continue
                margin = editor.document().documentMargin()
                left = editor.contentOffset().x() + margin
                # The background spans the text column (cell editors keep a small right inset).
                right = viewport.width() - max(8.0, margin)
                yield quote, QRectF(left, top, max(1, right - left), bottom - top)
            block = block.next()

    def _marker_rect(self, left, right):
        editor = self.editor
        block = editor.document().findBlock(left)
        bounds = editor.blockBoundingGeometry(block).translated(editor.contentOffset())
        line = block.layout().lineForTextPosition(left - block.position())
        x1 = line.cursorToX(left - block.position())[0]
        x2 = line.cursorToX(right - block.position())[0]
        return QRectF(bounds.left() + min(x1, x2), bounds.top() + line.y(),
                      abs(x2 - x1), line.height())

    def paint(self, event, *, background=False, code_frames=()):
        lines = list(self.visual_lines())
        if not lines:
            return
        base = self.editor.palette().base().color()
        text = self.editor.palette().text().color()
        fill = mix(base, text, 0.07 if base.lightness() < 128 else 0.025)
        painter = QPainter(self.editor.viewport())
        painter.setClipRegion(event.region())
        if background:
            for _quote, rectangle in lines:
                painter.fillRect(rectangle, fill)
        else:
            # Qt repaints selections natively; mask only the marker advances afterward.
            cursor = self.editor.textCursor()
            painted = set()
            for quote, _rectangle in lines:
                if quote.line in painted:
                    continue
                painted.add(quote.line)
                for left, right in quote.markers:
                    marker = self._marker_rect(left, right)
                    painter.fillRect(marker, fill)
                    for frame in code_frames:
                        overlap = marker.intersected(frame)
                        if not overlap.isEmpty():
                            painter.fillRect(overlap, code_block_colors(self.editor.palette())[0])
                    selected_left = max(left, cursor.selectionStart())
                    selected_right = min(right, cursor.selectionEnd())
                    if selected_left < selected_right:
                        group = (QPalette.ColorGroup.Active if self.editor.hasFocus()
                                 else QPalette.ColorGroup.Inactive)
                        color = self.editor.palette().color(group, QPalette.ColorRole.Highlight)
                        painter.fillRect(self._marker_rect(selected_left, selected_right), color)
            rule = mix(base, text, 0.32)
            for quote, rectangle in lines:
                for depth in range(quote.depth):
                    x = rectangle.left() + depth * QUOTE_INDENT
                    # Strips fill the exact bounds; stroked lines would extend past them.
                    painter.fillRect(QRectF(x, rectangle.top(), 4, rectangle.height()), rule)
        painter.end()


_PREFIX = re.compile(r"^(?:#{1,6}[ \t]+|>[ \t]?|(?:[-+*]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?)")


def _paragraph_parts(line):
    indent = re.match(r"[ \t]*", line)[0]
    body = line[len(indent):]
    while match := _PREFIX.match(body):
        body = body[match.end():]
    return indent, body, len(line) - len(body)


def paragraph_selection(cursor, style):
    document = cursor.document()
    starts, ends = source_line_positions(document.toPlainText())
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    first, last = bisect_right(starts, start) - 1, bisect_right(starts, end) - 1
    if cursor.hasSelection() and last > first and starts[last] == end:
        last -= 1
    cursor.setPosition(starts[first])
    cursor.setPosition(ends[last], QTextCursor.MoveMode.KeepAnchor)
    lines = cursor.selectedText().replace("\u2029", "\n").replace("\u2028", "\n").split("\n")
    result, number = [], 0
    for line in lines:
        indent, body, _prefix = _paragraph_parts(line)
        if not body.strip() and len(lines) > 1 and style != "quote":
            result.append("")
            continue
        number += 1
        prefix = {
            "normal": "", "quote": "> ", "bullet": "- ", "numbered": f"{number}. ",
            "task": "- [x] " if re.search(r"\[[xX]\]", line) else "- [ ] ",
        }.get(style, "")
        if style.startswith("heading"):
            prefix = "#" * int(style[-1]) + " "
        result.append(indent + prefix + body)
    replacement = "\n".join(result)
    cursor.insertText(replacement)
    cursor.setPosition(starts[first])
    cursor.setPosition(starts[first] + utf16_length(replacement), QTextCursor.MoveMode.KeepAnchor)


def clear_selection(cursor, analysis=None):
    if not cursor.hasSelection():
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    source = cursor.document().toPlainText()
    if analysis is None:
        try:
            analysis = MarkdownParser().parse(source)
        except (ImportError, RuntimeError, TypeError):
            return
    starts, _ends = source_line_positions(source)
    prefixes = []
    for row, (offset, line) in enumerate(zip(starts, source.split("\n"), strict=True)):
        indent, _body, prefix = _paragraph_parts(line)
        left, right = offset + utf16_length(indent), offset + utf16_length(line[:prefix])
        if left >= right or row in analysis.code_lines or any(
            item.role in ("code", "math", "comment") and item.start <= left < item.end
            for item in analysis.inline_formats
        ):
            continue
        marker, whole_line = QTextCursor(cursor.document()), QTextCursor(cursor.document())
        marker.setPosition(left)
        marker.setPosition(right, QTextCursor.MoveMode.KeepAnchor)
        whole_line.setPosition(offset)
        whole_line.setPosition(offset + utf16_length(line), QTextCursor.MoveMode.KeepAnchor)
        prefixes.append((marker, whole_line))
    # Only prefixes that were paragraph syntax: `- literal` keeps its hyphen.
    clear_inline_selection(cursor, analysis)
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    ranges = [(marker.selectionStart(), marker.selectionEnd()) for marker, line in prefixes
              if start <= marker.selectionEnd() and line.selectionEnd() <= end]
    remove_source_ranges(cursor, ranges)


def link_selection(cursor, *, external=False):
    text = cursor.selectedText().replace("\u2029", " ").replace("\u2028", " ")
    start = cursor.selectionStart()
    if external:
        label = (text or "Link text").replace("\\", "\\\\").replace("[", r"\[").replace("]", r"\]")
        prefix, target = f"[{label}](", "https://"
        cursor.insertText(prefix + target + ")")
        start += utf16_length(prefix)
    else:
        target = (text or "Link target").replace("\\", "\\\\").replace("]", r"\]").replace(
            "|", r"\|"
        )
        cursor.insertText("[[" + target + "]]")
        start += 2
    cursor.setPosition(start)
    cursor.setPosition(start + utf16_length(target), QTextCursor.MoveMode.KeepAnchor)


def insert_block(cursor, kind):
    document = cursor.document()
    encoded = document.toPlainText().encode("utf-16-le")
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    before = encoded[:start * 2].decode("utf-16-le")
    after = encoded[end * 2:].decode("utf-16-le")
    selected = cursor.selectedText().replace("\u2029", "\n").replace("\u2028", "\n")
    leading = "" if not before or before.endswith("\n\n") else (
        "\n" if before.endswith("\n") else "\n\n"
    )
    trailing = "" if after.startswith("\n\n") else "\n" if after.startswith("\n") else "\n\n"
    if kind == "rule":
        body, inner = "---", 3
        selected_length = 0
    else:
        if kind == "code":
            longest = max((len(run) for run in re.findall(r"`+", selected)), default=0)
            marker = "`" * max(3, longest + 1)
        else:
            marker = "$$"
        body = marker + "\n" + selected + "\n" + marker
        inner, selected_length = len(marker) + 1, utf16_length(selected)
    cursor.insertText(leading + body + trailing)
    position = start + utf16_length(leading) + inner
    if kind == "rule":
        position = start + utf16_length(leading + body) + 2
    cursor.setPosition(position)
    cursor.setPosition(position + selected_length, QTextCursor.MoveMode.KeepAnchor)


class TextActions:
    def __init__(self, editor, session, highlighter, *, cell=False):
        self.editor, self.session, self.highlighter, self.cell = editor, session, highlighter, cell

    @property
    def parent(self):
        return self.editor

    @property
    def editable(self):
        return (not self.editor.isReadOnly() and self.session is not None
                and not self.session.load_failed)

    @property
    def has_selection(self):
        return self.editor.textCursor().hasSelection()

    @property
    def has_text(self):
        return self.editor.document().characterCount() > 1

    def _analysis(self):
        if self.highlighter is None:
            return None
        self.highlighter.refresh()
        return self.highlighter.analysis if not self.highlighter.warning else None

    def _edit(self, operation):
        if not self.editable:
            return
        if self.cell:
            self.editor.token = object()
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        try:
            operation(cursor)
        finally:
            cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        if self.cell:
            self.editor.token = object()
        self.editor.setFocus()

    def format(self, delimiter):
        analysis = self._analysis()
        self._edit(lambda cursor: toggle_inline_selection(cursor, delimiter, analysis))

    def format_checked(self, delimiter):
        return selection_has_format(self.editor.textCursor(), delimiter, self._analysis())

    def clear(self):
        analysis = self._analysis()
        self._edit(lambda cursor: clear_selection(cursor, analysis))

    def link(self, external=False):
        self._edit(lambda cursor: link_selection(cursor, external=external))

    def paragraph(self, style):
        self._edit(lambda cursor: paragraph_selection(cursor, style))

    def insert(self, kind):
        self._edit(lambda cursor: insert_block(cursor, kind))

    def copy(self):
        self.editor.copy()

    def cut(self):
        self._edit(lambda cursor: (self.copy(), cursor.removeSelectedText()))

    def paste(self):
        self.paste_plain()

    def paste_plain(self):
        self._edit(lambda cursor: cursor.insertText(QApplication.clipboard().text()))

    def select_all(self):
        self.editor.selectAll()

    def undo(self):
        if self.editable:
            self.session.document.undo()
            self.session.highlighter.refresh()

    def redo(self):
        if self.editable:
            self.session.document.redo()
            self.session.highlighter.refresh()

    def author_lines(self):
        """The source lines an Author command acts on: the selection, else the paragraph (a
        code block's lines, or the run of nonblank lines) at the caret. None in a cell editor."""
        if self.cell or self.session is None:
            return None
        cursor, document = self.editor.textCursor(), self.editor.document()
        if cursor.hasSelection():
            first = document.findBlock(cursor.selectionStart())
            last = document.findBlock(cursor.selectionEnd())
            if last != first and cursor.selectionEnd() == last.position():
                last = last.previous()
            return range(first.blockNumber(), last.blockNumber() + 1)
        block = cursor.block()
        analysis = self._analysis()
        for code in analysis.code_blocks if analysis is not None else ():
            if code.start_line <= block.blockNumber() < code.end_line:
                return range(code.start_line, code.end_line)
        first = last = block
        while block.text().strip() and first.previous().text().strip():
            first = first.previous()
        while block.text().strip() and last.next().text().strip():
            last = last.next()
        return range(first.blockNumber(), last.blockNumber() + 1)

    def insert_table(self):
        from .tables import custom_table_size

        if self.editable and not self.cell:
            dimensions = custom_table_size(self.editor)
            if dimensions is not None:
                columns, rows = dimensions
                self.editor.insert_table(rows, columns)


class GridActions:
    cell = True

    def __init__(self, card):
        self.card = card
        self.session = card.model.session

    @property
    def parent(self):
        return self.card.view

    @property
    def editable(self):
        return self.card.model.alive and not self.session.load_failed

    @property
    def has_selection(self):
        return bool(self.card.selected_cells())

    @property
    def has_text(self):
        return self.has_selection

    def _transform(self, operation):
        if not self.editable:
            return
        def transform(value):
            document = QTextDocument()
            document.setPlainText(cell_editor_text(value))
            cursor = QTextCursor(document)
            cursor.select(QTextCursor.SelectionType.Document)
            operation(cursor)
            return document.toPlainText()
        self.card.model.change_cells(self.card.selected_cells(), transform)
        self.card.view.setFocus()

    def format(self, delimiter):
        self._transform(lambda cursor: toggle_inline_selection(cursor, delimiter))

    def format_checked(self, delimiter):
        parser = MarkdownParser()
        for row, column in self.card.selected_cells():
            document = QTextDocument()
            document.setPlainText(cell_editor_text(self.card.model.rows[row][column]))
            cursor = QTextCursor(document)
            cursor.select(QTextCursor.SelectionType.Document)
            if not selection_has_format(cursor, delimiter, parser.parse(document.toPlainText())):
                return False
        return self.has_selection

    def clear(self):
        self._transform(clear_selection)

    def link(self, external=False):
        if len(self.card.selected_cells()) == 1 and self.editable:
            self.card.focus_cell(*self.card.current_cell(), edit=True)
            editor = self.card.cell_editor
            if editor is not None:
                TextActions(editor, self.session, editor._highlighter, cell=True).link(external)
        else:
            self._transform(lambda cursor: link_selection(cursor, external=external))

    def paragraph(self, style):
        self._transform(lambda cursor: paragraph_selection(cursor, style))

    def insert(self, kind):
        self._transform(lambda cursor: insert_block(cursor, kind))

    def author_lines(self):
        """The source lines of the selected rows (a header row includes its delimiter line)."""
        model = self.card.model
        base = model.session.document.findBlock(model.start).blockNumber()
        rows = {row for row, _column in self.card.selected_cells()}
        return sorted({base + line for row in rows
                       for line in ((0, 1) if row == 0 else (row + 1,))})

    def copy(self):
        self.card.copy_selection()

    def cut(self):
        if self.editable:
            self.card.cut_selection()

    def paste(self):
        if self.editable:
            self.card.paste()

    def paste_plain(self):
        if self.editable:
            self.card.model.set_cell(*self.card.current_cell(), QApplication.clipboard().text())

    def select_all(self):
        self.card.view.selectAll()

    def undo(self):
        if self.editable:
            self.card.undo()

    def redo(self):
        if self.editable:
            self.card.redo()
