from __future__ import annotations

import html
import pickle
import re
import struct
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from functools import cache, lru_cache
from threading import local
from urllib.parse import quote

from .links import INDENT_COLUMNS, HLILLine, HLILToken, ILMode
from .markdown import (
    MarkdownAnalysis,
    MarkdownParser,
    activate_runtime_packages,
    cell_display_source,
    install_inline_extensions,
    normalized_table_source,
)

PREVIEW_ANCHOR = 'bnnotes-hlil-preview:'


def display_rows(preview, *, show_addresses=True):
    rows = preview.display_lines
    if show_addresses:
        return rows
    header_count = len(preview.header_lines)
    return tuple(row if index < header_count else
                 replace(row, indent=max(0, row.indent - INDENT_COLUMNS))
                 for index, row in enumerate(rows))


@lru_cache(maxsize=1)
def _comment_parser():
    return MarkdownParser()


def _without_comments(source):
    if '%%' not in source:
        return source
    try:
        comments = [item for item in _comment_parser().parse(source).inline_formats
                    if item.role == 'comment']
    except (ImportError, RuntimeError, TypeError):
        return source
    encoded = source.encode('utf-16-le')
    for item in sorted(comments, key=lambda item: item.start, reverse=True):
        encoded = encoded[:item.start * 2] + encoded[item.end * 2:]
    return encoded.decode('utf-16-le')


def _block_source(parser, source):
    """Interpret a closing cell fence glued to prose without rewriting storage."""
    for _ in range(source.count('\n') + 2):
        tokens = parser.parse(source)
        lines = source.splitlines(keepends=True)
        repair = None
        for token in tokens:
            if token.type != 'fence' or token.map is None:
                continue
            marker = re.escape(token.markup[0])
            closing = re.compile(rf'^( {{0,3}}{marker}{{{len(token.markup)},}})([^\r\n]*)')
            for index in range(token.map[0] + 1, min(token.map[1], len(lines))):
                match = closing.match(lines[index])
                if match and match[2].strip():
                    repair = index, match
                    break
            if repair:
                break
        if repair is None:
            return source, tokens
        index, match = repair
        ending = '\n' if lines[index].endswith('\n') else ''
        lines[index] = match[1] + '\n' + match[2] + ending
        source = ''.join(lines)
    return source, parser.parse(source)


def _highlight_code(content, language, dark):
    content = content.expandtabs(4)
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound
    except ImportError:
        return html.escape(content)
    try:
        lexer = get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return html.escape(content)
    highlighted = highlight(content, lexer, HtmlFormatter(
        nowrap=True, noclasses=True, style='native' if dark else 'friendly',
    ))
    return highlighted if content.endswith('\n') else highlighted.removesuffix('\n')


