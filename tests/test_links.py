from types import SimpleNamespace
from urllib.parse import quote

import pytest

from src.links import (
    AddressAnchor,
    ILMode,
    ast_region,
    binary_ninja_url,
    find_address_anchor,
    hlil_line_at,
    il_snapshot_at,
    in_bv_expression,
    resolve_in_bv_link,
)

from .conftest import SnapshotFunction, file_url


def _get_instruction_text(_data, address):
    return [SimpleNamespace(text='mov eax, ebx', type=19)], 2


@pytest.fixture
def snapshot_view():
    view = SimpleNamespace(start=0x1000, functions=[],
                           arch=SimpleNamespace(max_instr_length=16,
                                                get_instruction_text=_get_instruction_text),
                           read=lambda address, size: b'\x90' * size)

    def get_functions_containing(address):
        return [function for function in view.functions
                if function.start <= address < function.start + 0x100]

    view.get_functions_containing = get_functions_containing
    function = SnapshotFunction(0x1000)
    view.functions.append(function)
    view.function = function
    return view


@pytest.mark.parametrize('url', ['binaryninja://?expr=first+4', 'binaryninja://?expr=first%2B4'])
def test_binary_ninja_query_preserves_expression_addition(link_view, url):
    assert in_bv_expression(url, link_view.file) == 'first+4'
    assert resolve_in_bv_link(link_view, url) == link_view.start + 4


def test_same_bndb_uses_full_decoded_path_and_resolves_symlinks(link_view, tmp_path):
    assert resolve_in_bv_link(link_view, file_url(link_view)) == link_view.start
    target = tmp_path / 'analysis notes.bndb'
    target.touch()
    alias = tmp_path / 'alias.bndb'
    alias.symlink_to(target)
    assert resolve_in_bv_link(link_view, f'binaryninja://{alias}?expr=first') == link_view.start
    other = tmp_path / 'other' / target.name
    assert resolve_in_bv_link(link_view, f'binaryninja://{quote(str(other))}?expr=first') is None
    link_view.file.original_filename = str(other)
    assert resolve_in_bv_link(link_view, f'binaryninja://{quote(str(other))}?expr=first') is None


def test_generated_bnurl_round_trips_encoded_filename_and_full_width_address(link_view, tmp_path):
    link_view.file.filename = str(tmp_path / 'analysis # α?.bndb')
    url = binary_ninja_url(link_view, link_view.start)
    assert url.startswith('binaryninja:///')
    assert '%20' in url and '%23' in url and '%3F' in url
    assert url.endswith(f'?expr={link_view.start:#x}')
    assert resolve_in_bv_link(link_view, url) == link_view.start


@pytest.mark.parametrize('url', [
    'https://example.com/?expr=first',
    'binaryninja://remote/file.bndb?expr=first',
])
def test_other_files_unresolved_or_invalid_links_are_not_anchors(link_view, url):
    assert resolve_in_bv_link(link_view, url) is None


def test_address_matching_prefers_exact_then_nearest_in_function(link_view):
    start = link_view.start
    anchors = [AddressAnchor('a', 0, 10, start, 'first'),
               AddressAnchor('b', 0, 10, start + 0x20, 'first+20'),
               AddressAnchor('c', 0, 10, start + 0x1000, 'second')]
    assert find_address_anchor(link_view, anchors, start, preferred_note='b') is anchors[0]
    assert find_address_anchor(link_view, anchors, start + 4) is anchors[0]
    assert find_address_anchor(link_view, anchors, start + 0x30) is anchors[1]
    assert find_address_anchor(link_view, anchors, start + 0x1004) is anchors[2]
    assert find_address_anchor(link_view, anchors, start + 0x2000) is None
    duplicate = AddressAnchor('d', 0, 10, start, 'first')
    assert find_address_anchor(link_view, [anchors[0], duplicate], start,
                               preferred_note='d') is duplicate


@pytest.mark.parametrize('offset, expected', [
    (0, 'result = worker(arg1)'),
    (4, 'result = worker(arg1)'),
])
def test_hlil_lookup_uses_statement_mapping_or_exact_address(preview_view, offset, expected):
    result = hlil_line_at(preview_view, preview_view.start + offset)
    assert result.available and result.text == expected
    assert result.address == preview_view.start + offset
    assert result.function_name == 'first_function'
    assert result.function_start == preview_view.start
    assert result.is_function_entry == (offset == 0)
    if offset == 0:
        assert result.display_lines[0].text == 'first_function'
        assert result.display_lines[0].address == preview_view.start
        assert result.display_lines[0].indent == 0
        assert result.display_lines[1].address == preview_view.start + 4


