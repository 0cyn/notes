from __future__ import annotations

import csv
import html
import io
import platform
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .links import ILMode, il_mode_for_title


def runtime_directory() -> Path:
    tag = f"{sys.implementation.cache_tag}-{sys.platform}-{platform.machine()}"
    return Path(__file__).resolve().parents[1] / ".runtime" / tag


def activate_runtime_packages() -> None:
    directory = runtime_directory()
    if directory.is_dir() and str(directory) not in sys.path:
        # Packages installed in Binary Ninja take precedence; this holds runtime requirements only.
        sys.path.append(str(directory))


@dataclass(frozen=True, slots=True)
class Extension:
    role: str
    start: int
    content_start: int
    content_end: int
    end: int


def _escaped(source, position):
    slash = b"\\" if isinstance(source, bytes) else "\\"
    first = position
    while first > 0 and source[first - 1:first] == slash:
        first -= 1
    return (position - first) % 2 != 0


def extension_at(source, position, protected=(), *, math=False):
    binary = isinstance(source, bytes)

    def literal(text):
        return text.encode() if binary else text

    if _escaped(source, position) or any(left <= position < right for left, right in protected):
        return None
    pairs = [("%%", "%%", "comment"), ("==", "==", "highlight"), ("[[", "]]", "wikilink")]
    if math:
        pairs.extend((("$$", "$$", "math"), ("$", "$", "math")))
    for opening, closing, role in pairs:
        opening, closing = literal(opening), literal(closing)
        if not source.startswith(opening, position):
            continue
        inner = position + len(opening)
        end = source.find(closing, inner)
        while end >= 0 and (_escaped(source, end) or role != "comment" and any(
            left <= end < right for left, right in protected
        )):
            end = source.find(closing, end + len(closing))
        if end < 0:
            return None
        if role == "wikilink":
            if literal("\n") in source[inner:end]:
                return None
            alias = inner
            for match in re.finditer(literal("\\|"), source[inner:end]):
                pipe = inner + match.start()
                if not _escaped(source, pipe):
                    alias = pipe + 1
            inner = alias
        return Extension(role, position, inner, end, end + len(closing))
    return None


def extension_spans(source, protected=()):
    """Use byte or character offsets according to the supplied source type."""
    pattern = rb"%%|==|\[\[" if isinstance(source, bytes) else r"%%|==|\[\["
    skip_to, closing_markers = 0, set()
    for match in re.finditer(pattern, source):
        position = match.start()
        if position < skip_to or position in closing_markers:
            continue
        item = extension_at(source, position, protected)
        if item is None:
            continue
        yield item
        closing_markers.add(item.content_end)
        if item.role in ("comment", "wikilink"):
            skip_to = item.end


def install_inline_extensions(parser):
    """Add safe preview rendering without allowing raw HTML or resource loads."""
    def rule(state, silent):
        item = extension_at(state.src, state.pos, math=True)
        if item is None:
            return False
        if not silent:
            token = state.push("bnnotes_" + item.role, "", 0)
            token.content = state.src[item.content_start:item.content_end]
            if item.role in ("highlight", "wikilink"):
                token.children = []
                state.md.inline.parse(token.content, state.md, state.env, token.children)
        state.pos = item.end
        return True

    def render(tokens, index, options, env):
        token = tokens[index]
        if token.type == "bnnotes_comment":
            return ""
        if token.type == "bnnotes_math":
            return '<code class="cell-math">' + html.escape(token.content) + "</code>"
        body = parser.renderer.renderInline(token.children, options, env)
        if token.type == "bnnotes_highlight":
            return '<span style="background-color: #b79530; color: #151515;">' + body + "</span>"
        return '<span class="cell-wikilink">' + body + "</span>"

    parser.inline.ruler.before("emphasis", "bnnotes_extensions", rule)
    for role in ("comment", "highlight", "math", "wikilink"):
        parser.renderer.rules["bnnotes_" + role] = render


