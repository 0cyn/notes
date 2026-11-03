"""Measure first content paint, event-loop stalls and cold tab selection separately."""

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from src.ui.notebook import NotesWidget
from tools.benchmark_rendering import MemoryView, synthetic_hlil  # noqa: E402


def run(notes, paragraphs, seconds, switches, mode, capture=None):
    app = QApplication.instance() or QApplication([])
    # Binary Ninja already has a running Qt UI. Exclude one-time platform font
    # alias discovery, but do not warm Notes, parsers, documents or note caches.
    QLabel('Host application').sizeHint()
    data = MemoryView(notes, paragraphs, mode)
    preview = synthetic_hlil() if mode != 'prose' else None
    gaps, selections, first = [], [], []
    started = last = perf_counter()
    pulse = QTimer()
    pulse.setInterval(1)

    def heartbeat():
        nonlocal last
        now = perf_counter()
        gaps.append((now - last) * 1000)
        last = now

    pulse.timeout.connect(heartbeat)
    pulse.start()
    widget = NotesWidget(data)
    if preview is not None:
        widget._link_previews.request = lambda *_args, **_kwargs: preview
    constructed = perf_counter()
    widget.resize(1000, 700)
    widget.show()
    editor = widget.editor

    class FirstPaint(QObject):
        def eventFilter(self, _watched, event):
            if event.type() == QEvent.Type.Paint and not first:
                first.append(perf_counter())
                if capture:
                    QTimer.singleShot(0, capture_first)
            return False

    def capture_first():
        path = Path(capture)
        path.parent.mkdir(parents=True, exist_ok=True)
        widget.grab().save(str(path.with_stem(path.stem + '-first')))

    observer = FirstPaint(editor)
    editor.viewport().installEventFilter(observer)
    initial_ready = perf_counter()
    loop = QEventLoop()
    identities = list(widget.notebook.notes)[1:switches + 1]

    def select_next():
        if not identities:
            return
        before = perf_counter()
        widget.open_note(identities.pop(0))
        selections.append((perf_counter() - before) * 1000)
        QTimer.singleShot(30, select_next)

    QTimer.singleShot(250, select_next)
    QTimer.singleShot(round(seconds * 1000), loop.quit)
    loop.exec()
    pulse.stop()

    def summary(values):
        return {'median_ms': statistics.median(values),
                'p95_ms': sorted(values)[min(len(values) - 1, int(.95 * len(values)))],
                'max_ms': max(values)} if values else None

    result = {
        'notes': notes, 'paragraph_groups_per_note': paragraphs, 'mode': mode,
        'constructor_ms': (constructed - started) * 1000,
        'ready_to_type_ms': (initial_ready - started) * 1000,
        'first_content_paint_ms': (first[0] - started) * 1000 if first else None,
        'event_loop_gaps': summary(gaps), 'cold_tab_selection': summary(selections),
        'materialized_documents': sum(note.materialized for note in widget.notebook.notes),
        'restored_headers': len(widget._pages) - len(widget.tab_restore.pending),
    }
    if capture:
        widget.grab().save(str(Path(capture)))
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--notes', type=int, default=100)
    parser.add_argument('--paragraphs', type=int, default=30)
    parser.add_argument('--seconds', type=float, default=2)
    parser.add_argument('--switches', type=int, default=8)
    parser.add_argument('--mode', choices=('prose', 'hlil', 'tables'), default='prose')
    parser.add_argument('--capture', help='Optional first/final offscreen inspection images')
    args = parser.parse_args()
    result = run(args.notes, args.paragraphs, args.seconds, args.switches, args.mode, args.capture)
    QApplication.instance().aboutToQuit.emit()
    print(json.dumps(result, indent=2))
