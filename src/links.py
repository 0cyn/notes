from __future__ import annotations

import ctypes
import os
import sys
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from functools import cached_property
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit


class ILMode(str, Enum):
    """One decompilation or disassembly mode an In-BV link can embed."""

    HLIL = 'hlil'
    MLIL = 'mlil'
    MLIL_SSA = 'mlil-ssa'
    MLIL_MAPPED = 'mlil-mapped'
    MLIL_MAPPED_SSA = 'mlil-mapped-ssa'
    LLIL = 'llil'
    LLIL_SSA = 'llil-ssa'
    DISASM = 'disasm'
    PSEUDO_C = 'pseudo-c'
    PSEUDO_OBJC = 'pseudo-objc'
    PSEUDO_RUST = 'pseudo-rust'

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def language(self) -> str | None:
        """The registered language representation name, for pseudo-languages only."""
        return _LANGUAGES.get(self)


_LABELS = {
    ILMode.HLIL: 'HLIL',
    ILMode.MLIL: 'MLIL',
    ILMode.MLIL_SSA: 'MLIL SSA',
    ILMode.MLIL_MAPPED: 'Mapped MLIL',
    ILMode.MLIL_MAPPED_SSA: 'Mapped MLIL SSA',
    ILMode.LLIL: 'LLIL',
    ILMode.LLIL_SSA: 'LLIL SSA',
    ILMode.DISASM: 'Disassembly',
    ILMode.PSEUDO_C: 'Pseudo C',
    ILMode.PSEUDO_OBJC: 'Pseudo Objective-C',
    ILMode.PSEUDO_RUST: 'Pseudo Rust',
}

_LANGUAGES = {
    ILMode.PSEUDO_C: 'Pseudo C',
    ILMode.PSEUDO_OBJC: 'Pseudo Objective-C',
    ILMode.PSEUDO_RUST: 'Pseudo Rust',
}

# 'psuedo' spellings are accepted beside the correct ones as authored aliases.
_ALIASES = {
    'disasm': ILMode.DISASM, 'dasm': ILMode.DISASM, 'asm': ILMode.DISASM,
    'llil': ILMode.LLIL, 'llil-ssa': ILMode.LLIL_SSA,
    'mlil': ILMode.MLIL, 'mlil-ssa': ILMode.MLIL_SSA,
    'mlil-mapped': ILMode.MLIL_MAPPED, 'mlil-mapped-ssa': ILMode.MLIL_MAPPED_SSA,
    'hlil': ILMode.HLIL, 'hlil-ssa': ILMode.HLIL,
    'c': ILMode.PSEUDO_C, 'pseudo-c': ILMode.PSEUDO_C, 'pseudoc': ILMode.PSEUDO_C,
    'psuedo-c': ILMode.PSEUDO_C, 'psuedoc': ILMode.PSEUDO_C,
    'objc': ILMode.PSEUDO_OBJC, 'pseudo-objc': ILMode.PSEUDO_OBJC,
    'pseudoobjc': ILMode.PSEUDO_OBJC, 'psuedo-objc': ILMode.PSEUDO_OBJC,
    'psuedoobjc': ILMode.PSEUDO_OBJC,
    'rust': ILMode.PSEUDO_RUST, 'pseudo-rust': ILMode.PSEUDO_RUST,
    'pseudorust': ILMode.PSEUDO_RUST, 'psuedo-rust': ILMode.PSEUDO_RUST,
    'psuedorust': ILMode.PSEUDO_RUST,
}


# Context-menu order, grouped as Binary Ninja's view selector lists them.
MENU_GROUPS = (
    (ILMode.DISASM,),
    (ILMode.LLIL, ILMode.LLIL_SSA),
    (ILMode.MLIL, ILMode.MLIL_SSA, ILMode.MLIL_MAPPED, ILMode.MLIL_MAPPED_SSA),
    (ILMode.HLIL,),
    (ILMode.PSEUDO_C, ILMode.PSEUDO_OBJC, ILMode.PSEUDO_RUST),
)


