from __future__ import annotations

import os
import threading
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from src.storage import METADATA_KEY

_ids = count(1)
NOW = 1_800_000_000
SOURCE = (
    "# Notes\n\n| **Name** | Value |\n| :--- | ---: |\n"
    "| alpha | `0x10` |\n| beta | *pending* |\n\nAfter table"
)


class FakeView:
    """Model the metadata contract, including distinct analysis/Raw wrappers."""

    def __init__(self, source=None):
        self.values = {}
        self.writes = []
        self.write_error = False
        self.file = SimpleNamespace(session_id=next(_ids), display_name="sample.bin", raw=self)
        if source is not None:
            self.values[METADATA_KEY] = {
                'version': 2, 'notes': [{'id': 'initial', 'text': source}],
            }

    def query_metadata(self, key):
        return self.values[key]

    def store_metadata(self, key, value):
        if self.write_error:
            raise RuntimeError("simulated write failure")
        self.values[key] = value
        self.writes.append(value)


@pytest.fixture
def data():
    return FakeView()


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtCore import QLibraryInfo
    from PySide6.QtWidgets import QApplication

    from src.settings import MemorySettings
    from src.ui.preferences import EditorPreferences

    plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    # Fail before QApplication can call qFatal (or launch a native crash dialog).
    if not any((plugins / "platforms").glob("*offscreen*")):
        pytest.fail("Qt's offscreen plugin is missing; install requirements-dev.txt in .venv.")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    app._bnnotes_editor_preferences = EditorPreferences(MemorySettings(), app)
    yield app
    app.processEvents()


@pytest.fixture(autouse=True)
def reset_editor_preferences(request):
    if 'qapp' not in request.fixturenames:
        yield
        return
    app = request.getfixturevalue('qapp')
    preferences = app._bnnotes_editor_preferences
    preferences.backend.values.clear()
    preferences.synchronize()
    yield
    preferences.backend.values.clear()
    preferences.synchronize()


@pytest.fixture(autouse=True)
def isolate_view_settings(request):
    if 'qapp' not in request.fixturenames:
        yield
        return
    from PySide6.QtCore import QSettings

    from src.ui.common import PersistentViewSettings

    app = request.getfixturevalue('qapp')
    directory = request.getfixturevalue('tmp_path')
    previous = getattr(app, '_bnnotes_view_settings', None)
    store = PersistentViewSettings(QSettings(str(directory / 'ui-state.ini'), QSettings.IniFormat),
                                   app)
    # Tests start past the welcome tour; tests of the tour reset it.
    store.set_app_value('onboarding/done', True)
    app._bnnotes_view_settings = store
    yield
    store.sync()
    if previous is None:
        del app._bnnotes_view_settings
    else:
        app._bnnotes_view_settings = previous
    store.deleteLater()


@pytest.fixture
def notepad(qapp, data):
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid

    from src.ui.notepad import NotepadWidget
    from src.ui.session import notebook_for

    widgets = []

    def create(view=data, **kwargs):
        widget = NotepadWidget(notebook_for(view).default_note(), **kwargs)
        widget.materialize()
        widgets.append(widget)
        return widget

    yield create
    for widget in widgets:
        if isValid(widget):
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def notebook(qapp, data):
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid

    from src.ui.notebook import NotesWidget

    widgets = []

    def create(view=data, **kwargs):
        widget = NotesWidget(view, **kwargs)
        widgets.append(widget)
        return widget

    yield create
    for widget in widgets:
        if isValid(widget):
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def wait_until(predicate, timeout=2000):
    QCoreApplication.processEvents()
    if predicate():
        return
    # Run a real Qt event loop, releasing the GIL between callbacks just as the
    # application does. Repeated QTest.qWait calls can starve yielding workers.
    loop = QEventLoop()
    check = QTimer(loop)
    check.timeout.connect(lambda: loop.quit() if predicate() else None)
    check.start(5)
    deadline = QTimer(loop)
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(timeout)
    loop.exec()
    check.stop()
    deadline.stop()
    loop.deleteLater()
    assert predicate(), 'Preview did not reach the expected state'


class Instruction:
    def __init__(self, address, text):
        self.address, self.text = address, text
        self.instr = self.non_ast = self
        self.tokens = None
        self.rendered_lines = None

    @property
    def lines(self):
        if self.rendered_lines is not None:
            return self.rendered_lines
        tokens = self.tokens if self.tokens is not None else [SimpleNamespace(text=self.text)]
        return [SimpleNamespace(address=self.address, tokens=tokens)]


class Function:
    def __init__(self, start):
        self.start = start
        self.name = 'first_function'
        self.statement = Instruction(start + 4, 'result = worker(arg1)')
        self.return_statement = Instruction(start + 0x10, 'return result')
        self.hlil = SimpleNamespace(instructions=[self.statement, self.return_statement])
        self.threads = []
        self.started = threading.Event()
        self.release = None

    def get_llil_at(self, address):
        self.threads.append(threading.get_ident())
        self.started.set()
        if self.release is not None:
            assert self.release.wait(3)
        if address in (self.start + 4, self.start + 8):
            expression = SimpleNamespace(instr=self.statement)
            return SimpleNamespace(hlil=expression)
        return None


class LinkView(FakeView):
    def __init__(self, filename):
        super().__init__()
        self.file.filename = str(filename)
        self.file.project_file = None
        self.start = 0x100001000
        self.symbols = {'first': self.start, 'second': self.start + 0x1000}
        self.functions = [SimpleNamespace(start=address) for address in self.symbols.values()]

    def parse_expression(self, expression, here=0):
        if expression in self.symbols:
            return self.symbols[expression]
        if expression == 'first+4':
            return self.symbols['first'] + 4
        if expression == '$here':
            return here
        return int(expression, 0)

    def is_valid_offset(self, address):
        return self.start <= address < self.start + 0x3000

    def get_functions_containing(self, address):
        return [function for function in self.functions
                if function.start <= address < function.start + 0x100]


