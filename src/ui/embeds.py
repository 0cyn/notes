from __future__ import annotations

import math
import sys
from bisect import bisect_left, bisect_right
from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import dataclass, replace
from enum import Enum, auto
from urllib.parse import unquote
from weakref import WeakSet, ref

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSizeF, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPyTextObject,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QMenu, QToolTip, QWidget
from shiboken6 import isValid

from ..links import INDENT_COLUMNS, MENU_GROUPS, HLILLine, il_mode_available
from ..markdown import MarkdownLink, expansion_edit, label_edit, mode_edit, utf16_length
from ..preparation import PREVIEW_ANCHOR, display_rows
from .common import (
    BLOCK_INSET,
    RetainedDrawing,
    VerticalIndex,
    mix,
    preparation_pool,
    single_shot,
    watch_focus,
    yield_preparation,
)

_THEME_ROLES = {
    1: 'InstructionColor', 3: 'RegisterColor', 4: 'NumberColor',
    5: 'NumberColor', 8: 'NumberColor', 9: 'AnnotationColor', 10: 'NumberColor',
    11: 'LocalVariableColor', 16: 'OpcodeColor', 17: 'StringColor',
    18: 'StringColor', 19: 'KeywordColor', 20: 'TypeNameColor', 21: 'FieldNameColor',
    22: 'NameSpaceColor', 23: 'NameSpaceSeparatorColor', 25: 'NumberColor',
    28: 'GotoLabelColor', 29: 'CommentColor', 32: 'NumberColor',
    35: 'NumberColor', 36: 'OperationColor', 37: 'BaseStructureNameColor',
    64: 'CodeSymbolColor', 65: 'DataSymbolColor', 66: 'LocalVariableColor',
    67: 'ImportColor', 69: 'ImportColor', 70: 'CodeSymbolColor', 71: 'StackVariableColor',
}


def _theme_color(name):
    ui = sys.modules.get('binaryninjaui')
    role = getattr(getattr(ui, 'ThemeColor', None), name, None)
    getter = getattr(ui, 'getThemeColor', None)
    if role is not None and callable(getter):
        try:
            color = QColor(getter(role))
            if color.isValid():
                return color
        except (TypeError, ValueError, RuntimeError):
            pass
    return None


def token_color(widget, kind, palette):
    ui = sys.modules.get('binaryninjaui')
    getter = getattr(ui, 'getTokenColor', None)
    if widget is not None and callable(getter):
        enum = getattr(sys.modules.get('binaryninja'), 'InstructionTextTokenType', None)
        try:
            color = QColor(getter(widget, enum(kind) if enum is not None else kind))
            if color.isValid():
                return color
        except (TypeError, ValueError, RuntimeError):
            pass
    name = _THEME_ROLES.get(kind)
    native = _theme_color(name) if name else None
    if native is not None:
        return native
    # Standalone Qt previews still follow the active palette.
    text, base = palette.text().color(), palette.base().color()
    if kind in (9, 29):
        return mix(base, text, 0.6)
    role = {
        1: QPalette.ColorRole.Highlight, 19: QPalette.ColorRole.Highlight,
        4: QPalette.ColorRole.LinkVisited, 5: QPalette.ColorRole.LinkVisited,
        8: QPalette.ColorRole.LinkVisited, 17: QPalette.ColorRole.LinkVisited,
        18: QPalette.ColorRole.LinkVisited, 20: QPalette.ColorRole.Link,
        22: QPalette.ColorRole.Link, 64: QPalette.ColorRole.Link,
        67: QPalette.ColorRole.Link, 70: QPalette.ColorRole.Link,
    }.get(kind, QPalette.ColorRole.Text)
    color = palette.color(role)
    if role != QPalette.ColorRole.Text and base.lightness() < 128 and color.lightness() < 140:
        color = color.lighter(160)
    return color


@dataclass
class PreviewTheme:
    background: QColor
    border: QColor
    accent: QColor
    text: QColor
    muted: QColor
    tokens: tuple[tuple[int, int], ...]
    function_header: QColor

    @property
    def key(self):
        return (self.background.rgba(), self.border.rgba(), self.accent.rgba(),
                self.text.rgba(), self.muted.rgba(), self.tokens, self.function_header.rgba())


def preview_theme(widget=None, *, palette=None, kinds=()):
    palette = palette if palette is not None else widget.palette()
    base, text = palette.base().color(), palette.text().color()
    dark = base.lightness() < 128
    accent = token_color(widget, 64, palette)
    return PreviewTheme(
        mix(base, text, 0.065 if dark else 0.025),
        mix(base, text, 0.26 if dark else 0.20),
        accent, text, mix(base, text, 0.6),
        tuple((kind, token_color(widget, kind, palette).rgba()) for kind in sorted(set(kinds))),
        _theme_color('LinearDisassemblyFunctionHeaderColor') or mix(base, text, 0.11),
    )


SPINE_WIDTH = 2.0   # beside code; half beside a signature header, none beside a link title
SPINE_EASE = 8.0    # how far either side of a section boundary the width starts changing


