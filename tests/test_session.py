"""An open notebook when someone else's work is merged into its file (an Enterprise sync)."""

import copy
import random

from PySide6.QtCore import QCoreApplication

from src.merge import MARKER_FIRST, MARKER_SECOND, conflict_count
from src.storage import METADATA_KEY, NoteStorage, _runs

from .conftest import NOW


def _root():
    return {'version': 2, 'notes': [
        {'id': 'a', 'text': '# A\nfirst line\nsecond line', 'edited': NOW},
        {'id': 'b', 'text': '# B\nbody', 'edited': NOW},
        {'id': 'c', 'text': '# C\nkept', 'edited': NOW},
    ]}


def _sync(data, change):
    """Someone else's work arrives: the stored root changes underneath the notebook."""
    root = copy.deepcopy(data.values[METADATA_KEY])
    change(root)
    data.values[METADATA_KEY] = root


def _note(root, identity):
    return next((item for item in root['notes'] if item['id'] == identity), None)


def _stored(data):
    return {item['id']: item['text'] for item in data.values[METADATA_KEY]['notes']}


def _mirrors_storage(book, data):
    """The notebook holds exactly the stored notes, nothing more, nothing less."""
    stored = NoteStorage(data).load()
    assert [(note.id, note.source) for note in book.saved_notes] == [
        (note.id, note.text) for note in stored]
    assert {folder.id: folder.name for folder in book.folders} == {
        folder.id: folder.name for folder in NoteStorage(data).folders or book.storage.folders}


def _open(widget, identity):
    page = widget.open_note(widget.notebook.note(identity))
    QCoreApplication.processEvents()
    return page


def _type(page, text):
    cursor = page.editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    page.editor.setTextCursor(cursor)
    page.editor.insertPlainText(text)
    page.session.attribute_edits()
    QCoreApplication.processEvents()


def test_a_local_edit_merges_notes_synced_meanwhile(notebook, data):
    data.values[METADATA_KEY] = _root()
    widget = notebook()
    book = widget.notebook
    page = _open(widget, 'a')

    def change(root):
        _note(root, 'b')['text'] = '# B\nbody\nbob was here'
        root['notes'].append({'id': 'd', 'text': '# D\nfrom bob'})
    _sync(data, change)
    _type(page, '\nmine')
    stored = _stored(data)
    assert stored == {'a': '# A\nfirst line\nsecond line\nmine',
                      'b': '# B\nbody\nbob was here', 'c': '# C\nkept', 'd': '# D\nfrom bob'}
    # The notebook took the merge: the synced note exists and the closed note is current.
    assert book.adopted and book.note('d').source == '# D\nfrom bob'
    assert book.note('b').source == '# B\nbody\nbob was here'
    assert book.note('d').title == 'D'
    _mirrors_storage(book, data)
    # More typing after a merge keeps everyone's work: the merge is the new base.
    _type(page, ' and more')
    assert _stored(data)['d'] == '# D\nfrom bob'
    assert _stored(data)['a'].endswith('mine and more')
    _mirrors_storage(book, data)


def test_both_changing_the_same_lines_shows_conflict_markers_in_the_open_note(notebook, data):
    data.values[METADATA_KEY] = _root()
    widget = notebook()
    book = widget.notebook
    page = _open(widget, 'a')
    _sync(data, lambda root: _note(root, 'a').update(
        text='# A\nfirst line\nsecond line, as bob wrote it',
        users=[{'line': 2, 'count': 1, 'name': 'bob'}]))
    cursor = page.editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    page.editor.setTextCursor(cursor)
    page.editor.insertPlainText(', as I wrote it')
    page.session.attribute_edits()
    # Typing doesn't merge inside the document's own change: on the next event-loop turn.
    QCoreApplication.processEvents()
    text = page.editor.toPlainText()
    assert MARKER_FIRST in text and MARKER_SECOND in text
    assert 'second line, as I wrote it' in text and 'second line, as bob wrote it' in text
    assert text.splitlines()[-1] == '>>>>>>> bob'
    assert _stored(data)['a'] == text
    assert conflict_count(data.values[METADATA_KEY]) == 1
    # Bob's line keeps his name in the open document.
    assert (text.splitlines().index('second line, as bob wrote it'), 1, 'bob') in (
        _runs(_note(data.values[METADATA_KEY], 'a'), 'users'))
    # The history starts again after someone else's work, and editing carries on normally.
    assert not page.editor.document().isUndoAvailable()
    _type(page, '\nresolved later')
    assert _stored(data)['a'].endswith('resolved later')
    assert page.editor.document().isUndoAvailable()
    _mirrors_storage(book, data)


