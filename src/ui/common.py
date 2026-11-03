from __future__ import annotations

import hashlib
import math
import os
import sys
from bisect import bisect_left, bisect_right
from collections import deque
from concurrent.futures import CancelledError, ThreadPoolExecutor
from copy import deepcopy
from itertools import count
from threading import Event, local
from time import perf_counter, sleep
from weakref import WeakKeyDictionary, WeakMethod, WeakSet, ref

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetricsF,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPicture,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractSlider,
    QApplication,
    QInputDialog,
    QLabel,
    QMenu,
    QScrollBar,
    QStyle,
    QStyleOptionToolButton,
    QStylePainter,
    QToolButton,
)
from shiboken6 import isValid

NOTE_CONTENT_MARGIN = 8
# Blocks sit on the text column, DOCUMENT_MARGIN in; their content is BLOCK_INSET further.
DOCUMENT_MARGIN = 24
BLOCK_INSET = 8


def theme_icon_color(widget, *, active):
    ui = sys.modules.get("binaryninjaui")
    role_name = "SidebarActiveIconColor" if active else "SidebarInactiveIconColor"
    role = getattr(getattr(ui, "ThemeColor", None), role_name, None)
    getter = getattr(ui, "getThemeColor", None)
    if role is not None and callable(getter):
        try:
            color = QColor(getter(role))
            if color.isValid():
                return color
        except (TypeError, ValueError, RuntimeError):
            pass
    fallback = QPalette.ColorRole.ButtonText if active else QPalette.ColorRole.Mid
    return widget.palette().color(fallback)


def application_menu_font(widget):
    # The UI typeface at Binary Ninja's Linear View text size.
    font = QFont(QApplication.font())
    getter = getattr(sys.modules.get("binaryninjaui"), "getMonospaceFont", None)
    if callable(getter):
        try:
            reference = getter(widget)
            if reference.pointSizeF() > 0:
                font.setPointSizeF(reference.pointSizeF())
            elif reference.pixelSize() > 0:
                font.setPixelSize(reference.pixelSize())
        except (AttributeError, TypeError, ValueError, RuntimeError):
            pass
    return font


def single_shot(parent, slot, interval=0):
    """A single-shot timer running `slot` on a later event-loop turn; restarts coalesce."""
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(interval)
    timer.timeout.connect(slot)
    return timer


def app_singleton(name, factory):
    """One `factory(app)` per QApplication, created on first use."""
    app = QApplication.instance()
    instance = getattr(app, name, None)
    if instance is None:
        instance = factory(app)
        setattr(app, name, instance)
    return instance


class AppExecutor(QObject):
    """A worker pool that finishes its jobs while the application can still serve them."""

    def __init__(self, app, workers=1, name='bnnotes'):
        super().__init__(app)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.closed = False
        app.aboutToQuit.connect(self.close)

    def close(self):
        if not self.closed:
            self.closed = True
            self.executor.shutdown(wait=True, cancel_futures=True)


class _TimeSlicedQueue(QObject):
    """Runs queued jobs on the event loop in 3 ms slices."""

    def __init__(self, app):
        super().__init__(app)
        self.jobs = deque()
        self.timer = single_shot(self, self.drain)
        app.aboutToQuit.connect(self.timer.stop)

    def submit(self, job):
        self.jobs.append(job)
        if not self.timer.isActive():
            self.timer.start(1)

    def drain(self):
        deadline = perf_counter() + .003
        while self.jobs:
            self.jobs.popleft()()
            if perf_counter() >= deadline:
                break
        if self.jobs:
            self.timer.start(1)


def opacity(color: QColor, alpha: float) -> QColor:
    result = QColor(color)
    result.setAlphaF(max(0.0, min(1.0, alpha)))
    return result


def mix(background: QColor, foreground: QColor, amount: float) -> QColor:
    return QColor.fromRgbF(*(
        left * (1 - amount) + right * amount
        for left, right in zip(background.getRgbF()[:3], foreground.getRgbF()[:3], strict=True)
    ))


_ids = count()
_ACTION_ID = 'bnnotesActionId'


def track_action(action, menu):
    action._bnnotes_menu = ref(menu)
    action._bnnotes_action_id = next(_ids)
    action.setProperty(_ACTION_ID, action._bnnotes_action_id)
    return action


def live_action(action):
    if action is None or isValid(action):
        return action
    owner = getattr(action, '_bnnotes_menu', lambda: None)()
    identity = getattr(action, '_bnnotes_action_id', None)
    if owner is not None and isValid(owner) and identity is not None:
        for candidate in owner.actions():
            if candidate.property(_ACTION_ID) == identity:
                candidate._bnnotes_menu = ref(owner)
                candidate._bnnotes_action_id = identity
                return candidate
    return None