def test_full_function_renders_root_without_losing_signature_or_sibling_statements(block_view):
    function = block_view.functions[0]
    full = hlil_line_at(block_view, function.start, expand_block=True)
    assert full.available and full.expand_block and full.is_function_entry
    assert full.header_lines[0].text == function.name
    assert full.lines[0].text == 'prologue()'
    assert full.lines[-1].text == 'return result'
    assert 'second()' in full.text and 'fallback()' in full.text
    assert len(full.lines) == 12
    assert hlil_line_at(block_view, function.start).text == 'if (ready)'


@pytest.mark.parametrize('mode, expected', [
    (ILMode.LLIL, 'llil_result = llil_worker()'),
    (ILMode.LLIL_SSA, 'llil_result #4 = llil_worker() #1'),
])
def test_statement_snapshots_use_the_selected_form(snapshot_view, mode, expected):
    preview = il_snapshot_at(snapshot_view, 0x1004, expand_block=False, il_mode=mode)
    assert preview.available and preview.il_mode is mode
    assert preview.text == expected
    assert preview.address == 0x1004


def test_expanded_pseudo_language_renders_the_whole_function(snapshot_view):
    preview = il_snapshot_at(snapshot_view, 0x1000, expand_block=True, il_mode=ILMode.PSEUDO_C)
    assert preview.available and preview.il_mode is ILMode.PSEUDO_C
    assert preview.text == 'Pseudo C: body'


def test_expanded_il_embeds_trace_the_ast_and_fall_back_to_one_instruction(snapshot_view):
    statement = il_snapshot_at(snapshot_view, 0x1004, expand_block=True, il_mode=ILMode.LLIL)
    assert statement.available and statement.text == 'llil_result = llil_worker()'
    entry = il_snapshot_at(snapshot_view, 0x1000, expand_block=True, il_mode=ILMode.LLIL)
    assert entry.text == 'first()\nreturn result'


# An -O0 function, as Binary Ninja maps it: (block, predecessors, [(MLIL index, address,
# HLIL statements using it)]). HLIL statements: 1 `if (*arg1 > 0x20)` at 0x10 with body
# 2 (a call at 0x20); 3 `x = arg2 & 3` at 0x30; 4 `if (x == 0)` at 0x38 with body 5 and
# `else if (x == 1)` 6 at 0x48; 7 `return` at 0x50.
_ADDRESSES = {1: 0x10, 2: 0x20, 3: 0x30, 4: 0x38, 5: 0x40, 6: 0x48, 7: 0x50}
_BLOCKS = [
    ('entry', [], [(0, 0x08, {1, 2}),     # var_10 = arg1: read only by the if
                   (1, 0x0c, {3, 6}),     # var_18 = arg2: first read by statement 3
                   (2, 0x0e, {1}),
                   (3, 0x10, {1})]),      # the if's branch
    ('copy', ['entry'], [(4, 0x18, {2}), (5, 0x20, {2}),
                         (6, 0x22, set())]),  # goto after the body
    ('switch', ['entry', 'copy'], [(7, 0x2c, {3, 4, 6}),   # x is read by both ifs
                                   (8, 0x30, {3, 4, 6}),
                                   (9, 0x34, set()),        # cmp folded into the branch
                                   (10, 0x38, {4})]),
    ('next case', ['switch'], [(11, 0x3a, set())]),
    ('case 1', ['next case'], [(12, 0x44, {6}), (13, 0x48, {6})]),
    ('case 0', ['switch'], [(14, 0x40, {5}), (15, 0x42, set())]),
    ('return', ['case 1', 'case 0'], [(16, 0x50, {7})]),
]


def _region(shown):
    blocks = [(block, predecessors, [(key, address, frozenset(owners))
                                     for key, address, owners in items])
              for block, predecessors, items in _BLOCKS]
    return ast_region(blocks, frozenset(shown), _ADDRESSES)


def test_ast_region_keeps_branches_bodies_and_eliminated_instructions():
    # The if's own spill and load, its branch, its body and the goto after it; not the
    # spill of arg2, which statement 3 reads first.
    assert _region({1, 2}) == {0, 2, 3, 4, 5, 6}
    # The comparison HLIL folded, both cases and the goto-only block between them; not
    # the shared x, which belongs to the statement defining it, nor the shared return.
    assert _region({4, 5, 6}) == {9, 10, 11, 12, 13, 14, 15}


def test_ast_region_leaves_out_code_hoisted_for_a_later_statement():
    # var_18 = arg2 is statement 3's earliest use but sits in the entry block among the
    # first if's code, away from statement 3's address; the case block is not entered.
    assert _region({3}) == {7, 8}
    assert _region({7}) == {16}
