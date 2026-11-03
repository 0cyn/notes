from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog

from src.export import code_lines, embeds, export_markdown, fenced
from src.links import HLILLine, HLILSourceLine, HLILToken, ILMode
from src.markdown import MarkdownParser
from src.settings import Scope

from .conftest import Function, wait_until

START = 0x1000


def row(address, text, indent=4):
    return HLILSourceLine(address, (HLILToken(text),), indent)


STATEMENT = HLILLine(START + 4, '', 'first', available=True,
                     lines=(row(START + 4, 'int32_t size = read(a | b)'),))
FUNCTION = HLILLine(START, '', 'first', available=True, function_start=START, expand_block=True,
                    function_signature=(row(START, 'int64_t first(int a)', 0),),
                    lines=(row(START + 4, 'if (a)', 4), row(START + 8, 'return 1', 8)))


@pytest.fixture(scope='module')
def parser():
    return MarkdownParser()


def exported(parser, source, preview=STATEMENT, *, show_addresses=True, **by_link):
    """`source` exported, every link showing `preview` (or by_link[label text])."""
    analysis = parser.parse(source)
    previews = {link: preview for link in analysis.links}
    return export_markdown(source, analysis, previews, show_addresses=show_addresses)


def test_a_linked_statement_becomes_its_label_and_a_c_block(parser):
    assert exported(parser, 'Intro\n\n[The read](binaryninja://?expr=0x1004)\n\nAfter') == (
        'Intro\n\nThe read\n```c\n1004  int32_t size = read(a | b)\n```\n\nAfter')


def test_without_addresses_code_keeps_only_its_relative_indentation(parser):
    source = '[!](binaryninja://?expr=0x1000)'
    assert exported(parser, source, FUNCTION) == (
        '```c\n1000  int64_t first(int a)\n1004      if (a)\n1008          return 1\n```')
    assert exported(parser, source, FUNCTION, show_addresses=False) == (
        '```c\nint64_t first(int a)\n    if (a)\n        return 1\n```')


def test_text_around_an_inline_embed_stays_on_its_own_lines(parser):
    source = 'See [the read](binaryninja://?expr=0x1004) and [!](binaryninja://?expr=0x1000) end.'
    assert exported(parser, source, show_addresses=False) == (
        'See the read\n```c\nint32_t size = read(a | b)\n```\n'
        'and\n```c\nint32_t size = read(a | b)\n```\nend.')


def test_blocks_stay_inside_their_quote_and_list_item(parser):
    source = '> - [!Whole](binaryninja://?expr=0x1000)\n> - next\n\n1. [](binaryninja://?expr=1)'
    result = exported(parser, source, FUNCTION, show_addresses=False)
    assert result == ('> - Whole\n>   ```c\n>   int64_t first(int a)\n>       if (a)\n'
                      '>           return 1\n>   ```\n> - next\n\n'
                      '1. ```c\n   int64_t first(int a)\n       if (a)\n'
                      '           return 1\n   ```')
    analysis = parser.parse(result)
    assert len(analysis.code_blocks) == 2 and not analysis.links
    assert [item.language for item in analysis.code_blocks] == ['c', 'c']


def test_table_cells_get_one_code_span_per_line(parser):
    tricky = HLILLine(START, '', 'first', available=True,
                      lines=(row(START, 'if (a | b)', 4), row(START + 4, 'return `x`', 8)))
    source = ('| Where | Code |\n| --- | --- |\n'
              '| here | [Check](binaryninja://?expr=0x1000) and more |')
    result = exported(parser, source, tricky, show_addresses=False)
    assert result.splitlines()[2] == (
        '| here | Check<br>`if (a \\| b)`<br>``     return `x` `` and more |')
    table = parser.parse(result).tables[0]
    assert len(table.rows) == 2 and len(table.rows[1]) == 2