_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_CELL_WHITESPACE = re.compile(r"&#(?:0*(9|32)|x0*(9|20));", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MarkdownTable:
    start_position: int
    end_position: int
    start_line: int
    end_line: int
    rows: tuple[tuple[str, ...], ...]
    alignments: tuple[str, ...]
    cell_ranges: tuple[tuple[tuple[int, int], ...], ...] = ()
    prefix: str = ""
    continuation_prefix: str = ""


def split_table_row(source: str) -> list[tuple[str, int, int]]:
    """Locate raw cells inside a row already recognized by the GFM parser."""
    boundaries = []
    slashes = 0
    for position, char in enumerate(source):
        if char == "|" and slashes % 2 == 0:
            boundaries.append(position)
        slashes = slashes + 1 if char == "\\" else 0
    edges = [-1, *boundaries, len(source)]
    ranges = [(edges[index] + 1, edges[index + 1]) for index in range(len(edges) - 1)]
    if boundaries and not source[:boundaries[0]].strip():
        ranges = ranges[1:]
    if boundaries and not source[boundaries[-1] + 1:].strip():
        ranges = ranges[:-1]
    result = []
    for start, end in ranges:
        raw = source[start:end]
        left = start + len(raw) - len(raw.lstrip())
        value = raw.strip()
        result.append((value, left, left + len(value)))
    return result


def encode_cell(value: str) -> str:
    """Keep Markdown, whitespace, and line breaks inside a single pipe-table row."""
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u2028", "\n").replace("\u2029", "\n")
    value = value.replace("\n", "<br>").replace("\t", "&#9;")
    value = re.sub(r"^ +| +$", lambda match: "&#32;" * len(match[0]), value)
    return re.sub(
        r"(\\*)\|",
        lambda match: match[1] + ("\\" if len(match[1]) % 2 == 0 else "") + "|",
        value,
    )


@lru_cache(maxsize=2048)
def cell_editor_text(value: str) -> str:
    # Inline code may contain a literal '<br>'; line-leading fences use it to encode code lines.
    protected = []
    if "`" in value and _BREAK.search(value):

        analysis = MarkdownParser().parse_inline(value)
        encoded = value.encode("utf-16-le")
        for row, spans in enumerate(analysis.lines):
            for span in spans:
                if span.role != "code":
                    continue
                offset = analysis.line_starts[row] + span.start
                start = len(encoded[:offset * 2].decode("utf-16-le"))
                end = len(encoded[:(offset + span.length) * 2].decode("utf-16-le"))
                run = len(value[start:end]) - len(value[start:end].lstrip("`"))
                prefix = _BREAK.split(value[:start])[-1].rsplit("\n", 1)[-1]
                container_prefix = re.fullmatch(r"[ \t]*(?:(?:>|[-+*]|\d+[.)])[ \t]+|>)*", prefix)
                if run >= 3 and container_prefix:
                    continue
                protected.append((start, end))
    text = _BREAK.sub(
        lambda match: match[0] if any(start <= match.start() < end for start, end in protected)
        else "\n",
        value,
    )
    return _CELL_WHITESPACE.sub(
        lambda match: "\t" if (match[1] or match[2]).lower() == "9" else " ", text
    )


def cell_display_source(value: str) -> str:
    # GFM consumes the pipe's table-level escape before parsing the inline text.
    return cell_editor_text(value).replace(r"\|", "|")


def serialize_table(rows, alignments=(), *, prefix="", continuation_prefix="") -> str:
    if not rows or not rows[0]:
        raise ValueError("A Markdown table needs a header and at least one column.")
    columns = max(len(row) for row in rows)
    cells = [[encode_cell(value) for value in row] + [""] * (columns - len(row)) for row in rows]
    widths = [min(40, max(3, *(len(row[col]) for row in cells))) for col in range(columns)]

    def render(row):
        return "| " + " | ".join(value.ljust(widths[col]) for col, value in enumerate(row)) + " |"

    delimiters = []
    for col, width in enumerate(widths):
        alignment = alignments[col] if col < len(alignments) else ""
        if alignment == "center":
            delimiters.append(":" + "-" * max(3, width - 2) + ":")
        elif alignment == "right":
            delimiters.append("-" * max(3, width - 1) + ":")
        elif alignment == "left":
            delimiters.append(":" + "-" * max(3, width - 1))
        else:
            delimiters.append("-" * width)
    lines = [prefix + render(cells[0]), continuation_prefix + render(delimiters)]
    lines.extend(continuation_prefix + render(row) for row in cells[1:])
    return "\n".join(lines)


def new_table(rows: int, columns: int) -> str:
    if not 1 <= rows <= 1000 or not 1 <= columns <= 100:
        raise ValueError("Choose 1–100 columns and 1–1000 rows, including the header.")
    values = [[f"Column {col + 1}" for col in range(columns)]]
    values.extend([""] * columns for _ in range(rows - 1))
    return serialize_table(values)


def clipboard_matrix(text: str) -> tuple[list[list[str]], tuple[str, ...]]:
    """Accept a complete Markdown table, spreadsheet TSV, or a single text cell."""

    parsed = MarkdownParser().parse(text)
    if len(parsed.tables) == 1:
        table = parsed.tables[0]
        encoded = text.encode("utf-16-le")
        before = encoded[:table.start_position * 2].decode("utf-16-le")
        after = encoded[table.end_position * 2:].decode("utf-16-le")
        if not before.strip() and not after.strip():
            return [list(row) for row in table.rows], table.alignments
    if "\t" in text:
        return list(csv.reader(io.StringIO(text), delimiter="\t")), ()
    return [[text]], ()


_INLINE_MARKER_PARENTS = {
    "inline_link": frozenset("[]()"),
    "image": frozenset("![]()"),
    "full_reference_link": frozenset("[]"),
    "collapsed_reference_link": frozenset("[]"),
    "shortcut_link": frozenset("[]"),
    "link_label": frozenset("[]"),
}


@dataclass(frozen=True, slots=True)
class Span:
    start: int  # UTF-16 column within a QTextBlock
    length: int
    role: str


@dataclass(frozen=True, slots=True)
class InlineFormat:
    """One parsed wrapper, with absolute UTF-16 source/inner boundaries."""

    role: str
    start: int
    content_start: int
    content_end: int
    end: int


@dataclass(frozen=True, slots=True)
class MarkdownLink:
    url: str
    start: int  # Absolute UTF-16 source positions, including the Markdown wrapper.
    end: int
    expand_block: bool = False
    il_mode: ILMode = ILMode.HLIL
    # Source ranges for rewriting one part of an inline link; autolinks have none.
    label: tuple[int, int] | None = None  # Between the brackets, including any "!".
    destination_end: int | None = None    # Just after the URL (or its closing ">").
    title: tuple[int, int] | None = None  # Including the title's delimiters.


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    title: str
    line: int  # zero-based source line
    position: int = 0  # absolute UTF-16 position in QTextDocument


@dataclass(frozen=True, slots=True)
class CodeBlock:
    start_line: int
    end_line: int  # exclusive, including the closing fence when present
    language: str = ""
    start_position: int = 0
    end_position: int = 0  # end of the last source line, before its newline


@dataclass(frozen=True, slots=True)
class QuoteLine:
    line: int
    depth: int
    start: int
    end: int
    markers: tuple[tuple[int, int], ...] = ()

    @property
    def content_start(self):
        return self.markers[-1][1] if self.markers else self.start


@dataclass(frozen=True, slots=True)
class MarkdownAnalysis:
    lines: tuple[tuple[Span, ...], ...] = ()
    headings: tuple[Heading, ...] = ()
    code_lines: frozenset[int] = frozenset()
    code_blocks: tuple[CodeBlock, ...] = ()
    line_starts: tuple[int, ...] = ()
    line_ends: tuple[int, ...] = ()
    tables: tuple[MarkdownTable, ...] = ()
    inline_formats: tuple[InlineFormat, ...] = ()
    quote_lines: tuple[QuoteLine, ...] = ()
    links: tuple[MarkdownLink, ...] = ()

    def line_at_position(self, position: int) -> int:
        return max(0, bisect_right(self.line_starts, position) - 1)

    def spans_for_range(self, start: int, end: int) -> list[Span]:
        """Spans of a source range within one Qt paragraph (soft breaks included)."""
        result = []
        row = self.line_at_position(start)
        while row < len(self.line_starts) and self.line_starts[row] < end:
            offset = self.line_starts[row] - start
            for span in self.lines[row]:
                left = max(0, offset + span.start)
                right = min(end - start, offset + span.start + span.length)
                if left < right:
                    result.append(Span(left, right - left, span.role))
            row += 1
        return result


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def source_line_positions(source: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    starts, ends = [], []
    position = 0
    for line in source.split("\n"):
        starts.append(position)
        position += utf16_length(line)
        ends.append(position)
        position += 1
    return tuple(starts), tuple(ends)


class _SourceMap:
    def __init__(self, source: str):
        self.data = source.encode("utf-8")
        self.rows = self.data.split(b"\n")
        self.starts: list[int] = []
        self.line_starts, self.line_ends = source_line_positions(source)
        offset = 0
        for row in self.rows:
            self.starts.append(offset)
            offset += len(row) + 1
        self.spans: list[list[Span]] = [[] for _ in self.rows]
        self.inline_formats: list[InlineFormat] = []
        self.links: list[MarkdownLink] = []
        self.protected: list[tuple[int, int]] = []

    def position(self, byte_offset: int) -> int:
        row = bisect_right(self.starts, byte_offset) - 1
        return self.line_starts[row] + utf16_length(
            self.data[self.starts[row]:byte_offset].decode("utf-8")
        )

    def add(self, start: int, end: int, role: str) -> None:
        row = bisect_right(self.starts, start) - 1
        while row < len(self.rows) and self.starts[row] < end:
            line_start = self.starts[row]
            left = max(0, start - line_start)
            right = min(len(self.rows[row]), end - line_start)
            if left < right:
                column = utf16_length(self.rows[row][:left].decode("utf-8"))
                length = utf16_length(self.rows[row][left:right].decode("utf-8"))
                self.spans[row].append(Span(column, length, role))
            row += 1


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


@lru_cache(maxsize=128)
def _code_tokens(source: str, language: str) -> tuple[tuple[int, int, str], ...]:
    try:
        from pygments.lexers import get_lexer_by_name
        from pygments.token import Token
        from pygments.util import ClassNotFound
    except ImportError:
        return ()
    try:
        lexer = get_lexer_by_name(language)
    except ClassNotFound:
        return ()

    # Pygments reports Python character offsets, not UTF-8 byte offsets.
    offsets = [0]
    for char in source:
        offsets.append(offsets[-1] + len(char.encode("utf-8")))
    roles = (
        (Token.Comment, "syntax_comment"),
        (Token.Keyword, "syntax_keyword"),
        (Token.Literal.String, "syntax_string"),
        (Token.Literal.Number, "syntax_number"),
        (Token.Name.Function, "syntax_function"),
        (Token.Name.Class, "syntax_type"),
        (Token.Name.Builtin, "syntax_type"),
        (Token.Operator, "syntax_operator"),
    )
    result = []
    for offset, token, value in lexer.get_tokens_unprocessed(source):
        for family, role in roles:
            if token in family:
                result.append((offsets[offset], offsets[offset + len(value)], role))
                break
    return tuple(result)


class MarkdownParser:
    def __init__(self):
        # Lazy imports let the editor remain usable when dependencies are missing.
        activate_runtime_packages()
        import tree_sitter_markdown
        from markdown_it import MarkdownIt
        from tree_sitter import Language, Parser

        self._blocks = Parser(Language(tree_sitter_markdown.language()))
        self._inlines = Parser(Language(tree_sitter_markdown.inline_language()))
        self._tables = MarkdownIt("commonmark", {"html": False}).enable("table")

    def parse(self, source: str) -> MarkdownAnalysis:
        mapped = _SourceMap(source)
        # An EOF heading stays incomplete until a line terminator: parse with one, clipped below.
        block_source = mapped.data if mapped.data.endswith(b"\n") else mapped.data + b"\n"
        tree = self._blocks.parse(block_source)
        headings: list[Heading] = []
        code_lines: set[int] = set()
        code_blocks: list[CodeBlock] = []
        tables: list[MarkdownTable] = []
        # Inline punctuation is handled only by the inline grammar.
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            kind = node.type
            start, end = node.start_byte, min(node.end_byte, len(mapped.data))
            if kind in ("inline", "pipe_table_cell"):
                self._add_inline(mapped, start, end)
                continue
            if kind in ("atx_heading", "setext_heading"):
                marker = next(
                    (child for child in node.children if "marker" in child.type
                     or "underline" in child.type),
                    None,
                )
                level = int(re.search(r"h([1-6])", marker.type)[1]) if marker else 1
                mapped.add(start, end, f"heading{level}")
                inline = next((child for child in _walk(node) if child.type == "inline"), None)
                title = mapped.data[inline.start_byte:inline.end_byte].decode() if inline else ""
                title = re.sub(r"[ \t]+#+[ \t]*$", "", title).strip()
                headings.append(Heading(
                    level, title, node.start_point.row,
                    mapped.line_starts[node.start_point.row],
                ))
                if inline and kind == "atx_heading":
                    closing = re.search(
                        rb"[ \t]+(#+)[ \t]*$",
                        mapped.data[inline.start_byte:end].rstrip(b"\n"),
                    )
                    if closing:
                        mapped.add(inline.start_byte + closing.start(1),
                                   inline.start_byte + closing.end(1), "marker")
            elif kind in ("fenced_code_block", "indented_code_block"):
                mapped.protected.append((start, end))
                first = node.start_point.row
                last = min(
                    len(mapped.rows), node.end_point.row + (1 if node.end_point.column else 0)
                )
                fences = [child for child in node.children
                          if child.type == "fenced_code_block_delimiter"]
                if kind == "fenced_code_block" and len(fences) == 1 and end == len(mapped.data):
                    # Include the empty final line of an unfinished fence.
                    last = len(mapped.rows)
                # The whole line is code, including indentation and quote/list prefixes.
                mapped.add(
                    mapped.starts[first], mapped.starts[last - 1] + len(mapped.rows[last - 1]),
                    "code_block",
                )
                name = ""
                if kind == "fenced_code_block":
                    language = next(
                        (child for child in _walk(node) if child.type == "language"), None
                    )
                    content = next(
                        (child for child in node.children if child.type == "code_fence_content"),
                        None,
                    )
                    if language is not None:
                        name = mapped.data[language.start_byte:language.end_byte].decode()
                    if name and content is not None:
                        code = mapped.data[content.start_byte:content.end_byte].decode()
                        for left, right, role in _code_tokens(code, name):
                            mapped.add(content.start_byte + left, content.start_byte + right, role)
                    for child in node.children:
                        if child.type in ("fenced_code_block_delimiter", "info_string"):
                            mapped.add(child.start_byte, child.end_byte, "fence_marker")
                code_lines.update(range(first, last))
                code_blocks.append(CodeBlock(
                    first, last, name, mapped.line_starts[first], mapped.line_ends[last - 1]
                ))
                # Markdown-looking punctuation in code is not Markdown.
                continue
            elif kind == "block_quote":
                mapped.add(start, end, "quote")
            elif kind == "pipe_table":
                mapped.add(start, end, "table")
                tables.append(self._table(mapped, node))
            elif kind == "pipe_table_header":
                mapped.add(start, end, "strong")
            elif kind == "task_list_marker_checked":
                mapped.add(start, end, "checked")
            elif (
                kind.startswith(("atx_h", "setext_h", "list_marker_"))
                or kind in (
                    "block_quote_marker", "block_continuation", "task_list_marker_unchecked",
                    "fenced_code_block_delimiter", "info_string", "thematic_break",
                    "pipe_table_delimiter_row", "|", "html_block",
                )
            ):
                mapped.add(start, end, "marker")
            stack.extend(reversed(node.children))

        comments = self._add_extensions(mapped)

        def visible(position):
            return not any(left <= position < right for left, right in comments)

        containers = self._tables.parse(source) if ">" in source or "|" in source else ()
        quote_lines = self._quote_lines(mapped, tree, visible, containers)
        return MarkdownAnalysis(
            lines=tuple(tuple(row) for row in mapped.spans),
            headings=tuple(item for item in headings if visible(item.position)),
            code_lines=frozenset(row for row in code_lines if visible(mapped.line_starts[row])),
            code_blocks=tuple(item for item in code_blocks if visible(item.start_position)),
            line_starts=tuple(mapped.line_starts),
            line_ends=tuple(mapped.line_ends),
            tables=tuple(item for item in self._parse_tables(source, mapped, tables, containers)
                         if visible(item.start_position)),
            inline_formats=tuple(mapped.inline_formats),
            quote_lines=quote_lines,
            links=tuple(link for link in mapped.links if visible(link.start)),
        )

    def _quote_lines(self, mapped, tree, visible, containers):
        depths, markers = {}, {}
        # Container maps tell a quoted blank line closing a nested quote from a lazy continuation.
        for token in containers:
            if token.type != "blockquote_open" or token.map is None:
                continue
            first, end = token.map
            if not visible(mapped.line_starts[first]):
                continue
            for row in range(first, min(end, len(mapped.rows))):
                depths[row] = depths.get(row, 0) + 1
        for node in _walk(tree.root_node):
            if node.type in ("block_quote_marker", "block_continuation"):
                raw = mapped.data[node.start_byte:node.end_byte]
                for match in re.finditer(rb">[ \t]?", raw):
                    left, right = node.start_byte + match.start(), node.start_byte + match.end()
                    row = bisect_right(mapped.starts, left) - 1
                    markers.setdefault(row, set()).add((left, right))
        result = []
        for row, depth in sorted(depths.items()):
            ranges = sorted(markers.get(row, ()))
            # Continuation markers can sit inside nodes the block walk does not descend into.
            for left, right in ranges:
                mapped.add(left, right, "quote_marker")
            result.append(QuoteLine(
                row, depth, mapped.line_starts[row], mapped.line_ends[row],
                tuple((mapped.position(left), mapped.position(right)) for left, right in ranges),
            ))
        return tuple(result)

    def _parse_tables(self, source, mapped, tree_tables, containers):
        if "|" not in source:
            return ()
        result = []
        prefixes = {table.start_line: table for table in tree_tables}
        # markdown-it decides GFM table boundaries (tree-sitter misreads empty body rows).
        for token in containers:
            if token.type != "table_open" or token.map is None:
                continue
            first, end_line = token.map
            end_line = min(end_line, len(mapped.rows))
            reference = prefixes.get(first)
            prefix = reference.prefix if reference else ""
            continuation = reference.continuation_prefix if reference else prefix
            rows, ranges, alignments = [], [], []
            for row_number in range(first, end_line):
                text = mapped.rows[row_number].decode("utf-8")
                lead = prefix if row_number == first else continuation
                if not text.startswith(lead):
                    lead = ""
                content = text[len(lead):]
                cells = split_table_row(content)
                if row_number == first + 1:
                    for value, _left, _right in cells:
                        left, right = value.startswith(":"), value.endswith(":")
                        alignment = "center" if left and right else (
                            "left" if left else "right" if right else ""
                        )
                        alignments.append(alignment)
                    continue
                rows.append([value for value, _, _ in cells])
                ranges.append([
                    (mapped.line_starts[row_number] + utf16_length(lead + content[:left]),
                     mapped.line_starts[row_number] + utf16_length(lead + content[:right]))
                    for _, left, right in cells
                ])
            columns = max(1, *(len(row) for row in rows))
            for row, positions in zip(rows, ranges, strict=True):
                end = positions[-1][1] if positions else mapped.line_starts[first]
                positions.extend([(end, end)] * (columns - len(row)))
                row.extend([""] * (columns - len(row)))
            alignments.extend([""] * (columns - len(alignments)))
            result.append(MarkdownTable(
                mapped.line_starts[first], mapped.line_ends[end_line - 1], first, end_line,
                tuple(tuple(row) for row in rows), tuple(alignments),
                tuple(tuple(row) for row in ranges), prefix, continuation,
            ))
        return tuple(result)

    def parse_inline(self, source: str) -> MarkdownAnalysis:
        mapped = _SourceMap(source)
        self._add_inline(mapped, 0, len(mapped.data))
        comments = self._add_extensions(mapped)
        return MarkdownAnalysis(
            lines=tuple(tuple(row) for row in mapped.spans),
            line_starts=tuple(mapped.line_starts), line_ends=tuple(mapped.line_ends),
            inline_formats=tuple(mapped.inline_formats),
            links=tuple(link for link in mapped.links
                        if not any(left <= link.start < right for left, right in comments)),
        )

    def _add_extensions(self, mapped):
        items = list(extension_spans(mapped.data, mapped.protected))
        comments = [(mapped.position(item.start), mapped.position(item.end))
                    for item in items if item.role == "comment"]
        if comments:
            # Comment contents get no formatting, headings, tables or code frames of their own.
            for row, spans in enumerate(mapped.spans):
                offset = mapped.line_starts[row]
                clipped = []
                for span in spans:
                    parts = [(offset + span.start, offset + span.start + span.length)]
                    for left, right in comments:
                        parts = [(a, b) for start, end in parts for a, b in (
                            (start, min(end, left)), (max(start, right), end),
                        ) if a < b]
                    clipped.extend(Span(start - offset, end - start, span.role)
                                   for start, end in parts)
                mapped.spans[row] = clipped
            mapped.inline_formats = [item for item in mapped.inline_formats if not any(
                left <= item.start < right for left, right in comments
            )]
        for item in items:
            role = "link" if item.role == "wikilink" else item.role
            mapped.add(item.start, item.end, role)
            mapped.add(item.start, item.content_start, "marker")
            mapped.add(item.content_end, item.end, "marker")
            positions = (mapped.position(position) for position in (
                item.start, item.content_start, item.content_end, item.end,
            ))
            mapped.inline_formats.append(InlineFormat(role, *positions))
        return comments

    def _table(self, mapped: _SourceMap, node) -> MarkdownTable:
        row_nodes = [child for child in node.children
                     if child.type in ("pipe_table_header", "pipe_table_row")]
        delimiter = next(child for child in node.children
                         if child.type == "pipe_table_delimiter_row")
        rows, ranges = [], []
        for row in row_nodes:
            cells, cell_ranges = [], []
            for cell in row.children:
                if cell.type != "pipe_table_cell":
                    continue
                raw = mapped.data[cell.start_byte:cell.end_byte]
                trimmed = raw.strip(b" \t")
                left = cell.start_byte + len(raw) - len(raw.lstrip(b" \t"))
                line = cell.start_point.row
                position = mapped.line_starts[line] + utf16_length(
                    mapped.data[mapped.starts[line]:left].decode("utf-8")
                )
                value = trimmed.decode("utf-8")
                cells.append(value)
                cell_ranges.append((position, position + utf16_length(value)))
            rows.append(cells)
            ranges.append(cell_ranges)
        columns = max(1, *(len(row) for row in rows))
        for index, row in enumerate(rows):
            end = mapped.line_ends[row_nodes[index].start_point.row]
            ranges[index].extend([(end, end)] * (columns - len(row)))
            row.extend([""] * (columns - len(row)))
        alignments = []
        for cell in delimiter.children:
            if cell.type != "pipe_table_delimiter_cell":
                continue
            markers = {child.type for child in cell.children}
            left = "pipe_table_align_left" in markers
            right = "pipe_table_align_right" in markers
            alignment = "center" if left and right else (
                "left" if left else "right" if right else ""
            )
            alignments.append(alignment)
        alignments.extend([""] * (columns - len(alignments)))
        first = row_nodes[0].start_point.row
        last = max(row_nodes[-1].start_point.row, delimiter.start_point.row)
        prefix = mapped.data[mapped.starts[first]:row_nodes[0].start_byte].decode("utf-8")
        continuation = mapped.data[
            mapped.starts[delimiter.start_point.row]:delimiter.start_byte
        ].decode("utf-8")
        return MarkdownTable(
            mapped.line_starts[first], mapped.line_ends[last], first, last + 1,
            tuple(tuple(row) for row in rows), tuple(alignments),
            tuple(tuple(row) for row in ranges), prefix, continuation,
        )

    def _add_inline(self, mapped: _SourceMap, start: int, end: int) -> None:
        tree = self._inlines.parse(mapped.data[start:end])
        styles = {
            "strong_emphasis": "strong",
            "emphasis": "emphasis",
            "strikethrough": "strike",
            "code_span": "code",
            "latex_block": "math",
            "link_text": "link",
            "image_description": "link",
            "uri_autolink": "link",
            "email_autolink": "link",
            "link_destination": "destination",
            "link_title": "marker",
            "emphasis_delimiter": "marker",
            "code_span_delimiter": "marker",
            "latex_span_delimiter": "marker",
            "html_tag": "marker",
            "backslash_escape": "escape",
            "entity_reference": "escape",
            "numeric_character_reference": "escape",
        }
        for node in _walk(tree.root_node):
            role = styles.get(node.type)
            if node.type in ("code_span", "latex_block", "link_destination", "html_tag"):
                mapped.protected.append((start + node.start_byte, start + node.end_byte))
            if node.type in ("emphasis", "strong_emphasis", "strikethrough", "code_span",
                             "latex_block"):
                markers = [child for child in node.children if child.type in (
                    "emphasis_delimiter", "code_span_delimiter", "latex_span_delimiter",
                )]
                if len(markers) >= 2:
                    width = (2 if node.type == "strong_emphasis"
                             else markers[0].end_byte - markers[0].start_byte)
                    left, right = markers[0].start_byte, markers[-1].end_byte
                    positions = (mapped.position(start + offset) for offset in (
                        left, left + width, right - width, right,
                    ))
                    mapped.inline_formats.append(InlineFormat(role, *positions))
            elif node.type == "inline_link":
                label = next((child for child in node.children if child.type == "link_text"), None)
                if label is not None:
                    positions = (mapped.position(start + offset) for offset in (
                        node.start_byte, label.start_byte, label.end_byte, node.end_byte,
                    ))
                    mapped.inline_formats.append(InlineFormat("link", *positions))
                destination = next((child for child in node.children
                                    if child.type == "link_destination"), None)
                if destination is not None:
                    from markdown_it.common.utils import unescapeAll

                    url = mapped.data[start + destination.start_byte:
                                      start + destination.end_byte].decode("utf-8")
                    if url.startswith("<") and url.endswith(">"):
                        url = url[1:-1]
                    title = next((child for child in node.children
                                  if child.type == "link_title"), None)
                    mode = ILMode.HLIL
                    if title is not None and title.end_byte > title.start_byte:
                        # The node text keeps its "…", '…' or (…) delimiters.
                        raw = mapped.data[start + title.start_byte + 1:
                                          start + title.end_byte - 1].decode("utf-8")
                        mode = il_mode_for_title(unescapeAll(raw)) or ILMode.HLIL
                    mapped.links.append(MarkdownLink(
                        unescapeAll(url), mapped.position(start + node.start_byte),
                        mapped.position(start + node.end_byte),
                        expand_block=(label is not None and
                                      mapped.data[start + label.start_byte:
                                                  start + label.start_byte + 1] == b'!'),
                        il_mode=mode,
                        label=((mapped.position(start + label.start_byte),
                                mapped.position(start + label.end_byte)) if label is not None
                               else (mapped.position(start + node.start_byte + 1),) * 2),
                        destination_end=mapped.position(start + destination.end_byte),
                        title=(None if title is None else (
                            mapped.position(start + title.start_byte),
                            mapped.position(start + title.end_byte),
                        )),
                    ))
            elif node.type == "uri_autolink":
                url = mapped.data[start + node.start_byte + 1:start + node.end_byte - 1].decode()
                mapped.links.append(MarkdownLink(
                    url, mapped.position(start + node.start_byte),
                    mapped.position(start + node.end_byte),
                ))
            parent = node.parent
            # Dim punctuation only when it delimits a recognized construct.
            if parent is not None and node.type in _INLINE_MARKER_PARENTS.get(parent.type, ()):
                role = "marker"
            if role:
                mapped.add(start + node.start_byte, start + node.end_byte, role)


@dataclass(frozen=True, slots=True)
class SourceEdit:
    start: int
    end: int
    text: str


def _destination(url: str) -> str:
    return f'<{url}>' if any(character.isspace() or character in '()' for character in url) else url


def _title(mode: ILMode) -> str:
    return f'"{mode.value}"'


def _inline(link: MarkdownLink, *, expand: bool, mode: ILMode) -> SourceEdit:
    title = '' if mode is ILMode.HLIL else ' ' + _title(mode)
    return SourceEdit(link.start, link.end,
                      f'[{"!" if expand else ""}]({_destination(link.url)}{title})')


def expansion_edit(link: MarkdownLink, expand: bool) -> SourceEdit | None:
    """Add or remove the label's leading `!`, which embeds the whole HLIL block."""
    if expand == link.expand_block:
        return None
    if link.label is None:
        return _inline(link, expand=expand, mode=link.il_mode)
    start = link.label[0]
    return SourceEdit(start, start, '!') if expand else SourceEdit(start, start + 1, '')


def mode_edit(link: MarkdownLink, mode: ILMode) -> SourceEdit | None:
    """Select an IL mode through the link title; HLIL, the default, has none."""
    if mode == link.il_mode:
        return None
    if link.label is None:
        return _inline(link, expand=link.expand_block, mode=mode)
    if mode is ILMode.HLIL:
        # Any non-HLIL mode came from a title. Remove it with its leading space.
        return SourceEdit(link.destination_end, link.title[1], '')
    if link.title is not None:
        return SourceEdit(*link.title, _title(mode))
    return SourceEdit(link.destination_end, link.destination_end, ' ' + _title(mode))


def label_edit(link: MarkdownLink, label_source: str = '') -> tuple[SourceEdit | None, int, int]:
    """The edit (if any) and selection exposing the link's label, minus an expanded `!`."""
    if link.label is None:
        edit = _inline(link, expand=link.expand_block, mode=link.il_mode)
        caret = link.start + 1 + int(link.expand_block)
        return edit, caret, caret
    start, end = link.label
    if link.expand_block:
        rest = label_source[1:]
        start = min(end, start + 1 + utf16_length(rest[:len(rest) - len(rest.lstrip())]))
    return None, start, end


def separator_text(table):
    prefix = table.continuation_prefix
    return prefix.rstrip() if ">" in prefix else ""


def is_separator(line, table):
    return not line.strip() or line.strip() == separator_text(table) != ""


def padding_insertions(source, tables):
    """UTF-16 insertions giving each table a blank paragraph on either side."""
    lines = source.split("\n")
    starts, ends = source_line_positions(source)
    insertions = {}
    handled_before, handled_after = set(), set()

    def insert(position, text):
        insertions[position] = insertions.get(position, "") + text

    for index, (previous, following) in enumerate(zip(tables, tables[1:], strict=False)):
        last = bisect_right(starts, previous.end_position) - 1
        first = bisect_right(starts, following.start_position) - 1
        gap = lines[last + 1:first]
        if (previous.end_position == ends[last] and following.start_position == starts[first]
                and first > last and all(is_separator(line, previous)
                                        and is_separator(line, following) for line in gap)):
            if len(gap) < 3:
                prefix = (separator_text(previous) if previous.continuation_prefix ==
                          following.continuation_prefix else "")
                insert(following.start_position, (prefix + "\n") * (3 - len(gap)))
            handled_after.add(index)
            handled_before.add(index + 1)

    for index, table in enumerate(tables):
        start, end = table.start_position, table.end_position
        first, last = bisect_right(starts, start) - 1, bisect_right(starts, end) - 1
        guard = separator_text(table)
        if index not in handled_before:
            if first == 0 or start != starts[first]:
                insert(start, "\n" + guard + "\n")
            elif not is_separator(lines[first - 1], table):
                insert(start, guard + "\n")
            elif first == 1:
                insert(0, "\n")
        if index not in handled_after:
            if last == len(lines) - 1 or end != ends[last]:
                insert(end, "\n" + guard + "\n")
            elif not is_separator(lines[last + 1], table):
                insert(starts[last + 1], guard + "\n")
            elif last + 1 == len(lines) - 1:
                insert(ends[-1], "\n")
    return sorted(insertions.items(), reverse=True)


def apply_insertions(source, insertions):
    encoded = source.encode("utf-16-le")
    for position, text in insertions:
        encoded = encoded[:position * 2] + text.encode("utf-16-le") + encoded[position * 2:]
    return encoded.decode("utf-16-le")


def normalized_table_source(source, parser):
    logical = source.replace("\u2028", "\n").replace("\u2029", "\n")
    if parser is None or "|" not in logical:
        return source
    try:
        edits = padding_insertions(logical, parser.parse(logical).tables)
    except Exception:
        # The highlighter reports parser failures; the note must still load.
        return source
    return apply_insertions(source, edits)


@lru_cache(maxsize=64)
def _source_lines(source):
    lines = source.split("\n")
    starts, ends = source_line_positions(source)
    return lines, starts, ends


def table_region(source, table):
    """Return folded range and safe before/after caret positions for a table."""
    lines, starts, ends = _source_lines(source)
    first, last = table.start_line, table.end_line
    before = first - 1 if first > 0 and is_separator(lines[first - 1], table) else first
    after = last if last < len(lines) and is_separator(lines[last], table) else last - 1
    render_start, render_end = starts[before], ends[after]
    before_position = max(0, render_start - 1)
    after_position = min(utf16_length(source), render_end + 1)
    if after + 1 < len(lines) and table.continuation_prefix:
        prefix = table.continuation_prefix
        if lines[after + 1].startswith(prefix):
            after_position += utf16_length(prefix)
    return render_start, render_end, before_position, after_position
