import json

from tests.native_support import CoreCase


class SettingsPersistenceTests(CoreCase):
    def setUp(self):
        super().setUp()
        from bnnotes.src.settings import SCHEMA, NativeSettings, Scope

        self.schema, self.Scope = SCHEMA, Scope
        self.backend = NativeSettings()
        self.settings = self.bn.Settings()
        self.project = self.bn.Project.create_project(str(self.directory / 'notes.bnpr'), 'Tests')
        self.addCleanup(self.project.close)
        self.view = self.load(self.project.create_file(b'\x00', None, 'first.bin'))

    def stored(self, view):
        values = json.loads(self.settings.serialize_settings(
            view, self.bn.SettingsScope.SettingsResourceScope,
        ))
        return {key: values[key] for key, _properties in self.schema.values() if key in values}

    def different(self, value):
        if isinstance(value, bool):
            return not value
        if isinstance(value, int):
            return 21 if value != 21 else 23
        return 'No' if value != 'No' else 'Whitelisted'

    def test_new_files_inherit_without_a_manual_reset_and_follow_parent_edits(self):
        self.assertEqual(self.stored(self.view), {})
        changes = {}
        for name in self.schema:
            parent = self.backend.read(name, self.view, self.Scope.APPLICATION)[0]
            changes[name] = self.different(parent)
            self.backend.write(name, changes[name], self.view, self.Scope.PROJECT)
        peer = self.load(self.project.create_file(b'\x00', None, 'peer.bin'))
        self.assertEqual(self.stored(peer), {})
        for name, value in changes.items():
            with self.subTest(setting=name):
                self.assertEqual(self.backend.read(name, peer), (value, self.Scope.PROJECT))
                self.backend.write(name, self.different(value), peer, self.Scope.VIEW)
                self.assertEqual(self.backend.read(name, peer)[1], self.Scope.VIEW)
                self.backend.write(name, value, peer, self.Scope.VIEW)
                self.assertNotIn(self.schema[name][0], self.stored(peer))
        self.assertEqual(self.stored(self.view), {})
        self.assertEqual(self.stored(peer), {})

    def test_bndb_reopen_cleans_copies_but_preserves_genuine_overrides(self):
        parents = {}
        for name, (key, props) in self.schema.items():
            parent = parents[name] = self.backend.read(name, self.view, self.Scope.APPLICATION)[0]
            project_value = self.different(parent)
            self.backend.write(name, project_value, self.view, self.Scope.PROJECT)
            kind = {'boolean': 'bool', 'number': 'integer', 'string': 'string'}[props['type']]
            value = parent if name == 'word_wrap' else project_value
            self.assertTrue(getattr(self.settings, f'set_{kind}')(
                key, value, self.view, self.bn.SettingsScope.SettingsResourceScope,
            ))
        path = self.directory / 'legacy.bndb'
        self.assertTrue(self.view.create_database(str(path)))
        saved = self.project.create_file_from_path(str(path), None, 'saved.bndb')
        reopened = self.load(saved)
        expected = {self.schema['word_wrap'][0]: parents['word_wrap']}
        self.assertEqual(self.stored(reopened), expected)
        self.assertTrue(reopened.save_auto_snapshot())
        self.close_view(reopened)
        again = self.load(saved)
        self.assertEqual(self.stored(again), expected)
        for name in self.schema:
            self.backend.write(name, parents[name], again, self.Scope.PROJECT)
            self.assertEqual(self.backend.read(name, again)[0], parents[name])
        self.assertEqual(self.stored(again), {})
