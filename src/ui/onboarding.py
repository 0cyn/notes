"""The welcome tour a notebook shows instead of its notes the first time a Notepad opens.

Each step shows its feature, drawn with the editor's own painting, and says as little as
possible. Code samples come from a small function in the open file when its analysis is
ready, and from built-in sample code until then (or without a file).
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass
from html import escape

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..export import LANGUAGES, code_lines, fenced
from ..links import (
    INDENT_COLUMNS,
    MENU_GROUPS,
    HLILLine,
    HLILSourceLine,
    HLILToken,
    ILMode,
    il_mode_available,
    il_snapshot_at,
)
from ..markdown import _code_tokens
from ..settings import SCHEMA, Scope, setting_choices, setting_description, setting_title
from ..storage import enterprise_user, person_name
from .common import AppExecutor, app_singleton, mix, opacity
from .editor import (
    AGENT_HUE,
    CLAUDE_HUE,
    OPENAI_HUE,
    PEOPLE_HUES,
    AuthorBar,
    AuthorTag,
    agent_hue,
    author_color,
    paint_author_bar,
)
from .embeds import (
    CodePreviewLayout,
    _paint_box,
    follow_modifier,
    format_range,
    hint_rectangle,
    preview_theme,
    token_color,
)
from .highlighter import SYNTAX_COLORS, code_block_colors
from .home import _true, wordmark_font
from .preferences import editor_preferences
from .search import monospace_font

ONBOARDED_KEY = 'onboarding/done'
COLUMN_WIDTH = 640


def onboarding_due(store):
    """Whether no Notepad has shown the tour yet (an application-wide Qt setting)."""
    return not _true(store.app_value(ONBOARDED_KEY, False))


def mark_onboarded(store):
    store.set_app_value(ONBOARDED_KEY, True)


# Samples

# `{kind:text}` is a token of that InstructionTextTokenType, so samples take the theme's colors.
_TOKEN = re.compile(r'\{(\d+):([^}]*)\}')
FUNCTION = 0x100003ef0


def _tokens(spec):
    tokens, position = [], 0
    for match in _TOKEN.finditer(spec):
        if match.start() > position:
            tokens.append(HLILToken(spec[position:match.start()]))
        tokens.append(HLILToken(match.group(2), int(match.group(1))))
        position = match.end()
    if position < len(spec):
        tokens.append(HLILToken(spec[position:]))
    return tuple(tokens)


def _rows(*rows):
    """(offset into the function, nesting depth, token spec) rows, as embed source lines."""
    return tuple(HLILSourceLine(FUNCTION + offset, _tokens(spec),
                                indent=INDENT_COLUMNS * (depth + 1))
                 for offset, depth, spec in rows)


@dataclass(frozen=True)
class Samples:
    """A function's code for the tour: a statement, the whole function, and the disassembly
    behind that statement."""
    name: str
    offset: int          # The statement's offset into the function.
    statement: HLILLine
    function: HLILLine
    disassembly: HLILLine
    symbols: tuple = ()  # Functions around it, as the Symbols list names them.
    symbol: int = 0      # Its own place among them.

    @property
    def expression(self):
        return f'{self.name}+{self.offset:#x}' if self.offset else self.name


_LENGTH = '{20:int32_t} {66:length} = {64:read_length}({11:header}->{21:size}, {4:0x200})'
BUILT_IN = Samples(
    'parse_header', 0x34,
    HLILLine(FUNCTION + 0x34, '', 'parse_header', FUNCTION + 0x34, True, _tokens(_LENGTH),
             function_start=FUNCTION),
    HLILLine(FUNCTION, '', 'parse_header', available=True, function_start=FUNCTION,
             expand_block=True,
             function_signature=(HLILSourceLine(FUNCTION, _tokens(
                 '{20:int64_t} {64:parse_header}({20:struct header*} {11:header})'), indent=0),),
             lines=_rows(
                 (0x14, 0, '{19:if} ({11:header} == {4:0})'),
                 (0x60, 1, '{19:return} {4:-1}'),
                 (0x34, 0, _LENGTH),
                 (0x40, 0, '{19:if} ({66:length} u> {4:0x200})'),
                 (0x54, 1, '{19:return} {4:-2}'),
                 (0x58, 0, '{19:return} {64:decode_records}({11:header}, {66:length})'),
             )),
    HLILLine(FUNCTION + 0x34, '', 'parse_header', available=True, function_start=FUNCTION,
             expand_block=True, il_mode=ILMode.DISASM,
             lines=_rows(
                 (0x34, 0, '{1:ldr}     {3:w0}, [{3:x19}, {4:#0x8}]'),
                 (0x38, 0, '{1:mov}     {3:w1}, {4:#0x200}'),
                 (0x3c, 0, '{1:bl}      {64:read_length}'),
             )),
    ('decode_records(struct header*, int32_t)', 'log_error(char const*, ...)',
     'main(int32_t, char**)', 'parse_header(struct header*)',
     'read_length(uint32_t, uint32_t)', 'read_records(struct header*)',
     'validate(struct header*)'),
    3,
)


def _sample_function(data, limit=4000):
    """A small, named function whose HLIL is ready (a few statements, a short body), as
    (rank, function, hlil, the functions searched, its index among them)."""
    best = None
    functions = list(itertools.islice(data.functions, limit))
    for position, function in enumerate(functions):
        if not 16 <= function.total_bytes <= 160:
            continue
        hlil = function.hlil_if_available
        if hlil is None:
            continue
        count = sum(1 for _ in itertools.islice(hlil.instructions, 12))
        if not 3 <= count <= 10:
            continue
        name = function.name
        rank = (name.startswith('sub_'), name.lstrip('_').startswith('Z'), -count,
                function.total_bytes)
        if best is None or rank < best[0]:
            best = rank, function, hlil, functions, position
    return best


def _symbol_label(function):
    """A function as the Symbols list shows it: its name and parameter types."""
    try:
        return f"{function.name}({', '.join(str(item.type) for item in function.type.parameters)})"
    except Exception:
        return function.name


def file_samples(data):
    """Samples from `data`'s own code, or None. Runs off the UI thread, like IL snapshots."""
    try:
        found = _sample_function(data)
        if found is None:
            return None
        _rank, function, hlil, functions, position = found
        statements = [instruction.address for instruction in hlil.instructions]
        address = next((item for item in statements if item != function.start), function.start)
        first = max(0, position - 3)
        samples = Samples(
            function.name, address - function.start,
            il_snapshot_at(data, address),
            il_snapshot_at(data, function.start, expand_block=True),
            il_snapshot_at(data, address, expand_block=True, il_mode=ILMode.DISASM),
            tuple(_symbol_label(item) for item in functions[first:position + 4]),
            position - first,
        )
    except Exception:
        return None
    usable = all(item.available for item in (samples.statement, samples.function,
                                             samples.disassembly))
    return samples if usable else None


def _executor():
    return app_singleton('_bnnotes_tour_executor',
                         lambda app: AppExecutor(app, name='bnnotes-tour'))


# The fix the welcome step's miniature note proposes, as a fenced C block.
FIX = 'if (header->size > 0x200)\n    return -EINVAL;'


# Drawing


def _label(html, parent, *, muted=False):
    label = QLabel(html, parent)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    if muted:
        label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


