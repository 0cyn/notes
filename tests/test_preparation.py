import os
import sys
from types import ModuleType, SimpleNamespace

import pytest
from PySide6.QtCore import (
    QObject,
)
from PySide6.QtGui import QFont

from src.preparation import PreparedMarkdown, prepare_markdown
from src.ui import highlighter
from src.ui.highlighter import ParserProcess, parser_process
from src.ui.tables import cell_document, cell_html

from .conftest import wait_until


def test_cell_preview_renders_extensions_without_executing_html(qapp):
    document = cell_document(
        "==**marked**== $x+y$ %%hidden%% [[Target|label]] `<script>`", QFont(), 500,
    )
    text = document.toPlainText()
    assert "marked" in text and "x+y" in text and "label" in text
    assert "hidden" not in text
    assert "==" not in text and "[[" not in text and "$" not in text
    assert "<script>" in text
    assert document.find("marked").charFormat().fontWeight() > QFont.Weight.Normal
    assert document.find("marked").charFormat().background().color().isValid()


def test_parser_helper_preserves_full_unicode_analysis_and_is_a_separate_process(qapp):
    source = '# Unicode 🙂\n\n> quoted **text**\n\n| a | b |\n| --- | --- |\n| α | β |'
    results = []

    class Receiver(QObject):
        def preparation_finished(self, key, future):
            results.append((key, future.result()))

    receiver = Receiver()
    service = parser_process()
    service.submit(receiver, source, normalize=True, cells=True)
    wait_until(lambda: bool(results))
    assert service.process.pid != os.getpid()
    assert results[0][0] == source
    assert results[0][1] == prepare_markdown(source, normalize=True, cells=True)
    assert results[0][1].analysis.tables
    assert results[0][1].cells


def _prepare(service, source):
    results = []

    class Receiver(QObject):
        def preparation_finished(self, key, future):
            results.append(future.result())

    receiver = Receiver()
    service.submit(receiver, source, normalize=True, cells=True)
    wait_until(lambda: bool(results))
    return results[0]


@pytest.fixture
def binary_ninja(monkeypatch):
    """Binary Ninja's Python lookup, as `PythonScriptingProvider` exposes it."""
    found = SimpleNamespace(command=None, environment=None, error=None)

    class Provider:
        @property
        def _python_bin(self):
            if found.error:
                raise found.error
            return found.command

        def _get_python_environment(self):
            return found.environment

    module = ModuleType('binaryninja.scriptingprovider')
    module.PythonScriptingProvider = Provider
    monkeypatch.setitem(sys.modules, 'binaryninja', ModuleType('binaryninja'))
    monkeypatch.setitem(sys.modules, 'binaryninja.scriptingprovider', module)
    return found


def test_the_helper_runs_the_python_binary_ninja_installs_dependencies_with(binary_ninja):
    binary_ninja.command, binary_ninja.environment = '/opt/python3', {'PYTHONHOME': '/opt'}
    assert highlighter._python_command() == ('/opt/python3', {'PYTHONHOME': '/opt'})
    binary_ninja.command = None
    assert highlighter._python_command() is None
    binary_ninja.command, binary_ninja.error = '/opt/python3', AttributeError('renamed')
    assert highlighter._python_command() is None


def test_without_a_helper_notes_are_prepared_in_this_process(binary_ninja, qapp):
    source = '# Title\n\n| a | b |\n| --- | --- |\n| α | β |'
    service = ParserProcess(qapp)
    try:
        assert _prepare(service, source) == prepare_markdown(source, normalize=True, cells=True)
        assert not service.helper and service.process is None
    finally:
        service.close()


def test_a_helper_without_the_packages_is_replaced_by_this_process(monkeypatch, qapp):
    source = '# Title\n\nText'
    service = ParserProcess(qapp)
    monkeypatch.setattr(service, '_ask_helper', lambda source, _request: PreparedMarkdown(
        source, source, prepare_markdown('').analysis, 'Plain text mode: no tree_sitter'))
    try:
        result = _prepare(service, source)
        assert result == prepare_markdown(source, normalize=True, cells=True)
        assert not result.warning and not service.helper
    finally:
        service.close()


def test_prepared_cells_bind_only_resolved_links_and_keep_unresolved_labels(qapp):
    from src.links import HLILLine
    from src.preparation import prepare_cell

    first, second = 'binaryninja://?expr=template_first', 'binaryninja://?expr=template_second'
    source = f'**Before** [!**Expanded**]({first}) and [Ordinary]({second}) after'
    template = prepare_cell(source, False)
    assert template.render(()) == cell_html(source)
    expanded = template.render(((first, HLILLine(1, 'body', expand_block=True)),))
    assert expanded.count('\ufffc') == 1
    assert 'bnnotes-hlil-preview:0:0:Expanded' in expanded
    assert f'<a href="{second}">Ordinary</a>' in expanded
    assert '<strong>Before</strong>' in expanded
    ordinary = template.render(((second, HLILLine(2, 'body')),))
    assert ordinary.count('\ufffc') == 1
    assert f'<a href="{first}">!<strong>Expanded</strong></a>' in ordinary
    assert 'bnnotes-hlil-preview:0:1:Ordinary' in ordinary