def il_mode_available(mode: ILMode) -> bool:
    """Whether Binary Ninja can render `mode`: pseudo-languages are plugins."""
    if mode.language is None:
        return True
    registered = getattr(sys.modules.get('binaryninja'), 'LanguageRepresentationFunctionType', None)
    if registered is None:
        return True  # Outside Binary Ninja (tests): every mode is listed.
    try:
        return mode.language in registered
    except (RuntimeError, TypeError):
        return True


def il_mode_for_title(title: str) -> ILMode | None:
    """Resolve a Markdown link title to a mode; unrecognized titles select none."""
    return _ALIASES.get(title.strip().casefold())


INDENT_COLUMNS = 4


@dataclass(frozen=True)
class HLILToken:
    text: str
    kind: int = 0


@dataclass(frozen=True)
class HLILSourceLine:
    address: int
    tokens: tuple[HLILToken, ...]
    indent: int = INDENT_COLUMNS

    @cached_property
    def text(self):
        return ''.join(token.text for token in self.tokens)


@dataclass(frozen=True)
class HLILLine:
    address: int
    text: str
    function_name: str = ''
    line_address: int | None = None
    available: bool = False
    tokens: tuple[HLILToken, ...] = ()
    lines: tuple[HLILSourceLine, ...] = ()
    function_start: int | None = None
    function_signature: tuple[HLILSourceLine, ...] = ()
    expand_block: bool = False
    il_mode: ILMode = ILMode.HLIL

    @property
    def is_function_entry(self):
        return self.function_start == self.address and bool(
            self.function_name or self.function_signature,
        )

    @cached_property
    def header_lines(self):
        if not self.is_function_entry:
            return ()
        return self.function_signature or (HLILSourceLine(
            self.address, (HLILToken(self.function_name, 64),), indent=0,
        ),)

    @cached_property
    def display_lines(self):
        body = self.lines or (HLILSourceLine(
            self.line_address if self.line_address is not None else self.address,
            self.tokens or (HLILToken(self.text),),
        ),)
        return (*self.header_lines, *body)

    @cached_property
    def token_kinds(self):
        return frozenset(token.kind for line in self.display_lines for token in line.tokens)

    @cached_property
    def display_text(self):
        return '\n'.join(f'{line.address:x}\t' + ' ' * line.indent + line.text
                          for line in self.display_lines)

    @cached_property
    def _value_hash(self):
        return hash((self.address, self.text, self.function_name, self.line_address, self.available,
                     self.tokens, self.lines, self.function_start, self.function_signature,
                     self.expand_block, self.il_mode))

    def __hash__(self):
        return self._value_hash


def _copy_tokens(tokens):
    """Copy core token data while retaining types through trimming/tab expansion."""
    original = list(tokens)
    raw = ''.join(token.text for token in original)
    left, right = len(raw) - len(raw.lstrip()), len(raw.rstrip())
    offset, column = 0, 0
    result = []
    for token in original:
        end = offset + len(token.text)
        value = token.text[max(0, left - offset):max(0, min(len(token.text), right - offset))]
        offset = end
        expanded = []
        for character in value:
            text = ' ' * (4 - column % 4) if character == '\t' else character
            if character in '\r\n':
                text = ' '
            expanded.append(text)
            column += len(text)
        text = ''.join(expanded)
        if text:
            result.append(HLILToken(text, int(getattr(token, 'type', 0))))
    return tuple(result)


def _copy_lines(rendered, address, base_indent, *, force_address=False):
    rows = []
    for line in rendered:
        original = list(line.tokens)
        raw = ''.join(token.text for token in original).expandtabs(INDENT_COLUMNS)
        indent = len(raw) - len(raw.lstrip())
        line_address = None if force_address else getattr(line, 'address', None)
        if line_address is None:
            line_address = address
        rows.append(HLILSourceLine(line_address, _copy_tokens(original), indent))
    while rows and not rows[0].text:
        rows.pop(0)
    while rows and not rows[-1].text:
        rows.pop()
    if not rows:
        return ()
    base = rows[0].indent
    return tuple(HLILSourceLine(row.address, row.tokens,
                                max(0, row.indent - base + base_indent)) for row in rows)