def test_fences_outgrow_backticks_in_the_code(parser):
    assert fenced(['a ``` b', 'c'], 'c') == ['````c', 'a ``` b', 'c', '````']
    assert fenced([]) == ['```', '```']


@pytest.mark.parametrize('mode, language', [
    (ILMode.HLIL, 'c'), (ILMode.PSEUDO_C, 'c'), (ILMode.PSEUDO_RUST, 'rust'),
    (ILMode.PSEUDO_OBJC, 'objc'), (ILMode.DISASM, 'asm'), (ILMode.MLIL, ''), (ILMode.LLIL, ''),
])
def test_each_il_mode_gets_its_language(parser, mode, language):
    preview = HLILLine(START, '', available=True, il_mode=mode, lines=(row(START, 'x'),))
    assert exported(parser, f'[](binaryninja://?expr=1 "{mode.value}")', preview,
                    show_addresses=False) == f'```{language}\nx\n```'


def test_links_without_code_stay_links(parser):
    source = '[a](binaryninja://?expr=1) [b](binaryninja://?expr=2)'
    analysis = parser.parse(source)
    first, second = analysis.links
    unavailable = HLILLine(START, 'HLIL unavailable')
    assert export_markdown(source, analysis, {first: unavailable, second: None}) == source
    assert export_markdown(source, analysis, {}) == source
    assert export_markdown(source, analysis, {second: STATEMENT}, show_addresses=False) == (
        '[a](binaryninja://?expr=1) b\n```c\nint32_t size = read(a | b)\n```')


def test_wide_characters_before_a_link_keep_positions(parser):
    source = '🙂 é [日本](binaryninja://?expr=1) 🙂'
    assert exported(parser, source, show_addresses=False) == (
        '🙂 é 日本\n```c\nint32_t size = read(a | b)\n```\n🙂')


def test_code_lines_pad_addresses_and_flatten_newlines():
    preview = HLILLine(0xff, '', available=True,
                       lines=(row(0xff, 'a'), row(0x1000, '', 8), row(0x1000, 'b\nc', 8)))
    assert code_lines(preview, show_addresses=True) == ['ff    a', '', '1000      b c']


def test_embeds_are_links_to_this_file_on_one_line(parser, preview_view):
    source = ('[here](binaryninja://?expr=first+4) [web](https://example.com) '
              '[other](binaryninja:///other.bndb?expr=first) [missing](binaryninja://?expr=nope)\n'
              '`[code](binaryninja://?expr=first)` [split\nlabel](binaryninja://?expr=first)')
    found = embeds(parser.parse(source), preview_view)
    assert [item.address for item in found] == [preview_view.start + 4]
    assert found[0].key == (preview_view.start + 4, False, ILMode.HLIL)


def _save_as(monkeypatch, path):
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *_args, **_kwargs: (str(path), 'Markdown (*.md)')))


def test_export_note_writes_embeds_as_code(notebook, preview_view, monkeypatch, tmp_path, qapp):
    preview_view.functions[0] = Function(preview_view.start)
    widget = notebook(preview_view)
    source = '# Findings\n\n[The call](binaryninja://?expr=first+4)\n'
    widget.current_note.replace_source(source)
    path = tmp_path / 'note.md'
    _save_as(monkeypatch, path)
    widget.export_markdown()
    wait_until(path.exists)
    start = preview_view.start
    assert path.read_text() == (f'# Findings\n\nThe call\n```c\n{start + 4:x}  '
                                'result = worker(arg1)\n```\n')
    assert widget.current_note.session.source == source  # The note itself is unchanged.

    widget.notebook.preferences.set('hlil_show_addresses', False, Scope.APPLICATION)
    path.unlink()
    widget.export_markdown()
    wait_until(path.exists)
    assert '```c\nresult = worker(arg1)\n```' in path.read_text()

    widget.notebook.preferences.set('export_embeds_as_code', False, Scope.APPLICATION)
    path.unlink()
    widget.export_markdown()
    assert Path(path).read_text() == source  # Written at once, as the note's source.