def live_actions(actions):
    for key, stored in list(actions.items()):
        action = live_action(stored)
        if action is None:
            actions.pop(key, None)
        else:
            actions[key] = action
            yield key, action


class OwnedMenu(QMenu):
    def addAction(self, *args, **kwargs):
        if (not kwargs and 1 <= len(args) <= 2 and isinstance(args[0], str)
                and (len(args) == 1 or callable(args[1]))):
            action = track_action(QAction(args[0], self), self)
            if len(args) == 2:
                action.triggered.connect(args[1])
            super().addAction(action)
            return action
        return super().addAction(*args, **kwargs)

    def addMenu(self, menu):
        if isinstance(menu, str):
            child = OwnedMenu(self)
            child.setTitle(menu)
            super().addMenu(child)
            return child
        return super().addMenu(menu)


FORMATS = (
    ("Bold", "**", QKeySequence.StandardKey.Bold),
    ("Italic", "*", QKeySequence.StandardKey.Italic),
    ("Strikethrough", "~~", None),
    ("Highlight", "==", None),
    (None, None, None),
    ("Code", "`", None),
    ("Math", "$", None),
    ("Comment", "%%", None),
)


class NoteContextMenu(OwnedMenu):
    def __init__(self, parent=None, title=""):
        super().__init__(title, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_WindowPropagation)
        palette = (getattr(parent, "_theme_palette", parent.palette())
                   if parent is not None else self.palette())
        self._theme_palette = QPalette(palette)
        # Pin popup brushes: inherited palettes can have an empty resolve mask.
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
                      QPalette.ColorGroup.Disabled):
            for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Text,
                         QPalette.ColorRole.Window, QPalette.ColorRole.WindowText,
                         QPalette.ColorRole.Highlight, QPalette.ColorRole.HighlightedText):
                self._theme_palette.setBrush(group, role, palette.brush(group, role))
        self.setFont(QFont(parent.font()) if isinstance(parent, NoteContextMenu)
                     else application_menu_font(parent or self))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setSeparatorsCollapsible(True)
        self.setPalette(self._theme_palette)
        base = self._theme_palette.color(QPalette.ColorRole.Base)
        text = self._theme_palette.color(QPalette.ColorRole.Text)
        background = mix(base, text, 0.075 if base.lightness() < 128 else 0.015)
        border = mix(background, text, 0.22)
        rule = mix(background, text, 0.18)
        disabled = mix(background, text, 0.4)
        highlight = self._theme_palette.color(QPalette.ColorRole.Highlight)
        selected = self._theme_palette.color(QPalette.ColorRole.HighlightedText)
        self._text, self._selected_text, self._disabled = text, selected, disabled
        self.setStyleSheet(f"""
            QMenu {{
                background-color: {background.name()}; color: {text.name()};
                border: 1px solid {border.name()}; border-radius: 8px;
                padding: 3px; min-width: 160px;
            }}
            QMenu::item {{ padding: 3px 16px 3px 10px; margin: 0px 1px; border-radius: 4px; }}
            QMenu::item:selected {{
                background-color: {highlight.name()}; color: {selected.name()};
            }}
            QMenu::item:disabled {{ color: {disabled.name()}; }}
            QMenu::separator {{ height: 1px; background-color: {rule.name()}; margin: 3px 8px; }}
            QMenu::right-arrow {{ image: none; width: 10px; height: 10px; }}
        """)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for action in self.actions():
            if action.menu() is None or not action.isVisible():
                continue
            rectangle = self.actionGeometry(action)
            color = self._selected_text if action is self.activeAction() else self._text
            pen = QPen(color if action.isEnabled() else self._disabled, 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            x, y = rectangle.right() - 12, rectangle.center().y()
            painter.drawLine(QPointF(x - 2, y - 3.5), QPointF(x + 1.5, y))
            painter.drawLine(QPointF(x + 1.5, y), QPointF(x - 2, y + 3.5))
        painter.end()

    def addMenu(self, menu):
        if isinstance(menu, str):
            child = NoteContextMenu(self, menu)
            super().addMenu(child)
            return child
        return super().addMenu(menu)


def _action(menu, title, callback, *, enabled=True, shortcut=None, checked=None):
    action = menu.addAction(title, callback)
    action.setEnabled(enabled)
    if shortcut is not None:
        action.setShortcut(QKeySequence(shortcut))
        action.setShortcutVisibleInContextMenu(True)
    if checked is not None:
        action.setCheckable(True)
        action.setChecked(checked)
    return action


def _author_menu(menu, session, lines, parent, editable):
    """Who wrote `lines`: a person, or a named agent (as the author bar shows)."""
    current = session.authors(lines)
    names = sorted({'AI', *session.agent_names}, key=str.casefold)
    for title, name in (('Human', None), *((menu_text(name), name) for name in names)):
        _action(menu, title, lambda _checked=False, name=name: session.set_author(lines, name),
                enabled=editable, checked=current == {name})
    menu.addSeparator()

    def rename():
        agents = sorted(current - {None})
        name, accepted = QInputDialog.getText(
            parent, 'Agent Name', 'Mark these lines as written by:',
            text=agents[0] if len(agents) == 1 else 'AI')
        if accepted and name.strip():
            session.set_author(lines, name.strip())

    _action(menu, "Agent Name…", rename, enabled=editable)


def build_edit_menu(actions, menu=None):
    menu = menu if menu is not None else NoteContextMenu(actions.parent)
    editable = actions.editable
    _action(menu, "Add link", lambda: actions.link(), enabled=editable)
    _action(menu, "Add external link", lambda: actions.link(external=True), enabled=editable)
    menu.addSeparator()
    formatting = menu.addMenu("Format")
    for label, delimiter, shortcut in FORMATS:
        if label is None:
            formatting.addSeparator()
        else:
            _action(formatting, label,
                    lambda _checked=False, delimiter=delimiter: actions.format(delimiter),
                    enabled=editable, shortcut=shortcut, checked=actions.format_checked(delimiter))
    formatting.addSeparator()
    _action(formatting, "Clear formatting", actions.clear, enabled=editable and actions.has_text)

    paragraphs = menu.addMenu("Paragraph")
    _action(paragraphs, "Normal text", lambda: actions.paragraph("normal"), enabled=editable)
    for level in range(1, 7):
        _action(paragraphs, f"Heading {level}",
                lambda _checked=False, level=level: actions.paragraph(f"heading{level}"),
                enabled=editable)
    paragraphs.addSeparator()
    for label, style in (("Blockquote", "quote"), ("Bulleted list", "bullet"),
                         ("Numbered list", "numbered"), ("Task list", "task")):
        _action(paragraphs, label, lambda _checked=False, style=style: actions.paragraph(style),
                enabled=editable)

    insertion = menu.addMenu("Insert")
    if not actions.cell:
        _action(insertion, "Table…", actions.insert_table, enabled=editable)
        insertion.addSeparator()
    for label, kind in (("Code block", "code"), ("Math block", "math"),
                        ("Horizontal rule", "rule")):
        _action(insertion, label, lambda _checked=False, kind=kind: actions.insert(kind),
                enabled=editable)

    lines = actions.author_lines()
    if lines:
        _author_menu(menu.addMenu("Author"), actions.session, lines, actions.parent, editable)

    menu.addSeparator()
    _action(menu, "Cut", actions.cut, enabled=editable and actions.has_selection,
            shortcut=QKeySequence.StandardKey.Cut)
    _action(menu, "Copy", actions.copy, enabled=actions.has_selection,
            shortcut=QKeySequence.StandardKey.Copy)
    mime = QApplication.clipboard().mimeData()
    can_paste = editable and mime is not None and mime.hasText()
    _action(menu, "Paste", actions.paste, enabled=can_paste,
            shortcut=QKeySequence.StandardKey.Paste)
    _action(menu, "Paste as plain text", actions.paste_plain, enabled=can_paste)
    _action(menu, "Select all", actions.select_all, enabled=actions.has_text,
            shortcut=QKeySequence.StandardKey.SelectAll)
    menu.addSeparator()
    history = actions.session.document if actions.session is not None else None
    _action(menu, "Undo", actions.undo, enabled=editable and history.isUndoAvailable(),
            shortcut=QKeySequence.StandardKey.Undo)
    _action(menu, "Redo", actions.redo, enabled=editable and history.isRedoAvailable(),
            shortcut=QKeySequence.StandardKey.Redo)
    return menu


ICON_SIZE = 16
BUTTON_SIZE = 28


def _symbol_pixmap(symbol, color, font, scale):
    pixmap = QPixmap(ICON_SIZE * scale, ICON_SIZE * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.scale(scale, scale)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    pen = QPen(color, 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    if symbol in ("bold", "italic", "heading"):
        letter = {"bold": "B", "italic": "I", "heading": "H"}[symbol]
        glyph_font = QFont(font)
        glyph_font.setPixelSize(15)
        glyph_font.setWeight(QFont.Weight.Bold if symbol == "bold" else QFont.Weight.DemiBold)
        glyph_font.setItalic(symbol == "italic")
        painter.setFont(glyph_font)
        bounds = QFontMetricsF(glyph_font).tightBoundingRect(letter)
        painter.drawText(QPointF(8 - bounds.center().x(), 8 - bounds.center().y()), letter)
    elif symbol == "code":
        for start, middle, end in (
            (QPointF(5, 3), QPointF(1.5, 8), QPointF(5, 13)),
            (QPointF(11, 3), QPointF(14.5, 8), QPointF(11, 13)),
        ):
            painter.drawLine(start, middle)
            painter.drawLine(middle, end)
    elif symbol == "tasks":
        painter.drawRoundedRect(QRectF(1.5, 2.5, 6.5, 6.5), 1, 1)
        painter.drawLine(QPointF(3, 5.5), QPointF(4.5, 7))
        painter.drawLine(QPointF(4.5, 7), QPointF(7, 4.5))
        painter.drawLine(QPointF(10.5, 5.5), QPointF(14.5, 5.5))
        painter.drawLine(QPointF(2, 12.5), QPointF(14.5, 12.5))
    elif symbol == "more":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for x in (3, 8, 13):
            painter.drawEllipse(QPointF(x, 8), 1.25, 1.25)
    elif symbol == "plus":
        painter.drawLine(QPointF(8, 4.5), QPointF(8, 11.5))
        painter.drawLine(QPointF(4.5, 8), QPointF(11.5, 8))
    elif symbol == "menu":
        for y in (5, 8, 11):
            painter.drawLine(QPointF(3.5, y), QPointF(12.5, y))
    elif symbol == "table":
        painter.drawRoundedRect(QRectF(1.5, 2, 13, 12), 1, 1)
        for y in (6, 10):
            painter.drawLine(QPointF(1.5, y), QPointF(14.5, y))
        for x in (6, 10):
            painter.drawLine(QPointF(x, 2), QPointF(x, 14))
    elif symbol == "outline":
        for y, indent in ((4, 0), (8, 3), (12, 3)):
            painter.drawLine(QPointF(1.5 + indent, y), QPointF(2.5 + indent, y))
            painter.drawLine(QPointF(5.5 + indent, y), QPointF(14.5, y))
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


class ToolbarButton(QToolButton):
    def __init__(self, parent=None, *, symbol=None):
        super().__init__(parent)
        self._symbol = symbol
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedHeight(BUTTON_SIZE)
        if symbol is not None:
            self.setFixedWidth(BUTTON_SIZE)
            self.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            self._rebuild_icon()
    def _rebuild_icon(self):
        icon = QIcon()
        for mode, group in (
            (QIcon.Mode.Normal, QPalette.ColorGroup.Active),
            (QIcon.Mode.Active, QPalette.ColorGroup.Active),
            (QIcon.Mode.Disabled, QPalette.ColorGroup.Disabled),
        ):
            color = self.palette().color(group, QPalette.ColorRole.ButtonText)
            for scale in (1, 2):
                icon.addPixmap(_symbol_pixmap(self._symbol, color, self.font(), scale), mode)
        self.setIcon(icon)

    def paintEvent(self, _event):
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        # Native button styling, without a menu indicator.
        option.features &= ~(
            QStyleOptionToolButton.ToolButtonFeature.HasMenu
            | QStyleOptionToolButton.ToolButtonFeature.MenuButtonPopup
        )
        option.subControls = QStyle.SubControl.SC_ToolButton
        option.activeSubControls &= QStyle.SubControl.SC_ToolButton
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)
        painter.end()

    def changeEvent(self, event):
        super().changeEvent(event)
        if getattr(self, "_symbol", None) is not None and event.type() in (
            QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange,
        ):
            self._rebuild_icon()


class ElidedLabel(QLabel):
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        painter.drawText(
            self.rect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text
        )
        painter.end()


class ViewportButton(ToolbarButton):
    """A compact, icon-only control fixed over the source viewport."""

    def __init__(self, parent=None, *, symbol):
        super().__init__(parent, symbol=symbol)
        self.setFixedSize(24, 24)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = self.palette().color(QPalette.ColorRole.Base)
        text = self.palette().color(QPalette.ColorRole.Text)
        accent = self.palette().color(QPalette.ColorRole.Highlight)
        if base.lightness() < 128 and accent.lightness() < 145:
            accent = accent.lighter(140)
        hot = self.isEnabled() and (self.underMouse() or self.isDown())
        background = mix(base, text, 0.1 if self.isDown() else 0.06) if hot else base
        border = accent if self.hasFocus() else mix(base, text, 0.18 if hot else 0.08)
        # An opaque backing hides text scrolling beneath the fixed control.
        painter.fillRect(self.rect(), base)
        painter.setPen(QPen(border, 1))
        painter.setBrush(background)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 2, 2)
        if self.isEnabled():
            color = theme_icon_color(self, active=hot or self.hasFocus())
        else:
            color = self.palette().color(
                QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText,
            )
        scale = max(1, round(self.devicePixelRatioF()))
        icon = _symbol_pixmap(self._symbol, color, self.font(), scale)
        painter.drawPixmap((self.width() - ICON_SIZE) // 2, (self.height() - ICON_SIZE) // 2, icon)
        painter.end()


class OutlineButton(ViewportButton):
    def __init__(self, parent=None):
        super().__init__(parent, symbol="outline")
        self.setObjectName("bnnotes-outline-toggle")
        self.setAccessibleName("Toggle heading outline")
        self.setCheckable(True)
        self.toggled.connect(self._checked_changed)
        self._checked_changed(False)

    def _checked_changed(self, checked):
        self.setToolTip("Hide heading outline" if checked else "Show heading outline")
        self.update()


class FindKeyFilter(QObject):
    def __init__(self, root, callback, enabled):
        super().__init__(root)
        self.root, self.callback, self.enabled = root, callback, enabled
        app = QApplication.instance()
        app.focusChanged.connect(self._focus_changed)
        self._focus_changed(None, app.focusWidget())

    def _owns(self, widget):
        return (isValid(self.root) and widget is not None and isValid(widget)
                and (widget is self.root or self.root.isAncestorOf(widget)))

    def _focus_changed(self, previous, current):
        if previous is not None and isValid(previous):
            previous.removeEventFilter(self)
        if self._owns(current):
            current.installEventFilter(self)

    def eventFilter(self, watched, event):
        kind = event.type()
        if (kind not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
                or not self._owns(watched) or not self.enabled()
                or event.key() != Qt.Key.Key_F
                or event.modifiers() not in (Qt.KeyboardModifier.ControlModifier,
                                             Qt.KeyboardModifier.MetaModifier)):
            return False
        # Claim the host's Find shortcut and swallow the rest of the key sequence.
        event.accept()
        if kind == QEvent.Type.KeyPress:
            self.callback()
        return True


class _FocusObserver(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.listeners = WeakKeyDictionary()
        app.focusChanged.connect(self.changed)

    def changed(self, old, new):
        visited = set()
        for widget in (old, new):
            while widget is not None and isValid(widget) and widget not in visited:
                visited.add(widget)
                for pointer in self.listeners.get(widget, ()):
                    callback = pointer()
                    if callback is not None and isValid(callback.__self__):
                        callback()
                widget = widget.parentWidget()


def watch_focus(widget, callback):
    observer = app_singleton('_bnnotes_focus_observer', _FocusObserver)
    observer.listeners.setdefault(widget, []).append(WeakMethod(callback))


class _FocusKeeper(QObject):
    """Moves focus to a live widget when a focused Notes widget goes away.

    Binary Ninja's main menu acts on the widget that last had focus, refreshed whenever focus
    moves to a widget but not when it moves to nothing. Deleting the focused widget moves
    focus to nothing, so the menu bar keeps the deleted widget, and the next menu item or
    shortcut (⌘C, ⌘S…) validates its actions against freed memory and crashes. When focus
    inside a Notes surface goes to nothing while its window is still active, this focuses the
    nearest surviving widget that held it, which Binary Ninja then takes as its context.
    """

    def __init__(self, app):
        super().__init__(app)
        self.roots = WeakSet()
        self._lineage = ()  # The focused Notes widget and its ancestors, up to its window.
        self._restore = single_shot(self, self._restore_focus)
        app.focusChanged.connect(self._changed)

    def _changed(self, _old, new):
        # `_old` may be mid-destruction: never touch it.
        if new is not None:
            self._restore.stop()
            self._lineage = self._notes_lineage(new)
        elif self._lineage and QApplication.activeWindow() is not None:
            self._restore.start()  # Not deactivation, which Binary Ninja handles itself.

    def _notes_lineage(self, widget):
        lineage, inside = [], False
        while widget is not None:
            lineage.append(widget)
            inside = inside or widget in self.roots
            widget = widget.parentWidget()
        return tuple(lineage) if inside else ()

    def _restore_focus(self):
        window = QApplication.activeWindow()
        if QApplication.focusWidget() is not None or window is None:
            return
        for widget in self._lineage:
            if (isValid(widget) and widget.window() is window and widget.isVisible()
                    and widget.isEnabled()):
                widget.setFocus(Qt.FocusReason.OtherFocusReason)
                if QApplication.focusWidget() is not None:
                    return


def keep_focus(root):
    """Never leave focus on nothing when a focused widget inside `root` is deleted."""
    app_singleton('_bnnotes_focus_keeper', _FocusKeeper).roots.add(root)


class IdleTask(QObject):
    """A coalesced preparation callback; stop/destroy invalidates queued work."""

    timeout = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.active = False
        self.generation = 0

    def start(self, _delay=0):
        if self.active:
            return
        self.active = True
        pointer, generation = ref(self), self.generation

        def run():
            task = pointer()
            if (task is not None and isValid(task) and task.active
                    and task.generation == generation):
                task.active = False
                task.timeout.emit()

        app_singleton('_bnnotes_idle_queue', _TimeSlicedQueue).submit(run)

    def stop(self):
        self.active = False
        self.generation += 1

    def isActive(self):
        return self.active


VIEWS_KEY = 'binaryViews'


def binary_view_id(data):
    """A file identity that survives restarts (project ids or canonical path)."""
    file = data.file
    project_file = getattr(file, 'project_file', None)
    if project_file is not None:
        project = getattr(project_file, 'project', None) or getattr(file, 'project', None)
        project_id, file_id = getattr(project, 'id', None), getattr(project_file, 'id', None)
        if project_id and file_id:
            return f'project:{project_id}:{file_id}'
    paths = (getattr(project_file, 'path_on_disk', ''), getattr(file, 'filename', ''))
    for path in paths:
        if path and os.path.isabs(path):
            canonical = os.path.normcase(os.path.realpath(path))
            return 'file:' + hashlib.sha256(os.fsencode(canonical)).hexdigest()
    return f'session:{file.session_id}'


class PersistentViewSettings(QObject):
    """{view_id: {name: value}} in QSettings, flushed lazily; session ids stay in memory."""

    def __init__(self, backend=None, parent=None):
        super().__init__(parent)
        self.backend = backend if backend is not None else QSettings('0cyn', 'bnnotes', self)
        if self.backend.parent() is None:
            self.backend.setParent(self)
        self.backend.setFallbacksEnabled(False)
        value = self.backend.value(VIEWS_KEY, {})
        self._views = deepcopy(value) if isinstance(value, dict) else {}
        self._sessions = {}
        self._dirty = False
        self._flush = single_shot(self, self.sync, 100)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.sync)

    def _map(self, view_id):
        return self._sessions if view_id.startswith('session:') else self._views

    def view(self, view_id):
        value = self._map(view_id).get(view_id)
        return deepcopy(value) if isinstance(value, dict) else {}

    def get(self, view_id, name, default=None):
        return self.view(view_id).get(name, deepcopy(default))

    def set(self, view_id, name, value):
        values = self.view(view_id)
        if name in values and values[name] == value:
            return
        values[name] = deepcopy(value)
        self._map(view_id)[view_id] = values
        self._changed(view_id)

    def remove(self, view_id, name):
        values = self.view(view_id)
        if name in values:
            del values[name]
            self._map(view_id)[view_id] = values
            self._changed(view_id)

    def app_value(self, name, default=None):
        """An application-wide preference, beside the per-view map."""
        return self.backend.value(f'app/{name}', default)

    def set_app_value(self, name, value):
        if self.backend.value(f'app/{name}') == value:
            return
        self.backend.setValue(f'app/{name}', value)
        self._dirty = True
        self._flush.start()

    def remove_app_value(self, name):
        if self.backend.contains(f'app/{name}'):
            self.backend.remove(f'app/{name}')
            self._dirty = True
            self._flush.start()

    def _changed(self, view_id):
        if not view_id.startswith('session:'):
            self.backend.setValue(VIEWS_KEY, self._views)
            self._dirty = True
            self._flush.start()

    def sync(self):
        self._flush.stop()
        if self._dirty:
            self.backend.sync()
            self._dirty = self.backend.status() != QSettings.Status.NoError
        return self.backend.status() == QSettings.Status.NoError


def view_settings():
    return app_singleton('_bnnotes_view_settings', lambda app: PersistentViewSettings(parent=app))


_worker = local()


def yield_preparation():
    """Bound GIL ownership while constructing Qt value objects off-thread."""
    deadline = getattr(_worker, 'deadline', None)
    cancellation = getattr(_worker, 'cancellation', None)
    if cancellation is not None and cancellation.is_set():
        raise CancelledError()
    if deadline is not None and perf_counter() >= deadline:
        sleep(.001)
        _worker.deadline = perf_counter() + .001


def _prepare(operation, args, kwargs, cancellation):
    _worker.deadline = perf_counter() + .001
    _worker.cancellation = cancellation
    try:
        return operation(*args, **kwargs)
    finally:
        _worker.deadline = None
        _worker.cancellation = None


class _PreparationPool(QObject):
    completed = Signal(object, object, object)

    def __init__(self, app):
        super().__init__(app)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bnnotes-render')
        self.closed = False
        self.jobs = WeakSet()
        self.completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        app.aboutToQuit.connect(self.close)

    def submit(self, owner, key, operation, *args, cache=None, cache_key=None, **kwargs):
        if self.closed:
            return None
        future = cache.get(cache_key) if cache is not None else None
        if future is None:
            cancellation = Event()
            future = self.executor.submit(_prepare, operation, args, kwargs, cancellation)
            future.cancel_preparation = cancellation.set
            self.jobs.add(future)
            if cache is not None:
                cache[cache_key] = future
        pointer = ref(owner)
        future.add_done_callback(lambda result: self.completed.emit(pointer, key, result))
        return future

    def _deliver(self, pointer, key, future):
        owner = pointer()
        if not self.closed and owner is not None and isValid(owner) and not future.cancelled():
            owner.preparation_finished(key, future)

    def close(self):
        self.closed = True
        for future in list(self.jobs):
            future.cancel_preparation()
        self.executor.shutdown(wait=True, cancel_futures=True)


def preparation_pool():
    return app_singleton('_bnnotes_preparation_pool', _PreparationPool)


TILE_WIDTH = 1024
TILE_HEIGHT = 256


class _Tile:
    def __init__(self, bounds, draw):
        self.bounds = bounds
        self.images = {}
        self.pending = set()
        self.picture = QPicture()
        self.picture.setBoundingRect(QRect(0, 0, math.ceil(bounds.width()),
                                           math.ceil(bounds.height())))
        painter = QPainter(self.picture)
        painter.translate(-bounds.topLeft())
        painter.setClipRect(bounds)
        draw(painter, bounds)
        painter.end()

    def rasterize(self, scale):
        self.pending.discard(scale)
        if scale in self.images:
            return
        image = QImage(math.ceil(self.bounds.width() * scale),
                       math.ceil(self.bounds.height() * scale),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(scale)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.drawPicture(QPointF(), self.picture)
        painter.end()
        self.images[scale] = image

    def draw(self, painter, origin, scale):
        image = self.images.get(scale)
        point = origin + self.bounds.topLeft()
        if image is not None:
            painter.drawImage(point, image)
        else:
            painter.drawPicture(point, self.picture)
            if scale not in self.pending:
                self.pending.add(scale)
                pointer = ref(self)
                app_singleton('_bnnotes_raster_queue', _TimeSlicedQueue).submit(
                    lambda: (tile := pointer()) is not None and tile.rasterize(scale))


class RetainedDrawing:
    """Sparse 2-D tile grid. Blank space in tall source-backed embeds costs nothing."""

    @classmethod
    def record(cls, draw, bounds, *, regions=None):
        """Record once on a sized paint device; native document culling needs it."""
        picture = QPicture()
        picture.setBoundingRect(bounds.toAlignedRect())
        painter = QPainter(picture)
        painter.setClipRect(bounds)
        draw(painter, bounds)
        painter.end()
        return cls([bounds] if regions is None else regions,
                   lambda target, _clip: target.drawPicture(QPointF(), picture))

    def __init__(self, regions, draw):
        cells = {}
        for rectangle in regions:
            if rectangle.isEmpty():
                continue
            for row in range(math.floor(rectangle.top() / TILE_HEIGHT),
                             math.ceil(rectangle.bottom() / TILE_HEIGHT)):
                for column in range(math.floor(rectangle.left() / TILE_WIDTH),
                                    math.ceil(rectangle.right() / TILE_WIDTH)):
                    key = column, row
                    tile = QRectF(column * TILE_WIDTH, row * TILE_HEIGHT, TILE_WIDTH, TILE_HEIGHT)
                    bounds = QRectF(rectangle.toAlignedRect()).intersected(tile)
                    cells[key] = cells[key].united(bounds) if key in cells else bounds
        self.tiles = {}
        for key, bounds in cells.items():
            self.tiles[key] = _Tile(bounds, draw)
            yield_preparation()

    def draw(self, painter, origin, visible):
        clip = visible.translated(-origin)
        scale = painter.device().devicePixelRatioF()
        for row in range(math.floor(clip.top() / TILE_HEIGHT),
                         math.ceil(clip.bottom() / TILE_HEIGHT)):
            for column in range(math.floor(clip.left() / TILE_WIDTH),
                                math.ceil(clip.right() / TILE_WIDTH)):
                tile = self.tiles.get((column, row))
                if tile is not None:
                    tile.draw(painter, origin, scale)


class SmoothScrollBar(QScrollBar):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._target = float(self.value())
        self._updating = False
        self._handling_action = False
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(120)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._apply_value)
        self.valueChanged.connect(self._external_value_changed)
        self.rangeChanged.connect(self.cancel_animation)
        self.sliderPressed.connect(self.cancel_animation)
        self.actionTriggered.connect(self._smooth_action)

    def cancel_animation(self, *_args):
        self._animation.stop()
        self._target = float(self.value())

    def _external_value_changed(self, _value):
        if not self._updating:
            # Dragging and programmatic moves are immediate and cancel any animation.
            self.cancel_animation()

    def _apply_value(self, value):
        self._updating = True
        try:
            self.setValue(round(value))
        finally:
            self._updating = False

    def _scroll_by(self, amount, *, animated):
        self._animation.stop()
        self._target = max(self.minimum(), min(self.maximum(), self._target + amount))
        if animated:
            self._animation.setStartValue(float(self.value()))
            self._animation.setEndValue(float(self._target))
            self._animation.start()
        else:
            self._apply_value(self._target)

    def _smooth_action(self, action):
        if self._handling_action:
            return
        action = QAbstractSlider.SliderAction(action)
        if action == QAbstractSlider.SliderAction.SliderMove:
            self.cancel_animation()
            return
        if action not in (
            QAbstractSlider.SliderAction.SliderSingleStepAdd,
            QAbstractSlider.SliderAction.SliderSingleStepSub,
            QAbstractSlider.SliderAction.SliderPageStepAdd,
            QAbstractSlider.SliderAction.SliderPageStepSub,
        ):
            return
        amount = self.sliderPosition() - self.value()
        self._handling_action = True
        try:
            # actionTriggered precedes the move; hold the value and animate instead.
            self.setSliderPosition(self.value())
        finally:
            self._handling_action = False
        self._scroll_by(amount, animated=True)

    def wheelEvent(self, event):
        pixels, angle = event.pixelDelta(), event.angleDelta()
        if not pixels.isNull():
            self.cancel_animation()
            delta = ((pixels.x() or pixels.y()) if self.orientation() == Qt.Orientation.Horizontal
                     else pixels.y())
        else:
            delta = ((angle.x() or angle.y()) if self.orientation() == Qt.Orientation.Horizontal
                     else angle.y())
        if not delta:
            event.accept()
            return
        if ((delta > 0 and self._target <= self.minimum())
                or (delta < 0 and self._target >= self.maximum())):
            event.ignore()
            return
        if not pixels.isNull():
            # Trackpads already supply momentum.
            self._scroll_by(-delta, animated=False)
        else:
            amount = -delta / 120 * self.singleStep() * QApplication.wheelScrollLines()
            self._scroll_by(amount, animated=True)
        event.accept()


class VerticalIndex:
    def __init__(self, items=()):
        # Items are (top, bottom, payload); prefix maxima also handle overlaps.
        ordered = sorted(items, key=lambda item: item[0])
        self.tops = tuple(item[0] for item in ordered)
        ends, maximum = [], float('-inf')
        for _top, bottom, _payload in ordered:
            maximum = max(maximum, bottom)
            ends.append(maximum)
        self.ends = tuple(ends)
        self.items = tuple(item[2] for item in ordered)

    def between(self, top, bottom):
        first, last = bisect_left(self.ends, top), bisect_right(self.tops, bottom)
        for index in range(first, last):
            yield self.items[index]


def menu_text(text):
    """`text` for a QAction label, with literal ampersands."""
    return text.replace('&', '&&')


class Lease:
    """Holds one `acquire(token)` on a shared object until `release`, or until `owner` dies.

    It keeps no reference to its owner, so it can release from the `destroyed` signal.
    """

    def __init__(self, owner):
        self._held = None
        owner.destroyed.connect(self.release)

    def take(self, target):
        self.release()
        if target is not None:
            token = object()
            target.acquire(token)
            self._held = target, token

    def release(self, *_args):
        if self._held is not None:
            target, token = self._held
            self._held = None
            target.release(token)
