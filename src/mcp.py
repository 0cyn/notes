"""MCP tools for a file's notes, offered by Binary Ninja's MCP server in the UI and headlessly."""

import functools
import re
import threading
import time
from dataclasses import replace

from .settings import setting_title
from .storage import NoteStorage, NoteStorageError, StoredNote, enterprise_user, retag_writers

_HEADING = re.compile(r' {0,3}# +(.*?)(?:[ \t]+#+)?[ \t]*')
_FENCE = re.compile(r' {0,3}(`{3,}|~{3,})')


class NoteToolError(Exception):
    """A refused tool call, reported to the MCP client as `code` and `message`."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def normalized(text):
    """Note sources use `\\n` line endings, as the editor does."""
    return text.replace('\r\n', '\n').replace('\r', '\n').replace(' ', '\n').replace(' ', '\n')


def note_title(note, notes):
    """The note's first H1 outside code fences, else its fallback name (`Note 2`)."""
    fence = None
    for line in note.text.split('\n'):
        marker = _FENCE.match(line)
        if marker and (fence is None or marker[1][0] == fence[0] and len(marker[1]) >= len(fence)):
            fence = None if fence is not None else marker[1]
        elif fence is None and (heading := _HEADING.fullmatch(line)) and heading[1].strip():
            return heading[1].strip()
    return f'Note {notes.index(note) + 1}'


def changed_span(old, new):
    """The smallest (start, end, replacement) turning `old` into `new`."""
    start = 0
    limit = min(len(old), len(new))
    while start < limit and old[start] == new[start]:
        start += 1
    end = 0
    while end < limit - start and old[-end - 1] == new[-end - 1]:
        end += 1
    return start, len(old) - end, new[start:len(new) - end]


class StoredNotes:
    """A file's notes read and written straight from metadata, for files no notebook has open."""

    def __init__(self, data):
        self.storage = NoteStorage(data)

    def read(self):
        """(notes, folders, the view's mcp-allow flag)."""
        notes = self.storage.load()
        return notes, self.storage.folders, self.storage.mcp_allow

    def add(self, note):
        self.storage.save(self.storage.load() + [note])

    def set_text(self, note_id, text, author):
        note = next(note for note in self.storage.load() if note.id == note_id)
        agents, users, earlier = retag_writers(
            note.text, (note.agents, note.users, note.earlier), text, (author, enterprise_user()))
        self._update(note_id, text=text, edited=int(time.time()), agents=agents, users=users,
                     earlier=earlier)

    def move(self, note_id, folder_id):
        self._update(note_id, folder=folder_id)

    def delete(self, note_id):
        self.storage.save([note for note in self.storage.load() if note.id != note_id])

    def _update(self, note_id, **changes):
        self.storage.save([replace(note, **changes) if note.id == note_id else note
                           for note in self.storage.load()])


def _on_main_thread(function):
    """Run `function` on the thread owning the UI's notebooks; return or raise its outcome."""
    import binaryninja
    from binaryninja import mainthread

    if not binaryninja.core_ui_enabled() or mainthread.is_main_thread():
        return function()
    outcome = {}

    def run():
        try:
            outcome['value'] = function()
        except BaseException as error:
            outcome['error'] = error

    mainthread.execute_on_main_thread_and_wait(run)
    if 'error' in outcome:
        raise outcome['error']
    return outcome.get('value')


