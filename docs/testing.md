# Tests

Tests check observable behavior and report named failures. Agent-operated app
inspection and screenshot capture live separately in
[`tools/agent_ui/`](../tools/agent_ui/README.md).

## Local unit and Qt UI tests

There is one test module per source module (`tests/test_markdown.py` for
`src/markdown.py`, `tests/test_tables.py` for `src/ui/tables.py`, and so on); shared
fakes and fixtures live in `tests/conftest.py`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python tools/run_tests.py -q
.venv/bin/python -m ruff check .
```

`run_tests.py` runs pytest in a bounded subprocess, using the virtualenv's Qt and
its offscreen platform plugin. It does not launch, restart, photograph, or control
Binary Ninja. Temporary test files go under `.test-artifacts/`.

The UI tests exercise actual Qt editors, mouse/keyboard events, selection, undo,
focus, scrolling, and table interaction. Binary Ninja API boundaries are modeled
with test doubles where the host is not required. Host-only tests are explicitly
skipped when their runtime is unavailable.

Run a focused area while developing:

```sh
.venv/bin/python tools/run_tests.py -q tests/test_settings.py tests/test_embeds.py
```

## Native core integration tests

These use Binary Ninja's real metadata, project settings and BNDBs, and need its
Python package:

```sh
PYTHONPATH='/Applications/Binary Ninja.app/Contents/Resources/python' \
  .venv/bin/python tools/run_native_tests.py
```

`tests/native_core/` covers the registered MCP note tools, notes, MCP flags and deletion
across BNDB save/reopen, scoped settings inheritance in saved, project-backed BNDBs, and
Enterprise merges of notes: it branches real BNDB snapshots and merges them with
`collaboration.merge_database`, through the registered notes merge (random collections
included, checked against the Python merge). Reports are in
`.test-artifacts/native-core-tests/`.

## What belongs in a test

Prefer an action and an independently observable result: type into a cell and
reopen the database; click a wrapped search result and inspect its selection;
change a scoped preference and inspect the effective value and persisted store.

Keep layout checks that protect interaction: no clipping, correct hit targets,
space reserved for multiline embeds, and controls remaining reachable after
scrolling or resizing. Pixel checks are appropriate when actual painted content
matters, such as hidden quote markers or token colors. Merely saving an image,
copying a constructor's configuration into assertions, or checking a mocked
getter against its supplied value provides little protection.

For visual review, use the separate [agent UI tools](../tools/agent_ui/README.md).
See [shutdown lifecycle](shutdown-lifecycle.md#normal-exit-checks) for normal-exit
investigation; a save/kill/relaunch command does not exercise orderly shutdown.