@lru_cache(maxsize=2048)
def render_cell_html(source: str, dark=False, link_previews=(), preview_colors=(),
              show_addresses=True, object_placeholders=False) -> str:
    logical = _without_comments(cell_display_source(source))
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return html.escape(logical).replace('\n', '<br>')
    inline = MarkdownIt('commonmark', {'html': True, 'breaks': True})
    inline.enable('strikethrough')
    install_inline_extensions(inline)
    if link_previews:
        previews = {(inline.normalizeLink(url), preview.expand_block, preview.il_mode):
                    (index, preview) for index, (url, preview) in enumerate(link_previews)}
        modes = defaultdict(deque)
        for link in _comment_parser().parse(cell_display_source(source)).links:
            modes[inline.normalizeLink(link.url)].append((link.expand_block, link.il_mode))
        colors, targets, occurrence = dict(preview_colors), [], 0

        def link_open(items, index, options, env):
            token = items[index]
            href = token.attrGet('href')
            expand_block, il_mode = (modes[href].popleft() if modes[href]
                                     else (False, ILMode.HLIL))
            target = previews.get((href, expand_block, il_mode))
            end = next((i for i in range(index + 1, len(items)) if items[i].type == 'link_close'),
                       len(items))
            label = items[index + 1:end]
            text = (''.join(item.content for item in label)
                    if token.markup != 'autolink' else '')
            if expand_block:
                text = text.removeprefix('!').lstrip()
            targets.append((target, text if text.strip() else ''))
            if target is not None:
                for item in label:
                    item.type, item.content = 'text', ''
                return ''
            return inline.renderer.renderToken(items, index, options, env)

        def link_close(_items, _index, *_args):
            nonlocal occurrence
            target, label = targets.pop() if targets else (None, '')
            if target is None:
                return '</a>'
            preview_index, preview = target
            marker = f'{PREVIEW_ANCHOR}{preview_index}:{occurrence}:{quote(label, safe="")}'
            occurrence += 1
            if object_placeholders:
                return f'<a href="{marker}">\ufffc</a>'
            pieces = []
            for row in display_rows(preview, show_addresses=show_addresses):
                if pieces:
                    pieces.append('<br>')
                prefix = (f'{row.address:x}\t' if show_addresses else '') + ' ' * row.indent
                for token in (HLILToken(prefix, 68), *row.tokens):
                    rgba = colors.get(token.kind if preview.available else -1)
                    text = html.escape(token.text)
                    if rgba is not None:
                        text = f'<span style="color: #{rgba & 0xffffff:06x}">{text}</span>'
                    pieces.append(text)
            return (f'<a href="{marker}" style="text-decoration: none">'
                    '<code style="background-color: transparent; font-weight: normal">'
                    '&nbsp;' + ''.join(pieces) + '&nbsp;</code></a>')

        inline.renderer.rules['link_open'] = link_open
        inline.renderer.rules['link_close'] = link_close
    inline.renderer.rules['image'] = lambda tokens, index, *_: html.escape(tokens[index].content)
    inline.renderer.rules['html_inline'] = lambda tokens, index, *_: (
        '<br>' if re.fullmatch(r'<br\s*/?>', tokens[index].content, re.IGNORECASE)
        else html.escape(tokens[index].content)
    )
    inline.renderer.rules['html_block'] = lambda tokens, index, *_: html.escape(
        tokens[index].content,
    )
    logical, tokens = _block_source(MarkdownIt('commonmark', {'html': False}), logical)
    lines = logical.splitlines(keepends=True)
    if any(token.type == 'blockquote_open' for token in tokens):
        def quoted_code(items, index, *_args):
            token = items[index]
            language = token.info.strip().split()[0] if token.info.strip() else 'text'
            content = token.content.removesuffix('\n')
            return '<pre class="cell-code"><code>' + _highlight_code(
                content, language, dark,
            ) + '</code></pre>'
        inline.renderer.rules['fence'] = quoted_code
        inline.renderer.rules['code_block'] = quoted_code
        return inline.render(logical)
    pieces, position = [], 0

    def prose(text):
        if text:
            structured = any(token.type in ('heading_open', 'bullet_list_open',
                             'ordered_list_open', 'blockquote_open')
                             for token in inline.parse(text))
            body = inline.render(text) if structured else inline.renderInline(text)
            pieces.append('<div class="cell-prose">' + body + '</div>')

    for token in tokens:
        if token.type not in ('fence', 'code_block') or token.map is None:
            continue
        start, end = token.map
        if start < position:
            continue
        prose(''.join(lines[position:start]))
        content = token.content
        if token.type == 'fence':
            marker = re.escape(token.markup[0])
            closed = end <= len(lines) and re.fullmatch(
                rf' {{0,3}}{marker}{{{len(token.markup)},}}[ \t]*\n?', lines[end - 1],
            )
            if closed or end >= len(lines) and not logical.endswith('\n'):
                content = content.removesuffix('\n')
        elif end >= len(lines) and not logical.endswith('\n'):
            content = content.removesuffix('\n')
        language = token.info.strip().split()[0] if token.info.strip() else 'text'
        code = _highlight_code(content, language, dark)
        pieces.append(f'<pre class="cell-code"><code>{code}</code></pre>')
        position = end
    prose(''.join(lines[position:]))
    return ''.join(pieces)


_local = local()


class LazyMarkdownParser:
    """An empty editable note must not import/initialize grammars just to open."""

    def __init__(self, factory=MarkdownParser):
        self.factory = factory
        self.parser = None

    def _get(self):
        if self.parser is None:
            self.parser = self.factory()
        return self.parser

    def parse(self, source):
        return self._get().parse(source)

    def parse_inline(self, source):
        return self._get().parse_inline(source)


@dataclass(frozen=True)
class PreparedMarkdown:
    source: str
    text: str
    analysis: MarkdownAnalysis
    warning: str = ''
    cells: tuple = ()


