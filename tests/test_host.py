import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtWidgets import QMenu, QWidget

from src.ui.host import (
    NativeUIActions,
    RootContextMenus,
    RootMenuAction,
    copy_function_bnurl_action,
    copy_view_bnurl_actions,
    host_menu_context,
)


class LinearView(QWidget):
    pass


@pytest.fixture
def menus(qapp):
    owners, hooks = [], []

    def create(owner_type=LinearView):
        owner = owner_type()
        owner.show()
        owners.append(owner)
        return owner, QMenu(owner), hooks

    yield create
    for hook in hooks:
        hook.uninstall()
        hook.deleteLater()
    for owner in owners:
        owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class ComponentTree(QWidget):
    pass


class DisassemblyView(QWidget):
    pass


@pytest.fixture
def native_actions(qapp, tmp_path, monkeypatch):
    class Action:
        registered = set()
        keys = {}

        def __init__(self, activate, valid):
            self.activate, self.valid = activate, valid

        @classmethod
        def isActionRegistered(cls, name):
            return name in cls.registered

        @classmethod
        def registerAction(cls, name):
            cls.registered.add(name)

        @classmethod
        def getKeyBinding(cls, name):
            return cls.keys.get(name, [])

    class Handler:
        def __init__(self, owner=None):
            self.owner = owner
            self.bindings = {}
            self.executions = []

        def actionContext(self):
            if self.owner.inherits('ComponentTree') or self.owner.inherits('ComponentTreeView'):
                return SimpleNamespace(widget=None, binaryView=None, address=0, function=None)
            return SimpleNamespace(widget=self.owner, binaryView=data,
                                   address=self.owner.address, function=functions[0x1000])

        def setupActionHandler(self, widget):
            self.owner = widget
            widget.setProperty('__UIActionHandler', self)

        def bindAction(self, name, action):
            self.bindings[name] = action

        def unbindAction(self, name):
            self.bindings.pop(name)

        def isValidAction(self, name, context=None):
            return name in self.bindings and self.bindings[name].valid(
                context if context is not None else self.actionContext()
            )

        def executeAction(self, name, context=None):
            context = context if context is not None else self.actionContext()
            self.executions.append((name, context))
            if self.isValidAction(name, context):
                self.bindings[name].activate(context)

    functions = {address: SimpleNamespace(start=address, name=f'function_{address:x}')
                 for address in (0x1000, 0x2000)}
    data = SimpleNamespace(
        file=SimpleNamespace(filename=str(tmp_path / 'sample.bndb')),
        is_valid_offset=lambda address: 0x1000 <= address < 0x3000,
        get_function_at=lambda address: functions.get(address),
        get_functions_containing=lambda address: [function for start, function in functions.items()
                                                 if start <= address < start + 0x100],
    )
    frame = SimpleNamespace(getCurrentBinaryView=lambda: data)
    ui = SimpleNamespace(
        UIAction=Action,
        UIActionHandler=Handler,
        UIActionContext=lambda: SimpleNamespace(widget=None, binaryView=None, address=0),
        UIContext=SimpleNamespace(contextForWidget=lambda _owner: None,
                                  currentViewFrameForWidget=lambda _owner: frame),
    )
    monkeypatch.setitem(sys.modules, 'binaryninjaui', ui)
    owners, menus = [], []
    specs = [copy_function_bnurl_action(), *copy_view_bnurl_actions()]
    controller = NativeUIActions(ui, specs)

    def create(owner_type):
        owner = owner_type()
        owners.append(owner)
        owner.address = 0x1024
        handler = Handler(owner)
        owner.setProperty('__UIActionHandler', handler)
        owner.show()
        qapp.processEvents()
        return owner, handler

    yield SimpleNamespace(ui=ui, controller=controller, create=create, data=data,
                          specs=specs, menus=menus)
    for menu_hook in menus:
        menu_hook.uninstall()
        menu_hook.deleteLater()
    controller.uninstall()
    controller.deleteLater()
    for owner in owners:
        owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_root_actions_preserve_host_actions_and_exclude_submenus(menus, qapp):
    owner, menu, hooks = menus()
    data = object()
    owner.setProperty('__UIActionHandler', SimpleNamespace(actionContext=lambda: SimpleNamespace(
        address=0x100000400, binaryView=data,
    )))
    existing = menu.addAction('Native action')
    plugins = menu.addMenu('Plugins')
    plugins.addAction('Existing plugin')
    received = []
    hook = RootContextMenus([RootMenuAction('Root action', received.append)])
    hooks.append(hook)
    menu.popup(QPoint(20, 20))
    qapp.processEvents()
    action = menu.actions()[0]
    assert action.text() == 'Root action'
    assert action.menu() is None
    assert action.parent() is menu
    assert menu.actions()[1].isSeparator()
    assert menu.actions()[2] is existing
    action.trigger()
    assert received[0].kind == 'linear'
    assert received[0].binary_view is data
    assert received[0].address == 0x100000400
    plugins.popup(QPoint(30, 30))
    qapp.processEvents()
    assert [action.text() for action in plugins.actions()] == ['Existing plugin']
    plugins.hide()
    menu.hide()


@pytest.mark.parametrize('owner_type, kind', [(LinearView, 'linear'), (DisassemblyView, 'graph')])
def test_view_copy_group_has_distinct_address_and_function_urls(
    menus, qapp, tmp_path, owner_type, kind,
):
    owner, menu, hooks = menus(owner_type)
    for text in ['Comment', '', 'Copy', 'Copy Address', '', 'Patch']:
        menu.addAction(text) if text else menu.addSeparator()
    function = SimpleNamespace(start=0x100001000, name='view_function')
    address = function.start + 0x24
    path = tmp_path / 'view notes.bndb'
    data = SimpleNamespace(file=SimpleNamespace(filename=str(path)),
                           is_valid_offset=lambda offset: offset == address)
    owner.setProperty('__UIActionHandler', SimpleNamespace(actionContext=lambda: SimpleNamespace(
        address=address, binaryView=data, function=function,
    )))
    received = host_menu_context(kind, owner)
    assert received.address == address
    assert received.native_context.function is function
    hooks.append(RootContextMenus(copy_view_bnurl_actions()))
    expected = ['Comment', '', 'Copy', 'Copy Address', 'Copy BNURL to Address',
                'Copy BNURL to Function', '', 'Patch']
    for _ in range(2):
        menu.popup(QPoint(20, 20))
        qapp.processEvents()
        assert [action.text() for action in menu.actions()] == expected
        for index, expr in ((4, f'{address:#x}'), (5, 'view_function')):
            action = menu.actions()[index]
            assert action.menu() is None and action.isEnabled()
            action.trigger()
            assert qapp.clipboard().text() == (
                'binaryninja:' + path.as_uri().removeprefix('file:') + f'?expr={expr}'
            )
        menu.hide()


def test_host_hooks_never_install_an_application_event_filter(qapp, monkeypatch):
    def reject(_filter):
        raise AssertionError('A QApplication filter wraps non-widget Qt singletons')

    monkeypatch.setattr(qapp, 'installEventFilter', reject)
    actions = NativeUIActions(SimpleNamespace(), [])
    menus = RootContextMenus([])
    try:
        assert actions._installed and menus._installed
    finally:
        menus.uninstall()
        actions.uninstall()
        menus.deleteLater()
        actions.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

