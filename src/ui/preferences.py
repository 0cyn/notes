from __future__ import annotations

import sys
from html import escape

from PySide6.QtCore import QEvent, QObject, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..settings import (
    DEFAULTS,
    MAX_FONT_SIZE,
    MIN_FONT_SIZE,
    SCOPE_LABELS,
    MemorySettings,
    NativeSettings,
    Scope,
    project_for,
    setting_choices,
    setting_description,
    setting_title,
    validate,
)
from ..storage import view_name
from .common import app_singleton, opacity


class EditorPreferences(QObject):
    changed = Signal(str)
    invalidated = Signal()

    def __init__(self, backend=None, parent=None, *, data=None, hub=None):
        super().__init__(parent)
        self.backend = backend if backend is not None else NativeSettings()
        self.data = data
        self._hub = hub or self
        self._values = {}
        self.refresh()
        if hub is not None:
            hub.invalidated.connect(self.refresh)
        else:
            # Binary Ninja's Python Settings API has no change notification.
            self._poll = QTimer(self)
            self._poll.setInterval(750)
            self._poll.timeout.connect(self.synchronize)
            self._poll.start()
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._poll.stop)

    def scope_available(self, scope):
        return (scope == Scope.APPLICATION or scope == Scope.VIEW and self.data is not None
                or scope == Scope.PROJECT and project_for(self.data) is not None)

    def state(self, name, scope=Scope.AUTO):
        return self.backend.read(name, self.data, scope)

    def get(self, name, scope=Scope.AUTO):
        return self.state(name, scope)[0]

    def effective(self, name):
        """Last synchronized effective value for hot presentation paths."""
        return self._values[name][0][0]

    def later_overrides(self, name, scope):
        result = []
        for candidate in (Scope.PROJECT, Scope.VIEW):
            if candidate <= scope or not self.scope_available(candidate):
                continue
            value, origin = self.state(name, candidate)
            if origin == candidate:
                result.append((candidate, value))
        return result

    def _check_scope(self, scope):
        if not self.scope_available(scope):
            raise ValueError('This settings scope is not available for the current view.')

    def set(self, name, value, scope=Scope.APPLICATION):
        self._check_scope(scope)
        value = validate(name, value)
        self._hub.synchronize()
        self.backend.write(name, value, self.data, scope)
        self._hub.synchronize()
        self._refresh_native_views()

    def reset(self, name, scope=Scope.APPLICATION):
        self._check_scope(scope)
        self._hub.synchronize()
        self.backend.reset(name, self.data, scope)
        self._hub.synchronize()
        self._refresh_native_views()

    def reset_defaults(self, scope=Scope.APPLICATION):
        self._check_scope(scope)
        self._hub.synchronize()
        try:
            for name in DEFAULTS:
                self.backend.reset(name, self.data, scope)
        finally:
            self._hub.synchronize()
            self._refresh_native_views()

    def _refresh_native_views(self):
        if not isinstance(self.backend, NativeSettings):
            return
        settings_type = getattr(sys.modules.get('binaryninjaui'), 'SettingsView', None)
        if settings_type is None:
            return
        # API writes don't notify open native settings editors.
        for widget in QApplication.allWidgets():
            if isinstance(widget, settings_type):
                widget.refreshAllSettings()

    def refresh(self):
        states = {name: tuple(self.state(name, scope) for scope in (
            Scope.AUTO, Scope.APPLICATION, Scope.PROJECT, Scope.VIEW,
        )) for name in DEFAULTS}
        changed = [name for name in DEFAULTS if states[name] != self._values.get(name)]
        self._values = states
        for name in changed:
            self.changed.emit(name)

    def synchronize(self):
        revision = self.backend.normalization_revision
        self.refresh()
        self.invalidated.emit()
        if self.backend.normalization_revision != revision:
            self._refresh_native_views()

    def font(self, scope=Scope.AUTO):
        font = QFont(QApplication.font())
        size = self.effective('font_size') if scope == Scope.AUTO else self.get('font_size', scope)
        font.setPointSizeF(size or max(12.0, font.pointSizeF()))
        return font


def _application_preferences(app):
    try:
        import binaryninja
    except ImportError:
        binaryninja = None
    native = hasattr(binaryninja, 'Settings')
    return EditorPreferences(NativeSettings() if native else MemorySettings(), parent=app)


