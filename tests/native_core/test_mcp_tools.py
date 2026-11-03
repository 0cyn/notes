from tests.native_support import CoreCase


class McpToolTests(CoreCase):
    def test_tools_are_registered_headlessly_and_edit_view_metadata(self):
        try:
            from binaryninja import mcp
        except ImportError:
            self.skipTest('Requires Binary Ninja 6.0 or later')
        import bnnotes  # noqa: F401  The plugin entry point registers the tools.
        from bnnotes.src.storage import NoteStorage, StoredNote

        names = {tool.name for tool in mcp.Tool if tool.name.startswith('bnnotes_')}
        self.assertIn('bnnotes_edit_note', names)
        tool = mcp.Tool.by_name('bnnotes_edit_note')
        self.assertEqual(tool.scope, mcp.McpToolScope.BinaryViewScope)
        self.assertEqual(tool.input_schema['required'], ['note_id', 'old_text', 'new_text'])

        view = self.load(bytes(64))
        NoteStorage(view).save([StoredNote('a', '# Notes\n\nold\n', True),
                                StoredNote('b', 'private', False)])
        result = tool.invoke({'note_id': 'a', 'old_text': 'old', 'new_text': 'new'}, view)
        self.assertFalse(result.get('isError'), result)
        self.assertEqual(NoteStorage(view).load()[0].text, '# Notes\n\nnew\n')

        listed = mcp.Tool.by_name('bnnotes_list_notes').invoke({}, view)
        self.assertEqual([note['id'] for note in listed['structuredContent']['notes']], ['a'])
        refused = mcp.Tool.by_name('bnnotes_read_note').invoke({'note_id': 'b'}, view)
        self.assertTrue(refused.get('isError'), refused)
