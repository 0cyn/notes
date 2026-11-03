from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.storage import METADATA_KEY, NoteStorage, NoteStorageError, StoredFolder, StoredNote

from .conftest import _root


def test_round_trip_and_missing_note(data):
    storage = NoteStorage(data)
    assert storage.load() == []
    source = "# 🙂\n\n**bold** with trailing spaces  \n"
    notes = [StoredNote.create(source), StoredNote.create("second note")]
    storage.save(notes)
    assert NoteStorage(data).load() == notes
    stored = dict(data.values[METADATA_KEY])
    revision = stored.pop('revision')
    assert stored == {
        'version': 2, 'notes': [{'id': note.id, 'text': note.text, 'mcp-allow': False}
                                 for note in notes],
    }
    # Every write is a new revision, so concurrent changes always reach the notes merge;
    # writing what is already stored writes nothing.
    writes = len(data.writes)
    storage.save(notes)
    assert len(data.writes) == writes
    storage.save([*notes, StoredNote.create('third')])
    assert data.values[METADATA_KEY]['revision'] not in (revision, None)


def test_analysis_views_share_raw_storage(data):
    analysis_view = SimpleNamespace(file=data.file)
    notes = [StoredNote.create("shared")]
    NoteStorage(analysis_view).save(notes)
    assert NoteStorage(data).load() == notes


@pytest.mark.parametrize('payload', ['invalid', {}])
def test_invalid_or_future_metadata_is_never_replaced(data, payload):
    data.values[METADATA_KEY] = payload
    with pytest.raises(NoteStorageError):
        NoteStorage(data).load()
    with pytest.raises(NoteStorageError):
        NoteStorage(data).save([StoredNote.create('replacement')])
    assert data.values[METADATA_KEY] == payload
    assert not data.writes


def test_write_errors_are_reported(data):
    data.write_error = True
    with pytest.raises(NoteStorageError, match="simulated write failure"):
        NoteStorage(data).save([StoredNote.create("keep this text")])


def test_folders_and_note_folders_round_trip(data):
    data.values[METADATA_KEY] = _root()
    storage = NoteStorage(data)
    notes = storage.load()
    assert [note.folder for note in notes] == ['fs', 'iso', None]
    assert storage.folders[1] == StoredFolder('iso', 'ISO 9660', 'fs')
    storage.save(notes, folders=storage.folders)
    assert data.values[METADATA_KEY] == _root()


def test_optional_root_and_note_booleans_round_trip_with_extensions(data):
    root = {'version': 2, 'mcp-allow': False, 'client-extra': 'keep', 'notes': [
        {'id': 'one', 'text': 'body', 'mcp-allow': True, 'extra': 7},
        {'id': 'two', 'text': 'other'},
    ]}
    data.values[METADATA_KEY] = deepcopy(root)
    storage = NoteStorage(data)
    notes = storage.load()
    assert storage.mcp_allow is False
    assert [note.mcp_allow for note in notes] == [True, None]
    storage.save(notes)
    assert data.values == {METADATA_KEY: root}
    data.values[METADATA_KEY]['mcp-allow'] = True  # External root-gate change.
    storage.save([StoredNote('one', 'edited', True), notes[1]])
    assert data.values[METADATA_KEY]['mcp-allow'] is True
    assert storage.mcp_allow is True
    storage.save(notes, mcp_allow=None)
    assert 'mcp-allow' not in data.values[METADATA_KEY]




def test_agent_and_user_lines_round_trip_and_follow_line_edits(data):
    from src.storage import line_names, retag

    note = StoredNote.create("one\ntwo", author="Claude", user="cynder")
    NoteStorage(data).save([note])
    stored = data.values[METADATA_KEY]['notes'][0]
    assert stored['agents'] == [{'line': 0, 'count': 2, 'name': 'Claude'}]
    assert stored['users'] == [{'line': 0, 'count': 2, 'name': 'cynder'}]
    assert NoteStorage(data).load() == [note]
    # Unchanged lines keep their name; changed and new lines get the editor's.
    agents = retag("human\nagent\ntail", ((1, 1, 'Claude'),), "new\nhuman\nagent\ntail!", 'GPT')
    assert line_names("new\nhuman\nagent\ntail!", agents) == ['GPT', None, 'Claude', 'GPT']
    assert retag("a\nb", ((0, 2, 'Claude'),), "a\nb\nc", None) == ((0, 2, 'Claude'),)


def test_lines_keep_earlier_writers_whose_text_they_still_hold(data):
    from src.storage import line_names, parse_writers, retag_writers, writers_key

    claude, ada = ('Claude', 'cynder'), (None, 'ada')
    agents, users, earlier = retag_writers(
        "plan\nstep", (((0, 2, 'Claude'),), ((0, 2, 'cynder'),), ()), "plan!\nstep\nmore", ada)
    # The edited line holds both writers' text; the unchanged and the new lines hold one's.
    assert line_names("plan!\nstep\nmore", agents) == [None, 'Claude', None]
    assert line_names("plan!\nstep\nmore", users) == ['ada', 'cynder', 'ada']
    assert [parse_writers(key) for key in line_names("plan!\nstep\nmore", earlier)] == [
        (claude,), (), ()]
    # Claude editing again moves to the end: writers run oldest first by their last edit.
    _agents, _users, again = retag_writers("plan!\nstep\nmore", (agents, users, earlier),
                                           "plan!?\nstep\nmore", claude)
    assert again == ((0, 1, writers_key([ada])),)
    note = StoredNote('n', "plan!", earlier=((0, 1, writers_key([claude, (None, None)])),))
    NoteStorage(data).save([note])
    assert data.values[METADATA_KEY]['notes'][0]['earlier'] == [
        {'line': 0, 'count': 1, 'writers': [{'agent': 'Claude', 'user': 'cynder'}, {}]}]
    assert NoteStorage(data).load() == [note]
    # A client that keeps fields it doesn't know (an older Notes) can change the text under
    # them; the earlier writers no longer describe its lines and are ignored.
    data.values[METADATA_KEY]['notes'][0]['text'] = "new first line\nplan!"
    assert NoteStorage(data).load()[0].earlier == ()


def test_rewritten_lines_keep_writers_only_where_they_edit_an_old_line():
    from src.storage import line_names, parse_writers, retag_writers

    claude = ('Claude', None)
    # A line inserted above an edit is new; the edited line below it keeps Claude.
    new = "inserted\na!\nb"
    _agents, _users, earlier = retag_writers("a\nb", (((0, 2, 'Claude'),), (), ()), new,
                                             (None, None))
    assert [parse_writers(key) for key in line_names(new, earlier)] == [(), (claude,), ()]
    # Rewriting everything leaves no earlier writers behind.
    new = "totally\n\nnew text here\nand more"
    assert retag_writers("mine\n\nagent one\nagent two", (((2, 2, 'Claude'),), (), ()), new,
                         ('GPT', None))[2] == ()
