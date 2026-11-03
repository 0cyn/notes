when using vision-text models to develop this, it ended up being useful to have
a toolset for automating binja ui actions and capturing screenshots. So this is a bunch of 
bad applescript and python for doing that. Really not how this should be done but whatever. 

# Agent UI tools

Explicit tools for an agent to inspect Binary Ninja and the Notes UI it creates.
This package is **not test infrastructure**: tests do not import it, and these
commands do not run tests or treat a screenshot as a pass/fail result.

Run from the repository root using ordinary Python. Routine commands communicate
with a persistent server inside Binary Ninja over an authenticated loopback socket.
They do not activate the app, focus the console, type commands, or send OS keystrokes.

## Connect once per application process

```sh
python3 -m tools.agent_ui attach
```

This prints a short Python bootstrap command to run once in Binary Ninja's console.
Alternatively, on macOS, explicitly request automatic one-time bootstrap:

```sh
python3 -m tools.agent_ui attach --console --pid PID
```

Automatic bootstrap briefly focuses the console and restores the previously
foreground application. Subsequent calls use the socket. The server is bound to
`127.0.0.1` on an assigned port; its per-process credential is stored with owner-only
permissions under `.agent-artifacts/bridges/`. It is opt-in developer tooling, not
part of the installed Notes plugin or test runner.

```sh
python3 -m tools.agent_ui status
python3 -m tools.agent_ui stop --pid PID
```

The bridge closes on application quit. Restarting needs a new bridge; the explicit
`open --restart` command bootstraps the newly launched process once.

## Commands

```sh
# Discover exact window IDs and process IDs; handles multiple windows/processes.
python3 -m tools.agent_ui windows

# Photograph an existing app window without changing its view or selection.
python3 -m tools.agent_ui capture --pid PID
# macOS-only alternative, including native window chrome:
python3 -m tools.agent_ui capture --window 12345

# Render a disposable native Notes UI offscreen; no window activation or focus change.
python3 -m tools.agent_ui preview notes --width 900 --height 650
python3 -m tools.agent_ui preview settings --width 300 --height 760
python3 -m tools.agent_ui preview search
python3 -m tools.agent_ui preview hlil
python3 -m tools.agent_ui preview tabs

# Inspect existing Notes widgets through a reusable script.
python3 -m tools.agent_ui run tools/agent_ui/examples/current_notes.py

# Open a file in the running application, rather than launching a duplicate process.
python3 -m tools.agent_ui open /absolute/path/to/file.bndb --surface pane

# Save All, kill, relaunch the active file. Surface changes are optional.
python3 -m tools.agent_ui open --restart
```

Use `--pid` when more than one application process is running. Use `--output DIR`
to choose an artifact directory; otherwise every invocation gets a unique directory
under `.agent-artifacts/`. The commands print JSON with image paths, window IDs,
script results, or errors. Paths containing spaces, quotes and Unicode are passed
as arguments/literals rather than interpolated into shell/AppleScript source.

Every script run has a unique request/receipt ID and expected PID. The server
acknowledges the request and serializes UI jobs so concurrent agents cannot
interleave fixture creation/cleanup. Success means that this exact invocation
completed; an old result cannot masquerade as a fresh result. On timeout the job
may still be running and is not automatically resubmitted.

Preview scenes start past the welcome tour; `preview onboarding` captures each of its
steps (`onboarding-1.png` …). Custom scripts can pass `onboarding=True` to
`session.notebook()` to open on the tour.

Built-in preview scenes use Qt's `WA_DontShowOnScreen` and
`WA_ShowWithoutActivating`: layout and painting run normally, while the generated
widgets never become visible desktop windows. App/widget captures likewise do
not activate windows. Custom scripts should follow that convention and avoid
`activateWindow()`, host navigation, and OS input unless explicitly requested.

The bridge client and widget capture use Python/Qt. Automatic bootstrap, native
window-ID listing/capture, and launch/restart currently require macOS. Bootstrap
uses Accessibility/Automation; native screen capture also needs Screen Recording
access and Apple's Swift CLI. These are not needed for socket-based widget captures.

`open --restart` always saves through **File → Save All** before killing the
selected process. An unresolved dialog stops the restart. An ordinary `open` uses
the existing application's API and locates the file by full path across native
contexts, including when Binary Ninja opens it in another window.

## Writing an inspection script

Define `run(session)`. It executes on Binary Ninja's UI thread:

```python
def run(session):
    with session.notebook('# Example\n\n**Editable** Markdown', width=600) as notes:
        notes.current_note.outline_button.setChecked(False)
        session.settle()
        session.capture(notes, 'example')
        return session.describe(notes, depth=1)
```

Available operations:

- `session.capture(widget, name)`: save a widget image and record its size/scale.
- `session.describe(widget, depth=2)`: return widget classes, names, geometry and visibility.
- `session.settle(milliseconds=80)`: allow layout and queued events to complete.
- `session.wait_for(predicate, timeout=5, description='…')`: wait for a specific UI state.
- `session.notebook(source='', binary=None, width=900, height=650)`: an isolated,
  offscreen, in-memory Notes widget, cleaned up on exit, using memory-only preferences.
- `session.module('ui.notebook')`: import current plugin source in a fresh
  fixture namespace. This does not hot-reload the user's live Qt callback classes.
- `session.output`, `session.options`, `session.prepared`: run context.

An optional `prepare(options)` runs outside the UI thread, for example to analyze
a tiny in-memory binary. Its return value becomes `session.prepared`. An optional
`cleanup(prepared)` always runs afterward. See `scenes.py` for a complete example.
Return JSON-serializable data from `run`; exceptions produce a failed receipt with
the traceback and a nonzero CLI exit status.

Reacquire borrowed `binaryninjaui` wrappers after event-loop turns and operations
such as changing views/focus. Prefer `UIContext.getAllViewFramesForTab()` for frame
discovery. Fixture cleanup should close only resources the script created.

Visual inspection is deliberate: run a command, read its receipt, then examine
the resulting image. Old files under `.test-artifacts/ui-automation/` are historical
scratch artifacts, not scripts or dependencies of this framework.
