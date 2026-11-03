"""Standalone retained-rendering benchmark; no host application or test fixtures."""

import argparse
import cProfile
import json
import os
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.storage import METADATA_KEY
from src.ui.notebook import NotesWidget


class MemoryView:
    def __init__(self, count, paragraphs, mode):
        self.file = SimpleNamespace(session_id=id(self), display_name='Benchmark', raw=self)
        sources = {
            'prose': ('Paragraph with **bold text**, *emphasis* and a longer sentence.\n'
                      '> Quoted context with `code`\n\n'
                      '```c\nint value = compute(argument);\n```\n\n'),
            'hlil': '[!Function label](binaryninja://?expr=0x1000)\n\nContext\n\n',
            'tables': ('| Context | Code |\n| --- | --- |\n'
                       '| **Review** | [!Function label](binaryninja://?expr=0x1000) |\n\n'),
        }
        self.values = {METADATA_KEY: {'version': 2, 'notes': [
            {'id': str(index), 'text': f'# Note {index}\n\n' + sources[mode] * paragraphs}
            for index in range(count)
        ]}}

    def query_metadata(self, key):
        return self.values[key]

    def store_metadata(self, key, value):
        self.values[key] = value


def synthetic_hlil():
    from src.links import HLILLine, HLILSourceLine, HLILToken

    return HLILLine(0x1000, 'function', 'function', available=True, function_start=0x1000,
                    lines=tuple(HLILSourceLine(0x1000 + index * 4, (
                        HLILToken('value', 66), HLILToken(' = '),
                        HLILToken(f'process_{index}', 64), HLILToken('(argument);'),
                    )) for index in range(350)), expand_block=True)


def run(notes, paragraphs, frames, profile=None, mode='prose', capture=None):
    app = QApplication.instance() or QApplication([])
    started = time.perf_counter()
    view = MemoryView(notes, paragraphs, mode)
    widget = NotesWidget(view)
    if mode != 'prose':
        snapshot = synthetic_hlil()
        widget._link_previews.request = lambda *_args, **_kwargs: snapshot
    widget.resize(1000, 700)
    widget.show()
    for note in widget.notebook.notes:
        widget.open_note(note)
        app.processEvents()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        app.processEvents()
        if (widget.current_note.session.highlighter.analysis_is_current
                and not widget.current_note.session.layout._pending_blocks
                and not widget.current_note.session.highlighter._highlight_next
                and not widget.editor._links.controller._pending
                and all(future.done()
                        for future in widget._link_previews.prepared_layouts.values())):
            break
        time.sleep(.001)
    for _ in range(5):
        app.processEvents()
    opened = time.perf_counter() - started
    editor = widget.editor
    bar = editor.verticalScrollBar()
    samples = []
    profiler = cProfile.Profile() if profile else None
    if profiler:
        profiler.enable()
    for index in range(frames):
        started = time.perf_counter()
        bar.setValue((index * 37) % max(1, bar.maximum()))
        app.processEvents()
        editor.viewport().repaint()
        samples.append((time.perf_counter() - started) * 1000)
    if profiler:
        profiler.disable()
        profiler.dump_stats(profile)
    if capture:
        path = Path(capture)
        path.parent.mkdir(parents=True, exist_ok=True)
        widget.grab().save(str(path))
    result = {'mode': mode, 'notes': notes, 'paragraph_groups': paragraphs, 'frames': frames,
              'visit_all_tabs_seconds': round(opened, 3),
              'frame_ms_median': statistics.median(samples),
              'frame_ms_p95': sorted(samples)[int(len(samples) * .95)],
              'frame_ms_max': max(samples)}
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--notes', type=int, default=30)
    parser.add_argument('--paragraphs', type=int, default=30)
    parser.add_argument('--frames', type=int, default=120)
    parser.add_argument('--profile')
    parser.add_argument('--mode', choices=('prose', 'hlil', 'tables'), default='prose')
    parser.add_argument('--capture', help='Optional standalone offscreen visual-inspection image')
    args = parser.parse_args()
    print(json.dumps(run(args.notes, args.paragraphs, args.frames, args.profile,
                         args.mode, args.capture), indent=2))
