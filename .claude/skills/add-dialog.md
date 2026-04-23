# Add a New Monitor Dialog

Guide for adding a new dialog/window to the Leap Monitor GUI — covering the non-obvious wiring (zoom, geometry, theme, prefs persistence) that ad-hoc implementations tend to get wrong.

Live dialogs live under `src/leap/monitor/dialogs/`. They inherit from `QDialog`, mix in `ZoomMixin`, and interact with `MonitorWindow` for shared state (themes, prefs, tooltips).

## Core Pattern

```python
from PyQt5.QtWidgets import QDialog
from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import (
    load_dialog_geometry, save_dialog_geometry,
)
from leap.monitor.themes import current_theme


class MyDialog(ZoomMixin, QDialog):
    _DEFAULT_SIZE = (800, 500)      # used by MonitorWindow._reset_window_size

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('My Dialog')
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('my_dialog')
        if saved:
            self.resize(saved[0], saved[1])

        # ... build UI ...

        # Must be last: enable Cmd+scroll / Cmd+±/0 zoom
        self._init_zoom(pref_key='my_dialog_font_size')

    def done(self, result):
        save_dialog_geometry('my_dialog', self.width(), self.height())
        super().done(result)
```

**Every resizable dialog must:**
1. Subclass `ZoomMixin` (first in MRO, before `QDialog`).
2. Set a `_DEFAULT_SIZE` class attribute so `_reset_window_size` can restore sane defaults.
3. Load/save geometry via `load_dialog_geometry(key)` / `save_dialog_geometry(key, w, h)`.
4. Call `self._init_zoom(...)` at the end of `__init__`.

Trivial info/warning popups (one-off `QMessageBox` / `QInputDialog`) don't need this — the global `PopupZoomManager` handles their font.

## Zoom: Single-Target vs Split

**Simple dialogs** (form with inputs/buttons only) use a single zoom target:

```python
self._init_zoom(pref_key='my_dialog_font_size')
```

**Content-heavy dialogs** (dialogs with a QTextEdit / QListWidget / QTreeView / QTableWidget) **must** use split zoom so users can enlarge content without blowing up the chrome:

```python
self._editor = QTextEdit()
self._list = QListWidget()
# ... build UI ...
self._init_zoom(
    pref_key='my_dialog_font_size',            # chrome (buttons, labels)
    content_pref_key='my_dialog_text_font_size',  # content widgets
    content_widgets=[self._editor, self._list],
)
```

If content widgets are rebuilt dynamically (e.g., message cards regenerated on save), pass a **callable** instead of a list — the mixin re-resolves it on every zoom event:

```python
self._init_zoom(
    pref_key='my_dialog_font_size',
    content_pref_key='my_dialog_text_font_size',
    content_widgets=lambda: self._current_cards,  # list recomputed each call
)
```

After rebuilding the content widgets, call `self._zoom_reapply_content()` so new widgets render at the saved size.

## Close Hooks

- If the dialog closes via `accept()` / `reject()` (OK / Cancel / Escape): override `done()` and save there. `ZoomMixin.done()` handles zoom flush automatically.
- If it closes via `closeEvent()` or the X button: override `closeEvent()` and explicitly call `self._zoom_flush()`. `done()` is **not** called for those paths.

Do both if either is possible (Notes dialog, QueueEditDialog do this).

## Theme Integration

**Never hardcode colors.** Use `current_theme()`:

```python
from leap.monitor.themes import current_theme

t = current_theme()
self._label.setStyleSheet(f'color: {t.text_muted};')
```

For cell/button styles used inside tables, use the helpers in `monitor/ui/table_helpers.py` (`close_btn_style`, `active_btn_style`, `menu_btn_style`).

## Prefs Persistence — CRITICAL

This is where ad-hoc dialogs silently break. **Read carefully.**

### The Model

`monitor_prefs.json` has two classes of keys:

| Class | Owner | Updated via | Examples |
|-------|-------|-------------|----------|
| **Monitor-owned** | `MonitorWindow.self._prefs` (cached in memory at startup) | `self._prefs[key] = X; self._save_prefs()` | `main_font_size`, `window_geometry`, `column_widths`, `row_order`, `row_colors`, `aliases`, `theme`, `include_bots` |
| **Dialog-owned** | Read/written via helpers (no monitor cache) | `load_monitor_prefs()` → update → `save_monitor_prefs()` | `notes_font_size`, `send_position`, `send_comments_filter`, `run_session_include_completed`, all ZoomMixin `*_font_size` keys |

### The Trap

`MonitorWindow._prefs` is populated at startup from `load_monitor_prefs()`. If a dialog modifies a key on disk but `MonitorWindow._prefs` still has the stale startup value, the next unrelated monitor write (main-window zoom, theme change, window resize) would clobber the dialog's save.

### The Guarantee

`MonitorWindow._save_prefs` refreshes dialog-owned keys from disk before writing. This uses two mechanisms:

1. **Pattern match**: any key ending in `_font_size` or `_font_family` (except `main_font_size`) is treated as dialog-owned.
2. **Explicit list**: `MonitorWindow._DIALOG_OWNED_KEYS` — add your key here if it doesn't fit the pattern.

### Rules for New Dialog Prefs

**When adding a new dialog pref:**

