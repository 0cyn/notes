from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import replace
from weakref import WeakValueDictionary

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QTextBlockFormat, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QApplication

from ..markdown import normalized_table_source, padding_insertions, utf16_length
from ..mcp import changed_span
from ..storage import (
    NoteStorage,
    NoteStorageError,
    StoredFolder,
    StoredNote,
    edited_by,
    enterprise_user,
    line_names,
    name_runs,
    parse_writers,
    person_name,
    retag,
    retag_writers,
    writers_key,
)
from .common import DOCUMENT_MARGIN, single_shot
from .highlighter import MarkdownHighlighter, parser_process
from .layout import PixelDocumentLayout
from .preferences import editor_preferences
from .tables import TableRegistry

# Per-line attribution, as block format properties: they move with their lines and undo
# restores them, so the document is the truth while a note is open. AUTHOR is an agent's name
# (none: a person); USER is the Enterprise user who wrote the line, person or agent. Those are
# its latest writer; EARLIER (a `writers_key`) lists who edited it before, oldest first.
AUTHOR = QTextFormat.Property.UserProperty + 20
USER = QTextFormat.Property.UserProperty + 21
EARLIER = QTextFormat.Property.UserProperty + 22
ATTRIBUTES = (AUTHOR, USER, EARLIER)
# How often an open notebook looks for notes a sync merged into its file.
SYNC_CHECK_MS = 2000


def block_author(block):
    return block.blockFormat().property(AUTHOR) or None


def block_writer(block):
    """The (agent, user) who last edited a line."""
    fmt = block.blockFormat()
    return fmt.property(AUTHOR) or None, fmt.property(USER) or None


def block_writers(block):
    """Everyone whose text is in a line, as (agent, user), oldest first; the last wrote last."""
    return (*parse_writers(block.blockFormat().property(EARLIER) or None), block_writer(block))


def writer_label(agent, user):
    """`cynder's Claude`, `Claude`, the Enterprise user, or `person_name()`."""
    if agent:
        return f"{user}'s {agent}" if user else agent
    return user or person_name()


def note_people(document):
    """The people who wrote lines of `document`, in the order they take colors: the local
    person first (lines with no Enterprise user are theirs too), then the rest by name."""
    names = set()
    for fmt in document.allFormats():
        if not fmt.isBlockFormat():
            continue
        if not fmt.property(AUTHOR):
            names.add(fmt.property(USER) or None)
        names.update(user for agent, user in parse_writers(fmt.property(EARLIER) or None)
                     if not agent)
    local = enterprise_user()
    return (local, *sorted(name for name in names if name not in (None, local)))


def block_label(block):
    """Who last wrote a line."""
    return writer_label(*block_writer(block))


def recency(note, recent):
    """Seconds since the epoch of a note's latest activity; `recent` maps id to open time."""
    return max(recent.get(note.id, 0), note.edited or 0)


def recent_first(notes, recent):
    """Most recent first; ties keep their order."""
    return sorted(notes, key=lambda note: -recency(note, recent))


def now() -> int:
    """The time stamped on an edited note (tests replace it)."""
    return int(time.time())


class NoteIndex:
    """A note's link/heading analysis, for one exact source.

    Closed notes are indexed in the parser process; `analysis` is None while that is pending.
    """

    def __init__(self, source, analysis=None):
        self.source, self.analysis = source, analysis

    def ready_for(self, source):
        return self.source == source and self.analysis is not None

    def pending_for(self, source):
        return self.source == source and self.analysis is None


