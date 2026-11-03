from __future__ import annotations

import builtins
import io
import os
import pickle
import struct
import subprocess
import sys
from bisect import bisect_left, bisect_right
from concurrent.futures import Future, ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from time import perf_counter
from weakref import WeakKeyDictionary, ref

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from shiboken6 import isValid

from .. import links, markdown, preparation
from ..markdown import MarkdownAnalysis, MarkdownParser, utf16_length
from ..preparation import LazyMarkdownParser, PreparedMarkdown, install_templates, read_exact
from .common import IdleTask, app_singleton, mix, single_shot
from .layout import _BlockSpacingState


def _python_command():
    """The Python to run the parser helper with, and its environment (None: this process's).

    In Binary Ninja, the interpreter it installs plugin dependencies with, so this plugin's
    packages are there: its Python Path Override setting, else the one matching its Python
    Interpreter setting or bundled Python. Binary Ninja keeps that lookup private, so None if
    it's missing or fails. Outside Binary Ninja, this interpreter.
    """
    if 'binaryninja' not in sys.modules:
        return (sys.executable, None) if sys.executable else None
    try:
        from binaryninja.scriptingprovider import PythonScriptingProvider

        provider = PythonScriptingProvider()  # Not registered: used only for its lookup.
        command = provider._python_bin
        return (str(command), provider._get_python_environment()) if command else None
    except Exception:
        return None


class _ResultReader(pickle.Unpickler):
    """Unpickle only our own result types, in the caller's package namespace."""

    TYPES = {'src.markdown': markdown, 'src.links': links, 'src.preparation': preparation}

    def find_class(self, module, name):
        if module in self.TYPES and isinstance(getattr(self.TYPES[module], name, None), type):
            return getattr(self.TYPES[module], name)
        if module == 'builtins' and name in ('frozenset', 'set'):
            return getattr(builtins, name)
        raise pickle.UnpicklingError(f'Unexpected parser result type: {module}.{name}')


class ParserProcess(QObject):
    """Prepares notes in a helper process, so parsing a large note doesn't hold the GIL the UI
    needs. If the helper can't start, fails, or lacks this plugin's packages, notes are
    prepared in this process instead, on the same background thread."""

    completed = Signal(object, object, object)

    def __init__(self, app):
        super().__init__(app)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bnnotes-parser-io')
        self.process = None
        self.helper = True  # Whether to use the helper process.
        self.closed = False
        self.requests = WeakKeyDictionary()
        # The worker imports this checkout's `src` as a top-level package.
        self.paths = (str(Path(__file__).resolve().parents[2]), *sys.path)
        self.completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        app.aboutToQuit.connect(self.close)

    def submit(self, owner, source, *, inline=False, normalize=False, cells=False):
        if self.closed:
            return None
        previous = self.requests.get(owner)
        if previous is not None:
            previous.cancel()
        future = self.executor.submit(self._exchange, source, inline=inline, normalize=normalize,
                                      cells=cells)
        self.requests[owner] = future
        pointer = ref(owner)
        future.add_done_callback(lambda result: self._notify(pointer, source, result))
        return future

    def _notify(self, pointer, source, future):
        if not self.closed:
            self.completed.emit(pointer, source, future)

    def _exchange(self, source, **request):
        if self.closed:
            raise RuntimeError('Markdown parser closed')
        helped = self._ask_helper(source, request) if self.helper else None
        if helped is not None and not helped.warning:
            return helped
        result = preparation.prepare_markdown(source, **request)
        if helped is not None and not result.warning:
            self._stop_helper()  # It can't do what this process can: its packages are missing.
        return result

    def _ask_helper(self, source, request):
        """The helper's result, or None, after which it isn't asked again."""
        try:
            if self.process is None:
                self.process = self._launch()
            payload = pickle.dumps({'source': source, **request}, protocol=4)
            self.process.stdin.write(struct.pack('!I', len(payload)) + payload)
            self.process.stdin.flush()
            size = struct.unpack('!I', read_exact(self.process.stdout, 4))[0]
            return _ResultReader(io.BytesIO(read_exact(self.process.stdout, size))).load()
        except Exception:
            self._stop_helper()
            return None

    def _launch(self):
        found = _python_command()
        if found is None:
            raise RuntimeError('No Python interpreter for the Markdown parser')
        command, environment = found
        environment = dict(os.environ if environment is None else environment)
        environment['PYTHONPATH'] = os.pathsep.join(path for path in self.paths if path)
        return subprocess.Popen(
            [command, '-c', 'from src.preparation import serve; serve()'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0),
        )

    def _stop_helper(self):
        self.helper = False
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()

    def _deliver(self, pointer, source, future):
        owner = pointer()
        if not self.closed and owner is not None and isValid(owner) and not future.cancelled():
            owner.preparation_finished(source, future)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for future in self.requests.values():
            future.cancel()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.executor.submit(self.process.wait)
        self.executor.shutdown(wait=False)