def paint_code_frame(painter, rectangle, theme, *, hovered=False):
    painter.save()
    painter.setRenderHint(painter.RenderHint.Antialiasing)
    border = mix(theme.border, theme.accent, 0.55) if hovered else theme.border
    painter.setPen(QPen(border, 1))
    painter.setBrush(theme.background)
    painter.drawRoundedRect(rectangle.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
    painter.restore()


def spine_path(left, segments):
    """Outline of a spine along `left`, easing between `segments` of (top, bottom, width)."""
    segments = [segment for segment in segments if segment[1] > segment[0]]
    top, _bottom, width = segments[0]
    path = QPainterPath(QPointF(left, top))
    path.lineTo(left + width, top)
    for index, (top, bottom, width) in enumerate(segments):
        following = segments[index + 1] if index + 1 < len(segments) else None
        if following is None or following[2] == width:
            path.lineTo(left + width, bottom)
            continue
        ease = min(SPINE_EASE, (bottom - top) / 2, (following[1] - following[0]) / 2)
        target = following[2]
        path.lineTo(left + width, bottom - ease)
        path.quadTo(QPointF(left + width, bottom), QPointF(left + (width + target) / 2, bottom))
        path.quadTo(QPointF(left + target, bottom), QPointF(left + target, bottom + ease))
    path.lineTo(left, segments[-1][1])
    path.closeSubpath()
    return path


def paint_code_spine(painter, left, segments, theme, *, hovered=False, available=True):
    """The accent spine along a frame's inner left edge."""
    if not any(bottom > top for top, bottom, _width in segments):
        return
    accent = QColor(theme.accent if available else theme.muted)
    accent.setAlphaF(0.8 if hovered else 0.55)
    painter.save()
    painter.setRenderHint(painter.RenderHint.Antialiasing)
    painter.fillPath(spine_path(left, segments), accent)
    painter.restore()


CODE_PADDING_X = BLOCK_INSET  # Content inset shared with code blocks and table cells.
CODE_PADDING_Y = 3.0
TITLE_PADDING_Y = 2.0


def text_layout(text, font, formats=()):
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setTextDirection(Qt.LayoutDirection.LeftToRight)
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.setFormats(formats)
    return layout


def format_range(start, length, color):
    item = QTextLayout.FormatRange()
    item.start, item.length = start, length
    fmt = QTextCharFormat()
    fmt.setForeground(color)
    item.format = fmt
    return item


class CodePreviewLayout:
    def __init__(self, preview, font, available, theme, *, show_addresses=True,
                 label='', label_font=None):
        self.preview, self.theme = preview, theme
        self.label = label
        # Labels get a title strip above the code that widens the embed to fit, then elides.
        self.title_layout = None
        self.title_height = 0.0
        title_font = None
        if label:
            title_font = QFont(label_font or font)
            title_font.setUnderline(False)
            self.title_height = math.ceil(QFontMetricsF(title_font).height()
                                          + TITLE_PADDING_Y * 2)
        self.rows = []
        self.addresses = []
        self._row_tops, self._row_bottoms = [], []
        self._address_tops, self._address_bottoms = [], []
        self.header_height = 0.0
        rows = display_rows(preview, show_addresses=show_addresses)
        self.text = '\n'.join((f'{row.address:x}\t' if show_addresses else '')
                              + ' ' * row.indent + row.text for row in rows)
        metrics = QFontMetricsF(font)
        space = metrics.horizontalAdvance(' ')
        tab = space * INDENT_COLUMNS
        address_width = max(metrics.horizontalAdvance(f'{row.address:x}') for row in rows)
        self.tab_stop = (math.floor(address_width / tab) + 1) * tab if show_addresses else 0
        code_width = max(space * 6, available - self.tab_stop - CODE_PADDING_X * 2)
        colors = dict(theme.tokens)
        foreground = theme.text if preview.available else theme.muted
        address_color = QColor.fromRgba(colors.get(68, theme.muted.rgba()))
        y, right, wrapped = self.title_height + CODE_PADDING_Y, 0.0, False
        header_count = len(preview.header_lines)
        for index, row in enumerate(rows):
            yield_preparation()
            is_header = index < header_count
            if show_addresses:
                address = f'{row.address:x}'
                address_layout = text_layout(address, font, [
                    format_range(0, len(address), address_color),
                ])
                address_layout.beginLayout()
                address_line = address_layout.createLine()
                address_line.setLineWidth(address_width + 1)
                address_layout.endLayout()
            text = ' ' * row.indent + row.text
            formats = [format_range(0, utf16_length(text), foreground)]
            offset = row.indent
            for token in row.tokens:
                length = utf16_length(token.text)
                color = QColor.fromRgba(colors.get(token.kind, foreground.rgba()))
                token_range = format_range(offset, length, color)
                if is_header and token.kind == 64:
                    fmt = QTextCharFormat(token_range.format)
                    fmt.setFontWeight(QFont.Weight.DemiBold)
                    token_range.format = fmt
                formats.append(token_range)
                offset += length
            layout = text_layout(text, font, formats)
            layout.beginLayout()
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                # Only address-column mode hangs additional visual wraps.
                wrap_columns = row.indent + (INDENT_COLUMNS if show_addresses else 0)
                indent = (min(wrap_columns * space, code_width * 0.5)
                          if line.textStart() else 0)
                line.setLineWidth(max(space * 2, code_width - indent))
                line.setPosition(QPointF(CODE_PADDING_X + self.tab_stop + indent, y))
                if show_addresses:
                    address_y = y + line.ascent() - address_line.ascent()
                    self.addresses.append((address_layout,
                                           QPointF(CODE_PADDING_X, address_y)))
                    self._address_tops.append(address_y)
                    self._address_bottoms.append(address_y + address_line.height())
                right = max(right, line.x() + line.naturalTextWidth())
                wrapped |= line.textStart() > 0
                y += line.height()
            layout.endLayout()
            self.rows.append(layout)
            self._row_tops.append(layout.boundingRect().top())
            self._row_bottoms.append(layout.boundingRect().bottom())
            if is_header and index + 1 == header_count:
                self.header_height = y + CODE_PADDING_Y
                y = self.header_height + 1 + CODE_PADDING_Y
        # Shrink to the code, unless it already wraps: then fill the text column.
        self.width = (math.floor(available) if wrapped
                      else math.ceil(right + CODE_PADDING_X))
        self.height = math.ceil(y + CODE_PADDING_Y)
        if label:
            self._layout_title(title_font, available)
        self._drawing = RetainedDrawing(
            [QRectF(0, top, self.width, bottom - top)
             for top, bottom in zip(self._row_tops, self._row_bottoms, strict=True)],
            self._draw_text,
        )

    def _draw_text(self, painter, visible):
        top, bottom = visible.top(), visible.bottom()
        for index in range(bisect_right(self._row_bottoms, top),
                           bisect_left(self._row_tops, bottom)):
            self.rows[index].draw(painter, QPointF(), [], visible)
        for index in range(bisect_right(self._address_bottoms, top),
                           bisect_left(self._address_tops, bottom)):
            layout, point = self.addresses[index]
            layout.draw(painter, point, [], visible)

    def _layout_title(self, font, available):
        metrics = QFontMetricsF(font)
        text = ' '.join(self.label.split())
        wanted = math.ceil(metrics.horizontalAdvance(text) + CODE_PADDING_X * 2)
        self.width = max(self.width, min(wanted, math.floor(available)))
        text = metrics.elidedText(text, Qt.TextElideMode.ElideRight,
                                  self.width - CODE_PADDING_X * 2)
        layout = QTextLayout(text, font)
        layout.beginLayout()
        line = layout.createLine()
        line.setPosition(QPointF(CODE_PADDING_X, TITLE_PADDING_Y))
        layout.endLayout()
        self.title_layout = layout

    def title_rectangle(self, origin, visible=None):
        """The title strip: at the embed's top, or stuck to the top of `visible`."""
        if not self.title_height:
            return QRectF()
        y = origin.y()
        if visible is not None and visible.top() > y:
            y = min(visible.top(), origin.y() + self.height - self.title_height)
        return QRectF(origin.x(), y, self.width, self.title_height)

    def draw(self, painter, origin, *, hovered=False, visible=None):
        """Title strip, signature header and code, with the accent spine."""
        rectangle = QRectF(origin.x(), origin.y(), self.width, self.height)
        visible = rectangle if visible is None else rectangle.intersected(visible)
        paint_code_frame(painter, rectangle, self.theme, hovered=hovered)
        interior = QPainterPath()
        interior.addRoundedRect(rectangle.adjusted(1, 1, -1, -1), 2, 2)
        hlil_top = origin.y() + self.title_height
        if self.header_height:
            painter.save()
            painter.setClipPath(interior, Qt.ClipOperation.IntersectClip)
            header_bottom = origin.y() + self.header_height
            painter.fillRect(QRectF(origin.x(), hlil_top, self.width, header_bottom - hlil_top),
                             self.theme.function_header)
            painter.setPen(QPen(self.theme.border, 1))
            painter.drawLine(QPointF(origin.x(), header_bottom),
                             QPointF(origin.x() + self.width, header_bottom))
            painter.restore()
        self._drawing.draw(painter, origin, visible)
        painter.save()
        painter.setClipPath(interior, Qt.ClipOperation.IntersectClip)
        painter.setClipRect(visible, Qt.ClipOperation.IntersectClip)
        title = self.title_rectangle(origin, visible)
        if not title.isEmpty():
            # Opaque: a stuck title hides the rows passing under it.
            painter.fillRect(title, mix(self.theme.background, self.theme.text, 0.06))
            painter.setPen(QPen(self.theme.border, 1))
            painter.drawLine(QPointF(title.left(), title.bottom()),
                             QPointF(title.right(), title.bottom()))
            painter.setPen(self.theme.muted)
            self.title_layout.draw(painter, title.topLeft())
        paint_code_spine(painter, origin.x() + 1, self._spine_segments(origin, title),
                         self.theme, hovered=hovered, available=self.preview.available)
        painter.restore()

    def _spine_segments(self, origin, title):
        """(top, bottom, width) runs of the spine, following a stuck title."""
        bottom = origin.y() + self.height
        code_top = origin.y() + max(self.title_height, self.header_height)
        segments = [(code_top, bottom, SPINE_WIDTH)]
        if self.header_height:
            segments.insert(0, (origin.y() + self.title_height, code_top, SPINE_WIDTH / 2))
        if not title.isEmpty():
            segments = [(max(top, title.bottom()), end, width)
                        for top, end, width in segments if end > title.bottom()]
            segments.insert(0, (title.top(), title.bottom(), 0.0))
        return segments


CODE_OBJECT = QTextFormat.ObjectTypes.UserObject + 1
PREVIEW_INDEX = QTextFormat.Property.UserProperty + 1


class CellCodeObjects(QPyTextObject):
    def __init__(self, document, previews, font, theme, *, show_addresses=True,
                 layout_factory=None):
        super().__init__(document)
        self.layouts = {}
        self.rectangles = {}
        self.hover_point = None
        self.visible_rect = None
        self.recording = False
        self.pending = False
        document.documentLayout().registerHandler(CODE_OBJECT, self)
        ranges = {}
        block = document.begin()
        while block.isValid():
            fragment = block.begin()
            while not fragment.atEnd():
                item = fragment.fragment()
                marker = item.charFormat().anchorHref()
                suffix = marker.removeprefix(PREVIEW_ANCHOR).partition(':')[0]
                if (marker.startswith(PREVIEW_ANCHOR) and suffix.isdecimal()
                        and int(suffix) < len(previews)):
                    index = int(suffix)
                    start, end = item.position(), item.position() + item.length()
                    if ranges and next(reversed(ranges.values()))[1] == start:
                        key = next(reversed(ranges))
                        if ranges[key][3] == marker:
                            ranges[key] = ranges[key][0], end, index, marker
                        else:
                            ranges[start] = start, end, index, marker
                    else:
                        ranges[start] = start, end, index, marker
                fragment += 1
            block = block.next()
        metrics = QFontMetricsF(document.defaultFont())
        occurrences = list(ranges.values())
        for occurrence in reversed(range(len(occurrences))):
            start, end, index, marker = occurrences[occurrence]
            parts = marker.removeprefix(PREVIEW_ANCHOR).split(':', 2)
            label = unquote(parts[2]) if len(parts) == 3 else ''
            cursor = QTextCursor(document)
            cursor.setPosition(start)
            prefix = QTextCursor(cursor)
            prefix_start = cursor.block().position()
            if occurrence:
                # Earlier previews are separate objects; their placeholder text is not this label.
                prefix_start = max(prefix_start, occurrences[occurrence - 1][1])
            prefix.setPosition(prefix_start, QTextCursor.MoveMode.KeepAnchor)
            label_width = metrics.horizontalAdvance(prefix.selectedText())
            available = max(document.textWidth() * 0.5, document.textWidth() - label_width - 4)
            key = len(self.layouts)
            preview = previews[index][1]
            factory = layout_factory or CodePreviewLayout
            code = factory(preview, font, available, theme, show_addresses=show_addresses,
                           label=label, label_font=document.defaultFont())
            if code is None:
                self.pending = True
                code = CodePreviewLayout(
                    HLILLine(preview.address, f'Preparing {preview.il_mode.label}…',
                             il_mode=preview.il_mode),
                    font, available, theme, show_addresses=show_addresses,
                    label=label, label_font=document.defaultFont(),
                )
            self.pending |= getattr(code, 'pending', False)
            self.layouts[key] = code
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setObjectType(CODE_OBJECT)
            fmt.setProperty(PREVIEW_INDEX, key)
            fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignMiddle)
            cursor.insertText('\ufffc', fmt)

    def intrinsicSize(self, _document, _position, fmt):
        layout = self.layouts[fmt.intProperty(PREVIEW_INDEX)]
        return QSizeF(layout.width, layout.height)

    def drawObject(self, painter, rectangle, _document, _position, fmt):
        index = fmt.intProperty(PREVIEW_INDEX)
        self.rectangles[index] = QRectF(rectangle)
        if self.recording:
            return
        hovered = self.hover_point is not None and rectangle.contains(self.hover_point)
        self.layouts[index].draw(painter, rectangle.topLeft(), hovered=hovered,
                                  visible=self.visible_rect)


