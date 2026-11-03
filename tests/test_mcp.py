from types import SimpleNamespace

import pytest

from src.mcp import NoteToolError, NoteTools, note_title
from src.settings import MemorySettings, Scope
from src.storage import METADATA_KEY, NoteStorage, StoredNote, writers_key


def tools(**settings):
    backend = MemorySettings()
    for name, value in settings.items():
        backend.write(name, value, None, Scope.APPLICATION)
    return NoteTools(backend)


def stored(data, *notes, **root):
    data.values[METADATA_KEY] = dict(root, version=2, notes=[
        {'id': note_id, 'text': text, 'mcp-allow': allow} for note_id, text, allow in notes])
    return SimpleNamespace(binary_view=data)


def texts(data):
    return {note.id: note.text for note in NoteStorage(data).load()}


def test_list_read_and_patch_whitelisted_notes(data):
    call = stored(data, ('a', '# Crypto\n\nkey = 1\nkey = 2\n', True), ('b', 'secret', False))
    notes = tools()
    listed = notes.list_notes(call)
    assert [(note['id'], note['title']) for note in listed['notes']] == [('a', 'Crypto')]
    assert listed['hidden_notes'] == 1
    assert notes.read_note(call, 'a')['text'].startswith('# Crypto')

    with pytest.raises(NoteToolError, match='appears 2 times'):
        notes.edit_note(call, 'a', 'key', 'value')
    notes.edit_note(call, 'a', 'key = 2', 'key = 3')
    notes.append_to_note(call, 'a', 'done')
    assert texts(data)['a'] == '# Crypto\n\nkey = 1\nkey = 3\ndone'
    person, agent = (None, None), ('AI', None)
    note = NoteStorage(data).load()[0]
    # The edited line is still partly the person's; the appended one is the agent's alone.
    assert note.agents == ((3, 2, 'AI'),)
    assert note.earlier == ((3, 1, writers_key([person])),)
    notes.edit_note(call, 'a', 'key', 'k', replace_all=True, editor_name='Claude Opus 5')
    note = NoteStorage(data).load()[0]
    assert note.agents == ((2, 2, 'Claude Opus 5'), (4, 1, 'AI'))
    assert note.earlier == ((2, 1, writers_key([person])), (3, 1, writers_key([person, agent])))
    assert notes.replace_note(call, 'a', '# Renamed\r\n')['title'] == 'Renamed'
    assert texts(data) == {'a': '# Renamed\n', 'b': 'secret'}

    for action in (lambda: notes.read_note(call, 'b'),
                   lambda: notes.append_to_note(call, 'b', 'x')):
        with pytest.raises(NoteToolError) as error:
            action()
        assert error.value.code == 'forbidden'
    with pytest.raises(NoteToolError, match='cannot be emptied'):
        notes.edit_note(call, 'a', '# Renamed\n', '')


def test_view_gate_and_create_and_delete_policies(data):
    call = stored(data, ('a', 'one', True), ('b', 'two', False), **{'mcp-allow': False})
    with pytest.raises(NoteToolError, match='turned off'):
        tools().list_notes(call)

    call = stored(data, ('a', 'one', True), ('b', 'two', False))
    with pytest.raises(NoteToolError, match='Creating notes'):
        tools(mcp_allow_create=False).create_note(call, '# New')
    with pytest.raises(NoteToolError, match='Deleting notes'):
        tools(mcp_allow_delete='No').delete_note(call, 'a')

    created = tools().create_note(call, '# New')  # Agents may create notes by default.
    assert created['title'] == 'New' and NoteStorage(data).load()[-1].mcp_allow is True
    with pytest.raises(NoteToolError) as error:
        tools().delete_note(call, 'b')  # By default, only whitelisted notes.
    assert error.value.code == 'forbidden'
    tools().delete_note(call, 'a')
    tools(mcp_allow_delete='All').delete_note(call, 'b')
    assert list(texts(data)) == [created['id']]


def test_folders(data):
    call = stored(data, ('a', 'one', True), folders=[
        {'id': 'fs', 'name': 'Filesystems'}, {'id': 'iso', 'name': 'ISO', 'parent': 'fs'}])
    notes = tools(mcp_allow_create=True)
    assert notes.move_note(call, 'a', 'iso')['folder'] == 'Filesystems / ISO'
    assert notes.create_note(call, 'two', 'fs')['folder_id'] == 'fs'
    assert notes.move_note(call, 'a')['folder'] is None
    with pytest.raises(NoteToolError, match='No folder'):
        notes.move_note(call, 'a', 'missing')


def test_titles_skip_code_fences():
    notes = [StoredNote('a', '```\n# not this\n```\n# Title #\n'), StoredNote('b', 'plain')]
    assert [note_title(note, notes) for note in notes] == ['Title', 'Note 2']


def test_open_notebook_receives_changes_as_undoable_edits(notepad, data):
    from src.ui.session import LiveNotes, notebook_for

    widget = notepad()
    widget.replace_source('# Open\n\nfirst line\n')
    note = widget.session
    note.mcp_allow = True
    note.persist()
    notes = NoteTools(MemorySettings(), live=LiveNotes.for_view)
    call = SimpleNamespace(binary_view=data)

    notes.edit_note(call, note.id, 'first', 'second', editor_name='Claude')
    assert widget.editor.toPlainText() == '# Open\n\nsecond line\n'
    assert note.agents == ((2, 1, 'Claude'),)
    widget.editor.undo()
    assert note.agents == ()
    assert texts(data)[note.id] == '# Open\n\nfirst line\n'

    notes.settings.write('mcp_allow_create', True, None, Scope.APPLICATION)
    created = notes.create_note(call, 'from MCP')
    notebook = notebook_for(data)
    assert notebook.note(created['id']).source == 'from MCP'
    notes.append_to_note(call, created['id'], 'closed note')
    assert notebook.note(created['id']).source == 'from MCP\nclosed note'
    # Typing in the open note keeps the note MCP created.
    widget.replace_source('# Open\n')
    assert texts(data)[created['id']] == 'from MCP\nclosed note'
