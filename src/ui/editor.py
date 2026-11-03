from __future__ import annotations

import itertools
import re
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, field
from weakref import ref

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFontMetricsF,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QTextCursor,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import QHBoxLayout, QWidget
from shiboken6 import isValid

from ..markdown import new_table, source_line_positions, utf16_length
from .common import (
    DOCUMENT_MARGIN,
    OutlineButton,
    SmoothScrollBar,
    VerticalIndex,
    ViewportButton,
    build_edit_menu,
    mix,
)
from .embeds import (
    LinkPresentation,
    add_embed_actions,
    context_target,
    leave_link_source,
    source_link_at,
    spine_path,
)
from .formatting import QuotePresentation, TextActions, continue_quote, toggle_inline_selection
from .highlighter import code_block_colors
from .layout import (
    CODE_BLOCK_VERTICAL_PADDING,
    QUOTE_INDENT,
    TABLE_MARGIN,
    PixelDocumentLayout,
    PixelTextEdit,
)
from .session import block_writers, note_people, writer_label
from .tables import LEFT_RAIL, SIDE_RAIL, TableCard

_LIST = re.compile(
    r"^(?P<indent>[ \t]*)(?P<quote>(?:>[ \t]*)*)"
    r"(?:(?P<bullet>[-+*])|(?P<number>\d+)(?P<separator>[.)]))"
    r"[ \t]+(?P<task>\[[ xX]\][ \t]+)?(?P<body>.*)$"
)


def _drop_preview_provider(registry_ref, owner):
    registry = registry_ref()
    if registry is not None and isValid(registry):
        registry.set_preview_provider(owner, None, defer=True)


@dataclass
class _TypingRun:
    """Keystrokes of one kind, continuing at the caret, that undo as one step."""
    kind: object
    position: int = -1
    token: object = field(default_factory=object)

    def continued_by(self, kind, cursor) -> bool:
        return (kind is not None and kind == self.kind and not cursor.hasSelection()
                and cursor.position() == self.position)


# Writers' bar colors, as RGB: people take primaries in turn, agents secondaries by vendor.
PEOPLE_HUES = ((86, 140, 220), (214, 88, 88), (84, 170, 98))  # Blue, red, green.
CLAUDE_HUE = (226, 140, 64)    # Orange.
OPENAI_HUE = (156, 104, 222)   # Purple.
AGENT_HUE = (212, 180, 52)     # Yellow: any other agent.
_CLAUDE = re.compile(r'claude|anthropic|\b(?:opus|sonnet|haiku)\b', re.IGNORECASE)
_OPENAI = re.compile(r'openai|chatgpt|codex|davinci|\bgpt|\bo[1-9](?:\b|-)'
                     r'|\b(?:astra|terra|luna|sol)\b', re.IGNORECASE)


def agent_hue(name):
    """Orange for Claude models, purple for OpenAI's, yellow for any other agent."""
    if _CLAUDE.search(name):
        return CLAUDE_HUE
    return OPENAI_HUE if _OPENAI.search(name) else AGENT_HUE


def writer_hue(agent, user, people):
    """A writer's hue. `people` is `note_people`'s order; its first, the local person, also
    takes lines with no Enterprise user."""
    if agent:
        return agent_hue(agent)
    index = people.index(user) if user is not None and user in people else 0
    return PEOPLE_HUES[index % len(PEOPLE_HUES)]


def author_color(palette, hue, *, strong=False):
    """A writer's muted bar color against the editor background; `strong` when hovered."""
    base = palette.base().color()
    amount = (0.55 if base.lightness() < 128 else 0.45) + (0.25 if strong else 0)
    return mix(base, QColor(*hue), amount)


def paint_author_bar(painter, left, width, run, color):
    """A rounded rule in `run`'s first writer's color; each later writer's color grows in from
    the left edge across their boundary on a curve, as an embed's spine widens.

    `color(hue)` is the QColor for a section's hue."""
    frame = QRectF(left, run.top + 2, width, max(1, run.bottom - run.top - 4))
    outline = QPainterPath()
    outline.addRoundedRect(frame, width / 2, width / 2)
    painter.save()
    painter.setClipPath(outline)
    painter.fillRect(frame, color(run.sections[0][2]))

    def clamp(y):
        return min(max(y, frame.top()), frame.bottom())

    for previous, (top, bottom, hue) in itertools.pairwise(run.sections):
        # Eased within the two sections either side, so short sections still show.
        boundary = clamp(top)
        painter.fillPath(spine_path(left, [(clamp(previous[0]), boundary, 0),
                                           (boundary, clamp(bottom), width),
                                           (clamp(bottom), frame.bottom(), width)]),
                         color(hue))
    painter.restore()


@dataclass(frozen=True)
class AuthorBar:
    """One rule of the author bar: `sections` of (top, bottom, hue) by color, top to bottom,
    and `writers`, the (label, hue) of everyone in it in the order they appear."""
    top: float
    bottom: float
    sections: tuple
    writers: tuple

    @property
    def label(self):
        return ' & '.join(label for label, _hue in self.writers)


