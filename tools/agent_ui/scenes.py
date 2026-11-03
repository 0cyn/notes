"""Reusable visual scenes. These produce inspection artifacts, not test verdicts."""


def prepare(options):
    if options['scene'] != 'hlil':
        return None
    import binaryninja as bn

    binary = bn.BinaryView.new(bytes.fromhex('85ff7409e81700000083c02ac331c0c3')
                              + b'\x90' * 16 + bytes.fromhex('8d4701c3'))
    try:
        binary.platform = bn.Platform['linux-x86_64']
        binary.add_function(0)
        binary.add_function(0x20)
        binary.update_analysis_and_wait()
        binary.get_function_at(0).type = 'int compute_result(int objectId)'
        binary.get_function_at(0x20).name = 'process'
        binary.update_analysis_and_wait()
        return binary
    except Exception:
        binary.file.close()
        raise


def cleanup(binary):
    if binary is not None:
        binary.file.close()


def run(session):
    from pathlib import Path

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextCursor

    scene = session.options['scene']
    source = (Path(__file__).resolve().parents[2] / 'examples/analysis.md').read_text()
    if scene == 'hlil':
        source = ('# HLIL embeds\n\n[!](binaryninja://?expr=0)\n\n'
                  '[!](binaryninja://?expr=2)\n\n[statement](binaryninja://?expr=2)\n\n'
                  '| Code |\n| --- |\n| [!](binaryninja://?expr=2) |\n\n')
    if scene == 'onboarding':
        with session.notebook(binary=session.prepared, width=session.options['width'],
                              height=session.options['height'], onboarding=True) as widget:
            page = widget.onboarding
            for step in range(page.pages.count()):
                page.show_step(step)
                session.settle()
                session.capture(widget, f'onboarding-{step + 1}')
            return session.describe(widget, depth=1)
    with session.notebook(source, binary=session.prepared, width=session.options['width'],
                          height=session.options['height']) as widget:
        widget.current_note.outline_button.setChecked(False)
        widget.editor.moveCursor(QTextCursor.MoveOperation.Start)
        if scene == 'settings':
            scope = session.module('settings').Scope
            widget.notebook.preferences.set('font_size', 18, scope.APPLICATION)
            widget.notebook.preferences.set('font_size', 22, scope.VIEW)
            widget.open_settings()
        elif scene == 'search':
            widget.new_note().replace_source('# Findings\n\nAllocation failed here.\n')
            page = widget.open_find_all()
            page.query.setText('allocation')
            session.wait_for(lambda: page.results.matches > 0, description='search results')
        elif scene == 'tabs':
            first = widget.current_note
            widget.new_note().replace_source('# Another note\n\nAn independent pane.')
            container = widget.tabs.currentContainer()
            container.splitTab.emit(container.indexOf(first), Qt.Edge.RightEdge)
        elif scene == 'hlil':
            provider = widget._link_previews
            targets = [('binaryninja://?expr=0', True), ('binaryninja://?expr=2', True),
                       ('binaryninja://?expr=2', False)]
            session.wait_for(lambda: all(provider.request(url, expand_block=mode).available
                                        for url, mode in targets), description='HLIL snapshots')
        session.settle()
        session.capture(widget, scene)
        return session.describe(widget, depth=1)
