from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.settings import Scope
from src.ui import onboarding as onboarding_module
from src.ui.common import PersistentViewSettings
from src.ui.editor import AGENT_HUE, CLAUDE_HUE, OPENAI_HUE, PEOPLE_HUES
from src.ui.notebook import StandaloneNoteTabs
from src.ui.notepad import NotepadWidget
from src.ui.onboarding import (
    BUILT_IN,
    ONBOARDED_KEY,
    EmbedSample,
    OnboardingWidget,
    onboarding_due,
)
from src.ui.preferences import editor_preferences

from .conftest import FakeView, wait_until


def _store(tmp_path, name='tour.ini'):
    return PersistentViewSettings(QSettings(str(tmp_path / name), QSettings.IniFormat))


def _settle():
    for _ in range(3):
        QApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _pages(widget):
    return [page for page in widget.tabs.pages() if page is not None]


def _first_open(notebook, tmp_path, view=None, **kwargs):
    store = _store(tmp_path)
    store.remove_app_value(ONBOARDED_KEY)
    widget = notebook(view if view is not None else FakeView(), state_store=store,
                      tab_factory=StandaloneNoteTabs, **kwargs)
    return widget, store


def test_first_notepad_shows_only_the_tour_and_later_ones_show_notes(notebook, tmp_path):
    widget, store = _first_open(notebook, tmp_path)

    assert isinstance(widget.onboarding, OnboardingWidget)
    assert _pages(widget) == [widget.onboarding]
    assert widget.tabs.currentWidget() is widget.onboarding
    # Shown instead of the notes: no Note 1 was made for it.
    assert widget.notebook.notes == []
    assert not onboarding_due(store)

    later = notebook(FakeView(), state_store=store, tab_factory=StandaloneNoteTabs)
    assert later.onboarding is None
    assert isinstance(later.tabs.currentWidget(), NotepadWidget)


def test_get_started_opens_a_new_note(notebook, tmp_path):
    widget, _store_ = _first_open(notebook, tmp_path)
    widget.window().show()
    widget.window().activateWindow()
    wait_until(lambda: widget.window().isActiveWindow())
    page = widget.onboarding
    for _step in range(page.pages.count() - 1):
        assert page.next_button.text() == 'Next'
        QTest.mouseClick(page.next_button, Qt.MouseButton.LeftButton)
    assert page.next_button.text() == 'Get Started'
    QTest.mouseClick(page.next_button, Qt.MouseButton.LeftButton)
    _settle()

    assert widget.onboarding is None
    note = widget.current_note
    assert _pages(widget) == [note]
    assert [session.source for session in widget.notebook.notes] == ['']
    wait_until(lambda: QApplication.focusWidget() is note.editor)


def test_closing_the_tour_also_opens_a_new_note_after_the_saved_tabs(notebook, tmp_path):
    view = FakeView('# Kept\n')
    store = _store(tmp_path)
    onboarding_module.mark_onboarded(store)
    notebook(view, state_store=store, tab_factory=StandaloneNoteTabs)  # Saves its open tab.
    store.remove_app_value(ONBOARDED_KEY)

    widget = notebook(view, state_store=store, tab_factory=StandaloneNoteTabs)
    assert _pages(widget) == [widget.onboarding]
    assert widget.tabs.closeTab(widget.onboarding)
    _settle()

    assert widget.onboarding is None
    pages = list(widget._pages.values())
    assert pages[0].session.id == 'initial' and len(pages) == 2
    assert widget.tabs.currentWidget() is pages[1] is widget.current_note
    assert pages[1].session.source == ''


def test_navigation_leaves_the_tour_in_place(notebook, tmp_path):
    widget, _store_ = _first_open(notebook, tmp_path)
    assert widget._auxiliary_selected(home=False)
    assert widget.navigate_to_address(0x1000) is False
    assert widget.tabs.currentWidget() is widget.onboarding