def _pixel_font(widget, pixels, *, bold=False):
    font = QFont(widget.font())
    font.setPixelSize(pixels)
    font.setBold(bold)
    return font


def _tint(widget, amount=0.035):
    palette = widget.palette()
    return mix(palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text),
               amount)


class Stage(QWidget):
    """The tinted area where a step shows its feature."""

    def __init__(self, parent=None, *, spacing=10):
        super().__init__(parent)
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(18, 18, 18, 18)
        self.content.setSpacing(spacing)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_tint(self))
        painter.drawRoundedRect(QRectF(self.rect()), 8, 8)
        painter.end()


class Arrow(QWidget):
    """A small chevron from one part of a scene to the next."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(14)

    def paintEvent(self, _event):
        palette = self.palette()
        pen = QPen(mix(palette.color(QPalette.ColorRole.Base),
                       palette.color(QPalette.ColorRole.Text), 0.4), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(pen)
        x, y = 24.0, self.height() / 2
        painter.drawPolyline([QPointF(x - 5, y - 3), QPointF(x, y + 2), QPointF(x + 5, y - 3)])
        painter.end()


MENU_PADDING, MENU_ROW, MENU_SEPARATOR = 4, 24, 7


def _menu_checks(items):
    return 20 if any({'checked', 'unchecked'} & set(kind.split()) for _text, kind in items) else 0


def menu_size(items, font):
    """The size `paint_menu` draws `items` at."""
    metrics = QFontMetricsF(font)
    width = max(metrics.horizontalAdvance(text) + (16 if 'submenu' in kind else 0)
                for text, kind in items)
    height = sum(MENU_SEPARATOR if kind == '-' else MENU_ROW for _text, kind in items)
    return QSize(math.ceil(width) + 34 + _menu_checks(items), height + MENU_PADDING * 2)


def paint_menu(painter, origin, items, palette, font):
    """A context menu as Notes draws one at `origin`: (text, kind) items, kind being '-' for a
    separator, else words from 'selected', 'checked', 'unchecked', 'title' and 'submenu' (a
    chevron at the right)."""
    size = menu_size(items, font)
    base, text = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text)
    background = mix(base, text, 0.075 if base.lightness() < 128 else 0.015)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setFont(font)
    painter.translate(origin)
    painter.setPen(QPen(mix(background, text, 0.22), 1))
    painter.setBrush(background)
    painter.drawRoundedRect(QRectF(0.5, 0.5, size.width() - 1, size.height() - 1), 8, 8)
    y, check = MENU_PADDING, _menu_checks(items)
    for label, kind in items:
        if kind == '-':
            painter.setPen(QPen(mix(background, text, 0.18), 1))
            middle = y + MENU_SEPARATOR / 2
            painter.drawLine(QPointF(10, middle), QPointF(size.width() - 10, middle))
            y += MENU_SEPARATOR
            continue
        row = QRectF(MENU_PADDING + 1, y, size.width() - MENU_PADDING * 2 - 2, MENU_ROW)
        words = set(kind.split())
        color = text
        if 'selected' in words:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.color(QPalette.ColorRole.Highlight))
            painter.drawRoundedRect(row, 4, 4)
            color = palette.color(QPalette.ColorRole.HighlightedText)
        elif 'title' in words:
            color = mix(background, text, 0.5)
        if 'submenu' in words:
            pen = QPen(color, 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            x, middle = row.right() - 12, row.center().y()
            painter.drawPolyline([QPointF(x - 2, middle - 3.5), QPointF(x + 1.5, middle),
                                  QPointF(x - 2, middle + 3.5)])
        if 'checked' in words:
            pen = QPen(color, 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            cx, cy = row.left() + 12, row.center().y()
            painter.drawPolyline([QPointF(cx - 4, cy), QPointF(cx - 1, cy + 3),
                                  QPointF(cx + 4, cy - 3.5)])
        painter.setPen(color)
        painter.drawText(row.adjusted(10 + check, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter, label)
        y += MENU_ROW
    painter.restore()


class MenuSample(QWidget):
    """A context menu as Notes draws one (see `paint_menu`)."""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.items = items
        self.setAccessibleName(' / '.join(text for text, kind in items if kind != '-'))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return menu_size(self.items, self.font())

    def paintEvent(self, _event):
        painter = QPainter(self)
        paint_menu(painter, QPointF(), self.items, self.palette(), self.font())
        painter.end()


def _pane_frame(painter, widget):
    palette = widget.palette()
    base, text = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text)
    painter.setPen(QPen(mix(base, text, 0.2), 1))
    painter.setBrush(base)
    painter.drawRoundedRect(QRectF(widget.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)


class SymbolsSample(QWidget):
    """Binary Ninja's Symbols list, a function selected and its context menu open at
    Copy BNURL to Function."""

    TITLE, COLUMN, ROW = 28, 20, 18
    MENU = (('Copy Name', ''), ('Copy Raw Name', ''), ('Copy BNURL to Function', 'selected'))

    def __init__(self, symbols, selected, parent=None):
        super().__init__(parent)
        self.setAccessibleName('Symbols, with Copy BNURL to Function')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.set_symbols(symbols, selected)

    def set_symbols(self, symbols, selected):
        self.symbols, self.selected = tuple(symbols), selected
        self.updateGeometry()
        self.update()

    def _menu_origin(self):
        row_bottom = self.TITLE + self.COLUMN + (self.selected + 1) * self.ROW
        width = menu_size(self.MENU, self.font()).width()
        return QPointF(max(4, min(36, self.width() - width - 4)), row_bottom - 4)

    def sizeHint(self):
        rows = self.TITLE + self.COLUMN + len(self.symbols) * self.ROW + 6
        menu = self._menu_origin().y() + menu_size(self.MENU, self.font()).height() + 8
        return QSize(260, math.ceil(max(rows, menu)))

    def minimumSizeHint(self):
        return QSize(200, self.sizeHint().height())

    def paintEvent(self, _event):
        palette = self.palette()
        base, text = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text)
        muted = mix(base, text, 0.5)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _pane_frame(painter, self)
        painter.setClipRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        bold = QFont(self.font())
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(text)
        title = QRectF(10, 0, 70, self.TITLE)
        painter.drawText(title, Qt.AlignmentFlag.AlignVCenter, 'Symbols')
        search = QRectF(QFontMetricsF(bold).horizontalAdvance('Symbols') + 20, 5,
                        self.width() - QFontMetricsF(bold).horizontalAdvance('Symbols') - 30,
                        self.TITLE - 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(mix(base, text, 0.07))
        painter.drawRoundedRect(search, 4, 4)
        painter.setFont(self.font())
        painter.setPen(muted)
        painter.drawText(search.adjusted(8, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter,
                         'Search symbols')
        painter.fillRect(QRectF(1, self.TITLE, self.width() - 2, self.COLUMN),
                         mix(base, text, 0.08))
        painter.setPen(text)
        painter.drawText(QRectF(10, self.TITLE, 100, self.COLUMN), Qt.AlignmentFlag.AlignVCenter,
                         'Name')
        painter.setFont(monospace_font(self.font()))
        for index, label in enumerate(self.symbols):
            row = QRectF(1, self.TITLE + self.COLUMN + index * self.ROW, self.width() - 2,
                         self.ROW)
            color = text
            if index == self.selected:
                painter.fillRect(row, palette.color(QPalette.ColorRole.Highlight))
                color = palette.color(QPalette.ColorRole.HighlightedText)
            painter.setPen(color)
            painter.drawText(row.adjusted(20, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, label)
        painter.setClipping(False)
        paint_menu(painter, self._menu_origin(), self.MENU, palette, self.font())
        painter.end()


class LinearSample(QWidget):
    """Binary Ninja's Linear view of a function, a statement selected and its context menu
    open at Copy BNURL to Address."""

    PADDING, TITLE = 8, 28
    MENU = (('Copy Address', ''), ('Copy BNURL to Address', 'selected'),
            ('Copy BNURL to Function', ''))
    LINES = 9  # At most, below the function header.

    def __init__(self, preview, address, parent=None):
        super().__init__(parent)
        self.setAccessibleName('Linear view, with Copy BNURL to Address')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.set_preview(preview, address)

    def set_preview(self, preview, address):
        self.preview, self.address = preview, address
        self.updateGeometry()
        self.update()

    def _rows(self):
        """(header, body) source lines, the body trimmed around the selected statement."""
        body = list(self.preview.lines)
        selected = next((index for index, line in enumerate(body)
                         if line.address == self.address and line.tokens), 0)
        start = max(0, min(selected - 2, len(body) - self.LINES))
        return self.preview.function_signature, body[start:start + self.LINES], selected - start

    def _line_height(self):
        return math.ceil(QFontMetricsF(monospace_font(self.font())).height()) + 2

    def _menu_origin(self):
        header, _body, selected = self._rows()
        line = self._line_height()
        top = self.TITLE + self.PADDING + len(header) * line + 6 + (selected + 1) * line
        width = menu_size(self.MENU, self.font()).width()
        return QPointF(max(4, min(90, self.width() - width - 4)), top - 2)

    def sizeHint(self):
        header, body, _selected = self._rows()
        line = self._line_height()
        rows = self.TITLE + self.PADDING * 2 + (len(header) + len(body)) * line + 6
        menu = self._menu_origin().y() + menu_size(self.MENU, self.font()).height() + 8
        return QSize(380, math.ceil(max(rows, menu)))

    def minimumSizeHint(self):
        return QSize(220, self.sizeHint().height())

    def paintEvent(self, _event):
        palette = self.palette()
        theme = preview_theme(self, kinds=self.preview.token_kinds | {68})
        colors = dict(theme.tokens)
        base = palette.color(QPalette.ColorRole.Base)
        font = monospace_font(self.font())
        metrics = QFontMetricsF(font)
        line = self._line_height()
        header, body, selected = self._rows()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _pane_frame(painter, self)
        painter.setClipRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        # The pane header's view and IL menus.
        text = palette.color(QPalette.ColorRole.Text)
        painter.fillRect(QRectF(1, 1, self.width() - 2, self.TITLE - 1), mix(base, text, 0.04))
        painter.setPen(QPen(mix(base, text, 0.15), 1))
        painter.drawLine(QPointF(1, self.TITLE), QPointF(self.width() - 1, self.TITLE))
        painter.setFont(self.font())
        painter.setPen(mix(base, text, 0.75))
        x = 10.0
        for label in ('Linear ▾', 'High Level IL ▾'):
            painter.drawText(QRectF(x, 0, 200, self.TITLE), Qt.AlignmentFlag.AlignVCenter, label)
            x += QFontMetricsF(self.font()).horizontalAdvance(label) + 14
        painter.setFont(font)
        address_width = metrics.horizontalAdvance('0' * 10)
        y = self.TITLE + self.PADDING
        if header:
            painter.fillRect(QRectF(1, y - 3, self.width() - 2, len(header) * line + 6),
                             theme.function_header)
        rows = [(item, False) for item in header] + [(None, None)]
        rows += [(item, index == selected) for index, item in enumerate(body)]
        for item, chosen in rows:
            if item is None:
                y += 6
                continue
            if chosen:
                painter.fillRect(QRectF(1, y, self.width() - 2, line),
                                 mix(base, palette.color(QPalette.ColorRole.Highlight), 0.3))
            if item.text.strip():  # Linear leaves blank lines without an address.
                painter.setPen(QColor.fromRgba(colors.get(68, theme.muted.rgba())))
                painter.drawText(QRectF(10, y, address_width, line),
                                 Qt.AlignmentFlag.AlignVCenter, f'{item.address:x}')
            x = 10 + address_width + 14 + metrics.horizontalAdvance(' ') * item.indent
            for token in item.tokens:
                painter.setPen(QColor.fromRgba(colors.get(token.kind, theme.text.rgba())))
                painter.drawText(QPointF(x, y + (line + metrics.ascent() - metrics.descent()) / 2),
                                 token.text)
                x += metrics.horizontalAdvance(token.text)
            y += line
        painter.setClipping(False)
        paint_menu(painter, self._menu_origin(), self.MENU, palette, self.font())
        painter.end()


class Panes(QWidget):
    """Panes side by side where there is room, else stacked."""

    def __init__(self, panes, parent=None, *, stretches=None):
        super().__init__(parent)
        self.panes = panes
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)
        for pane, stretch in zip(panes, stretches or (1,) * len(panes), strict=True):
            self._layout.addWidget(pane, stretch, Qt.AlignmentFlag.AlignTop)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange()

    def arrange(self):
        """Side by side, equally tall, where both fit with room to spare; else stacked."""
        wide = self.width() >= sum(pane.minimumSizeHint().width() for pane in self.panes) + 160
        direction = (QBoxLayout.Direction.LeftToRight if wide
                     else QBoxLayout.Direction.TopToBottom)
        if direction != self._layout.direction():
            self._layout.setDirection(direction)
        tallest = max(pane.sizeHint().height() for pane in self.panes)
        for pane in self.panes:
            pane.setFixedHeight(tallest if wide else pane.sizeHint().height())
        self.updateGeometry()


class SourceSample(QWidget):
    """A line of note Markdown as it is typed, in a code-block frame."""

    PADDING_X, PADDING_Y = 10, 6

    def __init__(self, parts, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.set_parts(parts)

    def set_parts(self, parts):
        self.parts = parts  # (text, role): syntax, label, url, title or accent
        self.text = ''.join(text for text, _role in parts)
        self.setAccessibleName(self.text)
        self.updateGeometry()
        self.update()

    def _layout(self, width):
        palette = self.palette()
        base, text = palette.base().color(), palette.text().color()
        colors = {'syntax': mix(base, text, 0.5), 'label': text, 'url': palette.link().color(),
                  'title': token_color(self, 17, palette),
                  'accent': palette.color(QPalette.ColorRole.Highlight)}
        formats, offset = [], 0
        for part, role in self.parts:
            item = format_range(offset, len(part), colors[role])
            if role == 'accent':
                item.format.setFontWeight(QFont.Weight.Bold)
            formats.append(item)
            offset += len(part)
        layout = QTextLayout(self.text, monospace_font(self.font()))
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.setFormats(formats)
        layout.beginLayout()
        y = self.PADDING_Y
        while (line := layout.createLine()).isValid():
            line.setLineWidth(max(40, width - self.PADDING_X * 2))
            line.setPosition(QPointF(self.PADDING_X, y))
            y += line.height()
        layout.endLayout()
        return layout, math.ceil(y + self.PADDING_Y)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(width)[1]

    def sizeHint(self):
        return QSize(360, self.heightForWidth(360))

    def paintEvent(self, _event):
        background, border = code_block_colors(self.palette())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(border)
        painter.setBrush(background)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        self._layout(self.width())[0].draw(painter, QPointF())
        painter.end()


def highlighted(code, language):
    """Each line of `code` as (text, role) runs, role None for plain code."""
    tokens = _code_tokens(code, language)  # ASCII: byte and character offsets agree.
    lines, start = [], 0
    for line in code.split('\n'):
        end, runs, position = start + len(line), [], start
        for token_start, token_end, role in tokens:
            token_start, token_end = max(token_start, start), min(token_end, end)
            if token_start >= token_end:
                continue
            if token_start > position:
                runs.append((code[position:token_start], None))
            runs.append((code[token_start:token_end], role))
            position = token_end
        if position < end:
            runs.append((code[position:end], None))
        lines.append(runs)
        start = end + 1
    return lines


class CodeBlockSample(QWidget):
    """A fenced code block as a note shows it: shaded and outlined, its fences muted and its
    code syntax-highlighted."""

    PADDING = 8

    def __init__(self, language, code, parent=None):
        super().__init__(parent)
        self.language, self.code = language, code
        self.setAccessibleName(f'{language} code block')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def lines(self):
        return [f'```{self.language}', *self.code.split('\n'), '```']

    def runs(self):
        """Each line as (text, role) runs; role is None for plain code, 'fence' for fences."""
        return [[(self.lines()[0], 'fence')], *highlighted(self.code, self.language),
                [('```', 'fence')]]

    def _layouts(self, width):
        """Each line laid out, wrapping at `width` as a narrow note's code block does, and
        the height they take."""
        palette = self.palette()
        base, text = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text)
        dark = base.lightness() < 128
        colors = {role: QColor(night if dark else light) for role, light, night in SYNTAX_COLORS}
        colors['fence'] = colors['syntax_comment'] = mix(base, text, 0.55)
        font = monospace_font(self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layouts, y = [], float(self.PADDING)
        for runs in self.runs():
            layout = QTextLayout(''.join(chunk for chunk, _role in runs), font)
            layout.setTextOption(option)
            formats, offset = [], 0
            for chunk, role in runs:
                formats.append(format_range(offset, len(chunk), colors.get(role, text)))
                offset += len(chunk)
            layout.setFormats(formats)
            layout.beginLayout()
            while (line := layout.createLine()).isValid():
                line.setLineWidth(max(40, width - self.PADDING * 2))
                line.setPosition(QPointF(self.PADDING, y))
                y += line.height()
            layout.endLayout()
            layouts.append(layout)
        return layouts, math.ceil(y + self.PADDING)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layouts(width)[1]

    def sizeHint(self):
        metrics = QFontMetricsF(monospace_font(self.font()))
        width = math.ceil(max(metrics.horizontalAdvance(line) for line in self.lines()))
        width += self.PADDING * 2
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self):
        return QSize(120, self.heightForWidth(120))

    def paintEvent(self, _event):
        background, border = code_block_colors(self.palette())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, 1))
        painter.setBrush(background)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        for layout in self._layouts(self.width())[0]:
            layout.draw(painter, QPointF())
        painter.end()


class ExportSample(CodeBlockSample):
    """Exported Markdown as the file holds it: the embed's label, then its code in a fenced
    block, the fences muted and the code highlighted."""

    def __init__(self, parent=None):
        super().__init__('', '', parent)
        self.label, self.block = (), ['```', '```']

    def set_export(self, preview, label, *, show_addresses):
        self.language = LANGUAGES.get(preview.il_mode, '')
        self.block = fenced(code_lines(preview, show_addresses=show_addresses), self.language)
        self.code = '\n'.join(self.block[1:-1])
        self.label = (label,) if label else ()
        self.setAccessibleName('\n'.join(self.lines()))
        self.updateGeometry()
        self.update()

    def lines(self):
        return [*self.label, *self.block]

    def runs(self):
        return [*([(line, None)] for line in self.label), [(self.block[0], 'fence')],
                *highlighted(self.code, self.language), [(self.block[-1], 'fence')]]


def link_source(label, expression, title=None):
    """The parts of `[label](binaryninja://?expr=expression "title")` for a SourceSample;
    a leading `!` is accented."""
    parts = [('[', 'syntax')]
    if label.startswith('!'):
        parts.append(('!', 'accent'))
        label = label[1:]
    if label:
        parts.append((label, 'label'))
    parts += [('](', 'syntax'), (f'binaryninja://?expr={expression}', 'url')]
    if title:
        parts.append((f' "{title}"', 'title'))
    parts.append((')', 'syntax'))
    return parts


class EmbedSample(QWidget):
    """An in-BV link embed drawn as a note draws one, showing the follow shortcut while
    hovered, as a note's embeds do; with `hint`, always."""

    def __init__(self, preview, label, parent=None, *, hint=False):
        super().__init__(parent)
        self.label, self.hint = label, hint
        self.show_addresses = True
        self.hovered = hint
        self._code, self._key = None, None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.set_preview(preview)

    def set_preview(self, preview, label=None):
        self.preview = preview
        if label is not None:
            self.label = label
        self._key = None
        self.setAccessibleName(f'Embedded code: {self.label}' if self.label else 'Embedded code')
        self.updateGeometry()
        self.update()

    def set_show_addresses(self, shown):
        if shown != self.show_addresses:
            self.show_addresses = shown
            self.updateGeometry()
            self.update()

    def code(self, width=None):
        width = self.width() if width is None else width
        font = monospace_font(self.font())
        theme = preview_theme(self, kinds=self.preview.token_kinds | {68})
        key = width, self.show_addresses, font.key(), theme.key
        if key != self._key:
            self._code = CodePreviewLayout(self.preview, font, max(120, width), theme,
                                           show_addresses=self.show_addresses, label=self.label,
                                           label_font=QFont(self.font()))
            self._key = key
        return self._code

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.code(width).height + 12  # Room for the shortcut hint below.

    def sizeHint(self):
        return QSize(480, self.heightForWidth(480))

    def enterEvent(self, event):
        self.hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered = self.hint
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        code = self.code()
        code.draw(painter, QPointF(), hovered=self.hovered)
        if self.hovered:
            bounds = QRectF(0, 0, code.width, code.height)
            _paint_box(painter, hint_rectangle(bounds, QRectF(self.rect())), code.theme)
        painter.end()


