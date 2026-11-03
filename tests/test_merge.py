import json
import random
import time

import pytest

from src.merge import (
    MARKER_FIRST,
    MARKER_SECOND,
    MARKER_SPLIT,
    Line,
    Typed,
    conflict_count,
    decode_typed,
    encode_typed,
    is_notes_conflict,
    lines_item,
    merge_lines,
    merge_roots,
    note_lines,
    resolve,
    valid_root,
)
from src.storage import METADATA_KEY, NoteStorage, _earlier_runs, _runs, writers_key

MARKERS = (MARKER_FIRST, MARKER_SPLIT, MARKER_SECOND)


def lines(*texts, agent=None, user=None):
    return [Line(text, agent, user) for text in texts]


def texts(result):
    return [line.text for line in result]


def merged(base, first, second, **kwargs):
    result, conflicts = merge_lines(lines(*base), lines(*first), lines(*second), **kwargs)
    return texts(result), conflicts


# Line merges.

@pytest.mark.parametrize('base, first, second, expected', [
    # Each side's change to a different line: both apply.
    (['a', 'b', 'c'], ['A', 'b', 'c'], ['a', 'b', 'C'], ['A', 'b', 'C']),
    # Neighboring lines, as a paragraph edited by two people.
    (['a', 'b'], ['A', 'b'], ['a', 'B'], ['A', 'B']),
    # Appends at the same place: both kept, the first side's first.
    (['a'], ['a', 'x'], ['a', 'y'], ['a', 'x', 'y']),
    # The same change on both sides happens once.
    (['a', 'b'], ['a', 'B', 'n'], ['a', 'B', 'n'], ['a', 'B', 'n']),
    # One side deleted lines the other left alone.
    (['a', 'b', 'c', 'd'], ['a', 'd'], ['a', 'b', 'c', 'd', 'e'], ['a', 'd', 'e']),
    # An insertion at the start of the other's replaced lines comes before them.
    (['a', 'b'], ['x', 'a', 'b'], ['A', 'b'], ['x', 'A', 'b']),
])
def test_changes_to_different_lines_merge_cleanly(base, first, second, expected):
    assert merged(base, first, second) == (expected, 0)


def test_both_changing_the_same_lines_puts_both_between_markers():
    result, conflicts = merged(['# Title', 'size is checked', 'end'],
                               ['# Title', 'size is NOT checked', 'end'],
                               ['# Title', 'size is checked twice', 'end'])
    assert conflicts == 1
    assert result == ['# Title', '<<<<<<< left', 'size is NOT checked', '=======',
                      'size is checked twice', '>>>>>>> right', 'end']


def test_deleting_lines_the_other_side_edited_is_a_conflict():
    result, conflicts = merged(['a', 'b', 'c'], ['a', 'c'], ['a', 'B', 'c'])
    assert conflicts == 1
    assert result == ['a', '<<<<<<< left', '=======', 'B', '>>>>>>> right', 'c']


def test_markers_name_whoever_wrote_each_side():
    base = lines('a', 'b')
    first = [Line('a'), Line('B by alice', None, 'alice')]
    second = [Line('a'), Line('B by claude', 'Claude', 'bob')]
    result, _ = merge_lines(base, first, second)
    assert texts(result)[1] == '<<<<<<< alice'
    assert texts(result)[-1] == ">>>>>>> bob's Claude"
    # Mixed writers on one side: the side's default label.
    first = [Line('a'), Line('x', None, 'alice'), Line('y', None, 'carol')]
    result, _ = merge_lines(base, first, second, labels=('yours', 'synced'))
    assert texts(result)[1] == '<<<<<<< yours'


def test_lines_keep_their_writers_and_unchanged_lines_take_new_attribution():
    base = [Line('a', None, 'alice'), Line('b', None, 'alice'), Line('c', None, 'alice')]
    first = [Line('a', None, 'alice'), Line('B', 'Claude', 'bob'), Line('c', None, 'alice')]
    # The second side only re-attributed line c (the Author menu), without changing its text.
    second = [Line('a', None, 'alice'), Line('b', None, 'alice'), Line('c', 'Qwen', 'carol')]
    result, conflicts = merge_lines(base, first, second)
    assert conflicts == 0
    assert [(line.text, line.agent, line.user) for line in result] == [
        ('a', None, 'alice'), ('B', 'Claude', 'bob'), ('c', 'Qwen', 'carol')]