def _function_type_lines(function, settings):
    api = sys.modules.get('binaryninja')
    core = getattr(api, '_binaryninjacore', None)
    if core is not None and hasattr(function, 'handle'):
        # The Python wrapper passes settings, not its handle, to the core: call the core directly.
        count = ctypes.c_ulonglong()
        lines = core.BNGetFunctionTypeTokens(
            function.handle, settings.handle if settings is not None else None, count,
        )
        if lines is None:
            return
        try:
            for index in range(count.value):
                line = lines[index]
                tokens = api.InstructionTextToken._from_core_struct(line.tokens, line.count)
                yield SimpleNamespace(address=function.start, tokens=tokens)
        finally:
            core.BNFreeDisassemblyTextLines(lines, count.value)
    else:
        get_tokens = getattr(function, 'get_type_tokens', None)
        if callable(get_tokens):
            yield from get_tokens(settings)


def _function_signature(function, settings):
    try:
        with closing(_function_type_lines(function, settings)) as rendered:
            return _copy_lines(rendered, function.start, 0, force_address=True)
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError, ctypes.ArgumentError):
        return ()


def _assemble(rows, address, name, *, expand_block=False, il_mode=ILMode.HLIL):
    """Build one preview from source rows, joining them as a single multi-line token run."""
    if not any(row.text not in ('', '{', '}') for row in rows):
        return None
    tokens = []
    for index, row in enumerate(rows):
        if index:
            tokens.append(HLILToken('\n' + ' ' * max(0, row.indent - INDENT_COLUMNS)))
        tokens.extend(row.tokens)
    text = ''.join(token.text for token in tokens)
    return HLILLine(address, text, name, rows[0].address, True, tuple(tokens), rows,
                    expand_block=expand_block, il_mode=il_mode)


def _instruction_line(statement, address, name, settings=None, *, expand_block=False):
    settings_type = getattr(sys.modules.get('binaryninja'), 'DisassemblySettings', None)
    get_lines = getattr(statement, 'get_lines', None)
    # get_lines keeps only a raw settings handle; hold the wrapper until it is exhausted.
    if settings is None and callable(get_lines) and settings_type is not None:
        settings = settings_type.default_linear_settings()
    rendered = (get_lines(settings) if settings is not None and callable(get_lines)
                else getattr(statement, 'lines', ()))
    return _assemble(_copy_lines(rendered, getattr(statement, 'address', address),
                                 INDENT_COLUMNS), address, name, expand_block=expand_block)


def _statement_line(instruction, address, name, settings=None, *, expand_block=False):
    statement = getattr(instruction, 'instr', instruction)
    statement = getattr(statement, 'ast' if expand_block else 'non_ast', statement)
    return _instruction_line(statement, address, name, settings, expand_block=expand_block)


def _function_candidates(data, address):
    """Functions whose body may represent this address, entry and exact matches first."""
    lookup = getattr(data, 'get_functions_containing', None)
    functions = list(lookup(address)) if callable(lookup) else []
    exact = getattr(data, 'get_function_at', lambda _address: None)(address)
    if exact is not None:
        functions = [exact, *(item for item in functions if item != exact)]
    entries = [function for function in functions if function.start == address]
    if entries:
        functions = entries
    return functions


def _function_context(function, address, settings):
    symbol = getattr(function, 'symbol', None)
    name = str(getattr(symbol, 'short_name', '') or getattr(function, 'name', '') or '')
    signature = (_function_signature(function, settings) if function.start == address else ())
    return name, signature