_ACTIONS = 'Embed whole AST', 'Select IL mode', 'Set title'


def _analysis(links):
    controller = links.controller
    if controller is None:
        return None
    highlighter = controller.highlighter
    highlighter.refresh()  # Positions must match the document as it is now.
    return None if highlighter.warning else highlighter.analysis


def _embeddable(links, link):
    """An In-BV link on one source line: the links shown as embeds."""
    if links.provider is None or links.provider.request(link.url, load=False) is None:
        return False
    document = links.editor.document()
    return document.findBlock(link.start) == document.findBlock(max(link.start, link.end - 1))


def context_target(links, point):
    """The embed under `point` (in viewport coordinates), if one is shown there."""
    entry = links.hit_test(QPointF(point))
    return (entry.link, entry) if entry is not None else (None, None)


def source_link_at(links, position):
    """The revealed In-BV link whose source contains `position`."""
    analysis = _analysis(links)
    for link in analysis.links if analysis is not None else ():
        if link.start <= position < link.end and _embeddable(links, link):
            return link
    return None


def _source(links, start, end):
    cursor = QTextCursor(links.editor.document())
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    return cursor.selectedText()


def _apply(links, edit):
    """Replace one source range as a single undoable edit of the editor's note."""
    editor = links.editor
    session = getattr(editor, 'session', None)
    scope = (session.source_edit(edit.start)
             if session is not None and getattr(session, 'document', None) is editor.document()
             else nullcontext())
    if hasattr(editor, 'token'):
        editor.token = object()  # A cell editor's own undo step, not merged with typing.
    with scope:
        cursor = QTextCursor(editor.document())
        cursor.setPosition(edit.start)
        cursor.setPosition(edit.end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(edit.text)
    if hasattr(editor, 'token'):
        editor.token = object()


def _current(links, link):
    analysis = _analysis(links)
    return analysis is not None and link in analysis.links


def _rewrite(links, link, edit):
    if edit is not None and _current(links, link):
        _apply(links, edit)


def _set_title(links, link, entry, point):
    if not _current(links, link):
        return
    if entry is not None:
        links.hold_around(entry, point)  # Before the edit, while its geometry is current.
    label = _source(links, *link.label) if link.label is not None else ''
    edit, start, end = label_edit(link, label)
    if edit is not None:
        _apply(links, edit)
    links.select_source(start, end)


def add_embed_actions(menu, links, link, entry=None, point=None):
    """Put the embed's own commands at the top of `menu`, above a separator."""
    first = menu.actions()[0] if menu.actions() else None
    editable = not links.editor.isReadOnly()

    expand = QAction(_ACTIONS[0], menu)
    expand.setCheckable(True)
    expand.setChecked(link.expand_block)
    expand.setEnabled(editable)
    expand.triggered.connect(
        lambda checked: _rewrite(links, link, expansion_edit(link, checked)))
    menu.insertAction(first, expand)

    modes = menu.addMenu(_ACTIONS[1])
    menu.removeAction(modes.menuAction())
    menu.insertAction(first, modes.menuAction())
    modes.menuAction().setEnabled(editable)
    group = QActionGroup(modes)
    for index, members in enumerate(MENU_GROUPS):
        if index:
            modes.addSeparator()
        for mode in members:
            if not il_mode_available(mode):
                continue
            action = modes.addAction(mode.label)
            action.setCheckable(True)
            action.setChecked(mode == link.il_mode)
            action.setActionGroup(group)
            action.triggered.connect(
                lambda _checked=False, mode=mode: _rewrite(links, link, mode_edit(link, mode)))

    title = QAction(_ACTIONS[2], menu)
    title.setEnabled(editable)
    title.triggered.connect(lambda: _set_title(links, link, entry, point))
    menu.insertAction(first, title)
    menu.insertSeparator(first)


def leave_link_source(links):
    """Enter in a revealed link's source moves to the next line where it would split the link."""
    editor = links.editor
    cursor = editor.textCursor()
    first, last = cursor.selectionStart(), cursor.selectionEnd()
    analysis = _analysis(links)
    for link in analysis.links if analysis is not None else ():
        if first <= link.start and last >= link.end:
            continue
        if not any(link.start < position < link.end for position in (first, last)):
            continue
        if not _embeddable(links, link):
            continue
        document = editor.document()
        block = document.findBlock(link.end)
        following = block.next()
        target = QTextCursor(document)
        if following.isValid() and following.isVisible():
            target.setPosition(following.position())
            editor.setTextCursor(target)
        else:
            # The last line, or a line just above a folded table: open a new line.
            target.setPosition(block.position() + block.length() - 1)
            editor.setTextCursor(target)
            editor.insertPlainText('\n')
        return True
    return False


def follow_modifier():
    return '⌘' if sys.platform == 'darwin' else 'Ctrl'


def _hint_font():
    font = QFont(QApplication.font())
    font.setPointSizeF(9)
    font.setBold(False)
    return font


def hint_rectangle(code, bounds):
    metrics = QFontMetricsF(_hint_font())
    width = metrics.horizontalAdvance(f'{follow_modifier()} +') + 12 + 17
    height = max(12, metrics.height()) + 6
    left = min(max(bounds.left() + 1, code.right() - width - 2), bounds.right() - width - 1)
    top = min(max(bounds.top() + 1, code.bottom() - 3), bounds.bottom() - height - 1)
    return QRectF(left, top, width, height)


def _paint_hand(painter, origin):
    # A monochrome pointing-hand cursor avoids platform-dependent emoji glyphs.
    painter.save()
    painter.translate(origin)
    painter.scale(0.75, 0.75)
    path = QPainterPath(QPointF(5, 8))
    path.lineTo(5, 2)
    path.cubicTo(5, 0, 7.5, 0, 7.5, 2)
    path.lineTo(7.5, 6.5)
    path.cubicTo(8, 5, 10, 5, 10, 7)
    path.cubicTo(10.5, 5.5, 12.5, 6, 12.5, 8)
    path.cubicTo(13.5, 7, 15, 8, 15, 9.5)
    path.lineTo(14, 13)
    path.lineTo(12.5, 15)
    path.lineTo(7, 15)
    path.lineTo(2, 9.5)
    path.cubicTo(0.5, 7.5, 2, 6, 3.5, 7)
    path.lineTo(5, 8)
    painter.drawPath(path)
    painter.restore()


def _paint_box(painter, rectangle, theme):
    dark = theme.background.lightness() < 128
    background = QColor(30, 30, 30, 215) if dark else QColor(248, 248, 248, 220)
    foreground = QColor(180, 180, 180) if dark else QColor(90, 90, 90)
    border = QColor(foreground)
    border.setAlpha(90)
    painter.save()
    painter.setRenderHint(painter.RenderHint.Antialiasing)
    painter.setPen(QPen(border, 1))
    painter.setBrush(background)
    painter.drawRoundedRect(rectangle, 3, 3)
    painter.setFont(_hint_font())
    painter.setPen(foreground)
    text = f'{follow_modifier()} +'
    metrics = QFontMetricsF(painter.font())
    x = rectangle.left() + 6
    baseline = rectangle.center().y() + (metrics.ascent() - metrics.descent()) / 2
    painter.drawText(QPointF(x, baseline), text)
    pen = QPen(foreground, 1.2)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    _paint_hand(painter, QPointF(x + metrics.horizontalAdvance(text) + 4,
                                 rectangle.center().y() - 6))
    painter.restore()


class FollowHint(QWidget):
    """Float above table children and cell clipping without intercepting clicks."""

    def __init__(self, viewport):
        super().__init__(viewport)
        self.setObjectName('bnnotes-hlil-follow-hint')
        self.setAccessibleName(f'{follow_modifier()} + click to follow address')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._theme = None
        self.hide()

    def set_preview(self, code=None, theme=None):
        if code is None:
            self.hide()
            return
        rectangle = hint_rectangle(code, QRectF(self.parentWidget().rect())).toAlignedRect()
        changed = self._theme is None or self._theme.key != theme.key
        self._theme = theme
        if self.geometry() != rectangle:
            self.setGeometry(rectangle)
        if changed:
            self.update()
        self.raise_()
        self.show()

    def paintEvent(self, _event):
        if self._theme is not None:
            painter = QPainter(self)
            _paint_box(painter, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._theme)
            painter.end()


def scroll_owners(widget):
    while widget is not None:
        if isinstance(widget, QAbstractScrollArea):
            yield widget
        widget = widget.parentWidget()


def visible_rect(widget):
    """Account for a full-height cell editor clipped by its table/note viewport."""
    rectangle = QRectF(widget.rect())
    parent = widget.parentWidget()
    while parent is not None:
        origin = widget.mapFromGlobal(parent.mapToGlobal(QPoint()))
        rectangle = rectangle.intersected(QRectF(origin, parent.size()))
        parent = parent.parentWidget()
    return rectangle


class NavigationState:
    def __init__(self, widget):
        owners = list(scroll_owners(widget))
        focused = QApplication.focusWidget()
        if focused is not None:
            owners.extend(item for item in scroll_owners(focused) if item not in owners)
        self.values = []
        for owner in owners:
            cursor = QTextCursor(owner.textCursor()) if hasattr(owner, 'textCursor') else None
            source = owner.toPlainText() if cursor is not None else None
            self.values.append((ref(owner), cursor, source, owner.horizontalScrollBar().value(),
                                owner.verticalScrollBar().value()))

    def restore(self):
        for pointer, cursor, source, horizontal, vertical in self.values:
            owner = pointer()
            if owner is None or not isValid(owner):
                continue
            if cursor is not None:
                if owner.toPlainText() != source:
                    continue
                current = owner.textCursor()
                if (current.position(), current.anchor(), current.verticalMovementX()) != (
                    cursor.position(), cursor.anchor(), cursor.verticalMovementX(),
                ):
                    owner.setTextCursor(cursor)
            owner.horizontalScrollBar().setValue(horizontal)
            owner.verticalScrollBar().setValue(vertical)


def line_edges(editor, position):
    """Viewport (top, bottom) of the visual line at `position`, including reserved space."""
    block = editor.document().findBlock(position)
    top = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
    band = getattr(editor.document().documentLayout(), 'line_band', None)
    if band is not None:
        band_top, band_bottom = band(block, position)
        return top + band_top, top + band_bottom
    line = block.layout().lineForTextPosition(position - block.position())
    if not line.isValid():
        return top, top
    return top + line.y(), top + line.y() + line.height()


@dataclass(frozen=True)
class RevealHold:
    """The source line that stays still while an embed turns back into source."""
    position: int         # a source position on the held line
    y: float              # viewport y of that line's top (or bottom)
    bottom: bool = False


def reveal_hold(editor, entry, pointer_y, visible):
    """Where to hold the view when an embed turns into source: keep the visible side still."""
    link = entry.link
    block = editor.document().findBlock(link.start)
    first = block.layout().lineForTextPosition(link.start - block.position())
    if first.isValid():
        top, _bottom = line_edges(editor, link.start)
        if top >= visible.top():
            # Text before the link is unchanged, so the line still starts at its first character.
            return RevealHold(block.position() + first.textStart(), top)
        _top, bottom = line_edges(editor, link.end - 1)
        if bottom <= visible.bottom():
            return RevealHold(link.end - 1, bottom, bottom=True)
    return RevealHold(entry.edit_position, pointer_y)


@dataclass
class ViewportAnchor:
    editor: object
    cursor: QTextCursor
    global_y: int
    block_top: bool = False
    line_bottom: bool = False

    @classmethod
    def capture(cls, editor):
        block = editor.firstVisibleBlock()
        cursor = QTextCursor(block)
        y = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
        global_y = editor.viewport().mapToGlobal(QPoint(0, round(y))).y()
        return cls(ref(editor), cursor, global_y, True)

    @classmethod
    def holding(cls, editor, hold):
        cursor = QTextCursor(editor.document())
        cursor.setPosition(hold.position)
        global_y = editor.viewport().mapToGlobal(QPoint(0, round(hold.y))).y()
        return cls(ref(editor), cursor, global_y, line_bottom=hold.bottom)

    def restore(self):
        editor = self.editor()
        if editor is None or not isValid(editor) or self.cursor.document() != editor.document():
            return
        for owner in scroll_owners(editor):
            if self.block_top:
                y = editor.blockBoundingGeometry(self.cursor.block()).translated(
                    editor.contentOffset(),
                ).top()
            else:
                top, bottom = line_edges(editor, self.cursor.position())
                y = bottom if self.line_bottom else top
            current = editor.viewport().mapToGlobal(QPoint(0, round(y))).y()
            bar = owner.verticalScrollBar()
            bar.setValue(bar.value() + current - self.global_y)


def _source_slice(document, start, end):
    cursor = QTextCursor(document)
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    return cursor.selectedText()


def _label(highlighter, link):
    wrapper = next((item for item in highlighter.analysis.inline_formats
                    if item.role == 'link' and item.start == link.start and item.end == link.end),
                   None)
    if wrapper is None:
        return '', link.start
    start, end = wrapper.content_start, wrapper.content_end
    text = _source_slice(highlighter.document(), start, end).encode('utf-16-le')
    hidden = []
    for item in highlighter.analysis.inline_formats:
        if item is wrapper or not start <= item.start <= item.end <= end:
            continue
        if item.role == 'comment':
            hidden.append((item.start, item.end))
        else:
            hidden.extend(((item.start, item.content_start), (item.content_end, item.end)))
    merged = []
    for left, right in sorted(hidden):
        if merged and left <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(right, merged[-1][1])
        else:
            merged.append((left, right))
    for left, right in reversed(merged):
        text = text[:(left - start) * 2] + text[(right - start) * 2:]
    label = text.decode('utf-16-le')
    if '\\' in label or '&' in label:
        from markdown_it.common.utils import unescapeAll

        label = unescapeAll(label)
    if link.expand_block:
        label = label.removeprefix('!').lstrip()
    return label if label.strip() else '', start


@dataclass
class InlinePreview:
    link: MarkdownLink
    preview: HLILLine
    label: str
    edit_position: int
    layout: QTextLayout
    width: float
    height: float
    slot_font: QFont
    baseline: float
    text_top: float
    code_rectangle: QRectF
    theme: PreviewTheme
    code: CodePreviewLayout


def _inline_preview(link, preview, label, edit_position, label_font, theme, code):
    layout = code.title_layout or QTextLayout('', label_font)
    rectangle = QRectF(0, 0, code.width, code.height)
    return InlinePreview(link, preview, label, edit_position, layout,
                         code.width + 2, code.height, label_font,
                         code.height / 2, code.header_height + 3, rectangle, theme, code)


# Larger embeds are laid out on the preparation pool instead of the GUI thread.
_SYNCHRONOUS_LINES = 128


class Projection(Enum):
    """How one in-BV link is shown in the note."""

    SOURCE = auto()     # Markdown source: being edited, unresolved, or never yet prepared
    PREPARING = auto()  # layout preparing off-thread; keeps its previous embed meanwhile
    PROJECTED = auto()  # shown as an embed with a ready layout


@dataclass(frozen=True)
class _EmbedSettings:
    width: float
    theme: PreviewTheme
    code_font: QFont
    show_addresses: bool
    cache: dict  # layout key -> Future[CodePreviewLayout], shared with table cells


def _completed(value):
    future = Future()
    future.set_result(value)
    return future


def _identities(links):
    """Stable link identities across edits: (url, expanded, mode, occurrence of that triple)."""
    seen, result = {}, {}
    for link in links:
        triple = link.url, link.expand_block, link.il_mode
        seen[triple] = seen.get(triple, -1) + 1
        result[link] = (*triple, seen[triple])
    return result


class _DocumentLinks(QObject):
    """Shared layout state: editing a link in any view exposes its source."""

    def __init__(self, highlighter):
        super().__init__(highlighter)
        self.highlighter = highlighter
        self.clients = WeakSet()
        self.providers = WeakSet()
        self.entries = ()
        self._refreshing = False
        self._key = None
        self._shown = {}  # link identity -> InlinePreview currently projected
        self._pending = {}
        pending = self._pending
        # Futures can be shared by other notes; discard this subscriber only.
        self.destroyed.connect(lambda *_: pending.clear())
        self.geometry = VerticalIndex()
        self.load_targets = VerticalIndex()
        self._geometry_source_key = None
        layout = highlighter.document().documentLayout()
        if hasattr(layout, 'geometryChanged'):
            layout.geometryChanged.connect(self._prepare_geometry)
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._poll_visible)
        highlighter.analysisChanged.connect(self.refresh)
        highlighter.formatsChanged.connect(self.refresh)

    def register(self, client):
        self.clients.add(client)
        provider = client.provider
        if provider is not None and provider not in self.providers:
            provider.changed.connect(self.refresh)
            self.providers.add(provider)
        self.refresh()

    def unregister(self, client):
        self.clients.discard(client)
        self.refresh()

    def _poll_visible(self):
        for client in list(self.clients):
            if isValid(client) and isValid(client.editor) and client.editor.isVisible():
                client._load_visible()

    def refresh(self, *_args):
        if self._refreshing or not isValid(self.highlighter):
            return
        self._refreshing = True
        try:
            self._refresh()
        finally:
            self._refreshing = False

    def _refresh(self):
        for client in list(self.clients):
            if not isValid(client) or not isValid(client.editor):
                self.clients.discard(client)
        for provider in list(self.providers):
            if (not isValid(provider)
                    or not any(client.provider is provider for client in self.clients)):
                if isValid(provider):
                    provider.changed.disconnect(self.refresh)
                self.providers.discard(provider)
        clients = [client for client in self.clients
                   if client.editor.isVisible() and client.provider is not None
                   and isValid(client.provider)]
        highlighter = self.highlighter
        if not clients:
            self._clock.stop()
            return  # Keep the entire display list warm while the tab is hidden.
        if any(client.selecting for client in clients):
            return  # Never reflow under a held mouse button; its release refreshes.
        if not highlighter.analysis_is_current:
            return  # Hold the last projection until analysis catches up with the edit.
        analysis = highlighter.analysis
        if highlighter.warning or not analysis.links:
            self.entries = ()
            self._shown = {}
            self.geometry = VerticalIndex()
            self.load_targets = VerticalIndex()
            self._key = None
            self._clock.stop()
            highlighter.set_link_previews(())
            return
        if not self._clock.isActive():
            self._clock.start()
        editor = clients[0].editor
        document = highlighter.document()
        # An embed may span the whole text column, like code blocks and quotes.
        width = max(64, min(client.editor.viewport().width()
                           - document.documentMargin() * 2 for client in clients))
        tables = analysis.tables if any(client.fold_tables for client in clients) else ()
        candidates = []
        for link in analysis.links:
            if not 0 <= link.start < link.end <= document.characterCount() - 1:
                continue
            if any(table.start_position <= link.start <= table.end_position for table in tables):
                continue
            if any(client.is_editing(link) for client in clients):
                continue
            raw = _source_slice(document, link.start, link.end)
            if any(character in raw for character in ('\n', '\u2028', '\u2029')):
                continue
            previews = [client.provider.request(link.url, load=client.is_in_view(link),
                                                 expand_block=link.expand_block,
                                                 il_mode=link.il_mode)
                        for client in clients]
            # Surfaces of one file can use different data views; project against the right one.
            if any(item is None for item in previews):
                continue
            if len({item.address for item in previews}) != 1:
                continue
            preview = next((item for item in previews if item.available), previews[0])
            candidates.append((link, preview))
        kinds = {kind for _link, preview in candidates for kind in preview.token_kinds}
        theme = preview_theme(editor, kinds=kinds | {68})
        show_addresses = clients[0].show_addresses
        key = (id(analysis), tuple((link, id(preview)) for link, preview in candidates),
               width, document.defaultFont().toString(),
               highlighter.code_font.toString(), editor.palette().cacheKey(), theme.key,
               show_addresses, tuple(sorted(id(client.provider) for client in clients)))
        if key == self._key:
            return
        anchors = [client.screen_anchor() for client in clients]
        settings = _EmbedSettings(width=max(32, width - 2), theme=theme,
                                  code_font=QFont(highlighter.code_font),
                                  show_addresses=show_addresses,
                                  cache=getattr(clients[0].provider, 'prepared_layouts', {}))
        identities = _identities(analysis.links)
        entries = []
        for link, preview in candidates:
            state, entry = self._project(link, preview, settings,
                                         self._shown.get(identities[link]))
            if state is not Projection.SOURCE:
                entries.append(entry)
        self.entries = tuple(entries)
        self._shown = {identities[entry.link]: entry for entry in entries}
        self._key = key
        highlighter.set_link_previews((entry.link.start, entry.link.end, entry.width,
                                       entry.slot_font, entry.height, entry.baseline)
                                      for entry in self.entries)
        self._prepare_geometry()
        for anchor in anchors:
            if anchor is not None:
                anchor.restore()
        for client in clients:
            client.editor.viewport().update()

    def _project(self, link, preview, settings, previous):
        """Decide how one link is shown; an embed only ever appears fully prepared."""
        highlighter = self.highlighter
        label, position = _label(highlighter, link)
        font = highlighter.text_format_at(position).font().resolve(
            highlighter.document().defaultFont(),
        )
        key = (preview, settings.code_font.key(), settings.width, settings.theme.key,
               settings.show_addresses, label, font.key())
        future = settings.cache.get(key)
        if future is None and len(preview.display_lines) <= _SYNCHRONOUS_LINES:
            future = _completed(CodePreviewLayout(
                preview, settings.code_font, settings.width, settings.theme,
                show_addresses=settings.show_addresses, label=label, label_font=QFont(font),
            ))
            settings.cache[key] = future
        if future is not None and future.done():
            try:
                code = future.result()
            except Exception:
                code = None
            if code is not None:
                return Projection.PROJECTED, _inline_preview(link, preview, label, position,
                                                             font, settings.theme, code)
        elif key not in self._pending:
            submitted = preparation_pool().submit(
                self, key, CodePreviewLayout, preview, settings.code_font, settings.width,
                settings.theme, show_addresses=settings.show_addresses, label=label,
                label_font=QFont(font), cache=settings.cache, cache_key=key,
            )
            if submitted is not None:
                self._pending[key] = submitted
        if previous is None:
            return Projection.SOURCE, None
        # Preparing: keep showing the previous embed of this link, at its new position.
        return Projection.PREPARING, replace(previous, link=link, edit_position=position)

    def preparation_finished(self, key, _future):
        if self._pending.pop(key, None) is not None:
            self._key = None
            self.refresh()

    def _prepare_geometry(self):
        document = self.highlighter.document()
        layout = document.documentLayout()
        if not hasattr(layout, 'geometryChanged'):
            return
        if layout.progressive:
            key = id(self.highlighter.analysis), id(self.entries)
            if key != self._geometry_source_key:
                self._geometry_source_key = key
                self.geometry = VerticalIndex((entry.link.start, entry.link.end, entry)
                                              for entry in self.entries)
                self.load_targets = VerticalIndex((link.start, link.end, link)
                                                   for link in self.highlighter.analysis.links)
            return
        layout.documentSize()
        items = []
        for entry in self.entries:
            block = document.findBlock(entry.link.end - 1)
            if not block.isValid() or not block.isVisible():
                continue
            bounds = layout.presentation_rect(block)
            position = entry.link.end - 1 - block.position()
            line = block.layout().lineForTextPosition(position)
            if not line.isValid():
                continue
            x = min(line.cursorToX(position)[0], line.cursorToX(position + 1)[0])
            rectangle = QRectF(bounds.left() + x,
                               bounds.top() + line.y() + line.ascent() - entry.baseline,
                               entry.width, entry.height)
            items.append((rectangle.top(), rectangle.bottom(), (entry, rectangle)))
        self.geometry = VerticalIndex(items)
        targets = []
        for link in self.highlighter.analysis.links:
            block = document.findBlock(link.start)
            if block.isValid() and block.isVisible():
                bounds = layout.presentation_rect(block)
                targets.append((bounds.top(), bounds.bottom(), link))
        self.load_targets = VerticalIndex(targets)


