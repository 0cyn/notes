from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum, auto
from time import perf_counter
from weakref import WeakSet

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import (
    QFontMetricsF,
    QPalette,
    QTextBlockUserData,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
    QTextLayout,
)
from PySide6.QtWidgets import QApplication, QPlainTextDocumentLayout, QPlainTextEdit, QTextEdit
from shiboken6 import isValid

from .common import BLOCK_INSET, IdleTask, RetainedDrawing, VerticalIndex


class ParagraphHeights:
    def __init__(self, count, estimate):
        self.estimate = estimate
        self._values: list[float | None] = [None] * count
        self._rebuild()

    @property
    def count(self):
        return len(self._values)

    def _rebuild(self):
        # Fenwick tree of (height - estimate) deltas, built in O(n).
        tree = [0.0] * (len(self._values) + 1)
        for index, value in enumerate(self._values, 1):
            if value is not None:
                tree[index] += value - self.estimate
            parent = index + (index & -index)
            if parent < len(tree):
                tree[parent] += tree[index]
        self._tree = tree

    def set_estimate(self, estimate):
        if estimate != self.estimate:
            self.estimate = estimate
            self._rebuild()

    def height(self, index):
        value = self._values[index]
        return self.estimate if value is None else value

    def measured(self, index):
        return self._values[index] is not None

    def set(self, index, height):
        delta = height - self.height(index)
        self._values[index] = height
        if not delta:
            return
        index += 1
        while index < len(self._tree):
            self._tree[index] += delta
            index += index & -index

    def splice(self, first, removed, added):
        """Replace `removed` rows at `first` with `added` rows, keeping the first row's height."""
        kept = self._values[first:first + min(1, removed, added)]
        self._values[first:first + removed] = kept + [None] * (added - len(kept))
        self._rebuild()

    def top(self, index):
        result = index * self.estimate
        while index:
            result += self._tree[index]
            index -= index & -index
        return result

    def at_y(self, y):
        index, height = 0, 0.0
        step = 1 << self.count.bit_length()
        while step:
            candidate = index + step
            if candidate <= self.count:
                next_height = (height + (candidate & -candidate) * self.estimate
                               + self._tree[candidate])
                if next_height <= y:
                    index, height = candidate, next_height
            step >>= 1
        return max(0, min(index, self.count - 1))


CODE_BLOCK_VERTICAL_PADDING = 8.0
CODE_BLOCK_BOTTOM_MARGIN = 8.0
TABLE_MARGIN = 8.0
QUOTE_INDENT = 20.0
QUOTE_VERTICAL_PADDING = 5.0


_RULE = re.compile(r"\s*([-*_])(\s*\1){2,}\s*")


def table_exit_height(document):
    """Room below a table to click and start writing after it."""
    return max(0, math.ceil(QFontMetricsF(document.defaultFont()).lineSpacing()) - TABLE_MARGIN)


class _BlockSpacingState(QTextBlockUserData):
    def __init__(self):
        super().__init__()
        self.base_positions: tuple[QPointF, ...] = ()
        self.applied_signature: tuple = ()
        self.base_bottom = 0.0
        self.quote_key = ()
        self.base_x = 0.0
        self.base_width = 0.0
        # Per line, the (top, bottom) band it occupies, including its spacing.
        self.line_bands: tuple[tuple[float, float], ...] = ()


