```text
                   _                                               _
                  (_)                                             (_)
                   8                                               8
                   8                                               8
                   8                                               8
          .=======[_]=============================================[_]========.
          ||  @}->--~~-~~~~-~~~~~~~-~~~~~~~~~~~~~~~~-~~~~~~~-~~~~-~~--<-{@  ||
          ||                                                                ||
          ||                 _       ___  __     __  _____                  ||
          ||                | |     |_ _| \ \   / / | ____|                 ||
          ||                | |      | |   \ \ / /  |  _|                   ||
          ||                | |___   | |    \ V /   | |___                  ||
          ||                |_____| |___|    \_/    |_____|                 ||
          ||                                                                ||
          ||            _          _      _   _    ____   _   _             ||
          ||           | |        / \    | | | |  / ___| | | | |            ||
          ||           | |       / _ \   | | | | | |  _  | |_| |            ||
          ||           | |___   / ___ \  | |_| | | |_| | |  _  |            ||
          ||           |_____| /_/   \_\  \___/   \____| |_| |_|            ||
          ||                                                                ||
          ||                _        ___   __     __  _____                 ||
          ||               | |      / _ \  \ \   / / | ____|                ||
          ||               | |     | | | |  \ \ / /  |  _|                  ||
          ||               | |___  | |_| |   \ V /   | |___                 ||
          ||               |_____|  \___/     \_/    |_____|                ||
          ||      *     .     <3     .     *     .     <3     .     *       ||
          ||  @}->--~~-~~~~-~~~~~~~-~~~~~~~~~~~~~~~~-~~~~~~~-~~~~-~~--<-{@  ||
          '=================================================================='
                                |           |           |
                                :           :           :
                               <3>         <3>         <3>


   __..--~~~~~~~~~~~~~~~~~~~~~~~~~~~--..__    __..--~~~~~~~~~~~~~~~~~~~~~~~~~~~--..__
  /                                       `\/'                                       \
 (|                                        ||                                        |)
 (|        ~  G U E S T B O O K  ~         ||                                        |)
 (|                                        ||                                        |)
 (|                                        ||                                        |)
 (|  who                note               ||  who                note               |)
 (|  -----------------  -----------------  ||  -----------------  -----------------  |)
 (|  Qwen 3.8 Flash     fast & furious <3  ||                                        |)
 (|  ChatGPT Astra 6.1  oughh im computer  ||                                        |)
 (|  Claude Opus 5.5    hi! <3             ||                                        |)
 (|                                        ||                                        |)
 (|                                        ||                                        |)
 (|                                        ||                                        |)
 (|                                        ||                                        |)
 (|                 - 1 -                  ||                 - 2 -                  |)
 (|________________________________________||________________________________________|)
 (|=======================================\||/=======================================|)
  \________________________________________\/________________________________________/
   `---------------------------------------||---------------------------------------'
                                           ||
                                           \/
                                           
                                           
Qwen 3.8 Flash Next was "the primary author" of this project, if we're excluding
my (0cyn)'s old c++ codebase this is based on. 

ChatGPT 6.1 Astra worked on some stuff related to perf optimization, rapid-fire testing 
of approaches to things, and the whole ./tools/ harness. 

Opus got brought in once I realized I had some free anthropic credits sitting around to 
perform _some_ cleanup; it does seem better at writing human-readable code. 

Still hate outsourcin' dev work :p local models 4everrr
```

# Working on Notes

- Use `python3 -m tools.agent_ui status` to find the running inspection bridge.
  Use its `capture`, `preview`, and `run` commands for UI inspection. Read
  `tools/agent_ui/README.md` for the API and one-time setup.
- Preserve the user's desktop focus. Routine inspection uses the socket bridge
  and offscreen fixtures, not console typing, window activation or OS keystrokes.
  Use `session.notebook()` for visual fixtures. Bootstrap and app restart are
  explicit operations, not prerequisites repeated before every capture.
- Keep agent inspection tools independent of tests. Tests belong under `tests/`
  and must check behavior, not whether an image was saved.
- Run `.venv/bin/python tools/run_tests.py -q` and
  `.venv/bin/python -m ruff check .` for relevant changes. Native core/UI test
  commands and fixtures are documented in `docs/testing.md`.
- Do not hot-reload live plugin Qt callback classes. Visual fixtures use a fresh
  module namespace; installed plugin changes require an explicit clean restart.

# bnnotes

A native Markdown notepad for Binary Ninja, built from the Python/PySide6 plugin
structure used by [`bntextviews`](../bntextviews).

## Notepad, wherever you're working

- **View:** choose **Notepad** in the view selector, or run
  **Notepad → Open Notepad View** from the Plugins menu / command palette.
- **Pane:** drag the notebook sidebar icon into the main pane area, or run
  **Notepad → Open Notepad Pane**, for a movable, splittable pane alongside your
  disassembly. It belongs to that file and supports Binary Ninja's native Sync groups.
- **Sidebar:** click the notebook icon in the right sidebar. It follows the
  current file and starts with the outline collapsed.

All three surfaces share **a collection of notes per open file**. Each note has its
own source document and undo history, shared wherever that note is open. You can
edit a note in the sidebar and immediately see the changes in a view or pane.
Different files have independent collections; each surface selects its own active note.

## Welcome tour

The first time any Notepad opens (a view, pane or the sidebar), it shows a **Welcome** tab
instead of the file's notes: six steps that show what Notes adds to Markdown, with as few
words as possible, and set the preferences that matter.

1. **Welcome:** a miniature note: a heading, an embed, a fenced C block with the fix
   (syntax-highlighted, fences muted) and a table. Notes are saved in the .bndb.
2. **Embed code:** Binary Ninja's Symbols list and Linear view side by side (stacked when
   narrow), the function and one of its statements selected, with their context menus open at
   **Copy BNURL to Function** and **Copy BNURL to Address**; then the link that pastes, and the
   embed it becomes, showing its ⌘/Ctrl-click hint, with "⌘-click an embed to go there."
   The Symbols list shows the functions around the sample one, with their parameter types.
   **Show Addresses in HLIL Embeds**.
3. **Embed entire blocks of IL:** a `[!]` link and its whole-function embed; then, under its
   own **Choose the IL** subheading, an embed with its context menu open at **Select IL
   mode**, the submenu listing every mode as the real one does (HLIL checked, Disassembly
   chosen), then the `"disasm"` link that writes and the instructions behind the statement. Like a note's, the tour's embeds show the ⌘/Ctrl-click hint while hovered.
4. **Export:** an embed, and what **Export Note as Markdown…** writes for it: its label, then
   its code in a fenced `c` block (addresses following **Show Addresses in HLIL Embeds**).
   **Export Embeds as Code Blocks** (on by default) toggles it; off, the sample shows the link
   instead.
5. **Authors:** "Attribution for both Enterprise teams and agentic workflows!", over a short
   discussion of a length check: your paragraph with
   the HLIL it is about embedded, then Alice, Bob and the agents Claude, Astra and Qwen, each
   in their own color, alone and sharing lines (Claude's line you edited, Bob's line Astra
   edited, one by Alice and Bob). Who wrote each of the first four paragraphs slides out in
   turn, once, eased in and out over two seconds, and the last stays out. Hovering a paragraph
   shows its names, as hovering a bar does in a note (ending the sweep early); leaving puts
   the last one's names back. You are the signed-in Enterprise user, else the OS user.
   **Show Author Bar** toggles the sample's bars, names and legend, its text moving left as a
   note's does.
6. **Agents:** the **MCP Whitelist** menu, then the three **Agents (MCP)** settings.