def hlil_line_at(data, address, settings=None, *, expand_block=False):
    """HLIL via the LLIL mapping, then exact address, then a function entry's first statement.

    May generate IL: never call on the UI thread. Expanded embeds render the AST.
    """
    unavailable = HLILLine(address, 'HLIL unavailable', expand_block=expand_block)
    try:
        for function in _function_candidates(data, address):
            name, signature = _function_context(function, address, settings)
            unavailable = HLILLine(address, 'HLIL unavailable', name, function_start=function.start,
                                   function_signature=signature, expand_block=expand_block)
            il = getattr(function, 'hlil', None)
            if il is None:
                continue
            if expand_block and function.start == address:
                root = getattr(il, 'root', None)
                if root is not None:
                    result = _instruction_line(getattr(root, 'ast', root), address, name, settings,
                                               expand_block=True)
                    if result is not None:
                        return replace(result, function_start=function.start,
                                       function_signature=signature)
            get_low = getattr(function, 'get_llil_at', None)
            low = get_low(address) if callable(get_low) else None
            mapped = getattr(low, 'hlil', None)
            if mapped is not None:
                result = _statement_line(mapped, address, name, settings, expand_block=expand_block)
                if result is not None:
                    return replace(result, function_start=function.start,
                                   function_signature=signature)
            first = None
            for instruction in il.instructions:
                if first is None:
                    first = instruction
                if instruction.address == address:
                    result = _statement_line(instruction, address, name, settings,
                                             expand_block=expand_block)
                    if result is not None:
                        return replace(result, function_start=function.start,
                                       function_signature=signature)
            if address == function.start and first is not None:
                result = _statement_line(first, address, name, settings, expand_block=expand_block)
                if result is not None:
                    return replace(result, function_start=function.start,
                                   function_signature=signature)
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError):
        pass
    return unavailable


def _il_function(function, il_mode):
    """The IL function for a selected medium/low form, or None."""
    if il_mode is ILMode.LLIL:
        return getattr(function, 'llil', None)
    if il_mode is ILMode.LLIL_SSA:
        llil = getattr(function, 'llil', None)
        return getattr(llil, 'ssa_form', None) if llil is not None else None
    if il_mode is ILMode.MLIL:
        return getattr(function, 'mlil', None)
    if il_mode is ILMode.MLIL_SSA:
        mlil = getattr(function, 'mlil', None)
        return getattr(mlil, 'ssa_form', None) if mlil is not None else None
    if il_mode is ILMode.MLIL_MAPPED:
        return getattr(function, 'mapped_medium_level_il', None)
    if il_mode is ILMode.MLIL_MAPPED_SSA:
        mapped = getattr(function, 'mapped_medium_level_il', None)
        return getattr(mapped, 'ssa_form', None) if mapped is not None else None
    return None


def _mapped_il_instruction(function, il_mode, low, mlil):
    """Map the instructions at the target address into the selected form."""
    if il_mode is ILMode.LLIL:
        return low
    if il_mode is ILMode.LLIL_SSA:
        return getattr(low, 'ssa_form', None) if low is not None else None
    if mlil is None:
        return None
    if il_mode is ILMode.MLIL:
        return mlil
    if il_mode is ILMode.MLIL_SSA:
        return getattr(mlil, 'ssa_form', None)
    base = _il_function(function, ILMode.MLIL_MAPPED)
    llil_function = getattr(function, 'llil', None)
    mapper = getattr(llil_function, 'get_mapped_medium_level_il_expr_index', None)
    if base is None or low is None or not callable(mapper):
        return None
    try:
        expr = mapper(getattr(low, 'expr_index', low))
        create = getattr(type(mlil), 'create', None)
        if expr is None or not callable(create):
            return None
        result = create(base, expr)
    except (AssertionError, AttributeError, IndexError, RuntimeError, TypeError, ValueError):
        return None
    return getattr(result, 'ssa_form', None) if il_mode is ILMode.MLIL_MAPPED_SSA else result


