"""Enterprise merges of notes, through Binary Ninja's real snapshot merge."""

import json
import random

from tests.native_support import CoreCase


class MergeTests(CoreCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from binaryninja import collaboration
        except ImportError:
            raise cls.skipTest(cls, 'Requires the collaboration API') from None
        cls.collaboration = collaboration
        import bnnotes  # noqa: F401  The plugin entry point registers the merge.
        from bnnotes.src import merge, storage
        cls.merge, cls.storage = merge, storage
        cls.KEY = storage.METADATA_KEY
        cls.count = 0

    def branch(self, base, first, second):
        """A database whose two leaf snapshots changed `base` into `first` and `second`."""
        bn = self.bn
        MergeTests.count += 1
        path = self.directory / f'merge-{MergeTests.count}.bndb'
        view = bn.load(bytes(64), update_analysis=False)
        if base is not None:
            view.file.raw.store_metadata(self.KEY, base)
        view.store_metadata('bnnotes.test-other', {'side': 'base'})
        self.assertTrue(view.create_database(str(path)))
        view.file.close()
        view = bn.load(str(path), update_analysis=False)
        base_id = view.file.database.current_snapshot.id
        view.file.raw.store_metadata(self.KEY, first)
        view.store_metadata('bnnotes.test-other', {'side': 'first'})
        self.assertTrue(view.save_auto_snapshot())
        view.file.close()
        view = bn.load(str(path), update_analysis=False)
        database = view.file.database
        database.current_snapshot = database.get_snapshot(base_id)
        view.file.close()
        # A live Database object for the closed file keeps the reopened one from saving.
        del database
        view = bn.load(str(path), update_analysis=False)
        view.file.raw.store_metadata(self.KEY, second)
        view.store_metadata('bnnotes.test-other', {'side': 'second'})
        self.assertTrue(view.save_auto_snapshot())
        leaves = [snapshot for snapshot in view.file.database.snapshots if not snapshot.children]
        self.assertEqual(len(leaves), 2)
        return path, view

    def merged(self, base, first, second):
        """Merge in Binary Ninja; returns (stored root, conflict keys left to the handler)."""
        path, view = self.branch(base, first, second)
        offered = []

        def handler(conflicts):
            offered.extend(conflicts)
            for conflict in conflicts.values():
                conflict.success(conflict.first)
            return True

        self.collaboration.merge_database(view.file.database, handler)
        view.file.close()
        view = self.bn.load(str(path), update_analysis=False)
        try:
            return view.file.raw.query_metadata(self.KEY), offered
        finally:
            view.file.close()

    @staticmethod
    def root(*notes, **fields):
        return dict({'version': 2, 'notes': [dict(note) for note in notes]}, **fields)

    def test_two_peoples_notes_merge_without_asking(self):
        base = self.root({'id': 'a', 'text': '# A\nshared'}, {'id': 'b', 'text': '# B'})
        first = self.root({'id': 'a', 'text': '# A\nshared\nalice'}, {'id': 'b', 'text': '# B'},
                          {'id': 'c', 'text': '# C', 'edited': 12})
        second = self.root({'id': 'a', 'text': '# A\nshared'},
                           {'id': 'b', 'text': '# B\nbob', 'users': [
                               {'line': 1, 'count': 1, 'name': 'bob'}]},
                           {'id': 'd', 'text': '# D'}, folders=[{'id': 'f', 'name': 'Bob'}])
        stored, offered = self.merged(base, first, second)
        # Only the unrelated key reached the conflict dialog.
        self.assertEqual(offered, ['Mapped/value_store/value/bnnotes.test-other'])
        texts = {note['id']: note['text'] for note in stored['notes']}
        self.assertEqual(texts, {'a': '# A\nshared\nalice', 'b': '# B\nbob', 'c': '# C',
                                 'd': '# D'})
        self.assertEqual(stored['folders'], [{'id': 'f', 'name': 'Bob'}])
        notes = self.storage.NoteStorage(_View(stored)).load()
        self.assertEqual(next(note for note in notes if note.id == 'b').users, ((1, 1, 'bob'),))
        self.assertEqual(next(note for note in notes if note.id == 'c').edited, 12)

    def test_the_same_lines_changed_twice_merge_with_markers(self):
        base = self.root({'id': 'a', 'text': 'size check\nend'})
        first = self.root({'id': 'a', 'text': 'size check: missing\nend', 'users': [
            {'line': 0, 'count': 1, 'name': 'alice'}]})
        second = self.root({'id': 'a', 'text': 'size check: fine\nend', 'users': [
            {'line': 0, 'count': 1, 'name': 'bob'}]})
        stored, _ = self.merged(base, first, second)
        text = stored['notes'][0]['text']
        lines = text.split('\n')
        self.assertEqual({lines[0][8:], lines[4][8:]}, {'alice', 'bob'}, text)
        self.assertIn('size check: missing', text)
        self.assertIn('size check: fine', text)
        self.assertEqual(self.merge.conflict_count(stored), 1)

    def test_text_and_authors_changed_apart_still_merge_line_by_line(self):
        """Written through NoteStorage, as Notes writes. One side rewrites a note's lines;
        the other only re-attributes a line (the Author menu). Without a revision on every
        write, Binary Ninja would merge them field by field, pairing one side's text with
        the other side's per-line authors."""
        NoteStorage, StoredNote = self.storage.NoteStorage, self.storage.StoredNote
        bn = self.bn
        MergeTests.count += 1
        path = self.directory / f'storage-{MergeTests.count}.bndb'
        view = bn.load(bytes(64), update_analysis=False)
        NoteStorage(view).save([StoredNote('a', 'one\ntwo\nthree')])
        self.assertTrue(view.create_database(str(path)))
        view.file.close()
        view = bn.load(str(path), update_analysis=False)
        base_id = view.file.database.current_snapshot.id
        NoteStorage(view).save([StoredNote('a', 'zero\none\ntwo\nthree')])
        self.assertTrue(view.save_auto_snapshot())
        view.file.close()
        view = bn.load(str(path), update_analysis=False)
        database = view.file.database
        database.current_snapshot = database.get_snapshot(base_id)
        view.file.close()
        del database
        view = bn.load(str(path), update_analysis=False)
        storage = NoteStorage(view)
        storage.load()
        storage.save([StoredNote('a', 'one\ntwo\nthree', agents=((2, 1, 'Claude'),))])
        self.assertTrue(view.save_auto_snapshot())
        self.collaboration.merge_database(view.file.database, lambda conflicts: all(
            conflict.success(conflict.first) for conflict in conflicts.values()))
        view.file.close()
        view = bn.load(str(path), update_analysis=False)
        try:
            [note] = NoteStorage(view).load()
        finally:
            view.file.close()
        self.assertEqual(note.text, 'zero\none\ntwo\nthree')
        # 'three' moved down a line; its author moved with it.
        self.assertEqual(note.agents, ((3, 1, 'Claude'),))

    def test_unreadable_notes_are_left_to_the_dialog(self):
        base = self.root({'id': 'a', 'text': 'x'})
        stored, offered = self.merged(base, {'version': 99, 'notes': []},
                                      self.root({'id': 'a', 'text': 'y'}))
        self.assertIn(f'Raw/value_store/value/{self.KEY}', offered)
        self.assertEqual(stored, {'version': 99, 'notes': []})  # The handler chose first.

    def test_notes_new_on_both_sides_merge(self):
        stored, offered = self.merged(None, self.root({'id': 'a', 'text': 'alice'}),
                                      self.root({'id': 'b', 'text': 'bob'}))
        self.assertNotIn(f'Raw/value_store/value/{self.KEY}', offered)
        self.assertEqual(sorted(note['id'] for note in stored['notes']), ['a', 'b'])

    def leaves(self, base, *sides):
        """A database with one leaf snapshot per side, each changed from `base`."""
        bn = self.bn
        MergeTests.count += 1
        path = self.directory / f'leaves-{MergeTests.count}.bndb'
        view = bn.load(bytes(64), update_analysis=False)
        view.file.raw.store_metadata(self.KEY, base)
        self.assertTrue(view.create_database(str(path)))
        base_id = view.file.database.current_snapshot.id
        view.file.close()
        for side in sides:
            view = bn.load(str(path), update_analysis=False)
            database = view.file.database
            database.current_snapshot = database.get_snapshot(base_id)
            view.file.close()
            del database
            view = bn.load(str(path), update_analysis=False)
            view.file.raw.store_metadata(self.KEY, side)
            self.assertTrue(view.save_auto_snapshot())
            view.file.close()
        view = bn.load(str(path), update_analysis=False)
        leaves = [snapshot for snapshot in view.file.database.snapshots if not snapshot.children]
        self.assertEqual(len(leaves), len(sides))
        offered = []
        self.collaboration.merge_database(view.file.database, lambda conflicts: offered.extend(
            conflicts) or all(conflict.success(conflict.first) for conflict in conflicts.values()))
        view.file.close()
        view = bn.load(str(path), update_analysis=False)
        try:
            return view.file.raw.query_metadata(self.KEY), offered
        finally:
            view.file.close()

    def test_three_people_merge_down_to_one(self):
        """Three branches merge pairwise, merges of merges included; nobody's work is lost."""
        base = self.root({'id': 'shared', 'text': '# Shared\nintro\nbody\nend'}, revision='0')
        sides = []
        for index, name in enumerate(('alice', 'bob', 'carol')):
            shared = '# Shared\nintro\nbody\nend'.split('\n')
            shared.insert(1 + index * 2 if index < 2 else 4, f'{name} adds a line')
            sides.append(self.root(
                {'id': 'shared', 'text': '\n'.join(shared), 'users': [
                    {'line': 0, 'count': len(shared), 'name': name}]},
                {'id': name, 'text': f'# {name}\nown note'}, revision=str(index + 1)))
        stored, offered = self.leaves(base, *sides)
        self.assertNotIn(f'Raw/value_store/value/{self.KEY}', offered)
        texts = {note['id']: note['text'] for note in stored['notes']}
        self.assertEqual(set(texts), {'shared', 'alice', 'bob', 'carol'})
        for name in ('alice', 'bob', 'carol'):
            self.assertIn(f'{name} adds a line', texts['shared'])
        self.assertEqual(self.merge.conflict_count(stored), 0, texts['shared'])

    def test_any_text_and_other_clients_fields_survive(self):
        odd = 'tab\tquote " back\\slash \\n é 日本 🙂 \'single\' \x07 crlf\r\nend'
        base = self.root({'id': 'a', 'text': odd, 'x-ключ': {'k\\ey': 'v"al'}},
                         {'id': 'b', 'text': 'b'}, revision='0', extension=['é', 1, True])
        first = self.root({'id': 'a', 'text': odd + '\nfirst 🙂', 'x-ключ': {'k\\ey': 'v"al'}},
                          {'id': 'b', 'text': 'b'}, revision='1', extension=['é', 1, True])
        second = self.root({'id': 'a', 'text': 'é second\n' + odd, 'x-ключ': {'k\\ey': 'v"al'}},
                           {'id': 'b', 'text': 'b — “edited”'}, revision='2',
                           extension=['é', 1, True])
        stored, offered = self.merged(base, first, second)
        self.assertNotIn(f'Raw/value_store/value/{self.KEY}', offered)
        notes = {note['id']: note for note in stored['notes']}
        self.assertEqual(notes['a']['text'], 'é second\n' + odd + '\nfirst 🙂')
        self.assertEqual(notes['a']['x-ключ'], {'k\\ey': 'v"al'})
        self.assertEqual(notes['b']['text'], 'b — “edited”')
        self.assertEqual(stored['extension'], ['é', 1, True])

    def test_random_merges_match_the_python_merge(self):
        """Binary Ninja's merge, through the splitter, stores exactly what `merge_roots`
        computes, for random collections changed randomly on both sides, and it loads."""
        from tests.test_merge import _random_root
        rng = random.Random(17)
        for trial in range(40):
            with self.subTest(trial=trial):
                base = _random_root(rng)
                first, second = _random_root(rng, base), _random_root(rng, base)
                # As NoteStorage writes them: each write a new revision.
                for index, side in enumerate((base, first, second)):
                    side['revision'] = f'{trial}-{index}'
                stored, offered = self.merged(base, first, second)
                self.assertNotIn(f'Raw/value_store/value/{self.KEY}', offered)
                expected = [self.merge.merge_roots(base, first, second),
                            self.merge.merge_roots(base, second, first)]
                self.assertIn(json.loads(json.dumps(stored)), expected)
                self.storage.NoteStorage(_View(stored)).load()


class _View:
    def __init__(self, root):
        from bnnotes.src.storage import METADATA_KEY
        self.values = {METADATA_KEY: root}
        self.file = type('File', (), {'raw': None})()

    def query_metadata(self, key):
        return self.values[key]
