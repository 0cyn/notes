from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import count
from time import perf_counter
from weakref import WeakValueDictionary

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QAbstractTableModel,
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QContextMenuEvent,
    QCursor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QPlainTextEdit,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from ..markdown import (
    cell_editor_text,
    clipboard_matrix,
    encode_cell,
    serialize_table,
    table_region,
    utf16_length,
)
from ..preparation import cell_html
from .common import (
    BLOCK_INSET,
    DOCUMENT_MARGIN,
    IdleTask,
    NoteContextMenu,
    RetainedDrawing,
    build_edit_menu,
    mix,
    opacity,
    single_shot,
    watch_focus,
)
from .embeds import (
    CellCodeObjects,
    FollowHint,
    LinkPresentation,
    NavigationState,
    add_embed_actions,
    context_target,
    leave_link_source,
    preview_theme,
    source_link_at,
    visible_rect,
)
from .formatting import (
    GridActions,
    QuotePresentation,
    TextActions,
    continue_quote,
    toggle_inline_selection,
    toggle_inline_text,
)
from .highlighter import MarkdownHighlighter
from .layout import (
    QUOTE_INDENT,
    QUOTE_VERTICAL_PADDING,
    TABLE_MARGIN,
    MarkdownDocumentLayout,
    table_exit_height,
)

CELL_PADDING_X = BLOCK_INSET
CELL_PADDING_Y = 5
ROW_HEIGHT = 32
HEADER_HEIGHT = 36
TOP_RAIL = 24
SIDE_RAIL = 24
# The grid sits on the note's text column: its row handles hang in the margin.
LEFT_RAIL = DOCUMENT_MARGIN
ROW_HANDLE_WIDTH = 18
BOTTOM_RAIL = 28
TABLE_CHROME_HEIGHT = TOP_RAIL + BOTTOM_RAIL


class TableTheme:
    def __init__(self, palette: QPalette):
        self.base = palette.color(QPalette.ColorRole.Base)
        self.text = palette.color(QPalette.ColorRole.Text)
        self.dark = self.base.lightness() < 128
        self.surface = mix(self.base, self.text, 0.018 if self.dark else 0.005)
        self.header = mix(self.base, self.text, 0.055 if self.dark else 0.035)
        self.hover = mix(self.surface, self.text, 0.028)
        self.rule = mix(self.surface, self.text, 0.095)
        self.border = mix(self.surface, self.text, 0.17)
        self.muted = mix(self.surface, self.text, 0.54)
        self.accent = palette.color(QPalette.ColorRole.Highlight)
        if self.dark and self.accent.lightness() < 145:
            self.accent = self.accent.lighter(140)
        self.selection = mix(self.surface, self.accent, 0.16 if self.dark else 0.1)
        self.action_fill = mix(self.surface, self.accent, 0.14)
        self.code = mix(self.surface, self.text, 0.045)
        self.content_palette = QPalette(palette)
        self.content_palette.setColor(QPalette.ColorRole.Base, self.surface)
        self.content_palette.setColor(QPalette.ColorRole.Text, self.text)
        self.content_palette.setColor(QPalette.ColorRole.AlternateBase, self.code)
        self.content_palette.setColor(QPalette.ColorRole.Link, self.accent)


def small_font(font: QFont, *, size=10, bold=False) -> QFont:
    result = QFont(font)
    result.setPixelSize(size)
    result.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    return result


def draw_plus(painter, center, color, *, size=8, width=1.5):
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    half = size / 2
    painter.drawLine(QPointF(center.x() - half, center.y()),
                     QPointF(center.x() + half, center.y()))
    painter.drawLine(QPointF(center.x(), center.y() - half),
                     QPointF(center.x(), center.y() + half))


def draw_dots(painter, center, color, *, vertical=False):
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for offset in (-3, 0, 3):
        point = QPointF(center.x(), center.y() + offset) if vertical else QPointF(
            center.x() + offset, center.y()
        )
        painter.drawEllipse(point, 1.05, 1.05)


def draw_action(painter, rect, theme, *, hot=False, plus=False, vertical=False):
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.action_fill if hot else mix(theme.base, theme.text, 0.035))
    painter.drawRect(QRectF(rect))
    color = theme.accent if hot else theme.muted
    if plus:
        draw_plus(painter, rect.center(), color)
    else:
        draw_dots(painter, rect.center(), color, vertical=vertical)


@dataclass(frozen=True)
class TableHit:
    kind: str
    index: int = -1