class EmbedMenuSample(QWidget):
    """An embed with its context menu open at Select IL mode, choosing Disassembly."""

    MENU = (('Embed whole AST', 'checked'), ('Select IL mode', 'selected submenu'),
            ('Set title', ''))
    CURRENT, CHOSEN = ILMode.HLIL, ILMode.DISASM

    def __init__(self, preview, parent=None):
        super().__init__(parent)
        self.setAccessibleName('Embed menu: Select IL mode, Disassembly')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.show_addresses = True
        self._code, self._key = None, None
        self.set_preview(preview)

    def set_preview(self, preview):
        self.preview, self._key = preview, None
        self.updateGeometry()
        self.update()

    def set_show_addresses(self, shown):
        if shown != self.show_addresses:
            self.show_addresses = shown
            self.updateGeometry()
            self.update()

    def submenu(self):
        """Select IL mode's items, grouped as the real menu groups them."""
        items = []
        for members in MENU_GROUPS:
            group = [(mode.label, 'selected' if mode == self.CHOSEN else
                      'checked' if mode == self.CURRENT else 'unchecked')
                     for mode in members if il_mode_available(mode)]
            if group:
                items += [('', '-')] * bool(items) + group
        return tuple(items)

    def code(self, width=None):
        width = self.width() if width is None else width
        font = monospace_font(self.font())
        theme = preview_theme(self, kinds=self.preview.token_kinds | {68})
        key = width, self.show_addresses, font.key(), theme.key
        if key != self._key:
            self._code = CodePreviewLayout(self.preview, font, max(120, width), theme,
                                           show_addresses=self.show_addresses)
            self._key = key
        return self._code

    def menus(self, width=None):
        """(origin, items) of the menu and its submenu: the menu opened over the code, the
        submenu beside its Select IL mode row, both inside `width`."""
        width = self.width() if width is None else width
        code, font = self.code(width), self.font()
        main, sub = menu_size(self.MENU, font), menu_size(self.submenu(), font)
        main_origin = QPointF(max(0, min(code.width * 0.3, width - main.width() - sub.width())),
                              code.height - 4)
        row = main_origin.y() + MENU_PADDING + MENU_ROW  # Select IL mode's row.
        sub_origin = QPointF(max(0, min(main_origin.x() + main.width() - 4,
                                        width - sub.width())), row - MENU_PADDING)
        return (main_origin, self.MENU), (sub_origin, self.submenu())

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        font = self.font()
        bottoms = [origin.y() + menu_size(items, font).height()
                   for origin, items in self.menus(width)]
        return math.ceil(max(self.code(width).height, *bottoms)) + 4

    def sizeHint(self):
        return QSize(480, self.heightForWidth(480))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.code().draw(painter, QPointF(), hovered=True)
        for origin, items in self.menus():
            paint_menu(painter, origin, items, self.palette(), self.font())
        painter.end()