class AuthorTag(QWidget):
    """The name that slides out of a hovered author bar, over the text's left margin."""

    DURATION = 160
    PADDING = 8

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.writers, self.progress = (), 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._animation = QVariantAnimation(self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._set_progress)
        self._animation.finished.connect(lambda: self.setVisible(self.progress > 0))
        self.hide()

    def _font(self):
        font = self.editor.font()
        font.setPointSizeF(max(9.0, font.pointSizeF() * 0.85))
        return font

    @property
    def text(self):
        return ' & '.join(label for label, _hue in self.writers)

    def size_for(self, writers):
        metrics = QFontMetricsF(self._font())
        text = ' & '.join(label for label, _hue in writers)
        return QSize(round(metrics.horizontalAdvance(text)) + self.PADDING * 2,
                     round(metrics.height()) + 6)

    def present(self, writers, left, top):
        """Slide out the (label, agent) `writers` with the left edge at `left`, top at `top`."""
        if writers != self.writers:
            self.writers = writers
            self.update()
        self.setGeometry(QRect(QPoint(round(left), round(top)), self.size_for(writers)))
        self.show()
        self.raise_()
        self._slide(1.0)

    def retract(self):
        self._slide(0.0)

    def _slide(self, target):
        running = self._animation.state() == QVariantAnimation.State.Running
        if (self._animation.endValue() == target if running else self.progress == target):
            return
        self._animation.stop()
        self._animation.setDuration(max(1, round(self.DURATION * abs(target - self.progress))))
        self._animation.setStartValue(self.progress)
        self._animation.setEndValue(target)
        self._animation.start()

    def _set_progress(self, value):
        self.progress = float(value)
        self.update()

    def paintEvent(self, _event):
        if not self.writers:
            return
        palette = self.editor.palette()
        accent = author_color(palette, self.writers[0][1], strong=True)
        width = self.width() * self.progress
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(min(1.0, self.progress * 1.5))
        # Revealed left to right, as if drawn out of the bar beside it.
        painter.setClipRect(QRectF(0, 0, width, self.height()))
        frame = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        painter.setPen(QPen(mix(palette.base().color(), accent, 0.5), 1))
        painter.setBrush(mix(palette.base().color(), accent, 0.16))
        painter.drawRoundedRect(frame, 5, 5)
        font = self._font()
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        # The names trail the reveal slightly, so they slide rather than wipe in. Each is
        # tinted with its own bar's color.
        left = self.PADDING - (1 - self.progress) * 12
        for index, (label, hue) in enumerate(self.writers):
            tint = author_color(palette, hue, strong=True)
            for text, color in ((' & ', palette.text().color()) if index else (None, None),
                                (label, mix(palette.text().color(), tint, 0.25))):
                if text is None:
                    continue
                painter.setPen(color)
                painter.drawText(QRectF(left, 0, self.width(), self.height()),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
                left += metrics.horizontalAdvance(text)


class AuthorGutter(QWidget):
    """The author bar left of the editor's viewport: a rule beside each paragraph's lines,
    colored by who wrote them and handing off between writers on a curve. Hovering a bar
    slides out who wrote it."""

    WIDTH = 12
    BAR = 3
    LEFT = WIDTH - BAR - 1  # As close to the text as the margin allows.
    GAP = 5  # Between a bar and the name it slides out.

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.tag = AuthorTag(editor)
        self._hover_y = None
        self._people = ()
        self.setObjectName('bnnotes-author-gutter')
        self.setMouseTracking(True)
        self.hide()

    def runs(self):
        """`AuthorBar`s beside the visible text: one per paragraph (consecutive lines, table rows
        or a code block), whoever wrote its lines, linked across the blank lines between
        consecutive paragraphs that one writer wrote alone."""
        editor = self.editor
        self._people = note_people(editor.document())
        offset, height = editor.contentOffset().y(), editor.viewport().height()
        segments, block = [], editor.firstVisibleBlock()
        while block.isValid():
            rectangle = editor.blockBoundingGeometry(block).translated(0, offset)
            if rectangle.top() > height:
                break
            if block.text().strip() and rectangle.height() > 0:
                segments.append(self._segment(rectangle.top(), rectangle.bottom(), block))
            block = block.next()
        segments += [row for card in editor.table_cards.values() if card.isVisible()
                     for row in self._table_rows(card)]
        paragraphs = []
        for top, bottom, pieces in sorted(segments, key=lambda segment: segment[0]):
            if paragraphs and top <= paragraphs[-1][1] + 1:
                paragraphs[-1][1] = max(paragraphs[-1][1], bottom)
                paragraphs[-1][2].extend(pieces)
            else:
                paragraphs.append([top, bottom, list(pieces)])
        bars = []
        for top, bottom, pieces in paragraphs:
            # Paragraphs one writer wrote link up with the next by them; a paragraph with
            # several writers keeps its own bar.
            writers = {writer for _top, _bottom, writer in pieces}
            if bars and len(writers) == 1 and bars[-1][3] == writers:
                bars[-1][1] = bottom
                bars[-1][2].extend(pieces)
            else:
                bars.append([top, bottom, pieces, writers if len(writers) == 1 else None])
        return [self._bar(top, bottom, pieces) for top, bottom, pieces, _writers in bars]

    def _segment(self, top, bottom, block):
        """A line's extent split evenly between its writers, oldest at the top:
        (top, bottom, [(top, bottom, (label, hue))])."""
        writers = [(writer_label(agent, user), writer_hue(agent, user, self._people))
                   for agent, user in block_writers(block)]
        step = (bottom - top) / len(writers)
        return top, bottom, [(top + index * step, top + (index + 1) * step, writer)
                             for index, writer in enumerate(writers)]

    @staticmethod
    def _bar(top, bottom, pieces):
        sections, writers = [], []
        for piece_top, piece_bottom, writer in pieces:
            if writer not in writers:
                writers.append(writer)
            if sections and sections[-1][2] == writer[1]:
                sections[-1] = (sections[-1][0], piece_bottom, writer[1])
            else:
                # A gap between linked paragraphs belongs to the writer above it.
                if sections:
                    sections[-1] = (sections[-1][0], piece_top, sections[-1][2])
                sections.append((piece_top, piece_bottom, writer[1]))
        return AuthorBar(top, bottom, tuple(sections), tuple(writers))

    def _table_rows(self, card):
        """Segments beside a rendered table's visible rows; its source lines are folded away."""
        model, view = card.model, card.view
        document = self.editor.document()
        base = document.findBlock(model.start).blockNumber()
        clip = QRect(view.viewport().mapTo(self.editor.viewport(), QPoint(0, 0)),
                     view.viewport().size())
        rows = []
        for row in range(model.rowCount()):
            top = clip.top() + view.rowViewportPosition(row)
            bottom = min(top + view.rowHeight(row), clip.bottom())
            top = max(top, clip.top())
            if bottom > top:
                block = document.findBlockByNumber(base + (row + 1 if row else 0))
                rows.append(self._segment(top, bottom, block))
        return rows

    def run_at(self, y):
        return next((run for run in self.runs() if run.top - 2 <= y <= run.bottom + 2), None)

    def label_at(self, y):
        """The slide-out text for the bar at `y`."""
        run = self.run_at(y)
        return None if run is None else run.label

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), self.editor.palette().base())
        if self.editor.session is None:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        hovered, palette = self._present(), self.editor.palette()
        for run in self.runs():
            paint_author_bar(painter, self.LEFT, self.BAR, run,
                             lambda hue, strong=run == hovered: author_color(palette, hue,
                                                                             strong=strong))

    def _present(self):
        """Slide out the hovered run's writer (or retract); the run, if any."""
        run = self.run_at(self._hover_y) if self._hover_y is not None else None
        if run is None:
            self.tag.retract()
            return None
        # Always the same place relative to the bar: its top, GAP to its right.
        height = self.tag.size_for(run.writers).height()
        top = max(0, min(run.top + 2, self.height() - height))
        self.tag.present(run.writers, self.x() + self.LEFT + self.BAR + self.GAP, self.y() + top)
        return run

    def mouseMoveEvent(self, event):
        self._hover_y = event.position().y()
        self.update()

    def leaveEvent(self, event):
        self._hover_y = None
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._hover_y = None
        self.tag.retract()
        super().hideEvent(event)