def test_stored_writers_round_trip_through_lines():
    earlier = writers_key([(None, 'alice'), ('Claude', 'bob')])
    original = [Line('one', 'Claude', 'bob', earlier), Line('two', None, 'alice'), Line('')]
    item = lines_item(original)
    assert item['text'] == 'one\ntwo\n'
    assert _runs(item, 'agents') == ((0, 1, 'Claude'),)
    assert _runs(item, 'users') == ((0, 1, 'bob'), (1, 1, 'alice'))
    assert _earlier_runs(item) == ((0, 1, earlier),)
    assert [line.writers for line in note_lines(item)] == [line.writers for line in original]


# Random stress: many merges of random edits, checked against properties that any correct
# merge has, whatever lines SequenceMatcher pairs up.

def _mutate(rng, base, alphabet):
    result = list(base)
    for _ in range(rng.randint(0, 6)):
        kind = rng.random()
        index = rng.randint(0, len(result))
        if kind < 0.35:
            result[index:index] = [rng.choice(alphabet) for _ in range(rng.randint(1, 3))]
        elif kind < 0.6 and result:
            del result[min(index, len(result) - 1):index + rng.randint(1, 3)]
        elif result:
            spot = min(index, len(result) - 1)
            result[spot] = rng.choice(alphabet)
    return result


def _well_formed(result):
    """Markers come in order, never nested, and nothing else looks like a marker."""
    state = 0
    for text in result:
        kind = next((marker for marker in MARKERS if text.startswith(marker)), None)
        if kind is None:
            continue
        expected = MARKERS[state]
        assert kind == expected, result
        state = (state + 1) % 3
    assert state == 0, result


def _resolve(result, side):
    """The merge with every conflict resolved to one side (0: first, 1: second)."""
    out, state = [], 0
    for text in result:
        if text.startswith(MARKER_FIRST):
            state = 1
        elif text == MARKER_SPLIT and state:
            state = 2
        elif text.startswith(MARKER_SECOND) and state:
            state = 0
        elif state == 0 or state == side + 1:
            out.append(text)
    return out


@pytest.mark.parametrize('alphabet', [
    [f'line {index}' for index in range(40)],  # Mostly distinct lines.
    list('abc') + [''],  # Heavy repetition, blank lines: ambiguous alignments.
])
def test_random_merges_hold_their_invariants(alphabet):
    rng = random.Random(1234)
    for _ in range(1500):
        base = [rng.choice(alphabet) for _ in range(rng.randint(0, 12))]
        first, second = _mutate(rng, base, alphabet), _mutate(rng, base, alphabet)
        result, conflicts = merged(base, first, second)
        _well_formed(result)
        assert sum(text.startswith(MARKER_FIRST) for text in result) == conflicts
        # Trivial merges.
        assert merged(base, first, base) == (first, 0)
        assert merged(base, base, second) == (second, 0)
        assert merged(base, first, first) == (first, 0)
        # Re-merging a merge with itself changes nothing.
        assert merged(base, result, result) == (result, 0)
        # Swapping sides finds the same conflicts and keeps the same lines.
        swapped, swapped_conflicts = merged(base, second, first)
        assert swapped_conflicts == conflicts
        if not conflicts:
            # Clean: each side's version of every change is present, nothing else is lost:
            # merging the result back against either side as base yields the result.
            assert sorted(result) == sorted(swapped)
            assert merged(first, result, first) == (result, 0)


def test_random_merges_conserve_every_line():
    """With every line unique: lines either side inserted appear exactly once, lines both
    deleted are gone, lines both kept stay, and a line one side deleted survives only inside
    a conflict, as the other side's version."""
    rng = random.Random(99)
    counter = iter(range(10**9))

    def mutate(base):
        result = list(base)
        for _ in range(rng.randint(0, 5)):
            spot = rng.randint(0, len(result))
            roll = rng.random()
            if roll < 0.4:
                result[spot:spot] = [f'new {next(counter)}' for _ in range(rng.randint(1, 3))]
            elif result:
                del result[min(spot, len(result) - 1):spot + rng.randint(1, 2)]
        return result

    for _ in range(2000):
        base = [f'base {next(counter)}' for _ in range(rng.randint(0, 10))]
        first, second = mutate(base), mutate(base)
        result, _ = merged(base, first, second)
        for text in set(first + second) - set(base):
            assert result.count(text) == 1, (base, first, second, result)
        for text in base:
            if text in first and text in second:
                assert text in result
            elif text not in first and text not in second:
                assert text not in result
            else:
                # Gone wherever the side that deleted it wins.
                deleter = 0 if text not in first else 1
                assert text not in _resolve(result, deleter), (base, first, second, result)