class TableSample(QWidget):
    """A small rendered table: a tinted header row and restrained rules."""

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.rows = rows
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _widths(self):
        metrics = QFontMetricsF(self.font())
        bold = QFont(self.font())
        bold.setBold(True)
        header = QFontMetricsF(bold)
        return [math.ceil(max([header.horizontalAdvance(self.rows[0][column])]
                              + [metrics.horizontalAdvance(row[column])
                                 for row in self.rows[1:]])) + 24
                for column in range(len(self.rows[0]))]

    def _row_height(self):
        return math.ceil(QFontMetricsF(self.font()).height()) + 12

    def sizeHint(self):
        return QSize(sum(self._widths()) + 1, self._row_height() * len(self.rows) + 1)

    def paintEvent(self, _event):
        palette = self.palette()
        base, text = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text)
        widths, height = self._widths(), self._row_height()
        painter = QPainter(self)
        painter.fillRect(QRectF(0, 0, sum(widths), height * len(self.rows)), base)
        painter.fillRect(QRectF(0, 0, sum(widths), height), mix(base, text, 0.06))
        bold = QFont(self.font())
        bold.setBold(True)
        for index, row in enumerate(self.rows):
            painter.setFont(bold if index == 0 else self.font())
            painter.setPen(text)
            x = 0
            for column, cell in enumerate(row):
                painter.drawText(QRectF(x + 12, index * height, widths[column] - 12, height),
                                 Qt.AlignmentFlag.AlignVCenter, cell)
                x += widths[column]
        painter.setPen(QPen(mix(base, text, 0.2), 1))
        for index in range(len(self.rows) + 1):
            painter.drawLine(QPointF(0, index * height + 0.5),
                             QPointF(sum(widths), index * height + 0.5))
        x = 0.5
        for width in (0, *widths):
            x += width
            painter.drawLine(QPointF(x, 0), QPointF(x, height * len(self.rows)))
        painter.end()