class NoteTools:
    """The `bnnotes_*` tools. Each works on the notes of the MCP session's BinaryView.

    MCP may see and change only notes whose `mcp-allow` flag is set, and nothing when the view's
    own flag is false. Creating and deleting also follow the Notes MCP settings. While a notebook
    has the file open, `live(data)` returns it, so changes appear (and undo) in the open editors.
    Lines a tool writes are attributed to its `editor_name` (default `AI`), shown in the editor's
    author bar.
    """

    TOOLS = {
        'list_notes': dict(read_only=True),
        'read_note': dict(read_only=True),
        'create_note': dict(),
        'edit_note': dict(destructive=True),
        'append_to_note': dict(),
        'replace_note': dict(destructive=True, idempotent=True),
        'move_note': dict(idempotent=True),
        'delete_note': dict(destructive=True, idempotent=True),
    }

    def __init__(self, settings=None, live=None):
        self.settings = settings
        self.live = live
        self._lock = threading.Lock()

    def register(self, mcp):
        for name, options in self.TOOLS.items():
            mcp.tool(f'bnnotes_{name}', **options)(self._entry(getattr(self, name), mcp))

    def _entry(self, method, mcp):
        def locked(call, arguments):
            with self._lock:
                return method(call, **arguments)

        @functools.wraps(method)
        def run(call, **arguments):
            try:
                return _on_main_thread(lambda: locked(call, arguments))
            except NoteToolError as error:
                raise mcp.ToolError(error.code, error.message) from None
            except NoteStorageError as error:
                raise mcp.ToolError('storage_error', str(error)) from None
        return run

    # Tools. Their docstrings and parameters are the schemas MCP clients see.

    def list_notes(self, call) -> dict:
        """List the notes saved with this file that MCP may access (id, title, folder, size,
        last edit time), and the note folders. `hidden_notes` counts notes the user has not
        whitelisted for MCP.
        """
        _collection, notes, folders = self._open(call)
        visible = [note for note in notes if note.mcp_allow]
        return {
            'notes': [self._summary(note, notes, folders) for note in visible],
            'folders': [{'id': folder.id, 'path': _folder_path(folders, folder.id)}
                        for folder in folders],
            'hidden_notes': len(notes) - len(visible),
        }

    def read_note(self, call, note_id: str) -> dict:
        """Read a note's Markdown source.

        :param note_id: The note's id, from bnnotes_list_notes.
        """
        _collection, notes, folders = self._open(call)
        note = _whitelisted(notes, note_id)
        return dict(self._summary(note, notes, folders), text=note.text)

    def create_note(self, call, text: str, folder_id: str | None = None,
                    editor_name: str | None = None) -> dict:
        """Create a note from Markdown source. Its first `# Heading` becomes its title. In-BV links
        such as `[label](binaryninja://?expr=main)` render as code previews.

        :param text: The note's Markdown source; must not be empty.
        :param folder_id: The folder to put it in, from bnnotes_list_notes; omit for none.
        :param editor_name: Your model or agent name (such as "Claude Opus 5"), recorded as the
            author of the lines you write. Omit it to be recorded as "AI".
        """
        if not self._setting('mcp_allow_create', call.binary_view):
            raise NoteToolError('forbidden', 'Creating notes over MCP is turned off. The user '
                                'can enable Settings → Notes → '
                                f'"{setting_title("mcp_allow_create")}".')
        collection, _notes, folders = self._open(call)
        _check_folder(folders, folder_id)
        note = StoredNote.create(_content(text), mcp_allow=True, edited=int(time.time()),
                                 folder=folder_id, author=_author(editor_name),
                                 user=enterprise_user())
        collection.add(note)
        return self._result(call, note.id)

    def edit_note(self, call, note_id: str, old_text: str, new_text: str,
                  replace_all: bool = False, editor_name: str | None = None) -> dict:
        """Replace an exact piece of a note's source with new text. `old_text` must appear exactly
        once unless `replace_all` is set; include surrounding lines to make it unique.

        :param note_id: The note's id, from bnnotes_list_notes.
        :param old_text: Text currently in the note, matched exactly (including whitespace).
        :param new_text: The text to put in its place.
        :param replace_all: Replace every occurrence of `old_text` instead of exactly one.
        :param editor_name: Your model or agent name (such as "Claude Opus 5"), recorded as the
            author of the lines you write. Omit it to be recorded as "AI".
        """
        collection, notes, _folders = self._open(call)
        note = _whitelisted(notes, note_id)
        old_text, new_text = normalized(old_text), normalized(new_text)
        occurrences = note.text.count(old_text) if old_text else 0
        if not occurrences:
            raise NoteToolError('invalid_params', 'old_text does not appear in the note.')
        if occurrences > 1 and not replace_all:
            raise NoteToolError('invalid_params', f'old_text appears {occurrences} times; include '
                                'more context to make it unique, or set replace_all.')
        return self._set_text(call, collection, note, note.text.replace(old_text, new_text),
                              editor_name)

    def append_to_note(self, call, note_id: str, text: str,
                       editor_name: str | None = None) -> dict:
        """Add Markdown to the end of a note, starting on a new line.

        :param note_id: The note's id, from bnnotes_list_notes.
        :param text: The Markdown to add.
        :param editor_name: Your model or agent name (such as "Claude Opus 5"), recorded as the
            author of the lines you write. Omit it to be recorded as "AI".
        """
        collection, notes, _folders = self._open(call)
        note = _whitelisted(notes, note_id)
        separator = '' if note.text.endswith('\n') else '\n'
        return self._set_text(call, collection, note, note.text + separator + _content(text),
                              editor_name)

    def replace_note(self, call, note_id: str, text: str,
                     editor_name: str | None = None) -> dict:
        """Replace a note's whole Markdown source.

        :param note_id: The note's id, from bnnotes_list_notes.
        :param text: The note's new Markdown source; must not be empty.
        :param editor_name: Your model or agent name (such as "Claude Opus 5"), recorded as the
            author of the lines you write. Omit it to be recorded as "AI".
        """
        collection, notes, _folders = self._open(call)
        return self._set_text(call, collection, _whitelisted(notes, note_id), _content(text),
                              editor_name)

    def move_note(self, call, note_id: str, folder_id: str | None = None) -> dict:
        """Move a note into a folder, or out of any folder.

        :param note_id: The note's id, from bnnotes_list_notes.
        :param folder_id: The destination folder, from bnnotes_list_notes; omit for no folder.
        """
        collection, notes, folders = self._open(call)
        note = _whitelisted(notes, note_id)
        _check_folder(folders, folder_id)
        if note.folder != folder_id:
            collection.move(note.id, folder_id)
        return self._result(call, note.id)

    def delete_note(self, call, note_id: str) -> dict:
        """Delete a note. This cannot be undone.

        :param note_id: The note's id, from bnnotes_list_notes.
        """
        policy = self._setting('mcp_allow_delete', call.binary_view)
        collection, notes, _folders = self._open(call)
        note = next((note for note in notes if note.id == note_id), None)
        if policy == 'No':
            raise NoteToolError('forbidden', 'Deleting notes over MCP is turned off. The user can '
                                f'change Settings → Notes → "{setting_title("mcp_allow_delete")}".')
        if note is None or policy == 'Whitelisted':
            note = _whitelisted(notes, note_id)
        collection.delete(note.id)
        return {'deleted': note.id}

    # Shared steps.

    def _setting(self, name, data):
        if self.settings is None:
            from .settings import NativeSettings
            self.settings = NativeSettings()
        return self.settings.read(name, data)[0]

    def _open(self, call):
        """(collection, notes, folders) for the call's view, once its MCP gate is checked."""
        data = call.binary_view
        collection = (self.live(data) if self.live is not None else None) or StoredNotes(data)
        notes, folders, allowed = collection.read()
        if allowed is False:
            raise NoteToolError('forbidden', 'MCP access to this file\'s notes is turned off. The '
                                'user can allow it from the Notes menu → binja\'d MCP whitelist.')
        return collection, notes, folders

    def _set_text(self, call, collection, note, text, editor_name):
        if not text:
            raise NoteToolError('invalid_params', 'A note cannot be emptied; use '
                                'bnnotes_delete_note to remove it.')
        if text != note.text:
            collection.set_text(note.id, text, _author(editor_name))
        return self._result(call, note.id)

    def _result(self, call, note_id):
        _collection, notes, folders = self._open(call)
        note = next(note for note in notes if note.id == note_id)
        return self._summary(note, notes, folders)

    @staticmethod
    def _summary(note, notes, folders):
        folder = _folder_path(folders, note.folder)
        return {'id': note.id, 'title': note_title(note, notes), 'folder': folder,
                'folder_id': note.folder if folder else None,
                'edited': note.edited, 'length': len(note.text)}


