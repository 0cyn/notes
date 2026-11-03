from __future__ import annotations

import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import lru_cache

from PySide6.QtCore import QObject, QTimer, Signal

from ..links import HLILLine, ILMode, il_snapshot_at, resolve_in_bv_link
from ..markdown import MarkdownParser, cell_display_source
from ..preparation import prepared_targets
from .common import AppExecutor, app_singleton, preparation_pool
from .embeds import CodePreviewLayout


@dataclass(frozen=True)
class DisplaySettingsSnapshot:
    key: tuple
    settings: object = field(compare=False, repr=False)


def snapshot_settings(settings):
    """Own an independent core copy, including options absent from Python's facade."""
    from binaryninja import DisassemblyOption, DisassemblySettings
    from binaryninja import _binaryninjacore as core

    copy = DisassemblySettings(core.BNDuplicateDisassemblySettings(settings.handle))
    key = (
        copy.width, copy.max_symbol_width,
        core.BNGetDisassemblyGutterWidth(copy.handle),
        int(core.BNGetDisassemblyAddressMode(copy.handle)),
        core.BNGetDisassemblyAddressBaseOffset(copy.handle),
        int(core.BNGetDisassemblyCallParameterHints(copy.handle)),
        int(core.BNGetDisassemblyBlockLabels(copy.handle)),
        tuple(int(option) for option in DisassemblyOption if copy.is_option_set(option)),
    )
    return DisplaySettingsSnapshot(key, copy)


class DisplaySettingsSource:
    def __init__(self, widget=None):
        self.widget = widget
        self._session_id = None
        self._last = None

    def __call__(self, data):
        session_id = data.file.session_id if data is not None else None
        if session_id != self._session_id:
            self._session_id, self._last = session_id, None
        ui = sys.modules.get('binaryninjaui')
        if data is None or getattr(ui, 'UIContext', None) is None:
            return None
        try:
            settings = self._find_settings(ui, session_id)
            if settings is not None:
                self._last = snapshot_settings(settings)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            # Keep only the snapshot, never a borrowed native wrapper.
            pass
        return self._last

    def _find_settings(self, ui, session_id):
        owning = (ui.UIContext.contextForWidget(self.widget) if self.widget is not None else None)
        contexts = []
        for context in (owning, ui.UIContext.activeContext(),
                        *getattr(ui.UIContext, 'allContexts', lambda: ())()):
            if context is not None and context not in contexts:
                contexts.append(context)
        for context in contexts:
            settings = self._find_context_settings(ui, context, session_id)
            if settings is not None:
                return settings
        return None

    def _find_context_settings(self, ui, context, session_id):
        origin = (ui.UIContext.currentViewFrameForWidget(self.widget)
                  if self.widget is not None else None)
        active = context.getCurrentViewFrame()
        tab = context.getTabForSessionId(session_id)
        peers = context.getAllViewFramesForTab(tab) if tab is not None else []
        frames = []
        for frame in (origin, active, *peers):
            if frame is not None and frame not in frames:
                data = frame.getCurrentBinaryView()
                if data is not None and data.file.session_id == session_id:
                    frames.append(frame)
        for frame in frames:
            if frame.getCurrentView().split(':', 1)[0] not in ('Linear', 'Graph'):
                continue
            view = frame.getCurrentViewInterface()
            settings = view.getDisassemblySettings() if view is not None else None
            if settings is not None:
                return settings
        # Hidden cached Linear/Graph views still carry the user's options.
        for frame in frames:
            for name in ('LinearView', 'DisassemblyView'):
                cls = getattr(ui, name, None)
                if cls is None:
                    continue
                for view in frame.findChildren(cls):
                    data = view.getData()
                    if data is not None and data.file.session_id == session_id:
                        settings = view.getDisassemblySettings()
                        if settings is not None:
                            return settings
        return None


def _lookup_pool():
    # Closed at quit, while Binary Ninja can still serve the reads.
    return app_singleton('_bnnotes_hlil_pool', lambda app: AppExecutor(app, 2, 'bnnotes-hlil'))


@lru_cache(maxsize=1)
def _parser():
    return MarkdownParser()


@lru_cache(maxsize=512)
def _cell_targets(source):
    if 'binaryninja:' not in source.lower():
        return ()
    targets = prepared_targets(source)
    if targets is not None:
        return targets
    return tuple(dict.fromkeys((link.url, link.expand_block, link.il_mode)
                               for link in _parser().parse(
        cell_display_source(source),
    ).links))


