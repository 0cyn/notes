"""Read live notebook/rendering state and recent UI errors without changing focus."""


def run(session):
    import re
    import sys
    from collections import Counter

    import PySide6
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractItemView, QApplication, QPlainTextEdit, QTextEdit
    from shiboken6 import isValid

    app = QApplication.instance()
    result = {'python': sys.version, 'executable': sys.executable, 'prefix': sys.prefix,
              'pyside': PySide6.__version__, 'notebooks': [], 'logs': []}
    parser = getattr(app, '_bnnotes_parser_process', None)
    if parser is not None and isValid(parser):
        process = parser.process
        result['parser'] = {'command': parser.command, 'closed': parser.closed,
                            'pid': process.pid if process is not None else None,
                            'exit_code': process.poll() if process is not None else None}
    for widget in app.allWidgets():
        if not isValid(widget):
            continue
        if widget.objectName() == 'bnnotes-notebook':
            try:
                book = widget.notebook
                item = {'visible': widget.isVisible(), 'class': type(widget).__module__,
                        'file': book.storage.data.file.filename if book is not None else None,
                        'pages': []}
                for page in widget._pages.values():
                    note = page.session
                    state = {'id': note.id, 'visible': page.isVisible(), 'built': page._built,
                             'materialized': note.materialized}
                    if note.materialized:
                        highlighter = note.highlighter
                        state.update(warning=highlighter.warning,
                                     current=highlighter.analysis_is_current,
                                     link_count=len(highlighter.analysis.links),
                                     links=[link.url for link in highlighter.analysis.links[:8]],
                                     document_size=note.document.characterCount(),
                                     pending_blocks=len(note.layout._pending_blocks))
                    if page._built:
                        links = page.editor._links
                        state['bound_provider'] = links.provider is not None
                        state['controls'] = {name: hasattr(page, name) for name in (
                            '_connect_editor', 'find_bar', 'outline', 'status', '_find_keys',
                        )}
                        if links.controller is not None:
                            state['embeds'] = [{'address': entry.preview.address,
                                                'available': entry.preview.available}
                                               for entry in links.controller.entries[:8]]
                    item['pages'].append(state)
                provider = widget._link_previews
                item['provider'] = {'cached': len(provider._cache),
                                    'pending': len(provider._pending),
                                    'resolved': list(provider._resolved.items())[:8]}
                result['notebooks'].append(item)
            except Exception as error:
                result['notebooks'].append({'error': repr(error)})
        ancestry, parent = [], widget
        for _ in range(5):
            if parent is None:
                break
            ancestry.append(parent.metaObject().className())
            parent = parent.parentWidget()
        if not any(name == 'LogView' or name.startswith('ScriptingConsole') for name in ancestry):
            continue
        try:
            if isinstance(widget, (QTextEdit, QPlainTextEdit)):
                result['logs'].append({'class': ancestry, 'text': widget.toPlainText()[-20000:]})
            elif isinstance(widget, QAbstractItemView) and widget.model() is not None:
                model = widget.model()
                count = model.rowCount()
                role = 260 if 'LogView' in ancestry else Qt.ItemDataRole.DisplayRole
                rows = [[str(model.data(model.index(row, column), role))
                         for column in range(min(4, model.columnCount()))]
                        for row in range(max(0, count - 80), count)]
                result['logs'].append({'class': ancestry, 'count': count, 'rows': rows})
                if 'LogView' in ancestry:
                    details = {'model': model.metaObject().className(),
                               'methods': [name for name in dir(model) if any(
                                   word in name.lower()
                                   for word in ('log', 'entry', 'text', 'source')
                               )],
                               'roles': {str(key): bytes(value).decode(errors='replace')
                                         for key, value in model.roleNames().items()}, 'data': []}
                    for row in range(max(0, count - 8), count):
                        index = model.index(row, 0)
                        values = {}
                        for role in range(256, 280):
                            value = model.data(index, role)
                            if value is not None:
                                values[str(role)] = str(value)[:4000]
                        details['data'].append(values)
                    if hasattr(model, 'sourceModel'):
                        source = model.sourceModel()
                        details['source'] = {'class': source.metaObject().className(),
                                             'methods': [name for name in dir(source) if any(
                                                 word in name.lower() for word in
                                                 ('log', 'entry', 'text', 'message')
                                             )]}
                    result['log_model'] = details
                    messages = [str(model.data(model.index(row, 0), 260))
                                for row in range(max(0, count - 3000), count)]
                    blocks, block, errors = [], [], Counter()
                    for message in messages:
                        if 'Traceback (most recent call last)' in message:
                            block = []
                        block.append(message)
                        if re.match(r'^\w*(Error|Exception):', message):
                            errors[message] += 1
                            text = '\n'.join(block)
                            if 'bnnotes/' in text and text not in blocks:
                                blocks.append(text)
                            block = []
                    result['tracebacks'] = [text[-8000:] for text in blocks[:15]]
                    result['error_counts'] = dict(errors)
        except Exception as error:
            result['logs'].append({'class': ancestry, 'error': repr(error)})
    return result