class TableChrome(QWidget):
    """Transparent overlay; hit testing is shared with the card and its viewport."""

    def __init__(self, card):
        super().__init__(card)
        self.card = card
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._amount = 0.0
        self._inside = False
        self._focused = False
        self._point = QPointF(-100, -100)
        self.hover = None
        self.pressed = None
        self.drag = None
        self.hover_row = -1
        self.hover_column = -1
        self._animation = QPropertyAnimation(self, b"reveal", self)
        self._animation.setDuration(130)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        card.view.viewport().installEventFilter(self)
        card.view.horizontalScrollBar().valueChanged.connect(self.refresh)
        card.view.verticalScrollBar().valueChanged.connect(self.refresh)
        QApplication.instance().focusChanged.connect(self._focus_changed)

    def _get_reveal(self):
        return self._amount

    def _set_reveal(self, value):
        self._amount = value
        self.update()

    reveal = Property(float, _get_reveal, _set_reveal)

    def _focus_changed(self, _old, new):
        self._focused = new is not None and self.card.isAncestorOf(new)
        self._animate()

    def _animate(self):
        target = 1.0 if self._inside or self._focused else 0.0
        if abs(self._amount - target) < 0.001:
            self._animation.stop()
            return
        if (self._animation.endValue() == target
                and self._animation.state() == QAbstractAnimation.State.Running):
            return
        self._animation.stop()
        self._animation.setStartValue(self._amount)
        self._animation.setEndValue(target)
        self._animation.start()

    def grid_rect(self):
        viewport = self.card.view.viewport()
        origin = viewport.mapTo(self.card, viewport.rect().topLeft())
        return QRectF(origin.x(), origin.y(), viewport.width(), viewport.height())

    def row_rect(self, row):
        grid = self.grid_rect()
        view = self.card.view
        return QRectF(grid.left(), grid.top() + view.rowViewportPosition(row),
                      grid.width(), view.rowHeight(row))

    def column_rect(self, column):
        grid = self.grid_rect()
        view = self.card.view
        return QRectF(grid.left() + view.columnViewportPosition(column), grid.top(),
                      view.columnWidth(column), grid.height())

    def scroll_geometry(self, horizontal=False):
        grid = self.grid_rect()
        view = self.card.view
        bar = view.horizontalScrollBar() if horizontal else view.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return None
        track = (QRectF(grid.left() + 3, grid.bottom() + 2, max(1, grid.width() - 6), 4)
                 if horizontal else QRectF(grid.right() + 2, grid.top() + 3,
                                          4, max(1, grid.height() - 6)))
        length = track.width() if horizontal else track.height()
        total = bar.maximum() - bar.minimum() + max(1, bar.pageStep())
        thumb_length = min(length, max(26.0, length * bar.pageStep() / total))
        offset = (length - thumb_length) * (bar.value() - bar.minimum()) / (
            bar.maximum() - bar.minimum()
        )
        thumb = (QRectF(track.left() + offset, track.top(), thumb_length, track.height())
                 if horizontal else QRectF(track.left(), track.top() + offset,
                                          track.width(), thumb_length))
        return bar, track, thumb

    def control_rect(self, kind, index=-1):
        grid = self.grid_rect()
        left = (LEFT_RAIL - ROW_HANDLE_WIDTH) / 2
        if kind == "table_menu":
            return QRectF(left, 3, ROW_HANDLE_WIDTH, 18)
        if kind == "row_menu":
            visible = self.row_rect(index).intersected(grid)
            return QRectF(left, visible.center().y() - 9, ROW_HANDLE_WIDTH, 18)
        if kind == "column_menu":
            visible = self.column_rect(index).intersected(grid)
            return QRectF(visible.center().x() - 9, 3, 18, 18)
        if kind == "row_insert":
            y = (self.row_rect(index).top() if index < self.card.model.rowCount()
                 else self.row_rect(index - 1).bottom())
            return QRectF(left, y - 9, ROW_HANDLE_WIDTH, 18)
        if kind in ("column_insert", "resize"):
            column = index - 1 if kind == "column_insert" else index
            x = self.column_rect(column).right()
            if kind == "resize":
                return QRectF(x - 5, grid.top(), 10,
                              min(self.card.view.rowHeight(0), grid.height()))
            return QRectF(x - 9, 3, 18, 18)
        if kind == "append_row":
            top = grid.bottom() + (10 if self.scroll_geometry(True) else 3)
            return QRectF(grid.left(), top, grid.width(), max(1, self.height() - top - 2))
        if kind == "append_column":
            left = grid.right() + (9 if self.scroll_geometry() else 2)
            return QRectF(left, grid.top(), max(1, self.width() - left - 2), grid.height())
        return QRectF()

    def hit_test(self, point):
        grid = self.grid_rect()
        view, model = self.card.view, self.card.model
        if self.control_rect("table_menu").contains(point):
            return TableHit("table_menu")
        for horizontal, name in ((False, "vscroll"), (True, "hscroll")):
            geometry = self.scroll_geometry(horizontal)
            if geometry is not None and geometry[1].adjusted(-3, -3, 3, 3).contains(point):
                return TableHit(name)
        if self.control_rect("append_row").contains(point):
            return TableHit("append_row")
        column = view.columnAt(round(point.x() - grid.left()))
        row = view.rowAt(round(point.y() - grid.top()))
        if point.y() < grid.top() and grid.left() <= point.x() <= grid.right():
            candidates = {column, column + 1, model.columnCount()}
            for boundary in candidates:
                if 1 <= boundary <= model.columnCount():
                    rect = self.control_rect("column_insert", boundary)
                    if abs(point.x() - rect.center().x()) <= 6:
                        return TableHit("column_insert", boundary)
            if column >= 0:
                return TableHit("column_menu", column)
        if point.x() < grid.left() and grid.top() <= point.y() <= grid.bottom():
            for boundary in {row, row + 1, model.rowCount()}:
                if 0 <= boundary <= model.rowCount():
                    rect = self.control_rect("row_insert", boundary)
                    if abs(point.y() - rect.center().y()) <= 6:
                        return TableHit("row_insert", boundary)
            if row >= 0:
                return TableHit("row_menu", row)
        header_height = min(view.rowHeight(0), grid.height())
        if grid.contains(point) and point.y() <= grid.top() + header_height:
            for candidate in (column - 1, column):
                if 0 <= candidate < model.columnCount():
                    if self.control_rect("resize", candidate).contains(point):
                        return TableHit("resize", candidate)
        if self.control_rect("append_column").contains(point):
            return TableHit("append_column")
        return None

    def track(self, point, receiver=None):
        self._point = QPointF(point)
        self._inside = self.card.rect().contains(point.toPoint())
        grid = self.grid_rect()
        view = self.card.view
        self.hover_row = (view.rowAt(round(point.y() - grid.top()))
                          if grid.top() <= point.y() <= grid.bottom() else -1)
        self.hover_column = (view.columnAt(round(point.x() - grid.left()))
                             if grid.left() <= point.x() <= grid.right() else -1)
        self.hover = self.hit_test(point) if self._inside else None
        if receiver is not None:
            cursor = Qt.CursorShape.ArrowCursor
            if self.hover is not None:
                cursor = (Qt.CursorShape.SplitHCursor if self.hover.kind == "resize"
                          else Qt.CursorShape.PointingHandCursor)
            elif point.y() > grid.bottom():
                cursor = Qt.CursorShape.IBeamCursor
            receiver.setCursor(cursor)
            if receiver is self.card:
                receiver.setToolTip(self._tooltip(self.hover) if self.hover is not None else "")
        self._animate()
        view.viewport().update()
        self.update()

    def refresh(self, *_args):
        self.track(self._point)

    def _tooltip(self, hit):
        if hit.kind == "row_menu":
            return "Header row actions" if hit.index == 0 else f"Row {hit.index} actions"
        if hit.kind == "column_menu":
            return f"Column {hit.index + 1}: insert, align, or delete"
        return {
            "table_menu": "Table actions · copy Markdown, edit source, or delete",
            "row_insert": "Insert a row here", "column_insert": "Insert a column here",
            "append_row": "Append a row", "append_column": "Append a column",
            "resize": "Drag to resize · double-click to fit content",
        }.get(hit.kind, "Scroll table")

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                    QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
            point = QPointF(watched.mapTo(self.card, event.position().toPoint()))
            if watched is not self.card.view.viewport():
                self.track(point)
                return False
            return self.handle_mouse(event, point, watched)
        if kind == QEvent.Type.Leave:
            self.track(QPointF(self.card.mapFromGlobal(QCursor.pos())))
        elif kind == QEvent.Type.ToolTip:
            point = QPointF(watched.mapTo(self.card, event.pos()))
            hit = self.hit_test(point)
            if hit is not None:
                QToolTip.showText(event.globalPos(), self._tooltip(hit), self.card)
                return True
        return False

    def handle_mouse(self, event, point, receiver):
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            if self.drag is not None:
                self._drag_to(event.globalPosition())
                return True
            self.track(point, receiver)
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        hit = self.hit_test(point)
        if kind == QEvent.Type.MouseButtonDblClick and hit is not None and hit.kind == "resize":
            self.card.fit_column(hit.index)
            return True
        if kind == QEvent.Type.MouseButtonPress and hit is not None:
            self.card.activate()
            if hit.kind == "resize":
                self.drag = (
                    hit, event.globalPosition(), self.card.model.widths[hit.index], 1, receiver
                )
            elif hit.kind in ("vscroll", "hscroll"):
                horizontal = hit.kind == "hscroll"
                bar, track, thumb = self.scroll_geometry(horizontal)
                length = track.width() if horizontal else track.height()
                thumb_length = thumb.width() if horizontal else thumb.height()
                units = (bar.maximum() - bar.minimum()) / max(1, length - thumb_length)
                if not thumb.contains(point):
                    offset = point.x() - track.left() if horizontal else point.y() - track.top()
                    bar.setValue(round(bar.minimum() + (offset - thumb_length / 2) * units))
                self.drag = (hit, event.globalPosition(), bar.value(), units, receiver)
            else:
                self.pressed = hit
            if self.drag is not None:
                receiver.grabMouse()
            self.track(point, receiver)
            return True
        if kind == QEvent.Type.MouseButtonRelease:
            if self.drag is not None:
                grabbed = self.drag[4]
                self.drag = None
                grabbed.releaseMouse()
                self.track(point, receiver)
                return True
            if self.pressed is not None:
                pressed, self.pressed = self.pressed, None
                if pressed == hit:
                    self._invoke(hit)
                self.track(point, receiver)
                return True
        return False

    def _drag_to(self, point):
        hit, origin, value, units, _receiver = self.drag
        if hit.kind == "resize":
            self.card.model.set_column_width(hit.index, round(value + point.x() - origin.x()))
        else:
            horizontal = hit.kind == "hscroll"
            bar = (self.card.view.horizontalScrollBar() if horizontal
                   else self.card.view.verticalScrollBar())
            delta = point.x() - origin.x() if horizontal else point.y() - origin.y()
            bar.setValue(round(value + delta * units))
        self.update()

    def _invoke(self, hit):
        card, model = self.card, self.card.model
        if not model.alive:
            return
        if hit.kind == "append_row":
            card.append_row()
        elif hit.kind == "append_column":
            card.append_column()
        elif hit.kind == "row_insert":
            card.focus_cell(model.insert_row(hit.index), 0, edit=True)
        elif hit.kind == "column_insert":
            card.focus_cell(0, model.insert_column(hit.index), edit=True)
        elif hit.kind in ("row_menu", "column_menu", "table_menu"):
            scope = hit.kind.removesuffix("_menu")
            if scope == "row":
                card.focus_cell(hit.index, 0)
                card.view.selectRow(hit.index)
            elif scope == "column":
                card.focus_cell(0, hit.index)
                card.view.selectColumn(hit.index)
            rect = self.control_rect(hit.kind, hit.index)
            card.show_menu(card.mapToGlobal(rect.bottomLeft().toPoint()), scope=scope)

    def paintEvent(self, _event):
        grid = self.grid_rect()
        if grid.width() <= 1 or grid.height() <= 1:
            return
        theme = self.card.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outline = grid.adjusted(0.5, 0.5, -0.5, -0.5)
        frame = QPainterPath()
        frame.addRect(outline)
        square = QPainterPath()
        square.addRect(grid)
        painter.fillPath(square.subtracted(frame), theme.base)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        border = theme.accent if self.card.source_selected else theme.border
        painter.setPen(QPen(border, 1.5 if self.card.source_selected else 1))
        painter.drawPath(frame)

        if self.hover == TableHit("append_row"):
            footer = self.control_rect("append_row")
            painter.fillRect(footer, theme.action_fill)
            draw_plus(painter, QPointF(footer.left() + 14, footer.center().y()), theme.accent)
            painter.setFont(small_font(self.font()))
            painter.setPen(theme.accent)
            painter.drawText(
                footer.adjusted(28, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter, "Add row",
            )

        painter.save()
        painter.setOpacity(self._amount)
        draw_action(painter, self.control_rect("table_menu"), theme,
                     hot=self.hover == TableHit("table_menu"))
        row, column = self.hover_row, self.hover_column
        current = self.card.view.currentIndex()
        if self._focused and current.isValid():
            row = row if row >= 0 else current.row()
            column = column if column >= 0 else current.column()
        if row >= 0 and self.row_rect(row).intersects(grid):
            hit = TableHit("row_menu", row)
            if self.hover is None or self.hover.kind not in ("row_insert", "append_row"):
                draw_action(painter, self.control_rect(hit.kind, row), theme,
                            hot=self.hover == hit, vertical=True)
        if column >= 0 and self.column_rect(column).intersects(grid):
            hit = TableHit("column_menu", column)
            if self.hover is None or self.hover.kind != "column_insert":
                draw_action(
                    painter, self.control_rect(hit.kind, column), theme, hot=self.hover == hit
                )
        append = self.control_rect("append_column")
        hot = self.hover == TableHit("append_column")
        if hot:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.action_fill)
            painter.drawRect(append)
        draw_plus(painter, append.center(), theme.accent if hot else opacity(theme.muted, 0.65))
        painter.restore()

        if self.hover is not None and self.hover.kind in ("row_insert", "column_insert", "resize"):
            hit = self.hover
            rect = self.control_rect(hit.kind, hit.index)
            painter.setPen(QPen(opacity(theme.accent, 0.8), 1.5))
            if hit.kind == "row_insert":
                painter.drawLine(QPointF(grid.left(), rect.center().y()),
                                 QPointF(grid.right(), rect.center().y()))
            else:
                x = rect.center().x()
                painter.drawLine(QPointF(x, grid.top()), QPointF(x, grid.bottom()))
            if hit.kind != "resize":
                draw_action(painter, rect, theme, hot=True, plus=True)

        for horizontal, kind in ((False, "vscroll"), (True, "hscroll")):
            geometry = self.scroll_geometry(horizontal)
            if geometry is not None:
                hot = (self.hover == TableHit(kind)
                       or self.drag is not None and self.drag[0].kind == kind)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(opacity(theme.muted, 0.8 if hot else 0.4))
                painter.drawRect(geometry[2])
        painter.end()


class CellDocument(QTextDocument):
    def loadResource(self, _kind, _url):
        return None

    def drawContents(self, painter, rect=None):
        if rect is None:
            super().drawContents(painter)
        else:
            super().drawContents(painter, rect)
        painter.save()
        segments = {}

        def finish(level):
            top, bottom = segments.pop(level)
            x = level * QUOTE_INDENT
            painter.fillRect(QRectF(x, top - QUOTE_VERTICAL_PADDING, 4,
                                   bottom - top + QUOTE_VERTICAL_PADDING * 2), self.quote_color)

        block = self.begin()
        while block.isValid():
            depth = block.blockFormat().intProperty(QTextFormat.Property.BlockQuoteLevel)
            for level in list(segments):
                if level >= depth:
                    finish(level)
            if depth:
                bounds = self.documentLayout().blockBoundingRect(block)
                for level in range(depth):
                    top = segments.get(level, (bounds.top(),))[0]
                    segments[level] = top, bounds.bottom()
            block = block.next()
        for level in list(segments):
            finish(level)
        painter.restore()

