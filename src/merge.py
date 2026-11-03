"""Three-way merges of the notes metadata, for Binary Ninja Enterprise.

Every note lives under one metadata key, so two people editing any notes at once conflict on
the whole collection; Binary Ninja's own dialog would keep only one side. `register_merge`
offers Binary Ninja a conflict splitter that merges the collection instead: notes and folders
by id, text line by line with each line's authors, and where both sides changed the same
lines, both versions between conflict markers in the note, so the problem is visible and
fixable. An open notebook uses the same merge when the stored notes change underneath it.

Free of Qt and of Binary Ninja (except inside `register_merge`) so it runs headlessly.
"""

from __future__ import annotations

import json
from collections import Counter
from difflib import SequenceMatcher

from .storage import (
    METADATA_KEY,
    METADATA_VERSION,
    _earlier_item,
    _earlier_runs,
    _runs,
    line_names,
    name_runs,
    parse_writers,
    text_digest,
)

MARKER_FIRST = '<<<<<<<'
MARKER_SPLIT = '======='
MARKER_SECOND = '>>>>>>>'
# A note's text and the per-line records that only make sense with it.
TEXT_FIELDS = ('text', 'agents', 'users', 'earlier', 'earlier-text')
_ABSENT = object()


# Binary Ninja's typed metadata JSON, as merge conflicts carry it.

class Typed:
    """A metadata value of a type the merge doesn't interpret, carried through unchanged."""

    __slots__ = ('node',)

    def __init__(self, node):
        self.node = node

    def __eq__(self, other):
        return isinstance(other, Typed) and _canonical(self.node) == _canonical(other.node)

    def __hash__(self):
        return hash(_canonical(self.node))

    def __repr__(self):
        return f'Typed({self.node!r})'


def _canonical(node):
    return json.dumps(node, sort_keys=True)


_SCALARS = ('BooleanDataType', 'UnsignedIntegerDataType', 'SignedIntegerDataType',
            'DoubleDataType')
# Conflict strings arrive C-escaped (and `success` unescapes what it is given): \n, \t, \r,
# quotes and backslashes, and every other byte outside printable ASCII as \xHH of its UTF-8.
_UNESCAPE = {'n': 10, 't': 9, 'r': 13, '"': 34, "'": 39, '\\': 92, 'a': 7, 'b': 8, 'f': 12,
             'v': 11, '0': 0, '?': 63}
_ESCAPE = {10: '\\n', 9: '\\t', 13: '\\r', 34: '\\"', 39: "\\'", 92: '\\\\'}


def unescape(text):
    """A conflict string as stored: C escapes undone, then UTF-8 decoded."""
    data, index = bytearray(), 0
    while index < len(text):
        char = text[index]
        if char != '\\' or index + 1 == len(text):
            data += char.encode('utf-8', 'surrogatepass')
            index += 1
            continue
        code = text[index + 1]
        if code == 'x' and all(c in '0123456789abcdefABCDEF' for c in text[index + 2:index + 4]) \
                and len(text) >= index + 4:
            data.append(int(text[index + 2:index + 4], 16))
            index += 4
        elif code in _UNESCAPE:
            data.append(_UNESCAPE[code])
            index += 2
        else:  # Not an escape: keep it as it was.
            data += f'\\{code}'.encode('utf-8', 'surrogatepass')
            index += 2
    return data.decode('utf-8', 'replace')


def escape(text):
    """`unescape`'s inverse, as Binary Ninja writes conflict strings."""
    return ''.join(_ESCAPE.get(byte) or (chr(byte) if 0x20 <= byte < 0x7f else f'\\x{byte:02x}')
                   for byte in text.encode('utf-8', 'surrogatepass'))


def decode_typed(node):
    """Plain Python for a merge conflict's typed metadata node; unknown kinds stay `Typed`."""
    if not isinstance(node, dict) or 'type' not in node:
        return Typed(node)
    kind, value = node['type'], node.get('value')
    if kind == 'KeyValueDataType' and isinstance(value, dict):
        return {key: decode_typed(item) for key, item in value.items()}
    if kind == 'ArrayDataType' and isinstance(value, list):
        return [decode_typed(item) for item in value]
    if kind == 'StringDataType' and isinstance(value, str):
        return unescape(value)
    if kind in _SCALARS:
        return value
    return Typed(node)