class NoteSession(QObject):
    """One source document, parser, and native undo stack per note."""

    statusChanged = Signal()
    fontChanged = Signal()

    def __init__(self, notebook, stored: StoredNote):
        super().__init__(notebook)
        self.notebook = notebook
        self.id = stored.id
        self.mcp_allow = stored.mcp_allow
        self.edited = stored.edited
        self.folder = stored.folder
        # Attribution runs for storage; derived from the document once it exists.
        self._runs = {AUTHOR: stored.agents, USER: stored.users, EARLIER: stored.earlier}
        self._author = None  # Who the current edit is by: an agent name, or None for a person.
        self._touched = []  # (start, end, removed, (agent, user)) of edits not yet attributed.
        self._new_command = False
        self._undo_steps = 0
        self._tagging = False
        self._attribution = single_shot(self, self.attribute_edits)
        self._views = set()
        self._heading_title = ""
        notebook.statusChanged.connect(self.statusChanged.emit)
        self.source_revision = 0
        # The undo run: (token, revision) of the last edit; same token, same undo step.
        self._run = None
        self._normalizing = False
        self._source_edit_depth = 0
        # QTextDocument's UTF-16 paragraph coordinates, without a document.
        self._source = (stored.text.replace('\r\n', '\n').replace('\r', '\n')
                        .replace('\u2028', '\n').replace('\u2029', '\n'))
        self._document = None
        self._layout = None
        self._highlighter = None
        self._tables = None
        self._index = None  # A NoteIndex, or None before the first request.
        self._preferences = notebook.preferences
        # A cheap provisional title; parsing belongs to the note's preparation.
        first = self._source.split('\n', 1)[0]
        match = re.fullmatch(r' {0,3}#(?:[ \t]+(.*))?', first)
        if match:
            self._heading_title = re.sub(r'[ \t]+#+[ \t]*$', '', match[1] or '').strip()
        self._preferences.changed.connect(self._apply_preference)

    @property
    def materialized(self):
        return self._document is not None

    @property
    def document(self):
        self._materialize()
        return self._document

    @property
    def layout(self):
        self._materialize()
        return self._layout

    @property
    def highlighter(self):
        self._materialize()
        return self._highlighter

    @property
    def tables(self):
        self._materialize()
        return self._tables

    def _materialize(self):
        if self._document is not None:
            return

        self._document = QTextDocument(self)
        self._layout = PixelDocumentLayout(self.document)
        self._layout.progressive = bool(self._source)
        self.document.setDocumentLayout(self.layout)
        self.document.setDefaultFont(self._preferences.font())
        self.document.setDocumentMargin(DOCUMENT_MARGIN)
        self.document.setPlainText(self._source)
        self._source = self.document.toPlainText()
        # Qt represents newlines internally; don't dirty the file merely on opening.
        self._highlighter = MarkdownHighlighter(self.document, QApplication.palette(),
                                                deferred=bool(self._source), normalize_initial=True,
                                                prepared=self._prepared_index())
        self.highlighter.initialTextPrepared.connect(self._normalize_initial_text)
        normalized = (normalized_table_source(self._source, self.highlighter._parser)
                      if self.highlighter._parser is not None else self._source)
        if normalized != self._source:
            self.document.setPlainText(normalized)
            self.highlighter.refresh()
        self._reset_text(self.document.toPlainText())
        self.document.setModified(False)
        self.document.undoCommandAdded.connect(self._command_added)
        self.document.contentsChange.connect(self._record_change)
        self.document.contentsChanged.connect(self._content_changed)
        self.highlighter.analysisChanged.connect(self.layout.set_analysis)
        self.layout.set_analysis(self.highlighter.analysis)
        self._tables = TableRegistry(self)
        self.highlighter.analysisChanged.connect(self._tables_changed)
        self._tables_changed(self.highlighter.analysis)
        self.highlighter.analysisChanged.connect(self._title_changed)
        self.highlighter.analysisChanged.connect(self._index_from_document)
        if self.highlighter.analysis_is_current:
            self._title_changed(self.highlighter.analysis)

    def _prepared_index(self):
        """The index as a highlighter's first analysis; tables still need the real parse."""
        index = self._index
        ready = index is not None and index.ready_for(self._source)
        return index.analysis if ready and not index.analysis.tables else None

    @property
    def indexed(self):
        return self._index is not None and self._index.ready_for(self.source)

    def request_index(self):
        source = self.source
        if self._index is not None and self._index.source == source:
            return  # Ready, or already pending.
        self._index = NoteIndex(source)
        if self.materialized:
            self.highlighter.refresh()
            self._index_from_document(self.highlighter.analysis)
        else:
            parser_process().submit(self, source)

    def _index_from_document(self, analysis):
        if self.highlighter.analysis_is_current:
            self._index = NoteIndex(self.source, analysis)
            self.notebook.indexReady.emit(self)

    def preparation_finished(self, source, future):
        if source == self.source:
            analysis = future.result().analysis
            self._index = NoteIndex(source, analysis)
            self._title_changed(analysis)
        elif self._index is not None and self._index.pending_for(source):
            self._index = None  # Stale: a later request starts over.
        self.notebook.indexReady.emit(self)

    def navigation_analysis(self):
        """The current analysis for navigation, or None (and an index request) if not ready."""
        if self.materialized:
            self.highlighter.refresh()
            if self.highlighter.analysis_is_current:
                return self.highlighter.analysis
        if self.indexed:
            return self._index.analysis
        self.request_index()
        return None

    def _normalize_initial_text(self, expected, normalized):
        if self.source_revision or self.document.toPlainText() != expected:
            return
        self._normalizing = True
        try:
            self.document.setPlainText(normalized)
            self._reset_text(normalized)
            self.document.setModified(False)
        finally:
            self._normalizing = False

    # Authorship. Edits are attributed as they land; undo and redo restore it.

    def _reset_text(self, source):
        """Adopt a document set wholesale (outside the undo history) and re-apply its authors."""
        self._runs = {key: retag(self._source, runs, source, None)
                      for key, runs in self._runs.items()}
        self._source = source
        self._touched.clear()
        self._new_command = False
        if not any(self._runs.values()):
            return
        self._tagging = True
        self._document.setUndoRedoEnabled(False)  # Also clears the (new) undo history.
        try:
            for key, runs in self._runs.items():
                for line, count, name in runs:
                    block = self._document.findBlockByNumber(line)
                    for _ in range(count):
                        self._set_block(block, {key: name})
                        block = block.next()
        finally:
            self._document.setUndoRedoEnabled(True)
            self._tagging = False
            self._undo_steps = self._document.availableUndoSteps()

    @staticmethod
    def _differs(block, values):
        return block.isValid() and any(
            (block.blockFormat().property(key) or None) != value for key, value in values.items())

    @classmethod
    def _set_block(cls, block, values):
        if not cls._differs(block, values):
            return
        fmt = QTextBlockFormat(block.blockFormat())
        for key, value in values.items():
            if value is None:
                fmt.clearProperty(key)
            else:
                fmt.setProperty(key, value)
        QTextCursor(block).setBlockFormat(fmt)

    @contextmanager
    def authoring(self, name):
        """Attribute edits made inside the block to agent `name` (None: a person)."""
        previous, self._author = self._author, name
        try:
            yield
        finally:
            self._author = previous

    def _command_added(self):
        self._new_command = True

    def _record_change(self, position, removed, added):
        # A new undo command, or typing merged into the last one, is an edit to attribute;
        # anything else that changes the undo count is undo or redo.
        steps = self._document.availableUndoSteps()
        edit = self._new_command or steps == self._undo_steps and removed != added
        if not self._new_command and steps != self._undo_steps:
            self._touched.clear()  # Undone or redone before it was attributed.
        self._new_command, self._undo_steps = False, steps
        if not edit or self._tagging:
            return
        start = QTextCursor(self._document)
        start.setPosition(position)
        start.setKeepPositionOnInsert(True)
        end = QTextCursor(self._document)
        end.setPosition(min(position + added, self._document.characterCount() - 1))
        self._touched.append((start, end, removed > 0, (self._author, enterprise_user())))
        # Not now: Qt may not have laid the edit out yet, and must not be edited mid-signal.
        self._attribution.start()

    def attribute_edits(self):
        """Mark the lines recorded edits changed, joined to the edit's own undo step."""
        self._attribution.stop()
        if self._document is None:
            return
        touched, self._touched = self._touched, []
        blocks = []
        for start, end, removed, writer in touched:
            first = self._document.findBlock(start.position())
            last = self._document.findBlock(end.position())
            inserted = not removed and end.position() > start.position()
            if (inserted and end.position() - start.position() == 1
                    and self._document.characterAt(start.position()) == '\u2029'):
                # Only a line break: splitting a line writes nothing, so both halves keep their
                # writers. A new empty line is someone's once they type in it.
                continue
            if inserted and self._document.characterAt(start.position()) == '\u2029':
                first = first.next()  # A new line after this one, not a change to it.
            if inserted and last != first and end.position() == last.position():
                last = last.previous()  # Inserted lines ended with a newline before this one.
            block = first
            while block.isValid() and block.blockNumber() <= last.blockNumber():
                values = self._edited_values(block, writer, start.position(), end.position())
                if self._differs(block, values):
                    blocks.append((block, values))
                block = block.next()
        if blocks:
            self._mark(blocks, join=True)
            self._persist_authors()

    @staticmethod
    def _edited_values(block, writer, start, end):
        """A line's attribution after `writer` edits `start..end`: a line the edit wrote
        whole is theirs alone; one they changed keeps everyone whose text it still holds."""
        values = {AUTHOR: writer[0], USER: writer[1], EARLIER: None}
        position = block.position()
        if start <= position and position + len(block.text()) <= end:
            return values
        values[EARLIER] = writers_key(edited_by(
            parse_writers(block.blockFormat().property(EARLIER) or None), block_writer(block),
            writer))
        return values

    def _mark(self, blocks, *, join=False):
        cursor = QTextCursor(self._document)
        self._tagging = True
        try:
            if join:
                cursor.joinPreviousEditBlock()
            else:
                cursor.beginEditBlock()
            for block, values in blocks:
                self._set_block(block, values)
            cursor.endEditBlock()
        finally:
            self._tagging = False
            self._new_command, self._undo_steps = False, self._document.availableUndoSteps()

    def set_author(self, lines, name):
        """Attribute source `lines` to agent `name` (None: a person), as one undo step."""
        self._mark([(self.document.findBlockByNumber(line), {AUTHOR: name, EARLIER: None})
                    for line in lines])
        self._content_changed()

    def authors(self, lines):
        """The set of authors of source `lines`."""
        return {block_author(self.document.findBlockByNumber(line)) for line in lines}

    @property
    def agent_names(self):
        return sorted({name for _line, _count, name in self.agents})

    @property
    def agents(self):
        """(line, count, name) runs of agent-written lines."""
        return self._current_runs()[AUTHOR]

    @property
    def users(self):
        """(line, count, username) runs of the Enterprise users who wrote lines."""
        return self._current_runs()[USER]

    @property
    def earlier(self):
        """(line, count, writers_key) runs of who edited lines before their last writer."""
        return self._current_runs()[EARLIER]

    def _current_runs(self):
        if self._document is None:
            return self._runs
        formats = list(enumerate(self._document.allFormats()))
        result = {}
        for key in ATTRIBUTES:
            names = {index: fmt.property(key) for index, fmt in formats
                     if fmt.isBlockFormat() and fmt.property(key)}
            lines, block = [], self._document.begin() if names else None
            while block is not None and block.isValid():
                lines.append(names.get(block.blockFormatIndex()))
                block = block.next()
            result[key] = name_runs(lines)
        return result

    def _apply_preference(self, name):
        if not self.materialized:
            return
        if name == 'hlil_show_addresses':
            self.tables.refresh_metrics()
            return
        if name != 'font_size':
            return
        font = self._preferences.font()
        if font == self.document.defaultFont():
            return
        modified = self.document.isModified()
        self.document.setDefaultFont(font)
        self.highlighter.refresh_formats()
        self.tables.refresh_metrics()
        self.fontChanged.emit()
        self.document.setModified(modified)

    @property
    def storage(self):
        return self.notebook.storage

    @property
    def error(self):
        return self.notebook.error

    @property
    def load_failed(self):
        return self.notebook.load_failed

    @property
    def title(self):
        if self._heading_title:
            return self._heading_title
        return f"Note {self.notebook.notes.index(self) + 1}"

    def _title_changed(self, analysis):
        title = next((heading.title for heading in analysis.headings if heading.level == 1), "")
        if title != self._heading_title:
            self._heading_title = title
            if self in self.notebook.notes:
                self.notebook.noteChanged.emit(self)

    def acquire(self, token):
        """An open editor keeps the note (and its notebook) alive."""
        self._views.add(token)
        self.notebook.acquire(token)

    def release(self, token):
        self._views.discard(token)
        if not self._views and self.source == "" and not self.load_failed:
            self.notebook.discard_empty(self)
        self.notebook.release(token)

    def _tables_changed(self, analysis):
        self.tables.update(analysis.tables)

    def apply_source_edit(self, start, end, text, *, merge_token=None):
        if self.load_failed:
            return
        cursor = QTextCursor(self.document)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        with self.source_edit(start, merge_token=merge_token):
            cursor.insertText(text)
        return cursor

    def replace_text(self, start, end, text, *, author=None):
        """Replace `source[start:end]` (str indices) as one undo step, in every open editor."""
        if self.materialized:
            before = self._source
            with self.authoring(author):
                self.apply_source_edit(utf16_length(before[:start]), utf16_length(before[:end]),
                                       text)
            self.attribute_edits()
            return
        source = self._source[:start] + text + self._source[end:]
        runs = retag_writers(self._source, tuple(self._runs[key] for key in ATTRIBUTES), source,
                             (author, enterprise_user()))
        self._runs = dict(zip(ATTRIBUTES, runs, strict=True))
        self._source = source
        self.source_revision += 1
        self.edited = now()
        self.persist()
        self.request_index()
        self.notebook.noteChanged.emit(self)

    @contextmanager
    def source_edit(self, position, *, merge_token=None):
        """User input and paragraph maintenance in one undo step, opened at `position`."""
        if self._source_edit_depth:
            yield
            return
        cursor = QTextCursor(self.document)
        cursor.setPosition(position)
        if self.continues_run(merge_token):
            cursor.joinPreviousEditBlock()
        else:
            cursor.beginEditBlock()
        self._source_edit_depth += 1
        try:
            yield
        finally:
            cursor.endEditBlock()
            self._source_edit_depth -= 1
            self._run = (merge_token, self.source_revision) if merge_token is not None else None

    def continues_run(self, token) -> bool:
        """Whether the last undo step is run `token`'s, with no other edit since."""
        return token is not None and self._run == (token, self.source_revision)

    @property
    def source(self) -> str:
        return self._source

    def _content_changed(self) -> None:
        if self._normalizing or self._tagging or self not in self.notebook.notes:
            return
        source = self.document.toPlainText()
        # QSyntaxHighlighter may emit contentsChanged for formatting-only changes.
        if source == self._source:
            self._persist_authors()
            return
        self._normalizing = True
        try:
            self._maintain_table_paragraphs()
        except Exception as error:
            # Formatting/parsing failures must never interrupt note persistence.
            self.highlighter.warning = f"Table boundary formatting unavailable: {error}"
        finally:
            source = self.document.toPlainText()
            self._normalizing = False
        if source == self._source:
            self._run = None
            self._persist_authors()
            return
        self._runs = self._current_runs()
        self._source = source
        self.source_revision += 1
        self.edited = now()
        self.persist()
        self.notebook.noteChanged.emit(self)

    def _maintain_table_paragraphs(self):
        parser = self.highlighter._parser
        if parser is None or "|" not in self.document.toPlainText():
            return
        cursor = QTextCursor(self.document)
        joined = False

        def apply(edits):
            nonlocal joined
            if edits and not joined:
                cursor.joinPreviousEditBlock()
                joined = True
            for position, text in edits:
                cursor.setPosition(position)
                cursor.insertText(text)

        try:
            # Repair table boundaries first, or prose after a deleted separator becomes rows.
            models = self.tables.models if hasattr(self, "tables") else ()
            for model in models:
                if not model.alive or model.start >= model.end:
                    continue
                table = replace(model.table, start_position=model.start, end_position=model.end)
                edits = padding_insertions(self.document.toPlainText(), [table])
                if not edits:
                    continue
                fragment = model.source_markdown()
                parsed = parser.parse(fragment).tables
                if (len(parsed) == 1 and parsed[0].start_position == 0
                        and parsed[0].end_position == utf16_length(fragment)):
                    apply(edits)
            source = self.document.toPlainText()
            apply(padding_insertions(source, parser.parse(source).tables))
        finally:
            if joined:
                cursor.endEditBlock()

    def adopt(self, stored: StoredNote) -> bool:
        """Take this note as merged with someone else's work (an Enterprise sync): its text,
        authors and fields, outside the undo history, writing nothing. Returns whether
        anything changed."""
        changed = False
        for name, value in (('mcp_allow', stored.mcp_allow), ('edited', stored.edited),
                            ('folder', stored.folder)):
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        text = (stored.text.replace('\r\n', '\n').replace('\r', '\n')
                .replace('\u2028', '\n').replace('\u2029', '\n'))
        runs = {AUTHOR: stored.agents, USER: stored.users, EARLIER: stored.earlier}
        if text == self._source and runs == self._current_runs():
            return changed
        if not self.materialized:
            self._source, self._runs = text, runs
            first = text.split('\n', 1)[0]
            match = re.fullmatch(r' {0,3}#(?:[ \t]+(.*))?', first)
            self._heading_title = (re.sub(r'[ \t]+#+[ \t]*$', '', match[1] or '').strip()
                                   if match else '')
        else:
            self._attribution.stop()
            self._touched.clear()
            self._tagging = True
            # The history can't undo past someone else's work; it starts again here.
            self._document.setUndoRedoEnabled(False)
            try:
                if text != self._source:
                    start, end, replacement = changed_span(self._source, text)
                    cursor = QTextCursor(self._document)
                    cursor.setPosition(utf16_length(self._source[:start]))
                    cursor.setPosition(utf16_length(self._source[:end]),
                                       QTextCursor.MoveMode.KeepAnchor)
                    cursor.insertText(replacement)
                    self._source = self._document.toPlainText()
                    # As after any edit: every table keeps its blank lines before and after
                    # (a conflict marker can land right against one).
                    try:
                        self._maintain_table_paragraphs()
                    except Exception as error:
                        self.highlighter.warning = (
                            f"Table boundary formatting unavailable: {error}")
                    normalized = self._document.toPlainText()
                    if normalized != self._source:
                        runs = {key: retag(self._source, value, normalized, None)
                                for key, value in runs.items()}
                        self._source = normalized
                        self.notebook.write_soon()  # Store the separators too.
                lines = self._source.count('\n') + 1
                names = {key: line_names(self._source, value) for key, value in runs.items()}
                block = self._document.begin()
                for line in range(lines):
                    self._set_block(block, {key: names[key][line] for key in ATTRIBUTES})
                    block = block.next()
            finally:
                self._document.setUndoRedoEnabled(True)
                self._tagging = False
                self._new_command = False
                self._undo_steps = self._document.availableUndoSteps()
            self._runs = self._current_runs()
        self.source_revision += 1
        self.request_index()
        return True

    def _persist_authors(self):
        runs = self._current_runs()
        if runs != self._runs:
            self._runs = runs
            self.persist()
            self.notebook.noteChanged.emit(self)

    def persist(self) -> None:
        # Called while the document reports its own change: merging in others' work here
        # would edit the document inside that report, which Qt's layout doesn't survive.
        self.notebook.persist(merge=False)

    def stored(self) -> StoredNote:
        return StoredNote(self.id, self.source, self.mcp_allow, self.edited, self.folder,
                          self._runs[AUTHOR], self._runs[USER], self._runs[EARLIER])