def cell_document(source, font, width, *, header=False, palette=None, selected=False,
                  link_previews=(), widget=None, show_addresses=True, layout_factory=None):
    document = CellDocument()
    document.setUndoRedoEnabled(False)
    body = QFont(font)
    if header:
        body.setBold(True)
    document.setDefaultFont(body)
    system_mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    families = ([system_mono] if QFontDatabase.isFixedPitch(system_mono)
                else ["Menlo", "Consolas", "DejaVu Sans Mono", "monospace"])
    code_font = QFont(body)
    code_font.setFamilies(families)
    code_font.setStyleHint(QFont.StyleHint.Monospace)
    option = document.defaultTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    option.setTabStopDistance(QFontMetricsF(code_font).horizontalAdvance(" ") * 4)
    document.setDefaultTextOption(option)
    document.setDocumentMargin(0)
    palette = palette or QPalette()
    document.preview_theme = (preview_theme(widget, palette=palette, kinds={68} | {
        kind for _url, preview in link_previews for kind in preview.token_kinds
    }) if link_previews else None)
    document.quote_color = mix(palette.base().color(), palette.text().color(), 0.32)
    foreground = palette.color(
        QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
    ).name()
    link_color = palette.color(QPalette.ColorRole.Link)
    dark = palette.color(QPalette.ColorRole.Base).lightness() < 128
    if dark and link_color.lightness() < 140:
        link_color = link_color.lighter(180)
    link = foreground if selected else link_color.name()
    code_background = palette.color(QPalette.ColorRole.AlternateBase).name()
    mono = ", ".join(f'"{family}"' for family in families)
    document.setDefaultStyleSheet(
        f"p, div {{ margin: 0; }} a {{ color: {link}; }} "
        f".cell-wikilink {{ color: {link}; text-decoration: underline; }} "
        f"code {{ font-family: {mono}; background-color: {code_background}; }} "
        f"pre {{ font-family: {mono}; font-weight: normal; white-space: pre-wrap; "
        f"background-color: {code_background}; margin-top: 3px; margin-bottom: 3px; }}"
        f"blockquote {{ margin: {QUOTE_VERTICAL_PADDING}px 0 {QUOTE_VERTICAL_PADDING}px 20px; }}"
    )
    colors = (() if document.preview_theme is None else
              (*document.preview_theme.tokens, (-1, document.preview_theme.muted.rgba())))
    contents = cell_html(source, dark, link_previews, colors, show_addresses,
                         object_placeholders=True)
    document.setHtml(f'<div style="color: {foreground}">{contents}</div>')
    # Wrap code to the column instead of clipping it.
    block = document.begin()
    while block.isValid():
        fmt = block.blockFormat()
        # Qt's HTML import drops BlockQuoteLevel; CSS supplies the quote margin.
        depth = round(fmt.leftMargin() / QUOTE_INDENT)
        if depth:
            fmt.setProperty(QTextFormat.Property.BlockQuoteLevel, depth)
            fmt.setLeftMargin(depth * QUOTE_INDENT)
            fmt.setRightMargin(0)
            QTextCursor(block).setBlockFormat(fmt)
        if fmt.nonBreakableLines():
            fmt.setNonBreakableLines(False)
            QTextCursor(block).setBlockFormat(fmt)
        block = block.next()
    # Outer paragraph margins collapse; reserve quote padding in the root frame.
    frame = document.rootFrame()
    frame_format = frame.frameFormat()
    if document.begin().blockFormat().intProperty(QTextFormat.Property.BlockQuoteLevel):
        frame_format.setTopMargin(QUOTE_VERTICAL_PADDING)
    if document.lastBlock().blockFormat().intProperty(QTextFormat.Property.BlockQuoteLevel):
        frame_format.setBottomMargin(QUOTE_VERTICAL_PADDING)
    frame.setFrameFormat(frame_format)
    document.setTextWidth(max(20, width))
    if link_previews:
        code_font.setBold(False)
        document._preview_objects = CellCodeObjects(document, link_previews, code_font,
                                                     document.preview_theme,
                                                     show_addresses=show_addresses,
                                                     layout_factory=layout_factory)
    return document


_ids = count(1)
DEFAULT_COLUMN_WIDTH = 180


class TableModel(QAbstractTableModel):
    presentationChanged = Signal()
    removed = Signal()

    def __init__(self, registry, table):
        super().__init__(registry)
        self.registry = registry
        self.session = registry.session
        self.identity = next(_ids)
        self.alive = True
        self.table = table
        self.rows = [list(row) for row in table.rows]
        self.alignments = list(table.alignments)
        self.widths = [DEFAULT_COLUMN_WIDTH] * len(self.rows[0])
        self._range = QTextCursor(self.session.document)
        self._render_start = QTextCursor(self.session.document)
        self._render_end = QTextCursor(self.session.document)
        self._before = QTextCursor(self.session.document)
        self._after = QTextCursor(self.session.document)
        # _heights is None until measured; rows from _metric_next keep their last heights.
        self._heights = None
        self._metric_next = None
        self._metric_cache = {}  # (value, width, header) -> measured cell height
        self._metric_owners = set()
        self._metric_task = IdleTask(self)
        self._metric_task.timeout.connect(self._measure_chunk)
        self._editor_heights = {}
        self._set_range(table.start_position, table.end_position)
        self._set_region(table)

    @property
    def start(self):
        return self._range.selectionStart()

    @property
    def end(self):
        return self._range.selectionEnd()

    def _set_range(self, start, end):
        self._range.setPosition(start)
        self._range.setPosition(end, QTextCursor.MoveMode.KeepAnchor)

    @property
    def render_start(self):
        return self._render_start.position()

    @property
    def render_end(self):
        return self._render_end.position()

    @property
    def before_position(self):
        return self._before.position()

    @property
    def after_position(self):
        return self._after.position()

    def _set_region(self, table):
        positions = table_region(self.session.source, table)
        cursors = self._render_start, self._render_end, self._before, self._after
        for cursor, position in zip(cursors, positions, strict=True):
            cursor.setPosition(position)

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows[0])

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if (not index.isValid() or not 0 <= index.row() < self.rowCount()
                or not 0 <= index.column() < self.columnCount()):
            return None
        if role in (
            Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole, Qt.ItemDataRole.ToolTipRole,
        ):
            return self.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            alignment = self.alignments[index.column()]
            horizontal = {
                "center": Qt.AlignmentFlag.AlignHCenter,
                "right": Qt.AlignmentFlag.AlignRight,
            }.get(alignment, Qt.AlignmentFlag.AlignLeft)
            return horizontal | Qt.AlignmentFlag.AlignVCenter
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Vertical:
            return "H" if section == 0 else str(section)
        label, number = "", section + 1
        while number:
            number, remainder = divmod(number - 1, 26)
            label = chr(65 + remainder) + label
        return label

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and not self.session.load_failed:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        return self.set_cell(index.row(), index.column(), str(value))

    def update_table(self, table):
        self.table = table
        self._set_range(table.start_position, table.end_position)
        self._set_region(table)
        rows, alignments = [list(row) for row in table.rows], list(table.alignments)
        if (rows, alignments) != (self.rows, self.alignments):
            self._set_values(rows, alignments)

    def _set_values(self, rows, alignments):
        changed_shape = (len(rows), len(rows[0])) != (self.rowCount(), self.columnCount())
        if changed_shape:
            self._editor_heights.clear()
            self.beginResetModel()
        self.rows, self.alignments = rows, alignments
        self.widths = (self.widths + [DEFAULT_COLUMN_WIDTH] * len(rows[0]))[:len(rows[0])]
        self.invalidate_metrics(keep_cache=True, same_rows=not changed_shape)
        if changed_shape:
            self.endResetModel()
        else:
            self.dataChanged.emit(
                self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1)
            )
        self.presentationChanged.emit()

    def _write(self, rows, alignments=None, *, token=None):
        if not self.alive or self.session.load_failed:
            return False
        rows = [[encode_cell(value) for value in row] for row in rows]
        alignments = list(self.alignments if alignments is None else alignments)
        if (rows, alignments) == (self.rows, self.alignments):
            return True
        source = serialize_table(
            rows, alignments, prefix=self.table.prefix,
            continuation_prefix=self.table.continuation_prefix,
        )
        start, end = self.start, self.end
        self.session.apply_source_edit(start, end, source, merge_token=token)
        self._set_range(start, start + utf16_length(source))
        self.table = replace(self.table, start_position=start, end_position=self.end)
        self._set_values(rows, alignments)
        return True

    def set_cell(self, row, column, text, *, token=None):
        if not 0 <= row < self.rowCount() or not 0 <= column < self.columnCount():
            return False
        rows = [list(values) for values in self.rows]
        rows[row][column] = text
        return self._write(rows, token=token)

    def insert_row(self, index):
        index = max(0, min(index, self.rowCount()))
        rows = [list(row) for row in self.rows]
        rows.insert(index, [""] * self.columnCount())
        self._write(rows)
        return index

    def delete_row(self, index):
        if not self.alive or self.session.load_failed:
            return
        if self.rowCount() == 1 and index == 0:
            self.delete_table()
        elif 0 <= index < self.rowCount():
            self._write([
                list(row) for row_number, row in enumerate(self.rows) if row_number != index
            ])

    def delete_table(self):
        if self.alive and not self.session.load_failed:
            self.session.apply_source_edit(self.render_start, self.render_end, "")
            self.session.highlighter.refresh()

    def insert_column(self, index):
        index = max(0, min(index, self.columnCount()))
        rows = [list(row) for row in self.rows]
        for row_number, row in enumerate(rows):
            row.insert(index, f"Column {index + 1}" if row_number == 0 else "")
        alignments = list(self.alignments)
        alignments.insert(index, "")
        self.widths.insert(index, DEFAULT_COLUMN_WIDTH)
        self._write(rows, alignments)
        return index

    def delete_column(self, index):
        if self.columnCount() > 1 and 0 <= index < self.columnCount():
            rows = [[value for col, value in enumerate(row) if col != index] for row in self.rows]
            alignments = [value for col, value in enumerate(self.alignments) if col != index]
            self.widths.pop(index)
            self._write(rows, alignments)

    def set_alignment(self, columns, alignment):
        alignments = list(self.alignments)
        for column in columns:
            alignments[column] = alignment
        self._write([list(row) for row in self.rows], alignments)

    def change_cells(self, coordinates, transform):
        rows = [list(row) for row in self.rows]
        for row, column in coordinates:
            rows[row][column] = transform(rows[row][column])
        self._write(rows)

    def paste_matrix(self, row, column, matrix, alignments=()):
        if not matrix:
            return
        columns = max(self.columnCount(), column + max(len(values) for values in matrix))
        rows = [list(values) + [""] * (columns - len(values)) for values in self.rows]
        rows.extend([""] * columns for _ in range(max(0, row + len(matrix) - len(rows))))
        alignment_values = self.alignments + [""] * (columns - self.columnCount())
        for offset, values in enumerate(matrix):
            rows[row + offset][column:column + len(values)] = values
        for offset, alignment in enumerate(alignments):
            if column + offset < columns:
                alignment_values[column + offset] = alignment
        self._write(rows, alignment_values)

    def markdown(self):
        return serialize_table(self.rows, self.alignments)

    def source_markdown(self):
        return self._range.selectedText().replace("\u2029", "\n").replace("\u2028", "\n")

    def set_column_width(self, column, width):
        width = max(72, min(800, width))
        if self.widths[column] != width:
            self.widths[column] = width
            self.invalidate_metrics(keep_cache=True)
            self.presentationChanged.emit()

    @property
    def show_hlil_addresses(self):
        return self.session.notebook.preferences.effective('hlil_show_addresses')

    def invalidate_metrics(self, *, keep_cache=False, same_rows=True):
        """Re-measure rows, keeping published heights until measured."""
        if not keep_cache:
            self._metric_cache = {}
        if self._heights is not None:
            previous = self._heights
            self._heights = tuple(
                known if (known := self._cached_row_height(row)) is not None
                else previous[row] if same_rows and row < len(previous)
                else self._base_height(row)
                for row in range(self.rowCount())
            )
        self._metric_next = 0
        if self._metric_owners and self.session.layout.progressive:
            self._metric_task.start()

    @staticmethod
    def _base_height(row):
        return HEADER_HEIGHT if row == 0 else ROW_HEIGHT

    def _cell_key(self, row, column):
        return self.rows[row][column], self.widths[column], row == 0

    def _cached_row_height(self, row):
        heights = [self._metric_cache.get(self._cell_key(row, column))
                   for column in range(self.columnCount())]
        if None in heights:
            return None
        return max(self._base_height(row), *heights)

    def _measure_row(self, row, font):
        for column in range(self.columnCount()):
            key = self._cell_key(row, column)
            if key not in self._metric_cache:
                value = self.rows[row][column]
                self._metric_cache[key] = max(
                    self._measure_value(value, column, row == 0, font, previews)
                    for previews in self.registry.preview_options(value)
                )
        return self._cached_row_height(row)

    def _measure_rows(self, deadline=None):
        """Measure pending rows in order; returns whether any published height changed."""
        heights = list(self._heights)
        font = self.session.document.defaultFont()
        while self._metric_next < self.rowCount():
            heights[self._metric_next] = self._measure_row(self._metric_next, font)
            self._metric_next += 1
            if deadline is not None and perf_counter() >= deadline:
                break
        if self._metric_next >= self.rowCount():
            self._metric_next = None
        changed = tuple(heights) != self._heights
        self._heights = tuple(heights)
        return changed

    def row_heights(self):
        if self._heights is None:
            self._heights = tuple(self._base_height(row) for row in range(self.rowCount()))
            self._metric_next = 0
        if self._metric_next is not None:
            if self.session.layout.progressive:
                # Saved notes measure off-screen tables in idle time.
                if self._metric_owners:
                    self._metric_task.start()
            else:
                self._measure_rows()
        heights = list(self._heights)
        for row, _column, height in self._editor_heights.values():
            if row < len(heights):
                heights[row] = max(heights[row], height)
        return tuple(heights)

    def _measure_value(self, value, column, header, font, previews):
        document = cell_document(
            value, font, self.widths[column] - CELL_PADDING_X * 2, header=header,
            link_previews=previews, show_addresses=self.show_hlil_addresses,
            layout_factory=self.registry.layout_factory,
        )
        document.setParent(self)
        try:
            return math.ceil(document.size().height()) + CELL_PADDING_Y * 2
        finally:
            document.deleteLater()

    def set_metrics_active(self, owner, active):
        if active:
            self._metric_owners.add(owner)
            self.row_heights()
            if self._metric_next is not None:
                self._metric_task.start()
        else:
            self._metric_owners.discard(owner)
            if not self._metric_owners:
                self._metric_task.stop()

    def _measure_chunk(self):
        if not self.alive or not self._metric_owners or self._metric_next is None:
            return
        if self._heights is None:
            self.row_heights()
        changed = self._measure_rows(deadline=perf_counter() + .002)
        if self._metric_next is not None:
            self._metric_task.start()
        if changed:
            self.presentationChanged.emit()

    def set_editor_height(self, owner, row, column, height):
        if not self.alive or not 0 <= row < self.rowCount() or not 0 <= column < self.columnCount():
            return
        value = row, column, height
        if self._editor_heights.get(owner) != value:
            self._editor_heights[owner] = value
            self.presentationChanged.emit()

    def clear_editor_height(self, owner):
        if self._editor_heights.pop(owner, None) is not None and self.alive:
            self.presentationChanged.emit()

    @property
    def display_height(self):
        return min(self.registry.height_limit, TABLE_CHROME_HEIGHT + sum(self.row_heights()))

    def cell_at_position(self, position):
        for row, ranges in enumerate(self.table.cell_ranges):
            for col, (start, end) in enumerate(ranges):
                if start <= position <= end:
                    return row, col
        return 0, 0