# The code the author sample's first paragraph embeds, which its other paragraphs discuss.
AUTHORED_SAMPLE = HLILLine(
    FUNCTION + 0x2c, '', 'parse_header', available=True, function_start=FUNCTION,
    lines=_rows((0x2c, 0, '{64:memcpy}(&{66:buf}, {11:header}->{21:data}, {11:header}->{21:size})'),
                (0x34, 0, _LENGTH)),
)


class AuthorSample(QWidget):
    """Short paragraphs with their author bars: three people and three agents, alone and
    together (an agent's line you edited, a person's line an agent edited, and two people's),
    the first with the code they discuss embedded.

    Who wrote each of the first paragraphs slides out in turn, once, as hovering their bars in a
    note does, and the last stays out. Hovering a paragraph shows its names instead (and ends
    the sweep early), until the pointer leaves."""

    LEFT, BAR, TEXT, GAP = 2, 3, 16, 5
    EMBED_PADDING = 4
    SHARED = 3        # The paragraph the agent wrote and you edited.
    CYCLED = 4        # The paragraphs the names cycle through: the first four.
    SWEEP = 2000      # Milliseconds from the first to the last, eased in and out.

    def __init__(self, parent=None, *, person=None, agent='Claude'):
        super().__init__(parent)
        self.setAccessibleName('Author bar example')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        you = person or person_name(), PEOPLE_HUES[0]
        alice, bob = ('Alice', PEOPLE_HUES[1]), ('Bob', PEOPLE_HUES[2])
        claude = agent, agent_hue(agent)
        astra, qwen = ('Astra', agent_hue('Astra')), ('Qwen', agent_hue('Qwen'))
        self.paragraphs = (
            (('parse_header trusts header->size', (you,)), ('before checking it:', (you,)),
             (AUTHORED_SAMPLE, (you,))),
            (('Reproduced with a 0xffff size.', (alice,)),),
            (('Confirmed: read_length is unchecked.', (claude,)),),
            (('Fix: clamp size to 0x200 first.', (claude, you)),),
            (('The spec caps headers at 0x200 too.', (astra,)),),
            (('Patched in build 41; tests pass.', (bob, astra)),),
            (('Ship it after review.', (alice, bob)),),
            (('Qwen thought about this function for 7 minutes', (qwen,)),),
        )
        self.show_addresses = True
        self.bars_shown = True  # Follows Show author bar, as a note's gutter does.
        self._code, self._code_key = None, None
        self.tag = AuthorTag(self)
        self.current = 0      # The paragraph whose names are out.
        self.hovering = False
        self.played = False   # The sweep runs once; afterwards the last cycled names rest out.
        self.cycle = QVariantAnimation(self)
        self.cycle.setStartValue(0.0)
        self.cycle.setEndValue(float(self.SWEEP))
        self.cycle.setDuration(self.SWEEP)
        self.cycle.valueChanged.connect(self._cycled)
        self.cycle.finished.connect(lambda: setattr(self, 'played', True))
        self._ease = QEasingCurve(QEasingCurve.Type.InOutQuad)
        self.setMouseTracking(True)

    # Geometry

    def set_show_addresses(self, shown):
        if shown != self.show_addresses:
            self.show_addresses = shown
            self.updateGeometry()
            self.update()

    @property
    def text_left(self):
        """Where text starts: right of the bars, or at the edge without them."""
        return self.TEXT if self.bars_shown else 0

    def set_bars_shown(self, shown):
        """Show or hide the author bars (and their names), moving the text as a note does."""
        if shown == self.bars_shown:
            return
        self.bars_shown = shown
        if shown:
            if self.isVisible() and not self.hovering:
                self._shown()
        else:
            self.cycle.stop()
            self.tag.retract()
        self.updateGeometry()
        self.update()

    def code(self, width=None):
        """The embedded code's layout, at `width` (the widget's own by default)."""
        width = self.width() if width is None else width
        font = monospace_font(self.font())
        theme = preview_theme(self, kinds=AUTHORED_SAMPLE.token_kinds | {68})
        key = width, self.text_left, self.show_addresses, font.key(), theme.key
        if key != self._code_key:
            self._code = CodePreviewLayout(AUTHORED_SAMPLE, font, max(120, width - self.text_left),
                                           theme, show_addresses=self.show_addresses,
                                           label='parse_header', label_font=QFont(self.font()))
            self._code_key = key
        return self._code

    def _metrics(self):
        line = math.ceil(QFontMetricsF(self.font()).height()) + 2
        return line, round(line * 0.6)

    def _height(self, item, width):
        line, _gap = self._metrics()
        return line if isinstance(item, str) else (self.code(width).height
                                                  + self.EMBED_PADDING * 2)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        _line, gap = self._metrics()
        return round(sum(self._height(item, width) for paragraph in self.paragraphs
                         for item, _writers in paragraph)
                     + gap * (len(self.paragraphs) - 1))

    def sizeHint(self):
        return QSize(480, self.heightForWidth(480))

    def minimumSizeHint(self):
        # Its height follows its width; a narrow width's height would make the page scroll.
        return QSize(160, 0)

    def bars(self, width=None):
        """Each paragraph's AuthorBar, split line by line as the editor's gutter splits it."""
        width = self.width() if width is None else width
        _line, gap = self._metrics()
        bars, y = [], 0
        for paragraph in self.paragraphs:
            top, sections, writers = y, [], []
            for item, item_writers in paragraph:
                height = self._height(item, width)
                share = height / len(item_writers)
                for index, writer in enumerate(item_writers):
                    if writer not in writers:
                        writers.append(writer)
                    start, hue = y + share * index, writer[1]
                    if sections and sections[-1][2] == hue:
                        sections[-1] = (sections[-1][0], start + share, hue)
                    else:
                        sections.append((start, start + share, hue))
                y += height
            bars.append(AuthorBar(top, y, tuple(sections), tuple(writers)))
            y += gap
        return bars

    # Names

    def present(self, index=0):
        """Slide out paragraph `index`'s names, as hovering its bar does."""
        self.current = index
        if not self.bars_shown:
            return
        run = self.bars()[index]
        self.tag.present(run.writers, self.LEFT + self.BAR + self.GAP, run.top + 2)
        self.update()

    def cycle_index(self, milliseconds):
        """The paragraph shown `milliseconds` into the sweep: eased from the first to the last
        cycled one over SWEEP."""
        eased = self._ease.valueForProgress(min(1.0, milliseconds / self.SWEEP))
        return min(self.CYCLED - 1, int(eased * self.CYCLED))

    def _cycled(self, milliseconds):
        index = self.cycle_index(milliseconds)
        if not self.hovering and index != self.current:
            self.present(index)

    def start_cycle(self):
        """Sweep through the cycled paragraphs' names, unless that has already happened."""
        if self.played or not self.bars_shown:
            return
        self.present(0)
        self.cycle.stop()
        self.cycle.start()

    @property
    def resting(self):
        """The paragraph whose names show when nothing is hovered or sweeping."""
        return self.CYCLED - 1 if self.played else 0

    def paragraph_at(self, y):
        """The paragraph at `y`, counting half the gap either side; None outside them all."""
        _line, gap = self._metrics()
        return next((index for index, run in enumerate(self.bars())
                     if run.top - gap / 2 <= y <= run.bottom + gap / 2), None)

    def mouseMoveEvent(self, event):
        index = self.paragraph_at(event.position().y()) if self.bars_shown else None
        if index is not None:
            self.hovering = True
            if self.cycle.state() == self.cycle.State.Running:
                self.cycle.stop()
                self.played = True
            if index != self.current:
                self.present(index)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self.hovering:
            self.hovering = False
            self.present(self.resting)
        super().leaveEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(350, self, self._shown)

    def _shown(self):
        if self.isVisible() and not self.hovering:
            if self.played:
                self.present(self.resting)
            else:
                self.start_cycle()

    def hideEvent(self, event):
        self.cycle.stop()
        super().hideEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        palette = self.palette()
        for index, run in enumerate(self.bars() if self.bars_shown else ()):
            paint_author_bar(painter, self.LEFT, self.BAR, run,
                             lambda hue, strong=index == self.current: author_color(
                                 palette, hue, strong=strong))
        line, gap = self._metrics()
        metrics = self.fontMetrics()
        y = 0
        for paragraph in self.paragraphs:
            for item, _writers in paragraph:
                if isinstance(item, str):
                    painter.setPen(palette.color(QPalette.ColorRole.Text))
                    elided = metrics.elidedText(item, Qt.TextElideMode.ElideRight,
                                                self.width() - self.text_left)
                    painter.drawText(QRectF(self.text_left, y, self.width() - self.text_left,
                                            line),
                                     Qt.AlignmentFlag.AlignVCenter, elided)
                else:
                    self.code().draw(painter, QPointF(self.text_left, y + self.EMBED_PADDING))
                y += self._height(item, self.width())
            y += gap
        painter.end()


