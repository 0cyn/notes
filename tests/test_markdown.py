
import pytest

from src.links import ILMode
from src.markdown import (
    MarkdownParser,
    clipboard_matrix,
    expansion_edit,
    mode_edit,
    new_table,
    utf16_length,
)


def roles(analysis, row):
    return {span.role for span in analysis.lines[row]}


def _apply(source, edit):
    return source if edit is None else source[:edit.start] + edit.text + source[edit.end:]


def _link(source):
    (link,) = MarkdownParser().parse(source).links
    return link


URL = 'binaryninja://?expr=first+4'


@pytest.mark.parametrize("level", range(1, 7))
def test_eof_headings_parse_without_adding_a_newline_to_the_source(level):
    parser = MarkdownParser()
    source = "#" * level + " Heading"
    parsed = parser.parse(source)
    assert [(heading.level, heading.title) for heading in parsed.headings] == [(level, "Heading")]
    assert len(parsed.lines) == 1
    assert all(span.start + span.length <= len(source) for span in parsed.lines[0])
    assert not parser.parse("Heading").headings


def test_headings_and_code_are_distinct():
    parsed = MarkdownParser().parse(
        "# Title ###\n\nSubheading\n----------\n\n"
        "```markdown\n# not a heading\n**literal** | x\n```\n"
    )
    assert [(h.title, h.level, h.line) for h in parsed.headings] == [
        ("Title", 1, 0), ("Subheading", 2, 2)
    ]
    assert parsed.code_lines == frozenset({5, 6, 7, 8})
    assert "strong" not in roles(parsed, 7)
    assert "marker" not in roles(parsed, 7)


def test_gfm_tables_tasks_and_strikethrough():
    parsed = MarkdownParser().parse(
        "- [x] ~~done~~\n- [ ] todo\n\n| A | B |\n| - | - |\n| x | y |\n"
    )
    assert {"checked", "strike"} <= roles(parsed, 0)
    assert {"table", "strong"} <= roles(parsed, 3)
    assert "table" in roles(parsed, 5)


def test_extensions_produce_source_ranges_and_exclude_code_and_escaped_markers():
    source = r"==**marked**== $x+y$ %%hidden%% [[Target|label]] `==literal== %%text%%` \==plain\=="
    analysis = MarkdownParser().parse(source)
    roles = [item.role for item in analysis.inline_formats]
    assert roles.count("highlight") == 1
    assert roles.count("comment") == 1
    assert roles.count("math") == 1
    assert roles.count("code") == 1
    assert roles.count("link") == 1
    assert roles.count("strong") == 1


def test_multiline_quotes_track_explicit_blank_lazy_and_nested_lines():
    source = "> first\nlazy 🙂\n>\n> > nested\n> > second\n>\n> last\n\nOutside"
    analysis = MarkdownParser().parse(source)
    assert [(line.line, line.depth) for line in analysis.quote_lines] == [
        (0, 1), (1, 1), (2, 1), (3, 2), (4, 2), (5, 1), (6, 1),
    ]
    encoded = source.encode("utf-16-le")
    assert [[encoded[left * 2:right * 2].decode("utf-16-le") for left, right in line.markers]
            for line in analysis.quote_lines] == [["> "], [], [">"], ["> ", "> "],
                                                ["> ", "> "], [">"], ["> "]]


@pytest.mark.parametrize(("rows", "columns"), [(1, 1), (1, 4)])
def test_generated_tables_round_trip_dimensions(rows, columns):
    source = new_table(rows, columns)
    parsed = MarkdownParser().parse(source)
    assert len(parsed.tables) == 1
    table = parsed.tables[0]
    assert len(table.rows) == rows
    assert all(len(row) == columns for row in table.rows)
    assert table.end_position == utf16_length(source)


def test_clipboard_accepts_markdown_and_spreadsheet_tsv():
    rows, alignments = clipboard_matrix("| A | B |\n| --- | ---: |\n| **one** | two |")
    assert rows == [["A", "B"], ["**one**", "two"]]
    assert alignments == ("", "right")
    assert clipboard_matrix("one\ttwo\nthree\tfour")[0] == [["one", "two"], ["three", "four"]]
    assert clipboard_matrix("plain\ntext")[0] == [["plain\ntext"]]


@pytest.mark.parametrize('source, expand, expected', [
    (f'a [x]({URL}) b', True, f'a [!x]({URL}) b'),
    (f'a [!x]({URL}) b', False, f'a [x]({URL}) b'),
])
def test_expansion_edit_toggles_the_label_marker(source, expand, expected):
    assert _apply(source, expansion_edit(_link(source), expand)) == expected


@pytest.mark.parametrize('source, mode, expected', [
    (f'[x]({URL})', ILMode.MLIL, f'[x]({URL} "mlil")'),
    (f'[x]({URL} "mlil")', ILMode.PSEUDO_C, f'[x]({URL} "pseudo-c")'),
])
def test_mode_edit_writes_or_removes_the_link_title(source, mode, expected):
    edited = _apply(source, mode_edit(_link(source), mode))
    assert edited == expected
    assert _link(edited).il_mode is mode


@pytest.mark.parametrize('title, mode', [('"disasm"', ILMode.DISASM), ('"dasm"', ILMode.DISASM)])
def test_link_titles_select_il_modes(title, mode):
    source = f'[label](binaryninja://?expr=0x10 {title})'
    links = MarkdownParser().parse(source).links
    assert len(links) == 1
    assert links[0].il_mode is mode


def test_markdown_links_have_exact_utf16_source_ranges_and_decoded_destinations():
    source = '🙂 [**first**](<binaryninja://?expr=first+4&amp;view=Linear> "title")\n'
    source += '<binaryninja://?expr=second>'
    links = MarkdownParser().parse(source).links
    assert len(links) == 2
    assert links[0].url == 'binaryninja://?expr=first+4&view=Linear'
    assert links[0].start == utf16_length('🙂 ')
    encoded = source.encode('utf-16-le')
    selected = encoded[links[0].start * 2:links[0].end * 2].decode('utf-16-le')
    assert selected.startswith('[**first**]')
    assert links[1].url == 'binaryninja://?expr=second'
    assert links[1].end == utf16_length(source)
