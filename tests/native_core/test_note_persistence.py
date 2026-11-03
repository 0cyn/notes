from tests.native_support import CoreCase


class NotePersistenceTests(CoreCase):
    def test_notes_flags_and_extension_metadata_survive_bndb_save_reopen_and_delete(self):
        from bnnotes.src.storage import METADATA_KEY, NoteStorage, StoredNote

        view = self.load(bytes(64))
        view.file.raw.store_metadata(METADATA_KEY, {
            'version': 2, 'mcp-allow': False, 'extension': {'keep': '🙂'},
            'notes': [{'id': 'one', 'text': 'old', 'custom': 42}],
        })
        notes = [StoredNote('one', '# 🙂\n\n[!](binaryninja://?expr=0x10)\n', True),
                 StoredNote('two', '| Header |\n| --- |\n| final character z |\n', False)]
        NoteStorage(view).save(notes)
        path = self.directory / 'notes.bndb'
        self.assertTrue(view.create_database(str(path)))
        self.close_view(view)
        reopened = self.load(str(path))
        storage = NoteStorage(reopened)
        self.assertEqual(storage.load(), notes)
        self.assertIs(storage.mcp_allow, False)
        root = reopened.file.raw.query_metadata(METADATA_KEY)
        self.assertEqual(root['extension'], {'keep': '🙂'})
        self.assertEqual(root['notes'][0]['custom'], 42)
        storage.save([])
        self.assertTrue(reopened.save_auto_snapshot())
        self.close_view(reopened)
        last = NoteStorage(self.load(str(path)))
        self.assertEqual(last.load(), [])
        self.assertIs(last.mcp_allow, False)