def _il_statement_rows(data, function, address, il_mode):
    """The single instruction representing this address in the selected form."""
    if il_mode is ILMode.DISASM:
        arch = getattr(function, 'arch', None) or getattr(data, 'arch', None)
        read = getattr(data, 'read', None)
        if arch is None or not callable(read):
            return ()
        tokens, size = arch.get_instruction_text(
            read(address, getattr(arch, 'max_instr_length', 16)), address,
        )
        if not tokens or not size:
            return ()
        return (SimpleNamespace(address=address, tokens=tokens),)
    get_low = getattr(function, 'get_llil_at', None)
    low = get_low(address) if callable(get_low) else None
    mapped = _mapped_il_instruction(function, il_mode, low,
                                    getattr(low, 'mlil', None) if low is not None else None)
    if mapped is not None:
        return (mapped,)
    il = _il_function(function, il_mode)
    if il is None:
        return ()
    first = None
    for instruction in il.instructions:
        if first is None:
            first = instruction
        if instruction.address == address:
            return (instruction,)
    if address == function.start and first is not None:
        return (first,)
    return ()


def _disassembly_rows(function, settings):
    """The function's disassembly in address order, as Linear View lays out its blocks."""
    rows = []
    for block in sorted(getattr(function, 'basic_blocks', ()), key=lambda item: item.start):
        get_text = getattr(block, 'get_disassembly_text', None)
        rows.extend(get_text(settings) if callable(get_text)
                    else getattr(block, 'disassembly_text', ()))
    return rows


def _il_function_rows(function, il_mode, settings=None):
    """Every top-level instruction, in the selected form, for an expanded embed."""
    if il_mode is ILMode.DISASM:
        return _disassembly_rows(function, settings)
    il = _il_function(function, il_mode)
    return getattr(il, 'instructions', ()) if il is not None else ()


_NO_INDEX = 0xffffffffffffffff


def ast_region(blocks, shown, addresses):
    """Keys of the instructions that compute the HLIL statements in `shown`.

    `blocks` lists `(block, predecessors, items)` in layout order, where each item is
    `(key, address, owners)`: the HLIL statements (by instruction index) the instruction
    maps to, empty when HLIL eliminated it. `addresses` gives each statement's address.

    Mappings run from an instruction to every statement that uses its value, so a spilled
    argument or a frame-pointer setup belongs to many statements. Each instruction counts
    for the earliest statement that uses it, and eliminated instructions (a comparison
    folded into its branch, a goto, a dead store) for the statement that follows them in
    their block. A block's instructions for one statement are contiguous, so a run counts
    only if one of its instructions belongs to `shown` alone or sits at its statement's
    address: hoisted code for a later statement is left out. Blocks with no mapped
    instruction belong to the region when every block entering them does.
    """
    selected, keys, undecided = set(), {}, []
    for block, predecessors, items in blocks:
        if not items:
            continue
        keys[block] = [key for key, _address, _owners in items]
        primaries = [min(owners) if owners else None for _key, _address, owners in items]
        following = None
        for index in reversed(range(len(primaries))):
            if primaries[index] is None:
                primaries[index] = following
            else:
                following = primaries[index]
        preceding = None
        for index, primary in enumerate(primaries):
            if primary is None:
                primaries[index] = preceding
            else:
                preceding = primary
        if preceding is None:
            undecided.append((block, predecessors))
            continue
        run, anchored = [], False
        for (key, address, owners), primary in zip(items, primaries, strict=True):
            if primary in shown:
                run.append(key)
                anchored = anchored or bool(owners) and (
                    owners <= shown or address == addresses.get(primary))
                continue
            if anchored:
                selected.update(run)
            run, anchored = [], False
        if anchored:
            selected.update(run)
    changed = True
    while changed and undecided:
        changed = False
        for entry in list(undecided):
            block, predecessors = entry
            if predecessors and all(source in keys and keys[source][-1] in selected
                                    for source in predecessors):
                selected.update(keys[block])
                undecided.remove(entry)
                changed = True
    return selected


