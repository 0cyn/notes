# Qt/PySide shutdown lifecycle

## Reported failure

Binary Ninja 6.1.10832-dev with Qt/PySide 6.11.1 aborted during normal application
exit on macOS. The significant frames were:

```text
exit / __cxa_finalize_ranges
QtGlobalStatic::Holder<Q_QGS_pm_cache>::~Holder
QObject::~QObject / QObjectPrivate::ExtraData::~ExtraData
QVariant::~QVariant
PySide::invalidatePtr
Shiboken::BindingManager::hasWrapper
std::recursive_mutex::lock
std::__throw_system_error / terminate / abort
```

This is late destruction of Qt's global pixmap cache. The stack reaches a PySide
invalidation callback through a QObject property after Shiboken's registry mutex
has become unusable; it does not show a dockable-tab destructor.

## Why an event filter can trigger it

PySide creates a wrapper for the `watched` argument before calling a Python
`eventFilter`. `getWrapperForQObject()` installs a `_PySideInvalidatePtr` dynamic
property containing a `std::shared_ptr<char>` whose deleter calls `invalidatePtr`.
Qt's `QPMCache` is a parentless QObject in a `Q_GLOBAL_STATIC`, with a 30-second
cache timer. An application-wide Python filter can therefore attach that guard
to the cache even if its callback immediately rejects non-widgets.

Sources:

- [PySide 6.11 wrapper creation and invalidation](https://github.com/pyside/pyside-setup/blob/6.11/sources/pyside6/libpyside/pyside.cpp)
- [Qt 6.11 pixmap-cache singleton and timer](https://github.com/qt/qtbase/blob/6.11/src/gui/image/qpixmapcache.cpp)

## Plugin-side fix

`src/ui/host.py` attaches filters to individual `QWidget` instances.
Initial discovery uses `QApplication.allWidgets()`. Child-polish notifications
attach filters before a new menu's first Show; deferred child scans and Qt focus
signals cover later widgets and top-level windows.

The host menu and native-action controllers use this observer and uninstall on
`aboutToQuit`. Observation ends at uninstall, including pending scans and focus
connections. Pixmap-cache and other non-widget events never enter these Python
callbacks. This avoids creating the problematic wrapper/property in the first
place rather than trying to delete Qt-owned static objects at shutdown.

## Normal-exit checks

The usual save/SIGKILL/relaunch helper does not exercise this shutdown path.
For a native check:

1. Start a fresh Binary Ninja process with the plugin installed.
2. Open Notepad, create/split/close tabs, and exercise the BNURL menus and shortcuts.
3. Leave the event loop running for at least 35 seconds so Qt's 30-second
   pixmap-cache timer can fire.
4. Save through **File → Save All**, then choose **Binary Ninja → Quit Binary Ninja**.
5. Check for a normal process exit and a new macOS crash report.

`tests/test_host.py` prevents application-wide filter installation,
checks that non-widget receivers never reach the observer, and exercises the
first popup of a newly constructed child menu. Existing action/menu tests cover
selection routing, widget discovery, shortcuts, and uninstall behavior.

The initial isolated-profile shutdown comparison could not complete UI console
setup. Its diagnostics remain in `.test-artifacts/shutdown/`; no automated
before/after process-exit result is claimed from those attempts.
