from __future__ import annotations

import json
from enum import IntEnum
from pathlib import Path


class Scope(IntEnum):
    AUTO = 1
    DEFAULT = 2
    APPLICATION = 4
    PROJECT = 8
    VIEW = 16


SCOPE_LABELS = {
    Scope.DEFAULT: 'Default', Scope.APPLICATION: 'Application',
    Scope.PROJECT: 'Project', Scope.VIEW: 'BinaryView',
}
PARENT_SCOPES = {
    Scope.APPLICATION: Scope.DEFAULT, Scope.PROJECT: Scope.APPLICATION, Scope.VIEW: Scope.PROJECT,
}
MIN_FONT_SIZE = 1
MAX_FONT_SIZE = 72
# One title and description per setting, shown by Binary Ninja's Settings, the Settings tab
# and the welcome tour alike.
SCHEMA = {
    'font_size': ('bnnotes.editor.fontSize', {
        'title': 'Editor Font Size', 'type': 'number', 'default': 0,
        'minValue': 0, 'maxValue': MAX_FONT_SIZE,
        'description': 'Note text size in points; 0 uses the application font, at least 12.',
    }),
    'word_wrap': ('bnnotes.editor.wordWrap', {
        'title': 'Wrap Long Lines', 'type': 'boolean', 'default': True,
        'description': 'Wrap note text at the editor edge instead of scrolling sideways.',
    }),
    'show_authors': ('bnnotes.editor.showAuthors', {
        'title': 'Show Author Bar', 'type': 'boolean', 'default': True,
        'description': 'A bar beside each paragraph, colored by who wrote it. Authors are '
                       'recorded either way.',
    }),
    'hlil_show_addresses': ('bnnotes.hlil.showAddresses', {
        'title': 'Show Addresses in HLIL Embeds', 'type': 'boolean', 'default': True,
        'description': 'An address column beside embedded code.',
    }),
    'export_embeds_as_code': ('bnnotes.export.embedsAsCode', {
        'title': 'Export Embeds as Code Blocks', 'type': 'boolean', 'default': True,
        'description': 'Export Note as Markdown writes each embed as a code block of its code. '
                       'Off, embeds export as binaryninja:// links.',
    }),
    'follow_view_navigation': ('bnnotes.navigation.followView', {
        'title': 'Follow View Navigation', 'type': 'boolean', 'default': False,
        'description': 'When another view navigates, open the note linking there, even a '
                       'closed one, and scroll to the link.',
    }),
    'mcp_default_allow': ('bnnotes.mcp.defaultAllow', {
        'title': 'Allow Agents on New Notes by Default', 'type': 'boolean', 'default': False,
        'description': 'New notes start on the MCP whitelist. Existing notes keep theirs.',
    }),
    'mcp_allow_delete': ('bnnotes.mcp.allowDelete', {
        'title': 'Allow Agents to Delete Notes', 'type': 'string', 'default': 'Whitelisted',
        'enum': ['No', 'Whitelisted', 'All'],
        'enumDescriptions': ['No notes.', 'Whitelisted notes only.', 'Any note.'],
        'description': 'Which notes agents may delete.',
    }),
    'mcp_allow_create': ('bnnotes.mcp.allowCreate', {
        'title': 'Allow Agents to Create Notes on Views', 'type': 'boolean', 'default': True,
        'description': 'Agents may add notes to a file.',
    }),
}


def setting_title(name: str) -> str:
    return SCHEMA[name][1]['title']


def setting_description(name: str) -> str:
    return SCHEMA[name][1]['description']


def setting_choices(name: str) -> list:
    """(label, value) pairs for a setting with fixed values."""
    properties = SCHEMA[name][1]
    return list(zip(properties['enum'], properties['enum'], strict=True))


DEFAULTS = {name: properties['default'] for name, (_key, properties) in SCHEMA.items()}
_NATIVE_TYPES = {'boolean': 'bool', 'number': 'integer', 'string': 'string'}


def validate(name, value):
    default = DEFAULTS[name]
    if type(value) is not type(default):
        raise ValueError(f'{name} must be a {type(default).__name__}.')
    if name == 'font_size' and not 0 <= value <= MAX_FONT_SIZE:
        raise ValueError(f'Font size must be 0–{MAX_FONT_SIZE} pt.')
    if name == 'mcp_allow_delete' and value not in SCHEMA[name][1]['enum']:
        raise ValueError('Deletion policy must be No, Whitelisted, or All.')
    return value