def _shown_statements(hlil, statement):
    """Every HLIL statement an expanded embed prints, by instruction index.

    Rendered tokens name their expressions, which include statements printed in another's
    header, such as a for loop's initializer and increment.
    """
    shown = set()
    count = len(hlil)
    for line in statement.ast.lines:
        for token in line.tokens:
            expr = getattr(token, 'il_expr_index', _NO_INDEX)
            if expr is None or expr == _NO_INDEX:
                continue
            index = hlil.get_instruction_index_for_expr(expr)
            if index is not None and index < count:
                shown.add(int(index))
    if not shown:
        shown.update(int(node) for node in statement.ast.traverse(
            lambda node: node.instr_index, shallow=False))
    return shown


def _mlil_owners(function, hlil):
    """MLIL instruction index -> the HLIL statements whose expressions map to it."""
    mlil = function.mlil
    ssa = mlil.ssa_form
    count = len(mlil)
    owners = {}
    for statement in hlil.instructions:
        for expr in hlil.get_medium_level_il_expr_indexes(statement.expr_index):
            index = ssa.get_instruction_index_for_expr(expr)
            if index is None:
                continue
            index = ssa.get_non_ssa_instruction_index(index)
            if index < count:
                owners.setdefault(int(index), set()).add(int(statement.instr_index))
    return owners


def _il_blocks(il, owners_of):
    blocks = []
    for block in il.basic_blocks:
        predecessors = [edge.source.start for edge in block.incoming_edges]
        items = [(index, il[index].address, frozenset(owners_of(index)))
                 for index in range(block.start, block.end)]
        blocks.append((block.start, predecessors, items))
    return blocks


def _ssa_owners(ssa, owners_of):
    """Owners for an SSA form through its non-SSA instructions; phis have none of their own."""
    def owners(index):
        non_ssa = ssa.get_non_ssa_instruction_index(index)
        return owners_of(non_ssa) if non_ssa != _NO_INDEX else ()
    return owners


def _il_ast_rows(function, address, il_mode, settings=None):
    """The selected form's instructions for the HLIL AST an expanded embed shows.

    Walks the mapping from LLIL, mapped MLIL and disassembly through MLIL to HLIL, which is
    the direction Binary Ninja records; mapped MLIL is mapped one-to-one from LLIL.
    """
    statement = _hlil_statement(function, address)
    hlil = getattr(function, 'hlil', None)
    if statement is None or hlil is None:
        return ()
    statement = hlil[statement.instr_index]
    shown = _shown_statements(hlil, statement)
    addresses = {int(item.instr_index): item.address for item in hlil.instructions}
    mlil_owners = _mlil_owners(function, hlil)

    def mlil_owner(index):
        return mlil_owners.get(index, ())

    llil = function.llil

    def llil_owner(index):
        mapped = llil.get_medium_level_il_instruction_index(index)
        return mlil_owner(mapped) if mapped is not None else ()

    if il_mode in (ILMode.MLIL, ILMode.MLIL_SSA):
        il, owner = function.mlil, mlil_owner
    elif il_mode in (ILMode.LLIL, ILMode.LLIL_SSA):
        il, owner = llil, llil_owner
    elif il_mode in (ILMode.MLIL_MAPPED, ILMode.MLIL_MAPPED_SSA):
        mapped_owners = {}
        for index in range(len(llil)):
            mapped = llil.get_mapped_medium_level_il_instruction_index(index)
            if mapped is not None:
                mapped_owners.setdefault(int(mapped), set()).update(llil_owner(index))
        il, owner = function.mapped_medium_level_il, (lambda index: mapped_owners.get(index, ()))
    elif il_mode is ILMode.DISASM:
        return _disassembly_ast_rows(function, settings, shown, addresses, llil_owner)
    else:
        return ()
    if il_mode in (ILMode.MLIL_SSA, ILMode.LLIL_SSA, ILMode.MLIL_MAPPED_SSA):
        il, owner = il.ssa_form, _ssa_owners(il.ssa_form, owner)
    keys = ast_region(_il_blocks(il, owner), shown, addresses)
    return tuple(il[index] for index in sorted(keys))