class NotebookSession(QObject):
    """The ordered note collection shared by all surfaces of an open file."""

    notesChanged = Signal()
    noteChanged = Signal(object)
    lastNoteDeleted = Signal()
    statusChanged = Signal()
    indexReady = Signal(object)
    foldersChanged = Signal()

    def __init__(self, storage: NoteStorage):
        # Worker threads can trigger cyclic GC; Qt objects must die on the GUI thread.
        super().__init__(QApplication.instance())
        self._leases = set()
        self._release_timer = single_shot(self, self._release_if_unused)
        self.storage = storage
        self.preferences = editor_preferences(storage.data, self)
        self.error = ""
        self.load_failed = False
        self.notes: list[NoteSession] = []
        self.folders: list[StoredFolder] = []
        try:
            stored = storage.load()
            self.folders = list(storage.folders)
        except NoteStorageError as error:
            self.error = str(error)
            self.load_failed = True
            stored = [StoredNote.create()]
        self.notes.extend(NoteSession(self, note) for note in stored)
        self.adopted = False  # Whether the last save merged in work from elsewhere.
        # An Enterprise sync can merge others' notes into the file: look when the
        # database's snapshot moves, and merge them in rather than writing over them.
        self._snapshot = storage.snapshot()
        self._sync_check = QTimer(self)
        self._sync_check.setInterval(SYNC_CHECK_MS)
        self._sync_check.timeout.connect(self.check_elsewhere)
        # A save that found others' work without merging it: merge on the next turn.
        self._reconcile = single_shot(self, self.persist)
        if not self.load_failed:
            self._sync_check.start()
        self._index_queue = list(self.notes)
        self._index_timer = single_shot(self, self._index_next)
        self.indexReady.connect(self._queue_index)
        self._index_timer.start(25)

    def _queue_index(self, *_args):
        if self._index_queue:
            self._index_timer.start(25)

    def _index_next(self):
        while self._index_queue:
            note = self._index_queue.pop(0)
            if note in self.notes and not note.indexed:
                note.request_index()
                return

    def acquire(self, token):
        self._release_timer.stop()
        self._leases.add(token)

    def release(self, token):
        self._leases.discard(token)
        if not self._leases:
            self._release_timer.start(0)

    def _release_if_unused(self):
        if not self._leases:
            key = self.storage.session_id
            if _sessions.get(key) is self:
                _sessions.pop(key)
            self.deleteLater()

    def default_note(self):
        if not self.notes:
            return self.new_note()
        return self.notes[0]

    def new_note(self, text=""):
        return self.add_note(StoredNote.create(
            text, mcp_allow=self.preferences.get('mcp_default_allow'),
            edited=now() if text != "" else None,
        ))

    def add_note(self, stored: StoredNote):
        if self.load_failed:
            return None
        note = NoteSession(self, stored)
        self.notes.append(note)
        if stored.text != "":
            self.persist()
        self.notesChanged.emit()
        return note

    def note(self, identity):
        return next((note for note in self.notes if note.id == identity), None)

    def discard_empty(self, note):
        if note in self.notes and note.source == "" and not note._views:
            self.notes.remove(note)
            self.statusChanged.disconnect(note.statusChanged.emit)
            self.persist()
            self.notesChanged.emit()

    def delete_note(self, note) -> bool:
        if self.load_failed or note not in self.notes:
            return False
        remaining = [item for item in self.notes if item is not note]
        # Keep the note and every open editor available if the metadata write fails.
        if not self._save_notes(remaining):
            self.statusChanged.emit()
            return False
        if self.adopted:
            # Merged with work from elsewhere, which may have kept the note (if someone
            # edited it meanwhile); the collection is already as stored.
            return note not in self.notes
        self.notes.remove(note)
        self.statusChanged.disconnect(note.statusChanged.emit)
        self.notesChanged.emit()
        if not self.notes:
            self.lastNoteDeleted.emit()
        self.statusChanged.emit()
        return True

    # Folders. A note's `folder` is a folder id; an unknown id reads as unfiled.

    def folder(self, identity):
        return next((folder for folder in self.folders if folder.id == identity), None)

    def note_folder(self, note):
        return self.folder(note.folder) if note.folder is not None else None

    def child_folders(self, parent=None):
        return sorted((folder for folder in self.folders if folder.parent == parent),
                      key=lambda folder: (folder.name.casefold(), folder.id))

    def folder_notes(self, folder=None):
        """The saved notes directly in `folder` (None: unfiled)."""
        identity = folder.id if folder is not None else None
        return [note for note in self.saved_notes
                if (note.folder if self.note_folder(note) is not None else None) == identity]

    def folder_path(self, folder):
        names = []
        while folder is not None:
            names.append(folder.name)
            folder = self.folder(folder.parent)
        return ' / '.join(reversed(names))

    def _contains(self, folder, other):
        """Whether `other` is `folder` or inside it."""
        while other is not None:
            if other.id == folder.id:
                return True
            other = self.folder(other.parent)
        return False

    def _change_folders(self, folders, notes=None):
        """Persist a folder change; on a failed write, nothing changes."""
        if self.load_failed:
            return False
        previous = [(note, note.folder) for note in self.notes]
        for note, folder in (notes or {}).items():
            note.folder = folder
        if not self._save_notes(self.notes, folders=folders):
            for note, folder in previous:
                note.folder = folder
            self.statusChanged.emit()
            return False
        if self.adopted:
            return True  # Already as stored, merged with work from elsewhere.
        self.folders = list(folders)
        self.foldersChanged.emit()
        for note in notes or ():
            self.noteChanged.emit(note)
        self.statusChanged.emit()
        return True

    def new_folder(self, name, parent=None):
        name = name.strip()
        if not name or parent is not None and parent not in self.folders:
            return None
        folder = StoredFolder.create(name, parent.id if parent is not None else None)
        return folder if self._change_folders(self.folders + [folder]) else None

    def rename_folder(self, folder, name):
        name = name.strip()
        if not name or folder not in self.folders:
            return False
        renamed = StoredFolder(folder.id, name, folder.parent)
        return self._change_folders([renamed if item is folder else item for item in self.folders])

    def delete_folder(self, folder):
        """Remove a folder; its notes and folders move up into its parent."""
        if folder not in self.folders:
            return False
        folders = [StoredFolder(item.id, item.name, folder.parent)
                   if item.parent == folder.id else item
                   for item in self.folders if item is not folder]
        notes = {note: folder.parent for note in self.notes if note.folder == folder.id}
        return self._change_folders(folders, notes)

    def move_note(self, note, folder=None):
        if note not in self.notes or folder is not None and folder not in self.folders:
            return False
        if note.folder == (folder.id if folder is not None else None):
            return True
        return self._change_folders(self.folders, {note: folder.id if folder else None})

    def move_folder(self, folder, parent=None):
        """Nest `folder` in `parent` (None: top level); never inside itself."""
        if (folder not in self.folders or parent is not None
                and (parent not in self.folders or self._contains(folder, parent))):
            return False
        if folder.parent == (parent.id if parent is not None else None):
            return True
        moved = StoredFolder(folder.id, folder.name, parent.id if parent is not None else None)
        return self._change_folders([moved if item is folder else item for item in self.folders])

    def move_items(self, notes=(), folders=(), parent=None):
        """Move notes and folders into `parent` (None: top level) in one write.

        Items inside a folder that is also moving go with it, and a folder never moves into
        itself; such a request moves nothing.
        """
        if self.load_failed or parent is not None and parent not in self.folders:
            return False
        folders = [folder for folder in folders if folder in self.folders]
        if any(parent is not None and self._contains(folder, parent) for folder in folders):
            return False

        def carried(owner):
            return owner is not None and any(self._contains(folder, owner) for folder in folders)

        target = parent.id if parent is not None else None
        moved = {folder.id for folder in folders
                 if not carried(self.folder(folder.parent)) and folder.parent != target}
        notes = {note: target for note in notes
                 if note in self.notes and note.folder != target
                 and not carried(self.note_folder(note))}
        if not moved and not notes:
            return True
        return self._change_folders(
            [StoredFolder(item.id, item.name, target) if item.id in moved else item
             for item in self.folders], notes)

    def unique_folder_name(self, name, parent=None):
        """`name`, or `name 2`, `name 3`… if a folder beside it already has that name."""
        taken = {folder.name.casefold()
                 for folder in self.child_folders(parent.id if parent is not None else None)}
        candidate, number = name, 1
        while candidate.casefold() in taken:
            number += 1
            candidate = f'{name} {number}'
        return candidate

    @property
    def saved_notes(self):
        # Exactly empty is the only disposable case: whitespace and headings count.
        return [note for note in self.notes if note.source != ""]

    def set_mcp_allow(self, value, note=None):
        """Persist a metadata flag only; binjad interprets the permission policy."""
        if type(value) is not bool:
            raise ValueError('mcp-allow must be a boolean.')
        if self.load_failed or note is not None and note not in self.saved_notes:
            return False
        if note is None:
            saved = self._save_notes(self.notes, mcp_allow=value)
        else:
            previous = note.mcp_allow
            note.mcp_allow = value
            saved = self._save_notes(self.notes)
            if not saved:
                note.mcp_allow = previous
            self.noteChanged.emit(note)
        self.statusChanged.emit()
        return saved

    def _save_notes(self, notes, *, folders=None, merge=True, **metadata) -> bool:
        """Write the collection; on failure, `error` says why and nothing is changed. Work
        stored elsewhere since the last write is merged in and adopted; with `merge` false
        (inside a document's change), it is merged on the next event-loop turn instead."""
        if self.load_failed:
            return False
        self.adopted = False
        try:
            self.storage.save([note.stored() for note in notes],
                              folders=self.folders if folders is None else folders,
                              merge=merge, **metadata)
        except NoteStorageError as error:
            self.error = str(error)
            return False
        self.error = ""
        if self.storage.pending:
            self._reconcile.start()
        elif self.storage.merged is not None:
            self._adopt(*self.storage.merged)
            self.adopted = True
        return True

    def write_soon(self):
        """Persist on the next event-loop turn, merging in anything new from elsewhere."""
        self._reconcile.start()

    def check_elsewhere(self):
        """Merge in notes changed elsewhere (a sync moved the database's snapshot)."""
        snapshot = self.storage.snapshot()
        if snapshot == self._snapshot or self.load_failed:
            return False
        self._snapshot = snapshot
        if not self.storage.changed_elsewhere():
            return False
        self.persist()
        return self.adopted

    def _adopt(self, stored, folders):
        """Make the collection what was stored after a merge: the merged notes, in order,
        and folders. Open editors update in place; unsaved empty drafts are kept."""
        current = {note.id: note for note in self.notes}
        notes, changed = [], []
        for item in stored:
            note = current.pop(item.id, None)
            if note is None:
                note = NoteSession(self, item)
                changed.append(note)
            elif note.adopt(item):
                changed.append(note)
            notes.append(note)
        drafts = [note for note in current.values() if note.source == ""]
        removed = [note for note in current.values() if note.source != ""]
        for note in removed:
            self.statusChanged.disconnect(note.statusChanged.emit)
        self.notes[:] = notes + drafts
        kept = {folder: folder for folder in self.folders}
        self.folders = [kept.get(folder, folder) for folder in folders]
        self.notesChanged.emit()
        self.foldersChanged.emit()
        for note in changed:
            self.noteChanged.emit(note)
        if not self.notes:
            self.lastNoteDeleted.emit()

    def persist(self, *, merge=True) -> None:
        if self.load_failed:
            return
        self._save_notes(self.notes, merge=merge)
        self.statusChanged.emit()

