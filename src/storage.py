from __future__ import annotations

import getpass
import hashlib
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from uuid import uuid4

METADATA_KEY = "bnnotes.notepad"
METADATA_VERSION = 2
_UNCHANGED = object()


def _optional_bool(item):
    if 'mcp-allow' in item and type(item['mcp-allow']) is not bool:
        raise NoteStorageError('Invalid mcp-allow metadata; expected a boolean.')
    return item.get('mcp-allow')


def _edited(item):
    # Presentation-only: an unreadable time is dropped rather than failing the load.
    value = item.get('edited')
    return value if type(value) is int and value >= 0 else None


def _runs(item, key):
    """Readable (line, count, name) runs under `key`; malformed entries are dropped."""
    runs, end = [], 0
    for run in item.get(key) if isinstance(item.get(key), list) else ():
        if (isinstance(run, dict) and type(run.get('line')) is int and type(run.get('count')) is int
                and run['line'] >= end and run['count'] > 0
                and isinstance(run.get('name'), str) and run['name']):
            runs.append((run['line'], run['count'], run['name']))
            end = run['line'] + run['count']
    return tuple(runs)


def _without_revision(root):
    return {key: value for key, value in root.items() if key != 'revision'}


def text_digest(text):
    """Identifies the text `earlier` was recorded for: a client that keeps unknown fields but
    not this one (an older Notes) may change the text without moving its runs."""
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def _earlier_runs(item):
    """Readable (line, count, writers_key) runs of the writers before each line's last; none
    unless they were recorded for this exact text."""
    if item.get('earlier-text') != text_digest(item['text']):
        return ()
    runs, end = [], 0
    for run in item.get('earlier') if isinstance(item.get('earlier'), list) else ():
        if not (isinstance(run, dict) and type(run.get('line')) is int
                and type(run.get('count')) is int and run['line'] >= end and run['count'] > 0
                and isinstance(run.get('writers'), list)):
            continue
        writers = tuple((writer.get('agent') or None, writer.get('user') or None)
                        for writer in run['writers'] if isinstance(writer, dict)
                        and all(isinstance(writer.get(key, ''), str) for key in ('agent', 'user')))
        if writers:
            runs.append((run['line'], run['count'], writers_key(writers)))
            end = run['line'] + run['count']
    return tuple(runs)


def _earlier_item(line, count, key):
    return {'line': line, 'count': count, 'writers': [
        {name: value for name, value in (('agent', agent), ('user', user)) if value}
        for agent, user in parse_writers(key)]}


def line_names(text, runs):
    """Each source line's name from (line, count, name) runs, or None."""
    names = [None] * (text.count('\n') + 1)
    for line, count, name in runs:
        names[line:line + count] = [name] * len(names[line:line + count])
    return names


def name_runs(names):
    """(line, count, name) runs of consecutive lines with the same name; None is omitted."""
    runs = []
    for line, name in enumerate(names):
        if name is None:
            continue
        if runs and runs[-1][2] == name and runs[-1][0] + runs[-1][1] == line:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1, name)
        else:
            runs.append((line, 1, name))
    return tuple(runs)


def retag(old_text, runs, new_text, name):
    """Runs for `new_text`: unchanged lines keep their name, changed lines get `name`."""
    old, new = old_text.split('\n'), new_text.split('\n')
    names, result = line_names(old_text, runs), []
    for kind, old_start, old_end, new_start, new_end in SequenceMatcher(
            None, old, new, autojunk=False).get_opcodes():
        result += (names[old_start:old_end] if kind == 'equal'
                   else [name] * (new_end - new_start))
    return name_runs(result)


def writers_key(writers):
    """One hashable value for an ordered list of (agent, user) writers; None for none."""
    return json.dumps([list(writer) for writer in writers]) if writers else None