class TableRegistry(QObject):
    modelsChanged = Signal()
    geometryChanged = Signal()

    def __init__(self, session):
        super().__init__(session)
        self.session = session
        self.models: list[TableModel] = []
        self._height_limits = {}
        self._preview_providers = WeakValueDictionary()
        self._preview_timer = single_shot(self, self.refresh_metrics)
        self._metrics_dirty = False
        self._pending_tables = None

    def set_preview_provider(self, owner, provider, *, defer=False):
        if self._preview_providers.get(owner) is provider:
            return
        previous = self._preview_providers.pop(owner, None)
        if previous is not None and previous not in self._preview_providers.values():
            if isValid(previous):
                previous.changed.disconnect(self._preview_changed)
                previous.destroyed.disconnect(self._queue_preview_metrics)
        if provider is not None:
            if provider not in self._preview_providers.values():
                provider.changed.connect(self._preview_changed)
                provider.destroyed.connect(self._queue_preview_metrics)
            self._preview_providers[owner] = provider
        if defer:
            self._queue_preview_metrics()
        else:
            self.refresh_metrics()

    def _queue_preview_metrics(self):
        if isValid(self._preview_timer):
            self._preview_timer.start(0)

    def _preview_changed(self):
        provider = self.sender()
        urls = getattr(provider, 'changed_urls', None)
        if urls is not None:
            from .provider import _cell_targets

            affected = [model for model in self.models if any(
                url in urls for row in model.rows for value in row
                for url, _expanded, _mode in _cell_targets(value)
            )]
        else:
            affected = self.models
        if not affected:
            return
        for model in affected:
            model.invalidate_metrics()
        if self._height_limits:
            self._geometry_changed()
        else:
            self._metrics_dirty = True

    def preview_options(self, source):
        options = {provider.for_cell(source, load=False)
                   for provider in self._preview_providers.values() if isValid(provider)}
        return options or {()}

    @property
    def layout_factory(self):
        return next((provider.prepare_code for provider in self._preview_providers.values()
                     if isValid(provider)), None)

    def refresh_metrics(self):
        if not isValid(self.session.layout):
            return
        for model in self.models:
            model.invalidate_metrics()
        if not self._height_limits:
            self._metrics_dirty = True
            return
        self._geometry_changed()

    @property
    def height_limit(self):
        return min(520, *self._height_limits.values()) if self._height_limits else 520

    def set_height_limit(self, owner, height):
        old = self.height_limit
        if height is None:
            self._height_limits.pop(owner, None)
        else:
            document = self.session.document
            # Leave a writable paragraph: a tall EOF table must not fill the viewport.
            prose_height = QFontMetricsF(document.defaultFont()).lineSpacing() * 2
            reserved = math.ceil(document.documentMargin() * 2 + TABLE_MARGIN * 2
                                 + table_exit_height(document) + prose_height)
            self._height_limits[owner] = max(TABLE_CHROME_HEIGHT + 1, height - reserved)
        if self._height_limits and self._pending_tables is not None:
            pending, self._pending_tables = self._pending_tables, None
            self.update(pending)
        if self.height_limit != old or self._metrics_dirty and self._height_limits:
            self._metrics_dirty = False
            self._geometry_changed()

    def update(self, tables):
        if self.session.layout.progressive and not self._height_limits:
            self._pending_tables = tuple(tables)
            return
        remaining = list(self.models)
        updated = []
        for table in tables:
            model = next((item for item in remaining if item.start == table.start_position), None)
            if model is None:
                model = next(
                    (item for item in remaining if item.start <= table.end_position
                     and item.end >= table.start_position), None,
                )
            if model is None:
                model = TableModel(self, table)
                model.presentationChanged.connect(self._geometry_changed)
            else:
                remaining.remove(model)
                model.update_table(table)
            updated.append(model)
        for model in remaining:
            model.alive = False
            model.removed.emit()
        self.models = updated
        self._geometry_changed()
        self.modelsChanged.emit()
        for model in remaining:
            model.deleteLater()

    def _geometry_changed(self):
        self.session.layout.set_tables([
            (model.identity, model.render_start, model.render_end, model.display_height)
            for model in self.models
        ])
        self.geometryChanged.emit()

    def at_position(self, position):
        return next((model for model in self.models if model.start <= position <= model.end), None)

    def at_render_position(self, position):
        return next((model for model in self.models
                     if model.render_start <= position <= model.render_end), None)