def test_steps_move_with_back_next_and_the_progress_pills(qapp):
    page = OnboardingWidget()
    try:
        assert page.step == 0 and not page.back_button.isEnabled()
        page.advance()
        assert page.step == 1 and page.back_button.isEnabled()
        page.pills.stepClicked.emit(4)
        assert page.step == 4 and page.pills.current == 4
        page.back_button.click()
        assert page.step == 3
        finished = []
        page.finished.connect(lambda: finished.append(True))
        page.show_step(page.pages.count() - 1)
        QTest.keyClick(page, Qt.Key.Key_Return)
        assert finished == [True]
    finally:
        page.deleteLater()


def test_tour_settings_set_application_defaults(qapp):
    preferences = editor_preferences()
    page = OnboardingWidget(preferences=preferences)
    try:
        controls = page.controls
        assert set(controls) == {'hlil_show_addresses', 'export_embeds_as_code', 'show_authors',
                                 'mcp_default_allow', 'mcp_allow_delete', 'mcp_allow_create'}
        assert controls['show_authors'].isChecked()
        controls['show_authors'].click()
        assert preferences.state('show_authors') == (False, Scope.APPLICATION)

        # The defaults: new notes off the whitelist, agents may create notes and delete
        # whitelisted ones.
        assert not controls['mcp_default_allow'].isChecked()
        assert controls['mcp_allow_create'].isChecked()
        assert controls['mcp_allow_delete'].currentData() == 'Whitelisted'
        controls['mcp_allow_delete'].setCurrentIndex(controls['mcp_allow_delete'].findData('All'))
        controls['mcp_default_allow'].click()
        controls['mcp_allow_create'].click()
        assert preferences.get('mcp_allow_delete') == 'All'
        assert preferences.get('mcp_default_allow') is True
        assert preferences.get('mcp_allow_create') is False

        # Changes made elsewhere show up in the tour.
        preferences.set('show_authors', True, Scope.APPLICATION)
        assert controls['show_authors'].isChecked()
    finally:
        page.deleteLater()


def test_embed_samples_follow_the_address_setting(qapp):
    preferences = editor_preferences()
    page = OnboardingWidget(preferences=preferences)
    try:
        assert page.embeds and all(isinstance(sample, EmbedSample) for sample in page.embeds)
        sample = page.embeds[0]
        with_addresses = sample.code(500)
        assert with_addresses.tab_stop > 0 and '100003f24' in with_addresses.text
        page.controls['hlil_show_addresses'].click()
        assert preferences.get('hlil_show_addresses') is False
        without = sample.code(500)
        assert without.tab_stop == 0 and '100003f24' not in without.text
        assert without.width < with_addresses.width
    finally:
        page.deleteLater()


def test_function_sample_has_a_signature_header(qapp):
    sample = EmbedSample(onboarding_module.BUILT_IN.function, 'Header parser')
    try:
        code = sample.code(600)
        assert code.header_height > 0 and code.title_height > 0
        assert 'parse_header(struct header* header)' in code.text
    finally:
        sample.deleteLater()


def test_authors_step_names_the_enterprise_user(qapp, monkeypatch):
    def texts(page):
        return ' '.join(label.text() for label in page.findChildren(onboarding_module.QLabel))

    def writers(page):
        sample = page.findChild(onboarding_module.AuthorSample)
        return sample.bars()[sample.SHARED].writers

    monkeypatch.setattr(onboarding_module, 'enterprise_user', lambda: 'ada')
    signed_in = OnboardingWidget()
    monkeypatch.setattr(onboarding_module, 'enterprise_user', lambda: None)
    monkeypatch.setattr(onboarding_module, 'person_name', lambda: 'local')
    elsewhere = OnboardingWidget()
    try:
        # You are the signed-in Enterprise user, else the OS user; the line above is the same.
        assert writers(signed_in) == (('Claude', CLAUDE_HUE), ('ada', PEOPLE_HUES[0]))
        assert writers(elsewhere) == (('Claude', CLAUDE_HUE), ('local', PEOPLE_HUES[0]))
        for page in (signed_in, elsewhere):
            assert 'Attribution for both Enterprise teams and agentic workflows!' in texts(page)
            assert '(' not in texts(page).split('agentic workflows!')[1][:3]
    finally:
        signed_in.deleteLater()
        elsewhere.deleteLater()