def editor_preferences(data=None, parent=None):
    """The application's preferences, or a view of them for one BinaryView."""
    preferences = app_singleton('_bnnotes_editor_preferences', _application_preferences)
    if data is not None:
        return EditorPreferences(preferences.backend, parent, data=data, hub=preferences)
    return preferences


def toggle_author_bar(preferences):
    """Flip Show Author Bar as it applies to `preferences`, in the scope it comes from: a
    Project or BinaryView override if one decides it, else the Application preference."""
    shown, origin = preferences.state('show_authors')
    preferences.set('show_authors', not shown,
                    origin if origin in (Scope.PROJECT, Scope.VIEW) else Scope.APPLICATION)


def _small_font(widget, *, bold=False, smaller=2):
    font = widget.font()
    font.setPointSizeF(max(8, font.pointSizeF() - smaller))
    font.setBold(bold)
    return font


def _paint_frame(widget, fill, border, radius):
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(border, 1))
    painter.setBrush(fill)
    painter.drawRoundedRect(QRectF(widget.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    painter.end()


class SettingTitle(QLabel):
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.buddy() is not None:
            self.buddy().click()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class ControlRow(QWidget):
    """Keep related inputs inline whenever their available width permits it."""

    def __init__(self, widgets, parent):
        super().__init__(parent)
        self._widgets = widgets
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        for widget in widgets:
            self._layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignLeft)
        self._layout.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        size = super().sizeHint()
        size.setWidth(sum(widget.sizeHint().width() for widget in self._widgets)
                      + self._layout.spacing() * (len(self._widgets) - 1))
        return size

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        size.setWidth(max(widget.minimumSizeHint().width() for widget in self._widgets))
        return size

    def resizeEvent(self, event):
        super().resizeEvent(event)
        direction = (QBoxLayout.Direction.LeftToRight if self.width() >= self.sizeHint().width()
                     else QBoxLayout.Direction.TopToBottom)
        if direction != self._layout.direction():
            self._layout.setDirection(direction)
            self.updateGeometry()


class SettingsCard(QWidget):
    def __init__(self, title, parent=None, *, detail=None):
        super().__init__(parent)
        self.setAccessibleName(title)
        self._overridden = False
        self._panel = self._control = self._source_layout = None
        self._side_by_side = self._inline_control = False
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(10, 8, 10, 8)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(6)
        self._main = QWidget(self)
        self._main.installEventFilter(self)
        self.body = QVBoxLayout(self._main)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(4)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._grid.addWidget(self._main, 0, 0, alignment=Qt.AlignmentFlag.AlignTop)
        self._grid.setColumnStretch(0, 1)
        self.heading = QHBoxLayout()
        self.heading.setSpacing(6)
        self.title = SettingTitle(title, self)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        if detail:
            self.title.setTextFormat(Qt.TextFormat.RichText)
            self.title.setText(f'{escape(title)} '
                               f'<span style="font-weight: normal">({escape(detail)})</span>')
        self.heading.addWidget(self.title, 1)
        self.body.addLayout(self.heading)

    def set_control(self, control):
        self._control = control
        self.body.insertWidget(1, control)
        self._arrange_control()

    def set_source_row(self, row):
        self._source_layout = row
        self.body.addLayout(row)

    def set_override_panel(self, panel):
        self._panel = panel
        self._grid.addWidget(panel, 1, 0)
        self._arrange()

    def set_overridden(self, overridden):
        if overridden != self._overridden:
            self._overridden = overridden
            self._arrange()
            self.update()

    def _arrange(self):
        side = self._overridden and self._panel is not None and self.width() >= 600
        if side != self._side_by_side:
            self._side_by_side = side
            self._grid.removeWidget(self._panel)
            self._panel.setMaximumWidth(340 if side else 16777215)
            self._grid.addWidget(self._panel, 0 if side else 1, 1 if side else 0,
                                 alignment=Qt.AlignmentFlag.AlignTop)
            self._grid.setColumnStretch(0, 3 if side else 1)
            self._grid.setColumnStretch(1, 2 if side else 0)
            self.updateGeometry()

    def _arrange_control(self):
        if self._control is None:
            return
        title_width = self.title.fontMetrics().horizontalAdvance(self.title.text())
        inline = self._main.width() >= title_width + self._control.sizeHint().width() + 14
        if inline == self._inline_control:
            return
        self._inline_control = inline
        self.heading.removeWidget(self._control)
        self.body.removeWidget(self._control)
        if inline:
            self.heading.addWidget(self._control, alignment=Qt.AlignmentFlag.AlignRight)
        else:
            self.body.insertWidget(1, self._control)
        self.updateGeometry()

    def eventFilter(self, watched, event):
        if watched is self._main and event.type() == QEvent.Type.Resize:
            self._arrange_control()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange()

    def minimumSizeHint(self):
        # The stacked minimum, so wide cards can shrink into a sidebar.
        heading = [self.heading.itemAt(i).widget() for i in range(self.heading.count())]
        heading = [widget for widget in heading
                   if widget is not None and widget is not self._control]
        widths = [sum(widget.minimumSizeHint().width() for widget in heading)
                  + self.heading.spacing() * max(0, len(heading) - 1)]
        if self._control is not None:
            widths.append(self._control.minimumSizeHint().width())
        if self._source_layout is not None:
            widths.append(self._source_layout.minimumSize().width())
        if self._panel is not None and self._overridden:
            widths.append(self._panel.minimumSizeHint().width())
        margins = self._grid.contentsMargins()
        return QSize(max(widths) + margins.left() + margins.right(),
                     self._grid.minimumSize().height())

    def paintEvent(self, _event):
        palette = self.palette()
        dark = palette.color(QPalette.ColorRole.Window).lightness() < 128
        text = palette.color(QPalette.ColorRole.WindowText)
        _paint_frame(self, opacity(text, .07 if dark else .05),
                     opacity(text, .24 if self._overridden else .12), 5)


class ScopeBadge(QLabel):
    def __init__(self, text, parent, *, active=False):
        super().__init__(text, parent)
        self.active = active
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setContentsMargins(5, 3, 5, 3)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFont(_small_font(self, bold=active))

    def paintEvent(self, event):
        text = self.palette().color(QPalette.ColorRole.WindowText)
        _paint_frame(self, opacity(text, .12 if self.active else .06),
                     opacity(text, .35 if self.active else .14), 4)
        super().paintEvent(event)


class OverrideRow(QWidget):
    def __init__(self, scope, parent):
        super().__init__(parent)
        self.scope = ScopeBadge(SCOPE_LABELS[scope], self)
        self.value = QLabel(self)
        self.value.setTextFormat(Qt.TextFormat.PlainText)
        self.value.setWordWrap(True)
        self.same = QLabel('Same value', self)
        self.same.setFont(_small_font(self.same))
        self.same.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.same.setWordWrap(True)
        value_column = self._value_column = QVBoxLayout()
        value_column.setContentsMargins(0, 0, 0, 0)
        value_column.setSpacing(1)
        value_column.addWidget(self.value)
        value_column.addWidget(self.same)
        self.active = ScopeBadge('ACTIVE', self, active=True)
        self._stacked = False
        self._scope_column = QVBoxLayout()
        self._scope_column.setContentsMargins(0, 0, 0, 0)
        self._scope_column.setSpacing(4)
        self._scope_column.addWidget(self.scope, alignment=Qt.AlignmentFlag.AlignLeft)
        row = self._row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        row.addLayout(self._scope_column)
        row.setAlignment(self._scope_column, Qt.AlignmentFlag.AlignTop)
        row.addLayout(value_column, 1)
        row.addWidget(self.active, alignment=Qt.AlignmentFlag.AlignTop)

    def set_value(self, value, *, active, same):
        self.value.setText(value)
        self.value.setToolTip(value)
        font = self.value.font()
        font.setBold(active)
        self.value.setFont(font)
        self.scope.active = active
        self.scope.update()
        self.active.setVisible(active)
        self.same.setVisible(same)
        self._arrange_badge()
        self.setAccessibleName(
            f'{self.scope.text()} override: {value}' + (', active' if active else '')
            + (', same value' if same else ''),
        )

    def _arrange_badge(self):
        required = (self.scope.minimumWidth() + self.value.minimumSizeHint().width()
                    + self.active.sizeHint().width() + self._row.spacing() * 2)
        stacked = not self.active.isHidden() and self.width() < required
        if stacked != self._stacked:
            self._stacked = stacked
            self._row.removeWidget(self.active)
            self._scope_column.removeWidget(self.active)
            if stacked:
                self._scope_column.addWidget(self.active, alignment=Qt.AlignmentFlag.AlignLeft)
            else:
                self._row.addWidget(self.active, alignment=Qt.AlignmentFlag.AlignTop)
        value_width = self.width() - self.scope.minimumWidth() - self._row.spacing()
        if not stacked and not self.active.isHidden():
            value_width -= self.active.sizeHint().width() + self._row.spacing()
        value_height = self._value_column.heightForWidth(max(1, value_width))
        self.setMinimumHeight(max(self._scope_column.minimumSize().height(), value_height,
                                  self._value_column.minimumSize().height()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_badge()

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        size.setWidth(max(self.scope.minimumWidth(), self.active.sizeHint().width())
                      + self.value.minimumSizeHint().width() + self._row.spacing())
        return size


class OverridePanel(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAccessibleName(f'Overrides for {parent.accessibleName()}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(6)
        caption = QLabel('OVERRIDES', self)
        caption.setFont(_small_font(caption, bold=True))
        caption.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout.addWidget(caption)
        self.rows = {scope: OverrideRow(scope, self) for scope in (Scope.PROJECT, Scope.VIEW)}
        self._state = None
        scope_width = max(row.scope.sizeHint().width() for row in self.rows.values())
        for row in self.rows.values():
            row.scope.setFixedWidth(scope_width)
            layout.addWidget(row)
            row.hide()
        self.hide()

    def set_overrides(self, values, current_value, format_value):
        state = tuple(values), current_value
        if state == self._state:
            return
        self._state = state
        present = {scope for scope, _value in values}
        for scope, row in self.rows.items():
            if scope not in present:
                row.hide()
        for index, (scope, value) in enumerate(values):
            row = self.rows[scope]
            row.set_value(format_value(value), active=index == len(values) - 1,
                          same=value == current_value)
            row.show()
        self.setVisible(bool(values))

    def paintEvent(self, _event):
        palette = self.palette()
        _paint_frame(self, opacity(palette.color(QPalette.ColorRole.Window), .6),
                     opacity(palette.color(QPalette.ColorRole.WindowText), .1), 4)


def scope_style(ui, enabled):
    class ScopeStyle(ui.GlobalAreaTabStyle):
        def paintTab(self, widget, painter, info, index, count, active, state, rect):
            painter.save()
            if not enabled[index]:
                painter.setOpacity(0.4)
                state = ui.DockableTabInteractionState.NoTabInteraction
            super().paintTab(widget, painter, info, index, count, active, state, rect)
            painter.restore()

    return ScopeStyle()


class ScopeTabBar(QWidget):
    currentChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('bnnotes-scope-tabs')
        self.setAccessibleName('Settings scope')
        self._enabled = []
        ui = sys.modules.get('binaryninjaui')
        self.native_style = None
        if hasattr(ui, 'GlobalAreaTabStyle'):
            # Keep the collection alive until after its native bar is destroyed.
            self._collection = ui.DockableTabCollection()
            self._bar = ui.DockableTabBar(self._collection)
            self.native_style = scope_style(ui, self._enabled)
            self._bar.setTabStyle(self.native_style)
            self._bar.setCanCreateNewWindow(False)
            self._bar.setCanSplit(False)
            self._bar.setAutoFillBackground(False)
            self._bar.viewport().setAutoFillBackground(False)
            self._bar.installEventFilter(self)
            self._bar.viewport().installEventFilter(self)
        else:
            self._bar = QTabBar(self)
            self._bar.setDrawBase(False)
            self._bar.setExpanding(False)
            self._bar.setUsesScrollButtons(True)
            self._bar.setStyleSheet('''
                QTabBar::tab { background: transparent; border: none; padding: 5px 10px; }
                QTabBar::tab:selected { border-bottom: 1px solid palette(text); }
                QTabBar::tab:disabled { color: palette(mid); }
            ''')
        self._bar.setAccessibleName('Settings scope')
        self._bar.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._bar.currentChanged.connect(self.currentChanged.emit)
        self.setFocusProxy(self._bar)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 0)
        layout.addWidget(self._bar, alignment=Qt.AlignmentFlag.AlignBottom)

    def addTab(self, title):
        self._enabled.append(True)
        index = self._bar.addTab(title)
        if self.native_style is not None:
            self._bar.setCanCloseTab(index, False)
        return index

    def count(self):
        return self._bar.count()

    def tabText(self, index):
        return self._bar.tabText(index)

    def setTabText(self, index, text):
        if self._bar.tabText(index) != text:
            self._bar.setTabText(index, text)
            self._bar.updateGeometry()
            if self.native_style is not None:
                self._bar.ensureCurrentTabVisible()

    def setTabToolTip(self, index, text):
        self._bar.setTabToolTip(index, text)

    def currentIndex(self):
        return self._bar.currentIndex()

    def setCurrentIndex(self, index):
        if 0 <= index < self.count() and self.isTabEnabled(index):
            self._bar.setCurrentIndex(index)

    def isTabEnabled(self, index):
        return self._enabled[index]

    def setTabEnabled(self, index, enabled):
        self._enabled[index] = enabled
        if self.native_style is None:
            self._bar.setTabEnabled(index, enabled)
        elif not enabled and self.currentIndex() == index:
            replacement = next((i for i, value in enumerate(self._enabled) if value), -1)
            self.setCurrentIndex(replacement)
        if self.native_style is not None:
            self._bar.viewport().update()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            index = self._bar.tabAt(event.position().toPoint())
            if index >= 0 and not self.isTabEnabled(index):
                return True
        elif (event.type() == QEvent.Type.MouseMove
              and event.buttons() & Qt.MouseButton.LeftButton):
            # Scopes are fixed identities, rather than movable/detachable documents.
            return True
        elif event.type() == QEvent.Type.KeyPress:
            enabled = [i for i, value in enumerate(self._enabled) if value]
            if enabled and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right,
                                          Qt.Key.Key_Home, Qt.Key.Key_End):
                current = (enabled.index(self.currentIndex())
                           if self.currentIndex() in enabled else 0)
                target = {Qt.Key.Key_Left: max(0, current - 1),
                          Qt.Key.Key_Right: min(len(enabled) - 1, current + 1),
                          Qt.Key.Key_Home: 0, Qt.Key.Key_End: len(enabled) - 1}[event.key()]
                self.setCurrentIndex(enabled[target])
                return True
        return super().eventFilter(watched, event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        marker = self.palette().color(QPalette.ColorRole.WindowText)
        tint = QColor(marker)
        tint.setAlpha(12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(tint)
        painter.drawRoundedRect(QRectF(self.rect()), 4, 4)
        marker.setAlpha(100)
        painter.setBrush(marker)
        painter.drawRoundedRect(QRectF(0, 7, 2, max(0, self.height() - 14)), 1, 1)
        painter.end()


SCOPES = (Scope.APPLICATION, Scope.PROJECT, Scope.VIEW)


def label(text, parent):
    widget = QLabel(text, parent)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


def setting_value(name, value):
    if name == 'font_size':
        return f'{value} pt' if value else 'Default font'
    if isinstance(value, bool):
        return 'On' if value else 'Off'
    return str(value)


class NotepadSettingsWidget(QWidget):
    CHECKBOXES = ('word_wrap', 'show_authors', 'hlil_show_addresses', 'export_embeds_as_code',
                  'follow_view_navigation', 'mcp_default_allow', 'mcp_allow_create')

    def __init__(self, parent=None, *, preferences=None):
        super().__init__(parent)
        self.setObjectName('bnnotes-settings')
        self.setAccessibleName('Notepad Settings')
        self.preferences = None
        self.scope = Scope.APPLICATION
        self.origins, self.reset_buttons = {}, {}
        self.overrides = {}
        self.items = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scope_tabs = ScopeTabBar(self)
        for scope in SCOPES:
            self.scope_tabs.addTab(SCOPE_LABELS[scope])
        scope_row = QHBoxLayout()
        scope_row.setContentsMargins(16, 6, 16, 0)
        scope_row.addWidget(self.scope_tabs)
        layout.addLayout(scope_row)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(scroll)
        body = QWidget(scroll)
        content = QVBoxLayout(body)
        content.setContentsMargins(16, 12, 16, 16)
        content.setSpacing(8)
        self.description = label('', body)
        content.addWidget(self.description)
        self.error = label('', body)
        self.error.hide()
        content.addWidget(self.error)

        self._section('Editor', content)
        card = self._card('font_size', content)
        self.default_font = QCheckBox('Use Application Default', card)
        self.font_size = QSpinBox(card)
        self.font_size.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.font_size.setSuffix(' pt')
        self.font_size.setMaximumWidth(120)
        self.font_size.setKeyboardTracking(False)
        self.font_size.setAccessibleName(setting_title('font_size'))
        card.set_control(ControlRow((self.default_font, self.font_size), card))
        self._origin_row('font_size', card)
        self.word_wrap = self._checkbox_card('word_wrap', content)
        self.show_authors = self._checkbox_card('show_authors', content)
        self.hlil_show_addresses = self._checkbox_card('hlil_show_addresses', content)

        self._section('Export', content)
        self.export_embeds_as_code = self._checkbox_card('export_embeds_as_code', content)

        self._section('Navigation', content)
        self.follow_view_navigation = self._checkbox_card('follow_view_navigation', content)

        self._section("Agents (MCP)", content)
        self.mcp_default_allow = self._checkbox_card('mcp_default_allow', content)
        card = self._card('mcp_allow_delete', content)
        self.mcp_allow_delete = QComboBox(card)
        for text, value in setting_choices('mcp_allow_delete'):
            self.mcp_allow_delete.addItem(text, value)
        self.mcp_allow_delete.setAccessibleName(setting_title('mcp_allow_delete'))
        self.mcp_allow_delete.setMinimumContentsLength(12)
        card.set_control(self.mcp_allow_delete)
        self._origin_row('mcp_allow_delete', card)
        self.mcp_allow_create = self._checkbox_card('mcp_allow_create', content)

        self.restore_defaults = QPushButton('Restore Defaults', body)
        content.addWidget(self.restore_defaults, alignment=Qt.AlignmentFlag.AlignLeft)
        content.addWidget(label(
            'All of these items can also be configured in BinaryNinja settings, '
            'this is just a reduced version w/ all of the Notes settings here :p', body,
        ))
        content.addStretch(1)
        scroll.setWidget(body)
        self.setFocusProxy(self.default_font)

        self.scope_tabs.currentChanged.connect(self._scope_changed)
        self.default_font.toggled.connect(self._default_font_changed)
        self.font_size.valueChanged.connect(lambda value: self._set('font_size', value))
        for name in self.CHECKBOXES:
            getattr(self, name).toggled.connect(lambda value, name=name: self._set(name, value))
        self.mcp_allow_delete.currentIndexChanged.connect(
            lambda _index: self._set('mcp_allow_delete', self.mcp_allow_delete.currentData()),
        )
        self.restore_defaults.clicked.connect(
            lambda: self._write(lambda: self.preferences.reset_defaults(self.scope)),
        )
        self.bind_preferences(preferences or editor_preferences())

    def _section(self, title, layout):
        heading = label(title, layout.parentWidget())
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)

    def _card(self, name, layout):
        card = SettingsCard(setting_title(name), layout.parentWidget(),
                            detail=setting_description(name))
        card.setObjectName(f'bnnotes-setting-{name}')
        self.items[name] = card
        layout.addWidget(card)
        return card

    def _checkbox_card(self, name, layout):
        card = self._card(name, layout)
        checkbox = QCheckBox(card)
        checkbox.setAccessibleName(setting_title(name))
        card.heading.insertWidget(0, checkbox, alignment=Qt.AlignmentFlag.AlignTop)
        card.title.setBuddy(checkbox)
        self._origin_row(name, card)
        return checkbox

    def _origin_row(self, name, card):
        row = QHBoxLayout()
        origin = label('', card)
        origin.setFont(_small_font(origin, smaller=1))
        origin.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.origins[name] = origin
        row.addWidget(origin, 1)
        reset = QToolButton(card)
        reset.setText('Reset')
        reset.setAutoRaise(True)
        reset.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        reset.setAccessibleName(f'Reset {name.replace("_", " ")} in this scope')
        reset.clicked.connect(lambda: self._write(lambda: self.preferences.reset(name, self.scope)))
        self.reset_buttons[name] = reset
        row.addWidget(reset, alignment=Qt.AlignmentFlag.AlignTop)
        card.set_source_row(row)
        overridden = OverridePanel(card)
        self.overrides[name] = overridden
        card.set_override_panel(overridden)

    def bind_preferences(self, preferences):
        if self.preferences is not None:
            self.preferences.changed.disconnect(self._refresh)
        self.preferences = preferences
        preferences.changed.connect(self._refresh)
        # Don't refresh an intermediate scope while rebinding.
        with QSignalBlocker(self.scope_tabs):
            for index, scope in enumerate(SCOPES):
                self.scope_tabs.setTabEnabled(index, preferences.scope_available(scope))
            if not preferences.scope_available(self.scope):
                self.scope = Scope.APPLICATION
            self.scope_tabs.setCurrentIndex(SCOPES.index(self.scope))
        self.error.hide()
        self._refresh()

    def _scope_changed(self, index):
        self.scope = SCOPES[index]
        self.error.hide()
        self._refresh()

    def _write(self, operation):
        try:
            operation()
        except (RuntimeError, ValueError) as error:
            self.error.setText(str(error))
            self.error.show()
        else:
            self.error.hide()
        self._refresh()

    def _set(self, name, value):
        self._write(lambda: self.preferences.set(name, value, self.scope))

    def _default_font_changed(self, checked):
        self._set('font_size', 0 if checked else self.font_size.value())

    def _refresh(self, _name=None):
        if self.preferences is None:
            return
        preferences, scope = self.preferences, self.scope
        data = preferences.data
        project = project_for(data)
        project_name = getattr(project, 'name', '') or 'Untitled'
        titles = (f'Project: {project_name}' if project is not None else 'Project',
                  f'BinaryView: {view_name(data)}' if data is not None else 'BinaryView')
        for index, title in enumerate(titles, 1):
            self.scope_tabs.setTabText(index, title)
            self.scope_tabs.setTabToolTip(index, title)
        if scope == Scope.APPLICATION:
            description = 'Application defaults. Project and BinaryView overrides take precedence.'
        elif scope == Scope.PROJECT:
            description = 'Overrides Application settings.'
        else:
            description = 'Overrides Application and Project settings; saved with the BNDB.'
        self.description.setText(description)
        use_default = preferences.get('font_size', scope) == 0
        with QSignalBlocker(self.default_font), QSignalBlocker(self.font_size):
            self.default_font.setChecked(use_default)
            self.font_size.setValue(round(preferences.font(scope).pointSizeF()))
            self.font_size.setEnabled(not use_default)
        for name in self.CHECKBOXES:
            control = getattr(self, name)
            with QSignalBlocker(control):
                control.setChecked(preferences.get(name, scope))
        with QSignalBlocker(self.mcp_allow_delete):
            self.mcp_allow_delete.setCurrentIndex(max(0, self.mcp_allow_delete.findData(
                preferences.get('mcp_allow_delete', scope))))
        for name in DEFAULTS:
            value, origin = preferences.state(name, scope)
            self.origins[name].setText(f'Set in {SCOPE_LABELS[scope]}' if origin == scope else
                                      f'Inherited from {SCOPE_LABELS[origin]}')
            self.reset_buttons[name].setEnabled(origin == scope)
            later = preferences.later_overrides(name, scope)
            overridden = self.overrides[name]
            overridden.set_overrides(later, value,
                                     lambda value, name=name: setting_value(name, value))
            self.items[name].set_overridden(bool(later))
            overridden.setToolTip(
                f'Overrides for {data.file.display_name}. Changes here take effect when '
                'the later-scope overrides are reset.' if later else '',
            )
        self.restore_defaults.setText('Restore Defaults' if scope == Scope.APPLICATION
                                      else 'Reset Scope Overrides')
        self.restore_defaults.setToolTip('Remove only Notes overrides in the selected scope.')