class TableGridPicker(QWidget):
    chosen = Signal(int, int)  # columns, rows (including the header)
    CELL = 22
    MARGIN = 14
    TOP = 44
    COLUMNS = 10
    ROWS = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.columns = 1
        self.rows = 1
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Insert Markdown table")
        self.setFixedSize(self.sizeHint())
        self._announce()

    def sizeHint(self):
        return QSize(
            self.MARGIN * 2 + self.CELL * self.COLUMNS, self.TOP + self.CELL * self.ROWS + 30
        )

    def _announce(self):
        self.setAccessibleDescription(f"{self.columns} columns, {self.rows} rows including header")
        self.update()

    def _pick(self, point):
        column = (point.x() - self.MARGIN) // self.CELL
        row = (point.y() - self.TOP) // self.CELL
        if 0 <= column < self.COLUMNS and 0 <= row < self.ROWS:
            self.columns, self.rows = column + 1, row + 1
            self._announce()
            return True
        return False

    def mouseMoveEvent(self, event):
        self._pick(event.position().toPoint())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pick(event.position().toPoint()):
            self.chosen.emit(self.columns, self.rows)

    def keyPressEvent(self, event):
        moves = {
            Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1),
        }
        if event.key() in moves:
            dx, dy = moves[event.key()]
            self.columns = max(1, min(self.COLUMNS, self.columns + dx))
            self.rows = max(1, min(self.ROWS, self.rows + dy))
            self._announce()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.chosen.emit(self.columns, self.rows)
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = TableTheme(self.palette())
        painter.fillRect(self.rect(), theme.base)
        painter.setPen(theme.text)
        painter.setFont(small_font(self.font(), size=12, bold=True))
        painter.drawText(QRectF(self.MARGIN, 8, self.width() - 90, 24),
                         Qt.AlignmentFlag.AlignVCenter, "Insert table")
        badge = QRectF(self.width() - self.MARGIN - 56, 8, 56, 24)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.action_fill)
        painter.drawRect(badge)
        painter.setPen(theme.accent)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, f"{self.columns} × {self.rows}")
        for row in range(self.ROWS):
            for column in range(self.COLUMNS):
                selected = row < self.rows and column < self.columns
                border = opacity(theme.accent, 0.75) if selected else theme.rule
                painter.setPen(QPen(border, 1))
                fill = (mix(theme.surface, theme.accent, 0.22 if row == 0 else 0.12)
                        if selected else theme.surface)
                painter.setBrush(fill)
                painter.drawRect(QRectF(self.MARGIN + column * self.CELL + 0.5,
                                        self.TOP + row * self.CELL + 0.5,
                                        self.CELL - 4, self.CELL - 4))
        painter.setPen(theme.muted)
        painter.setFont(small_font(self.font()))
        painter.drawText(QRectF(self.MARGIN, self.height() - 25, self.width() - 28, 20),
                         Qt.AlignmentFlag.AlignVCenter, "First row is the header")
        painter.end()


def custom_table_size(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Insert table")
    layout = QFormLayout(dialog)
    columns, rows = QSpinBox(dialog), QSpinBox(dialog)
    columns.setRange(1, 100)
    rows.setRange(1, 1000)
    columns.setValue(3)
    rows.setValue(4)
    layout.addRow("Columns", columns)
    layout.addRow("Rows (including header)", rows)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return columns.value(), rows.value()
    return None


_NAVIGATION_KEYS = (
    Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right,
    Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Home, Qt.Key.Key_End,
)


class CellEditor(QPlainTextEdit):
    def __init__(self, card, index, parent):
        super().__init__(parent)
        self.card = card
        self.row, self.column = index.row(), index.column()
        self.token = object()
        self.original = index.data(Qt.ItemDataRole.EditRole)
        self._syncing = True
        self._reflowing = False
        self._keeping_cursor = False
        self._height_key = object()
        self._caret_timer = single_shot(self, self._ensure_table_cursor_visible)
        self._reflow_timer = single_shot(self, self._reflow)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setUndoRedoEnabled(False)
        self.setTabChangesFocus(False)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        font = QFont(card.model.session.document.defaultFont())
        font.setBold(index.row() == 0)
        self.setFont(font)
        self._quote_layout = MarkdownDocumentLayout(self.document(), code_spacing=False)
        self.document().setDocumentLayout(self._quote_layout)
        self.document().setDocumentMargin(0)
        self.setViewportMargins(CELL_PADDING_X - 1, CELL_PADDING_Y - 1,
                                CELL_PADDING_X - 1, CELL_PADDING_Y - 1)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setPalette(card.theme.content_palette)
        self.setPlainText(cell_editor_text(self.original))
        self._last_source_text = self.toPlainText()
        self._highlighter = MarkdownHighlighter(self.document(), self.palette())
        self._highlighter.analysisChanged.connect(self._quote_layout.set_analysis)
        self._quote_layout.set_analysis(self._highlighter.analysis)
        self._quotes = QuotePresentation(self)
        self._quotes.bind(self._highlighter)
        self._links = LinkPresentation(self, follow=card.follow_link)
        self._links.bind(self._highlighter, card.preview_provider,
                         preferences=card.model.session.notebook.preferences)
        self.setTabStopDistance(
            QFontMetricsF(self._highlighter.code_font).horizontalAdvance(" ") * 4
        )
        self._highlighter.analysisChanged.connect(self._reflow)
        self._highlighter.previewGeometryChanged.connect(self._queue_preview_reflow)
        self._syncing = False
        self.textChanged.connect(self._changed)
        self.cursorPositionChanged.connect(self._queue_caret_visibility)
        self.destroyed.connect(
            lambda _obj=None, key=self._height_key, model=card.model: model.clear_editor_height(key)
        )
        self.setAccessibleName(f"Markdown cell, row {self.row + 1}, column {self.column + 1}")

    def refresh_font(self):
        font = QFont(self.card.model.session.document.defaultFont())
        font.setBold(self.row == 0)
        self.setFont(font)
        self.document().setDefaultFont(font)
        self._highlighter.refresh_formats()
        self._links.refresh()
        self.setTabStopDistance(
            QFontMetricsF(self._highlighter.code_font).horizontalAdvance(" ") * 4,
        )
        self._reflow()

    def _changed(self):
        if not self._syncing and not self._reflowing and self.card.model.alive:
            text = self.toPlainText()
            if text == self._last_source_text:
                return
            self._last_source_text = text
            self._reflow()
            self.card.model.set_cell(self.row, self.column, text, token=self.token)
            self._queue_caret_visibility()

    def _reflow(self, *_args):
        if self._reflowing or not hasattr(self, "_highlighter") or not self.card.model.alive:
            return
        self._reflowing = True
        try:
            self._highlighter.refresh()
            document = self.document()
            layout = document.documentLayout()
            height = 0.0
            block = document.begin()
            while block.isValid():
                height += layout.blockBoundingRect(block).height()
                block = block.next()
            # Room for the empty final line and the caret.
            required = math.ceil(height) + CELL_PADDING_Y * 2 + 2
            self.card.model.set_editor_height(self._height_key, self.row, self.column, required)
            self.card.view.updateGeometries()
            self.card.view.updateEditorGeometries()
            self.verticalScrollBar().setValue(0)
            self.horizontalScrollBar().setValue(0)
        finally:
            self._reflowing = False
        self._ensure_table_cursor_visible()
        self._queue_caret_visibility()

    def _queue_preview_reflow(self):
        if not self._reflow_timer.isActive():
            self._reflow_timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Not from inside the scroll area's resize handler: it restores a stale size.
        if hasattr(self, "_reflow_timer") and not self._reflow_timer.isActive():
            self._reflow_timer.start(0)

    def _queue_caret_visibility(self):
        if self._links.preserving_view:
            return
        if not self._caret_timer.isActive():
            self._caret_timer.start(0)

    def _ensure_table_cursor_visible(self):
        if (self._keeping_cursor or self._links.preserving_view
                or not self.hasFocus() or self.textCursor().hasSelection()):
            return
        self._keeping_cursor = True
        try:
            view = self.card.view
            rectangle = QRect(self.cursorRect())
            rectangle.moveTopLeft(self.viewport().mapTo(view.viewport(), rectangle.topLeft()))
            viewport = view.viewport().rect().adjusted(2, 2, -2, -2)
            vertical = view.verticalScrollBar()
            if rectangle.bottom() > viewport.bottom():
                vertical.setValue(vertical.value() + rectangle.bottom() - viewport.bottom())
            elif rectangle.top() < viewport.top():
                vertical.setValue(vertical.value() + rectangle.top() - viewport.top())
        finally:
            self._keeping_cursor = False

    def wheelEvent(self, event):
        event.ignore()  # Scroll the table/note, never a second viewport inside a cell.

    def mousePressEvent(self, event):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.RightButton:
            # Passed on, the table would take focus and close this editor.
            event.accept()

    def paintEvent(self, event):
        self._quotes.paint(event, background=True)
        super().paintEvent(event)
        self._quotes.paint(event)
        self._links.paint(event)

    def sync_text(self, value):
        if encode_cell(self.toPlainText()) == encode_cell(value):
            return
        self.original = value
        self.token = object()
        self._syncing = True
        position = self.textCursor().position()
        self.setPlainText(cell_editor_text(value))
        cursor = self.textCursor()
        cursor.setPosition(min(position, self.document().characterCount() - 1))
        self.setTextCursor(cursor)
        self._last_source_text = self.toPlainText()
        self._syncing = False
        self._reflow()

    def wrap_selection(self, delimiter):
        self.token = object()  # Formatting is a separate undo operation.
        self._highlighter.refresh()
        self.setTextCursor(toggle_inline_selection(
            self.textCursor(), delimiter,
            self._highlighter.analysis if not self._highlighter.warning else None,
        ))
        self.token = object()  # Subsequent typing must not merge into the toggle.
        self.setFocus()

    def event(self, event):
        if event.type() == QEvent.Type.ShortcutOverride and event.key() in (
            Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Return, Qt.Key.Key_Enter,
        ):
            event.accept()
            return True
        if event.type() == QEvent.Type.ShortcutOverride and any(event.matches(key) for key in (
            QKeySequence.StandardKey.Undo, QKeySequence.StandardKey.Redo,
            QKeySequence.StandardKey.Bold, QKeySequence.StandardKey.Italic,
            QKeySequence.StandardKey.Copy, QKeySequence.StandardKey.Cut,
            QKeySequence.StandardKey.Paste, QKeySequence.StandardKey.SelectAll,
        )):
            event.accept()
            return True
        return super().event(event)

    def in_code_block(self):
        # Synchronously: the next key must see the current fences.
        self._highlighter.refresh()
        cursor = self.textCursor()
        return any(
            code.start_position <= cursor.selectionEnd()
            and code.end_position >= cursor.selectionStart()
            for code in self._highlighter.analysis.code_blocks
        )

    def edits_text(self, event):
        return (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and (bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                     or self.in_quote())
                or self.in_code_block())

    def in_quote(self):
        self._highlighter.refresh()
        row = self._highlighter.analysis.line_at_position(self.textCursor().position())
        return any(quote.line == row for quote in self._highlighter.analysis.quote_lines)

    def outdent_code(self):
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        block = self.document().findBlock(start)
        cursor.beginEditBlock()
        while block.isValid() and block.position() <= end:
            if block.position() == end and start != end:
                break
            text = block.text()
            count = 1 if text.startswith("\t") else min(4, len(text) - len(text.lstrip(" ")))
            if count:
                edit = QTextCursor(block)
                edit.movePosition(QTextCursor.MoveOperation.Right,
                                  QTextCursor.MoveMode.KeepAnchor, count)
                edit.removeSelectedText()
                end -= count
            block = block.next()
        cursor.endEditBlock()

    def keyPressEvent(self, event):
        shifted = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.edits_text(event)
                and leave_link_source(self._links)):
            return  # A newline here would split a revealed embed's link.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.edits_text(event):
            if shifted or not continue_quote(self, self._highlighter.analysis):
                self.insertPlainText("\n")
        elif event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and self.in_code_block():
            if shifted or event.key() == Qt.Key.Key_Backtab:
                self.outdent_code()
            else:
                self.insertPlainText("\t")
        elif event.matches(QKeySequence.StandardKey.Undo):
            self.card.undo()
        elif event.matches(QKeySequence.StandardKey.Redo):
            self.card.redo()
        elif event.matches(QKeySequence.StandardKey.Bold):
            self.wrap_selection("**")
        elif event.matches(QKeySequence.StandardKey.Italic):
            self.wrap_selection("*")
        else:
            super().keyPressEvent(event)

    def create_context_menu(self, embed=None, point=None):
        menu = build_edit_menu(TextActions(
            self, self.card.model.session, self._highlighter, cell=True,
        ))
        link = (embed.link if embed is not None
                else source_link_at(self._links, self.textCursor().position()))
        if link is not None:
            add_embed_actions(menu, self._links, link, embed, point)
        menu.addSeparator()
        self.card._populate_table_menu(menu.addMenu("Table"), "cell")
        return menu

    def contextMenuEvent(self, event):
        embed = None
        if event.reason() == QContextMenuEvent.Reason.Mouse:
            # Right-clicking an embed acts on it where it is: it is not revealed.
            _link, embed = context_target(self._links, event.pos())
            if embed is None and not self.textCursor().hasSelection():
                self.setTextCursor(self._links.cursor_for_position(event.pos()))
        menu = self.create_context_menu(embed, QPointF(event.pos()))
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.exec(event.globalPos())