def test_author_sample_shows_several_people_and_a_shared_line(qapp):
    sample = onboarding_module.AuthorSample(person='cynder')
    try:
        bars = sample.bars()
        assert [bar.label for bar in bars] == [
            'cynder', 'Alice', 'Claude', 'Claude & cynder', 'Astra', 'Bob & Astra', 'Alice & Bob',
            'Qwen']
        # Each person their own primary color; each agent its vendor's: Claude orange, Astra
        # (OpenAI) purple, Qwen yellow. Shared lines hand off, oldest writer on top.
        blue, red, green = PEOPLE_HUES
        assert [tuple(hue for _top, _bottom, hue in bar.sections) for bar in bars] == [
            (blue,), (red,), (CLAUDE_HUE,), (CLAUDE_HUE, blue), (OPENAI_HUE,),
            (green, OPENAI_HUE), (red, green), (AGENT_HUE,)]
        shared = bars[sample.SHARED]
        # The edited line is half the agent's (oldest, on top), half yours.
        assert shared.sections[0][1] - shared.sections[0][0] == (shared.bottom - shared.top) / 2
    finally:
        sample.deleteLater()


def test_author_sample_names_the_hovered_paragraph(qapp):
    from PySide6.QtCore import QEvent as Event
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    sample = onboarding_module.AuthorSample(person='cynder')
    sample.resize(400, sample.sizeHint().height())
    try:
        sample.present()
        assert sample.current == 0 and sample.tag.text == 'cynder'

        def hover(index):
            run = sample.bars()[index]
            point = QPointF(100, (run.top + run.bottom) / 2)
            QApplication.sendEvent(sample, QMouseEvent(
                QMouseEvent.Type.MouseMove, point, point, Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))

        sample.start_cycle()
        assert sample.cycle.state() == sample.cycle.State.Running
        hover(1)
        assert sample.current == 1 and sample.tag.text == 'Alice'
        assert sample.cycle.state() == sample.cycle.State.Stopped  # Hovering interrupts it.
        hover(sample.SHARED)
        assert sample.tag.text == 'Claude & cynder'
        sample.cycle.setCurrentTime(0)  # A stray tick changes nothing while hovering.
        assert sample.current == sample.SHARED
        QApplication.sendEvent(sample, QEvent(Event.Type.Leave))
        # The sweep is over, not restarted: the last of it rests out.
        assert sample.played and sample.cycle.state() == sample.cycle.State.Stopped
        assert sample.current == sample.CYCLED - 1 and sample.tag.text == 'Claude & cynder'
        sample.start_cycle()
        assert sample.cycle.state() == sample.cycle.State.Stopped
    finally:
        sample.cycle.stop()
        sample.deleteLater()


def test_author_sample_sweeps_through_the_first_four_once(qapp):
    sample = onboarding_module.AuthorSample(person='cynder')
    sample.resize(500, sample.heightForWidth(500))
    try:
        sweep = sample.SWEEP
        times = range(0, sweep + 1, 50)
        indexes = [sample.cycle_index(time) for time in times]
        assert indexes[0] == 0 and indexes == sorted(indexes)
        assert set(indexes) == {0, 1, 2, 3} and sample.cycle_index(sweep) == 3
        # Eased in and out (quadratic): slow at both ends, fast through the middle.
        changes = [time for time, previous, index in zip(times[1:], indexes, indexes[1:],
                                                         strict=False) if index != previous]
        assert changes[0] > sweep / 4 and changes[-1] < sweep * 3 / 4
        sample.present()
        labels = []
        for time in (0, sweep * 0.4, sweep * 0.6, sweep):
            sample.cycle.setCurrentTime(round(time))
            labels.append(sample.tag.text)
        assert labels == ['cynder', 'Alice', 'Claude', 'Claude & cynder']
        assert sample.cycle.loopCount() == 1
        sample.start_cycle()
        sample.cycle.setCurrentTime(sweep)  # Finishes the one sweep.
        assert sample.played and sample.resting == sample.CYCLED - 1
        sample.start_cycle()  # Showing the step again doesn't replay it.
        assert sample.cycle.state() == sample.cycle.State.Stopped
    finally:
        sample.deleteLater()