def test_disjoint_edits_never_conflict_and_merge_exactly():
    rng = random.Random(7)
    for _ in range(800):
        size = rng.randint(4, 30)
        base = [f'base {index}' for index in range(size)]
        # Alternate ownership of stretches separated by untouched lines.
        first, second, expected = [], [], []
        index = 0
        while index < size:
            stretch = base[index:index + rng.randint(1, 3)]
            owner = rng.choice((0, 1, None))
            edited = ([f'{text} edited {owner}' for text in stretch] if owner is not None
                      else stretch)
            if rng.random() < 0.3 and owner is not None:
                edited = edited + [f'new after {index} by {owner}']
            first += edited if owner == 0 else stretch
            second += edited if owner == 1 else stretch
            expected += edited
            index += len(stretch)
            if index < size:  # An untouched separator line.
                first.append(base[index])
                second.append(base[index])
                expected.append(base[index])
                index += 1
        assert merged(base, first, second) == (expected, 0)


def test_large_notes_merge_quickly():
    base = [f'line {index} of a long investigation' for index in range(5000)]
    first, second = list(base), list(base)
    for index in range(0, 5000, 50):
        first[index] = f'first edited {index}'
        second[index + 25] = f'second edited {index}'
    second[100] = 'second also edited 100'  # One overlap.
    start = time.perf_counter()
    result, conflicts = merged(base, first, second)
    assert time.perf_counter() - start < 2
    assert conflicts == 1 and len(result) == 5000 + 4


# Typed metadata JSON.

def _random_value(rng, depth=0):
    kind = rng.randint(0, 6 if depth < 3 else 4)
    if kind == 0:
        return rng.choice([True, False])
    if kind == 1:
        return rng.randint(0, 2**40)
    if kind == 2:
        return -rng.randint(1, 2**20)
    if kind == 3:
        return ''.join(rng.choice('aé🙂\n\t"\\') for _ in range(rng.randint(0, 8)))
    if kind == 4:
        return rng.random()
    if kind == 5:
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {f'k{index}': _random_value(rng, depth + 1) for index in range(rng.randint(0, 4))}


def test_typed_json_round_trips():
    rng = random.Random(5)
    for _ in range(500):
        value = _random_value(rng)
        node = encode_typed(value)
        assert decode_typed(json.loads(json.dumps(node))) == value


# Exactly as Binary Ninja 6.1 hands conflict strings to a splitter (and takes them back).
BINARY_NINJA_ESCAPES = {
    'plain': 'plain', 'new\nline': 'new\\nline', 'tab\there': 'tab\\there',
    'quote " here': 'quote \\" here', 'back\\slash': 'back\\\\slash',
    'both \\n literal': 'both \\\\n literal', 'cr\r': 'cr\\r', 'bell\x07': 'bell\\x07',
    'é ü': '\\xc3\\xa9 \\xc3\\xbc', '日本': '\\xe6\\x97\\xa5\\xe6\\x9c\\xac',
    '🙂 emoji': '\\xf0\\x9f\\x99\\x82 emoji', ' sep': '\\xe2\\x80\\xa8sep',
    'del\x7f': 'del\\x7f', '\\u0041 literal': '\\\\u0041 literal', '/slash': '/slash',
    "'single'": "\\'single\\'", 'éa': '\\xc3\\xa9a', 'é1f': '\\xc3\\xa91f',
    'bell\x07ab': 'bell\\x07ab', '\\x41': '\\\\x41', 'ü\n': '\\xc3\\xbc\\n',
}


def test_conflict_strings_are_c_escaped_utf8():
    for plain, escaped in BINARY_NINJA_ESCAPES.items():
        assert encode_typed(plain) == {'type': 'StringDataType', 'value': escaped}
        assert decode_typed({'type': 'StringDataType', 'value': escaped}) == plain
    rng = random.Random(3)
    for _ in range(2000):
        text = ''.join(rng.choice('ab\\\'"\n\r\t\x01\x7fé日🙂x01')
                       for _ in range(rng.randint(0, 12)))
        assert decode_typed(encode_typed(text)) == text


def test_unknown_typed_values_are_carried_through():
    raw = {'type': 'RawDataType', 'value': 'AAEC'}
    node = {'type': 'KeyValueDataType', 'value': {'blob': raw, 'n': {
        'type': 'UnsignedIntegerDataType', 'value': 3}}}
    value = decode_typed(node)
    assert value == {'blob': Typed(raw), 'n': 3}
    assert encode_typed(value) == node