def encode_typed(value):
    """The typed metadata node for plain Python (from `decode_typed`)."""
    if isinstance(value, Typed):
        return value.node
    if isinstance(value, bool):
        return {'type': 'BooleanDataType', 'value': value}
    if isinstance(value, int):
        return {'type': 'UnsignedIntegerDataType' if value >= 0 else 'SignedIntegerDataType',
                'value': value}
    if isinstance(value, float):
        return {'type': 'DoubleDataType', 'value': value}
    if isinstance(value, str):
        return {'type': 'StringDataType', 'value': escape(value)}
    if isinstance(value, dict):
        return {'type': 'KeyValueDataType',
                'value': {str(key): encode_typed(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        return {'type': 'ArrayDataType', 'value': [encode_typed(item) for item in value]}
    raise TypeError(f'Cannot store {type(value).__name__} in metadata')


# Lines: three-way merge of text, each line carrying its writers.

class Line:
    """One line of a note and who wrote it: compared by text alone."""

    __slots__ = ('text', 'agent', 'user', 'earlier')

    def __init__(self, text, agent=None, user=None, earlier=None):
        self.text, self.agent, self.user, self.earlier = text, agent, user, earlier

    @property
    def writers(self):
        return self.agent, self.user, self.earlier

    def __repr__(self):
        return f'Line({self.text!r}, {self.agent!r}, {self.user!r})'


def note_lines(item):
    """A stored note's lines with their writers."""
    text = item.get('text', '')
    agents = line_names(text, _runs(item, 'agents'))
    users = line_names(text, _runs(item, 'users'))
    earlier = line_names(text, _earlier_runs(dict(item, text=text)))
    return [Line(*values) for values in zip(text.split('\n'), agents, users, earlier,
                                            strict=True)]


def _edits(base, side, which):
    """(start, end, lines, which) replacements of base lines, and base index -> side line."""
    matcher = SequenceMatcher(None, [line.text for line in base], [line.text for line in side],
                              autojunk=False)
    edits, same = [], {}
    for kind, start, end, side_start, side_end in matcher.get_opcodes():
        if kind == 'equal':
            same.update(zip(range(start, end), side[side_start:side_end], strict=True))
        else:
            edits.append((start, end, side[side_start:side_end], which))
    return edits, same


def _texts(lines):
    return [line.text for line in lines]


def _related(one, other):
    """Whether two edits from different sides touch the same base lines (or are the same)."""
    start, end, lines, _which = one
    other_start, other_end, other_lines, _ = other
    if (start, end) == (other_start, other_end) and _texts(lines) == _texts(other_lines):
        return True
    if start < other_end and other_start < end:
        return True
    # An insertion strictly inside the other's replaced lines.
    return (start == end and other_start < start < other_end
            or other_start == other_end and start < other_start < end)


def _apply(keep, start, end, edits):
    """Base lines start..end (each from `keep(index)`) with one side's `edits` applied."""
    result, position = [], start
    for edit_start, edit_end, lines, _which in sorted(edits, key=lambda edit: edit[:2]):
        result += [keep(index) for index in range(position, edit_start)]
        result += lines
        position = edit_end
    return result + [keep(index) for index in range(position, end)]


def _label(lines, default):
    """Who wrote a side's changed lines, when that is one writer; else `default`."""
    writers = {(line.agent, line.user) for line in lines if line.text.strip()}
    if len(writers) == 1:
        agent, user = writers.pop()
        if agent and user:
            return f"{user}'s {agent}"
        if agent or user:
            return agent or user
    return default


def merge_lines(base, first, second, labels=('left', 'right')):
    """Three-way merge of line lists. Returns (lines, conflicts): changes to different lines
    both apply; where both sides changed the same lines differently, both versions go
    between conflict markers. Lines both sides inserted at the same place are both kept,
    the first side's first; lines unchanged in text take the newer writers."""
    first_edits, first_same = _edits(base, first, 0)
    second_edits, second_same = _edits(base, second, 1)
    edits = first_edits + second_edits
    # Groups of edits that touch each other (union-find over first x second pairs).
    parent = list(range(len(edits)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for one in range(len(first_edits)):
        for other in range(len(first_edits), len(edits)):
            if _related(edits[one], edits[other]):
                parent[find(one)] = find(other)
    groups = {}
    for index, edit in enumerate(edits):
        groups.setdefault(find(index), []).append(edit)
    ordered = sorted(groups.values(), key=lambda group: (
        min(edit[0] for edit in group), max(edit[1] for edit in group),
        min(edit[3] for edit in group)))

    def unchanged(index):
        line = base[index]
        for same in (first_same, second_same):
            other = same.get(index)
            if other is not None and other.writers != line.writers:
                return other  # Re-attributed (Author menu) on that side.
        return line

    result, conflicts, position = [], 0, 0
    for group in ordered:
        start = min(edit[0] for edit in group)
        end = max(edit[1] for edit in group)
        if start < position:  # Never happens for well-formed opcodes; never lose lines.
            start = position
        result += [unchanged(index) for index in range(position, start)]
        if len({edit[3] for edit in group}) == 1:
            result += _apply(unchanged, start, end, group)  # One side: one edit.
        else:
            ours = _apply(unchanged, start, end, [edit for edit in group if edit[3] == 0])
            theirs = _apply(unchanged, start, end, [edit for edit in group if edit[3] == 1])
            if _texts(ours) == _texts(theirs):
                result += ours
            else:
                conflicts += 1
                changed_ours = [line for edit in group if edit[3] == 0 for line in edit[2]]
                changed_theirs = [line for edit in group if edit[3] == 1 for line in edit[2]]
                result.append(Line(f'{MARKER_FIRST} {_label(changed_ours, labels[0])}'))
                result += ours
                result.append(Line(MARKER_SPLIT))
                result += theirs
                result.append(Line(f'{MARKER_SECOND} {_label(changed_theirs, labels[1])}'))
        position = max(position, end)
    result += [unchanged(index) for index in range(position, len(base))]
    return result, conflicts


def lines_item(lines):
    """A note's text and per-line writer records, as stored."""
    text = '\n'.join(line.text for line in lines)
    item = {'text': text}
    for key, names in (('agents', [line.agent for line in lines]),
                       ('users', [line.user for line in lines])):
        runs = name_runs(names)
        if runs:
            item[key] = [{'line': line, 'count': count, 'name': name}
                         for line, count, name in runs]
    earlier = name_runs([line.earlier if parse_writers(line.earlier) else None
                         for line in lines])
    if earlier:
        item['earlier'] = [_earlier_item(*run) for run in earlier]
        item['earlier-text'] = text_digest(text)
    return item


# The collection: notes and folders by id, fields three ways.

def _field(base, first, second, key):
    """Three-way merge of one field (`_ABSENT` when missing); both changed: the first's."""
    old, ours, theirs = (side.get(key, _ABSENT) for side in (base, first, second))
    if ours == theirs or theirs == old:
        return ours
    if ours == old:
        return theirs
    if key == 'edited' and type(ours) is int and type(theirs) is int:
        return max(ours, theirs)
    if key == 'mcp-allow':
        return False  # Both changed agent access differently: deny until someone decides.
    return ours


def _fields(base, first, second, skip=()):
    merged = {}
    for key in dict.fromkeys([*first, *second, *base]):
        if key in skip:
            continue
        value = _field(base, first, second, key)
        if value is not _ABSENT:
            merged[key] = value
    return merged


def _order(first_ids, second_ids):
    """The first side's order, with the second side's new ids after their predecessor."""
    order = list(first_ids)
    placed = set(order)
    for index, identity in enumerate(second_ids):
        if identity in placed:
            continue
        before = next((other for other in reversed(second_ids[:index]) if other in placed), None)
        order.insert(order.index(before) + 1 if before is not None else 0, identity)
        placed.add(identity)
    return order


def _by_id(entries):
    return {entry['id']: entry for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get('id'), str)}


def _merge_entries(base, first, second, merge_one):
    """Entries (notes or folders) by id. One side deleting what the other changed keeps the
    change; deleting what the other left alone deletes it."""
    old, ours, theirs = _by_id(base), _by_id(first), _by_id(second)
    result = {}
    for identity in _order(list(ours), list(theirs)):
        mine, yours, was = ours.get(identity), theirs.get(identity), old.get(identity)
        if mine is not None and yours is not None:
            result[identity] = merge_one(was or {}, mine, yours)
        elif was is None:
            result[identity] = mine if mine is not None else yours  # Added on one side.
        else:
            kept = mine if mine is not None else yours
            if kept != was:
                result[identity] = kept  # Changed on one side, deleted on the other.
    # Notes are never stored empty (one side may have cleared one the other left alone).
    return [entry for entry in result.values()
            if entry is not None and entry.get('text', True) != '']


def _merge_note(labels):
    def merge(base, first, second):
        merged = _fields(base, first, second, skip=TEXT_FIELDS)
        if all(first.get(key) == second.get(key) for key in TEXT_FIELDS):
            text = {key: first[key] for key in TEXT_FIELDS if key in first}
        else:
            lines, _conflicts = merge_lines(note_lines(base) if base else [Line('')],
                                            note_lines(first), note_lines(second), labels)
            text = lines_item(lines)
        if not text.get('text'):
            return None  # Merged to nothing: notes are never stored empty.
        merged.update(text)
        merged['id'] = first['id']
        return merged
    return merge


def _merge_folder(base, first, second):
    merged = _fields(base, first, second)
    merged['id'] = first['id']
    return merged


def valid_root(root):
    return (isinstance(root, dict) and root.get('version') == METADATA_VERSION
            and isinstance(root.get('notes'), list)
            and all(isinstance(note, dict) and isinstance(note.get('id'), str)
                    and isinstance(note.get('text'), str) for note in root['notes']))


def merge_roots(base, first, second, labels=('left', 'right')):
    """Three-way merge of notes metadata roots (plain Python); None if any side is unreadable,
    to leave the choice to Binary Ninja. A missing side is an empty collection."""
    empty = {'version': METADATA_VERSION, 'notes': []}
    base, first, second = (empty if side is None else side for side in (base, first, second))
    if not all(valid_root(side) for side in (base, first, second)):
        return None
    merged = _fields(base, first, second, skip=('notes', 'folders'))
    merged['version'] = METADATA_VERSION
    merged['notes'] = _merge_entries(base['notes'], first['notes'], second['notes'],
                                     _merge_note(labels))
    folders = _merge_entries(*(side.get('folders') if isinstance(side.get('folders'), list)
                               else [] for side in (base, first, second)), _merge_folder)
    if folders:
        merged['folders'] = folders
    return merged


def conflict_count(root):
    """How many conflict blocks the root's notes hold."""
    return sum(Counter(line.split(' ', 1)[0] for line in note['text'].split('\n'))[MARKER_FIRST]
               for note in root.get('notes', ()) if isinstance(note, dict))


# Binary Ninja's conflict splitter.

_registered = []


def is_notes_conflict(key, conflict_type):
    return (conflict_type == 'value_store_entry'
            and key.endswith(f'/value_store/value/{METADATA_KEY}'))


def resolve(base, first, second):
    """The typed merged root for a conflict's typed sides, or None to leave it to the user."""
    plain = [None if side is None else decode_typed(side) for side in (base, first, second)]
    merged = merge_roots(*plain)
    return None if merged is None else encode_typed(merged)


def register_merge():
    """Offer Binary Ninja the notes merge for its merge conflicts. Safe without Enterprise."""
    if _registered:
        return _registered[0]
    try:
        from binaryninja import log_warn
        from binaryninja.collaboration.merge import ConflictSplitter
    except Exception:
        return None

    class NotesMerge(ConflictSplitter):
        def get_name(self):
            return 'Notes'

        def can_split(self, key, conflict):
            return is_notes_conflict(key, conflict.type)

        def split(self, key, conflict, result):
            try:
                merged = resolve(conflict.base, conflict.first, conflict.second)
            except Exception as error:
                log_warn(f'Notes: could not merge notes ({error}); choose a side instead.')
                return None
            # The splitter API only accepts an empty dict of further conflicts.
            return {} if merged is not None and conflict.success(merged) else None

    try:
        splitter = NotesMerge()
        splitter.register()
    except Exception:
        return None
    # Binary Ninja keeps raw callbacks into it: it must live as long as the process.
    _registered.append(splitter)
    return splitter