def test_author_sample_embeds_code_in_the_first_paragraph(qapp):
    sample = onboarding_module.AuthorSample(person='cynder')
    try:
        line, _gap = sample._metrics()
        first = sample.bars(500)[0]
        code = sample.code(500)
        assert 'read_length(header->size, 0x200)' in code.text and code.title_height > 0
        # One bar, yours, beside both lines of text and the embed below them.
        assert first.writers == (('cynder', PEOPLE_HUES[0]),)
        assert first.bottom - first.top == 2 * line + code.height + 2 * sample.EMBED_PADDING
        sample.set_show_addresses(False)
        assert '100003f24' not in sample.code(500).text
    finally:
        sample.deleteLater()


def test_open_onboarding_shows_the_tour_beside_open_notes(notebook, tmp_path):
    store = _store(tmp_path)
    onboarding_module.mark_onboarded(store)
    widget = notebook(FakeView('# Kept\n'), state_store=store, tab_factory=StandaloneNoteTabs)
    note = widget.current_note
    page = widget.open_onboarding()
    assert widget.onboarding is page and widget.tabs.currentWidget() is page
    assert note in _pages(widget)  # Notes stay open beside it.
    widget.tabs.selectWidget(note)
    assert widget.open_onboarding() is page  # Again: the same tab, selected.
    assert widget.tabs.currentWidget() is page
    page.show_step(page.pages.count() - 1)
    page.advance()
    _settle()
    assert widget.onboarding is None and note in _pages(widget)
    new = widget.current_note
    assert new is not note and widget.tabs.currentWidget() is new
    assert new.session.source == ''


class _Function:
    def __init__(self, name, start, size, statements):
        self.name, self.start, self.total_bytes = name, start, size
        self.hlil_if_available = None if statements is None else _HLIL(start, statements)


class _HLIL:
    def __init__(self, start, count):
        self.instructions = [type('Statement', (), {'address': start + 4 * index})()
                             for index in range(count)]


def test_tour_samples_prefer_a_small_named_function(monkeypatch):
    functions = [
        _Function('sub_1000', 0x1000, 40, 5),      # unnamed
        _Function('__Z3fooi', 0x2000, 40, 5),      # mangled
        _Function('big_one', 0x3000, 4000, 5),     # too long
        _Function('pending', 0x4000, 40, None),    # no HLIL yet
        _Function('one_liner', 0x5000, 20, 1),     # too few statements
        _Function('udf_read', 0x6000, 64, 6),
    ]
    data = type('View', (), {'functions': functions})()
    seen = []

    def snapshot(_data, address, *, expand_block=False, il_mode=onboarding_module.ILMode.HLIL):
        seen.append((address, expand_block, il_mode))
        return onboarding_module.HLILLine(address, 'code', available=True)

    monkeypatch.setattr(onboarding_module, 'il_snapshot_at', snapshot)
    samples = onboarding_module.file_samples(data)
    assert samples.name == 'udf_read' and samples.expression == 'udf_read+0x4'
    # Its neighbors in the Symbols list: up to three before it and three after.
    assert samples.symbols == ('big_one', 'pending', 'one_liner', 'udf_read')
    assert samples.symbols[samples.symbol] == 'udf_read'
    assert seen == [(0x6004, False, onboarding_module.ILMode.HLIL),
                    (0x6000, True, onboarding_module.ILMode.HLIL),
                    (0x6004, True, onboarding_module.ILMode.DISASM)]

    monkeypatch.setattr(onboarding_module, 'il_snapshot_at',
                        lambda *_args, **_kwargs: onboarding_module.HLILLine(0, 'x'))
    assert onboarding_module.file_samples(data) is None  # Unavailable code: built-in samples.