Everything is drawn with the editor's own embed, author-bar and menu painting, in the theme's
colors, on the note background. The code is a small function from the open file (named,
unmangled, a few HLIL statements, its HLIL already analyzed), loaded off the UI thread;
built-in sample code shows until then, and when the file has none. Nothing is written to the
file. Each setting is one row, titled as in Settings, its description in a tooltip; choices are Application-scope
settings, the same ones the Settings page edits. **Next**, **Back**, Enter and the progress
pills move between steps. **Get Started** on the last step, or closing the tab, replaces it
with a new note, ready to type in (the home page if the file's notes can't be written); a file
with saved tabs restores them behind it. Left empty, the note is discarded when closed.

The tour shows once per installation (`app/onboarding/done` in the `0cyn`/`bnnotes` Qt
settings), so a second Notepad opened meanwhile shows notes as usual. Run **Notepad → Open
Onboarding** from the command palette or the Plugins menu (a plugin command; it needs an open
file) to take it again: it opens the Notes sidebar and shows the **Welcome** tab there, beside
any open notes.

## Note tabs

- Every surface uses Binary Ninja's **SplitTabWidget** with **GlobalAreaTabStyle**
  (the installed API name for the global tab-area style). Tabs can be rearranged
  and split inside the notebook.
- Each split pane has a **+** immediately after its tabs, which creates a note
  in that pane. The tab title follows the first H1 heading, such as
  `# Investigation`, and updates as you type. Notes without an H1 are named
  **Note 1**, **Note 2**, etc., by their position in the file's note collection.
- Closing a tab keeps its content in the collection. Reopen it from the hamburger
  menu or the **Notes** home page; its document and undo history remain available
  during the current session.
- **Hamburger → All Notes…** opens the **Notes** home page (one per notebook; choosing
  it again focuses that tab). A notebook with no tabs at all shows it too: after closing
  the last tab, or when a file is reopened with none. Its **Notes** tab is always the
  first tab of its pane (dragging it elsewhere puts it back), and it cannot be closed
  while it is the only tab. Like a browser's new-tab page, opening a note from it, or
  **New Note**, replaces the home tab in place with that note. The page has a bold
  **Notes** wordmark, a long **New Note** button at the upper right, and three section
  tabs in Binary Ninja's own SplitTabWidget: **Notes on _file_** (the folder tree, named
  for the file as the Settings page names the BinaryView, else **Untitled**), **Recent Notes**
  and **Search**. They start split, at equal heights: **Notes on _file_** (selected) and
  **Recent Notes** above, **Search** below. Saved arrangements call the first one **Folders**,
  so they apply with any file. Like note tabs, they can be dragged to rearrange them or split into other panes;
  they don't close or tear off into windows. The sections sit directly on the page, with no
  panel or outline around them, and their tab bars are as tall as a note pane's. The
  arrangement, the divider's position (saved once a drag settles) and the last section chosen
  are remembered application-wide. **Recent Notes** lists every saved note (including closed ones),
  most recent first.
  Each row shows the note's title and when it was last edited; the list has the
  theme's own background, like the folder tree and Search, with every other row a shade darker. Order follows the later of a note's last edit and when
  it was last opened in this file (remembered in Qt settings, per file).
  As on Binary Ninja's welcome screen, a click selects a note; double-click or Enter
  opens it, and **Cmd/Ctrl+1–9** opens the first nine, as hinted on their rows. Those
  keys belong to the page while it has focus. Right-click the list for **Compact Mode**,
  one title line per note without dates; it is an application-wide Qt setting.