# Whole collections.

def note(identity, text, **fields):
    return dict({'id': identity, 'text': text}, **fields)


def root(*notes, **fields):
    return dict({'version': 2, 'notes': list(notes)}, **fields)


def ids(merged_root):
    return [item['id'] for item in merged_root['notes']]


def test_notes_added_edited_and_deleted_on_either_side_merge():
    base = root(note('a', '# A\n'), note('b', '# B\n'), note('gone', 'old'),
                note('kept', 'left alone'))
    first = root(note('a', '# A\nalice\n'), note('b', '# B\n'), note('c', '# C\n'),
                 note('kept', 'left alone'))
    second = root(note('a', '# A\n'), note('b', '# B\nbob\n'), note('gone', 'old'),
                  note('d', '# D\n'), note('kept', 'left alone'))
    result = merge_roots(base, first, second)
    # New notes follow the note before them on their own side.
    assert ids(result) == ['a', 'b', 'd', 'c', 'kept']
    assert [item['text'] for item in result['notes']][:2] == ['# A\nalice\n', '# B\nbob\n']
    assert conflict_count(result) == 0


def test_deleting_a_note_someone_else_edited_keeps_the_edit():
    base = root(note('a', 'text'))
    assert ids(merge_roots(base, root(), root(note('a', 'text, edited')))) == ['a']
    assert ids(merge_roots(base, root(note('a', 'text, edited')), root())) == ['a']
    assert ids(merge_roots(base, root(), root())) == []


def test_note_fields_merge_and_agent_access_conflicts_deny():
    base = root(note('a', 'x', edited=10, folder='f1', **{'mcp-allow': True}))
    first = root(note('a', 'x', edited=20, folder='f2', **{'mcp-allow': False}))
    second = root(note('a', 'x', edited=30, folder='f1', extra='from another client',
                       **{'mcp-allow': True}))
    item = merge_roots(base, first, second)['notes'][0]
    assert item == note('a', 'x', edited=30, folder='f2', extra='from another client',
                        **{'mcp-allow': False})
    both = merge_roots(root(note('a', 'x')), root(note('a', 'x', **{'mcp-allow': True})),
                       root(note('a', 'x', **{'mcp-allow': False})))
    assert both['notes'][0]['mcp-allow'] is False
    gate = merge_roots(root(**{'mcp-allow': True}), root(**{'mcp-allow': False}),
                       root(**{'mcp-allow': True}))
    assert gate['mcp-allow'] is False


def test_same_note_both_edited_conflicts_inside_it_with_authors():
    base = root(note('a', 'one\ntwo', users=[{'line': 0, 'count': 2, 'name': 'alice'}]))
    first = root(note('a', 'one\nTWO', users=[{'line': 0, 'count': 1, 'name': 'alice'},
                                               {'line': 1, 'count': 1, 'name': 'bob'}]))
    second = root(note('a', 'one\n2', users=[{'line': 0, 'count': 1, 'name': 'alice'},
                                              {'line': 1, 'count': 1, 'name': 'carol'}]))
    result = merge_roots(base, first, second)
    item = result['notes'][0]
    assert item['text'] == 'one\n<<<<<<< bob\nTWO\n=======\n2\n>>>>>>> carol'
    assert _runs(item, 'users') == ((0, 1, 'alice'), (2, 1, 'bob'), (4, 1, 'carol'))
    assert conflict_count(result) == 1


def test_folders_merge_by_id():
    base = root(folders=[{'id': 'f', 'name': 'Old'}, {'id': 'g', 'name': 'Gone'}])
    first = root(folders=[{'id': 'f', 'name': 'Renamed'}, {'id': 'g', 'name': 'Gone'},
                          {'id': 'h', 'name': 'Mine'}])
    second = root(folders=[{'id': 'f', 'name': 'Old', 'parent': 'n'}, {'id': 'n', 'name': 'New'}])
    result = merge_roots(base, first, second)
    assert result['folders'] == [{'id': 'f', 'name': 'Renamed', 'parent': 'n'},
                                 {'id': 'n', 'name': 'New'}, {'id': 'h', 'name': 'Mine'}]
    assert 'folders' not in merge_roots(base, root(), root())