class LiveNotes:
    """An open notebook as the MCP tools (`src/mcp.py`) see it: changes are ordinary edits."""

    def __init__(self, notebook):
        self.notebook = notebook

    @classmethod
    def for_view(cls, data):
        notebook = _sessions.get(data.file.session_id)
        return cls(notebook) if notebook is not None else None

    def read(self):
        if self.notebook.load_failed:
            raise NoteStorageError(self.notebook.error)
        return ([note.stored() for note in self.notebook.saved_notes], list(self.notebook.folders),
                self.notebook.storage.mcp_allow)

    def add(self, stored):
        self.notebook.add_note(stored)
        self._check()

    def set_text(self, note_id, text, author):
        note = self.notebook.note(note_id)
        note.replace_text(*changed_span(note.source, text), author=author)
        self._check()

    def move(self, note_id, folder_id):
        self._check(self.notebook.move_note(self.notebook.note(note_id),
                                            self.notebook.folder(folder_id)))

    def delete(self, note_id):
        self._check(self.notebook.delete_note(self.notebook.note(note_id)))

    def _check(self, done=True):
        if not done or self.notebook.error:
            raise NoteStorageError(self.notebook.error or 'Could not change the note.')


_sessions: WeakValueDictionary[int, NotebookSession] = WeakValueDictionary()


def notebook_for(data) -> NotebookSession:
    key = data.file.session_id
    session = _sessions.get(key)
    if session is None:
        session = NotebookSession(NoteStorage(data))
        _sessions[key] = session
    return session