def prepare_markdown(source, *, inline=False, normalize=False, cells=False,
                     parser_factory=MarkdownParser):
    try:
        parser = getattr(_local, 'parser', None)
        if parser is None or getattr(_local, 'factory', None) is not parser_factory:
            parser = _local.parser = parser_factory()
            _local.factory = parser_factory
        text = normalized_table_source(source, parser) if normalize and '|' in source else source
        analysis = parser.parse_inline(text) if inline else parser.parse(text)
        prepared_cells = ()
        if cells and analysis.tables:

            values = dict.fromkeys(value for table in analysis.tables
                                   for row in table.rows for value in row)
            prepared_cells = tuple((value, prepare_cell(value, False), prepare_cell(value, True))
                                   for value in values)
        return PreparedMarkdown(source, text, analysis, cells=prepared_cells)
    except ImportError as error:
        return PreparedMarkdown(source, source, MarkdownAnalysis(),
                                'Plain text mode: install requirements.txt in Binary Ninja. '
                                f'({error})')
    except Exception as error:
        return PreparedMarkdown(source, source, MarkdownAnalysis(),
                                f'Markdown formatting unavailable: {error}')


def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise EOFError('Markdown parser connection closed')
        result.extend(part)
    return bytes(result)


def serve():
    """The parser process's loop: length-prefixed pickled requests on stdin, results on stdout."""
    activate_runtime_packages()
    while True:
        try:
            size = struct.unpack('!I', read_exact(sys.stdin.buffer, 4))[0]
        except EOFError:
            return
        request = pickle.loads(read_exact(sys.stdin.buffer, size))
        payload = pickle.dumps(prepare_markdown(**request), protocol=4)
        sys.stdout.buffer.write(struct.pack('!I', len(payload)) + payload)
        sys.stdout.buffer.flush()


_templates = {}
_objects = re.compile(r'<a href="bnnotes-hlil-preview:(\d+):(\d+):[^\"]*">\ufffc</a>')
_anchors = re.compile(r'<a href="([^\"]*)">.*?</a>', re.DOTALL)


@dataclass(frozen=True)
class CellTemplate:
    html: str
    targets: tuple
    replacements: tuple

    def render(self, previews):
        indices = {(url, preview.expand_block, preview.il_mode): index
                   for index, (url, preview) in enumerate(previews)}
        result = self.html
        for marker, target, old_index, fallback in self.replacements:
            index = indices.get(target)
            replacement = (fallback if index is None else marker.replace(
                f'bnnotes-hlil-preview:{old_index}:', f'bnnotes-hlil-preview:{index}:', 1,
            ))
            result = result.replace(marker, replacement, 1)
        return result


@cache
def prepare_cell(source, dark):

    targets = tuple(dict.fromkeys((link.url, link.expand_block, link.il_mode)
                                 for link in _comment_parser().parse(
                                     cell_display_source(source),
                                 ).links if link.url.lower().startswith('binaryninja:')))
    plain = render_cell_html(source, dark)
    if not targets:
        return CellTemplate(plain, (), ())
    previews = tuple((url, HLILLine(0, '', expand_block=expanded, il_mode=mode))
                     for url, expanded, mode in targets)
    projected = render_cell_html(source, dark, link_previews=previews, object_placeholders=True)
    markers = tuple(_objects.finditer(projected))
    original = tuple(match.group() for match in _anchors.finditer(plain)
                     if html.unescape(match[1]).lower().startswith('binaryninja:'))
    if len(markers) != len(original):
        return CellTemplate(plain, targets, ())
    replacements = tuple((marker.group(), targets[int(marker[1])], int(marker[1]), fallback)
                         for marker, fallback in zip(markers, original, strict=True))
    return CellTemplate(projected, targets, replacements)


def install_templates(cells):
    for source, light, dark in cells:
        _templates[source, False] = light
        _templates[source, True] = dark


def prepared_html(source, dark, previews):
    template = _templates.get((source, dark))
    return template.render(previews) if template is not None else None


def prepared_targets(source):
    template = _templates.get((source, False))
    return template.targets if template is not None else None


def cell_html(source, dark=False, link_previews=(), preview_colors=(), show_addresses=True,
              object_placeholders=False):
    """A cell's HTML, from its prepared template when there is one."""
    if object_placeholders:
        prepared = prepared_html(source, dark, link_previews)
        if prepared is not None:
            return prepared
    return render_cell_html(source, dark, link_previews, preview_colors, show_addresses,
                            object_placeholders)