def _content(text):
    text = normalized(text)
    if not text:
        raise NoteToolError('invalid_params', 'text must not be empty.')
    return text


def _author(editor_name):
    return (editor_name or '').strip() or 'AI'


def _whitelisted(notes, note_id):
    note = next((note for note in notes if note.id == note_id), None)
    if note is None:
        raise NoteToolError('not_found', f'No note has the id {note_id!r}; see bnnotes_list_notes.')
    if not note.mcp_allow:
        raise NoteToolError('forbidden', f'Note {note_id!r} is not whitelisted for MCP. The user '
                            'can allow it from the Notes menu → binja\'d MCP whitelist.')
    return note


def _check_folder(folders, folder_id):
    if folder_id is not None and not any(folder.id == folder_id for folder in folders):
        raise NoteToolError('not_found',
                            f'No folder has the id {folder_id!r}; see bnnotes_list_notes.')


def _folder_path(folders, folder_id):
    """`Parent / Child` for a folder id, or None when the id names no folder."""
    by_id = {folder.id: folder for folder in folders}
    names, folder = [], by_id.get(folder_id)
    while folder is not None and len(names) <= len(by_id):
        names.append(folder.name)
        folder = by_id.get(folder.parent)
    return ' / '.join(reversed(names)) or None


TOOLS = NoteTools()


def register_tools():
    """Offer the tools to Binary Ninja's MCP server; versions before 6.0 have none."""
    try:
        from binaryninja import mcp
    except ImportError:
        return
    TOOLS.register(mcp)