def _ast_rows(function, address, il_mode, settings):
    try:
        return _il_ast_rows(function, address, il_mode, settings)
    except (AssertionError, AttributeError, IndexError, KeyError, RuntimeError, TypeError,
            ValueError):
        return ()


def _disassembly_ast_rows(function, settings, shown, addresses, llil_owner):
    """Disassembly lines for the AST, attributed through the LLIL at each address."""
    blocks, lines = [], []
    for block in sorted(getattr(function, 'basic_blocks', ()), key=lambda item: item.start):
        rendered = list(block.get_disassembly_text(settings))
        lines.extend(rendered)
        items, seen = [], set()
        for line in rendered:
            if line.address in seen:
                continue
            seen.add(line.address)
            owners = set()
            for low in function.get_llils_at(line.address):
                owners.update(llil_owner(low.instr_index))
            items.append((line.address, line.address, frozenset(owners)))
        blocks.append((block.start, [edge.source.start for edge in block.incoming_edges], items))
    keys = ast_region(blocks, shown, addresses)
    return tuple(line for line in lines if line.address in keys)


def _hlil_statement(function, address):
    """The HLIL instruction for an address: mapping, exact match, then entry prologue."""
    get_low = getattr(function, 'get_llil_at', None)
    low = get_low(address) if callable(get_low) else None
    mapped = getattr(low, 'hlil', None)
    if mapped is not None:
        return getattr(mapped, 'instr', mapped)
    il = getattr(function, 'hlil', None)
    if il is None:
        return None
    first = None
    for instruction in il.instructions:
        if first is None:
            first = instruction
        if instruction.address == address:
            return instruction
    return first if address == function.start else None


def _pseudo_rows(function, address, settings, il_mode, expand_block):
    """Pseudo-language lines, selected through the same HLIL statement mapping."""
    get_representation = getattr(function, 'language_representation', None)
    representation = (get_representation(il_mode.language)
                      if callable(get_representation) else None)
    get_lines = getattr(representation, 'get_linear_lines', None)
    if representation is None or not callable(get_lines):
        return ()
    if expand_block and address == function.start:
        statement = getattr(getattr(function, 'hlil', None), 'root', None)
    else:
        statement = _hlil_statement(function, address)
    if statement is None:
        return ()
    # Rendering follows the instruction's form: the AST includes nested bodies.
    return get_lines(getattr(statement, 'ast' if expand_block else 'non_ast', statement), settings)


def _without_address_column(settings):
    """A copy of `settings` without the address column; embeds draw their own."""
    api = sys.modules.get('binaryninja')
    settings_type = getattr(api, 'DisassemblySettings', None)
    option = getattr(getattr(api, 'DisassemblyOption', None), 'ShowAddress', None)
    core = getattr(api, '_binaryninjacore', None)
    if settings_type is None or option is None or core is None:
        return settings
    # The default linear settings are shared by the core: never modify them in place.
    source = settings if hasattr(settings, 'handle') else settings_type.default_linear_settings()
    copy = settings_type(core.BNDuplicateDisassemblySettings(source.handle))
    copy.set_option(option, False)
    return copy


def il_snapshot_at(data, address, settings=None, *, expand_block=False, il_mode=ILMode.HLIL):
    """The selected IL or pseudo-language at an address; never call on the UI thread."""
    if il_mode == ILMode.HLIL:
        return hlil_line_at(data, address, settings, expand_block=expand_block)
    rendering = _without_address_column(settings)
    unavailable = HLILLine(address, f'{il_mode.label} unavailable',
                           expand_block=expand_block, il_mode=il_mode)
    try:
        for function in _function_candidates(data, address):
            name, signature = _function_context(function, address, settings)
            unavailable = HLILLine(address, f'{il_mode.label} unavailable', name,
                                   function_start=function.start, function_signature=signature,
                                   expand_block=expand_block, il_mode=il_mode)
            if il_mode.language is not None:
                rows = _pseudo_rows(function, address, rendering, il_mode, expand_block)
            elif expand_block and address == function.start:
                rows = _il_function_rows(function, il_mode, rendering)
            elif expand_block:
                # The instructions behind the HLIL AST this embed would show; a single
                # instruction when that cannot be traced.
                rows = (_ast_rows(function, address, il_mode, rendering)
                        or _il_statement_rows(data, function, address, il_mode))
            else:
                rows = _il_statement_rows(data, function, address, il_mode)
            result = _assemble(_copy_lines(rows, address, INDENT_COLUMNS), address, name,
                               expand_block=expand_block, il_mode=il_mode)
            if result is not None:
                return replace(result, function_start=function.start,
                               function_signature=signature)
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError):
        pass
    return unavailable