@pytest.fixture
def preview_view(tmp_path):
    data = LinkView(tmp_path / 'long analysis directory' / 'analyzed binary with spaces.bndb')
    data.functions[0] = Function(data.start)
    return data


def file_url(view, expression='first'):
    return f'binaryninja://{quote(view.file.filename)}?expr={expression}'


def _action(menu, text):
    return next(action for action in menu.actions() if action.text() == text)


@pytest.fixture
def block_view(preview_view):
    function = preview_view.functions[0]
    statement = function.statement
    statement.text = 'if (ready)'
    statement.ast = Instruction(statement.address, '')
    lines = [
        '        if (ready)', '        {', '            first()',
        '            if (nested)', '                second()', '        }',
        '        else', '        {', '            fallback()', '        }',
    ]
    statement.ast.rendered_lines = [SimpleNamespace(
        address=statement.address + index, tokens=[SimpleNamespace(text=text, type=19)],
    ) for index, text in enumerate(lines)]
    root = Instruction(function.start, '')
    root.ast = root
    # A root's statement mapping is not the root; rendering .instr would truncate it.
    root.instr = statement
    root.rendered_lines = [
        SimpleNamespace(address=function.start, tokens=[SimpleNamespace(text='prologue()')]),
        *statement.ast.rendered_lines,
        SimpleNamespace(address=function.start + 0x10,
                        tokens=[SimpleNamespace(text='return result')]),
    ]
    function.hlil.root = root
    return preview_view


class ILInstruction:
    def __init__(self, address, text):
        self.address = address
        self.tokens = [SimpleNamespace(text=text, type=19)]

    @property
    def lines(self):
        return [SimpleNamespace(address=self.address, tokens=self.tokens)]

    @classmethod
    def create(cls, function, expr_index):
        return function.mapped_instructions[expr_index]


class _Language:
    def __init__(self, name, start):
        self.name = name
        self.start = start

    def get_linear_lines(self, statement, settings=None):
        return [SimpleNamespace(address=getattr(statement, 'address', self.start),
                                tokens=[SimpleNamespace(text=f'{self.name}: body', type=19)])]


class SnapshotFunction:
    """One function exposing every snapshot mode through the installed API shapes."""

    def __init__(self, start):
        self.start = start
        self.name = 'sample'
        self.low = ILInstruction(start + 4, 'llil_result = llil_worker()')
        self.low.ssa_form = ILInstruction(start + 4, 'llil_result #4 = llil_worker() #1')
        self.mlil_statement = ILInstruction(start + 4, 'result = worker(arg1)')
        self.mlil_statement.ssa_form = ILInstruction(start + 4, 'result #1 = worker() #1')
        self.mapped_statement = ILInstruction(start + 4, 'mapped result = worker(arg1)')
        self.mapped_statement.ssa_form = ILInstruction(start + 4, 'mapped result #1 = worker() #1')
        self.low.mlil = self.mlil_statement
        self.low.hlil = SimpleNamespace(instr=self.mlil_statement)
        body = [ILInstruction(start, 'first()'), ILInstruction(start + 0x10, 'return result')]
        self.llil = SimpleNamespace(
            instructions=body,
            ssa_form=SimpleNamespace(instructions=body),
            get_mapped_medium_level_il_expr_index=lambda _expr: 7,
        )
        self.mlil = SimpleNamespace(
            instructions=body,
            ssa_form=SimpleNamespace(instructions=body),
        )
        self.mapped_medium_level_il = SimpleNamespace(
            instructions=body,
            mapped_instructions={7: self.mapped_statement},
            ssa_form=SimpleNamespace(instructions=body),
        )
        self.hlil = SimpleNamespace(instructions=body, root=ILInstruction(start, 'root'))
        self.basic_blocks = [SimpleNamespace(start=start, disassembly_text=body)]
        self.languages = {}

    def get_llil_at(self, address):
        return self.low if address in (self.start + 4, self.start + 8) else None

    def language_representation(self, language):
        return self.languages.setdefault(language, _Language(language, self.start))


def file_view(path):
    data = FakeView()
    data.file.filename = str(path)
    data.file.display_name = path.name
    data.values[METADATA_KEY] = {'version': 2, 'notes': [
        {'id': name, 'text': f'# {name.upper()}\n\nSaved body'} for name in ('a', 'b', 'c')
    ]}
    return data


def _root(**extra):
    root = {'version': 2, 'notes': [
        {'id': 'a', 'text': '# A\n', 'edited': NOW - 1, 'folder': 'fs'},
        {'id': 'b', 'text': '# B\n', 'edited': NOW - 2, 'folder': 'iso'},
        {'id': 'c', 'text': '# C\n', 'edited': NOW - 3},
    ], 'folders': [
        {'id': 'fs', 'name': 'Filesystems'},
        {'id': 'iso', 'name': 'ISO 9660', 'parent': 'fs'},
        {'id': 'k', 'name': 'Kernel'},
    ]}
    root.update(extra)
    return root


@pytest.fixture
def link_view(tmp_path):
    return LinkView(tmp_path / 'analysis notes.bndb')


def table_widget(notepad, qapp, source=SOURCE, *, compact=False):
    widget = notepad(compact=compact)
    widget.replace_source(source)
    widget.resize(1000 if not compact else 320, 620)
    widget.show()
    qapp.processEvents()
    return widget, widget.session.tables.models[0], next(iter(widget.editor.table_cards.values()))