class Legend(QWidget):
    """Author colors, named: a short bar per color, and a word."""

    STEP = 6  # Between the bars of one entry.

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = (('People', PEOPLE_HUES), ('Agents', (CLAUDE_HUE, OPENAI_HUE, AGENT_HUE)))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _widths(self):
        metrics = QFontMetricsF(self.font())
        return [len(hues) * self.STEP + 3 + metrics.horizontalAdvance(text) + 24
                for text, hues in self.entries]

    def sizeHint(self):
        return QSize(math.ceil(sum(self._widths())),
                     math.ceil(QFontMetricsF(self.font()).height()) + 2)

    def paintEvent(self, _event):
        palette = self.palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = 0.0
        for (text, hues), width in zip(self.entries, self._widths(), strict=True):
            for index, hue in enumerate(hues):
                path = QPainterPath()
                path.addRoundedRect(QRectF(x + index * self.STEP, 2, 3, self.height() - 4),
                                    1.5, 1.5)
                painter.fillPath(path, author_color(palette, hue, strong=True))
            painter.setPen(palette.color(QPalette.ColorRole.Text))
            painter.drawText(QRectF(x + (len(hues) - 1) * self.STEP + 9, 0, 200, self.height()),
                             Qt.AlignmentFlag.AlignVCenter, text)
            x += width
        painter.end()