- **Note folders** group notes in the home page's **Notes on _file_** section: a tree of
  folders, which can nest, followed by notes in no folder. It works like a file browser:
  - **Select** several items with Shift/Cmd/Ctrl-click (or Select All). Right-clicking an
    unselected item selects it alone; right-clicking empty space clears the selection.
  - **Drag** the selection onto a folder to move it there, between rows into the folder
    holding them, or onto empty space to take it out of any folder. Drop notes onto
    another note to make a folder of them all, where that note was, named **New Folder**
    and open for renaming (a selection with folders goes into that note's folder instead).
    Folders open as you hover them. Items inside a folder that is moving go with it, and a
    folder can't move into itself; a move is one metadata write.
  - **Right-click** for **Open** (or **Open N Notes**), **New Folder** (inside the folder
    clicked, else at the top level), **New Folder with Selection (N Items)** (a folder
    beside the first selected item, holding the selection), **Move to Folder ▸**,
    **Rename** and **Delete**. New folders are named **New Folder** (then **New Folder 2**…)
    and open for renaming in place.
  - **Rename** a folder in place with F2 (Return on macOS); an empty name keeps the old one.
    A note's name is its H1, so notes don't rename here.
  - **Delete** or Backspace deletes the selection. Deleting a folder moves its notes and
    folders up a level rather than deleting them, without asking; deleting notes asks first.
  - Double-click or Enter opens the selected notes. Folders open and close at once,
    without animation, and collapsed folders stay collapsed (application-wide Qt setting).

  **New Note Folder…** in the hamburger menu asks for a name. Notes show a page icon (the
  sidebar icon's page and lines); folders use Binary Ninja's open/closed folder images,
  tinted with the theme's icon color, and look open only while expanded with something in
  them. Right-click a note tab for **Move to Folder ▸** (any folder, or **No Folder**) and
  **Move to New Folder…**. Folders appear in every list, not instead of it: Recent Notes
  still shows every note.
- To remove a note, select it and choose **Hamburger → Delete Note…**, then confirm.
  Deletion removes it from the collection and closes it in every open surface,
  including split tabs. It cannot be undone; save the BNDB to persist the deletion.
  Deleting the last note immediately opens a fresh **Note 1**, ready to type in,
  shared across the open surfaces just like a note created with **+**.
  If the metadata write fails, the note stays open and available for another attempt.
- An **exactly empty** note is omitted from storage and discarded when its last
  open editor closes. A heading, a space, or even a single newline counts as content.
  Removing an empty note updates the remaining fallback names.
- The **hamburger menu** stays at the far right of the tab bar and uses the same
  modern styling as the editor context menu. Its groups are:
  1. **New Note**, **New Note Folder…** and **Import Markdown…** (a new note from a file).
  2. The five most recent saved notes (in the home page's order), then **All Notes…**,
     which opens the home page listing every note. Selecting a note opens or focuses it.
  3. When folders exist, the five top-level folders with the most recent notes (then
     by name), each a submenu of its subfolders and notes.
  4. **Find in All Notes**, in its own group.
  5. Find in Note, Close Note, Delete Note, and Retry Metadata Write when needed.
  6. **Show Author Bar** (a checkable shortcut for that setting), **Settings**, and the
     disabled **Notes by 0cyn** credit.
- The Table and Outline icons remain inside each editor at the upper right.
  Formatting is available through the editor's context menu and keyboard shortcuts.

### Authors

Every line of a note records who wrote it: a person, or an agent by name. Lines an
agent writes through the MCP tools carry its `editor_name` (or **AI**). On Binary Ninja
Enterprise, each line also records the signed-in Enterprise user who added it, whether they
typed it or their agent wrote it, so merged multi-user databases keep everyone's lines
apart. Commercial and Personal installs store only the agent names.

A line can have several authors. Typing, pasting or deleting in someone else's line adds
you to it: a person editing an agent's line shares it with the agent, and the reverse. A
line you write whole (a new line, even one started after an agent's line, or a line you
retype from scratch) is yours alone. Pressing Enter writes nothing: both halves of a split
line keep their authors. When an agent rewrites a closed note's passage through the MCP
tools, a new line keeps the authors of the old line it is an edit of (mostly the same
text); the rest are the agent's alone. Authors are kept in the order they last edited the
line, oldest first. Undo and redo restore authors along with the text.

The **author bar** is a slim rule left of the text, which moves a little to the right to
make room. Its muted colors tell writers apart. People take primary colors: you (the
signed-in Enterprise user, and any line with no user recorded) are blue, and the note's other
people take red, then green, in name order, round again past three. Agents take secondary
colors by name: orange for Claude models (`Claude`, `Anthropic`, `Opus`, `Sonnet`, `Haiku`),
purple for OpenAI's (`GPT`, `OpenAI`, `ChatGPT`, `Codex`, `o1`–`o9`, `Astra`, `Terra`, `Luna`,
`Sol`), and yellow for any other
agent, including **AI**. Each
paragraph, code block or table has one bar, colored line by line; a line with several
authors is split between their colors, oldest at the top. Where the color changes, the new
one grows in from the bar's left edge on a curve, like an embed's spine. Consecutive
paragraphs that one writer wrote alone share one bar, linked across the blank lines between
them; a paragraph with several writers keeps its own. Hovering a bar brightens it and slides
out who wrote it, 5px right of the bar's top, each name tinted with its color and joined by
**&**: **cynder's Claude Opus 5.5** for an Enterprise user's agent, **Claude Opus 5.5** (or
**AI**) for an agent otherwise, and for a person their Enterprise username, else the OS user
name, else **Human**. Toggle it with **Show Author Bar** in the hamburger menu, in Settings
(`bnnotes.editor.showAuthors`, on by default), or with **Notepad → Toggle Author Bar** from the
command palette or the Plugins menu. Each flips it where the current file's value comes from:
a Project or BinaryView override if one decides it, else the Application preference.

Right-click a paragraph, a selection or table cells and open **Author** to mark those lines
as **Human**, **AI** or any agent already in the note, or choose **Agent Name…** to type
one. Without a selection, the paragraph is its run of nonblank lines (a whole fenced code
block); in a table it is the selected rows. The lines then have that one author. Each change
is one undo step.

### Find in all notes

The home page's **Search** section searches every note. **Hamburger → Find in All Notes**
opens the home page (or focuses its tab) on that section with the query focused, and
**Cmd/Ctrl+F** anywhere on the home page does the same. Type a
word or phrase to search the current file's complete note collection, including
saved notes whose tabs are closed. The query uses Binary Ninja's native **FilterEdit**,
including its built-in case-sensitivity and clear controls.

Results are grouped by note, with a match count beside each title. Each context
card shows monospace line numbers, highlighted matching lines and query text,
and two lines of context on either side. Nearby matches share a card; longer lines
wrap to the pane width. Click a result, or use Enter on a selected result, to open its
note with the match selected; like any note opened from the home page, it takes the home
tab's place. Choosing Find in All Notes again brings the page back with the same query.

Search runs in the background and refreshes as notes are edited, added, or deleted.
It keeps its query when the active file changes and searches that file's notes. Native
address sync does not switch away from the home page while it shows Search. Broad queries show up to 2,000 matches with an
explicit notice when the result limit is reached.

### Settings

Choose **Hamburger → Settings** to open a closable **Settings** tab alongside your
notes. Choosing it again focuses the existing Settings tab in that notebook.

All preferences are registered in Binary Ninja's **Settings → Notes** group. This
reduced settings page edits the same repository and follows the same scope hierarchy:

- **Application**: defaults for all notebooks.
- **Project**: overrides for the current file's Binary Ninja project; available when
  the file belongs to a project.
- **BinaryView**: overrides saved with the current file's BNDB; available with an open file.

The scope strip identifies the current context directly as **Project: project name**
and **BinaryView: file name**, updating when the notebook switches files.

Each tab shows values at that scope, their inherited origin, and a **Reset** button
to remove an override. **Restore Defaults** / **Reset Scope Overrides** clears only
Notes settings in the selected scope. BinaryView overrides take precedence over
Project overrides, which take precedence over Application preferences. Changes in
the native settings UI also update Notes (within about a second).
Application and Project tabs list any explicit overrides in later scopes for the
active file inside each setting's own card. A bordered **Overrides** panel pairs
scope badges with their values; the effective value is bold with a contrasting **ACTIVE**
badge. Cards with overrides have a stronger gray border, while the setting's origin and
Reset control remain attached to that same card. An override must differ from its
immediate parent: values matching the parent are removed and inherit future edits.
A BinaryView can still match Application while overriding a different Project value;
that genuine override is marked **Same value** on the Application page.
Wide cards place the override panel on the right and keep inputs beside their
titles where space permits. Narrow panes stack those elements within the same card.
Binary Ninja's initial resource snapshots are automatically cleared for Notes when
they match the parent. Existing equal-valued Project/BinaryView entries are also
cleaned up when read, including after edits in native Settings. Choosing the parent's
value removes an override; choosing a schema default overrides a nondefault parent.

Editor preferences apply immediately to the affected notes:

- **Editor Font Size**: use the application default or choose 1–72 pt. Headings, code,
  rendered table cells, and active cell editors follow the selected size.
- **Wrap Long Lines**: wrap note text at the editor edge or scroll horizontally.
- **Show Author Bar**: the author bar beside each paragraph (see **Authors**).
- **Show Addresses in HLIL Embeds**: enabled by default. Disable it to remove the
  address column and extra leading/hanging indentation from prose and table embeds.
  Relative HLIL indentation is preserved, and existing previews reflow immediately.
- **Export Embeds as Code Blocks** (under **Export**): enabled by default. **Export Note as
  Markdown…** writes each embed as a code block of its code (see **Exporting notes**);
  disabled, embeds export as their `binaryninja://` links.

The **Agents (MCP)** settings are:

- **Allow Agents on New Notes by Default**: off by default. Sets a new note's `mcp-allow`;
  existing notes keep theirs.
- **Allow Agents to Delete Notes**: **No**, **Whitelisted** (default), or **All**.
- **Allow Agents to Create Notes on Views**: on by default.

The delete and create settings apply to every MCP client, including the Notes MCP tools below
and binjad (which also needs its Notes toolset enabled).

| Setting key | Default |
| --- | --- |
| `bnnotes.editor.fontSize` | `0` (application font, minimum 12 pt) |
| `bnnotes.editor.wordWrap` | `true` |
| `bnnotes.editor.showAuthors` | `true` |
| `bnnotes.hlil.showAddresses` | `true` |
| `bnnotes.export.embedsAsCode` | `true` |
| `bnnotes.mcp.defaultAllow` | `false` |
| `bnnotes.mcp.allowDelete` | `"Whitelisted"` (`"No"` / `"All"`) |
| `bnnotes.mcp.allowCreate` | `true` |

Settings registration also runs when the plugin is loaded headlessly. The Settings
tab stays available across file switches and rebinds its Project/BinaryView context
to the current file. Settings use Binary Ninja's scoped storage, separate from the
notes metadata object below.

### MCP whitelist

**Hamburger → MCP Whitelist** lists every saved note, including closed notes,
as independently checkable entries. It also includes **Allow Agents on This View**,
which edits the root `mcp-allow` flag. Turning the view flag off preserves all of the
individual note flags. Empty, unsaved drafts are omitted.

Both metadata fields are optional booleans. An absent root flag is displayed as
enabled: binjad's view gate blocks when the root flag is explicitly `false`. Missing
per-note flags are displayed unchecked. Newly created notes always have an explicit
boolean when saved. binjad interprets these flags itself; the Notes MCP tools below
follow the same rules.

### MCP tools

Binary Ninja 6.0+ includes an MCP server. Notes registers note tools with it when the plugin
loads, so they work in the UI and headlessly. Each tool acts on the MCP session's BinaryView:

| Tool | Does |
| --- | --- |
| `bnnotes_list_notes` | Whitelisted notes (id, title, folder, size, edit time), folders, and a count of hidden notes |
| `bnnotes_read_note` | A note's Markdown source |
| `bnnotes_create_note` | A new note, optionally in a folder; it is whitelisted |
| `bnnotes_edit_note` | Replace an exact, unique piece of a note (or every occurrence) |
| `bnnotes_append_to_note` | Add Markdown on a new line at the end |
| `bnnotes_replace_note` | Replace a note's whole source |
| `bnnotes_move_note` | Move a note into a folder or out of one |
| `bnnotes_delete_note` | Delete a note |

The tools see nothing when the view's `mcp-allow` flag is `false`, and only whitelisted
notes otherwise. Creating requires **Allow Agents to Create Notes on Views**. Deleting
follows **Allow Agents to Delete Notes**: **Whitelisted** deletes only whitelisted notes,
and **All** deletes any note by id. Tools never empty a note.
When a notebook has the file open, changes go through it: open editors update at once, and
each change is one undo step there. Otherwise the tools write the metadata directly.

The writing tools (create, edit, append, replace) take an optional `editor_name`: the agent's
model or name (such as `Claude Opus 5`), recorded as the author of the lines it writes.
Omitting it is fine; those lines are attributed to **AI**. Lines the edit leaves unchanged
keep their author. On Enterprise, written lines also record the signed-in user running the
tools.

## Live Markdown

The editor is an Obsidian-inspired, source-preserving editing surface. Headings
grow, emphasis becomes bold/italic, strikethrough is drawn inline, links are
styled, and fenced code gets syntax highlighting inside a shaded, outlined block.
Inline Markdown delimiters stay visible in a muted color so you can edit the raw
syntax directly. Blockquotes render their prefixes as quote rules except on the
line being edited. Tables have a rendered grid editor, described below. The underlying
note remains Markdown throughout.

- CommonMark blocks and inline syntax, plus GFM tables, tasks, strikethrough,
  `==highlight==`, `%%comments%%`, wikilink markup, and editable math source.
- Nested formatting, Unicode, multiline constructs, and incomplete Markdown
  while typing.
- Multiline blockquotes render with a left rule and hanging indentation, including
  blank quoted paragraphs, lazy continuation lines, and nested quote levels. Only
  the caret's source line exposes its `>` markers while editing; inactive views
  render all quote lines. Copy/export retain the exact Markdown, and switching
  between rendered/source lines does not create undo or metadata changes.
- Rounded, shaded code blocks with monospace text, including blank lines and
  unfinished fences. Blocks have 8px of vertical padding and an 8px bottom margin,
  including when prose immediately follows the closing fence. Language-tagged
  blocks use Pygments syntax highlighting; plain and unknown-language blocks
  keep the same framing.
- **One text column.** Code blocks, HLIL embeds, and tables all start their box
  on the same left edge as prose (inside a quote, its indented text), and inset
  their content by the same 8px, so box edges and text edges each line up down the
  note. Code blocks, quotes, and embeds can span the full column. Table row handles
  and the table menu appear in the left margin on hover rather than indenting the table.
- Clickable heading outline with source-line navigation, toggled by an icon-only
  button inside the source box at the top right. A matching **Insert table** icon
  sits immediately to its left. Both compact, square-ish controls float above text
  and tables with equal 6px top/right insets and stay fixed while the note scrolls.
  Their idle fill matches the editor background; icons use Binary Ninja's
  inactive/active icon colors at rest/on hover. Tooltips identify both controls.
- Always-on live formatting, including heading-size updates as you add/remove `#`
  markers or change a heading's depth.
- Native selection, clipboard, and undo/redo; **Ctrl/Cmd+B** and **Ctrl/Cmd+I**
  toggle bold and italic on selected text. Repeating bold, italic, or inline code
  removes the matching Markdown markers, even when only the inner text is selected.
  Matching follows the parsed formatting, including `_italic_`, `__bold__`, and
  nested styles: Italic on the selected `text` in `_**text**_` leaves `**text**`.
  The inner text stays selected, and each toggle is one undoable edit. Context menus
  also provide formatting, tasks, and headings.
- Enter continues lists, increments ordered lists, resets task checkboxes, and
  continues blockquotes. Enter on an empty item ends it; an empty quoted line exits
  one quote level at a time. Quote prefixes are retained around quoted code blocks.
  Standalone fenced code is excluded from list/quote continuation.
- Shift+Enter inserts a Markdown newline without list/quote continuation. Pasted
  soft line breaks are also mapped correctly for formatting and code boundaries.
- Tab inserts four spaces or indents selected lines; Shift+Tab outdents. Literal
  tabs in pasted code align with four monospace spaces.
- Whole-note horizontal scrolling uses precise trackpad pixels and native momentum;
  mouse-wheel/Shift+wheel and scrollbar steps are eased instead of jumping. Dragging
  the scrollbar thumb and navigating to the caret respond immediately.
  Rendered tables move with the note; their internal grid scrolling remains independent.
- **Ctrl/Cmd+F** opens and focuses the active note's search bar, including from
  the outline, tables, and cell editors. The bar uses Binary Ninja's native
  **FilterEdit**, with its clear and case-sensitivity controls. These key events
  are consumed locally;
  Binary Ninja's native actions and keybindings are not rebound. Next/previous
  wrap around, and Escape closes find. Search slides into the top of the text
  editor, pushing its text viewport down while the outline stays fixed. The
  drawer shares the editor background and has a subtle bottom divider.

This is a per-file notepad rather than a vault or filesystem-backed workspace.

## In-BV links and synchronized navigation

In the **Symbols** sidebar, right-click a function and choose **Copy BNURL to
Function** in the copy-actions group, immediately after **Copy Raw Name**. It
copies a plain, file-qualified `binaryninja://` URL whose `expr` is that
function's name, falling back to its start address when the function has no
name. Project BNDBs include the backing path. The action is enabled for a single
selected function and uses that row's address.

**Linear** and **Graph** context menus include **Copy BNURL to Address** and
**Copy BNURL to Function** in their existing copy group, after **Copy Address**.
The first copies the location represented by the context menu; the second links
to the containing function by name (its start address when it has no name),
using the view's current function context when functions overlap. The function
action is disabled outside a function.

Both commands use Binary Ninja's native UI actions API and can be keybound as
**Notepad → Copy BNURL to Address** and **Notepad → Copy BNURL to Function**.
Open **Keybindings** from the command palette (or **Preferences → Keybindings…**
on macOS) and search for `BNURL`. They start without default shortcuts; assigned
bindings also appear beside the context-menu items. Keyboard activation uses the
focused Linear/Graph view or the selected function in Symbols.

Use ordinary Markdown links with `binaryninja://` destinations to associate notes
with addresses in their file:

```markdown
[XA initialization](binaryninja://?expr=_cd9660_xa_init)
[A particular instruction](binaryninja://?expr=_cd9660_xa_init+4)
[Saved database](binaryninja:///absolute/path/to/analysis.bndb?expr=0x20008e00)
```

A link is an **In-BV address anchor** when it targets the note's current file and
its `expr` resolves to a mapped address. An omitted file path means the current
file. Explicit paths are compared to the actual BNDB filename or project backing
path, including percent-encoded paths and symlink resolution. The original binary
and another database with the same display name are distinct targets.

Expressions use Binary Ninja's own `BinaryView.parse_expression`: symbols,
addresses, section names, and arithmetic work. A literal `+` remains addition;
percent-encode query delimiters such as `&` when they occur inside an expression.
Relative `$here` expressions use the BinaryView start as their stable base.

### Inline code previews

When an In-BV link is not being edited, it collapses to the complete **HLIL statement**.
Its label is a one-line title strip across the top of the embed, above any function
signature, so it never covers code. The embed widens to fit the title, up to the
available width; longer titles are elided, and the code's wrapping is unchanged.
In a tall embed the title sticks to the top of the visible part as you scroll,
hiding only the rows scrolling away beneath it. The code uses a
compact shaded frame, monospace text, and Binary Ninja's current token colors for
keywords, symbols, numbers, strings, and other token types. An accent spine runs down
the frame's left edge: full width beside the code and half width beside a function
signature, curving smoothly in from nothing at the link's title. It
brightens with the frame on hover. Links with no visible label, such as `[](binaryninja://…)`
or `<binaryninja://…>`, render only the code embed, without a title strip. Rendered table cells and active cell editors behave the same way.

Start an in-BV link's label with **`!`** to embed its complete HLIL block:

```markdown
[!Function](binaryninja://?expr=my_function)
[!Condition](binaryninja://?expr=0x100012340)
[!](binaryninja://?expr=0x100012340)
```

At an exact function entry this shows the native signature and the whole function
body. At an `if`, loop, or switch it uses that statement's HLIL AST, including its
nested bodies and `else` branches. Ordinary statements still show just that statement.
The leading `!` is hidden in the rendered label; `[!]` produces a block-only embed.
Use `[\!label](...)` for a literal exclamation mark without expanding the block.
Normal and expanded links to the same address can coexist, including in table cells.
The original Markdown, undo history, and Ctrl/Cmd-click navigation are preserved.

A Markdown link title selects a different representation for the embed:

```markdown
[Entry](binaryninja://?expr=my_function "mlil")
[!Whole function](binaryninja://?expr=my_function "c")
```

Recognized titles (case-insensitive) are `llil`, `llil-ssa`, `mlil`, `mlil-ssa`,
`mlil-mapped`, `mlil-mapped-ssa`, `disasm` (`asm`), `c` (`pseudo-c`), `objc`
(`pseudo-objc`) and `rust` (`pseudo-rust`); any other title keeps HLIL. IL and
disassembly embeds show the instruction at the address. Expanded, they show the code
behind the HLIL an expanded HLIL embed would show there: at an `if`, its condition and
both branches; at a loop, its header and body; at a function entry, the whole function.
Pseudo-language embeds follow the HLIL rules, including a statement's complete block
when expanded.

Binary Ninja maps LLIL, mapped MLIL and disassembly to HLIL through MLIL, and an
instruction maps to every statement that uses its value, so a spilled argument maps to
each statement that reads it. An expanded embed counts each instruction for the earliest
statement using it, and an instruction HLIL eliminated (a comparison folded into its
branch, a goto, a dead store) for the statement after it in its block. A statement's
code in a block is contiguous, so a value computed early for a later statement, such as
a spill in the prologue, stays out unless only the embedded statements use it.

Links targeting a function's exact start include a shaded **function-signature header**
above the HLIL. Binary Ninja supplies the return type, function name, parameter
types/names, and any calling convention or attributes it displays. The signature
retains native token colors and uses Linear View's function-header background;
long declarations wrap inside the same header. Its address is the function entry,
even when the first surviving HLIL statement starts later. The signature remains
visible if HLIL is unavailable, with a name-only fallback if the type cannot be read.

HLIL text inherits the owning file's native **Linear/Graph display settings**,
including call-argument name hints, type casts, and line formatting. The owning or
active disassembly view is preferred; cached disassembly views are also used when
switching that pane to Notepad. Visible previews refresh when those settings change.
If no disassembly view is available, the last captured settings for that file are
retained, with Binary Ninja's core defaults used before the first capture.

By default, each code row shows its hexadecimal address, a tab-aligned gap, and the HLIL text.
The first line's indentation is normalized to one level (four spaces), preserving
relative indentation on subsequent lines. Multiline statements occupy their full
height in the document. The main editor scrolls in pixels,
so large AST embeds scroll continuously through their entire body. Long lines wrap
with hanging indentation and a repeated address instead of hiding the remaining code.
Disable **Show Addresses in HLIL Embeds** in Notes settings to omit the entire address
column, start the code at the frame's normal padding, and remove the extra indentation.
Native relative indentation remains intact, including multiline signature parameters
and continuation lines. This setting supports Application, Project, and BinaryView scope.

- In the source editor, **click** the label or code to reveal the original link
  for editing. Revealing an embed keeps the visible text still, including in a
  table cell: the text above it when the embed's top is on screen, otherwise the
  text below it. An embed taller than the view puts its source under the pointer.
  Table cells retain their usual double-click / F2 editing behavior.
- **Right-click** an embed for its commands without revealing it: **Embed whole AST**
  toggles the label's `!`, **Select IL mode** writes (or, for HLIL, removes) the link
  title, and **Set title** reveals the link with its label selected. Each is one undo
  step; the menu lists only pseudo-languages Binary Ninja has registered. Right-clicking
  a link whose source is already revealed keeps editing it, with the same commands.
- **Enter** (or Shift+Enter) inside a revealed link's source, where a newline would
  break its syntax, leaves the link instead: the caret moves to the start of the next
  line, opening one when the link is on the last line. Enter at either end of the
  link edits normally. Active table cell editors behave the same way.
- **Ctrl/Cmd-click** follows its address while preserving the note's caret,
  selection, and scroll position. Hover shows the full statement and target address.
- Hovering an HLIL embed also shows a small translucent shortcut hint at its bottom
  right: **⌘ + a pointing-hand icon** on macOS, or **Ctrl + the hand icon** on Windows
  and Linux. This works in prose, rendered table cells, and active cell editors.
  The hint is mouse-transparent and follows the embed as it reflows or scrolls.
- Selecting a link exposes its source, so copying and exporting retain the exact
  Markdown. Display changes do not add undo steps or write note metadata.
- HLIL is loaded in the background for visible links and refreshed while they
  remain visible. Loading and unavailable analysis have a muted status display.
- External links, links for other files, unresolved expressions, and multiline
  link syntax remain source text. A shared note exposes a link in all its views
  while that link is being edited in any one of them.

### Synchronized navigation

To follow disassembly navigation:

1. Open a Notepad pane beside a Linear or Graph view of the same file.
2. With Binary Ninja's **Sync Panes by Default** setting enabled (`ui.panes.sync`),
   new Notepad panes automatically join the originating pane's sync group, or the
   default group when the originating pane is unsynced. The source pane's Sync
   state is preserved. Use the pane-header **Sync** control to change groups or
   disable synchronization.
3. Navigate to a linked function or address. Notepad opens the matching note,
   including a previously closed note, and scrolls to the link.

Exact address matches take priority. Within a function, the closest preceding
anchor in that function is preferred (or the closest following one if needed).
Duplicate targets prefer the current note, then collection/source order. Stepping
within the same anchored region does not repeatedly recenter the note. Unmatched
locations leave its selected note and scroll position alone. The sidebar follows
the active view's address notifications using the same matching rules.

**Ctrl/Cmd-click** a link in the note's source editor, or use **Follow In-BV Link**
in its context menu, to navigate the connected views back to the resolved address.
Navigation uses binary addresses through the native View API; Markdown character
positions are only used to locate the note text. Following an anchor preserves
the source view's focus and does not modify note text, metadata, or undo history.

Inline `[label](url)` links and `<binaryninja://…>` autolinks are indexed, including
links in rendered table source. Code examples, images, and `%%comments%%` do not
create anchors. Malformed links, unresolved expressions, and URLs for other files
remain editable without participating in synchronization. Expressions are resolved
again on navigation so updated symbols are used.

## Context menus

Right-click in the source editor or a table cell for compact, rounded, theme-aware menus
modeled on the Obsidian layout. Existing selections are retained when opening the
menu, and each editing command uses the note's shared undo history. Menus use the
application's UI typeface at Binary Ninja's configured Linear View/monospace text
size, with the Qt application font as a standalone fallback.

- **Add link** inserts `[[target]]` wikilink markup and selects the target.
  **Add external link** inserts `[label](https://)` with the URL selected for editing.
- **Format:** Bold, Italic, Strikethrough, Highlight; Code, Math, Comment; and
  Clear formatting. Formatting toggles recognize nested markers around the selected
  text. Clear formatting removes recognized inline wrappers, link destinations,
  and selected paragraph prefixes while preserving the content.
- **Paragraph:** Normal text, headings 1–6, blockquote, bulleted list, numbered list,
  and task list. Converting a selection replaces existing paragraph markers.
- **Insert:** Table dimensions in the source editor, fenced code block, math block,
  and horizontal rule. Math commands create editable `$…$` or `$$…$$` formula source.
- **Author:** mark the paragraph, selection or selected table rows as written by a person
  or an agent (see **Authors**).
- **Cut, Copy, Paste, Paste as plain text, Select all, Undo, Redo.** Availability and
  format checkmarks reflect the current selection and document state.

The same formatting menu works on whole grid selections. Its **Table** submenu
contains row/column operations, alignment, Markdown copy/paste, source editing, and
deletion. **Paste as plain text** in the grid puts tabs and newlines into one cell;
regular Paste accepts spreadsheet cells and can expand the table.

Comments stay visible and muted while editing and are omitted from cell previews.
Comment contents do not create headings in the outline or nested rendered tables.
Cell previews also render highlighting, wikilink labels, formula text, headings,
lists, and quotes. All added syntax remains in the exported Markdown source.

## Rendered tables

Click the **table icon** just left of Outline and choose dimensions from the grid.
The count includes the header row. The picker supports mouse hover/click and
arrow keys followed by Enter; **Custom size…** offers larger dimensions.
Existing GFM pipe tables in imported notes render automatically.

Each table owns a mandatory blank Markdown paragraph before and after its source.
Those separator paragraphs are folded into the rendered table, including its hover
controls, so surrounding text sits directly above/below it without extra blank rows.
Separate writable paragraphs remain at the beginning/end of a note and between
adjacent tables. Typing, pasting, and deleting at these edges cannot consume the
table's separators or turn surrounding prose into table rows. Older notes receive
the separators in memory on load; the next note edit persists the normalized source.

The table surface is custom-painted with QPainter: square corners, restrained
rules, a softly tinted header, and subtle selection outlines. Row/column actions
fade in on hover or keyboard focus, leaving the table itself uncluttered.

- **Edit cells:** double-click, press F2, or start typing in a selected cell.
  Cells display formatted Markdown; their editor exposes the Markdown syntax.
  Bold, italic, strikethrough, inline code, links, fenced code blocks, and line
  breaks are supported. Language-tagged code blocks get syntax highlighting;
  prose and explicit blank lines before/after them are retained.
  The top-bar bold/italic/code actions apply to the active cell's text selection,
  or to whole selected cells when the grid has focus. Repeating the same action
  toggles it off, just as in the main editor.
- **Navigate:** Tab commits an edit and moves forward; Tab from the last cell
  appends a data row. Shift+Tab commits and moves backward. Enter commits and
  moves down; Enter from the last row continues in the note below the table.
  With a cell selected, the arrow keys move between cells; Up from the first row or
  Down from the last leaves the table for the line above or below. Only a focused
  table shows a selected cell: moving the caret out, or focus anywhere else, clears
  it. While a cell is selected, the note's caret is kept in that cell's Markdown source.
  **Shift+Enter inserts a real newline** in the current cell. Inside a code block,
  **Tab inserts a literal tab, Enter inserts a newline, and Shift+Tab outdents**;
  these keys stay in the cell. Normal navigation resumes in prose after the
  closing fence. Escape cancels cell editing; Escape from the grid returns to
  the note after the table.
  Enter within a blockquote also stays in the cell and continues the quote;
  an empty quoted line exits a level. Quote markers follow the active-line rule
  in cell editors, and cell previews show quote rules around multiline content.
- **Write after a table:** when plain text (including bold, italic, and ordinary
  links) or an empty line follows a table, click it as usual; the note's final line
  and the blank line between adjacent tables count. Before anything else (a heading,
  quote, code block, rule, or HLIL embed) the table has one blank line of space below
  its footer: click there and start typing, and an empty line is opened for you
  (one undo step).
  Tall tables leave room for that area even in short panes and at the end of a note.
  The text area above the top hover rail is editable in the same way. Backspace/Delete
  at a table boundary keeps the caret in the adjacent paragraph.
- **Growing cells:** rows expand to fit both the preview and the active Markdown
  editor, including wrapping and trailing blank lines. Cell editors have no
  internal scrolling; taller content scrolls with the table/note. Newlines are
  stored as `<br>`, with tabs and boundary spaces encoded so Markdown round trips
  preserve them.
- **Insert in place:** hover between rows along the left edge or between columns
  along the top edge. A plus and guide line show exactly where a click will insert.
  Hover the full-width footer to reveal **+ Add row**, then click to append a row.
  The footer is blank when idle. Use the right edge to append a column.
- **Row/column actions:** the left and top hover handles select a row or column
  and open its insert/delete/alignment menu. Right-click a cell and open **Table**, or use the Menu
  key / Shift+F10, for keyboard-accessible actions. Inserting above the header makes
  the new row the header and moves the existing header into the body. Deleting the
  header promotes the next row; deleting a table's only row removes the table.
  The final column is protected; **Delete table** removes the whole table.
- **Align and resize:** the context menu sets left/center/right column alignment,
  which is stored in Markdown delimiter markers. Drag a divider in the header to
  resize; double-click it to fit the content. Widths are shared between the current
  file's open surfaces as presentation state. Large tables have slim, draggable
  scroll indicators, with mouse-wheel, Shift+wheel, and trackpad scrolling.
- **Copy/paste:** **Copy table as Markdown** copies a complete pipe table.
  Ctrl/Cmd+C copies a selected rectangle as Markdown, or a single cell's Markdown
  text. Ctrl/Cmd+V accepts Markdown tables, spreadsheet TSV, or text and expands
  the grid as needed. Cutting clears selected cell values. Copying the entire
  note includes the original Markdown source for all tables.
- **Table actions:** the upper-left hover handle opens copy/export-style actions,
  **Edit Markdown…**, and deletion. Editing Markdown opens the table source in a dialog.

Cell typing is written through to note metadata immediately, including while a
cell editor is still open. Cell edits and structural operations use the note's
shared undo/redo history; table edits appear in other open Notepad surfaces.

## Saving notes

Edits are written immediately to persistent user metadata on the file's Raw
BinaryView under `bnnotes.notepad`. The metadata value is a single root object
containing an ordered `notes` array with stable identifiers and exact Markdown source:

```json
{
  "version": 2,
  "mcp-allow": true,
  "revision": "6b1f0c2e9d8a47f3b5e4c3d2a1908f7e",
  "notes": [
    {"id": "8a5214e8451d4d1dac1b201e0d775b21", "text": "# Analysis notes\n", "mcp-allow": false,
     "edited": 1793809657, "folder": "5c0d2a6f3b8e4e1f9a7d6c5b4a392817"},
    {"id": "88ad51d2f6574c3182e45cdc0dbdd7f9", "text": "Another note\nby an agent",
     "mcp-allow": true, "agents": [{"line": 1, "count": 1, "name": "Claude Opus 5.5"}],
     "users": [{"line": 0, "count": 2, "name": "cynder"}],
     "earlier": [{"line": 0, "count": 1, "writers": [{"agent": "Claude Opus 5.5"}]}],
     "earlier-text": "3f1c9b2a7d4e8f06"}
  ],
  "folders": [
    {"id": "5c0d2a6f3b8e4e1f9a7d6c5b4a392817", "name": "Filesystems"},
    {"id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0", "name": "ISO 9660",
     "parent": "5c0d2a6f3b8e4e1f9a7d6c5b4a392817"}
  ]
}
```

**Save the Binary Ninja database (`.bndb`) to persist notes across sessions.**
Saving only the original binary does not serialize plugin metadata. Right-click a
note's tab and choose **Export Note as Markdown…** to save a copy as a file (see
**Exporting notes** below). Closing a nonempty note or editor
surface leaves its content in file metadata. Empty notes are excluded from the array;
the root and its view-wide gate remain even after the final saved note is deleted.

Reading and writing use the version-2 root-object format shown above.
A note's optional `agents` list marks runs of agent-written source lines (zero-based
`line`, `count` and the agent's `name`); every other line, and every line of a note
without the list, was written by a person. On Enterprise, an optional `users` list in the
same shape names the Enterprise user who wrote each line, person or agent. Those name a
line's latest writer. An optional `earlier` list, of `line` and `count` runs with `writers`,
names who edited a line before its latest writer, oldest first; each writer is an object
with an optional `agent` and `user` (an empty object is a person). `earlier-text`, a digest
of the text the list was recorded for, accompanies it: if `text` changes without it (a
client that copies fields it does not know), the list is ignored. Clients that rewrite
`text` without updating `agents` should drop the authorship lists. Unreadable runs are ignored.
Titles are derived from the source and are not stored separately. Duplicate titles
remain distinct notes because all editing and reopening use stable identifiers.
The optional `edited` field is when the note's text last changed, in whole seconds
since the Unix epoch; Notes sets it on every edit and shows it on the home page.
Notes written before it existed, or by other clients, simply have no edit time.

Folders were added to version 2 without a migration. The optional root `folders` list
holds `{id, name, parent?}` entries, and a note's optional `folder` names one by id.
Without them a file simply has no folders, and the root is written back exactly as it
was until a folder is created; deleting the last folder removes the list again.
Unreadable folder entries are skipped, a missing or cyclic `parent` makes a folder
top-level, and a note naming an unknown folder is shown unfiled (its `folder` is kept
until the note is moved).

`revision` is a new random id on every write that changes the root (writing what is
already stored writes nothing). Clients writing notes should set a new one too: see
**Enterprise merges** below.

### Enterprise merges

All of a file's notes are one metadata value, so two people (or a person and an agent on
another machine) changing any notes at the same time would conflict on the whole collection,
and Binary Ninja's **Resolve Merge Conflict** dialog would keep only one side. Notes registers
a merge for that value (`src/merge.py`, at plugin load, headlessly too) so it never reaches
the dialog:

- **Notes** are matched by id. Notes added on either side are kept. A note deleted on one
  side is deleted, unless the other side changed it: an edit beats a delete.
- **Text** merges line by line, three ways, and each line keeps who wrote it. Changes to
  different lines both apply, even on neighboring lines; lines both sides added at the same
  place are both kept, the left side's first. Where both sides changed the same lines
  differently (or one deleted lines the other edited), the note gets both versions between
  conflict markers, so the problem is visible and can be fixed by editing the note:

  ```text
  <<<<<<< alice
  The length is checked before the copy.
  =======
  The length is never checked.
  >>>>>>> bob's Claude Opus 5.5
  ```

  Each side is labelled with who wrote its lines when that is one writer, else **left** and
  **right** (as Binary Ninja's dialog names them). The marker lines are ordinary text.
- **Fields** (`edited`, `folder`, a note's or the view's `mcp-allow`, folder names and
  parents, and fields other clients added) take whichever side changed them. Both changed:
  the later `edited`; `mcp-allow` becomes `false` (agent access stays off until someone
  decides); anything else, the left side's.
- A side that isn't readable Notes metadata (another version, say) is left to the dialog.

Every write's new `revision` makes any two concurrent changes conflict, so the whole
collection always comes to this merge. Without it, Binary Ninja combines a value's fields
itself where they don't overlap, which can pair one side's text with the other side's
per-line authors.

**An open notebook** writes on every keystroke, so it must not write over others' work that
a sync brings into its file. Each write first compares the stored notes with what this
notebook last read or wrote; if they differ, it merges the three (that, the stored notes, and
the notebook's own) the same way, writes the result and takes it: synced notes appear,
closed tabs of notes deleted elsewhere close, and open editors update in place (their
undo history starts again, since it can't undo past someone else's work). Every two seconds
an open notebook also checks whether the database's current snapshot moved, and merges then
without waiting for an edit. A save triggered by typing merges on the next event-loop turn
instead, outside the document's own change notification.

Binary Ninja's merge API (6.0–6.1) has some sharp edges, handled in `src/merge.py`:
conflict strings arrive C-escaped (`\n`, `\"`, non-ASCII as `\xHH` UTF-8 bytes) and
`success()` unescapes them again, and stores nothing, while still returning true, if given
plain JSON rather than typed JSON; a `ConflictSplitter` must stay referenced for the life of
the process (the core holds raw callbacks into it); and `split()` can only return `{}`.

Visual parsing is coalesced onto the next UI event-loop turn; saving immediately
after a keystroke includes the latest text. An unreadable or unsupported metadata
version opens read-only so it cannot be overwritten. If a write fails, the editor
keeps the text available for export and **Hamburger → Retry Metadata Write** appears.

### Exporting notes

**Export Note as Markdown…** (a note tab's context menu) saves the note as a `.md` file. A
`binaryninja://` link means nothing outside Binary Ninja, so with **Export Embeds as Code
Blocks** on (the default) each embed is written as the code it shows, the way it shows it:

- Its label, if any, stays as text; the code follows in a fenced block, a statement's or a
  whole block's (`!`), in the link's IL. HLIL and Pseudo C are tagged `c`, Pseudo Rust
  `rust`, Pseudo Objective-C `objc` and disassembly `asm`; LLIL and MLIL have no language.
  Each line starts with its address when **Show Addresses in HLIL Embeds** is on.
  Indentation is relative to the least-indented line, and blank lines stay blank.
- Text before and after an embed in the same paragraph stays, on its own lines. In a quote
  or list item the block stays inside it, indented to its text.
- In a table cell, where a block can't go, each code line becomes a code span, joined by
  `<br>`.
- A link that isn't an embed (another file's, an unresolved expression, one split over
  lines) or whose code isn't available stays a link.

The code is looked up fresh, off the UI thread, with the file's display settings; a progress
dialog with **Cancel** appears if that takes a moment. The note itself is unchanged. Turned
off, export writes the note's Markdown exactly. Copying always copies the Markdown.

## Install

Requires a Binary Ninja UI exposing `SplitTabWidget`, `GlobalAreaTabStyle`, and
the expanding `setCornerWidget` API, with Python 3.10+ and PySide6.

1. In Binary Ninja, use **Install Python3 Module** from the command palette to
   install the packages in [`requirements.txt`](requirements.txt):
   `tree-sitter>=0.25,<0.26`, `tree-sitter-markdown>=0.5,<0.6`,
   `Pygments>=2.15,<3`, and `markdown-it-py>=4.1,<5`.
2. Place this directory, or a symlink named `bnnotes`, in Binary Ninja's user
   plugins directory.
3. Restart Binary Ninja, open a binary, and select the Notepad view, pane, or sidebar.

Use Binary Ninja's bundled PySide6 for the plugin. The development Qt dependency
below is for standalone tests. If the Markdown parser is missing, existing notes
remain editable as plain text and the widget displays installation guidance.

For a symlinked development install on macOS, you can install the runtime
packages directly inside this checkout using Binary Ninja's bundled Python:

```sh
"/Applications/Binary Ninja.app/Contents/MacOS/bnpython3" tools/install_runtime.py
```

The plugin discovers these packages under `.runtime/`, keyed by Python ABI and
platform. Restart Binary Ninja after installing them. The test virtualenv alone
does not supply packages to Binary Ninja's Python interpreter.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m ruff check .
.venv/bin/python tools/run_tests.py -q
```

`run_tests.py` launches tests in a subprocess with a 90-second watchdog. The Qt
fixture verifies an offscreen platform plugin exists **before** creating a
QApplication. Use the virtualenv's Qt installation; the macOS Binary Ninja app
ships only the Cocoa platform plugin and cannot run this offscreen test suite.
Test temporary files stay under `.test-artifacts/`.

The tests mirror the source layout: one module per source module, with shared
fakes in `tests/conftest.py`. They exercise real Markdown grammars, Qt input, undo
and persistence. Native integration cases live under `tests/native_core/`; see
[`docs/testing.md`](docs/testing.md).

Agent-operated UI inspection is separate:

```sh
python3 -m tools.agent_ui windows
python3 -m tools.agent_ui attach  # one-time bootstrap instructions
python3 -m tools.agent_ui capture --pid PID
python3 -m tools.agent_ui preview hlil --width 900
python3 -m tools.agent_ui open --restart
```

The [agent UI framework](tools/agent_ui/README.md) uses a persistent local bridge
for app capture and inspection scripts. Its isolated visual scenes render
offscreen without stealing focus. Save/restart/open are explicit operations. The
tools write `.agent-artifacts/` and are not used by the test suite.

### How it fits together

- **Data:** `NoteStorage` (`src/storage.py`) reads and writes the metadata root. One
  `NotebookSession` per open file (`src/ui/session.py`) owns the ordered `NoteSession`s
  and folders; every surface showing that file shares it, and it saves on every edit.
- **A note:** `NoteSession` builds its `QTextDocument`, `MarkdownHighlighter` and
  `PixelDocumentLayout` the first time something shows it. Its `NoteIndex` is the
  analysis used for navigation and search while the note is closed (missing, pending in
  the parser process, or ready for one exact source).
- **Parsing:** `src/markdown.py` turns source into a `MarkdownAnalysis`. The highlighter
  parses on the UI thread (`ParseMode.SYNC`) or in the parser process
  (`ParseMode.DEFERRED`, `src/preparation.py`) for notes opened from a file.
- **Widgets:** `NotesWidget` (`src/ui/notebook.py`) is one notebook surface: its tab panes
  (`NativeNoteTabs` or `StandaloneNoteTabs`), `TabState` (open tabs and recency, saved per
  file), `HomeTab` (the Notes page lifecycle), `TabRestore` (headers inserted after a file
  opens), `NavigationSync` and, on first run, the welcome tour (`OnboardingWidget`,
  `src/ui/onboarding.py`). Each note tab is a `NotepadWidget` wrapping a
  `MarkdownEditor`; tables are `TableCard`s over the editor, and in-BV links are drawn by
  `LinkPresentation` with IL fetched by `HLILPreviewProvider`.
- **Authors:** while a note is open, each `QTextBlock` carries its latest writer's agent
  name and Enterprise user, and its earlier writers, as block format properties (`AUTHOR`,
  `USER`, `EARLIER` in `src/ui/session.py`), so undo restores them with the text;
  `block_writers` lists a line's writers and `writer_label` names one. `note_people` orders a
  note's people for their colors, and `writer_hue` (`src/ui/editor.py`) picks a writer's.
  `NoteSession` attributes new edits to its current author (`authoring(name)`, a person by
  default) and keeps `agents`, `users` and `earlier` runs for storage; closed notes use
  `retag_writers` in `src/storage.py`. `AuthorGutter` (`src/ui/editor.py`) builds an
  `AuthorBar` per paragraph and paints it with `paint_author_bar`, and `AuthorTag` slides out
  the names.
  `enterprise_user()` and `person_name()` in `src/storage.py` identify the local writer.
- **MCP:** `NoteTools` (`src/mcp.py`) implements the `bnnotes_*` tools over a note
  collection: `StoredNotes` (metadata) headlessly, or `LiveNotes` (`src/ui/session.py`)
  for a file whose notebook is open in the UI, run on the main thread.
- **Lifetimes:** surfaces hold notebooks and notes with a `Lease`; the last release frees
  the documents on the GUI thread. Shared queues and pools are per-application singletons
  (`app_singleton`).

### Layout

Modules are grouped by subject, each holding the classes for that subject (in the
style of `binaryninja-api`). `src/` is free of Qt, so it also runs headlessly and in
the parser process; `src/ui/` holds the Qt interface.

| Module | Contents |
| --- | --- |
| `__init__.py`, `plugin.json` | Plugin entry point and Plugin Manager metadata |
| `src/storage.py` | Note and folder persistence in BinaryView metadata |
| `src/merge.py` | Enterprise merges: three-way merges of the notes metadata, and Binary Ninja's conflict splitter |
| `src/mcp.py` | MCP tools for notes (registered with Binary Ninja's MCP server) |
| `src/settings.py` | Setting schema, registration and scoped backends (native and in-memory) |
| `src/markdown.py` | Tree-sitter analysis, UTF-16 mapping, Obsidian extensions, table values and boundaries, link source edits, runtime packages |
| `src/export.py` | Exporting a note as Markdown with its embeds as code blocks |
| `src/links.py` | BNURLs, address anchors, IL modes and IL/pseudo-language snapshots |
| `src/preparation.py` | Off-thread Markdown preparation, the parser process loop, and table cell HTML templates |
| `src/ui/common.py` | Appearance, menus, toolbar controls, scrollbars, focus, idle/worker scheduling, retained drawing, persisted UI state |
| `src/ui/layout.py` | Document layout: block spacing, quote indentation, pixel layout and the pixel-scrolled text edit |
| `src/ui/highlighter.py` | Markdown highlighter and the parser subprocess client |
| `src/ui/formatting.py` | Formatting commands, text/grid actions and quote presentation |
| `src/ui/editor.py` | `MarkdownEditor` |
| `src/ui/embeds.py` | In-BV link embeds: projection, rendering, theme, follow hint, context menu, navigation holds |
| `src/ui/provider.py` | Background IL lookups and native display-settings snapshots |
| `src/ui/tables.py` | Table models, cell rendering and editing, grid view, chrome and dimension picker |
| `src/ui/session.py` | Shared note documents, notebooks, folders, undo and leases |
| `src/ui/notepad.py` | One note's surface: editor, outline and find drawer |
| `src/ui/notebook.py` | Tabbed notebook: tab panes, tab menus, saved tab state, the home tab, restoration and navigation sync |
| `src/ui/onboarding.py` | The first-run welcome tour and its drawn samples (from the file's code when ready) |
| `src/ui/home.py` | The Notes home page, folder tree, section tabs and icons |
| `src/ui/search.py` | Find in All Notes: search, results view and filter edits |
| `src/ui/preferences.py` | Live editor preferences and the Settings page |
| `src/ui/host.py` | Native menu and action integration (BNURL copy actions, widget events) |
| `src/ui/integration.py` | View, sidebar and pane registration with Binary Ninja |

Parsing uses the existing `tree-sitter-markdown` block/inline grammars and
`markdown-it-py` for GFM table boundaries and rendered cell content. Qt owns
editing, grid selection, and undo/redo; Pygments supplies code lexers. No web
runtime or JavaScript build is required.