def parser_process():
    return app_singleton('_bnnotes_parser_process', ParserProcess)


# Fenced code's syntax colors, as (role, light theme, dark theme).
SYNTAX_COLORS = (
    ("syntax_keyword", "#8250df", "#c4a5f5"),
    ("syntax_string", "#116329", "#a8ce93"),
    ("syntax_number", "#953800", "#edb887"),
    ("syntax_function", "#0550ae", "#8cc2f0"),
    ("syntax_type", "#0a6768", "#78d1ce"),
    ("syntax_operator", "#a626a4", "#d799d3"),
)


def code_block_colors(palette: QPalette) -> tuple[QColor, QColor]:
    base = palette.color(QPalette.ColorRole.Base)
    text = palette.color(QPalette.ColorRole.Text)
    return mix(base, text, 0.055), mix(base, text, 0.25)


def _rebased_formats(ranges, old, new):
    """Map a paragraph's formats from `old` to `new` text; insertions take the previous format."""
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < limit - prefix
           and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]):
        suffix += 1
    # Format ranges count UTF-16 code units; measure the edit the same way.
    prefix = utf16_length(old[:prefix])
    removed = utf16_length(old) - prefix - utf16_length(old[len(old) - suffix:])
    added = utf16_length(new) - prefix - utf16_length(new[len(new) - suffix:])
    old_end, delta = prefix + removed, added - removed
    for item in ranges:
        start, end = item.start, item.start + item.length
        if end < prefix:                      # before the edit
            mapped = start, end
        elif start >= old_end:                # after the edit
            mapped = start + delta, end + delta
        elif start < prefix:                  # reaches the edit: covers inserted text
            mapped = start, max(end + delta, prefix + added)
        elif end > old_end:                   # starts in replaced text: keep the tail
            mapped = prefix + added, end + delta
        else:                                 # replaced entirely
            continue
        if mapped[1] > mapped[0]:
            yield mapped[0], mapped[1] - mapped[0], item.format


# A deferred note parses on the UI thread at its first edit when small enough.
_SYNC_PARSE_LIMIT = 4096


class ParseMode(Enum):
    """DEFERRED parses in the subprocess; SYNC on the UI thread. Only DEFERRED → SYNC."""
    DEFERRED = 'deferred'
    SYNC = 'sync'


