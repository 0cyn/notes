"""In-app inspection session, dispatched by the local agent bridge."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import runpy
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


class MemoryView:
    def __init__(self, binary=None):
        self.binary = binary
        self.values = {}
        self.file = SimpleNamespace(session_id=-id(self), display_name='Agent preview',
                                    filename='', project_file=None, raw=self)

    def __getattr__(self, name):
        if self.binary is None:
            raise AttributeError(name)
        return getattr(self.binary, name)

    def query_metadata(self, key):
        return self.values[key]

    def store_metadata(self, key, value):
        self.values[key] = value


class Session:
    def __init__(self, request):
        self.output = Path(request['output'])
        self.options = request['options']
        self.prepared = None
        self.images = []
        self._package = None

    def settle(self, milliseconds=80):
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def wait_for(self, predicate, *, timeout=5, description='UI state'):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.settle(20)
        raise TimeoutError(f'Timed out waiting for {description}.')

    def capture(self, widget, name):
        # Callers reacquire borrowed native widgets after event-loop turns.
        path = self.output / f'{Path(name).name}.png'
        image = widget.grab()
        if image.isNull() or not image.save(str(path)):
            raise RuntimeError(f'Could not capture {name}.')
        self.images.append({'path': str(path), 'width': image.width(), 'height': image.height(),
                            'device_pixel_ratio': image.devicePixelRatio()})
        return path

    def describe(self, widget, depth=2):
        from PySide6.QtWidgets import QWidget
        from shiboken6 import isValid

        result = {'class': widget.metaObject().className(), 'name': widget.objectName(),
                  'geometry': list(widget.geometry().getRect()), 'visible': widget.isVisible()}
        if depth:
            result['children'] = [self.describe(child, depth - 1) for child in widget.children()
                                  if isinstance(child, QWidget) and isValid(child)]
        return result

    def module(self, name):
        if self._package is None:
            # Fresh fixture classes, not a hot reload of the user's live Qt callbacks.
            root = Path(__file__).resolve().parents[2]
            self._package = f'_bnnotes_agent_{uuid4().hex}'
            spec = importlib.util.spec_from_file_location(self._package, root / 'src/__init__.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[self._package] = module
            spec.loader.exec_module(module)
            importlib.import_module(f'{self._package}.markdown').activate_runtime_packages()
            preferences = importlib.import_module(f'{self._package}.ui.preferences')
            settings = importlib.import_module(f'{self._package}.settings')
            importlib.import_module(f'{self._package}.ui.session').editor_preferences = (
                lambda data, parent: preferences.EditorPreferences(
                    settings.MemorySettings(), parent, data=data,
                )
            )
        return importlib.import_module(f'{self._package}.{name}')

    @contextmanager
    def notebook(self, source='', *, binary=None, width=900, height=650, onboarding=False):
        from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt

        store = self.module('ui.common').PersistentViewSettings(
            QSettings(str(self.output / f'ui-state-{uuid4().hex}.ini'), QSettings.IniFormat),
        )
        if not onboarding:  # Scenes start past the welcome tour unless they show it.
            store.set_app_value('onboarding/done', True)
        widget = self.module('ui.notebook').NotesWidget(MemoryView(binary), state_store=store)
        store.setParent(widget)
        # Qt still lays out and paints these widgets, but Cocoa never maps a
        # native window. Preview generation must not interrupt the user's work.
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        widget._link_previews._settings_source = lambda _data: None
        try:
            widget.setWindowTitle('Notes agent preview')
            widget.resize(width, height)
            if source:
                widget.current_note.replace_source(source)
            widget.show()
            self.settle()
            yield widget
        finally:
            widget.close()
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def execute(request_path):
    import binaryninja as bn
    from PySide6.QtWidgets import QApplication

    request = json.loads(Path(request_path).read_text())
    receipt = Path(request['receipt'])
    result = {'run_id': request['run_id'], 'pid': os.getpid(), 'status': 'running', 'images': []}

    def publish():
        temporary = receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, indent=2))
        temporary.replace(receipt)

    publish()
    session = Session(request)
    namespace = {}
    try:
        if not bn.core_ui_enabled() or QApplication.instance() is None:
            raise RuntimeError('Agent UI scripts require the initialized Binary Ninja UI.')
        namespace = runpy.run_path(request['script'])
        if 'prepare' in namespace:
            session.prepared = namespace['prepare'](session.options)

        def invoke():
            try:
                result['value'] = namespace['run'](session)
            except Exception:
                result.update(status='failed', error=traceback.format_exc())

        bn.execute_on_main_thread_and_wait(invoke)
        if result['status'] != 'failed':
            result['status'] = 'complete'
    except Exception:
        result.update(status='failed', error=traceback.format_exc())
    finally:
        try:
            if 'cleanup' in namespace:
                namespace['cleanup'](session.prepared)
        except Exception:
            result.update(status='failed', error=traceback.format_exc())
        result['images'] = session.images
        # A non-serializable script result is a failed run, not an absent/stale receipt.
        try:
            publish()
        except (TypeError, ValueError):
            result.pop('value', None)
            result.update(status='failed', error=traceback.format_exc())
            publish()