def parse_writers(key):
    """The (agent, user) writers in a `writers_key`; unreadable entries are skipped."""
    try:
        items = json.loads(key) if key else []
    except (TypeError, ValueError):
        return ()
    return tuple((agent or None, user or None) for agent, user in (
        item for item in items if isinstance(item, list) and len(item) == 2
        and all(value is None or isinstance(value, str) for value in item)))


def edited_by(earlier, current, writer):
    """Earlier writers of a line `writer` edits, oldest first: everyone whose text it still
    holds, by when they last edited it, except `writer`. A writer recorded without a user
    (before Enterprise, or another install) is taken to be the same as `writer`'s kind."""
    result = [item for item in (*earlier, current)
              if item != writer and not (item[0] == writer[0] and item[1] is None)]
    return tuple(item for index, item in enumerate(result) if item not in result[index + 1:])


# How alike a rewritten line must be to the line it replaced to still hold its writers' text.
_SIMILAR = 0.5


def _similar(old, new):
    """`old` line index -> `new` line index for rewritten lines that are edits of each other,
    in order: each new line takes the most similar old line after the last one taken."""
    pairs, after = {}, 0
    for index, line in enumerate(new):
        best, score = None, _SIMILAR
        for candidate in range(after, len(old)):
            matcher = SequenceMatcher(None, old[candidate], line, autojunk=False)
            if (old[candidate] and line and matcher.real_quick_ratio() >= score
                    and matcher.quick_ratio() >= score and matcher.ratio() >= score):
                best, score = candidate, matcher.ratio()
        if best is not None:
            pairs[best] = index
            after = best + 1
    return pairs


def retag_writers(old_text, runs, new_text, writer):
    """(agents, users, earlier) runs for `new_text`, edited by the (agent, user) `writer`.

    `runs` are the old text's (agents, users, earlier). Unchanged lines keep their writers.
    A rewritten line that is an edit of an old one (mostly the same text) keeps that line's
    writers as earlier writers; other new lines are the writer's alone.
    """
    old, new = old_text.split('\n'), new_text.split('\n')
    agents, users, earlier = (line_names(old_text, item) for item in runs)
    result = ([], [], [])
    for kind, old_start, old_end, new_start, new_end in SequenceMatcher(
            None, old, new, autojunk=False).get_opcodes():
        if kind == 'equal':
            for names, source in zip(result, (agents, users, earlier), strict=True):
                names += source[old_start:old_end]
            continue
        edits = {index: line for line, index in _similar(
            old[old_start:old_end], new[new_start:new_end]).items()}
        for offset in range(new_end - new_start):
            result[0].append(writer[0])
            result[1].append(writer[1])
            line = old_start + edits[offset] if offset in edits else None
            result[2].append(None if line is None else writers_key(edited_by(
                parse_writers(earlier[line]), (agents[line], users[line]), writer)))
    return tuple(name_runs(names) for names in result)


def enterprise_user():
    """The signed-in Enterprise username, which notes record per line; None elsewhere."""
    try:
        from binaryninja import enterprise
        return enterprise.username() if enterprise.is_authenticated() else None
    except Exception:
        return None


def person_name():
    """A name for a line written by a person who recorded none: the OS user, or `Human`."""
    try:
        return getpass.getuser() or 'Human'
    except Exception:
        return 'Human'



def _folder_id(item):
    value = item.get('folder')
    return value if isinstance(value, str) and value else None


def _folders(value):
    """Readable folders; a missing or cyclic parent makes a folder top-level."""
    folders = {}
    for item in value if isinstance(value, list) else ():
        if (isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']
                and item['id'] not in folders and isinstance(item.get('name'), str)):
            parent = item.get('parent')
            folders[item['id']] = StoredFolder(
                item['id'], item['name'], parent if isinstance(parent, str) else None)
    result = []
    for folder in folders.values():
        seen, parent = {folder.id}, folder.parent
        while parent is not None and parent in folders and parent not in seen:
            seen.add(parent)
            parent = folders[parent].parent
        # Missing parent, or a cycle back to this folder (the walk stops on revisiting it).
        if folder.parent is not None and (folder.parent not in folders or parent == folder.id):
            folder = StoredFolder(folder.id, folder.name, None)
        result.append(folder)
    return result