class MarkdownHighlighter(QSyntaxHighlighter):
    """Styles a document from its analysis.

    `_revision` counts text changes; positions from the analysis are only valid when
    `_analyzed_revision` matches (`analysis_is_current`).
    """
    analysisChanged = Signal(object)
    formatsChanged = Signal()
    previewGeometryChanged = Signal()
    initialTextPrepared = Signal(str, str)

    def __init__(self, document, palette, *, inline=False, deferred=False, normalize_initial=False,
                 prepared=None):
        super().__init__(None)
        self.setParent(document)
        self.analysis = MarkdownAnalysis()
        self.warning = ""
        self._source: str | None = None  # the text `analysis` describes
        self._revision = 0
        self._analyzed_revision = -1
        self._request: tuple[int, str] | None = None
        self._mode = ParseMode.DEFERRED if deferred else ParseMode.SYNC
        # Before setDocument: the base class's re-highlight must see the new revision.
        document.documentLayout()
        document.contentsChange.connect(self._text_changed)
        self.setDocument(document)
        self._palette = QPalette(palette)
        self._formats = self._make_formats()
        self._link_previews = ()
        self._preview_starts = ()
        self._format_revision = 0
        self._inline = inline
        self._normalize_initial = normalize_initial  # only the untouched text (revision 0)
        self._prepared_initial = prepared             # analysis of the untouched text
        self._parser = None
        if not deferred:
            self._make_parser()
        # Large deferred notes restyle in slices: the next block to check, or None when done.
        self._highlight_next = None
        # Once any view reports visibility, sliced restyling only runs while one is visible.
        self._active_views = set()
        self._view_tracking = False
        self._format_timer = IdleTask(self)
        self._format_timer.timeout.connect(self._highlight_chunk)
        # Coalesce bursts without waiting for typing to stop.
        self._timer = single_shot(self, self.refresh)
        if deferred:
            self._timer.start()
        else:
            self.refresh()

    def _make_parser(self):
        try:
            self._parser = LazyMarkdownParser(MarkdownParser)
        except (ImportError, RuntimeError, TypeError) as error:
            self.warning = f"Plain text mode: install requirements.txt in Binary Ninja. ({error})"

    def _text_changed(self, _position, _removed, _added):
        self._revision += 1
        if (self._mode is ParseMode.DEFERRED
                and self.document().characterCount() <= _SYNC_PARSE_LIMIT):
            self._mode = ParseMode.SYNC
            self._request = None
            self._make_parser()
        if not self._timer.isActive():
            self._timer.start()

    @property
    def analysis_is_current(self) -> bool:
        """Whether analysis positions describe the document's current text."""
        return self._analyzed_revision == self._revision

    @property
    def code_font(self) -> QFont:
        return self._formats["code_block"].font().resolve(self.document().defaultFont())

    def refresh(self):
        """Bring the analysis up to date, or request it from the parser subprocess."""
        self._timer.stop()
        if self.analysis_is_current:
            return
        if self._mode is ParseMode.DEFERRED:
            self._request_parse()
            return
        source = self.document().toPlainText()
        if source != self._source:  # undo can restore the analyzed text
            if not source or self._parser is None:
                # Without a parser the note stays plain text; keep its warning.
                analysis, warning = MarkdownAnalysis(), '' if not source else self.warning
            else:
                try:
                    parse = self._parser.parse_inline if self._inline else self._parser.parse
                    analysis, warning = parse(source), ''
                except Exception as error:
                    # A parser error must never interrupt editing or persistence.
                    analysis = MarkdownAnalysis()
                    warning = f"Markdown formatting unavailable: {error}"
            self.analysis, self.warning = analysis, warning
        self._adopt(source)

    def _request_parse(self):
        if self._request is not None and self._request[0] == self._revision:
            return  # already in flight
        source = self.document().toPlainText()
        self._request = self._revision, source
        untouched = self._revision == 0
        if untouched and self._prepared_initial is not None:
            future = Future()
            future.set_result(PreparedMarkdown(source, source, self._prepared_initial))
            self._prepared_initial = None
            self.preparation_finished(source, future)
            return
        parser_process().submit(self, source, inline=self._inline, cells=not self._inline,
                                normalize=self._normalize_initial and untouched)

    def preparation_finished(self, source, future):
        if self._mode is not ParseMode.DEFERRED or self._request != (self._revision, source):
            return  # superseded: by an edit (its parse is queued) or by SYNC parsing
        self._request = None
        result = future.result()
        install_templates(result.cells)
        if result.text != source:
            # The session decides whether to adopt the parser's normalized text.
            self.initialTextPrepared.emit(source, result.text)
            if self.document().toPlainText() != result.text:
                self._normalize_initial = False
                self._timer.start(0)
                return
        self.analysis, self.warning = result.analysis, result.warning
        if self._parser is None and not self.warning:
            self._make_parser()
        self._adopt(result.text)

    def _adopt(self, source):
        """Mark the analysis as describing `source`, the document's current text."""
        self._source = source
        self._analyzed_revision = self._revision
        self._rehighlight_changed()
        self.analysisChanged.emit(self.analysis)

    def _block_signature(self, block):
        start, end = block.position(), block.position() + block.length() - 1
        spans = tuple(self.analysis.spans_for_range(start, end))
        previews = tuple((left - start, right - start, width, font, height, baseline)
                         for left, right, width, font, height, baseline in self._link_previews[
                             max(0, bisect_right(self._preview_starts, start) - 1):
                             bisect_left(self._preview_starts, end)] if right > start)
        return self._format_revision, spans, previews

    def _rehighlight_changed(self):
        # rehighlight() clears every layout; preserve unaffected paragraphs.
        if self._mode is ParseMode.DEFERRED and (self.document().blockCount() > 64
                               or self._view_tracking and not self._active_views):
            self._highlight_next = 0
            if not self._view_tracking or self._active_views:
                self._highlight_chunk()
            return
        self._highlight_range()

    def _highlight_chunk(self):
        if self._mode is ParseMode.DEFERRED and self._view_tracking and not self._active_views:
            return
        if self._highlight_next is None or not self.analysis_is_current:
            self._highlight_next = None
            return
        self._highlight_range(deadline=perf_counter() + .003)
        if self._highlight_next is not None:
            self._format_timer.start(1)

    def set_view_active(self, owner, active):
        self._view_tracking = True
        if active:
            self._active_views.add(owner)
            if self._highlight_next is not None:
                self._format_timer.start()
        else:
            self._active_views.discard(owner)
            if self._mode is ParseMode.DEFERRED and not self._active_views:
                self._format_timer.stop()

    def _highlight_range(self, deadline=None):
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        try:
            block = self.document().findBlockByNumber(self._highlight_next or 0)
            while block.isValid():
                state = block.userData()
                if getattr(state, 'highlight_signature', None) != self._block_signature(block):
                    self.rehighlightBlock(block)
                block = block.next()
                if deadline is not None and perf_counter() >= deadline:
                    break
            self._highlight_next = block.blockNumber() if block.isValid() else None
        finally:
            cursor.endEditBlock()

    def set_palette(self, palette):
        self._palette = QPalette(palette)
        layout = self.document().documentLayout()
        if hasattr(layout, 'foreground'):
            layout.foreground = palette.color(QPalette.ColorRole.Text)
        self.refresh_formats()

    def refresh_formats(self):
        self._formats = self._make_formats()
        self._format_revision += 1
        self.rehighlight()
        self.formatsChanged.emit()

    def set_link_previews(self, previews):
        previews = tuple(previews)
        if previews != self._link_previews:
            old = {item[0]: item for item in self._link_previews}
            new = {item[0]: item for item in previews}
            self._link_previews = previews
            self._preview_starts = tuple(item[0] for item in previews)
            positions = {pos for pos in old.keys() | new.keys() if old.get(pos) != new.get(pos)}
            blocks = {self.document().findBlock(pos).blockNumber() for pos in positions}
            for number in blocks:
                self.rehighlightBlock(self.document().findBlockByNumber(number))
            layout = self.document().documentLayout()
            if hasattr(layout, 'set_inline_previews'):
                layout.set_inline_previews(tuple((right - 1, height, baseline)
                                                 for _left, right, _width, _font, height, baseline
                                                 in previews))
            self.previewGeometryChanged.emit()

    def text_format_at(self, position):
        result = QTextCharFormat()
        for span in self.analysis.spans_for_range(position, position + 1):
            if span.role not in ('marker', 'destination', 'fence_marker', 'quote_marker'):
                result.merge(self._formats.get(span.role, QTextCharFormat()))
        return result

    def _make_formats(self) -> dict[str, QTextCharFormat]:
        palette = self._palette
        base_size = self.document().defaultFont().pointSizeF()
        dark = palette.color(QPalette.ColorRole.Base).lightness() < 128
        system_mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        mono = ([system_mono] if QFontDatabase.isFixedPitch(system_mono)
                else ["Menlo", "Consolas", "DejaVu Sans Mono", "monospace"])
        code_background, _ = code_block_colors(palette)
        result: dict[str, QTextCharFormat] = {}

        def style(name, color=None, *, bold=False, italic=False, strike=False,
                  code=False, size=None, underline=False):
            fmt = QTextCharFormat()
            if color is not None:
                fmt.setForeground(color)
            if bold:
                fmt.setFontWeight(QFont.Weight.Bold)
            if italic:
                fmt.setFontItalic(True)
            if strike:
                fmt.setFontStrikeOut(True)
            if code:
                fmt.setFontFamilies(mono)
                fmt.setFontFixedPitch(True)
                fmt.setFontStyleHint(QFont.StyleHint.Monospace)
                fmt.setFontPointSize(base_size)
            if size:
                fmt.setFontPointSize(size)
            if underline:
                fmt.setFontUnderline(True)
            result[name] = fmt
            return fmt

        for level, scale in enumerate((1.8, 1.5, 1.3, 1.15, 1.05, 1.0), 1):
            style(f"heading{level}", bold=True, size=base_size * scale)
        style("strong", bold=True)
        style("emphasis", italic=True)
        style("strike", strike=True)
        highlight = QColor("#d6ad39" if dark else "#ffe58a")
        style("highlight").setBackground(mix(palette.color(QPalette.ColorRole.Base),
                                                highlight, 0.38 if dark else 0.7))
        style("math", palette.color(QPalette.ColorRole.Text), code=True)
        style("code", code=True).setBackground(code_background)
        code = style("code_block", palette.color(QPalette.ColorRole.Text), code=True)
        code.setFontItalic(False)
        code.setFontWeight(QFont.Weight.Normal)
        code.setFontStrikeOut(False)
        code.setFontUnderline(False)
        style("table").setFontFamilies(mono)
        muted = mix(
            palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text), 0.55
        )
        style("quote")
        style("comment", muted, italic=True)
        marker = style("marker", muted, size=base_size * 0.9)
        marker.setFontWeight(QFont.Weight.Normal)
        marker.setFontItalic(False)
        marker.setFontStrikeOut(False)
        result["quote_marker"] = QTextCharFormat(marker)
        result["quote_marker"].setFontPointSize(base_size)
        style("fence_marker", muted, code=True)
        style("link", palette.color(QPalette.ColorRole.Link), underline=True)
        style("destination", muted)
        style("checked", palette.color(QPalette.ColorRole.Link), bold=True)
        style("escape", palette.color(QPalette.ColorRole.Link))
        for role, light, night in SYNTAX_COLORS:
            style(role, QColor(night if dark else light))
        style("syntax_comment", muted, italic=True)
        return result

    def highlightBlock(self, text):
        block = self.currentBlock()
        state = block.userData()
        if not isinstance(state, _BlockSpacingState):
            state = _BlockSpacingState()
            block.setUserData(state)
        if not self.analysis_is_current:
            # Stale analysis: keep the edited paragraph's styles, shifted, until the new parse.
            state.highlight_signature = None
            previous = getattr(state, 'styled_text', None)
            if previous is not None:
                for start, length, fmt in _rebased_formats(block.layout().formats(),
                                                           previous, text):
                    self.setFormat(start, length, fmt)
                state.styled_text = text
            return
        signature = self._block_signature(block)
        state.highlight_signature = signature
        state.styled_text = text
        # Merge overlapping formats, so nested styles keep each part.
        events: dict[int, list[tuple[int, bool]]] = {}
        start = self.currentBlock().position()
        spans = signature[1]
        for index, span in enumerate(spans):
            events.setdefault(span.start, []).append((index, True))
            events.setdefault(span.start + span.length, []).append((index, False))
        active: set[int] = set()
        previous = 0
        for position in sorted(events):
            if active and position > previous:
                fmt = QTextCharFormat()
                for index in sorted(active):
                    fmt.merge(self._formats[spans[index].role])
                self.setFormat(previous, position - previous, fmt)
            for index, entering in events[position]:
                if entering:
                    active.add(index)
                else:
                    active.discard(index)
            previous = position
        # Layout formats only: source text and undo history are untouched.
        end = start + utf16_length(text)
        for left, right, width, slot_font, _height, _baseline in self._link_previews:
            if right <= start or left >= end:
                continue
            hidden = QTextCharFormat()
            font = QFont(self.document().defaultFont())
            font.setPixelSize(1)
            font.setStretch(1)
            hidden.setFont(font)
            hidden.setForeground(QColor(Qt.GlobalColor.transparent))
            self.setFormat(max(left, start) - start, min(right, end) - max(left, start), hidden)
            if start <= right - 1 < end:
                slot = QTextCharFormat(hidden)
                font = QFont(slot_font)
                font.setUnderline(False)
                font.setStrikeOut(False)
                font.setStretch(1)
                font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, width)
                slot.setFont(font)
                self.setFormat(right - 1 - start, 1, slot)