1. If it's a `*_font_size` or `*_font_family` — just use the name, it's auto-covered.
2. Otherwise, add the key name to `MonitorWindow._DIALOG_OWNED_KEYS` in `app.py`.
3. Never touch `MonitorWindow._prefs[key]` for a dialog-owned key.
4. Always read via `load_monitor_prefs()` and write via `save_monitor_prefs()` (or a purpose-built `load_X`/`save_X` helper in `pr_tracking/config.py`).

**Example — add a "my_dialog_last_tab" key:**

```python
# src/leap/monitor/pr_tracking/config.py
def load_my_dialog_last_tab() -> int:
    return load_monitor_prefs().get('my_dialog_last_tab', 0)

def save_my_dialog_last_tab(tab: int) -> None:
    prefs = load_monitor_prefs()
    prefs['my_dialog_last_tab'] = tab
    save_monitor_prefs(prefs)
```

```python
# src/leap/monitor/app.py — add to the class-level set
_DIALOG_OWNED_KEYS: frozenset[str] = frozenset({
    'run_session_include_completed',
    'save_preset_include_completed',
    'send_position',
    'send_comments_filter',
    'send_comments_mode',
    'preset_editor_last_name',
    'my_dialog_last_tab',   # ← add here
})
```

### Why This Matters

Before this pattern, dialogs would save to disk, then a main-window event (e.g., Cmd+scroll on the table) would call `_save_prefs` which wrote `MonitorWindow._prefs` (with stale dialog values) back to disk — silently reverting your save. A user would zoom in a dialog, switch themes, and watch their zoom change "mysteriously" revert. Symptoms look like "save isn't working" but both save and load work in isolation. **Don't let this bug come back.**

### MonitorWindow-Owned Keys

Conversely, if your key **is** monitor-owned (rare for dialogs — usually only applies when MonitorWindow itself needs the value for rendering):
- Update `self._prefs[key]` first, then call `self._save_prefs()` (NOT `save_monitor_prefs(self._prefs)` directly).
- Direct `save_monitor_prefs(self._prefs)` bypasses the dialog-owned refresh and re-introduces the stale-cache bug.

## Font-Size Cascade Gotcha (Qt Quirk)

If any widget in your dialog has its own `setStyleSheet(...)` with color/background/border/padding but **not** font-size, Qt blocks the ancestor-level font-size cascade from reaching it. That widget will render at the default size regardless of your dialog-level zoom stylesheet.

**Fix:** bake font-size into the widget's own stylesheet whenever you set one:

```python
# Wrong — font-size cascade is blocked
self._label.setStyleSheet('font-weight: bold;')

# Right — inherit from cascade (widget has no stylesheet at all)
self._label.setFont(my_bold_font)

# Right — explicit font-size in the widget's stylesheet
self._label.setStyleSheet(
    f'font-weight: bold; font-size: {self._zoom_font_size}pt;'
)
```

ZoomMixin handles this for widgets that don't set their own stylesheet. For widgets that do, you need to re-apply font-size whenever their stylesheet is rewritten (see `NotesDialog._apply_buttons_font_size` for the marker-based pattern that survives multiple zoom deltas).

## Storage Directories

If your dialog reads/writes files under `.storage/<subdir>/`:

1. Add the constant in `utils/constants.py` (alongside `QUEUE_DIR`, `SOCKET_DIR`, `HISTORY_DIR`).
2. Add a `.mkdir()` call in `ensure_storage_dirs()` in `utils/constants.py`.
3. Add the path to the `ensure-storage` Makefile target.

## CLAUDE.md Registration

When your dialog is user-visible and non-trivial, add an entry to CLAUDE.md:

1. **Project Structure** tree — list the file under `src/leap/monitor/dialogs/` with a one-line description.
2. **Key Classes** table — `| MyDialog | monitor/dialogs/my_dialog.py | What it does |`.

## Testing

Dialogs aren't easy to unit-test (Qt requires a display). The practical checks:

1. **AST parse** after every edit (`python3 -c "import ast; ast.parse(open(...).read())"`).
2. **Runtime import** (`poetry run python -c "from leap.monitor.dialogs.my_dialog import MyDialog"`).
3. **Manual verification** via `make run-monitor` — open the dialog, use every control, close, reopen, verify prefs persist.
4. **Prefs regression test** — zoom in the dialog, make some change in the main window (Cmd+scroll the table, switch themes), close the dialog, reopen. Your zoom should still be there. If not, you forgot to register a dialog-owned key.

## Anti-Patterns Checklist

- ❌ `save_monitor_prefs(self._prefs)` from anywhere that isn't `_save_prefs` itself — bypasses dialog-owned refresh.
- ❌ `self._prefs['dialog_owned_key'] = X` from MonitorWindow — will be overwritten by the refresh.
- ❌ Hardcoded colors in stylesheets — use `current_theme()`.
- ❌ Dialogs without `_DEFAULT_SIZE` — breaks "reset window sizes".
- ❌ Widget stylesheets with color/weight but no font-size — silently blocks the zoom cascade.
- ❌ `setStyleSheet('...')` that overwrites a previously-applied zoom stylesheet — use the marker split pattern (`existing.split(MARKER)[0]`) to preserve it.
- ❌ Missing `closeEvent` flush when the dialog can close via the X button — zoom changes lost.
- ❌ Hidden-but-layout-allocated widgets that silently still take space — use `setVisible(False)`, not opacity tricks.