class MarkdownEditor(PixelTextEdit):
    addressActivated = Signal(object)
    CONTROLS_INSET = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self.resolve_link = lambda _url: None
        self.preview_provider = None
        self.table_cards = {}
        self._table_models = {}
        self._active_table = None
        self._positioning_tables = False
        self._preparing_chrome = False
        self._code_index = VerticalIndex()
        self._table_index = VerticalIndex()
        self._table_tops = {}
        self._chrome_source_key = None
        self._visible_cards = set()
        self._routing_suspended = False  # see _without_routing
        self._previous_cursor_position = 0
        self._typing: _TypingRun | None = None
        self.setObjectName("bnnotes-editor")
        self.setAccessibleName("Markdown note")
        self.setPlaceholderText("# Analysis notes\n\nStart writing Markdown…")
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTabChangesFocus(False)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFrameShape(self.Shape.NoFrame)
        self.setHorizontalScrollBar(SmoothScrollBar(Qt.Orientation.Horizontal, self))
        self.setVerticalScrollBar(SmoothScrollBar(Qt.Orientation.Vertical, self))
        self._controls_overlay = QWidget(self)
        # Clicks keep the editor focused; the controls are still reachable with Tab.
        self._controls_overlay.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._controls_overlay.setFocusProxy(self)
        controls = QHBoxLayout(self._controls_overlay)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(4)
        self.table_button = ViewportButton(self._controls_overlay, symbol="table")
        self.table_button.setObjectName("bnnotes-insert-table")
        self.table_button.setToolTip("Insert table")
        self.table_button.setAccessibleName("Insert table")
        controls.addWidget(self.table_button)
        self.outline_button = OutlineButton(self._controls_overlay)
        controls.addWidget(self.outline_button)
        self.table_button.installEventFilter(self)
        self.outline_button.installEventFilter(self)
        self._position_controls()
        self.updateRequest.connect(self._position_tables)
        self.horizontalScrollBar().valueChanged.connect(self._position_tables)
        self.cursorPositionChanged.connect(self._cursor_changed)
        self.selectionChanged.connect(self._table_selection_changed)
        self._quotes = QuotePresentation(self)
        self._links = LinkPresentation(self, follow=self.addressActivated.emit, fold_tables=True)
        self.author_gutter = AuthorGutter(self)

    def set_author_bar(self, visible):
        """Show the author bar, moving the text right to make room for it."""
        if visible == self.author_gutter.isVisibleTo(self):
            return
        margins = self.viewportMargins()
        margins.setLeft(AuthorGutter.WIDTH if visible else 0)
        self.setViewportMargins(margins)
        self.author_gutter.setVisible(visible)
        self._position_controls()

    def _position_controls(self):
        viewport = self.viewport().geometry()
        if hasattr(self, 'author_gutter'):
            self.author_gutter.setGeometry(viewport.left() - AuthorGutter.WIDTH, viewport.top(),
                                           AuthorGutter.WIDTH, viewport.height())
        size = self._controls_overlay.sizeHint()
        x = viewport.left() + max(0, viewport.width() - size.width() - self.CONTROLS_INSET)
        y = viewport.top() + self.CONTROLS_INSET
        self._controls_overlay.setGeometry(x, y, size.width(), size.height())
        self._controls_overlay.raise_()

    def eventFilter(self, watched, event):
        if (watched in (self.table_button, self.outline_button)
                and event.type() == QEvent.Type.Resize):
            self._position_controls()
        return super().eventFilter(watched, event)

    def viewportEvent(self, event):
        result = super().viewportEvent(event)
        if (event.type() in (QEvent.Type.Resize, QEvent.Type.Move)
                and hasattr(self, "outline_button")):
            self._position_controls()
        return result

    def wheelEvent(self, event):
        delta = event.pixelDelta() if not event.pixelDelta().isNull() else event.angleDelta()
        horizontal = (abs(delta.x()) > abs(delta.y())
                      or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        if horizontal and not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.horizontalScrollBar().wheelEvent(event)
        elif not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.verticalScrollBar().wheelEvent(event)
        else:
            super().wheelEvent(event)

    def bind_session(self, session):
        self._quotes.bind(None)
        self._links.bind(None)
        self._controls_overlay.setFocusProxy(self)
        previous = self.session
        if previous is not None:
            previous.layout.set_view_active(self, False)
            previous.highlighter.set_view_active(id(self), False)
            previous.highlighter.analysisChanged.disconnect(self._markdown_changed)
            previous.layout.geometryChanged.disconnect(self._prepare_chrome)
            previous.tables.modelsChanged.disconnect(self._sync_tables)
            previous.tables.geometryChanged.disconnect(self._prepare_chrome)
            previous.fontChanged.disconnect(self._font_changed)
            previous.tables.set_height_limit(id(self), None)
            previous.tables.set_preview_provider(id(self), None)
        for card in self.table_cards.values():
            card.deleteLater()
        self.table_cards.clear()
        self._table_models.clear()
        self._visible_cards.clear()
        self._code_index = VerticalIndex()
        self._table_index = VerticalIndex()
        self._table_tops = {}
        self._active_table = None
        self.session = session
        if session is None:
            empty = QTextDocument(self)
            empty.setDocumentLayout(PixelDocumentLayout(empty))
            self.setDocument(empty)
            self.setReadOnly(True)
        else:
            self.setDocument(session.document)
            session.layout.set_view_active(self, self.isVisible())
            session.highlighter.set_view_active(id(self), self.isVisible())
            self.setReadOnly(session.load_failed)
            session.highlighter.analysisChanged.connect(self._markdown_changed)
            session.layout.geometryChanged.connect(self._prepare_chrome)
            session.tables.modelsChanged.connect(self._sync_tables)
            session.tables.geometryChanged.connect(self._prepare_chrome)
            session.fontChanged.connect(self._font_changed)
            self._sync_tables()
            self._quotes.bind(session.highlighter)
            self.set_link_provider(self.preview_provider)
        self._update_tab_width()
        self._position_controls()
        if session is not None:
            session.layout.settle()
        self.viewport().update()

    def set_link_provider(self, provider):
        self.preview_provider = provider
        if self.session is not None:
            self._links.bind(self.session.highlighter, provider,
                             preferences=self.session.notebook.preferences)
            self.session.tables.set_preview_provider(id(self), provider)
            registry = ref(self.session.tables)
            owner = id(self)
            self.destroyed.connect(lambda *_args, registry=registry, owner=owner:
                                   _drop_preview_provider(registry, owner))
        for card in self.table_cards.values():
            card.set_link_provider(provider)

    def _markdown_changed(self, _analysis):
        self._update_tab_width()
        self._prepare_chrome()
        self.viewport().update()

    def _prepare_chrome(self):
        if self.session is None or self._preparing_chrome:
            return
        self._preparing_chrome = True
        try:
            analysis = self.session.highlighter.analysis
            if self.session.layout.progressive:
                key = id(analysis), id(self.session.tables.models)
                if key != self._chrome_source_key:
                    self._chrome_source_key = key
                    depths = {quote.line: quote.depth for quote in analysis.quote_lines}
                    self._code_index = VerticalIndex(
                        (code.start_position, code.end_position,
                         (code, self._code_frame_left(depths.get(code.start_line, 0))))
                        for code in analysis.code_blocks
                    )
                    self._table_index = VerticalIndex(
                        (model.render_start, model.render_end, model.identity)
                        for model in self.session.tables.models
                    )
                    self._table_tops.clear()
                return
            self.session.layout.documentSize()
            offset = self.contentOffset().y()
            depths = {quote.line: quote.depth for quote in analysis.quote_lines}
            codes = []
            for code in analysis.code_blocks:
                top = self._visual_line_rect(code.start_position).top() - offset
                bottom = self._visual_line_rect(code.end_position).bottom() - offset
                top -= CODE_BLOCK_VERTICAL_PADDING
                bottom += CODE_BLOCK_VERTICAL_PADDING
                codes.append((top, bottom, (top, bottom,
                                           self._code_frame_left(depths.get(code.start_line, 0)))))
            self._code_index = VerticalIndex(codes)
            tables, tops = [], {}
            for model in self.session.tables.models:
                top = self._visual_line_rect(model.render_start).top() - offset
                tops[model.identity] = top
                tables.append((top, top + model.display_height, model.identity))
            self._table_tops = tops
            self._table_index = VerticalIndex(tables)
        finally:
            self._preparing_chrome = False
            if self.session.layout.progressive:
                self._position_tables()
        self._position_tables()

    def _visible_source_range(self):
        layout = self.session.layout
        first = self.firstVisibleBlock()
        last = layout.block_at_y(self.verticalScrollBar().value() + self.viewport().height())
        return first.position(), last.position() + last.length()

    def _font_changed(self):
        self._update_tab_width()
        self._links.refresh()
        if self.isVisible():
            self.session.tables.set_height_limit(id(self), self.viewport().height())
        self._position_tables()
        self.viewport().update()

    @property
    def active_table(self):
        if self.hasFocus() or self._active_table is None or not self._active_table.model.alive:
            return None
        return self._active_table

    def _set_active_table(self, card):
        """The table the toolbar acts on while focus is inside it (None: the prose)."""
        self._active_table = card
        self._controls_overlay.setFocusProxy(
            self if card is None else card.cell_editor or card.view)
        if card is not None:
            if not card.view.currentIndex().isValid():
                # Focus returned without choosing a cell: resume at the caret.
                model = card.model
                caret = self.textCursor().position()
                row, column = (model.cell_at_position(caret)
                               if model.start <= caret <= model.end else (0, 0))
                card.view.setCurrentIndex(model.index(row, column))
            self._follow_table_cell(card, card.view.currentIndex())

    @contextmanager
    def _without_routing(self):
        """Move the caret without routing it into a table (nests)."""
        previous, self._routing_suspended = self._routing_suspended, True
        try:
            yield
        finally:
            self._routing_suspended = previous

    def _sync_tables(self):
        if self.session is None:
            return
        identities = {model.identity for model in self.session.tables.models}
        self._table_models = {model.identity: model for model in self.session.tables.models}
        for identity in list(self.table_cards):
            if identity not in identities:
                card = self.table_cards.pop(identity)
                if card is self._active_table:
                    self._set_active_table(None)
                card.hide()
                card.deleteLater()
        if not self.session.layout.progressive:
            for model in self.session.tables.models:
                self._create_table_card(model)
        self._prepare_chrome()
        self._position_tables()
        self._table_selection_changed()
        self._cursor_changed()

    def _create_table_card(self, model):
        if model.identity not in self.table_cards:
            card = TableCard(model, self.viewport(), preview_provider=self.preview_provider)
            card.activated.connect(self._set_active_table)
            card.leaveRequested.connect(self._leave_table)
            card.view.selectionModel().currentChanged.connect(
                lambda current, _previous, card=card: self._follow_table_cell(card, current))
            self.table_cards[model.identity] = card
        return self.table_cards[model.identity]

    def _follow_table_cell(self, card, index):
        """Keep the note's caret in the source of the cell being worked on."""
        if self.hasFocus() or not index.isValid() or not card.model.alive:
            return
        ranges = card.model.table.cell_ranges
        if not (0 <= index.row() < len(ranges) and 0 <= index.column() < len(ranges[index.row()])):
            return
        start, end = ranges[index.row()][index.column()]
        current = self.textCursor()
        if start <= current.selectionStart() and current.selectionEnd() <= end:
            return  # Already there, e.g. a search match selected inside the cell.
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        bars = self.horizontalScrollBar(), self.verticalScrollBar()
        values = [bar.value() for bar in bars]
        with self._without_routing():
            self.setTextCursor(cursor)
        for bar, value in zip(bars, values, strict=True):
            bar.setValue(value)

    def _table_selection_changed(self):
        cursor = self.textCursor()
        for card in self.table_cards.values():
            selected = (cursor.hasSelection() and cursor.selectionEnd() > card.model.start
                        and cursor.selectionStart() < card.model.end)
            if selected != card.source_selected:
                card.source_selected = selected
                card.update()

    def _table_rect(self, model):
        top = self._table_tops.get(model.identity)
        top = (self._visual_line_rect(model.render_start).top() if top is None
               else top + self.contentOffset().y())
        # The grid starts on the text column; its row handles hang in the margin.
        left = DOCUMENT_MARGIN - LEFT_RAIL
        width = min(max(1, self.viewport().width() - left),
                    max(260, sum(model.widths) + LEFT_RAIL + SIDE_RAIL))
        return QRectF(left + self.contentOffset().x(), top, width, model.display_height)

    def _position_tables(self, *_args):
        if self._positioning_tables or self.session is None:
            return
        self._positioning_tables = True
        try:
            viewport = QRectF(self.viewport().rect())
            offset = self.contentOffset().y()
            interval = (self._visible_source_range() if self.session.layout.progressive
                        else (-offset, viewport.height() - offset))
            visible = set(self._table_index.between(*interval))
            for identity in self._visible_cards - visible:
                card = self.table_cards.get(identity)
                if card is not None:
                    card.hide()
            for identity in visible:
                card = self.table_cards.get(identity)
                if card is None:
                    model = self._table_models.get(identity)
                    if model is None:
                        continue
                    card = self._create_table_card(model)
                if not card.model.alive:
                    continue
                rectangle = self._table_rect(card.model)
                card.setGeometry(QRect(round(rectangle.x()), round(rectangle.y()),
                                       round(rectangle.width()), round(rectangle.height())))
                card.setVisible(rectangle.intersects(viewport))
                card.raise_()
            self._visible_cards = visible
            # A viewport sibling, above every table and independent of scrolling.
            self._position_controls()
        finally:
            self._positioning_tables = False

    def _cursor_changed(self):
        cursor = self.textCursor()
        previous = self._previous_cursor_position
        self._previous_cursor_position = cursor.position()
        if (self._routing_suspended or self.session is None
                or not self.hasFocus() or cursor.hasSelection()):
            return
        model = self.session.tables.at_render_position(cursor.position())
        if model is not None:
            if cursor.position() > model.end:
                self._ensure_table_visible(model)
                self._leave_table(self.table_cards[model.identity], True)
                return
            with self._without_routing():
                self._ensure_table_visible(model)
                card = self.table_cards[model.identity]
                if cursor.position() < previous:
                    card.focus_cell(model.rowCount() - 1, model.columnCount() - 1)
                else:
                    card.focus_cell(*model.cell_at_position(cursor.position()))

    def _ensure_table_visible(self, model):
        rectangle = self._table_rect(model)
        if rectangle.left() < 0 or rectangle.right() > self.viewport().width():
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().minimum())
        if rectangle.top() < 0 or rectangle.bottom() > self.viewport().height():
            block = self.document().findBlock(model.render_start)
            self.verticalScrollBar().setValue(round(self.blockBoundingGeometry(block).top()))
        self._position_tables()

    def show_source_cursor(self, cursor, *, focus_table=True):
        with self._without_routing():
            self.setTextCursor(cursor)
        model = self.session.tables.at_position(cursor.selectionStart()) if self.session else None
        if model is not None:
            self._ensure_table_visible(model)
            card = self.table_cards[model.identity]
            row, column = model.cell_at_position(cursor.selectionStart())
            if focus_table:
                card.focus_cell(row, column)
            else:
                card.view.scrollTo(model.index(row, column))
        else:
            self.ensureCursorVisible()

    def _leave_table(self, card, after):
        with self._without_routing():
            self.session.highlighter.refresh()
            model = card.model
            cursor = QTextCursor(self.document())
            cursor.setPosition(model.after_position if after else model.before_position)
            self.setTextCursor(cursor)
            self.setFocus()
        self.ensureCursorVisible()

    def insert_table(self, rows, columns):
        if self.session is None or self.isReadOnly():
            return
        cursor = self.textCursor()
        card = self.active_table
        start = card.model.end if card is not None else cursor.selectionStart()
        end = start if card is not None else cursor.selectionEnd()
        encoded = self.toPlainText().encode("utf-16-le")
        before = encoded[:start * 2].decode("utf-16-le")
        after = encoded[end * 2:].decode("utf-16-le")
        leading = "\n\n" if not before else "" if before.endswith("\n\n") else (
            "\n" if before.endswith("\n") else "\n\n"
        )
        trailing = "" if after.startswith("\n\n") else "\n" if after.startswith("\n") else "\n\n"
        replacement = leading + new_table(rows, columns) + trailing
        with self._without_routing():
            inserted = self.session.apply_source_edit(start, end, replacement)
            self.session.highlighter.refresh()
        position = inserted.position() - utf16_length(trailing) - 1
        model = self.session.tables.at_position(position)
        if model is not None:
            self._ensure_table_visible(model)
            self.table_cards[model.identity].focus_cell(0, 0, edit=True)

    def focusInEvent(self, event):
        mouse = event.reason() == Qt.FocusReason.MouseFocusReason
        horizontal, vertical = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        super().focusInEvent(event)
        if mouse:
            self.horizontalScrollBar().setValue(horizontal)
            self.verticalScrollBar().setValue(vertical)
        self._set_active_table(None)
        # Mouse focus arrives before the caret moves; don't route focus back into the old table.
        if not mouse:
            self._cursor_changed()

    def mousePressEvent(self, event):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._typing = None
        card = (self._table_above(event.position())
                if event.button() == Qt.MouseButton.LeftButton and not event.modifiers()
                else None)
        if card is not None:
            self._write_after_table(card)
            event.accept()
            return
        with self._without_routing():
            super().mousePressEvent(event)
        if (self.session is None or event.button() != Qt.MouseButton.LeftButton
                or self.textCursor().hasSelection()):
            return
        position = self.textCursor().position()
        model = self.session.tables.at_render_position(position)
        if model is not None:
            card = self.table_cards[model.identity]
            # At EOF, hit testing below a table still targets its folded source.
            if event.position().y() > card.geometry().bottom():
                self._leave_table(card, True)
                return
            if event.position().y() < card.geometry().top():
                self._leave_table(card, False)
                return
        self._cursor_changed()

    def _table_above(self, point):
        """The table whose blank room for writing after it contains `point`."""
        if self.session is None:
            return None
        for card in self.table_cards.values():
            room = self.session.layout.table_room(card.model.identity)
            if not room:
                continue  # Plain text follows directly: clicking there types in it.
            bottom = card.geometry().bottom()
            if (card.isVisible() and card.model.alive
                    and bottom < point.y() <= bottom + TABLE_MARGIN + room):
                return card
        return None

    def _write_after_table(self, card):
        """Start a paragraph after a table, opening an empty line if needed."""
        self.session.highlighter.refresh()
        position = card.model.after_position
        block = self.document().findBlock(position)
        if self.isReadOnly() or block.position() == position and not block.text():
            self._leave_table(card, True)
            return
        with self._without_routing():
            self.session.apply_source_edit(position, position, "\n")
            self.session.highlighter.refresh()
            cursor = QTextCursor(self.document())
            cursor.setPosition(position)
            self.setTextCursor(cursor)
            self.setFocus()
        self.ensureCursorVisible()

    def link_address_at(self, position):
        if self.session is not None:
            self.session.highlighter.refresh()
            for link in self.session.highlighter.analysis.links:
                if link.start <= position < link.end:
                    return self.resolve_link(link.url)
        return None

    def create_context_menu(self, embed=None, point=None):
        """The editor menu; `embed` is a collapsed embed it was opened on."""
        highlighter = self.session.highlighter if self.session is not None else None
        menu = build_edit_menu(TextActions(self, self.session, highlighter))
        link = embed.link if embed is not None else (
            source_link_at(self._links, self.textCursor().position())
            if self.session is not None else None)
        if link is not None:
            add_embed_actions(menu, self._links, link, embed, point)
        address = (embed.preview.address if embed is not None
                   else self.link_address_at(self.textCursor().position()))
        if address is not None:
            first = menu.actions()[0]
            action = menu.addAction(f"Follow In-BV Link ({address:#x})",
                                    lambda: self.addressActivated.emit(address))
            menu.insertAction(first, action)
        return menu

    def contextMenuEvent(self, event):
        embed = None
        if event.reason() == QContextMenuEvent.Reason.Mouse:
            # Right-clicking an embed acts on it where it is: it is not revealed.
            _link, embed = context_target(self._links, event.pos())
            if embed is None and not self.textCursor().hasSelection():
                self.setTextCursor(self._links.cursor_for_position(event.pos()))
                self._cursor_changed()
        if (self.active_table is not None and embed is None
                and not self.textCursor().hasSelection()):
            self.active_table.show_menu(event.globalPos())
            return
        menu = self.create_context_menu(embed, QPointF(event.pos()))
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.exec(event.globalPos())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.session is not None and self.isVisible():
            self.session.tables.set_height_limit(id(self), self.viewport().height())
        self._position_tables()

    def showEvent(self, event):
        super().showEvent(event)
        if self.session is not None:
            self.session.layout.set_view_active(self, True)
            self.session.highlighter.set_view_active(id(self), True)
            self.session.tables.set_height_limit(id(self), self.viewport().height())
        self._position_tables()

    def hideEvent(self, event):
        if self.session is not None:
            self.session.layout.set_view_active(self, False)
            self.session.highlighter.set_view_active(id(self), False)
            self.session.tables.set_height_limit(id(self), None)
        super().hideEvent(event)

    def _update_tab_width(self):
        font = self.session.highlighter.code_font if self.session is not None else self.font()
        self.setTabStopDistance(QFontMetricsF(font).horizontalAdvance(" ") * 4)

    def _visual_line_rect(self, position: int) -> QRectF:
        position = max(0, min(position, self.document().characterCount() - 1))
        block = self.document().findBlock(position)
        rectangle = self.blockBoundingGeometry(block).translated(self.contentOffset())
        layout = block.layout()
        line = layout.lineForTextPosition(position - block.position())
        if not line.isValid() and layout.lineCount():
            line = layout.lineAt(layout.lineCount() - 1)
        if line.isValid():
            return QRectF(
                rectangle.left() + line.x(), rectangle.top() + line.y(),
                line.width(), line.height(),
            )
        return rectangle  # An empty final paragraph can have no laid-out text line yet.

    def _code_frame_left(self, depth):
        """A fenced code frame starts on its paragraph's text column."""
        return DOCUMENT_MARGIN + depth * QUOTE_INDENT

    def _code_block_rects(self) -> list[QRectF]:
        if self.session is None or not self.session.highlighter.analysis_is_current:
            return []
        viewport = QRectF(self.viewport().rect())
        rectangles = []
        if self.session.layout.progressive:
            for code, left in self._code_index.between(*self._visible_source_range()):
                top = (self._visual_line_rect(code.start_position).top()
                       - CODE_BLOCK_VERTICAL_PADDING)
                bottom = (self._visual_line_rect(code.end_position).bottom()
                          + CODE_BLOCK_VERTICAL_PADDING)
                rectangle = QRectF(left, top, max(0, viewport.width() - left - DOCUMENT_MARGIN),
                                   bottom - top)
                if rectangle.intersects(viewport):
                    rectangles.append(rectangle)
            return rectangles
        offset = self.contentOffset().y()
        for top, bottom, left in self._code_index.between(-offset, viewport.height() - offset):
            rectangle = QRectF(left, top + offset,
                               max(0, viewport.width() - left - DOCUMENT_MARGIN), bottom - top)
            if rectangle.intersects(viewport):
                rectangles.append(rectangle)
        return rectangles

    def paintEvent(self, event):
        self._quotes.paint(event, background=True)
        rectangles = self._code_block_rects()
        background, border = code_block_colors(self.palette())
        if rectangles:
            painter = QPainter(self.viewport())
            painter.setClipRegion(event.region())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            for rectangle in rectangles:
                painter.drawRoundedRect(rectangle, 5, 5)
            painter.end()
        super().paintEvent(event)
        self._quotes.paint(event, code_frames=rectangles)
        if self.table_cards:
            painter = QPainter(self.viewport())
            painter.setClipRegion(event.region())
            for identity in self._visible_cards:
                card = self.table_cards.get(identity)
                if card is None:
                    continue
                if card.model.alive:
                    rectangle = self._table_rect(card.model)
                    # Hide the whole raw-source band, even when the grid is scrolled out of view.
                    rectangle.setLeft(0)
                    rectangle.setRight(self.viewport().width())
                    painter.fillRect(rectangle, self.palette().base())
            painter.end()
        if rectangles:
            # An overlay only: no document formats or undo entries.
            painter = QPainter(self.viewport())
            painter.setClipRegion(event.region())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(border, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for rectangle in rectangles:
                painter.drawRoundedRect(rectangle, 5, 5)
            painter.end()
        self._links.paint(event)
        if self.author_gutter.isVisible():
            self.author_gutter.update()

    def event(self, event):
        if event.type() == QEvent.Type.ShortcutOverride:
            # Keep editing commands here rather than in Binary Ninja's binary action handlers.
            if any(event.matches(key) for key in (
                QKeySequence.StandardKey.Undo, QKeySequence.StandardKey.Redo,
                QKeySequence.StandardKey.Copy, QKeySequence.StandardKey.Cut,
                QKeySequence.StandardKey.Paste, QKeySequence.StandardKey.SelectAll,
                QKeySequence.StandardKey.Bold, QKeySequence.StandardKey.Italic,
            )):
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event):
        if self.session is None or self.isReadOnly() or any(event.matches(key) for key in (
            QKeySequence.StandardKey.Undo, QKeySequence.StandardKey.Redo,
        )) or not event.text() and event.key() not in (
            Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab, Qt.Key.Key_Backtab,
            Qt.Key.Key_Backspace, Qt.Key.Key_Delete,
        ):
            self._typing = None
            self._key_press(event)
            return
        cursor = self.textCursor()
        kind = None
        shortcuts = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        if not event.modifiers() & shortcuts:
            if event.text() and all(ord(char) >= 32 for char in event.text()):
                kind = "text"
            elif event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
                kind = event.key()
        run = self._typing
        if run is None or not run.continued_by(kind, cursor):
            run = _TypingRun(kind)
        with self.session.source_edit(cursor.position(), merge_token=run.token):
            self._key_press(event)
        run.position = self.textCursor().position()
        self._typing = run if kind is not None else None

    def insertPlainText(self, text):
        if self.session is None or self.isReadOnly():
            super().insertPlainText(text)
        else:
            with self.session.source_edit(self.textCursor().position()):
                super().insertPlainText(text)

    def insertFromMimeData(self, source):
        if self.session is None or self.isReadOnly():
            super().insertFromMimeData(source)
        else:
            with self.session.source_edit(self.textCursor().position()):
                super().insertFromMimeData(source)

    def inputMethodEvent(self, event):
        if self.session is None or self.isReadOnly():
            super().inputMethodEvent(event)
        else:
            with self.session.source_edit(self.textCursor().position()):
                super().inputMethodEvent(event)

    def dropEvent(self, event):
        if self.session is None or self.isReadOnly():
            super().dropEvent(event)
        else:
            with self.session.source_edit(self.textCursor().position()):
                super().dropEvent(event)

    def _key_press(self, event):
        if not self.isReadOnly():
            cursor = self.textCursor()
            if (self.session is not None and not cursor.hasSelection() and not event.modifiers()
                    and event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete)):
                backward = event.key() == Qt.Key.Key_Backspace
                for model in self.session.tables.models:
                    boundary = model.after_position if backward else model.before_position
                    if cursor.position() == boundary:
                        # The hidden separators belong to the table.
                        event.accept()
                        return
            for key, delimiter in (
                (QKeySequence.StandardKey.Bold, "**"),
                (QKeySequence.StandardKey.Italic, "*"),
            ):
                if event.matches(key):
                    self.wrap_selection(delimiter)
                    return
            if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    and not event.modifiers() & ~(Qt.KeyboardModifier.ShiftModifier
                                                  | Qt.KeyboardModifier.KeypadModifier)
                    and leave_link_source(self._links)):
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers():
                if not self._continue_block():
                    self._new_line()
                return
            if (
                event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() == Qt.KeyboardModifier.ShiftModifier
            ):
                # Shift+Enter: a real newline (Qt inserts U+2028), without list/quote continuation.
                self.insertPlainText("\n")
                return
            if event.key() == Qt.Key.Key_Backtab:
                self.indent_selection(remove=True)
                return
            if event.key() == Qt.Key.Key_Tab and not event.modifiers():
                if self.textCursor().hasSelection():
                    self.indent_selection()
                else:
                    self.insertPlainText("    ")
                return
        super().keyPressEvent(event)

    def _new_line(self):
        # Not Qt's Enter: on an empty line with a block format (as author tags are), it resets
        # the format instead of starting a line.
        cursor = self.textCursor()
        cursor.beginEditBlock()
        cursor.removeSelectedText()
        cursor.insertBlock()
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def wrap_selection(self, delimiter: str) -> None:
        if self.active_table is not None:
            self.active_table.wrap_selection(delimiter)
            return
        if self.isReadOnly():
            return
        highlighter = self.session.highlighter
        highlighter.refresh()
        self.setTextCursor(toggle_inline_selection(
            self.textCursor(), delimiter, highlighter.analysis if not highlighter.warning else None,
        ))
        self.setFocus()

    def transform_lines(self, transform) -> None:
        if self.active_table is not None:
            card = self.active_table
            card.model.change_cells(card.selected_cells(), transform)
            return
        if self.isReadOnly():
            return
        cursor = self.textCursor()
        starts, ends = source_line_positions(self.toPlainText())
        first = bisect_right(starts, cursor.selectionStart()) - 1
        end = cursor.selectionEnd()
        last = bisect_right(starts, end) - 1
        if cursor.hasSelection() and last > first and starts[last] == end:
            last -= 1
        cursor.setPosition(starts[first])
        cursor.setPosition(ends[last], QTextCursor.MoveMode.KeepAnchor)
        text = cursor.selectedText().replace("\u2029", "\n").replace("\u2028", "\n")
        replacement = "\n".join(transform(line) for line in text.split("\n"))
        start = cursor.selectionStart()
        cursor.beginEditBlock()
        cursor.insertText(replacement)
        cursor.endEditBlock()
        cursor.setPosition(start)
        cursor.setPosition(start + utf16_length(replacement), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.setFocus()

    def indent_selection(self, remove=False):
        if remove:
            self.transform_lines(lambda line: re.sub(r"^(?: {1,4}|\t)", "", line))
        else:
            self.transform_lines(lambda line: "    " + line)

    def _continue_block(self) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection() or self.session is None:
            return False
        highlighter = self.session.highlighter
        highlighter.refresh()
        analysis = highlighter.analysis
        row = analysis.line_at_position(cursor.position())
        if highlighter.warning or row >= len(analysis.line_ends):
            return False
        if row in analysis.code_lines:
            return continue_quote(self, analysis)
        if cursor.position() != analysis.line_ends[row]:
            return False
        source_line = QTextCursor(self.document())
        source_line.setPosition(analysis.line_starts[row])
        source_line.setPosition(analysis.line_ends[row], QTextCursor.MoveMode.KeepAnchor)
        line = source_line.selectedText()
        match = _LIST.match(line)
        if match is not None:
            if not match["body"].strip():
                prefix = match["indent"] + match["quote"]
                cursor.setPosition(analysis.line_starts[row], QTextCursor.MoveMode.KeepAnchor)
                cursor.insertText(prefix)
            else:
                marker = match["bullet"] or str(int(match["number"]) + 1) + match["separator"]
                prefix = match["indent"] + match["quote"] + marker + " "
                if match["task"]:
                    prefix += "[ ] "
                cursor.insertText("\n" + prefix)
            self.setTextCursor(cursor)
            return True
        return continue_quote(self, analysis)