class NoteStorageError(RuntimeError):
    """A note could not be loaded or persisted in Binary Ninja metadata."""


@dataclass(frozen=True)
class StoredNote:
    id: str
    text: str
    mcp_allow: bool | None = None
    # When the note's text last changed, in whole seconds since the Unix epoch.
    edited: int | None = None
    # The id of the StoredFolder holding the note; None (or an unknown id) is unfiled.
    folder: str | None = None
    # (line, count, name) runs of lines written by an agent; every other line is a person's.
    agents: tuple = ()
    # (line, count, username) runs: the Enterprise user who wrote each line, person or agent.
    users: tuple = ()
    # (line, count, writers_key) runs: who edited each line before its writer above, oldest
    # first, as (agent, user) pairs; their text is still part of the line.
    earlier: tuple = ()

    @classmethod
    def create(cls, text="", *, mcp_allow=False, edited=None, folder=None, author=None,
               user=None):
        lines = text.count('\n') + 1
        return cls(uuid4().hex, text, mcp_allow, edited, folder,
                   name_runs([author] * lines), name_runs([user] * lines))


@dataclass(frozen=True)
class StoredFolder:
    id: str
    name: str
    parent: str | None = None

    @classmethod
    def create(cls, name, parent=None):
        return cls(uuid4().hex, name, parent)


def view_name(data) -> str:
    """A file's name as Notes shows it: Binary Ninja's display name, else Untitled."""
    return data.file.display_name or "Untitled"


