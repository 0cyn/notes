"""Resolve the requested file across native contexts; optionally open Notes."""

from pathlib import Path
from types import SimpleNamespace


def run(session):
    import binaryninjaui as ui

    path = Path(session.options['file'])

    def find():
        return [(context, tab, frame) for context in ui.UIContext.allContexts()
                for tab in context.getTabs() for frame in context.getAllViewFramesForTab(tab)
                if frame.getCurrentBinaryView() is not None
                and Path(frame.getCurrentBinaryView().file.filename).resolve() == path]

    if not find():
        context = ui.UIContext.activeContext()
        if context is None or not context.openFilename(str(path)):
            raise RuntimeError(f'Could not open {path}.')
    session.wait_for(find, timeout=session.options['timeout'], description=str(path))
    context, tab, _frame = find()[0]
    context.activateTab(tab)
    context.mainWindow().activateWindow()
    surface = session.options['surface']
    if surface:
        from bnnotes.src.ui.integration import _open_pane, _open_view

        frame = context.getCurrentViewFrame()
        action = SimpleNamespace(context=context, binaryView=frame.getCurrentBinaryView())
        (_open_pane if surface == 'pane' else _open_view)(action)
    return {'file': str(path), 'surface': surface, 'window': context.mainWindow().windowTitle()}
