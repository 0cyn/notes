from __future__ import annotations

import binaryninja
import binaryninjaui as ui
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ..mcp import TOOLS
from .host import (
    NativeUIActions,
    RootContextMenus,
    copy_function_bnurl_action,
    copy_view_bnurl_actions,
)
from .notebook import NotesWidget
from .preferences import editor_preferences, toggle_author_bar
from .session import LiveNotes

NOTEPAD_VIEW = "Notepad"

_registered_types: list[object] = []
_host_menus = None
_host_actions = None


class NotepadView(QWidget, ui.View):
    def __init__(self, parent, data):
        QWidget.__init__(self, parent)
        ui.View.__init__(self)
        self.data = data
        self.current_offset = int(data.start)
        self._current_function = None
        ui.View.setBinaryDataNavigable(self, True)
        self.setupView(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.notepad = NotesWidget(data, self)
        self.notepad.addressActivated.connect(self._activate_address)
        layout.addWidget(self.notepad)
        self.setFocusProxy(self.notepad)

    def getData(self):
        return self.data

    def getCurrentOffset(self):
        return self.current_offset

    def getSelectionOffsets(self):
        return self.current_offset, self.current_offset

    def setSelectionOffsets(self, selection):
        self.navigate(selection.start if hasattr(selection, "start") else selection[0])

    def getFont(self):
        editor = self.notepad.editor if hasattr(self, "notepad") else None
        return editor.document().defaultFont() if editor is not None else QApplication.font()

    def getCurrentFunction(self):
        return self._current_function

    def navigate(self, offset):
        return self._navigate(offset)

    def navigateToFunction(self, function, offset):
        return self._navigate(offset, function)

    def navigateToViewLocation(self, location, center=False):
        return self._navigate(location.getOffset(), location.getFunction())

    def _navigate(self, offset, function=None):
        self.current_offset = int(offset)
        functions = (self.data.get_functions_containing(self.current_offset)
                     if function is None else ())
        self._current_function = function or next(iter(functions), None)
        if hasattr(self, "notepad"):
            self.notepad.navigate_to_address(self.current_offset, function=self._current_function)
        # Accept unmatched locations: stay open without jumping to an unrelated note.
        return True

    def _activate_address(self, offset):
        frame = ui.ViewFrame.viewFrameForWidget(self)
        if frame is None:
            self.navigate(offset)
        else:
            frame.navigate(frame.getCurrentView(), offset)
            self.updateCrossReferenceSelection(frame)
            self.forceSyncFromView(frame)


class NotepadViewType(ui.ViewType):
    def __init__(self):
        super().__init__(NOTEPAD_VIEW, "Markdown Notepad")

    def getPriority(self, data, filename):
        return 1 if data is not None else 0

    def create(self, data, view_frame):
        return NotepadView(view_frame, data)


class NotepadSidebarWidget(ui.SidebarWidget):
    def __init__(self, frame, data):
        super().__init__(NOTEPAD_VIEW)
        self.actionHandler = ui.UIActionHandler()
        self.actionHandler.setupActionHandler(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.notepad = NotesWidget(data, self, compact=True)
        self.notepad.addressActivated.connect(self._activate_address)
        layout.addWidget(self.notepad)
        self.setFocusProxy(self.notepad)

    def notifyViewChanged(self, view_frame):
        view = view_frame.getCurrentViewInterface() if view_frame is not None else None
        self.notepad.set_data(view.getData() if view is not None else None)

    def notifyOffsetChanged(self, offset):
        self.notepad.navigate_to_address(offset)

    def _activate_address(self, offset):
        frame = ui.UIContext.currentViewFrameForWidget(self)
        data = frame.getCurrentBinaryView() if frame is not None else None
        if (data is not None and self.notepad.notebook is not None
                and data.file.session_id == self.notepad.notebook.storage.session_id):
            frame.navigate(frame.getCurrentView(), offset)
            frame.forceSyncFromView()


def _notepad_icon() -> QImage:
    # Binary Ninja expects white shapes on a black 56px image and themes it itself.
    icon = QImage(56, 56, QImage.Format.Format_RGB32)
    icon.fill(Qt.GlobalColor.black)
    painter = QPainter(icon)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("white"), 3))
    painter.drawRoundedRect(12, 7, 33, 42, 3, 3)
    for y in (17, 27, 37):
        painter.drawLine(7, y, 16, y)
        painter.drawLine(23, y, 38, y)
    painter.end()
    return icon