class MarkdownCellDelegate(QStyledItemDelegate):
    def __init__(self, card):
        super().__init__(card.view)
        self.card = card
        self._documents = {}
        self._prepared = {}

    def prepare(self, index):
        """Resolve/shape/record a cell before painting; retain all prepared variants."""
        theme = self.card.theme
        width = self.card.view.columnWidth(index.column()) - CELL_PADDING_X * 2
        value = index.data(Qt.ItemDataRole.DisplayRole)
        previews = self.card.link_previews(value, load=True)
        kinds = {68} | {kind for _url, preview in previews for kind in preview.token_kinds}
        colors = (preview_theme(self.card.view, palette=theme.content_palette,
                                 kinds=kinds).key if previews else None)
        alignment = self.card.model.alignments[index.column()]
        key = (value, width, index.row() == 0, theme.content_palette.cacheKey(),
               self.card.view.font().toString(), previews, colors, alignment,
               self.card.model.show_hlil_addresses)
        document = self._documents.get(key)
        revision = getattr(self.card.preview_provider, 'layout_revision', 0)
        if (document is not None
                and getattr(getattr(document, '_preview_objects', None), 'pending', False)
                and document._preparation_revision != revision):
            document = None
        if document is None:
            document = cell_document(
                value, self.card.view.font(), width, header=index.row() == 0,
                palette=theme.content_palette, link_previews=previews, widget=self.card.view,
                show_addresses=self.card.model.show_hlil_addresses,
                layout_factory=(self.card.preview_provider.prepare_code
                                if self.card.preview_provider is not None else None),
            )
            document.setParent(self)
            option = document.defaultTextOption()
            option.setAlignment({
                'center': Qt.AlignmentFlag.AlignHCenter, 'right': Qt.AlignmentFlag.AlignRight,
            }.get(alignment, Qt.AlignmentFlag.AlignLeft))
            document.setDefaultTextOption(option)
            bounds = QRectF(QPointF(), document.size())
            objects = getattr(document, '_preview_objects', None)
            if objects is not None:
                objects.recording = True
                objects.rectangles.clear()
            regions = None
            if objects is not None and not document.toPlainText().replace('\ufffc', '').strip():
                block = document.begin()
                quoted = False
                while block.isValid():
                    quoted |= bool(block.blockFormat().intProperty(
                        QTextFormat.Property.BlockQuoteLevel,
                    ))
                    block = block.next()
                if not quoted:
                    regions = []
            document._retained = RetainedDrawing.record(document.drawContents, bounds,
                                                        regions=regions)
            if objects is not None:
                objects.recording = False
            self._documents[key] = document
            document._preparation_revision = revision
        self._prepared[index.row(), index.column()] = document

    def paint(self, painter, option, index):
        theme = self.card.theme
        bounds = QRectF(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        background = theme.header if index.row() == 0 else theme.surface
        if selected:
            background = theme.selection
        elif self.card.chrome.hover_row == index.row():
            background = theme.hover if index.row() else theme.header
        painter.fillRect(bounds, background)
        painter.setPen(QPen(theme.rule, 1))
        if index.row() < self.card.model.rowCount() - 1:
            painter.drawLine(QPointF(bounds.left(), bounds.bottom() - 0.5),
                             QPointF(bounds.right(), bounds.bottom() - 0.5))
        if index.column() < self.card.model.columnCount() - 1:
            painter.setPen(QPen(opacity(theme.rule, 0.7), 1))
            painter.drawLine(QPointF(bounds.right() - 0.5, bounds.top()),
                             QPointF(bounds.right() - 0.5, bounds.bottom()))
        rect = bounds.adjusted(CELL_PADDING_X, CELL_PADDING_Y, -CELL_PADDING_X, -CELL_PADDING_Y)
        document = self._prepared.get((index.row(), index.column()))
        if document is None:
            self.card.view.queue_preparation(restart=False)
            return
        painter.save()
        painter.setClipRect(rect)
        top = bounds.top() + max(CELL_PADDING_Y, (bounds.height() - document.size().height()) / 2)
        origin = QPointF(rect.left(), top)
        objects = getattr(document, '_preview_objects', None)
        visible = rect.intersected(visible_rect(self.card.view.viewport()))
        document._retained.draw(painter, origin, visible)
        if objects is not None:
            for identity, code_rect in objects.rectangles.items():
                code_rect = code_rect.translated(origin)
                clipped = code_rect.intersected(visible)
                if not clipped.isEmpty():
                    code = objects.layouts[identity]
                    point = self.card.view._hover_point
                    code.draw(painter, code_rect.topLeft(), visible=visible,
                              hovered=point is not None and clipped.contains(point))
                    self.card.view._preview_hits.append(
                        (code_rect, clipped, code),
                    )
        painter.restore()

    def sizeHint(self, option, index):
        # Size hints must never rebuild cells.
        model = self.card.model
        return QSize(model.widths[index.column()], model.row_heights()[index.row()])

    def createEditor(self, parent, option, index):
        editor = CellEditor(self.card, index, parent)
        editor.selectAll()
        self.card.cell_editor = editor
        editor.viewport().installEventFilter(self.card.chrome)
        self.card.activate()
        return editor

    def setEditorData(self, editor, index):
        editor.sync_text(index.data(Qt.ItemDataRole.EditRole))

    def setModelData(self, editor, model, index):
        if model.alive and index.isValid():
            model.set_cell(index.row(), index.column(), editor.toPlainText(), token=editor.token)

    def updateEditorGeometry(self, editor, option, _index):
        rectangle = QRect(option.rect)
        viewport = self.card.view.viewport().rect()
        rectangle.setLeft(max(rectangle.left(), viewport.left()))
        rectangle.setRight(min(rectangle.right(), viewport.right()))
        if rectangle.width() <= 2:
            editor.hide()
            return
        # Keep the full row height; a clipped editor would scroll internally.
        rectangle = rectangle.adjusted(1, 1, -1, -1)
        resized = editor.size() != rectangle.size()
        editor.setGeometry(rectangle)
        if resized:
            editor._reflow()

    def destroyEditor(self, editor, index):
        if self.card.cell_editor is editor:
            self.card.cell_editor = None
        self.card.model.clear_editor_height(editor._height_key)
        super().destroyEditor(editor, index)

    def eventFilter(self, editor, event):
        if isinstance(editor, CellEditor) and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                vertical = key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                shifted = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                if editor.edits_text(event):
                    return False
                row, column = editor.row, editor.column
                self.commitData.emit(editor)
                self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
                backward = key == Qt.Key.Key_Backtab or shifted
                self.card.advance(row, column, backward=backward, vertical=vertical)
                return True
            if key == Qt.Key.Key_Escape:
                session = self.card.model.session
                original = editor.original
                row, column = editor.row, editor.column
                if session.continues_run(editor.token):
                    session.document.undo()
                    session.highlighter.refresh()
                if (self.card.model.alive
                        and self.card.model.rows[row][column] != encode_cell(original)):
                    self.card.model.set_cell(row, column, original)
                self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.RevertModelCache)
                self.card.view.setFocus()
                return True
        return super().eventFilter(editor, event)