class MarkdownDocumentLayout(QPlainTextDocumentLayout):
    """Reserves paragraph spacing and quote indentation without touching the source."""

    def __init__(self, document, *, code_spacing=True):
        self._starts: tuple[int, ...] = ()
        self._ends: tuple[int, ...] = ()
        self._code_ranges: tuple[tuple[int, int], ...] = ()
        self._tables: tuple[tuple[int, int, int, int], ...] = ()
        self._inline_previews = ()
        self._preview_starts = ()
        self._table_starts = ()
        self._quotes = ()
        self._quote_starts = ()
        self._quote_padding_starts = ()
        self._quote_padding_ends = ()
        self._code_spacing = code_spacing
        self._analysis = None
        self._rooms: dict = {}  # table identity -> whether blank room is left below it
        super().__init__(document)

    def set_analysis(self, analysis):
        self._analysis = analysis
        if self._refresh_rooms():
            self.document().markContentsDirty(0, self.document().characterCount())
            self.requestUpdate()
        starts = (tuple(code.start_position for code in analysis.code_blocks)
                  if self._code_spacing else ())
        ends = (tuple(code.end_position for code in analysis.code_blocks)
                if self._code_spacing else ())
        quotes = analysis.quote_lines
        if (starts, ends, quotes) == (self._starts, self._ends, self._quotes):
            return
        self._starts, self._ends = starts, ends
        self._code_ranges = tuple(zip(starts, ends, strict=True))
        self._quotes = quotes
        self._quote_starts = tuple(quote.start for quote in quotes)
        rows = {quote.line for quote in quotes}
        self._quote_padding_starts = tuple(
            quote.start for quote in quotes if quote.line - 1 not in rows
        )
        self._quote_padding_ends = tuple(
            quote.end for quote in quotes if quote.line + 1 not in rows
        )
        self.document().markContentsDirty(0, self.document().characterCount())
        self.requestUpdate()

    def set_tables(self, tables):
        tables = tuple(tables)
        if tables == self._tables:
            return
        self._tables = tables
        self._table_starts = tuple(item[1] for item in tables)
        self._refresh_rooms()
        block = self.document().begin()
        index = 0
        while block.isValid():
            start, end = block.position(), block.position() + block.length() - 1
            while index < len(tables) and tables[index][2] < start:
                index += 1
            hidden = False
            if index < len(tables):
                _, table_start, table_end, _ = tables[index]
                hidden = table_start < start and end <= table_end
            block.setVisible(not hidden)
            block.setLineCount(0 if hidden else max(1, block.layout().lineCount()))
            block = block.next()
        self.document().markContentsDirty(0, self.document().characterCount())
        self.requestUpdate()

    def set_inline_previews(self, previews):
        if previews == self._inline_previews:
            return
        self._inline_previews = previews
        self._preview_starts = tuple(item[0] for item in previews)
        self._refresh_rooms()
        self.document().markContentsDirty(0, self.document().characterCount())
        self.requestUpdate()

    def table_room(self, identity):
        """Blank room left below a table to click and write after it (0: none)."""
        return table_exit_height(self.document()) if self._rooms.get(identity, True) else 0

    def _refresh_rooms(self):
        rooms = {identity: self._needs_room(end) for identity, _start, end, _height
                 in self._tables}
        changed, self._rooms = rooms != self._rooms, rooms
        return changed

    def _needs_room(self, table_end):
        """Whether a table needs room below it: not when plain text or a blank line follows."""
        analysis, document = self._analysis, self.document()
        position = table_end + 1
        if analysis is None or position > document.characterCount() - 1:
            return True
        block = document.findBlock(position)
        encoded = block.text().encode('utf-16-le')
        text = encoded[(position - block.position()) * 2:].decode('utf-16-le')
        text = text.split('\u2028', 1)[0]
        row = analysis.line_at_position(position)
        line_end = position + len(text.encode('utf-16-le')) // 2
        if not text.strip():
            return False
        return (row in analysis.code_lines
                or any(item.line == row for item in analysis.headings)
                or any(item.line == row for item in analysis.quote_lines)
                or any(item.start_line == row for item in analysis.tables)
                or bool(_RULE.fullmatch(text)) or text.lstrip().startswith('$$')
                or any(position <= item[0] <= line_end for item in self._inline_previews))

    def decoration_key(self, start, end):
        """Only paragraph-local dependencies invalidate its retained text layout."""
        boundaries = tuple(tuple(pos - start for pos in values[
            bisect_left(values, start):bisect_right(values, end)
        ]) for values in (self._starts, self._ends, self._quote_padding_starts,
                          self._quote_padding_ends))
        previews = tuple((pos - start, height, baseline) for pos, height, baseline
                         in self._inline_previews[bisect_left(self._preview_starts, start):
                                                  bisect_right(self._preview_starts, end)])
        quotes = self._quotes[bisect_left(self._quote_starts, start):
                              bisect_right(self._quote_starts, end)]
        tables = tuple((identity, left - start, right - start, height,
                        self._rooms.get(identity, True))
                       for identity, left, right, height in self._tables[
                           max(0, bisect_right(self._table_starts, start) - 1):
                           bisect_right(self._table_starts, end)] if right >= start)
        return boundaries, previews, quotes, tables

    @staticmethod
    def _signature(lines) -> tuple:
        return tuple(
            (line.textStart(), line.textLength(), line.x(), line.y(), line.width(), line.height())
            for line in lines
        )

    def _code_inset(self, start):
        """Fenced code is inset in its frame, which sits on the text column."""
        index = bisect_right(self._starts, start) - 1
        if index >= 0 and start <= self._code_ranges[index][1]:
            return BLOCK_INSET
        return 0.0

    def _reflow_quotes(self, block, state, quotes, inset=0.0):
        """Re-wrap with hanging quote indentation and any code inset on both sides."""
        layout = block.layout()
        offset = block.position()
        metrics = {}
        for quote in quotes:
            first = layout.lineForTextPosition(quote.start - offset)
            if not first.isValid():
                continue
            removed = 0.0
            for left, right in quote.markers:
                line = layout.lineForTextPosition(left - offset)
                if not line.isValid():  # A range from text since changed: Qt would crash.
                    continue
                removed += line.cursorToX(right - offset)[0] - line.cursorToX(left - offset)[0]
            prefix = first.cursorToX(quote.content_start - offset)[0] - first.cursorToX(
                quote.start - offset
            )[0]
            indent = min(quote.depth * QUOTE_INDENT, max(0, state.base_width - 32))
            metrics[quote.start] = indent - removed, indent + prefix - removed
        starts = tuple(quote.start for quote in quotes)
        layout.beginLayout()
        height, count = 0.0, 0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            position = offset + line.textStart()
            index = bisect_right(starts, position) - 1
            quote = quotes[index] if index >= 0 and position <= quotes[index].end else None
            shift = 0.0
            if quote is not None and quote.start in metrics:
                shift = metrics[quote.start][0 if position == quote.start else 1]
            line.setLineWidth(max(1, state.base_width - shift - inset * 2))
            line.setPosition(QPointF(state.base_x + shift + inset, height))
            height += line.height()
            count += 1
        layout.endLayout()
        block.setLineCount(count)

    def blockBoundingRect(self, block):
        if not block.isValid() or not block.isVisible():
            return QRectF()
        return self.decorate_block(block, QRectF(super().blockBoundingRect(block)))

    def decorate_block(self, block, rectangle):
        """Apply source-preserving geometry to a freshly measured paragraph."""
        layout = block.layout()
        lines = [layout.lineAt(index) for index in range(layout.lineCount())]
        if not lines:
            return rectangle

        state = block.userData()
        if not isinstance(state, _BlockSpacingState):
            state = _BlockSpacingState()
            block.setUserData(state)
        signature = self._signature(lines)
        start, end = block.position(), block.position() + block.length() - 1
        left, right = bisect_left(self._quote_starts, start), bisect_right(self._quote_starts, end)
        quotes = tuple(quote for quote in self._quotes[left:right] if not any(
            table_start <= quote.start <= table_end for _, table_start, table_end, _ in self._tables
        ))
        inset = self._code_inset(start)
        quote_key = quotes, inset, block.revision()
        if signature == state.applied_signature:
            # Remove previously applied shifts before recomputing, or padding accumulates.
            old_shift = layout.boundingRect().bottom() - state.base_bottom
            rectangle.setHeight(rectangle.height() - old_shift)
        else:
            # Qt rebuilt the lines (typing, rehighlighting, resizing, etc.).
            state.base_x, state.base_width = lines[0].x(), lines[0].width()
            state.base_positions = tuple(line.position() for line in lines)
            state.base_bottom = layout.boundingRect().bottom()
        if (quotes or inset or state.quote_key and any(state.quote_key[:2])) and (
            signature != state.applied_signature or quote_key != state.quote_key
        ):
            old_height = layout.boundingRect().height()
            self._reflow_quotes(block, state, quotes, inset)
            rectangle.setHeight(rectangle.height() + layout.boundingRect().height() - old_height)
            lines = [layout.lineAt(index) for index in range(layout.lineCount())]
            state.base_positions = tuple(line.position() for line in lines)
            state.base_bottom = layout.boundingRect().bottom()
        state.quote_key = quote_key

        before = [0.0] * len(lines)
        after = [0.0] * len(lines)
        start = block.position()
        end = start + block.length() - 1
        for boundaries, amounts, spacing in (
            (self._starts, before, CODE_BLOCK_VERTICAL_PADDING),
            (self._ends, after, CODE_BLOCK_VERTICAL_PADDING + CODE_BLOCK_BOTTOM_MARGIN),
            (self._quote_padding_starts, before, QUOTE_VERTICAL_PADDING),
            (self._quote_padding_ends, after, QUOTE_VERTICAL_PADDING),
        ):
            left, right = bisect_left(boundaries, start), bisect_right(boundaries, end)
            for position in boundaries[left:right]:
                line = layout.lineForTextPosition(position - start)
                index = line.lineNumber() if line.isValid() else len(lines) - 1
                amounts[index] += spacing

        # Reserve space around tall inline objects instead of enlarging the line's font.
        preview_before, preview_after = {}, {}
        for position, height, baseline in self._inline_previews[
            bisect_left(self._preview_starts, start):bisect_right(self._preview_starts, end)
        ]:
            line = layout.lineForTextPosition(position - start)
            if not line.isValid():
                continue
            index = line.lineNumber()
            preview_before[index] = max(preview_before.get(index, 0), baseline - line.ascent())
            preview_after[index] = max(preview_after.get(index, 0),
                                       height - baseline - (line.height() - line.ascent()))
        for index, amount in preview_before.items():
            before[index] += amount
        for index, amount in preview_after.items():
            after[index] += amount

        shift = 0.0
        anchors = {}
        bands = []
        for index, line in enumerate(lines):
            position = start + line.textStart()
            table_index = bisect_right(self._table_starts, position) - 1
            table = self._tables[table_index] if table_index >= 0 else None
            if table is not None and position > table[2]:
                table = None
            if table is not None:
                identity, table_start, _table_end, height = table
                origin = state.base_positions[index]
                first_line = (
                    layout.lineForTextPosition(table_start - start)
                    if table_start >= start else None
                )
                if (first_line is not None and first_line.isValid()
                        and first_line.lineNumber() == index):
                    y = origin.y() + shift + TABLE_MARGIN
                    anchors[identity] = y
                    shift += (height + TABLE_MARGIN * 2 + self.table_room(identity)
                              - line.height())
                else:
                    y = anchors.get(identity, -height - TABLE_MARGIN)
                    shift -= line.height()
                # The source stays in the document, covered by the table widget and folded.
                line.setPosition(QPointF(origin.x(), y))
                bands.append((y, y + line.height()))
                continue
            origin = state.base_positions[index]
            top = origin.y() + shift
            shift += before[index]
            line.setPosition(QPointF(origin.x(), origin.y() + shift))
            shift += after[index]
            bands.append((top, origin.y() + shift + line.height()))
        rectangle.setHeight(rectangle.height() + shift)
        state.applied_signature = self._signature(lines)
        state.line_bands = tuple(bands)
        return rectangle

    @staticmethod
    def line_band(block, position):
        """Block-relative (top, bottom) of the visual line at `position`, with reserved space."""
        line = block.layout().lineForTextPosition(position - block.position())
        if not line.isValid():
            return 0.0, 0.0
        bands = getattr(block.userData(), 'line_bands', ())
        if len(bands) == block.layout().lineCount():
            return bands[line.lineNumber()]
        return line.y(), line.y() + line.height()