def test_notes_deleted_elsewhere_close_unless_edited_here(notebook, data):
    data.values[METADATA_KEY] = _root()
    widget = notebook()
    book = widget.notebook
    page_b = _open(widget, 'b')
    page_a = _open(widget, 'a')

    def delete(root):
        root['notes'] = [item for item in root['notes'] if item['id'] not in ('a', 'b')]
    _sync(data, delete)
    _type(page_a, '\nstill writing')
    # B was untouched here: deleted, its tab closed. A was being edited: kept.
    assert book.note('b') is None and 'b' not in _stored(data)
    assert page_b not in widget.tabs.pages()
    assert book.note('a') is not None and _stored(data)['a'].endswith('still writing')
    assert page_a in widget.tabs.pages()
    _mirrors_storage(book, data)


def test_deleting_a_note_someone_just_edited_keeps_it(notebook, data, monkeypatch):
    data.values[METADATA_KEY] = _root()
    widget = notebook()
    book = widget.notebook
    _sync(data, lambda root: _note(root, 'b').update(text='# B\nbob kept writing'))
    assert not book.delete_note(book.note('b'))
    assert book.note('b').source == '# B\nbob kept writing'
    assert _stored(data)['b'] == '# B\nbob kept writing'
    _mirrors_storage(book, data)
    # Deleting it again, with nothing new from elsewhere, deletes it.
    assert book.delete_note(book.note('b'))
    assert 'b' not in _stored(data)


def test_folder_changes_merge_with_synced_folders(notebook, data):
    data.values[METADATA_KEY] = dict(_root(), folders=[{'id': 'f', 'name': 'Shared'}])
    widget = notebook()
    book = widget.notebook
    _sync(data, lambda root: root['folders'].append({'id': 'g', 'name': 'Bob’s'}))
    mine = book.new_folder('Mine')
    assert mine is not None
    assert [folder['name'] for folder in data.values[METADATA_KEY]['folders']] == [
        'Shared', 'Bob’s', 'Mine']  # Each side's new folders follow their predecessor.
    assert {folder.name for folder in book.folders} == {'Shared', 'Mine', 'Bob’s'}
    # Folder objects the notebook already had stay the same objects.
    assert book.move_note(book.note('a'), book.folder(mine.id))
    assert _note(data.values[METADATA_KEY], 'a')['folder'] == mine.id


def test_a_sync_is_noticed_without_any_local_edit(notebook, data, monkeypatch):
    data.values[METADATA_KEY] = _root()
    widget = notebook()
    book = widget.notebook
    page = _open(widget, 'c')
    snapshot = [1]
    monkeypatch.setattr(book.storage, 'snapshot', lambda: snapshot[0])
    book._snapshot = 1
    writes = len(data.writes)
    _sync(data, lambda root: _note(root, 'c').update(text='# C\nkept\nand synced'))
    assert not book.check_elsewhere()  # The snapshot didn't move: nothing to look at.
    snapshot[0] = 2
    assert book.check_elsewhere()
    assert page.editor.toPlainText() == '# C\nkept\nand synced'
    # Nothing of ours to add, so nothing is written back.
    assert len(data.writes) == writes
    _mirrors_storage(book, data)
    assert not book.check_elsewhere()