class StepPills(QWidget):
    """The tour's progress: a pill per step, the current one long. Click one to go there."""

    stepClicked = Signal(int)
    PILL, CURRENT, HEIGHT, GAP = 14, 30, 6, 6

    def __init__(self, titles, parent=None):
        super().__init__(parent)
        self.titles, self.current = titles, 0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.sizeHint())

    def sizeHint(self):
        count = len(self.titles)
        return QSize(self.CURRENT + (count - 1) * self.PILL + (count - 1) * self.GAP,
                     self.HEIGHT + 8)

    def set_current(self, index):
        self.current = index
        self.setToolTip(f'{index + 1} of {len(self.titles)}: {self.titles[index]}')
        self.update()

    def _pills(self):
        x, top = 0.0, (self.height() - self.HEIGHT) / 2
        for index in range(len(self.titles)):
            width = self.CURRENT if index == self.current else self.PILL
            yield index, QRectF(x, top, width, self.HEIGHT)
            x += width + self.GAP

    def mousePressEvent(self, event):
        for index, pill in self._pills():
            if pill.adjusted(-self.GAP / 2, -4, self.GAP / 2, 4).contains(event.position()):
                self.stepClicked.emit(index)
                return
        super().mousePressEvent(event)

    def paintEvent(self, _event):
        palette = self.palette()
        accent = palette.color(QPalette.ColorRole.Highlight)
        text = palette.color(QPalette.ColorRole.WindowText)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for index, pill in self._pills():
            painter.setBrush(accent if index == self.current else
                             opacity(accent, 0.45) if index < self.current else opacity(text, 0.16))
            painter.drawRoundedRect(pill, self.HEIGHT / 2, self.HEIGHT / 2)
        painter.end()


class Column(QWidget):
    """Content at most COLUMN_WIDTH wide, centered in wider panes, so the header, each page
    and the footer line up. It paints the note background itself: Binary Ninja's style
    sheet gives scroll areas their own."""

    def __init__(self, parent=None, *, layout=QVBoxLayout, top=0, bottom=0, spacing=14):
        super().__init__(parent)
        self.content = layout(self)
        self.content.setSpacing(spacing)
        self._top, self._bottom = top, bottom
        self._center()

    def _center(self):
        side = max(20, (self.width() - COLUMN_WIDTH) // 2)
        self.content.setContentsMargins(side, self._top, side, self._bottom)

    def resizeEvent(self, event):
        self._center()
        super().resizeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), self.palette().color(QPalette.ColorRole.Base))
        painter.end()