class Gesture(Enum):
    """A left-button gesture on an editor's links, from press to release."""

    IDLE = auto()
    FOLLOWING = auto()  # a modified press followed a link; caret and scroll held until release
    REVEALING = auto()  # a press revealed an embed's source; its release is swallowed
    # Freeze the projection while selecting, so embeds don't move text under the pointer.
    SELECTING = auto()


class LinkPresentation(QObject):
    def __init__(self, editor, *, follow, fold_tables=False):
        super().__init__(editor)
        self.editor = editor
        self.follow = follow
        self.fold_tables = fold_tables
        self.provider = None
        self.preferences = None
        self.controller = None
        self._gesture = Gesture.IDLE
        self._held = None           # NavigationState restored through a FOLLOWING gesture
        self._screen_anchor = None  # click anchor for editors without a pixel view anchor
        self._generation = 0        # invalidates deferred restores of a superseded gesture
        self._hover_point = None
        self.hint = FollowHint(editor.viewport())
        self._timer = single_shot(self, self.refresh)
        self._load_timer = single_shot(self, self._load_visible)
        editor.installEventFilter(self)
        editor.viewport().installEventFilter(self)
        editor.viewport().setMouseTracking(True)
        editor.cursorPositionChanged.connect(self._cursor_changed)
        editor.selectionChanged.connect(self.refresh)
        editor.updateRequest.connect(self._scrolled)
        watch_focus(editor, self._view_changed)

    def bind(self, highlighter, provider=None, *, preferences=None):
        if self.controller is not None and isValid(self.controller):
            self.controller.unregister(self)
        if self.preferences is not None and isValid(self.preferences):
            self.preferences.changed.disconnect(self._preference_changed)
        self.preferences = preferences
        if preferences is not None:
            preferences.changed.connect(self._preference_changed)
        self.provider = provider
        self.controller = None
        self._end_gesture()
        self._hover_point = None
        self.hint.hide()
        if highlighter is not None:
            controller = getattr(highlighter, '_inline_links', None)
            if controller is None:
                controller = _DocumentLinks(highlighter)
                highlighter._inline_links = controller
            self.controller = controller
            controller.register(self)

    @property
    def show_addresses(self):
        return (self.preferences.effective('hlil_show_addresses')
                if self.preferences is not None else True)

    def _preference_changed(self, name):
        if name == 'hlil_show_addresses':
            self.refresh()

    def refresh(self, *_args):
        if (isValid(self.editor) and self.controller is not None
                and isValid(self.controller)):
            self.controller.refresh()

    def _view_changed(self, *_args):
        if isValid(self.editor) and isValid(self._timer) and self.editor.isVisible():
            self._timer.start(0)

    def _scrolled(self, *_args):
        if self.editor.isVisible() and self.provider is not None:
            self._load_timer.start(0)

    def _load_visible(self):
        if not self.editor.isVisible() or self.provider is None or not isValid(self.provider):
            return
        controller = self.controller
        if controller is None or not isValid(controller):
            return
        if hasattr(self.editor.document().documentLayout(), 'geometryChanged'):
            top = -self.editor.contentOffset().y()
            layout = self.editor.document().documentLayout()
            if layout.progressive:
                first = layout.block_at_y(top)
                last = layout.block_at_y(top + self.editor.viewport().height())
                interval = first.position(), last.position() + last.length()
            else:
                interval = top, top + self.editor.viewport().height()
            links = controller.load_targets.between(*interval)
        else:
            links = (entry.link for entry, _rectangle in self.rectangles())
        changed = False
        for link in links:
            previous = self.provider.request(link.url, load=False, expand_block=link.expand_block,
                                             il_mode=link.il_mode)
            current = self.provider.request(link.url, expand_block=link.expand_block,
                                            il_mode=link.il_mode)
            changed |= ((previous.address if previous is not None else None)
                        != (current.address if current is not None else None))
        if changed:
            self.refresh()

    def _cursor_changed(self):
        if not isValid(self.editor):
            return
        self.refresh()

    def is_editing(self, link):
        cursor = self.editor.textCursor()
        if (cursor.hasSelection() and cursor.selectionStart() < link.end
                and cursor.selectionEnd() > link.start):
            return True
        popup = QApplication.activePopupWidget()
        while popup is not None and isinstance(popup.parentWidget(), QMenu):
            popup = popup.parentWidget()
        focused = (self.editor.hasFocus()
                   or popup is not None and popup.parentWidget() is self.editor)
        return (focused and not self.editor.isReadOnly()
                and link.start <= cursor.position() < link.end)

    def is_in_view(self, link):
        block = self.editor.document().findBlock(link.start)
        if not block.isValid() or not block.isVisible():
            return False
        bounds = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset())
        return bounds.bottom() >= 0 and bounds.top() <= self.editor.viewport().height()

    def rectangles(self):
        if (not isValid(self.editor) or self.controller is None
                or not isValid(self.controller)):
            return
        document_layout = self.editor.document().documentLayout()
        candidates = self.controller.entries
        if getattr(document_layout, 'progressive', False):
            top = -self.editor.contentOffset().y()
            first = document_layout.block_at_y(top)
            last = document_layout.block_at_y(top + self.editor.viewport().height())
            candidates = self.controller.geometry.between(first.position(),
                                                          last.position() + last.length())
        elif hasattr(document_layout, 'geometryChanged'):
            offset = self.editor.contentOffset()
            viewport = QRectF(self.editor.viewport().rect())
            for entry, rectangle in self.controller.geometry.between(
                -offset.y(), viewport.height() - offset.y(),
            ):
                projected = rectangle.translated(offset)
                if projected.intersects(viewport):
                    yield entry, projected
            return
        for entry in candidates:
            block = self.editor.document().findBlock(entry.link.end - 1)
            if not block.isValid() or not block.isVisible():
                continue
            bounds = self.editor.blockBoundingGeometry(block).translated(
                self.editor.contentOffset(),
            )
            position = entry.link.end - 1 - block.position()
            line = block.layout().lineForTextPosition(position)
            if not line.isValid():
                continue
            x = min(line.cursorToX(position)[0], line.cursorToX(position + 1)[0])
            rectangle = QRectF(bounds.left() + x,
                               bounds.top() + line.y() + line.ascent() - entry.baseline,
                               entry.width, entry.height)
            if rectangle.intersects(QRectF(self.editor.viewport().rect())):
                yield entry, rectangle

    def hit_test(self, point):
        return next((entry for entry, rectangle in self.rectangles() if rectangle.contains(point)),
                    None)

    @property
    def preserving_view(self):
        return (self._gesture in (Gesture.FOLLOWING, Gesture.REVEALING)
                or self._screen_anchor is not None)

    @property
    def selecting(self):
        return self._gesture is Gesture.SELECTING

    def _end_gesture(self):
        self._gesture = Gesture.IDLE
        self._held = None
        self._screen_anchor = None
        self._generation += 1

    def screen_anchor(self):
        """A screen position to hold through a projection change (cell editors only)."""
        if hasattr(self.editor, 'pin'):
            return None
        return self._screen_anchor or ViewportAnchor.capture(self.editor)

    def _raw_address(self, point):
        if self.controller is None:
            return None
        highlighter = self.controller.highlighter
        highlighter.refresh()
        position = self.editor.cursorForPosition(point.toPoint()).position()
        for link in highlighter.analysis.links:
            if link.start <= position < link.end:
                resolver = getattr(self.editor, 'resolve_link', None)
                if resolver is not None:
                    return resolver(link.url)
                if self.provider is not None:
                    preview = self.provider.request(link.url, load=False,
                                                    expand_block=link.expand_block,
                                                    il_mode=link.il_mode)
                    return preview.address if preview is not None else None
        return None

    def _hold(self):
        """Enter FOLLOWING: the caret, selection and scroll survive the gesture."""
        self._end_gesture()
        self._gesture = Gesture.FOLLOWING
        self._held = NavigationState(self.editor)

    def _follow(self, address):
        self._hold()
        timer = getattr(self.editor, '_caret_timer', None)
        if timer is not None:
            timer.stop()
        self.follow(address)
        self._held.restore()

    def _finish_follow(self):
        held, generation = self._held, self._generation
        held.restore()

        def restore():
            # Host navigation can echo back on the next event-loop turn.
            if isValid(self) and generation == self._generation:
                held.restore()
                self._end_gesture()
        QTimer.singleShot(0, restore)

    def _reveal(self, entry, event):
        self._pin_reveal(entry, event.position())
        self._gesture = Gesture.REVEALING
        cursor = self.editor.textCursor()
        mode = (QTextCursor.MoveMode.KeepAnchor
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                else QTextCursor.MoveMode.MoveAnchor)
        cursor.setPosition(entry.edit_position, mode)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self.refresh()
        if self._screen_anchor is not None:
            self._screen_anchor.restore()

    def hold_around(self, entry, point):
        """Keep the text around `entry` still while its source is revealed."""
        self._pin_reveal(entry, point)

    def select_source(self, start, end):
        """Select `start`..`end` of the source, revealing any link it touches."""
        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self.refresh()
        if self._screen_anchor is not None:
            self._screen_anchor.restore()

    def _pin_reveal(self, entry, point):
        """Keep the text around the revealed source still, however the layout settles."""
        self._end_gesture()
        hold = reveal_hold(self.editor, entry, point.y(), visible_rect(self.editor.viewport()))
        pin = getattr(self.editor, 'pin', None)
        if pin is not None:
            pin(hold.position, hold.y, bottom=hold.bottom)
            return
        generation = self._generation
        self._screen_anchor = ViewportAnchor.holding(self.editor, hold)

        def restore():
            if isValid(self) and generation == self._generation and self._screen_anchor:
                self._screen_anchor.restore()
                self._screen_anchor = None
        QTimer.singleShot(0, restore)

    def cursor_for_position(self, point):
        entry = self.hit_test(QPointF(point))
        if entry is None:
            return self.editor.cursorForPosition(point)
        self._pin_reveal(entry, QPointF(point))
        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(entry.edit_position)
        return cursor

    def paint(self, event):
        painter = QPainter(self.editor.viewport())
        painter.setClipRegion(event.region())
        hint = None
        visible = visible_rect(self.editor.viewport())
        for entry, rectangle in self.rectangles():
            code = entry.code_rectangle.translated(rectangle.topLeft())
            hovered = self._hover_point is not None and code.contains(self._hover_point)
            entry.code.draw(painter, code.topLeft(), hovered=hovered,
                             visible=visible)
            if hovered:
                hint = code, entry.theme
        painter.end()
        if hint is not None:
            self.hint.set_preview(*hint)
        else:
            self.hint.set_preview()

    def eventFilter(self, watched, event):
        if not isValid(self.editor):
            return False
        kind = event.type()
        if watched is self.editor:
            if kind == QEvent.Type.Hide:
                self._hover_point = None
                self.hint.hide()
                self._timer.stop()
                self._load_timer.stop()
                self.refresh()
            if kind in (QEvent.Type.FocusOut, QEvent.Type.Hide) and self.selecting:
                self._end_gesture()  # the release may never arrive
            if kind in (QEvent.Type.FocusIn, QEvent.Type.FocusOut, QEvent.Type.Show,
                        QEvent.Type.Hide, QEvent.Type.PaletteChange, QEvent.Type.FontChange):
                self._view_changed()
            elif kind in (QEvent.Type.KeyPress, QEvent.Type.InputMethod):
                self._end_gesture()
                self.refresh()
        # Native bindings can invalidate a cached viewport wrapper; resolve it again.
        elif watched is self.editor.viewport():
            if kind in (QEvent.Type.Resize, QEvent.Type.Move):
                self._view_changed()
            elif kind == QEvent.Type.ToolTip:
                entry = self.hit_test(QPointF(event.pos()))
                if entry is not None:
                    text = ((f'{entry.label}\n' if entry.label else '')
                            + f'{entry.preview.il_mode.label} at {entry.preview.address:#x}\n'
                            f'{entry.preview.display_text}')
                    text += f'\n\nClick to edit; {follow_modifier()}-click to follow.'
                    QToolTip.showText(event.globalPos(), text, self.editor.viewport())
                    return True
            elif kind == QEvent.Type.MouseMove:
                self._hover_point = event.position()
                self.editor.viewport().update()
                if self._gesture is Gesture.FOLLOWING:
                    return True
            elif kind == QEvent.Type.Leave and self._hover_point is not None:
                self._hover_point = None
                self.hint.hide()
                self.editor.viewport().update()
            elif (kind == QEvent.Type.MouseButtonRelease
                  and event.button() == Qt.MouseButton.LeftButton):
                if self._gesture is Gesture.FOLLOWING:
                    self._finish_follow()
                    return True
                if self._gesture is Gesture.REVEALING:
                    self._gesture = Gesture.IDLE
                    return True
                if self.selecting:
                    # Let the editor finish the selection first, then reflow.
                    self._end_gesture()
                    self._timer.start(0)
            elif (kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
                  and event.button() == Qt.MouseButton.LeftButton):
                entry = self.hit_test(event.position())
                modified = event.modifiers() & (
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
                )
                if modified or entry is not None and self.editor.isReadOnly():
                    address = entry.preview.address if entry is not None else self._raw_address(
                        event.position(),
                    )
                    if address is not None:
                        self._follow(address)
                        return True
                    if (self.controller is not None
                            and not self.controller.highlighter.analysis_is_current):
                        cursor = self.editor.cursorForPosition(event.position().toPoint())
                        if 'binaryninja:' in cursor.block().text().lower():
                            # A follow gesture before parsing finishes must not move the caret.
                            self._hold()
                            return True
                self._end_gesture()
                if entry is not None:
                    self._reveal(entry, event)
                    return True
                self._gesture = Gesture.SELECTING
        return False