class MarkdownTableView(QTableView):
    def __init__(self, card):
        super().__init__(card)
        self.card = card
        self._wheel_remainder = [0.0, 0.0]
        self._hover_point = None
        self._preview_hits = []
        self._followed_preview = False
        self._follow_state = None
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._follow_hint = FollowHint(self.viewport())
        self.setSelectionMode(QAbstractItemView.SelectionMode.ContiguousSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.setTabKeyNavigation(False)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setMinimumSectionSize(72)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.horizontalHeader().hide()
        self.verticalHeader().hide()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setShowGrid(False)
        self.setCornerButtonEnabled(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setAccessibleName("Markdown table grid")
        self._prepare_timer = IdleTask(self)
        self._prepare_scan = None
        self._prepare_timer.timeout.connect(self._prepare_chunk)
        self._refresh_clock = QTimer(self)
        self._refresh_clock.setInterval(1000)
        self._refresh_clock.timeout.connect(self.prepare_visible)

    def queue_preparation(self, *, restart=True):
        if restart:
            self._prepare_scan = None
        if self.isVisible() and not self._prepare_timer.isActive():
            self._prepare_timer.start(0)

    def prepare_visible(self):
        if self.card.model.session.layout.progressive:
            self.queue_preparation()
        else:
            self._prepare_scan = None
            self._prepare_chunk(budgeted=False)

    def _visible_indices(self):
        model = self.model()
        for row in range(max(0, self.rowAt(0)), model.rowCount()):
            if self.rowViewportPosition(row) >= self.viewport().height():
                break
            for column in range(max(0, self.columnAt(0)), model.columnCount()):
                if self.columnViewportPosition(column) >= self.viewport().width():
                    break
                yield model.index(row, column)

    def _prepare_chunk(self, *, budgeted=True):
        if not self.isVisible() or not hasattr(self.card, 'delegate'):
            return
        model = self.model()
        if model is None or not model.alive:
            return
        if self._prepare_scan is None:
            self._prepare_scan = iter(self._visible_indices())
        deadline = perf_counter() + .002 if budgeted else float('inf')
        while self._prepare_scan is not None:
            try:
                index = next(self._prepare_scan)
            except StopIteration:
                self._prepare_scan = None
                break
            self.card.delegate.prepare(index)
            if perf_counter() >= deadline:
                self._prepare_timer.start()
                break
        self.viewport().update()

    def showEvent(self, event):
        super().showEvent(event)
        self.prepare_visible()
        self._refresh_clock.start()

    def hideEvent(self, event):
        self._prepare_timer.stop()
        self._refresh_clock.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_prepare_timer'):
            self.queue_preparation()

    def paintEvent(self, event):
        self._preview_hits = []
        painter = QPainter(self.viewport())
        painter.setClipRegion(event.region())
        theme = self.card.theme
        painter.fillRect(event.rect(), theme.surface)
        model = self.model()
        if model is None or not model.alive:
            self._follow_hint.hide()
            painter.end()
            return
        selection = self.selectionModel()
        first_row = max(0, self.rowAt(0))
        first_column = max(0, self.columnAt(0))
        for row in range(first_row, model.rowCount()):
            if self.rowViewportPosition(row) >= self.viewport().height():
                break
            for column in range(first_column, model.columnCount()):
                if self.columnViewportPosition(column) >= self.viewport().width():
                    break
                index = model.index(row, column)
                option = QStyleOptionViewItem()
                option.initFrom(self)
                option.rect = self.visualRect(index)
                option.font = self.font()
                option.palette = theme.content_palette
                if selection.isSelected(index):
                    option.state |= QStyle.StateFlag.State_Selected
                else:
                    option.state &= ~QStyle.StateFlag.State_Selected
                self.card.delegate.paint(painter, option, index)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(theme.accent, 1.5))
        for area in selection.selection():
            bounds = QRectF(self.visualRect(area.topLeft())).united(
                QRectF(self.visualRect(area.bottomRight()))
            ).adjusted(0.75, 0.75, -0.75, -0.75)
            painter.drawRect(bounds)
        hit = self.preview_at(self._hover_point)
        cell = self.card.cell_editor
        if cell is not None and self._hover_point is not None:
            if cell.geometry().contains(self._hover_point.toPoint()):
                hit = None
        painter.end()
        if hit is not None:
            code_rect, code = hit
            self._follow_hint.set_preview(code_rect, code.theme)
        else:
            self._follow_hint.set_preview()

    def preview_at(self, point):
        if point is not None:
            for rectangle, visible, code in self._preview_hits:
                if visible.contains(point):
                    return rectangle, code
        return None

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self.queue_preparation()
        self.viewport().update()
        cell = self.card.cell_editor
        if cell is not None and isValid(cell):
            cell.viewport().update()

    def mouseMoveEvent(self, event):
        self._hover_point = event.position()
        self.viewport().update()
        if not self._followed_preview:
            super().mouseMoveEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Leave:
            self._hover_point = None
            if hasattr(self, '_follow_hint'):
                self._follow_hint.hide()
            self.viewport().update()
        return super().viewportEvent(event)

    def _follow_preview(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        ):
            # Resolve current object geometry after any queued resize/scroll.
            self.viewport().repaint()
            hit = self.preview_at(event.position())
            if hit is not None:
                self._followed_preview = True
                self._follow_state = NavigationState(self)
                self.card.follow_link(hit[1].preview.address)
                self._follow_state.restore()
                event.accept()
                return True
        return False

    def mousePressEvent(self, event):
        self._followed_preview = False
        if self._follow_preview(event):
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._follow_preview(event):
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        if self._followed_preview and event.button() == Qt.MouseButton.LeftButton:
            self._followed_preview = False
            self._follow_state.restore()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.card.activate()

    def event(self, event):
        if event.type() == QEvent.Type.ShortcutOverride and any(event.matches(key) for key in (
            QKeySequence.StandardKey.Copy, QKeySequence.StandardKey.Cut,
            QKeySequence.StandardKey.Paste,
            QKeySequence.StandardKey.SelectAll,
            QKeySequence.StandardKey.Undo, QKeySequence.StandardKey.Redo,
            QKeySequence.StandardKey.Bold, QKeySequence.StandardKey.Italic,
        )):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Menu or (
            event.key() == Qt.Key.Key_F10 and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            rectangle = self.visualRect(self.currentIndex())
            self.card.show_menu(self.viewport().mapToGlobal(rectangle.bottomLeft()))
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.selectAll()
            return
        actions = (
            (QKeySequence.StandardKey.Copy, self.card.copy_selection),
            (QKeySequence.StandardKey.Cut, self.card.cut_selection),
            (QKeySequence.StandardKey.Paste, self.card.paste),
            (QKeySequence.StandardKey.Undo, self.card.undo),
            (QKeySequence.StandardKey.Redo, self.card.redo),
            (QKeySequence.StandardKey.Bold, lambda: self.card.wrap_selection("**")),
            (QKeySequence.StandardKey.Italic, lambda: self.card.wrap_selection("*")),
        )
        for key, callback in actions:
            if event.matches(key):
                callback()
                return
        if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            index = self.currentIndex()
            self.card.advance(
                index.row(), index.column(), backward=(event.key() == Qt.Key.Key_Backtab
                    or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            )
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.card.model.change_cells(self.card.selected_cells(), lambda _: "")
        elif event.key() == Qt.Key.Key_Escape:
            self.card.leave_table(after=True)
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.edit(self.currentIndex())
        elif self._leaves_table(event):
            self.card.leave_table(after=event.key() == Qt.Key.Key_Down)
        else:
            super().keyPressEvent(event)
            if event.key() in _NAVIGATION_KEYS:
                # At an edge, don't let the key reach the note editor underneath.
                event.accept()

    def _leaves_table(self, event):
        """Up from the first row or Down from the last leaves the table, as in prose."""
        if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            return False
        row = self.currentIndex().row()
        return (event.key() == Qt.Key.Key_Up and row <= 0
                or event.key() == Qt.Key.Key_Down and row >= self.card.model.rowCount() - 1)

    def contextMenuEvent(self, event):
        index = self.indexAt(event.pos())
        if index.isValid() and not self.selectionModel().isSelected(index):
            self.setCurrentIndex(index)
        self.card.show_menu(event.globalPos())

    def wheelEvent(self, event):
        horizontal = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        horizontal = horizontal or abs(event.angleDelta().x()) > abs(event.angleDelta().y())
        bar = self.horizontalScrollBar() if horizontal else self.verticalScrollBar()
        delta = (
            event.angleDelta().x() if horizontal and event.angleDelta().x()
            else event.angleDelta().y()
        )
        pixels = event.pixelDelta()
        if not pixels.isNull():
            amount = pixels.x() if horizontal and pixels.x() else pixels.y()
        else:
            amount = delta / 120 * max(1, bar.singleStep()) * QApplication.wheelScrollLines()
        if (bar.maximum() == 0 or amount > 0 and bar.value() == bar.minimum()
                or amount < 0 and bar.value() == bar.maximum()):
            event.ignore()  # Let the surrounding note scroll at the grid's edges.
            return
        axis = 0 if horizontal else 1
        movement = self._wheel_remainder[axis] - amount
        whole = math.trunc(movement)
        self._wheel_remainder[axis] = movement - whole
        bar.setValue(bar.value() + whole)
        event.accept()


class TableCard(QFrame):
    activated = Signal(object)
    leaveRequested = Signal(object, bool)

    def __init__(self, model, parent, *, preview_provider=None):
        super().__init__(parent)
        self.model = model
        self.preview_provider = preview_provider
        self.cell_editor = None
        self.source_selected = False
        self._sizing = False
        self._reset_cell = (0, 0)
        self._theme_key = None
        self._theme = None
        self.setAutoFillBackground(False)
        self.setBackgroundRole(QPalette.ColorRole.Base)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setAccessibleName("Markdown table with hover row and column controls")
        self.view = MarkdownTableView(self)
        self.view.setMinimumSize(0, 0)
        self.view.setFont(model.session.document.defaultFont())
        self.view.setModel(model)
        self.delegate = MarkdownCellDelegate(self)
        self.view.setItemDelegate(self.delegate)
        self.chrome = TableChrome(self)
        self.view.selectionModel().selectionChanged.connect(self.chrome.refresh)
        self.view.selectionModel().currentChanged.connect(self.chrome.refresh)
        self.view.horizontalHeader().sectionResized.connect(self._column_resized)
        model.presentationChanged.connect(self.refresh_geometry)
        model.modelAboutToBeReset.connect(self._before_reset)
        model.modelReset.connect(self._after_reset)
        model.registry.geometryChanged.connect(self.refresh_geometry)
        model.session.fontChanged.connect(self._font_changed)
        self.refresh_geometry()
        # Only a focused table shows a current cell.
        watch_focus(self, self._focus_changed)

    def has_focus_within(self):
        focus = QApplication.focusWidget()
        return focus is not None and (focus is self or self.isAncestorOf(focus))

    def _focus_changed(self):
        if self.has_focus_within() or QApplication.activePopupWidget() is not None:
            return  # Inside the table, or its own context menu acting on the selection.
        selection = self.view.selectionModel()
        if selection.hasSelection() or selection.currentIndex().isValid():
            selection.clear()
            self.chrome.refresh()
            self.view.viewport().update()

    def _font_changed(self):
        self.view.setFont(self.model.session.document.defaultFont())
        self.delegate._documents.clear()
        if self.cell_editor is not None:
            self.cell_editor.refresh_font()
        self.refresh_geometry()

    def set_link_provider(self, provider):
        self.preview_provider = provider
        self.delegate._documents.clear()
        if self.cell_editor is not None:
            self.cell_editor._links.bind(self.cell_editor._highlighter, provider,
                                         preferences=self.model.session.notebook.preferences)
        self.refresh_geometry()

    def link_previews(self, source, *, load):
        if self.preview_provider is not None and isValid(self.preview_provider):
            return self.preview_provider.for_cell(source, load=load)
        return ()

    def follow_link(self, address):
        editor = self.parentWidget().parentWidget()
        signal = getattr(editor, 'addressActivated', None)
        if signal is not None:
            signal.emit(address)

    def activate(self):
        self.activated.emit(self)

    @property
    def source_selected(self):
        return self._source_selected

    @source_selected.setter
    def source_selected(self, selected):
        self._source_selected = selected
        if hasattr(self, "chrome"):
            self.chrome.update()

    @property
    def theme(self):
        key = self.palette().cacheKey()
        if self._theme is None or self._theme_key != key:
            self._theme = TableTheme(self.palette())
            self._theme_key = key
        return self._theme

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_surface()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_geometry()
        self.model.set_metrics_active(id(self), True)

    def hideEvent(self, event):
        self.model.set_metrics_active(id(self), False)
        super().hideEvent(event)

    def _layout_surface(self):
        if not hasattr(self, "chrome"):
            return
        self.view.setGeometry(LEFT_RAIL, TOP_RAIL, max(1, self.width() - LEFT_RAIL - SIDE_RAIL),
                              max(1, self.height() - TOP_RAIL - BOTTOM_RAIL))
        self.chrome.setGeometry(self.rect())
        self.chrome.raise_()
        self.chrome.refresh()

    def _before_reset(self):
        self._reset_cell = self.current_cell() if self.view.currentIndex().isValid() else None
        self.chrome.pressed = None
        if self.chrome.drag is not None:
            self.chrome.drag[4].releaseMouse()
            self.chrome.drag = None

    def _after_reset(self):
        self.delegate._prepared.clear()
        if self._reset_cell is None:
            return  # An unfocused table keeps no current cell across edits.
        row, column = self._reset_cell
        self.view.setCurrentIndex(self.model.index(
            min(row, self.model.rowCount() - 1), min(column, self.model.columnCount() - 1)
        ))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), self.theme.base)
        painter.end()

    def mouseMoveEvent(self, event):
        self.chrome.handle_mouse(event, event.position(), self)

    def mousePressEvent(self, event):
        if self.chrome.handle_mouse(event, event.position(), self):
            return
        if (event.button() == Qt.MouseButton.LeftButton and not event.modifiers()
                and event.position().y() > self.chrome.grid_rect().bottom()):
            # The margin below the grid is part of the note's text area.
            self.leave_table(after=True)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if not self.chrome.handle_mouse(event, event.position(), self):
            super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self.chrome.track(QPointF(self.mapFromGlobal(QCursor.pos())))
        super().leaveEvent(event)

    def contextMenuEvent(self, event):
        hit = self.chrome.hit_test(QPointF(event.pos()))
        if hit is not None and hit.kind in ("row_menu", "column_menu"):
            self.chrome._invoke(hit)
        else:
            self.show_menu(event.globalPos(), scope="table")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
                            QEvent.Type.FontChange) and hasattr(self, "chrome"):
            self._theme_key = None
            self.delegate._documents.clear()
            self.delegate._prepared.clear()
            self.view.queue_preparation()
            if self.cell_editor is not None:
                self.cell_editor.setPalette(self.theme.content_palette)
                self.cell_editor._highlighter.set_palette(self.theme.content_palette)
            self.view.viewport().update()
            self.chrome.update()

    def refresh_geometry(self):
        if not self.model.alive or not self.isVisible():
            return
        self._sizing = True
        for col, width in enumerate(self.model.widths):
            self.view.setColumnWidth(col, width)
        for row, height in enumerate(self.model.row_heights()):
            self.view.setRowHeight(row, height)
        self._sizing = False
        self._layout_surface()
        self.view.prepare_visible()
        self.view.viewport().update()

    def fit_column(self, column):
        width = 72
        for row, values in enumerate(self.model.rows):
            document = cell_document(values[column], self.view.font(), 600, header=row == 0,
                                     link_previews=self.link_previews(values[column], load=True),
                                     show_addresses=self.model.show_hlil_addresses,
                                      widget=self.view)
            document.setParent(self)
            width = max(width, math.ceil(document.idealWidth()) + CELL_PADDING_X * 2)
            document.deleteLater()
        self.model.set_column_width(column, min(480, width))

    def _column_resized(self, column, _old, width):
        if not self._sizing and column < self.model.columnCount():
            self.model.set_column_width(column, width)

    def selected_cells(self):
        indexes = self.view.selectionModel().selectedIndexes()
        if not indexes:
            indexes = [self.view.currentIndex()]
        return [(index.row(), index.column()) for index in indexes if index.isValid()]

    def current_cell(self):
        index = self.view.currentIndex()
        return (index.row(), index.column()) if index.isValid() else (0, 0)

    def focus_cell(self, row=0, column=0, *, edit=False):
        row = max(0, min(row, self.model.rowCount() - 1))
        column = max(0, min(column, self.model.columnCount() - 1))
        index = self.model.index(row, column)
        self.view.setCurrentIndex(index)
        self.view.scrollTo(index)
        self.view.setFocus()
        if edit:
            self.view.edit(index)

    def advance(self, row, column, *, backward=False, vertical=False):
        if vertical:
            row += -1 if backward else 1
        else:
            position = row * self.model.columnCount() + column + (-1 if backward else 1)
            row, column = divmod(position, self.model.columnCount())
        if row < 0:
            self.leave_table(after=False)
            return
        if row >= self.model.rowCount():
            if vertical:
                self.leave_table(after=True)
                return
            self.model.insert_row(self.model.rowCount())
        self.focus_cell(row, column, edit=True)

    def append_row(self):
        self.focus_cell(self.model.insert_row(self.model.rowCount()), 0, edit=True)

    def append_column(self):
        self.focus_cell(0, self.model.insert_column(self.model.columnCount()), edit=True)

    def wrap_selection(self, delimiter):
        if self.cell_editor is not None:
            self.cell_editor.wrap_selection(delimiter)
            return
        self.model.change_cells(
            self.selected_cells(), lambda value: toggle_inline_text(value, delimiter),
        )
        self.view.setFocus()

    def copy_selection(self):
        coordinates = self.selected_cells()
        if not coordinates:
            return
        top, bottom = min(row for row, _ in coordinates), max(row for row, _ in coordinates)
        left, right = min(col for _, col in coordinates), max(col for _, col in coordinates)
        if top == bottom and left == right:
            text = self.model.rows[top][left]
        else:
            text = serialize_table([row[left:right + 1] for row in self.model.rows[top:bottom + 1]],
                                   self.model.alignments[left:right + 1])
        QApplication.clipboard().setText(text)

    def copy_table(self):
        QApplication.clipboard().setText(self.model.markdown())

    def cut_selection(self):
        self.copy_selection()
        self.model.change_cells(self.selected_cells(), lambda _: "")

    def paste(self):
        matrix, alignments = clipboard_matrix(QApplication.clipboard().text())
        row, column = self.current_cell()
        self.model.paste_matrix(row, column, matrix, alignments)

    def undo(self):
        self.model.session.document.undo()
        self.model.session.highlighter.refresh()

    def redo(self):
        self.model.session.document.redo()
        self.model.session.highlighter.refresh()

    def leave_table(self, *, after):
        self.leaveRequested.emit(self, after)

    def delete_table(self):
        self.model.delete_table()

    def edit_markdown(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit table Markdown")
        dialog.resize(700, 400)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(dialog)
        text.setPlainText(self.model.source_markdown())
        layout.addWidget(text)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.model.session.apply_source_edit(
                self.model.start, self.model.end, text.toPlainText()
            )
            self.model.session.highlighter.refresh()

    def _populate_table_menu(self, menu, scope):
        row, column = self.current_cell()
        if scope in ("row", "cell"):
            menu.addSection("Header row" if row == 0 else f"Row {row}")
            menu.addAction("Insert row above", lambda: self.model.insert_row(row))
            menu.addAction("Insert row below", lambda: self.model.insert_row(row + 1))
            menu.addAction("Delete row", lambda: self.model.delete_row(row))
        if scope in ("column", "cell"):
            menu.addSection(f"Column {column + 1}")
            menu.addAction("Insert column left", lambda: self.model.insert_column(column))
            menu.addAction("Insert column right", lambda: self.model.insert_column(column + 1))
            remove_column = menu.addAction(
                "Delete column", lambda: self.model.delete_column(column)
            )
            remove_column.setEnabled(self.model.columnCount() > 1)
            alignment = menu.addMenu("Alignment")
            for label, value in (("Left", "left"), ("Center", "center"), ("Right", "right")):
                action = alignment.addAction(
                    label,
                    lambda _checked=False, value=value: self.model.set_alignment([column], value),
                )
                action.setCheckable(True)
                action.setChecked((self.model.alignments[column] or "left") == value)
        if scope == "table":
            menu.addSection("Table")
            menu.addAction("Append row", self.append_row)
            menu.addAction("Append column", self.append_column)
        menu.addSeparator()
        menu.addAction("Copy table as Markdown", self.copy_table)
        menu.addAction("Copy selected cells", self.copy_selection)
        menu.addAction("Paste Markdown / spreadsheet cells", self.paste)
        menu.addAction("Edit Markdown…", self.edit_markdown)
        menu.addSeparator()
        menu.addAction("Write above table", lambda: self.leave_table(after=False))
        menu.addSeparator()
        menu.addAction("Delete table", self.delete_table)

    def create_context_menu(self, *, scope="cell"):
        menu = NoteContextMenu(self)
        if scope == "cell":
            build_edit_menu(GridActions(self), menu)
            menu.addSeparator()
            self._populate_table_menu(menu.addMenu("Table"), scope)
        else:
            self._populate_table_menu(menu, scope)
        return menu

    def show_menu(self, position, *, scope="cell"):
        self.activate()
        menu = self.create_context_menu(scope=scope)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.exec(position)