def _cancel(pending):
    for future in pending.values():
        cancel_preparation = getattr(future, 'cancel_preparation', None)
        if cancel_preparation is not None:
            cancel_preparation()
        future.cancel()
    pending.clear()


def _lookup(data, address, settings, expanded, mode, previous):
    snapshot = il_snapshot_at(data, address, settings, expand_block=expanded, il_mode=mode)
    if previous is not None and snapshot == previous:
        return previous
    # Finish the expensive comparison here, off the GUI thread.
    hash(snapshot)
    _ = snapshot.token_kinds, snapshot.display_text
    return snapshot


class Lookup(Enum):
    """A preview's state; a known result stays shown while it refreshes."""

    MISSING = auto()  # never looked up: a muted "Loading HLIL…" placeholder is shown
    FRESH = auto()    # current for this view and its display settings
    STALE = auto()    # still shown; due for (or awaiting) a refresh lookup


def _same_view(left, right):
    """Binary Ninja wrappers are re-created per call; compare native views."""
    return left is right or left is not None and right is not None and left == right


class HLILPreviewProvider(QObject):
    changed = Signal()
    CACHE_LIMIT = 256
    PENDING_LIMIT = 32
    REFRESH_SECONDS = 5.0

    def __init__(self, data=None, parent=None, *, settings_source=None):
        super().__init__(parent)
        self.data = data
        self._cache = OrderedDict()
        self._resolved = {}
        self._loading = {}
        self.prepared_layouts = {}
        self._layout_geometry = {}
        self._layout_placeholders = {}
        self.layout_revision = 0
        self._prepared_notifications = set()
        self.changed_urls = None
        self._pending = {}
        self._settings_source = (settings_source if settings_source is not None
                                 else DisplaySettingsSource(parent))
        self._render_settings = None
        self._settings_timer = QTimer(self)
        self._settings_timer.setInterval(1000)
        self._settings_timer.timeout.connect(self._refresh_settings)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._collect)
        pending = self._pending
        self.destroyed.connect(lambda *_args: _cancel(pending))
        prepared = self.prepared_layouts
        self.destroyed.connect(lambda *_args: _cancel(prepared))

    def set_data(self, data):
        if _same_view(data, self.data):
            self.data = data  # Host callbacks deliver a new wrapper of the same view.
            return
        self.data = data
        _cancel(self._pending)
        self._cache.clear()
        self._resolved.clear()
        self._loading.clear()
        _cancel(self.prepared_layouts)
        self._layout_geometry.clear()
        self._layout_placeholders.clear()
        self.layout_revision += 1
        self._prepared_notifications.clear()
        self.changed_urls = None
        self._timer.stop()
        self._settings_timer.stop()
        self._render_settings = None
        self.changed.emit()

    def _refresh_settings(self, *, notify=True):
        parent = self.parent()
        if (self.data is None or parent is not None and hasattr(parent, 'isVisible')
                and not parent.isVisible()):
            return
        snapshot = self._settings_source(self.data)
        if snapshot is None or (self._render_settings is not None
                                and snapshot.key == self._render_settings.key):
            return
        self._render_settings = snapshot
        # Results for the previous settings stay visible until their refreshes land.
        _cancel(self._pending)
        self._cache = OrderedDict((key, (float('-inf'), result))
                                  for key, (_time, result) in self._cache.items())
        self._timer.stop()
        if notify:
            self.changed_urls = None
            self.changed.emit()

    def request(self, url, *, load=True, expand_block=False, il_mode=ILMode.HLIL):
        now = time.monotonic()
        resolution = self._resolved.get(url)
        if resolution is None or load and now - resolution[0] > 1:
            resolution = now, resolve_in_bv_link(self.data, url)
            self._resolved[url] = resolution
        address = resolution[1]
        if address is None:
            return None
        if load and not self._settings_timer.isActive():
            self._refresh_settings(notify=False)
            self._settings_timer.start()
        key = address, expand_block, il_mode
        cached = self._cache.get(key)
        state = self._lookup_state(cached)
        if load and state is not Lookup.FRESH and key not in self._pending:
            pool = _lookup_pool()
            if not pool.closed and len(self._pending) < self.PENDING_LIMIT:
                settings = self._render_settings.settings if self._render_settings else None
                self._pending[key] = pool.executor.submit(
                    _lookup, self.data, address, settings, expand_block, il_mode,
                    cached[1] if cached is not None else None,
                )
                self._timer.start()
        if state is not Lookup.MISSING:
            self._cache.move_to_end(key)
            return cached[1]
        if key not in self._loading:
            self._loading[key] = HLILLine(address, f'Loading {il_mode.label}…',
                                          expand_block=expand_block, il_mode=il_mode)
        return self._loading[key]

    def snapshots(self, keys):
        """Fresh lookups of (address, expand_block, il_mode) keys, as futures: for export."""
        pool = _lookup_pool()
        if self.data is None or pool.closed:
            return {}
        self._refresh_settings(notify=False)
        settings = self._render_settings.settings if self._render_settings else None
        return {key: pool.executor.submit(il_snapshot_at, self.data, key[0], settings,
                                          expand_block=key[1], il_mode=key[2])
                for key in keys}

    def _lookup_state(self, cached):
        if cached is None:
            return Lookup.MISSING
        if time.monotonic() - cached[0] < self.REFRESH_SECONDS:
            return Lookup.FRESH
        return Lookup.STALE

    def for_cell(self, source, *, load=False):
        result = []
        for url, expand_block, il_mode in _cell_targets(source):
            preview = self.request(url, load=load, expand_block=expand_block, il_mode=il_mode)
            if preview is not None:
                result.append((url, preview))
        return tuple(result)

    def prepare_code(self, preview, font, width, theme, *, show_addresses, label, label_font):
        """Prepare large cell objects without blocking a table reveal/scroll."""
        if len(preview.display_lines) <= 128:
            return CodePreviewLayout(preview, font, width, theme, show_addresses=show_addresses,
                                     label=label, label_font=label_font)
        key = preview, font.key(), width, theme.key, show_addresses, label, label_font.key()
        future = self.prepared_layouts.get(key)
        if future is None or not future.done() and key not in self._prepared_notifications:
            self._prepared_notifications.add(key)
            future = preparation_pool().submit(
                self, key, CodePreviewLayout, preview, font, width, theme,
                show_addresses=show_addresses, label=label, label_font=label_font,
                cache=self.prepared_layouts, cache_key=key,
            )
        if future is not None and future.done() and not future.cancelled():
            try:
                return future.result()
            except Exception:
                return CodePreviewLayout(
                    HLILLine(preview.address, f'{preview.il_mode.label} unavailable',
                             il_mode=preview.il_mode),
                    font, width, theme, label=label, label_font=label_font,
                    show_addresses=show_addresses,
                )
        geometry = self._layout_geometry.get((*key[:3], *key[4:]))
        if geometry is not None:
            placeholder = self._layout_placeholders.get(key)
            if placeholder is None:
                status = HLILLine(preview.address, f'Preparing {preview.il_mode.label}…',
                                  preview.function_name, function_start=preview.function_start,
                                  function_signature=preview.function_signature,
                                  il_mode=preview.il_mode)
                placeholder = CodePreviewLayout(status, font, width, theme,
                                                 show_addresses=show_addresses, label=label,
                                                 label_font=label_font)
                placeholder.width, placeholder.height = geometry.width, geometry.height
                placeholder.pending = True
                self._layout_placeholders[key] = placeholder
            return placeholder
        return None

    def preparation_finished(self, key, future):
        if self.prepared_layouts.get(key) is not future:
            return
        if not future.cancelled() and future.exception() is None:
            self._layout_geometry[(*key[:3], *key[4:])] = future.result()
        self.layout_revision += 1
        urls = frozenset(url for url, (_time, address) in self._resolved.items()
                         if address == key[0].address)
        self.changed_urls = urls or None
        self.changed.emit()

    def _collect(self):
        changed = set()
        for key, future in list(self._pending.items()):
            if not future.done():
                continue
            del self._pending[key]
            if future.cancelled():
                continue
            try:
                result = future.result()
            except Exception:
                result = HLILLine(key[0], f'{key[2].label} unavailable', expand_block=key[1],
                                  il_mode=key[2])
            old = self._cache.get(key)
            if old is None or old[1] is not result:
                changed.add(key[0])
            self._cache[key] = time.monotonic(), result
            self._cache.move_to_end(key)
        if not self._pending:
            self._timer.stop()
        if changed:
            self.changed_urls = frozenset(url for url, (_time, address) in self._resolved.items()
                                          if address in changed)
            self.changed.emit()
