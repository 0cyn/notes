"""Exporting a note as portable Markdown: each embed becomes the code it shows."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .links import HLILLine, ILMode, resolve_in_bv_link
from .markdown import MarkdownAnalysis, MarkdownLink

# Fenced-code languages for each mode; HLIL reads as C. Other ILs get no language.
LANGUAGES = {
    ILMode.HLIL: 'c', ILMode.PSEUDO_C: 'c', ILMode.PSEUDO_OBJC: 'objc',
    ILMode.PSEUDO_RUST: 'rust', ILMode.DISASM: 'asm',
}
# Blockquote markers and list-item markers that start a line, before its text.
_CONTAINERS = re.compile(r'(?:[ \t]*(?:>[ \t]?|(?:[-+*]|\d{1,9}[.)])[ \t]+))*[ \t]*')
_LIST_MARKER = re.compile(r'(?:[-+*]|\d{1,9}[.)])(?=[ \t])')


@dataclass(frozen=True)
class Embed:
    """An in-BV link shown as an embed, and the address it resolves to."""

    link: MarkdownLink
    address: int

    @property
    def key(self):
        return self.address, self.link.expand_block, self.link.il_mode


def embeds(analysis: MarkdownAnalysis, data) -> list[Embed]:
    """The links `analysis` shows as embeds: In-BV links on one source line that resolve."""
    found = []
    for link in analysis.links:
        if analysis.line_at_position(link.start) != analysis.line_at_position(
                max(link.start, link.end - 1)):
            continue
        address = resolve_in_bv_link(data, link.url)
        if address is not None:
            found.append(Embed(link, address))
    return found


def code_lines(preview: HLILLine, *, show_addresses: bool) -> list[str]:
    """The embed's code as plain text lines: its signature, then its body."""
    rows = preview.display_lines
    indent = min(row.indent for row in rows)
    width = max(len(f'{row.address:x}') for row in rows)
    lines = []
    for row in rows:
        address = f'{row.address:x}'.ljust(width) + '  ' if show_addresses else ''
        text = ' '.join(row.text.splitlines())
        # Blank separator rows stay blank, without an address.
        lines.append((address + ' ' * (row.indent - indent) + text).rstrip() if text.strip()
                     else '')
    return lines


def fenced(lines: list[str], language: str = '') -> list[str]:
    """`lines` in a fenced code block whose fence no line can close."""
    longest = max((len(run) for line in lines for run in re.findall('`+', line)), default=0)
    fence = '`' * max(3, longest + 1)
    return [fence + language, *lines, fence]


def _code_span(text):
    """One code line inside a table cell, its pipes escaped. Code spans keep indentation."""
    if not text:
        return ''
    longest = max((len(run) for run in re.findall('`+', text)), default=0)
    ticks = '`' * (longest + 1)
    padding = ' ' if text.startswith('`') or text.endswith('`') else ''
    return ticks + padding + text.replace('|', '\\|') + padding + ticks


class _Positions:
    """Converts the analysis's UTF-16 positions to indices into the Python string."""

    def __init__(self, source):
        self.source = source
        self.wide = len(source.encode('utf-16-le')) // 2 != len(source)

    def __call__(self, position):
        if not self.wide:
            return position
        return len(self.source.encode('utf-16-le')[:position * 2].decode('utf-16-le'))


def _label(source, link, index):
    if link.label is None:
        return ''
    label = source[index(link.label[0]):index(link.label[1])]
    if link.expand_block:
        label = label.removeprefix('!')
    return label.strip()


def export_markdown(source: str, analysis: MarkdownAnalysis, previews: dict, *,
                    show_addresses: bool = True) -> str:
    """`source` with each embed in `previews` ({MarkdownLink: HLILLine}) replaced by its code.

    A link with a label keeps the label as text; the code follows as a fenced block, inside
    the same quote or list item. In a table cell, where a block cannot go, the code becomes
    one code span per line, joined by `<br>`. Links whose code isn't available stay links.
    """
    index = _Positions(source)
    shown = {link: preview for link, preview in previews.items()
             if preview is not None and preview.available}
    if not shown:
        return source
    tables = [(index(table.start_position), index(table.end_position))
              for table in analysis.tables]
    lines = source.split('\n')
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line) + 1
    by_line = {}
    for link in sorted(shown, key=lambda item: item.start):
        start, end = index(link.start), index(link.end)
        row = analysis.line_at_position(link.start)
        by_line.setdefault(row, []).append((start - starts[row], end - starts[row], link))
    output = []
    for row, line in enumerate(lines):
        links = by_line.get(row)
        if not links:
            output.append(line)
        elif any(left <= starts[row] + links[0][0] < right for left, right in tables):
            output.append(_table_line(source, line, links, shown, index, show_addresses))
        else:
            output.extend(_block_lines(source, line, links, shown, index, show_addresses))
    return '\n'.join(output)


def _table_line(source, line, links, shown, index, show_addresses):
    pieces, position = [], 0
    for start, end, link in links:
        pieces.append(line[position:start])
        label = _label(source, link, index)
        code = [_code_span(text) for text in code_lines(shown[link], show_addresses=show_addresses)]
        pieces.append('<br>'.join(([label.replace('|', '\\|')] if label else []) + code))
        position = end
    pieces.append(line[position:])
    return ''.join(pieces)


def _block_lines(source, line, links, shown, index, show_addresses):
    prefix = line[:min(_CONTAINERS.match(line).end(), links[0][0])]
    # Later lines of the same quote or list item: quote markers stay, list markers indent.
    continuation = _LIST_MARKER.sub(lambda match: ' ' * len(match[0]), prefix)
    output, lead, current, position = [], prefix, prefix, len(prefix)
    for start, end, link in links:
        text = line[position:start]
        current += text if current[len(lead):].strip() else text.lstrip()
        current += _label(source, link, index)
        if current[len(lead):].strip():
            output.append(current.rstrip())
            opening = continuation
        else:
            opening = lead  # The block starts the line, as the link did (`- ```c`).
        preview = shown[link]
        block = fenced(code_lines(preview, show_addresses=show_addresses),
                       LANGUAGES.get(preview.il_mode, ''))
        output.append(opening + block[0])
        output.extend((continuation + text).rstrip() for text in block[1:])
        lead = current = continuation
        position = end
    rest = line[position:]
    current += rest if current[len(lead):].strip() else rest.lstrip()
    if current[len(lead):].strip():
        output.append(current)
    return output