class NotepadSidebarWidgetType(ui.SidebarWidgetType):
    def __init__(self):
        super().__init__(_notepad_icon(), NOTEPAD_VIEW)

    def createWidget(self, frame, data):
        return NotepadSidebarWidget(frame, data)

    def canUseAsPane(self, split_pane_widget, data):
        return data is not None

    def createPane(self, split_pane_widget, data):
        return _new_notepad_pane(split_pane_widget, data, split_pane_widget.currentViewFrame())

    def defaultLocation(self):
        return ui.SidebarWidgetLocation.RightContent

    def contextSensitivity(self):
        return ui.SidebarContextSensitivity.SelfManagedSidebarContext


def _can_open(context) -> bool:
    return context.context is not None and context.binaryView is not None


def _open_view(context):
    frame = context.context.getCurrentViewFrame()
    if frame is not None:
        frame.setViewType(f"{NOTEPAD_VIEW}:{context.binaryView.view_type}")


def _toggle_author_bar(context):
    # With a file open, flip the setting as it applies to that file (its overrides included).
    data = context.binaryView if context is not None else None
    toggle_author_bar(editor_preferences(data))


def _new_notepad_pane(parent, data, source):
    # Custom frames don't apply ui.panes.sync; capture the source pane first.
    sync = binaryninja.Settings().get_bool('ui.panes.sync')
    group = source.syncGroup() if source is not None else None
    group_id = group.identifier() if group is not None else None
    offset = source.getCurrentOffset() if source is not None else None
    frame = ui.ViewFrame(parent, parent.fileContext(), f'{NOTEPAD_VIEW}:{data.view_type}')
    if not frame.getCurrentBinaryView():
        frame.deleteLater()
        return None
    pane = ui.ViewPane(frame)
    if sync:
        # Seed the new pane without publishing its initial address to peers.
        if offset is not None:
            frame.navigate(frame.getCurrentView(), offset)
        if group_id is None:
            frame.enableSync()
        else:
            frame.enableSync(group_id)
    return pane


def _open_pane(context):
    source = context.context.getCurrentViewFrame()
    if source is not None:
        parent = source.parentWidget()
        while parent is not None and not isinstance(parent, ui.SplitPaneWidget):
            parent = parent.parentWidget()
        if parent is not None:
            # ViewFrame needs an attached parent to resolve its UI context.
            pane = _new_notepad_pane(parent, context.binaryView, source)
            if pane is not None:
                context.context.openPane(pane)


def _open_onboarding(_data=None):
    """Show the Notes sidebar and its welcome tour."""
    # Plugin commands can run off the UI thread; widgets belong to it.
    def show():
        context = ui.UIContext.activeContext()
        if context is None:
            context = next(iter(ui.UIContext.allContexts()), None)
        sidebar = context.sidebar() if context is not None else None
        if sidebar is None:
            binaryninja.log_warn('Notes: no window to show the welcome tour in.')
            return
        sidebar.activate(NOTEPAD_VIEW)
        widget = sidebar.widget(NOTEPAD_VIEW)
        notepad = getattr(widget, 'notepad', None)
        if notepad is None:
            binaryninja.log_warn('Notes: the Notes sidebar is unavailable.')
            return
        notepad.open_onboarding()

    binaryninja.execute_on_main_thread(show)


def _install_host_menus():
    global _host_menus, _host_actions
    if _host_menus is None:
        actions = [copy_function_bnurl_action(), *copy_view_bnurl_actions()]
        _host_actions = NativeUIActions(ui, actions)
        _host_menus = RootContextMenus(actions, action_dispatcher=_host_actions)


def register_ui() -> None:
    if _registered_types:
        return
    TOOLS.live = LiveNotes.for_view  # MCP edits go through notebooks open in the UI.
    view_type = NotepadViewType()
    sidebar_type = NotepadSidebarWidgetType()
    ui.ViewType.registerViewType(view_type)
    ui.Sidebar.addSidebarWidgetType(sidebar_type)
    # Python subclasses must remain alive after handing them to the UI core.
    _registered_types.extend((view_type, sidebar_type))
    for name, callback, valid in (
        ("Notepad\\Open Notepad View", _open_view, _can_open),
        ("Notepad\\Open Notepad Pane", _open_pane, _can_open),
        ("Notepad\\Toggle Author Bar", _toggle_author_bar, lambda _context: True),
    ):
        ui.UIAction.registerAction(name)
        ui.UIActionHandler.globalActions().bindAction(name, ui.UIAction(callback, valid))
        ui.Menu.mainMenu("Plugins").addAction(name, "Notepad")
    binaryninja.PluginCommand.register(
        "Notepad\\Open Onboarding",
        "Show the Notes sidebar and its welcome tour.",
        _open_onboarding,
    )
    binaryninja.execute_on_main_thread(_install_host_menus)