def register_settings(settings=None):
    if settings is None:
        from binaryninja import Settings
        settings = Settings()
    settings.register_group('bnnotes', 'Notes')
    for key, properties in SCHEMA.values():
        if (not settings.contains(key)
                and not settings.register_setting(key, json.dumps(properties))):
            raise RuntimeError(f'Could not register Notes setting: {key}')
    return settings


def project_for(data):
    return getattr(data.file, 'project', None) if data is not None else None


class ScopedSettings:
    """Store only differences from the immediately broader effective scope."""

    normalization_revision = 0

    def read(self, name, data=None, scope=Scope.AUTO):
        value, origin = self._read(name, data, scope)
        if origin in (Scope.PROJECT, Scope.VIEW):
            inherited = self.read(name, data, PARENT_SCOPES[origin])
            if value == inherited[0]:
                # Binary Ninja snapshots resource settings on load; drop copies so they inherit.
                self.reset(name, data, origin)
                self.normalization_revision += 1
                return inherited
        return value, origin

    def write(self, name, value, data, scope):
        value = validate(name, value)
        self.read(name, data)  # Clear stale copies while they still equal their parent.
        if value == self.read(name, data, PARENT_SCOPES[scope])[0]:
            self.reset(name, data, scope)
        else:
            self._write(name, value, data, scope)


class NativeSettings(ScopedSettings):
    def __init__(self, settings=None):
        self.settings = register_settings(settings)
        self._projects = set()

    @staticmethod
    def _native(name, data, scope):
        from binaryninja import SettingsScope
        key, properties = SCHEMA[name]
        return (key, _NATIVE_TYPES[properties['type']],
                data if scope != Scope.APPLICATION else None, SettingsScope(scope))

    def _prepare_project(self, data):
        project = project_for(data)
        if project is None or self.settings.instance_id != 'default':
            return
        key = data.file.session_id, data.view_type, project.path
        if key in self._projects:
            return
        from binaryninja import SettingsScope
        # Share the native project UI's settings.json; headless opens may not have bound it yet.
        path = str(Path(project.path) / 'settings.json')
        if not self.settings.load_settings_file(path, SettingsScope.SettingsProjectScope, data):
            raise RuntimeError(f'Could not load project settings: {path}')
        self._projects.add(key)

    def _read(self, name, data=None, scope=Scope.AUTO):
        if scope != Scope.APPLICATION:
            self._prepare_project(data)
        key, kind, view, native = self._native(name, data, scope)
        value, origin = getattr(self.settings, f'get_{kind}_with_scope')(key, view, native)
        try:
            value = validate(name, value)
        except ValueError:
            value = DEFAULTS[name]
        return value, Scope(origin)

    def _write(self, name, value, data, scope):
        if scope != Scope.APPLICATION:
            self._prepare_project(data)
        key, kind, view, native = self._native(name, data, scope)
        if not getattr(self.settings, f'set_{kind}')(key, validate(name, value), view, native):
            raise RuntimeError(f'Could not save {key} in {SCOPE_LABELS[scope]} scope.')

    def reset(self, name, data, scope):
        if scope != Scope.APPLICATION:
            self._prepare_project(data)
        key, _kind, view, native = self._native(name, data, scope)
        if not self.settings.reset(key, view, native):
            raise RuntimeError(f'Could not reset {key}.')


def normalize_view_settings(data):
    backend = NativeSettings()
    for name in DEFAULTS:
        backend.read(name, data)


class MemorySettings(ScopedSettings):
    """Ephemeral settings for use without Binary Ninja."""

    def __init__(self):
        self.values = {}

    def _key(self, name, data, scope):
        context = (data.file.session_id if scope == Scope.VIEW else
                   getattr(project_for(data), 'path', None) if scope == Scope.PROJECT else None)
        return scope, context, name

    def _read(self, name, data=None, scope=Scope.AUTO):
        for candidate in (Scope.VIEW, Scope.PROJECT, Scope.APPLICATION):
            if scope != Scope.AUTO and candidate > scope:
                continue
            if candidate == Scope.VIEW and data is None:
                continue
            if candidate == Scope.PROJECT and project_for(data) is None:
                continue
            key = self._key(name, data, candidate)
            if key in self.values:
                return self.values[key], candidate
        return DEFAULTS[name], Scope.DEFAULT

    def _write(self, name, value, data, scope):
        self.values[self._key(name, data, scope)] = validate(name, value)

    def reset(self, name, data, scope):
        self.values.pop(self._key(name, data, scope), None)