# Re-measuring can move an anchor and expose more paragraphs; bound that loop.
_SETTLE_PASSES = 4


def _retained(block):
    """(key, rectangle, drawing | None, line tops) shaped for this block, if any."""
    return getattr(block.userData(), 'retained', None)


class PixelDocumentLayout(MarkdownDocumentLayout):
    geometryChanged = Signal()

    def __init__(self, document):
        self.generation = 0
        self.progressive = False
        self.foreground = QApplication.palette().color(QPalette.ColorRole.Text)
        self._size = QSizeF()
        self._publishing = False
        self._republish = False
        self._views = WeakSet()       # visible views; their viewports are kept measured
        self._view_tracking = False   # becomes True once any view reports visibility
        self._dirty = True            # document or page changed since the last shaping pass
        self._page_width = None
        # Full mode: the whole document is shaped at once.
        self._rects = {}
        self._blocks = []
        self._tops = []
        # Progressive mode: paragraphs are shaped on demand, indexed by block number.
        self._heights = None
        self._shape_key = None
        self._pending_blocks = set()  # need (re)shaping; keep their published height
        self._pending_records = set()  # shaped without a retained drawing
        self._next_block = 0
        self._maximum_width = 1.0
        super().__init__(document)
        self._flush_timer = IdleTask(self)
        self._flush_timer.timeout.connect(self._flush)

    # ---- invalidation -------------------------------------------------------------

    def documentChanged(self, position, removed, added):
        self._dirty = True
        if self.progressive and self._heights is not None:
            self._track_change(position, added)
            self.settle()
        self._schedule_flush()

    def _track_change(self, position, added):
        """Mark edited paragraphs pending; splice heights when blocks come or go."""
        document = self.document()
        first = document.findBlock(position).blockNumber()
        last = document.findBlock(min(document.characterCount() - 1, position + added))
        if first < 0:
            return
        last = max(first, last.blockNumber())
        delta = document.blockCount() - self._heights.count
        if delta:
            new_rows = last - first + 1
            old_rows = new_rows - delta
            self._heights.splice(first, old_rows, new_rows)
            self._pending_blocks = self._renumber(self._pending_blocks, first, old_rows, delta)
            self._pending_records = self._renumber(self._pending_records, first, old_rows, delta)
            self._next_block = min(self._next_block, first)
        self._mark_pending(range(first, last + 1))

    @staticmethod
    def _renumber(numbers, first, removed, delta):
        return {number if number < first else number + delta
                for number in numbers if not first <= number < first + removed}

    def _mark_pending(self, numbers):
        numbers = set(numbers)
        self._pending_blocks |= numbers
        self._pending_records -= numbers
        if numbers:
            self._next_block = min(self._next_block, min(numbers))

    def _schedule_flush(self):
        if hasattr(self, '_flush_timer') and (not self._view_tracking or self._views):
            self._flush_timer.start(0)

    def set_inline_previews(self, previews):
        super().set_inline_previews(previews)
        self._seed(position for position, _height, _baseline in previews)

    def set_tables(self, tables):
        super().set_tables(tables)
        self._seed(start for _identity, start, _end, _height in self._tables)

    def _seed(self, positions, *, publish=True):
        """Give never-shaped embed/table paragraphs their known object height."""
        if not self.progressive or self._heights is None:
            return
        document = self.document()
        seeded = False
        for position in positions:
            number = document.findBlock(position).blockNumber()
            if number >= 0 and not self._heights.measured(number):
                self._heights.set(number, self._object_height(number))
                seeded = True
        if seeded and publish:
            self._publish()

    def _object_height(self, number):
        block = self.document().findBlockByNumber(number)
        start, end = block.position(), block.position() + block.length() - 1
        height = self._heights.estimate
        for position, preview_height, _baseline in self._inline_previews:
            if start <= position <= end:
                height = max(height, preview_height)
        for _identity, table_start, _table_end, table_height in self._tables:
            if start <= table_start <= end:
                height = max(height, table_height + 16)
        return height

    # ---- views --------------------------------------------------------------------

    @property
    def publishing(self):
        return self._publishing

    def set_view_active(self, view, active):
        self._view_tracking = True
        if active:
            self._views.add(view)
            self.settle()
            if self._dirty or self._pending_blocks or self._pending_records:
                self._flush_timer.start()
        else:
            self._views.discard(view)
            if not self._views and self.progressive:
                self._flush_timer.stop()

    def settle(self):
        """Shape every pending paragraph a visible view shows, then publish once."""
        if not self.progressive or self._publishing:
            return
        self._ensure_progressive()
        shaped = False
        for _attempt in range(_SETTLE_PASSES):
            numbers = self._visible_pending()
            if not numbers:
                break
            for number in sorted(numbers):
                self._shape(self.document().findBlockByNumber(number), record=False)
            shaped = True
        if shaped:
            self._publish()

    def _visible_pending(self):
        if not self._pending_blocks:
            return set()
        margin = self.document().documentMargin()
        result = set()
        for view in list(self._views):
            top, height = view.anchored_viewport()
            first = self._heights.at_y(max(0.0, top - margin))
            last = self._heights.at_y(max(0.0, top + height - margin))
            result.update(number for number in range(first, last + 1)
                          if number in self._pending_blocks)
        return result

    def _publish(self):
        # Shaping triggered by re-anchoring is published in a follow-up pass.
        for _attempt in range(_SETTLE_PASSES):
            self._republish = False
            if self.progressive:
                self._size = QSizeF(self._maximum_width, self.document_height())
            self.generation += 1
            self._publishing = True
            try:
                self.documentSizeChanged.emit(self._size)
                self.geometryChanged.emit()
            finally:
                self._publishing = False
            if not self._republish:
                break
        self.requestUpdate()

    def document_height(self):
        if self.progressive and self._heights is not None:
            return self._heights.top(self._heights.count) + self.document().documentMargin() * 2
        return self._size.height()

    # ---- idle shaping -------------------------------------------------------------

    def _flush(self):
        if self._view_tracking and not self._views:
            return
        if not self.progressive:
            self._ensure_layout()
            self.requestUpdate()
            return
        self._ensure_progressive()
        self.settle()
        deadline = perf_counter() + .002
        document = self.document()
        shaped = False
        while self._pending_records or self._next_block < document.blockCount():
            if self._pending_records:
                number = self._pending_records.pop()
            else:
                number = self._next_block
                self._next_block += 1
                if number not in self._pending_blocks:
                    continue
            self._shape(document.findBlockByNumber(number), record=True)
            shaped = True
            if perf_counter() >= deadline:
                break
        if shaped:
            self._publish()
        if self._pending_blocks or self._pending_records:
            self._flush_timer.start()

    # ---- progressive shaping ------------------------------------------------------

    def _ensure_progressive(self):
        if self._heights is not None and not self._dirty:
            return
        document = self.document()
        width, margin = document.pageSize().width(), document.documentMargin()
        font, option = document.defaultFont(), document.defaultTextOption()
        common = (width, margin, font.toString(), option.wrapMode(), option.alignment(),
                  option.textDirection(), option.tabStopDistance(), option.flags())
        key = common, self.foreground.rgba()
        estimate = QFontMetricsF(font).lineSpacing()
        if self._heights is None:
            self._heights = ParagraphHeights(document.blockCount(), estimate)
            self._mark_pending(range(document.blockCount()))
            self._seed([*(item[0] for item in self._inline_previews),
                        *(item[1] for item in self._tables)], publish=False)
        elif key != self._shape_key:
            # Re-shape everything; published heights stay until remeasured.
            self._heights.set_estimate(estimate)
            self._mark_pending(range(document.blockCount()))
        if key != self._shape_key:
            self._shape_key = key
            self._shape_common, self._shape_font, self._shape_option = common, font, option
            self._maximum_width = max(1, width)
        self._dirty = False
        self._page_width = width
        self._schedule_flush()

    def _in_step(self):
        """Whether the heights index matches the document. Qt asks for geometry mid-edit,
        before reporting the edit in documentChanged; until then rows may be missing or shifted."""
        return self._heights.count == self.document().blockCount()

    def _progressive_rect(self, block):
        if not block.isValid() or not block.isVisible() or not self._in_step():
            return QRectF()
        number = block.blockNumber()
        return QRectF(0, self.document().documentMargin() + self._heights.top(number),
                      self._maximum_width, self._heights.height(number))

    def _shape(self, block, *, record):
        """Measure one pending paragraph. Callers publish the result."""
        if not block.isValid():
            return
        number = block.blockNumber()
        self._pending_blocks.discard(number)
        if not block.isVisible():
            self._heights.set(number, 0)
            return
        width = self._page_width
        available = max(1, width - self.document().documentMargin() * 2) if width > 0 else 1_000_000
        saved = self._paragraph(block, available, width, self._shape_font,
                                self._shape_option, self._shape_common, record=record)
        self._heights.set(number, saved[1].height())
        self._maximum_width = max(self._maximum_width, saved[1].width())
        block.layout().setPosition(self._progressive_rect(block).topLeft())
        if saved[2] is None:
            self._pending_records.add(number)

    def _shape_now(self, block):
        """A geometry query needs this paragraph's real lines (cursor, hit test)."""
        if block.blockNumber() not in self._pending_blocks and _retained(block) is not None:
            return
        before = self._heights.height(block.blockNumber())
        self._shape(block, record=False)
        if self._heights.height(block.blockNumber()) != before:
            if self._publishing:
                self._republish = True
            else:
                self._publish()

    # ---- shared paragraph shaping -------------------------------------------------

    def _paragraph(self, block, available, width, font, option, common, *, record=True):
        layout = block.layout()
        start, end = block.position(), block.position() + block.length() - 1
        decoration = self.decoration_key(start, end)
        formats = tuple((item.start, item.length, QTextCharFormat(item.format))
                        for item in layout.formats())
        key = common, block.text(), formats, decoration, self.foreground.rgba()
        state = block.userData()
        if not isinstance(state, _BlockSpacingState):
            state = _BlockSpacingState()
            block.setUserData(state)
        saved = getattr(state, 'retained', None)
        if saved is not None and saved[0] == key and layout.lineCount():
            if record and saved[2] is None:
                saved = (key, saved[1], self._record_paragraph(layout), saved[3])
                state.retained = saved
            return saved
        layout.clearLayout()
        layout.setCacheEnabled(True)
        layout.setFont(font)
        layout.setTextOption(option)
        layout.beginLayout()
        height, natural, count = 0.0, 0.0, 0
        margin = self.document().documentMargin()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLeadingIncluded(True)
            line.setLineWidth(available)
            line.setPosition(QPointF(margin, height))
            height += line.height()
            natural = max(natural, line.naturalTextWidth() + margin * 2)
            count += 1
        layout.endLayout()
        block.setLineCount(count)
        # Reset spacing detection: these are fresh, unshifted source lines.
        state.applied_signature = ()
        rectangle = self.decorate_block(block, QRectF(0, 0, max(width, natural), height))
        lines = [layout.lineAt(index) for index in range(layout.lineCount())]
        layout.setPosition(QPointF())

        drawing = self._record_paragraph(layout) if record else None
        saved = key, rectangle, drawing, tuple(line.y() for line in lines)
        state.retained = saved
        return saved

    def _record_paragraph(self, layout):
        origin = layout.position()
        layout.setPosition(QPointF())

        def draw(painter, clip):
            painter.setPen(self.foreground)
            layout.draw(painter, QPointF(), [], clip)

        drawing = RetainedDrawing([layout.lineAt(index).naturalTextRect().adjusted(-2, -1, 2, 1)
                                   for index in range(layout.lineCount())
                                   if layout.lineAt(index).naturalTextWidth() > 0], draw)
        layout.setPosition(origin)
        return drawing

    # ---- full mode ----------------------------------------------------------------

    def _ensure_layout(self):
        if self.progressive:
            self._ensure_progressive()
            return
        document = self.document()
        width = document.pageSize().width()
        if self._publishing or not self._dirty and width == self._page_width:
            return
        self._dirty = False
        self._page_width = width
        margin = document.documentMargin()
        available = max(1, width - margin * 2) if width > 0 else 1_000_000
        y, maximum = margin, max(0, width)
        rectangles, blocks, tops = {}, [], []
        font, option = document.defaultFont(), document.defaultTextOption()
        common = (width, margin, font.toString(), option.wrapMode(), option.alignment(),
                  option.textDirection(), option.tabStopDistance(), option.flags())
        block = document.begin()
        while block.isValid():
            layout = block.layout()
            if not block.isVisible():
                block.setLineCount(0)
                block = block.next()
                continue
            saved = self._paragraph(block, available, width, font, option, common)
            rectangle = QRectF(saved[1])
            rectangle.moveTop(y)
            layout.setPosition(QPointF(0, y))
            rectangles[block.position()] = rectangle
            blocks.append(block.position())
            tops.append(y)
            y += rectangle.height()
            maximum = max(maximum, rectangle.width())
            block = block.next()
        self._rects, self._blocks, self._tops = rectangles, blocks, tops
        self._size = QSizeF(maximum, y + margin)
        self._publish()

    # ---- geometry queries ---------------------------------------------------------

    def documentSize(self):
        self._ensure_layout()
        if self.progressive:
            return QSizeF(self._maximum_width, self.document_height())
        return QSizeF(self._size)

    def frameBoundingRect(self, _frame):
        return QRectF(QPointF(), self.documentSize())

    def presentation_rect(self, block):
        """Published geometry; decorations and anchors never force text shaping."""
        self._ensure_layout()
        if self.progressive:
            return self._progressive_rect(block)
        return QRectF(self._rects.get(block.position(), QRectF()))

    def line_edges(self, position):
        """Document (top, bottom) of the visual line at `position`."""
        block = self.document().findBlock(position)
        top = self.blockBoundingRect(block).top()
        band_top, band_bottom = self.line_band(block, position)
        return top + band_top, top + band_bottom

    def blockBoundingRect(self, block):
        self._ensure_layout()
        if self.progressive:
            if block.isValid() and self._in_step():
                self._shape_now(block)
            return self._progressive_rect(block)
        return QRectF(self._rects.get(block.position(), QRectF()))

    def block_at_y(self, y):
        self._ensure_layout()
        if self.progressive:
            number = self._heights.at_y(max(0, y - self.document().documentMargin()))
            return self.document().findBlockByNumber(number)
        if not self._blocks:
            return self.document().begin()
        return self.document().findBlock(self._blocks[max(0, bisect_right(self._tops, y) - 1)])

    def hitTest(self, point, accuracy):
        block = self.block_at_y(point.y())
        if self.progressive and block.isValid():
            self._shape_now(block)
        saved = _retained(block)
        if not block.isValid() or not block.layout().lineCount() or saved is None:
            return 0
        y = point.y() - self.presentation_rect(block).top()
        index = max(0, bisect_right(saved[3], y) - 1)
        line = block.layout().lineAt(index)
        if accuracy == Qt.HitTestAccuracy.ExactHit and not line.rect().contains(
            QPointF(point.x(), y),
        ):
            return -1
        return block.position() + line.xToCursor(point.x())

    # ---- painting -----------------------------------------------------------------

    def draw(self, painter, context):
        self._ensure_layout()
        # Index selections once per frame, not per visible paragraph.
        selection_index = VerticalIndex(
            (item.cursor.selectionStart(), item.cursor.selectionEnd(),
             (item.cursor.selectionStart(), item.cursor.selectionEnd(),
              QTextCharFormat(item.format)))
            for item in context.selections
        )
        if self.progressive:
            first = self.block_at_y(context.clip.top()).blockNumber()
            last = self.block_at_y(context.clip.bottom()).blockNumber()
            positions = (self.document().findBlockByNumber(number).position()
                         for number in range(first, last + 1))
        else:
            first = max(0, bisect_right(self._tops, context.clip.top()) - 1)
            positions = (self._blocks[index] for index in range(first, len(self._blocks)))
        for position in positions:
            block = self.document().findBlock(position)
            if not block.isVisible():
                continue
            rectangle = self.presentation_rect(block)
            block.layout().setPosition(rectangle.topLeft())
            if context.clip.isValid() and rectangle.top() > context.clip.bottom():
                break
            if context.clip.isValid() and rectangle.bottom() < context.clip.top():
                continue
            saved = _retained(block)
            if saved is None:
                continue
            # A pending paragraph's retained drawing may show superseded text.
            live = self.progressive and block.blockNumber() in self._pending_blocks
            selections = []
            length = block.length()
            for left, right, fmt in selection_index.between(position, position + length - 1):
                start, end = left - position, right - position
                item = QTextLayout.FormatRange()
                item.start = max(0, start)
                item.length = min(length, end) - item.start
                item.format = fmt
                if left == right and item.format.boolProperty(
                    QTextFormat.Property.FullWidthSelection,
                ):
                    line = block.layout().lineForTextPosition(item.start)
                    if line.isValid():
                        item.start, item.length = line.textStart(), line.textLength()
                selections.append(item)
            painter.save()
            painter.setPen(context.palette.color(QPalette.ColorRole.Text))
            # QTextLayout already includes its document position.
            if (selections or live or saved[2] is None
                    or context.palette.color(QPalette.ColorRole.Text) != self.foreground):
                block.layout().draw(painter, QPointF(), selections, context.clip)
            else:
                visible = context.clip if context.clip.isValid() else rectangle
                saved[2].draw(painter, rectangle.topLeft(), visible)
            position = context.cursorPosition - block.position()
            if 0 <= position < block.length():
                block.layout().drawCursor(painter, QPointF(), position, self.cursorWidth())
            painter.restore()