def test_file_samples_replace_the_built_in_code(qapp):
    page = OnboardingWidget()
    try:
        line = onboarding_module.HLILLine(0x6004, 'udf code', available=True)
        page.use_samples(onboarding_module.Samples('udf_read', 4, line, line, line))
        assert all(sample.preview is line for sample in page.embeds)
        assert page._sources['statement'].text == (
            '[udf_read](binaryninja://?expr=udf_read+0x4)')
        assert page._sources['disassembly'].text == (
            '[!](binaryninja://?expr=udf_read+0x4 "disasm")')
        assert page.linear_sample.preview is line and page.linear_sample.address == 0x6004
        assert page.symbols_sample.symbols == onboarding_module.BUILT_IN.symbols  # None given.
        page.use_samples(onboarding_module.Samples('udf_read', 4, line, line, line,
                                                   ('udf_open()', 'udf_read()'), 1))
        assert page.symbols_sample.symbols[page.symbols_sample.selected] == 'udf_read()'
    finally:
        page.deleteLater()


def test_embed_step_shows_the_copy_menus_in_symbols_and_linear(qapp):
    page = OnboardingWidget()
    try:
        symbols, linear = page.symbols_sample, page.linear_sample
        assert ('Copy BNURL to Function', 'selected') in symbols.MENU
        assert ('Copy BNURL to Address', 'selected') in linear.MENU
        assert symbols.symbols[symbols.selected].startswith('parse_header(')
        header, body, selected = linear._rows()
        assert header and body[selected].address == onboarding_module.BUILT_IN.statement.address
        for width in (220, 600):  # Menus stay inside their pane, however narrow.
            for pane in (symbols, linear):
                pane.resize(width, pane.sizeHint().height())
                menu = onboarding_module.menu_size(pane.MENU, pane.font())
                assert pane._menu_origin().x() + menu.width() <= width
    finally:
        page.deleteLater()


def test_embed_pages_show_how_to_jump_to_code(qapp):
    page = OnboardingWidget()
    try:
        assert page.titles == ('Welcome', 'Embed code', 'Embed entire blocks of IL', 'Export',
                               'Authors', 'Agents')
        pinned = [sample for sample in page.embeds if sample.hint]
        assert len(pinned) == 1 and pinned[0].hovered  # The Embed code step's last embed.
        texts = ' '.join(label.text() for label in page.findChildren(onboarding_module.QLabel))
        assert '-click an embed to go there.' in texts
        # Every other embed shows the shortcut while hovered, as a note's do.
        other = next(sample for sample in page.embeds if not sample.hint)
        assert not other.hovered
        QApplication.sendEvent(other, QEvent(QEvent.Type.Enter))
        assert other.hovered
        QApplication.sendEvent(other, QEvent(QEvent.Type.Leave))
        assert not other.hovered
    finally:
        page.deleteLater()


def test_export_page_shows_what_export_writes_and_sets_it(qapp):
    preferences = editor_preferences()
    page = OnboardingWidget(preferences=preferences)
    try:
        page.resize(420, 700)
        page.show()
        page.show_step(page.titles.index('Export'))
        qapp.processEvents()
        sample, link = page.export_sample, page._sources['export']
        name, start = BUILT_IN.name, BUILT_IN.statement.address
        assert sample.isVisible() and not link.isVisible()
        assert sample.lines() == [name, '```c',
                                  f'{start:x}  int32_t length = read_length(header->size, 0x200)',
                                  '```']
        assert {role for run in sample.runs() for _chunk, role in run} >= {
            None, 'fence', 'syntax_keyword'}
        # Narrow: the long line wraps rather than being cut off.
        line = QFontMetricsF(sample.font()).height()
        assert sample.height() >= sample.heightForWidth(sample.width()) > 4 * line
        page.controls['hlil_show_addresses'].setChecked(False)
        assert sample.lines()[2] == 'int32_t length = read_length(header->size, 0x200)'
        check = page.controls['export_embeds_as_code']
        assert check.isChecked()
        check.setChecked(False)
        assert preferences.get('export_embeds_as_code', Scope.APPLICATION) is False
        assert link.isVisible() and not sample.isVisible()
        assert link.text == f'[{name}](binaryninja://?expr={BUILT_IN.expression})'
    finally:
        page.deleteLater()