def test_random_local_and_synced_work_never_diverges(notebook, data):
    """Interleave local edits, deletions and folders with syncs of random changes; after
    every save, the open notebook must be exactly what is stored and nothing synced is lost
    unless it was deleted, or changed into a conflict, here."""
    rng = random.Random(42)
    data.values[METADATA_KEY] = _root()
    widget = notebook()
    book = widget.notebook
    pages = {identity: _open(widget, identity) for identity in ('a', 'b')}
    counter = iter(range(10**6))
    merges = conflicts = 0
    for _round in range(150):
        synced = set()
        if rng.random() < 0.6:
            def change(root, synced=synced):
                for item in list(root['notes']):
                    roll = rng.random()
                    if roll < 0.2:
                        item['text'] += f'\nsynced {next(counter)}'
                        synced.add(item['text'].rsplit('\n', 1)[1])
                    elif roll < 0.3 and '\n' in item['text']:
                        # Rewrite the last line, which local edits also rewrite.
                        line = f'synced rewrite {next(counter)}'
                        item['text'] = item['text'].rsplit('\n', 1)[0] + '\n' + line
                        synced.add(line)
                    elif roll < 0.33:
                        root['notes'].remove(item)
                if rng.random() < 0.4:
                    text = f'# Synced {next(counter)}'
                    root['notes'].append({'id': f's{next(counter)}', 'text': text})
                    synced.add(text)
            _sync(data, change)
        live = [note for note in book.saved_notes]
        roll = rng.random()
        if live and roll < 0.6:
            note = rng.choice(live)
            page = pages.get(note.id)
            line = f'local {next(counter)}'
            if rng.random() < 0.4 and '\n' in note.source:
                start = note.source.rindex('\n') + 1  # Rewrite the last line.
                note.replace_text(start, len(note.source), line)
            elif page is not None and page.session is note:
                _type(page, f'\n{line}')
            else:
                note.replace_text(len(note.source), len(note.source), f'\n{line}')
        elif live and roll < 0.7:
            book.delete_note(rng.choice(live))
        elif roll < 0.8:
            book.new_folder(f'Folder {next(counter)}')
        else:
            book.persist()
        QCoreApplication.processEvents()
        merges += book.adopted
        stored = '\n'.join(_stored(data).values())
        # Local work only appends, and an edit beats a delete: nothing synced is ever lost.
        for text in synced:
            assert text in stored, (text, _stored(data))
        conflicts = max(conflicts, conflict_count(data.values[METADATA_KEY]))
        _mirrors_storage(book, data)
        pages = {identity: page for identity, page in pages.items()
                 if page in widget.tabs.pages()}
    # The run really merged, and really hit conflicts (both appending to the same note).
    assert merges > 30 and conflicts > 0, (merges, conflicts)


def test_conflicts_inside_tables_and_code_blocks_stay_editable(notebook, data):
    table = '| Field | Size |\n| --- | --- |\n| header | 4 |\n| body | 8 |'
    code = '```c\nint size = 4;\nreturn size;\n```'
    data.values[METADATA_KEY] = {'version': 2, 'notes': [
        {'id': 'a', 'text': f'# A\n\n{table}\n\n{code}\nend'}]}
    widget = notebook()
    book = widget.notebook
    page = _open(widget, 'a')
    _sync(data, lambda root: _note(root, 'a').update(text=_note(root, 'a')['text'].replace(
        '| body | 8 |', '| body | 16 |').replace('int size = 4;', 'int size = 16;')))
    # Every keystroke is written, so only the edit after a sync can meet it: this one, in the
    # table row the sync also changed. The code block takes the synced change cleanly.
    source = page.session.source
    start = source.index('| body | 8 |')
    page.session.replace_text(start, start + len('| body | 8 |'), '| body | 12 |')
    QCoreApplication.processEvents()
    text = page.editor.toPlainText()
    assert conflict_count({'notes': [{'text': text}]}) == 1
    assert '| body | 12 |' in text and '| body | 16 |' in text
    assert 'int size = 16;' in text and 'int size = 4;' not in text
    # The editor keeps working on the conflicted note: tables re-render, typing persists.
    QCoreApplication.processEvents()
    _type(page, '\nfixed')
    assert _stored(data)['a'].endswith('fixed')
    _mirrors_storage(book, data)


def test_a_large_collection_adopts_a_sync_quickly(notebook, data):
    import time
    body = '\n'.join(f'line {index} of the investigation' for index in range(100))
    data.values[METADATA_KEY] = {'version': 2, 'notes': [
        {'id': f'n{index}', 'text': f'# Note {index}\n{body}'} for index in range(300)]}
    widget = notebook()
    book = widget.notebook
    pages = [_open(widget, f'n{index}') for index in range(5)]

    def change(root):
        for item in root['notes'][::6]:
            item['text'] = item['text'].replace('line 50 ', 'synced line 50 ')
        root['notes'].append({'id': 'new', 'text': '# New'})
    _sync(data, change)
    start = time.perf_counter()
    pages[0].session.replace_text(0, 0, 'local ')
    QCoreApplication.processEvents()
    elapsed = time.perf_counter() - start
    assert elapsed < 3, elapsed
    assert book.note('new') is not None
    assert 'synced line 50 ' in book.note('n6').source
    assert 'synced line 50 ' in pages[0].editor.toPlainText()
    assert _stored(data)['n0'].startswith('local # Note 0')
    _mirrors_storage(book, data)