class AnchorMode(Enum):
    READING = auto()
    TYPING = auto()
    PINNED = auto()


@dataclass
class ViewAnchor:
    cursor: QTextCursor  # follows edits to its document
    y: float             # viewport y of the anchored block top (or line top)
    line: bool = False   # anchor the cursor's visual line rather than its block
    bottom: bool = False  # with `line`: `y` is that line's bottom edge, not its top


_READING_EVENTS = (QEvent.Type.Wheel, QEvent.Type.MouseButtonPress,
                   QEvent.Type.MouseButtonDblClick)


class PixelTextEdit(QTextEdit):
    updateRequest = Signal(QRect, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self._anchor = None
        self._anchor_mode = AnchorMode.READING
        self._reprojecting = False
        self._capturing = False
        self._watched_bar = None

    # ---- geometry helpers ---------------------------------------------------------

    def _pixel_layout(self):
        layout = self.document().documentLayout()
        return layout if isinstance(layout, PixelDocumentLayout) else None

    def contentOffset(self):
        return QPointF(-self.horizontalScrollBar().value(), -self.verticalScrollBar().value())

    def blockBoundingGeometry(self, block):
        layout = self.document().documentLayout()
        return (layout.presentation_rect(block) if hasattr(layout, 'presentation_rect')
                else layout.blockBoundingRect(block))

    def blockBoundingRect(self, block):
        return self.blockBoundingGeometry(block)

    def firstVisibleBlock(self):
        layout = self.document().documentLayout()
        return (layout.block_at_y(self.verticalScrollBar().value())
                if hasattr(layout, 'block_at_y') else self.document().begin())

    def centerCursor(self):
        self.ensureCursorVisible()
        bar = self.verticalScrollBar()
        bar.setValue(round(bar.value() + self.cursorRect().center().y()
                            - self.viewport().height() / 2))

    # ---- document and scroll bar binding ------------------------------------------

    def setDocument(self, document):
        previous = self._pixel_layout()
        if previous is not None:
            previous.geometryChanged.disconnect(self._reproject)
        self._anchor = None
        self._anchor_mode = AnchorMode.READING
        super().setDocument(document)
        layout = self._pixel_layout()
        if layout is not None:
            layout.geometryChanged.connect(self._reproject)

    def setVerticalScrollBar(self, bar):
        super().setVerticalScrollBar(bar)
        self._watch_bar(bar)

    def _watch_bar(self, bar):
        if self._watched_bar is bar:
            return
        if self._watched_bar is not None and isValid(self._watched_bar):
            self._watched_bar.actionTriggered.disconnect(self.start_reading)
            self._watched_bar.sliderPressed.disconnect(self.start_reading)
        self._watched_bar = bar
        bar.actionTriggered.connect(self.start_reading)
        bar.sliderPressed.connect(self.start_reading)

    # ---- anchor state machine -----------------------------------------------------

    def pin(self, position, viewport_y, *, bottom=False):
        """Keep `position`'s line at `viewport_y` until the next user input."""
        cursor = QTextCursor(self.document())
        cursor.setPosition(position)
        self._anchor = ViewAnchor(cursor, float(viewport_y), line=True, bottom=bottom)
        self._anchor_mode = AnchorMode.PINNED

    def start_reading(self, *_args):
        """Wheel, mouse or scroll bar input: hold the top line, wherever the caret is."""
        mode, self._anchor_mode = self._anchor_mode, AnchorMode.READING
        if mode is AnchorMode.PINNED:
            self._capture()

    def _start_typing(self):
        if self._anchor_mode is AnchorMode.PINNED:
            self._anchor_mode = AnchorMode.READING
            self._capture()
        self._anchor_mode = AnchorMode.TYPING

    def _capture(self):
        """Anchor the line now at the top of the viewport (unless pinned)."""
        layout = self._pixel_layout()
        if layout is None or self._anchor_mode is AnchorMode.PINNED or self._capturing:
            return
        self._capturing = True
        try:
            # The reader's scroll position, not a stale anchor, defines the new anchor.
            layout.documentSize()
            self._anchor = self._top_anchor(layout)
        finally:
            self._capturing = False

    def _top_anchor(self, layout):
        value = self.verticalScrollBar().value()
        block = layout.block_at_y(value)
        rectangle = layout.blockBoundingRect(block)  # shapes a lazily built block
        lines = block.layout()
        if rectangle.top() < value and lines.lineCount() > 1:
            # A tall paragraph straddles the top edge: hold its line at the edge.
            for index in range(lines.lineCount()):
                position = block.position() + lines.lineAt(index).textStart()
                top, bottom = layout.line_edges(position)
                if bottom > value or index == lines.lineCount() - 1:
                    cursor = QTextCursor(self.document())
                    cursor.setPosition(position)
                    return ViewAnchor(cursor, top - value, line=True)
        return ViewAnchor(QTextCursor(block), rectangle.top() - value)

    def _anchor_document_y(self):
        layout = self._pixel_layout()
        anchor = self._anchor
        if layout is None or anchor is None or anchor.cursor.document() is not self.document():
            return None
        if anchor.line:
            top, bottom = layout.line_edges(anchor.cursor.position())
            return bottom if anchor.bottom else top
        return layout.presentation_rect(anchor.cursor.block()).top()

    def anchored_viewport(self):
        """Document (top, height) this view shows once its anchor is re-projected."""
        height = self.viewport().height()
        y = self._anchor_document_y()
        if y is None:
            return float(self.verticalScrollBar().value()), height
        layout = self._pixel_layout()
        top = y - self._anchor.y
        if self._anchor_mode is AnchorMode.TYPING:
            top = self._showing_caret(layout, top, height)
        limit = max(0.0, layout.document_height() - height)
        return min(max(0.0, top), limit), height

    def _showing_caret(self, layout, top, height):
        """The nearest viewport top to `top` that shows the caret's line."""
        cursor = self.textCursor()
        block = cursor.block()
        rectangle = layout.presentation_rect(block)  # never forces shaping
        line = block.layout().lineForTextPosition(cursor.position() - block.position())
        if line.isValid():
            caret_top = rectangle.top() + line.y()
            caret_bottom = caret_top + line.height()
        else:
            caret_top, caret_bottom = rectangle.top(), rectangle.bottom()
        if caret_bottom > top + height:
            top = caret_bottom - height
        return min(top, caret_top)

    def _reproject(self):
        if self._capturing:
            return
        if self._anchor_document_y() is None:
            self._capture()
            return
        layout = self._pixel_layout()
        top, height = self.anchored_viewport()
        held = self._anchor_document_y() - self._anchor.y
        caret_moved_view = (self._anchor_mode is AnchorMode.TYPING
                            and abs(self._showing_caret(layout, held, height) - held) >= 1)
        self._reprojecting = True
        try:
            self.verticalScrollBar().setValue(round(top))
        finally:
            self._reprojecting = False
        if caret_moved_view:
            # Showing the caret moved the view: that is the reader's new position.
            self._anchor = self._top_anchor(layout)

    # ---- input and scrolling ------------------------------------------------------

    def event(self, event):
        if event.type() == QEvent.Type.KeyPress:
            self._start_typing()
        return super().event(event)

    def viewportEvent(self, event):
        if event.type() in _READING_EVENTS:
            self.start_reading()
        return super().viewportEvent(event)

    def scrollContentsBy(self, dx, dy):
        layout = self._pixel_layout()
        if dy and not self._reprojecting and (layout is None or not layout.publishing):
            self._capture()  # The reader (or caret navigation) chose a new position.
        if layout is not None:
            layout.settle()
        super().scrollContentsBy(dx, dy)
        # The label is pinned to the viewport; repaint its old and new positions.
        self.viewport().update()
        self.updateRequest.emit(self.viewport().rect(), dy)

    def setLineWrapMode(self, mode):
        super().setLineWrapMode(QTextEdit.LineWrapMode(mode.value))

    def lineWrapMode(self):
        # Keep the two existing plain-text wrap choices usable by callers.
        return QPlainTextEdit.LineWrapMode(super().lineWrapMode().value)