def test_unreadable_sides_are_left_to_binary_ninja():
    assert merge_roots(root(), {'version': 3, 'notes': []}, root()) is None
    assert merge_roots(root(), root(), {'notes': 'nope', 'version': 2}) is None
    # A side without the key at all is an empty collection.
    assert ids(merge_roots(None, root(note('a', 'x')), None)) == ['a']


def test_resolve_takes_and_gives_typed_json():
    sides = [encode_typed(value) for value in (
        root(note('a', 'x')), root(note('a', 'x'), note('b', 'y')), root(note('a', 'z')))]
    result = decode_typed(resolve(*sides))
    assert result == root(note('a', 'z'), note('b', 'y'))
    assert resolve(sides[0], {'type': 'StringDataType', 'value': 'nope'}, sides[2]) is None


def test_only_the_notes_key_is_ours():
    assert is_notes_conflict(f'Raw/value_store/value/{METADATA_KEY}', 'value_store_entry')
    assert is_notes_conflict(f'Mapped/value_store/value/{METADATA_KEY}', 'value_store_entry')
    assert not is_notes_conflict(f'Raw/value_store/value/{METADATA_KEY}.other',
                                 'value_store_entry')
    assert not is_notes_conflict('Raw/value_store/value/other', 'value_store_entry')
    assert not is_notes_conflict(f'Raw/value_store/value/{METADATA_KEY}', 'symbol')


class _Store:
    def __init__(self, value):
        self.values = {METADATA_KEY: value}
        self.file = type('File', (), {'raw': None})()

    def query_metadata(self, key):
        return self.values[key]


def _random_root(rng, base=None):
    """A random collection, or a random set of changes to `base`."""
    alphabet = [f'line {index}' for index in range(12)] + ['']
    if base is None:
        notes = [note(f'n{index}',
                      '\n'.join(rng.choice(alphabet) for _ in range(rng.randint(1, 6))),
                      edited=rng.randint(0, 100))
                 for index in range(rng.randint(0, 6))]
        folders = [{'id': f'f{index}', 'name': f'Folder {index}'} for index in range(3)]
        return root(*notes, folders=folders, **{'mcp-allow': rng.choice([True, False])})
    result = json.loads(json.dumps(base))
    for item in list(result['notes']):
        roll = rng.random()
        if roll < 0.15:
            result['notes'].remove(item)
        elif roll < 0.6:
            item['text'] = '\n'.join(_mutate(rng, item['text'].split('\n'), alphabet))
            item['users'] = [{'line': 0, 'count': item['text'].count('\n') + 1,
                              'name': rng.choice(['alice', 'bob'])}]
            item['edited'] = rng.randint(100, 200)
        if rng.random() < 0.2:
            item['folder'] = rng.choice(['f0', 'f1', 'f2', 'missing'])
        if rng.random() < 0.1:
            item['mcp-allow'] = rng.choice([True, False])
    for _ in range(rng.randint(0, 2)):
        result['notes'].insert(rng.randint(0, len(result['notes'])),
                               note(f'new{rng.randint(0, 10**9)}', rng.choice(alphabet) or 'x'))
    if rng.random() < 0.3:
        result['folders'] = [dict(folder, name=f'{folder["name"]}!') for folder in
                             result['folders']]
    if rng.random() < 0.2:
        result['folders'].append({'id': f'g{rng.randint(0, 10**9)}', 'name': 'More',
                                  'parent': 'f0'})
    return result


def test_random_collections_merge_into_loadable_notes():
    rng = random.Random(2026)
    for _ in range(600):
        base = _random_root(rng)
        first, second = _random_root(rng, base), _random_root(rng, base)
        result = merge_roots(base, first, second)
        assert valid_root(result)
        identities = ids(result)
        assert len(identities) == len(set(identities))
        assert all(item['text'] for item in result['notes'])
        # Notes added on either side survive; notes deleted on one side and left alone on
        # the other are gone.
        old = {item['id']: item for item in base['notes']}
        for side, other in ((first, second), (second, first)):
            mine = {item['id'] for item in side['notes']}
            yours = {item['id']: item for item in other['notes']}
            for identity in mine - set(old):
                assert identity in identities
            for identity in set(old) - mine:
                if yours.get(identity) == old[identity]:
                    assert identity not in identities
        # Both orders keep the same notes; the typed form loads like any stored root.
        assert sorted(ids(merge_roots(base, second, first))) == sorted(identities)
        typed = json.loads(json.dumps(encode_typed(result)))
        loaded = NoteStorage(_Store(decode_typed(typed))).load()
        assert [stored.id for stored in loaded] == identities
        assert merge_roots(base, result, result) == result