def test_welcome_note_has_a_highlighted_code_block(qapp):
    page = OnboardingWidget()
    try:
        block = next(item for item in page.findChildren(onboarding_module.CodeBlockSample)
                     if type(item) is onboarding_module.CodeBlockSample)
        assert block.language == 'c'
        runs = block.runs()
        assert runs[0] == [('```c', 'fence')] and runs[-1] == [('```', 'fence')]
        assert ''.join(chunk for run in runs[1:-1] for chunk, _role in run) == (
            block.code.replace('\n', ''))
        roles = {role for run in runs[1:-1] for _chunk, role in run}
        assert {'syntax_keyword', 'syntax_number'} <= roles  # `if`/`return`, `0x200`.
    finally:
        page.deleteLater()


def test_blocks_page_demos_the_il_choice_under_its_own_subheading(qapp):
    page = OnboardingWidget()
    try:
        column = page.pages.widget(page.titles.index('Embed entire blocks of IL')).widget()
        labels = [label.text() for label in column.findChildren(onboarding_module.QLabel)]
        assert labels.index('Embed entire blocks of IL') < labels.index('Choose the IL')
        stages = column.findChildren(onboarding_module.Stage)
        assert len(stages) == 2  # The whole block, then the IL choice.
        sources = [source.text for stage in stages
                   for source in stage.findChildren(onboarding_module.SourceSample)]
        assert '"disasm"' not in sources[0] and sources[1].endswith('"disasm")')
    finally:
        page.deleteLater()


def test_choose_the_il_shows_the_embed_menu_choosing_disassembly(qapp):
    page = OnboardingWidget()
    try:
        sample = page.il_menu_sample
        assert ('Select IL mode', 'selected submenu') in sample.MENU
        items = dict(item for item in sample.submenu() if item[1] != '-')
        assert items['Disassembly'] == 'selected' and items['HLIL'] == 'checked'
        assert {'LLIL', 'MLIL', 'Mapped MLIL'} <= set(items)
        for width in (320, 700):  # Both menus stay inside the sample.
            for origin, menu in sample.menus(width):
                size = onboarding_module.menu_size(menu, sample.font())
                assert origin.x() >= 0 and origin.x() + size.width() <= max(width, size.width())
                assert origin.y() + size.height() < sample.heightForWidth(width)
        (main, _items), (submenu, _subitems) = sample.menus(700)
        row = main.y() + onboarding_module.MENU_PADDING + onboarding_module.MENU_ROW
        assert submenu.y() + onboarding_module.MENU_PADDING == row  # Beside Select IL mode.
        line = onboarding_module.HLILLine(0x6004, 'udf code', available=True)
        page.use_samples(onboarding_module.Samples('udf_read', 4, line, line, line))
        assert sample.preview is line
    finally:
        page.deleteLater()


def test_show_author_bar_toggles_the_authors_sample(qapp):
    preferences = editor_preferences()
    page = OnboardingWidget(preferences=preferences)
    try:
        sample, legend = page.author_sample, page.author_legend
        assert sample.bars_shown and sample.text_left == sample.TEXT
        page.controls['show_authors'].click()
        assert preferences.get('show_authors') is False
        assert not sample.bars_shown and sample.text_left == 0 and legend.isHidden()
        sample.present(1)  # No names without bars.
        assert sample.tag.text != 'Alice'
        sample.start_cycle()
        assert sample.cycle.state() == sample.cycle.State.Stopped
        page.controls['show_authors'].click()
        assert sample.bars_shown and sample.text_left == sample.TEXT and not legend.isHidden()
    finally:
        page.deleteLater()