@dataclass(frozen=True)
class AddressAnchor:
    note_id: str
    position: int
    end: int
    address: int
    url: str


def _file_path(path):
    if not path:
        return None
    if os.name == "nt" and len(path) > 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if not os.path.isabs(path):
        return None
    return os.path.normcase(os.path.realpath(path))


def binary_ninja_url(data, expr):
    """A file-qualified BNURL for an address or (percent-encoded) symbol name."""
    if data is None:
        return None
    if isinstance(expr, int):
        if not 0 <= expr < 1 << 64:
            return None
        expr = f"{expr:#x}"
    elif not isinstance(expr, str) or not expr.strip():
        return None
    path = _file_path(getattr(data.file, "filename", ""))
    if path is None:
        project_file = getattr(data.file, "project_file", None)
        if project_file is not None:
            path = _file_path(project_file.path_on_disk)
    if path is None:
        return None
    return ("binaryninja:" + Path(path).as_uri().removeprefix("file:")
            + "?expr=" + quote(expr, safe=""))


def in_bv_expression(url, file):
    """`expr` of a link to this file; '+' stays addition (no form decoding)."""
    try:
        target = urlsplit(url)
        if target.scheme.lower() != "binaryninja":
            return None
        if target.netloc not in ("", "localhost"):
            return None
        if target.path:
            filename = _file_path(unquote(target.path))
            if filename is None:
                return None
            paths = {_file_path(getattr(file, "filename", ""))}
            project_file = getattr(file, "project_file", None)
            if project_file is not None:
                paths.add(_file_path(project_file.path_on_disk))
            if filename not in paths:
                return None
        expressions = [unquote(value) for field in target.query.split("&")
                       for key, separator, value in [field.partition("=")]
                       if separator and unquote(key) == "expr"]
        return expressions[0] if len(expressions) == 1 and expressions[0].strip() else None
    except (OSError, ValueError):
        return None


def resolve_in_bv_link(data, url):
    if data is None or not callable(getattr(data, "parse_expression", None)):
        return None
    expression = in_bv_expression(url, data.file)
    if expression is None:
        return None
    try:
        address = data.parse_expression(expression, here=data.start)
        if not isinstance(address, int) or not 0 <= address < 1 << 64:
            return None
        return address if data.is_valid_offset(address) else None
    except (ValueError, TypeError, OverflowError):
        # Incomplete links and symbols that have not been analyzed yet are editable.
        return None


def find_address_anchor(data, anchors, offset, *, preferred_note=None, function=None):
    """Prefer exact targets, then the nearest preceding anchor in the same function."""
    exact = [anchor for anchor in anchors if anchor.address == offset]
    if exact:
        return min(exact, key=lambda anchor: anchor.note_id != preferred_note)
    if not anchors:
        return None
    functions = [function] if function is not None else data.get_functions_containing(offset)
    starts = {item.start for item in functions}
    if not starts:
        return None
    matches = []
    for anchor in anchors:
        if anchor.address in starts or any(
            item.start in starts for item in data.get_functions_containing(anchor.address)
        ):
            matches.append(anchor)
    return min(matches, key=lambda anchor: (
        anchor.address > offset, abs(offset - anchor.address), anchor.note_id != preferred_note,
    ), default=None)