class OnboardingWidget(QWidget):
    """A short tour of what Notes adds to Markdown, which also sets the defaults that matter.

    Settings it changes are Application-scope preferences, as on the Settings page.
    """

    finished = Signal()
    TITLE = 'Welcome'

    def __init__(self, parent=None, *, preferences=None, data=None):
        super().__init__(parent)
        self.setObjectName('bnnotes-onboarding')
        self.setAccessibleName('Welcome to Notes')
        self.preferences = preferences if preferences is not None else editor_preferences()
        self.controls = {}   # setting name: its checkbox or combo box
        self.embeds = []     # samples that follow the address-column setting
        self.author_sample = self.author_legend = self.il_menu_sample = None
        self.export_sample = None
        self.samples = BUILT_IN
        self._sources = {}   # role: SourceSample, rewritten when the file's samples arrive
        self._embed_roles = {}

        self.wordmark = QLabel('Notes', self)
        self.wordmark.setObjectName('bnnotes-onboarding-wordmark')
        self.wordmark.setFont(wordmark_font(self.font()))
        header = Column(self, top=18, bottom=6, spacing=10)
        header.content.addWidget(self.wordmark)

        steps = (
            ('Welcome', self._welcome),
            ('Embed code', self._embeds),
            ('Embed entire blocks of IL', self._blocks),
            ('Export', self._export),
            ('Authors', self._authors),
            ('Agents', self._agents),
        )
        self.titles = tuple(title for title, _build in steps)
        self.pills = StepPills(self.titles, self)
        self.pills.stepClicked.connect(self.show_step)
        header.content.addWidget(self.pills)

        self.pages = QStackedWidget(self)
        for title, build in steps:
            self.pages.addWidget(self._page(title, build))

        self.error = _label('', self)
        self.error.setContentsMargins(20, 0, 20, 0)
        self.error.hide()
        self.back_button = QPushButton('Back', self)
        self.back_button.clicked.connect(lambda _checked=False: self.show_step(self.step - 1))
        self.next_button = QPushButton('Next', self)
        self.next_button.setDefault(True)
        self.next_button.setMinimumWidth(110)
        self.next_button.clicked.connect(lambda _checked=False: self.advance())
        footer = Column(self, layout=QHBoxLayout, top=10, bottom=14)
        footer.content.addWidget(self.back_button)
        footer.content.addStretch(1)
        footer.content.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self.pages, 1)
        layout.addWidget(self.error)
        layout.addWidget(footer)
        self.setFocusProxy(self.next_button)

        self.preferences.changed.connect(self._refresh)
        self._refresh()
        self.show_step(0)
        self._load_samples(data)

    # Steps

    @property
    def step(self):
        return self.pages.currentIndex()

    def show_step(self, index):
        index = max(0, min(index, self.pages.count() - 1))
        self.pages.setCurrentIndex(index)
        self.pills.set_current(index)
        self.back_button.setEnabled(index > 0)
        self.next_button.setText('Get Started' if index == self.pages.count() - 1 else 'Next')
        self.pages.currentWidget().verticalScrollBar().setValue(0)

    def advance(self):
        if self.step == self.pages.count() - 1:
            self.finished.emit()
        else:
            self.show_step(self.step + 1)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.advance()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _page(self, title, build):
        scroll = QScrollArea(self.pages)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        column = Column(scroll, top=14, bottom=24)
        heading = QLabel(title, column)
        heading.setWordWrap(True)
        heading.setFont(_pixel_font(self, 22, bold=True))
        column.content.addWidget(heading)
        build(column.content, column)
        column.content.addStretch(1)
        scroll.setWidget(column)
        return scroll

    def _source(self, layout, parent, role):
        sample = SourceSample(self._source_parts(role), parent)
        self._sources[role] = sample
        layout.addWidget(sample)

    def _source_parts(self, role):
        samples = self.samples
        if role in ('statement', 'export'):
            return link_source(samples.name, samples.expression)
        if role == 'function':
            return link_source('!', samples.name)
        return link_source('!', samples.expression, 'disasm')

    def _embed(self, layout, parent, role, **kwargs):
        sample = EmbedSample(getattr(self.samples, role), self._embed_label(role), parent,
                             **kwargs)
        self.embeds.append(sample)
        self._embed_roles[sample] = role
        layout.addWidget(sample)
        return sample

    def _embed_label(self, role):
        return self.samples.name if role == 'statement' else ''

    def _welcome(self, layout, parent):
        layout.addWidget(_label('Markdown notes, saved in the .bndb.', parent))
        stage = Stage(parent)
        heading = QLabel('Findings', stage)
        heading.setFont(_pixel_font(self, 18, bold=True))
        stage.content.addWidget(heading)
        self._embed(stage.content, stage, 'statement')
        stage.content.addWidget(CodeBlockSample('c', FIX, stage))
        rows = ('Finding', 'Status'), ('Unchecked length', 'Confirmed'), ('Patch', 'To do')
        stage.content.addWidget(TableSample(rows, stage))
        layout.addWidget(stage)

    def _embeds(self, layout, parent):
        stage = Stage(parent, spacing=6)
        samples = self.samples
        self.symbols_sample = SymbolsSample(samples.symbols, samples.symbol, stage)
        self.linear_sample = LinearSample(samples.function, samples.statement.address, stage)
        self.panes = Panes((self.symbols_sample, self.linear_sample), stage, stretches=(2, 3))
        stage.content.addWidget(self.panes)
        stage.content.addWidget(Arrow(stage))
        self._source(stage.content, stage, 'statement')
        stage.content.addWidget(Arrow(stage))
        self._embed(stage.content, stage, 'statement', hint=True)
        layout.addWidget(stage)
        layout.addWidget(_label(f'{follow_modifier()}-click an embed to go there.', parent))
        self._settings(layout, parent, ('hlil_show_addresses',))

    def _blocks(self, layout, parent):
        stage = Stage(parent, spacing=6)
        self._source(stage.content, stage, 'function')
        stage.content.addWidget(Arrow(stage))
        self._embed(stage.content, stage, 'function')
        layout.addWidget(stage)
        # A different feature: which IL an embed shows.
        layout.addSpacing(8)
        subheading = QLabel('Choose the IL', parent)
        subheading.setFont(_pixel_font(self, 16, bold=True))
        layout.addWidget(subheading)
        stage = Stage(parent, spacing=6)
        self.il_menu_sample = EmbedMenuSample(self.samples.statement, stage)
        stage.content.addWidget(self.il_menu_sample)
        stage.content.addWidget(Arrow(stage))
        self._source(stage.content, stage, 'disassembly')
        stage.content.addWidget(Arrow(stage))
        self._embed(stage.content, stage, 'disassembly')
        layout.addWidget(stage)
        layout.addWidget(_label(
            'Titles: <code>llil</code> · <code>mlil</code> · <code>disasm</code> · '
            '<code>c</code> · …', parent, muted=True))

    def _export(self, layout, parent):
        layout.addWidget(_label('Export a note as Markdown, and its embeds become code blocks '
                                'anyone can read.', parent))
        stage = Stage(parent, spacing=6)
        self._embed(stage.content, stage, 'statement')
        stage.content.addWidget(Arrow(stage))
        self.export_sample = ExportSample(stage)
        stage.content.addWidget(self.export_sample)
        self._source(stage.content, stage, 'export')  # What it exports when the setting is off.
        layout.addWidget(stage)
        layout.addWidget(_label('Right-click a note’s tab: <b>Export Note as Markdown…</b>',
                                parent, muted=True))
        self._settings(layout, parent, ('export_embeds_as_code',))

    def _authors(self, layout, parent):
        layout.addWidget(_label('Attribution for both Enterprise teams and agentic workflows!',
                                parent))
        stage = Stage(parent)
        self.author_sample = AuthorSample(stage, person=enterprise_user())
        stage.content.addWidget(self.author_sample)
        self.author_legend = Legend(stage)
        stage.content.addWidget(self.author_legend)
        layout.addWidget(stage)
        self._settings(layout, parent, ('show_authors',))

    def _agents(self, layout, parent):
        stage = Stage(parent)
        stage.content.addWidget(MenuSample((('MCP Whitelist', 'title'), ('', '-'),
                                            ('Allow Agents on This View', 'checked'), ('', '-'),
                                            ('Header parsing', 'checked'),
                                            ('Scratch', 'unchecked')), stage))
        layout.addWidget(stage)
        layout.addWidget(_label('Agents see only whitelisted notes.', parent))
        self._settings(layout, parent,
                       ('mcp_default_allow', 'mcp_allow_create', 'mcp_allow_delete'))

    # Settings

    def _settings(self, layout, parent, names):
        """A row for each setting, named and explained as everywhere else."""
        box = QVBoxLayout()
        box.setSpacing(6)
        box.setContentsMargins(2, 4, 0, 0)
        for name in names:
            title, detail = setting_title(name), setting_description(name)
            if 'enum' in SCHEMA[name][1]:
                row = QHBoxLayout()
                text = QLabel(title, parent)
                text.setToolTip(detail)
                combo = QComboBox(parent)
                combo.setAccessibleName(title)
                combo.setToolTip(detail)
                for label, value in setting_choices(name):
                    combo.addItem(label, value)
                combo.currentIndexChanged.connect(
                    lambda _index, name=name, combo=combo: self._set(name, combo.currentData()))
                row.addWidget(text)
                row.addWidget(combo)
                row.addStretch(1)
                box.addLayout(row)
                self.controls[name] = combo
            else:
                check = QCheckBox(title, parent)
                check.setToolTip(detail)
                check.toggled.connect(lambda value, name=name: self._set(name, value))
                box.addWidget(check)
                self.controls[name] = check
        layout.addLayout(box)

    def _set(self, name, value):
        try:
            self.preferences.set(name, value, Scope.APPLICATION)
        except (RuntimeError, ValueError) as error:
            self.error.setText(escape(str(error)))
            self.error.show()
        else:
            self.error.hide()
        self._refresh()

    def _refresh(self, _name=None):
        for name, control in self.controls.items():
            value = self.preferences.get(name, Scope.APPLICATION)
            with QSignalBlocker(control):
                if isinstance(control, QCheckBox):
                    control.setChecked(value)
                else:
                    control.setCurrentIndex(max(0, control.findData(value)))
        shown = self.preferences.get('hlil_show_addresses', Scope.APPLICATION)
        for sample in self.embeds:
            sample.set_show_addresses(shown)
        if self.author_sample is not None:
            self.author_sample.set_show_addresses(shown)
            bars = self.preferences.get('show_authors', Scope.APPLICATION)
            self.author_sample.set_bars_shown(bars)
            self.author_legend.setVisible(bars)
        if self.il_menu_sample is not None:
            self.il_menu_sample.set_show_addresses(shown)
        if self.export_sample is not None:
            self.export_sample.set_export(self.samples.statement, self.samples.name,
                                          show_addresses=shown)
            as_code = self.preferences.get('export_embeds_as_code', Scope.APPLICATION)
            self.export_sample.setVisible(as_code)
            self._sources['export'].setVisible(not as_code)

    # The file's own code

    def _load_samples(self, data):
        if data is None or not hasattr(data, 'functions'):
            return
        self._future = _executor().executor.submit(file_samples, data)
        self._poll = QTimer(self)
        self._poll.setInterval(40)
        self._poll.timeout.connect(self._collect_samples)
        self._poll.start()

    def _collect_samples(self):
        if not self._future.done():
            return
        self._poll.stop()
        samples = None if self._future.cancelled() else self._future.result()
        if samples is not None:
            self.use_samples(samples)

    def use_samples(self, samples):
        """Show `samples` in every source line and embed."""
        self.samples = samples
        for role, source in self._sources.items():
            source.set_parts(self._source_parts(role))
        for sample, role in self._embed_roles.items():
            sample.set_preview(getattr(samples, role), self._embed_label(role))
        if samples.symbols:
            self.symbols_sample.set_symbols(samples.symbols, samples.symbol)
        self.linear_sample.set_preview(samples.function, samples.statement.address)
        self.il_menu_sample.set_preview(samples.statement)
        self.panes.arrange()
        self._refresh()

    # Appearance

    def paintEvent(self, event):
        # Binary Ninja paints tab pages with the window color; a note uses the base color.
        painter = QPainter(self)
        painter.fillRect(event.rect(), self.palette().color(QPalette.ColorRole.Base))
        painter.end()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (event.Type.FontChange, event.Type.ApplicationFontChange):
            self.wordmark.setFont(wordmark_font(self.font()))