class NoteStorage:
    """Notes on the file's Raw view, shared by every analysis view; writes are synchronous."""

    def __init__(self, data):
        self.data = data
        raw = data.file.raw
        self.view = raw if raw is not None else data
        self.mcp_allow = None
        self.folders: list[StoredFolder] = []
        # The root as this storage last read or wrote it: the base for merging changes made
        # by anyone else (an Enterprise sync) since then.
        self._baseline = None
        # (notes, folders) merged in by the last save, for the open notebook to adopt.
        self.merged = None
        # Whether the last save found work from elsewhere and, told not to merge, wrote nothing.
        self.pending = False

    @property
    def session_id(self) -> int:
        return self.view.file.session_id

    @property
    def display_name(self) -> str:
        return view_name(self.view)

    def _read_root(self):
        try:
            value = self.view.query_metadata(METADATA_KEY)
        except KeyError:
            return {'version': METADATA_VERSION, 'notes': []}
        except Exception as error:
            raise NoteStorageError(f"Could not read note metadata: {error}") from error

        if (not isinstance(value, dict) or value.get('version') != METADATA_VERSION
                or not isinstance(value.get('notes'), list)):
            raise NoteStorageError('Invalid note metadata; the stored notes were preserved.')
        _optional_bool(value)
        identities = set()
        for item in value['notes']:
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                    or not item['id'] or item['id'] in identities
                    or not isinstance(item.get('text'), str)):
                raise NoteStorageError('Invalid note metadata; the stored notes were preserved.')
            _optional_bool(item)
            identities.add(item['id'])
        return value

    def load(self) -> list[StoredNote]:
        root = self._read_root()
        self._baseline = root
        self.mcp_allow = root.get('mcp-allow')
        self.folders = _folders(root.get('folders'))
        return self._notes(root)

    @staticmethod
    def _notes(root):
        return [StoredNote(item['id'], item['text'], item.get('mcp-allow'), _edited(item),
                           _folder_id(item), _runs(item, 'agents'), _runs(item, 'users'),
                           _earlier_runs(item))
                for item in root['notes']]

    def snapshot(self):
        """The database's current snapshot id: it moves when a sync merges others' work in."""
        try:
            database = self.view.file.database
            current = database.current_snapshot if database is not None else None
            return current.id if current is not None else None
        except Exception:
            return None

    def changed_elsewhere(self) -> bool:
        """Whether the stored notes differ from what this storage last read or wrote."""
        try:
            return self._baseline is not None and self._read_root() != self._baseline
        except NoteStorageError:
            return False

    def save(self, notes: list[StoredNote], *, mcp_allow=_UNCHANGED,
             folders=_UNCHANGED, merge=True) -> None:
        """Write the collection. If the stored notes changed since this storage last read or
        wrote them (someone else's work arrived), the two are merged three ways and the
        result written; `merged` then holds it for the notebook to adopt. With `merge` false,
        such a save writes nothing and sets `pending`, for a merging save to follow."""
        self.merged = None
        self.pending = False
        try:
            stored = self._read_root()
            elsewhere = self._baseline is not None and stored != self._baseline
            if elsewhere and not merge:
                self.pending = True
                return
            ours = self._compose(dict(self._baseline if elsewhere else stored), notes,
                                 mcp_allow, folders)
            root = ours
            if elsewhere:
                from .merge import merge_roots
                root = merge_roots(self._baseline, ours, stored, labels=('yours', 'synced'))
                if root is None:
                    raise NoteStorageError('Could not merge the notes changed elsewhere.')
            if _without_revision(root) != _without_revision(stored):
                # A new revision on every write: when two people change the notes at once,
                # Binary Ninja's merge then always hands the whole collection to ours (see
                # src/merge.py) instead of combining their fields itself, which would pair one
                # side's text with the other's per-line authors.
                root = dict(root, revision=uuid4().hex)
                # Persistent user metadata also marks analysis changed in Binary Ninja.
                self.view.store_metadata(METADATA_KEY, root)
            else:
                root = stored
            self._baseline = root
            self.mcp_allow = root.get('mcp-allow')
            if elsewhere:
                self.folders = _folders(root.get('folders'))
                self.merged = (self._notes(root), list(self.folders))
            elif folders is not _UNCHANGED:
                self.folders = list(folders)
        except NoteStorageError:
            raise
        except Exception as error:
            raise NoteStorageError(f"Could not write note metadata: {error}") from error

    def _compose(self, root, notes, mcp_allow, folders):
        """`root` with this collection written into it, keeping fields other clients added."""
        previous = {item['id']: item for item in root['notes']}
        items = []
        for note in notes:
            if note.text == '':
                continue
            item = dict(previous.get(note.id, {}), id=note.id, text=note.text)
            if note.mcp_allow is not None:
                item['mcp-allow'] = note.mcp_allow
            else:
                item.pop('mcp-allow', None)
            if note.edited is not None:
                item['edited'] = note.edited
            if note.folder is not None:
                item['folder'] = note.folder
            else:
                item.pop('folder', None)
            for key, runs in (('agents', note.agents), ('users', note.users)):
                if runs:
                    item[key] = [{'line': line, 'count': count, 'name': name}
                                 for line, count, name in runs]
                else:
                    item.pop(key, None)
            if note.earlier:
                item['earlier'] = [_earlier_item(*run) for run in note.earlier]
                item['earlier-text'] = text_digest(note.text)
            else:
                item.pop('earlier', None)
                item.pop('earlier-text', None)
            _optional_bool(item)
            items.append(item)
        root['notes'] = items
        if mcp_allow is not _UNCHANGED:
            if mcp_allow is None:
                root.pop('mcp-allow', None)
            else:
                root['mcp-allow'] = mcp_allow
        if folders is not _UNCHANGED:
            # Only written once there is a folder, so older roots stay as they were.
            if folders:
                root['folders'] = [
                    dict({'id': folder.id, 'name': folder.name},
                         **({'parent': folder.parent} if folder.parent else {}))
                    for folder in folders]
            else:
                root.pop('folders', None)
        _optional_bool(root)
        return root
