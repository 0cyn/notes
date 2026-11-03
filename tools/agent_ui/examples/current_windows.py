"""Capture native app client areas without activating a window or changing tabs."""


def run(session):
    import binaryninjaui as ui

    result = []
    for context in ui.UIContext.allContexts():
        window = context.mainWindow()
        session.capture(window, f'app-{len(result)}')
        result.append({'title': window.windowTitle(),
                       'geometry': list(window.geometry().getRect())})
    return result
