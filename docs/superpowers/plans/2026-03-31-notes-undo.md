# Notes Dialog Undo/Redo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Cmd+Z / Cmd+Shift+Z undo/redo for all structural operations in the Notes dialog (delete, rename, move, reorder, create, mode switch, checklist mutations), with deferred image deletion that commits on dialog close.

**Architecture:** Command pattern with an undo stack capped at 50. Each undoable operation is a command object with `execute()` and `undo()`. Images are deferred-deleted (tracked in a set, only unlinked on dialog close). Cmd+Z routes to the undo stack only when focus is NOT in a text-editing widget (Qt's built-in undo handles that).

**Tech Stack:** Python 3, PyQt5, pytest

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/leap/monitor/dialogs/notes_undo.py` (CREATE) | `UndoCommand` ABC, `NotesUndoStack`, `NotesCmdContext`, all 13 command classes |
| `src/leap/monitor/dialogs/notes_dialog.py` (MODIFY) | Wire undo stack into `__init__`, `keyPressEvent`, all mutation methods, deferred image deletion, close path |
| `tests/test_notes_undo.py` (CREATE) | All undo stack + command tests |

---

### Task 1: UndoStack and UndoCommand base classes

**Files:**
- Create: `src/leap/monitor/dialogs/notes_undo.py`
- Create: `tests/test_notes_undo.py`

- [ ] **Step 1: Write failing tests for UndoStack mechanics**

Create `tests/test_notes_undo.py`:

```python
"""Tests for Notes dialog undo/redo system."""

import pytest

from leap.monitor.dialogs.notes_undo import NotesUndoStack, UndoCommand


class _FakeCmd(UndoCommand):
    """Test command that records execute/undo calls."""

    def __init__(self, label: str, log: list[str]) -> None:
        super().__init__(description=label)
        self._label = label
        self._log = log

    def execute(self, ctx: object) -> None:
        self._log.append(f'exec:{self._label}')

    def undo(self, ctx: object) -> None:
        self._log.append(f'undo:{self._label}')


class TestUndoStack:
    def test_push_executes_command(self) -> None:
        log: list[str] = []
        stack = NotesUndoStack(limit=50)
        stack.push(_FakeCmd('a', log), ctx=None)
        assert log == ['exec:a']
        assert stack.can_undo()
        assert not stack.can_redo()

    def test_undo_calls_undo(self) -> None:
        log: list[str] = []
        stack = NotesUndoStack(limit=50)
        stack.push(_FakeCmd('a', log), ctx=None)
        log.clear()
        stack.undo(ctx=None)
        assert log == ['undo:a']
        assert not stack.can_undo()
        assert stack.can_redo()

    def test_redo_calls_execute(self) -> None:
        log: list[str] = []
        stack = NotesUndoStack(limit=50)
        stack.push(_FakeCmd('a', log), ctx=None)
        stack.undo(ctx=None)
        log.clear()
        stack.redo(ctx=None)
        assert log == ['exec:a']
        assert stack.can_undo()
        assert not stack.can_redo()

    def test_push_after_undo_truncates_redo(self) -> None:
        log: list[str] = []
        stack = NotesUndoStack(limit=50)
        stack.push(_FakeCmd('a', log), ctx=None)
        stack.push(_FakeCmd('b', log), ctx=None)
        stack.undo(ctx=None)  # undo b
        stack.push(_FakeCmd('c', log), ctx=None)  # truncates b from redo
        assert not stack.can_redo()
        stack.undo(ctx=None)
        assert log[-1] == 'undo:c'
        stack.undo(ctx=None)
        assert log[-1] == 'undo:a'
        assert not stack.can_undo()

    def test_cap_at_limit(self) -> None:
        log: list[str] = []
        stack = NotesUndoStack(limit=5)
        for i in range(8):
            stack.push(_FakeCmd(str(i), log), ctx=None)
        # Only last 5 should remain
        count = 0
        while stack.can_undo():
            stack.undo(ctx=None)
            count += 1
        assert count == 5
        # Oldest should be '3' (indices 3,4,5,6,7 survive)
        assert log[-1] == 'undo:3'

    def test_clear(self) -> None:
        log: list[str] = []
        stack = NotesUndoStack(limit=50)
        stack.push(_FakeCmd('a', log), ctx=None)
        stack.clear()
        assert not stack.can_undo()
        assert not stack.can_redo()

    def test_undo_when_empty_is_noop(self) -> None:
        stack = NotesUndoStack(limit=50)
        stack.undo(ctx=None)  # should not raise

    def test_redo_when_empty_is_noop(self) -> None:
        stack = NotesUndoStack(limit=50)
        stack.redo(ctx=None)  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'leap.monitor.dialogs.notes_undo'`

- [ ] **Step 3: Implement UndoCommand and NotesUndoStack**

Create `src/leap/monitor/dialogs/notes_undo.py`:

```python
"""Undo/redo system for the Notes dialog.

Provides a command-pattern undo stack and concrete command classes for
all structural operations (create, delete, rename, move, reorder,
mode switch, checklist mutations).
"""

from abc import ABC, abstractmethod
from typing import Optional


class UndoCommand(ABC):
    """Abstract undoable command."""

    def __init__(self, description: str) -> None:
        self.description = description

    @abstractmethod
    def execute(self, ctx: 'NotesCmdContext') -> None:
        """Perform the operation."""

    @abstractmethod
    def undo(self, ctx: 'NotesCmdContext') -> None:
        """Reverse the operation."""


class NotesUndoStack:
    """Fixed-size undo/redo stack."""

    def __init__(self, limit: int = 50) -> None:
        self._commands: list[UndoCommand] = []
        self._cursor: int = 0  # index of next command to execute (== len of executed)
        self._limit = limit

    def push(self, cmd: UndoCommand, ctx: object) -> None:
        """Execute *cmd* and push it onto the stack.

        Truncates any redo tail. Drops oldest if over limit.
        """
        # Truncate redo tail
        del self._commands[self._cursor:]
        cmd.execute(ctx)
        self._commands.append(cmd)
        self._cursor = len(self._commands)
        # Enforce limit
        if len(self._commands) > self._limit:
            excess = len(self._commands) - self._limit
            del self._commands[:excess]
            self._cursor -= excess

    def undo(self, ctx: object) -> None:
        """Undo the last executed command."""
        if not self.can_undo():
            return
        self._cursor -= 1
        self._commands[self._cursor].undo(ctx)

    def redo(self, ctx: object) -> None:
        """Redo the last undone command."""
        if not self.can_redo():
            return
        self._commands[self._cursor].execute(ctx)
        self._cursor += 1

    def can_undo(self) -> bool:
        return self._cursor > 0

    def can_redo(self) -> bool:
        return self._cursor < len(self._commands)

    def clear(self) -> None:
        """Discard all commands."""
        self._commands.clear()
        self._cursor = 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py tests/test_notes_undo.py
git commit -m "feat(notes): add UndoStack and UndoCommand base classes"
```

---

### Task 2: NotesCmdContext

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add `NotesCmdContext`
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — construct context in `__init__`

- [ ] **Step 1: Add NotesCmdContext to notes_undo.py**

Append to `src/leap/monitor/dialogs/notes_undo.py`:

```python
class NotesCmdContext:
    """Thin interface between undo commands and the NotesDialog.

    Decouples command logic from the full dialog API.
    """

    def __init__(self, dialog: 'NotesDialog') -> None:
        self._d = dialog

    @property
    def current_name(self) -> Optional[str]:
        return self._d._current_name

    @current_name.setter
    def current_name(self, value: Optional[str]) -> None:
        self._d._current_name = value

    @property
    def saved_text(self) -> str:
        return self._d._saved_text

    @saved_text.setter
    def saved_text(self, value: str) -> None:
        self._d._saved_text = value

    @property
    def pending_image_deletes(self) -> set:
        return self._d._pending_image_deletes

    def refresh_tree(self, select_name: Optional[str] = None,
                     select_type: Optional[str] = None) -> None:
        """Refresh the tree and optionally select an item."""
        kwargs: dict = {}
        if select_name is not None:
            kwargs['select_name'] = select_name
        if select_type is not None:
            kwargs['select_type'] = select_type
        self._d._refresh_tree(**kwargs)

    def trigger_item_changed(self) -> None:
        """Re-fire the current item changed handler."""
        self._d._on_item_changed(self._d._tree.currentItem(), None)

    def select_and_load(self, name: Optional[str] = None,
                        select_type: str = 'note') -> None:
        """Refresh tree, select the given item, and trigger item changed."""
        if name is not None:
            self.refresh_tree(select_name=name, select_type=select_type)
        else:
            self.refresh_tree()
        self.trigger_item_changed()

    def select_first_or_none(self) -> None:
        """Select the first note, or clear the editor if none exist."""
        first = self._d._find_first_note(self._d._tree.invisibleRootItem())
        if first:
            self._d._tree.setCurrentItem(first)
        else:
            self._d._on_item_changed(None, None)

    def set_mode_combo(self, index: int) -> None:
        """Set the mode combo without triggering _on_mode_changed."""
        self._d._switching_mode = True
        self._d._mode_combo.setCurrentIndex(index)
        self._d._switching_mode = False

    def load_note_into_editor(self, name: str, text: str, mode: str) -> None:
        """Load specific content into the editor/checklist."""
        from leap.monitor.dialogs.notes_dialog import _parse_checklist
        self._d._current_name = name
        self._d._saved_text = text
        self._d._switching_mode = True
        if mode == 'checklist':
            self._d._mode_combo.setCurrentIndex(self._d._MODE_CHECKLIST)
            self._d._checklist.set_items(_parse_checklist(text))
            self._d._stack.setCurrentIndex(self._d._MODE_CHECKLIST)
        else:
            self._d._mode_combo.setCurrentIndex(self._d._MODE_TEXT)
            self._d._editor.set_note_content(text)
            self._d._editor.setEnabled(True)
            self._d._stack.setCurrentIndex(self._d._MODE_TEXT)
        self._d._switching_mode = False
        self._d._mode_combo.setEnabled(True)
        self._d._update_action_visibility(True)

    def get_checklist_items(self) -> list[dict]:
        """Get current checklist items from the widget."""
        return self._d._checklist.get_items()

    def set_checklist_items(self, items: list[dict]) -> None:
        """Set checklist items and rebuild."""
        self._d._checklist.set_items(items)

    def get_editor_content(self) -> str:
        """Get current text editor content."""
        return self._d._editor.get_note_content()

    def set_editor_content(self, text: str) -> None:
        """Set text editor content."""
        self._d._editor.set_note_content(text)
```

- [ ] **Step 2: Wire undo stack + context + pending_image_deletes into NotesDialog.__init__**

In `src/leap/monitor/dialogs/notes_dialog.py`, add the import at the top:

```python
from leap.monitor.dialogs.notes_undo import NotesCmdContext, NotesUndoStack
```

In `NotesDialog.__init__`, after `self._switching_mode: bool = False` (line 1725), add:

```python
        self._undo_stack = NotesUndoStack(limit=50)
        self._cmd_ctx = NotesCmdContext(self)
        self._pending_image_deletes: set[str] = set()
```

- [ ] **Step 3: Run existing tests to verify no regressions**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All existing tests still pass

- [ ] **Step 4: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py src/leap/monitor/dialogs/notes_dialog.py
git commit -m "feat(notes): add NotesCmdContext and wire undo stack into dialog"
```

---

### Task 3: Deferred image deletion

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — modify `_cleanup_orphaned_images`, add `_finalize_image_cleanup`, update close path

- [ ] **Step 1: Modify _cleanup_orphaned_images to support deferred mode**

In `src/leap/monitor/dialogs/notes_dialog.py`, change the signature and body of `_cleanup_orphaned_images` (line 94):

```python
def _cleanup_orphaned_images(
    current_text: str, previous_text: str, note_name: str,
    pasted: Optional[set[str]] = None,
    deferred: Optional[set[str]] = None,
) -> None:
    """Delete images removed from a note, unless still used by another note.

    *pasted* includes images saved to disk this session that may not appear
    in *previous_text* (e.g. pasted then deleted before save).

    If *deferred* is a set, orphaned filenames are added to it instead
    of being deleted immediately (for undo support).
    """
    old_refs = _collect_image_refs(previous_text)
    if pasted:
        old_refs |= pasted
    new_refs = _collect_image_refs(current_text)
    candidates = old_refs - new_refs
    if not candidates:
        return
    # Check all other notes before deleting
    other_refs = _all_note_image_refs(exclude_name=note_name)
    for filename in candidates - other_refs:
        if deferred is not None:
            deferred.add(filename)
        else:
            try:
                (NOTE_IMAGES_DIR / filename).unlink(missing_ok=True)
            except OSError:
                pass
```

- [ ] **Step 2: Pass deferred set from _save_current**

In `_save_current` (line 2845), change the `_cleanup_orphaned_images` call to pass the deferred set:

```python
                _cleanup_orphaned_images(
                    text, self._saved_text, self._current_name, pasted,
                    deferred=self._pending_image_deletes)
```

- [ ] **Step 3: Add _finalize_image_cleanup method**

Add this method to `NotesDialog`, just before the `done` method (before line 2872):

```python
    def _finalize_image_cleanup(self) -> None:
        """Delete deferred orphaned images. Called on dialog close."""
        if not self._pending_image_deletes:
            return
        # Final safety check — scan all notes on disk
        all_refs = _all_note_image_refs()
        for filename in self._pending_image_deletes - all_refs:
            try:
                (NOTE_IMAGES_DIR / filename).unlink(missing_ok=True)
            except OSError:
                pass
        self._pending_image_deletes.clear()
```

- [ ] **Step 4: Call _finalize_image_cleanup in done() and closeEvent()**

In `done()` (line 2872), after `self._save_current()` (line 2882), add:

```python
        self._finalize_image_cleanup()
        self._undo_stack.clear()
```

In `closeEvent()` (line 2890), after `self._save_current()` (line 2905), add:

```python
        self._finalize_image_cleanup()
        self._undo_stack.clear()
```

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add src/leap/monitor/dialogs/notes_dialog.py
git commit -m "feat(notes): deferred image deletion for undo support"
```

---

### Task 4: Cmd+Z / Cmd+Shift+Z key routing

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — update `keyPressEvent`

- [ ] **Step 1: Update keyPressEvent**

In `NotesDialog.keyPressEvent` (line 2926), add the Cmd+Z / Cmd+Shift+Z handling. Insert this block right after `mods = event.modifiers()` (line 2932) and before the existing `if mods & Qt.ControlModifier:` block (line 2933):

```python
        # Undo/redo — only when not inside a text-editing widget
        if (mods & Qt.ControlModifier) and event.key() == Qt.Key_Z:
            focus = QApplication.focusWidget()
            if not isinstance(focus, (_NoteTextEdit, _ItemLineEdit, QTextEdit)):
                if mods & Qt.ShiftModifier:
                    self._undo_stack.redo(self._cmd_ctx)
                else:
                    self._undo_stack.undo(self._cmd_ctx)
                return
```

- [ ] **Step 2: Update the bottom hint label**

In `__init__`, find the hint label text (line 1889-1891) and update it to include Cmd+Z:

```python
        hint = QLabel(
            'Cmd+N: New note  |  Cmd+Shift+N: New folder  |  Cmd+F: Search'
            '  |  Cmd+Z: Undo  |  Delete/\u232b: Delete  |  Right-click: More')
```

- [ ] **Step 3: Run existing tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add src/leap/monitor/dialogs/notes_dialog.py
git commit -m "feat(notes): route Cmd+Z/Shift+Z to undo stack"
```

---

### Task 5: CreateNoteCmd and CreateFolderCmd

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add command classes
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — wire into `_on_new` and `_on_new_folder`
- Modify: `tests/test_notes_undo.py` — add tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_notes_undo.py`:

```python
import json
from pathlib import Path

from leap.monitor.dialogs.notes_undo import (
    CreateFolderCmd, CreateNoteCmd, NotesCmdContext, NotesUndoStack,
)
from leap.monitor.dialogs import notes_dialog as nd


@pytest.fixture
def notes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect NOTES_DIR and NOTE_IMAGES_DIR to tmp_path."""
    d = tmp_path / 'notes'
    d.mkdir()
    img_d = tmp_path / 'note_images'
    img_d.mkdir()
    monkeypatch.setattr(nd, 'NOTES_DIR', d)
    monkeypatch.setattr(nd, '_NOTES_META_FILE', d / '.notes_meta.json')
    # Also patch the module-level constants used by notes_undo
    import leap.monitor.dialogs.notes_undo as nu
    monkeypatch.setattr(nu, 'NOTES_DIR', d)
    monkeypatch.setattr(nu, '_NOTES_META_FILE', d / '.notes_meta.json')
    monkeypatch.setattr(nu, 'NOTE_IMAGES_DIR', img_d)
    return d


class _StubCtx:
    """Minimal stub for NotesCmdContext in tests."""

    def __init__(self) -> None:
        self.current_name: str | None = None
        self.saved_text: str = ''
        self.pending_image_deletes: set[str] = set()
        self._refreshed: list = []

    def refresh_tree(self, select_name=None, select_type=None) -> None:
        self._refreshed.append((select_name, select_type))

    def trigger_item_changed(self) -> None:
        pass

    def select_and_load(self, name=None, select_type='note') -> None:
        self._refreshed.append((name, select_type))

    def select_first_or_none(self) -> None:
        pass

    def set_mode_combo(self, index: int) -> None:
        pass

    def load_note_into_editor(self, name, text, mode) -> None:
        pass

    def get_checklist_items(self) -> list[dict]:
        return []

    def set_checklist_items(self, items: list[dict]) -> None:
        pass

    def get_editor_content(self) -> str:
        return ''

    def set_editor_content(self, text: str) -> None:
        pass


class TestCreateNoteCmd:
    def test_undo_removes_created_note(self, notes_dir: Path) -> None:
        ctx = _StubCtx()
        cmd = CreateNoteCmd(name='MyNote', folder='')
        cmd.execute(ctx)
        assert (notes_dir / 'MyNote.txt').exists()
        cmd.undo(ctx)
        assert not (notes_dir / 'MyNote.txt').exists()

    def test_undo_removes_note_in_folder(self, notes_dir: Path) -> None:
        (notes_dir / 'sub').mkdir()
        ctx = _StubCtx()
        cmd = CreateNoteCmd(name='sub/Test', folder='sub')
        cmd.execute(ctx)
        assert (notes_dir / 'sub' / 'Test.txt').exists()
        cmd.undo(ctx)
        assert not (notes_dir / 'sub' / 'Test.txt').exists()

    def test_redo_recreates_note(self, notes_dir: Path) -> None:
        ctx = _StubCtx()
        stack = NotesUndoStack(limit=50)
        stack.push(CreateNoteCmd(name='X', folder=''), ctx=ctx)
        assert (notes_dir / 'X.txt').exists()
        stack.undo(ctx=ctx)
        assert not (notes_dir / 'X.txt').exists()
        stack.redo(ctx=ctx)
        assert (notes_dir / 'X.txt').exists()


class TestCreateFolderCmd:
    def test_undo_removes_created_folder(self, notes_dir: Path) -> None:
        ctx = _StubCtx()
        cmd = CreateFolderCmd(folder_path='NewFolder')
        cmd.execute(ctx)
        assert (notes_dir / 'NewFolder').is_dir()
        cmd.undo(ctx)
        assert not (notes_dir / 'NewFolder').exists()

    def test_undo_removes_nested_folder(self, notes_dir: Path) -> None:
        (notes_dir / 'parent').mkdir()
        ctx = _StubCtx()
        cmd = CreateFolderCmd(folder_path='parent/child')
        cmd.execute(ctx)
        assert (notes_dir / 'parent' / 'child').is_dir()
        cmd.undo(ctx)
        assert not (notes_dir / 'parent' / 'child').exists()
        assert (notes_dir / 'parent').is_dir()  # parent untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestCreateNoteCmd -v`
Expected: FAIL — `ImportError: cannot import name 'CreateNoteCmd'`

- [ ] **Step 3: Implement CreateNoteCmd and CreateFolderCmd**

Add to `src/leap/monitor/dialogs/notes_undo.py`, adding required imports at the top:

```python
import json
import shutil
from pathlib import Path
from typing import Optional

from leap.utils.constants import NOTE_IMAGES_DIR, NOTES_DIR

_NOTES_META_FILE: Path = NOTES_DIR / '.notes_meta.json'
```

Then add helper functions (these mirror the ones in notes_dialog.py but are needed by commands independently):

```python
def _note_path(name: str) -> Path:
    return NOTES_DIR / f'{name}.txt'


def _load_notes_meta() -> dict:
    try:
        if _NOTES_META_FILE.exists():
            return json.loads(_NOTES_META_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_notes_meta(meta: dict) -> None:
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        _NOTES_META_FILE.write_text(json.dumps(meta, indent=2), encoding='utf-8')
    except OSError:
        pass


def _load_order() -> dict[str, list[str]]:
    return _load_notes_meta().get('_order', {})


def _save_order(order: dict[str, list[str]]) -> None:
    meta = _load_notes_meta()
    if order:
        meta['_order'] = order
    else:
        meta.pop('_order', None)
    _save_notes_meta(meta)


def _remove_from_order(folder: str, leaf: str) -> None:
    order = _load_order()
    lst = order.get(folder, [])
    if leaf in lst:
        lst.remove(leaf)
        if lst:
            order[folder] = lst
        else:
            order.pop(folder, None)
        _save_order(order)


def _insert_into_order(folder: str, leaf: str, position: Optional[int] = None) -> None:
    """Insert *leaf* into *folder*'s order at *position* (or end if None)."""
    order = _load_order()
    lst = order.get(folder, [])
    if leaf in lst:
        return
    if position is not None and 0 <= position <= len(lst):
        lst.insert(position, leaf)
    else:
        lst.append(leaf)
    order[folder] = lst
    _save_order(order)
```

Then the command classes:

```python
class CreateNoteCmd(UndoCommand):
    """Undo command for creating a new note."""

    def __init__(self, name: str, folder: str) -> None:
        leaf = name.rsplit('/', 1)[-1] if '/' in name else name
        super().__init__(description=f"Create note '{leaf}'")
        self._name = name
        self._folder = folder

    def execute(self, ctx: object) -> None:
        path = _note_path(self._name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('', encoding='utf-8')

    def undo(self, ctx: object) -> None:
        path = _note_path(self._name)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        leaf = self._name.rsplit('/', 1)[-1] if '/' in self._name else self._name
        _remove_from_order(self._folder, leaf)
        # Remove metadata
        meta = _load_notes_meta()
        if meta.pop(self._name, None) is not None:
            _save_notes_meta(meta)
        if hasattr(ctx, 'select_first_or_none'):
            ctx.select_first_or_none()


class CreateFolderCmd(UndoCommand):
    """Undo command for creating a new folder."""

    def __init__(self, folder_path: str) -> None:
        leaf = folder_path.rsplit('/', 1)[-1] if '/' in folder_path else folder_path
        super().__init__(description=f"Create folder '{leaf}'")
        self._folder_path = folder_path

    def execute(self, ctx: object) -> None:
        (NOTES_DIR / self._folder_path).mkdir(parents=True, exist_ok=True)

    def undo(self, ctx: object) -> None:
        target = NOTES_DIR / self._folder_path
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        parent = self._folder_path.rsplit('/', 1)[0] if '/' in self._folder_path else ''
        leaf = self._folder_path.rsplit('/', 1)[-1] if '/' in self._folder_path else self._folder_path
        _remove_from_order(parent, leaf)
        if hasattr(ctx, 'select_first_or_none'):
            ctx.select_first_or_none()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py -v`
Expected: All tests pass

- [ ] **Step 5: Wire CreateNoteCmd into _on_new()**

In `notes_dialog.py`, add `CreateNoteCmd` to the import:

```python
from leap.monitor.dialogs.notes_undo import (
    CreateNoteCmd, CreateFolderCmd, NotesCmdContext, NotesUndoStack,
)
```

In `_on_new()` (line 2375), replace the file creation + tree refresh block (lines 2404-2411):

```python
        self._save_current()
        cmd = CreateNoteCmd(name=full_name, folder=folder)
        self._undo_stack.push(cmd, self._cmd_ctx)
        self._refresh_tree(select_name=full_name)
        self._on_item_changed(self._tree.currentItem(), None)
        if self._current_mode() == self._MODE_TEXT:
            self._editor.setFocus()
```

- [ ] **Step 6: Wire CreateFolderCmd into _on_new_folder()**

In `_on_new_folder()` (line 2413), replace lines 2442-2447:

```python
        cmd = CreateFolderCmd(folder_path=full_path)
        self._undo_stack.push(cmd, self._cmd_ctx)
        self._refresh_tree(select_name=full_path, select_type='folder')
```

- [ ] **Step 7: Run all tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py src/leap/monitor/dialogs/notes_dialog.py tests/test_notes_undo.py
git commit -m "feat(notes): undo for create note/folder"
```

---

### Task 6: DeleteNoteCmd

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add `DeleteNoteCmd`
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — wire into `_on_delete` (note path)
- Modify: `tests/test_notes_undo.py` — add tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_notes_undo.py`:

```python
import re

from leap.monitor.dialogs.notes_undo import DeleteNoteCmd


class TestDeleteNoteCmd:
    def test_delete_and_undo_restores_note(self, notes_dir: Path) -> None:
        # Create a note on disk
        (notes_dir / 'Todo.txt').write_text('buy milk', encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(
            name='Todo',
            content='buy milk',
            metadata={'mode': 'text'},
            order_position=('', 0),
            image_refs=set(),
        )
        cmd.execute(ctx)
        assert not (notes_dir / 'Todo.txt').exists()
        cmd.undo(ctx)
        assert (notes_dir / 'Todo.txt').exists()
        assert (notes_dir / 'Todo.txt').read_text(encoding='utf-8') == 'buy milk'

    def test_delete_defers_image_cleanup(self, notes_dir: Path) -> None:
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'abc123.png').write_bytes(b'fake')
        (notes_dir / 'Img.txt').write_text('![image](abc123.png)', encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(
            name='Img',
            content='![image](abc123.png)',
            metadata={},
            order_position=('', 0),
            image_refs={'abc123.png'},
        )
        cmd.execute(ctx)
        # Image NOT deleted — just deferred
        assert (img_dir / 'abc123.png').exists()
        assert 'abc123.png' in ctx.pending_image_deletes

    def test_undo_restores_image_from_deferred(self, notes_dir: Path) -> None:
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'abc123.png').write_bytes(b'fake')
        (notes_dir / 'Img.txt').write_text('![image](abc123.png)', encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(
            name='Img',
            content='![image](abc123.png)',
            metadata={},
            order_position=('', 0),
            image_refs={'abc123.png'},
        )
        cmd.execute(ctx)
        assert 'abc123.png' in ctx.pending_image_deletes
        cmd.undo(ctx)
        assert 'abc123.png' not in ctx.pending_image_deletes
        assert (notes_dir / 'Img.txt').exists()

    def test_delete_restores_metadata(self, notes_dir: Path) -> None:
        (notes_dir / 'CL.txt').write_text('- [ ] a', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({'CL': {'mode': 'checklist'}}), encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(
            name='CL',
            content='- [ ] a',
            metadata={'mode': 'checklist'},
            order_position=('', 0),
            image_refs=set(),
        )
        cmd.execute(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert 'CL' not in meta
        cmd.undo(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta.get('CL') == {'mode': 'checklist'}

    def test_delete_restores_order_position(self, notes_dir: Path) -> None:
        (notes_dir / 'A.txt').write_text('', encoding='utf-8')
        (notes_dir / 'B.txt').write_text('', encoding='utf-8')
        (notes_dir / 'C.txt').write_text('', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            '_order': {'': ['A', 'B', 'C']}
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(
            name='B',
            content='',
            metadata={},
            order_position=('', 1),
            image_refs=set(),
        )
        cmd.execute(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert 'B' not in meta.get('_order', {}).get('', [])
        cmd.undo(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        order = meta.get('_order', {}).get('', [])
        assert order.index('B') == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestDeleteNoteCmd -v`
Expected: FAIL — `ImportError: cannot import name 'DeleteNoteCmd'`

- [ ] **Step 3: Implement DeleteNoteCmd**

Add to `src/leap/monitor/dialogs/notes_undo.py`:

```python
class DeleteNoteCmd(UndoCommand):
    """Undo command for deleting a single note."""

    def __init__(
        self,
        name: str,
        content: str,
        metadata: dict,
        order_position: tuple[str, int],
        image_refs: set[str],
    ) -> None:
        leaf = name.rsplit('/', 1)[-1] if '/' in name else name
        super().__init__(description=f"Delete note '{leaf}'")
        self._name = name
        self._content = content
        self._metadata = metadata
        self._order_folder = order_position[0]
        self._order_index = order_position[1]
        self._image_refs = image_refs

    def execute(self, ctx: object) -> None:
        path = _note_path(self._name)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        # Remove metadata
        meta = _load_notes_meta()
        meta.pop(self._name, None)
        _save_notes_meta(meta)
        # Remove from order
        leaf = self._name.rsplit('/', 1)[-1] if '/' in self._name else self._name
        _remove_from_order(self._order_folder, leaf)
        # Defer image deletes
        if hasattr(ctx, 'pending_image_deletes'):
            ctx.pending_image_deletes |= self._image_refs

    def undo(self, ctx: object) -> None:
        # Restore file
        path = _note_path(self._name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._content, encoding='utf-8')
        # Restore metadata
        if self._metadata:
            meta = _load_notes_meta()
            meta[self._name] = dict(self._metadata)
            _save_notes_meta(meta)
        # Restore order position
        leaf = self._name.rsplit('/', 1)[-1] if '/' in self._name else self._name
        _insert_into_order(self._order_folder, leaf, self._order_index)
        # Un-defer images
        if hasattr(ctx, 'pending_image_deletes'):
            ctx.pending_image_deletes -= self._image_refs
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=self._name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestDeleteNoteCmd -v`
Expected: All 5 tests pass

- [ ] **Step 5: Wire into _on_delete (note deletion path)**

In `notes_dialog.py`, add `DeleteNoteCmd` to the import. Then in `_on_delete()`, we need to snapshot state before deletion and push the command. This is a partial refactor — the note deletion path (the loop over `all_notes`) needs to create one command per note. However since `_on_delete` can delete multiple notes AND folders at once, we'll handle this by creating a compound approach: push individual commands for each note being deleted. But since the method also handles folders, we'll complete folder delete in Task 7.

For now, modify `_on_delete` to push `DeleteNoteCmd` for each standalone note (not those inside deleted folders). The full wiring happens after `DeleteFolderCmd` is implemented. For this step, add the import:

```python
from leap.monitor.dialogs.notes_undo import (
    CreateNoteCmd, CreateFolderCmd, DeleteNoteCmd,
    NotesCmdContext, NotesUndoStack,
)
```

The actual wiring of `_on_delete` will be done in Task 8 after `DeleteFolderCmd` exists, since the method handles both notes and folders together.

- [ ] **Step 6: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py src/leap/monitor/dialogs/notes_dialog.py tests/test_notes_undo.py
git commit -m "feat(notes): add DeleteNoteCmd with deferred image cleanup"
```

---

### Task 7: DeleteFolderCmd

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add `DeleteFolderCmd`
- Modify: `tests/test_notes_undo.py` — add tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_notes_undo.py`:

```python
from leap.monitor.dialogs.notes_undo import DeleteFolderCmd


class TestDeleteFolderCmd:
    def test_delete_and_undo_restores_folder(self, notes_dir: Path) -> None:
        folder = notes_dir / 'Work'
        folder.mkdir()
        (folder / 'Task1.txt').write_text('do stuff', encoding='utf-8')
        (folder / 'Task2.txt').write_text('more stuff', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'Work/Task1': {'mode': 'text'},
            'Work/Task2': {'mode': 'checklist'},
            '_order': {'': ['Work'], 'Work': ['Task1', 'Task2']},
        }), encoding='utf-8')

        ctx = _StubCtx()
        cmd = DeleteFolderCmd(
            folder_path='Work',
            notes={'Work/Task1': 'do stuff', 'Work/Task2': 'more stuff'},
            metadata_entries={
                'Work/Task1': {'mode': 'text'},
                'Work/Task2': {'mode': 'checklist'},
            },
            order_entries={'Work': ['Task1', 'Task2']},
            subfolder_paths=[],
            parent_order_position=('', 0),
            image_refs=set(),
        )
        cmd.execute(ctx)
        assert not folder.exists()

        cmd.undo(ctx)
        assert folder.is_dir()
        assert (folder / 'Task1.txt').read_text(encoding='utf-8') == 'do stuff'
        assert (folder / 'Task2.txt').read_text(encoding='utf-8') == 'more stuff'
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta.get('Work/Task1') == {'mode': 'text'}
        assert meta.get('Work/Task2') == {'mode': 'checklist'}
        order = meta.get('_order', {})
        assert 'Work' in order.get('', [])
        assert order.get('Work') == ['Task1', 'Task2']

    def test_delete_folder_defers_images(self, notes_dir: Path) -> None:
        folder = notes_dir / 'Pics'
        folder.mkdir()
        (folder / 'Note.txt').write_text('![image](img1.png)', encoding='utf-8')
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'img1.png').write_bytes(b'fake')

        ctx = _StubCtx()
        cmd = DeleteFolderCmd(
            folder_path='Pics',
            notes={'Pics/Note': '![image](img1.png)'},
            metadata_entries={},
            order_entries={},
            subfolder_paths=[],
            parent_order_position=('', 0),
            image_refs={'img1.png'},
        )
        cmd.execute(ctx)
        assert 'img1.png' in ctx.pending_image_deletes
        cmd.undo(ctx)
        assert 'img1.png' not in ctx.pending_image_deletes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestDeleteFolderCmd -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement DeleteFolderCmd**

Add to `src/leap/monitor/dialogs/notes_undo.py`:

```python
class DeleteFolderCmd(UndoCommand):
    """Undo command for deleting a folder and all its contents."""

    def __init__(
        self,
        folder_path: str,
        notes: dict[str, str],
        metadata_entries: dict[str, dict],
        order_entries: dict[str, list[str]],
        subfolder_paths: list[str],
        parent_order_position: tuple[str, int],
        image_refs: set[str],
    ) -> None:
        leaf = folder_path.rsplit('/', 1)[-1] if '/' in folder_path else folder_path
        super().__init__(description=f"Delete folder '{leaf}'")
        self._folder_path = folder_path
        self._notes = notes
        self._metadata_entries = metadata_entries
        self._order_entries = order_entries
        self._subfolder_paths = subfolder_paths
        self._parent_folder = parent_order_position[0]
        self._parent_index = parent_order_position[1]
        self._image_refs = image_refs

    def execute(self, ctx: object) -> None:
        # Delete all note files
        for name in self._notes:
            try:
                _note_path(name).unlink(missing_ok=True)
            except OSError:
                pass
        # Remove metadata
        meta = _load_notes_meta()
        for name in self._notes:
            meta.pop(name, None)
        _save_notes_meta(meta)
        # Remove order entries for the folder and children
        order = _load_order()
        for k in list(order):
            if k == self._folder_path or k.startswith(self._folder_path + '/'):
                del order[k]
        _save_order(order)
        # Remove folder from parent order
        leaf = (self._folder_path.rsplit('/', 1)[-1]
                if '/' in self._folder_path else self._folder_path)
        _remove_from_order(self._parent_folder, leaf)
        # Delete the folder tree
        target = NOTES_DIR / self._folder_path
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        # Defer images
        if hasattr(ctx, 'pending_image_deletes'):
            ctx.pending_image_deletes |= self._image_refs

    def undo(self, ctx: object) -> None:
        # Recreate folder structure
        (NOTES_DIR / self._folder_path).mkdir(parents=True, exist_ok=True)
        for sf in self._subfolder_paths:
            (NOTES_DIR / sf).mkdir(parents=True, exist_ok=True)
        # Restore note files
        for name, content in self._notes.items():
            path = _note_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        # Restore metadata
        if self._metadata_entries:
            meta = _load_notes_meta()
            for name, entry in self._metadata_entries.items():
                meta[name] = dict(entry)
            _save_notes_meta(meta)
        # Restore order entries
        if self._order_entries:
            order = _load_order()
            for k, v in self._order_entries.items():
                order[k] = list(v)
            _save_order(order)
        # Restore in parent order
        leaf = (self._folder_path.rsplit('/', 1)[-1]
                if '/' in self._folder_path else self._folder_path)
        _insert_into_order(self._parent_folder, leaf, self._parent_index)
        # Un-defer images
        if hasattr(ctx, 'pending_image_deletes'):
            ctx.pending_image_deletes -= self._image_refs
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(
                name=self._folder_path, select_type='folder')
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestDeleteFolderCmd -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py tests/test_notes_undo.py
git commit -m "feat(notes): add DeleteFolderCmd"
```

---

### Task 8: Wire delete undo into _on_delete

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — refactor `_on_delete` to use commands
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add `BatchDeleteCmd` for multi-item deletes

- [ ] **Step 1: Add BatchDeleteCmd**

When multiple notes/folders are selected, we need a single undo action. Add to `notes_undo.py`:

```python
class BatchDeleteCmd(UndoCommand):
    """Wraps multiple delete commands into a single undo action."""

    def __init__(self, commands: list[UndoCommand], description: str) -> None:
        super().__init__(description=description)
        self._commands = commands

    def execute(self, ctx: object) -> None:
        for cmd in self._commands:
            cmd.execute(ctx)

    def undo(self, ctx: object) -> None:
        for cmd in reversed(self._commands):
            cmd.undo(ctx)
```

- [ ] **Step 2: Refactor _on_delete to snapshot and push commands**

Replace the body of `_on_delete` in `notes_dialog.py` (from line 2538). Update the import to include `BatchDeleteCmd, DeleteFolderCmd`:

```python
from leap.monitor.dialogs.notes_undo import (
    BatchDeleteCmd, CreateNoteCmd, CreateFolderCmd, DeleteFolderCmd,
    DeleteNoteCmd, NotesCmdContext, NotesUndoStack,
)
```

New `_on_delete` body:

```python
    def _on_delete(self) -> None:
        """Delete the selected note(s) or folder(s)."""
        selected = self._tree.selectedItems()
        if not selected:
            return

        note_names: list[str] = []
        folder_paths: list[str] = []
        for sel_item in selected:
            path = sel_item.data(0, self._ROLE_PATH)
            if not path:
                continue
            if sel_item.data(0, self._ROLE_TYPE) == 'folder':
                folder_paths.append(path)
            else:
                note_names.append(path)

        if not note_names and not folder_paths:
            return

        # Build confirmation message
        parts: list[str] = []
        if note_names:
            if len(note_names) == 1:
                leaf = note_names[0].rsplit('/', 1)[-1]
                parts.append(f"note '{leaf}'")
            else:
                parts.append(f'{len(note_names)} notes')
        if folder_paths:
            if len(folder_paths) == 1:
                parts.append(
                    f"folder '{folder_paths[0]}' and all its contents")
            else:
                parts.append(
                    f'{len(folder_paths)} folders and all their contents')

        reply = QMessageBox.question(
            self, 'Delete', f'Delete {" and ".join(parts)}?',
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # Save current note before snapshotting
        self._save_current()

        # Collect all note names inside folders
        folder_note_names: set[str] = set()
        for fp in folder_paths:
            prefix = fp + '/'
            for n in _list_notes():
                if n.startswith(prefix):
                    folder_note_names.add(n)

        # Build commands
        from leap.monitor.dialogs.notes_undo import (
            BatchDeleteCmd, DeleteFolderCmd, DeleteNoteCmd,
        )
        commands: list = []

        # Individual note delete commands
        for name in note_names:
            if name in folder_note_names:
                continue  # will be handled by folder cmd
            path = _note_path(name)
            try:
                content = path.read_text(encoding='utf-8') if path.exists() else ''
            except OSError:
                content = ''
            # Live content for current note
            if name == self._current_name:
                if self._current_mode() == self._MODE_CHECKLIST:
                    content = _serialize_checklist(self._checklist.get_items())
                else:
                    content = self._editor.get_note_content()
            meta = _load_notes_meta().get(name, {})
            parent = name.rsplit('/', 1)[0] if '/' in name else ''
            leaf = name.rsplit('/', 1)[-1] if '/' in name else name
            order = _load_order().get(parent, [])
            pos = order.index(leaf) if leaf in order else len(order)
            image_refs = _collect_image_refs(content)
            commands.append(DeleteNoteCmd(
                name=name, content=content, metadata=dict(meta),
                order_position=(parent, pos), image_refs=image_refs,
            ))

        # Folder delete commands
        for fp in folder_paths:
            notes_in_folder: dict[str, str] = {}
            image_refs: set[str] = set()
            for n in _list_notes():
                if n.startswith(fp + '/') or n == fp:
                    path = _note_path(n)
                    try:
                        c = path.read_text(encoding='utf-8') if path.exists() else ''
                    except OSError:
                        c = ''
                    if n == self._current_name:
                        if self._current_mode() == self._MODE_CHECKLIST:
                            c = _serialize_checklist(self._checklist.get_items())
                        else:
                            c = self._editor.get_note_content()
                    notes_in_folder[n] = c
                    image_refs |= _collect_image_refs(c)

            all_meta = _load_notes_meta()
            meta_entries = {k: dict(v) for k, v in all_meta.items()
                           if k.startswith(fp + '/') or k == fp}

            all_order = _load_order()
            order_entries = {k: list(v) for k, v in all_order.items()
                            if k == fp or k.startswith(fp + '/')}

            # Subfolders
            subfolder_paths = []
            for p in sorted(NOTES_DIR.rglob('*')):
                if p.is_dir():
                    rel = str(p.relative_to(NOTES_DIR))
                    if rel.startswith(fp + '/'):
                        subfolder_paths.append(rel)

            parent = fp.rsplit('/', 1)[0] if '/' in fp else ''
            leaf = fp.rsplit('/', 1)[-1] if '/' in fp else fp
            parent_order = _load_order().get(parent, [])
            pos = parent_order.index(leaf) if leaf in parent_order else len(parent_order)

            commands.append(DeleteFolderCmd(
                folder_path=fp, notes=notes_in_folder,
                metadata_entries=meta_entries, order_entries=order_entries,
                subfolder_paths=subfolder_paths,
                parent_order_position=(parent, pos), image_refs=image_refs,
            ))

        if len(commands) == 1:
            self._undo_stack.push(commands[0], self._cmd_ctx)
        elif commands:
            desc = f'Delete {" and ".join(parts)}'
            self._undo_stack.push(
                BatchDeleteCmd(commands, desc), self._cmd_ctx)

        self._current_name = None
        self._saved_text = ''
        self._refresh_tree()
        first = self._find_first_note(self._tree.invisibleRootItem())
        if first:
            self._tree.setCurrentItem(first)
        else:
            self._on_item_changed(None, None)
```

- [ ] **Step 3: Run all tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py src/leap/monitor/dialogs/notes_dialog.py
git commit -m "feat(notes): wire delete undo into _on_delete with batch support"
```

---

### Task 9: RenameNoteCmd and RenameFolderCmd

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add both commands
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — wire into `_on_rename`
- Modify: `tests/test_notes_undo.py` — add tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_notes_undo.py`:

```python
from leap.monitor.dialogs.notes_undo import RenameNoteCmd, RenameFolderCmd


class TestRenameNoteCmd:
    def test_rename_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'Old.txt').write_text('content', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'Old': {'mode': 'text'},
            '_order': {'': ['Old']},
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = RenameNoteCmd(
            old_name='Old', new_name='New',
            parent_folder='', old_leaf='Old', new_leaf='New',
        )
        cmd.execute(ctx)
        assert not (notes_dir / 'Old.txt').exists()
        assert (notes_dir / 'New.txt').exists()
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert 'New' in meta
        assert 'Old' not in meta

        cmd.undo(ctx)
        assert (notes_dir / 'Old.txt').exists()
        assert not (notes_dir / 'New.txt').exists()
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert 'Old' in meta
        assert 'New' not in meta


class TestRenameFolderCmd:
    def test_rename_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'OldDir').mkdir()
        (notes_dir / 'OldDir' / 'Note.txt').write_text('hi', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'OldDir/Note': {'mode': 'text'},
            '_order': {'': ['OldDir'], 'OldDir': ['Note']},
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = RenameFolderCmd(
            old_path='OldDir', new_path='NewDir',
            parent_folder='', old_leaf='OldDir', new_leaf='NewDir',
        )
        cmd.execute(ctx)
        assert not (notes_dir / 'OldDir').exists()
        assert (notes_dir / 'NewDir').is_dir()
        assert (notes_dir / 'NewDir' / 'Note.txt').exists()
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert 'NewDir/Note' in meta
        assert 'OldDir/Note' not in meta
        order = meta.get('_order', {})
        assert 'NewDir' in order
        assert 'OldDir' not in order

        cmd.undo(ctx)
        assert (notes_dir / 'OldDir').is_dir()
        assert not (notes_dir / 'NewDir').exists()
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert 'OldDir/Note' in meta
        order = meta.get('_order', {})
        assert 'OldDir' in order
```

- [ ] **Step 2: Run tests to verify failure**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestRenameNoteCmd tests/test_notes_undo.py::TestRenameFolderCmd -v`
Expected: FAIL

- [ ] **Step 3: Implement RenameNoteCmd and RenameFolderCmd**

Add to `src/leap/monitor/dialogs/notes_undo.py`:

```python
class RenameNoteCmd(UndoCommand):
    """Undo command for renaming a note."""

    def __init__(
        self,
        old_name: str, new_name: str,
        parent_folder: str, old_leaf: str, new_leaf: str,
    ) -> None:
        super().__init__(description=f"Rename note '{old_leaf}' to '{new_leaf}'")
        self._old_name = old_name
        self._new_name = new_name
        self._parent_folder = parent_folder
        self._old_leaf = old_leaf
        self._new_leaf = new_leaf

    def _do_rename(self, from_name: str, to_name: str,
                   from_leaf: str, to_leaf: str, ctx: object) -> None:
        try:
            _note_path(from_name).rename(_note_path(to_name))
        except OSError:
            return
        # Rename metadata
        meta = _load_notes_meta()
        if from_name in meta:
            meta[to_name] = meta.pop(from_name)
            _save_notes_meta(meta)
        # Rename in order
        order = _load_order()
        lst = order.get(self._parent_folder, [])
        if from_leaf in lst:
            lst[lst.index(from_leaf)] = to_leaf
            order[self._parent_folder] = lst
            _save_order(order)
        # Update current name
        if hasattr(ctx, 'current_name') and ctx.current_name == from_name:
            ctx.current_name = to_name
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=to_name)

    def execute(self, ctx: object) -> None:
        self._do_rename(self._old_name, self._new_name,
                        self._old_leaf, self._new_leaf, ctx)

    def undo(self, ctx: object) -> None:
        self._do_rename(self._new_name, self._old_name,
                        self._new_leaf, self._old_leaf, ctx)


class RenameFolderCmd(UndoCommand):
    """Undo command for renaming a folder."""

    def __init__(
        self,
        old_path: str, new_path: str,
        parent_folder: str, old_leaf: str, new_leaf: str,
    ) -> None:
        super().__init__(description=f"Rename folder '{old_leaf}' to '{new_leaf}'")
        self._old_path = old_path
        self._new_path = new_path
        self._parent_folder = parent_folder
        self._old_leaf = old_leaf
        self._new_leaf = new_leaf

    def _do_rename(self, from_path: str, to_path: str,
                   from_leaf: str, to_leaf: str, ctx: object) -> None:
        old_dir = NOTES_DIR / from_path
        new_dir = NOTES_DIR / to_path
        try:
            old_dir.rename(new_dir)
        except OSError:
            return
        # Rename metadata keys
        meta = _load_notes_meta()
        updated: dict = {}
        for key, value in meta.items():
            if key == '_order':
                updated[key] = value
                continue
            if key.startswith(from_path + '/') or key == from_path:
                new_key = to_path + key[len(from_path):]
                updated[new_key] = value
            else:
                updated[key] = value
        _save_notes_meta(updated)
        # Rename in parent order
        order = _load_order()
        lst = order.get(self._parent_folder, [])
        if from_leaf in lst:
            lst[lst.index(from_leaf)] = to_leaf
            order[self._parent_folder] = lst
        # Rename order keys
        for old_k in [k for k in order if k == from_path or k.startswith(from_path + '/')]:
            order[to_path + old_k[len(from_path):]] = order.pop(old_k)
        _save_order(order)
        # Update current name if under renamed folder
        if hasattr(ctx, 'current_name') and ctx.current_name:
            cn = ctx.current_name
            if cn.startswith(from_path + '/'):
                ctx.current_name = to_path + cn[len(from_path):]
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=to_path, select_type='folder')

    def execute(self, ctx: object) -> None:
        self._do_rename(self._old_path, self._new_path,
                        self._old_leaf, self._new_leaf, ctx)

    def undo(self, ctx: object) -> None:
        self._do_rename(self._new_path, self._old_path,
                        self._new_leaf, self._old_leaf, ctx)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestRenameNoteCmd tests/test_notes_undo.py::TestRenameFolderCmd -v`
Expected: All pass

- [ ] **Step 5: Wire into _on_rename**

In `_on_rename()` in `notes_dialog.py`, add the commands to the import and push after the rename succeeds.

For the note rename path (inside the `if item_type == 'note':` block, line 2504), replace lines 2504-2517 with:

```python
        if item_type == 'note':
            self._save_current()
            from leap.monitor.dialogs.notes_undo import RenameNoteCmd
            cmd = RenameNoteCmd(
                old_name=old_path, new_name=new_full,
                parent_folder=parent_folder,
                old_leaf=old_display, new_leaf=new_name,
            )
            self._undo_stack.push(cmd, self._cmd_ctx)
            self._refresh_tree(select_name=new_full)
            self._on_item_changed(self._tree.currentItem(), None)
```

For the folder rename path (the `else:` block, line 2518), replace lines 2518-2536 with:

```python
        else:
            from leap.monitor.dialogs.notes_undo import RenameFolderCmd
            cmd = RenameFolderCmd(
                old_path=old_path, new_path=new_full,
                parent_folder=parent_folder,
                old_leaf=old_display, new_leaf=new_name,
            )
            self._undo_stack.push(cmd, self._cmd_ctx)
            self._refresh_tree(select_name=new_full, select_type='folder')
```

- [ ] **Step 6: Run all tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py src/leap/monitor/dialogs/notes_dialog.py tests/test_notes_undo.py
git commit -m "feat(notes): undo for rename note/folder"
```

---

### Task 10: MoveNoteCmd, MoveFolderCmd, and ReorderCmd

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add 3 commands
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — wire into `_move_note`, `_move_folder`, `_reorder_in_folder`
- Modify: `tests/test_notes_undo.py` — add tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_notes_undo.py`:

```python
from leap.monitor.dialogs.notes_undo import MoveNoteCmd, MoveFolderCmd, ReorderCmd


class TestMoveNoteCmd:
    def test_move_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'Work').mkdir()
        (notes_dir / 'Task.txt').write_text('hello', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'Task': {'mode': 'text'},
            '_order': {'': ['Task', 'Work']},
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = MoveNoteCmd(
            old_name='Task', new_name='Work/Task',
            old_folder='', new_folder='Work',
            old_order_position=('', 0),
        )
        cmd.execute(ctx)
        assert not (notes_dir / 'Task.txt').exists()
        assert (notes_dir / 'Work' / 'Task.txt').exists()
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert 'Work/Task' in meta
        assert 'Task' not in meta

        cmd.undo(ctx)
        assert (notes_dir / 'Task.txt').exists()
        assert not (notes_dir / 'Work' / 'Task.txt').exists()
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert 'Task' in meta
        assert 'Work/Task' not in meta
        order = meta.get('_order', {}).get('', [])
        assert order.index('Task') == 0


class TestMoveFolderCmd:
    def test_move_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'A').mkdir()
        (notes_dir / 'B').mkdir()
        (notes_dir / 'A' / 'Note.txt').write_text('x', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'A/Note': {'mode': 'text'},
            '_order': {'': ['A', 'B'], 'A': ['Note']},
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = MoveFolderCmd(
            old_path='A', new_path='B/A',
            old_parent='', new_parent='B',
            old_order_position=('', 0),
        )
        cmd.execute(ctx)
        assert not (notes_dir / 'A').exists()
        assert (notes_dir / 'B' / 'A').is_dir()
        assert (notes_dir / 'B' / 'A' / 'Note.txt').exists()

        cmd.undo(ctx)
        assert (notes_dir / 'A').is_dir()
        assert not (notes_dir / 'B' / 'A').exists()
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        order = meta.get('_order', {}).get('', [])
        assert order.index('A') == 0


class TestReorderCmd:
    def test_reorder_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'A.txt').write_text('', encoding='utf-8')
        (notes_dir / 'B.txt').write_text('', encoding='utf-8')
        (notes_dir / 'C.txt').write_text('', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            '_order': {'': ['A', 'B', 'C']}
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = ReorderCmd(
            folder='',
            old_order=['A', 'B', 'C'],
            new_order=['C', 'A', 'B'],
        )
        cmd.execute(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta['_order'][''] == ['C', 'A', 'B']

        cmd.undo(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta['_order'][''] == ['A', 'B', 'C']
```

- [ ] **Step 2: Run tests to verify failure**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestMoveNoteCmd tests/test_notes_undo.py::TestMoveFolderCmd tests/test_notes_undo.py::TestReorderCmd -v`
Expected: FAIL

- [ ] **Step 3: Implement MoveNoteCmd, MoveFolderCmd, ReorderCmd**

Add to `src/leap/monitor/dialogs/notes_undo.py`:

```python
class MoveNoteCmd(UndoCommand):
    """Undo command for moving a note to a different folder."""

    def __init__(
        self,
        old_name: str, new_name: str,
        old_folder: str, new_folder: str,
        old_order_position: tuple[str, int],
    ) -> None:
        leaf = old_name.rsplit('/', 1)[-1] if '/' in old_name else old_name
        super().__init__(description=f"Move note '{leaf}'")
        self._old_name = old_name
        self._new_name = new_name
        self._old_folder = old_folder
        self._new_folder = new_folder
        self._old_order_folder = old_order_position[0]
        self._old_order_index = old_order_position[1]

    def execute(self, ctx: object) -> None:
        dest = _note_path(self._new_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            _note_path(self._old_name).rename(dest)
        except OSError:
            return
        # Rename metadata key
        meta = _load_notes_meta()
        if self._old_name in meta:
            meta[self._new_name] = meta.pop(self._old_name)
            _save_notes_meta(meta)
        # Remove from old order
        leaf = self._old_name.rsplit('/', 1)[-1] if '/' in self._old_name else self._old_name
        _remove_from_order(self._old_folder, leaf)
        if hasattr(ctx, 'current_name') and ctx.current_name == self._old_name:
            ctx.current_name = self._new_name

    def undo(self, ctx: object) -> None:
        dest = _note_path(self._old_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            _note_path(self._new_name).rename(dest)
        except OSError:
            return
        # Rename metadata back
        meta = _load_notes_meta()
        if self._new_name in meta:
            meta[self._old_name] = meta.pop(self._new_name)
            _save_notes_meta(meta)
        # Remove from new folder order
        leaf = self._new_name.rsplit('/', 1)[-1] if '/' in self._new_name else self._new_name
        _remove_from_order(self._new_folder, leaf)
        # Restore old order position
        old_leaf = self._old_name.rsplit('/', 1)[-1] if '/' in self._old_name else self._old_name
        _insert_into_order(self._old_order_folder, old_leaf, self._old_order_index)
        if hasattr(ctx, 'current_name') and ctx.current_name == self._new_name:
            ctx.current_name = self._old_name
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=self._old_name)


class MoveFolderCmd(UndoCommand):
    """Undo command for moving a folder to a different parent."""

    def __init__(
        self,
        old_path: str, new_path: str,
        old_parent: str, new_parent: str,
        old_order_position: tuple[str, int],
    ) -> None:
        leaf = old_path.rsplit('/', 1)[-1] if '/' in old_path else old_path
        super().__init__(description=f"Move folder '{leaf}'")
        self._old_path = old_path
        self._new_path = new_path
        self._old_parent = old_parent
        self._new_parent = new_parent
        self._old_order_folder = old_order_position[0]
        self._old_order_index = old_order_position[1]

    def _do_move(self, from_path: str, to_path: str,
                 from_parent: str, ctx: object) -> None:
        src = NOTES_DIR / from_path
        dest = NOTES_DIR / to_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.rename(dest)
        except OSError:
            return
        # Rename metadata keys
        meta = _load_notes_meta()
        updated: dict = {}
        for key, value in meta.items():
            if key == '_order':
                updated[key] = value
                continue
            if key.startswith(from_path + '/') or key == from_path:
                new_key = to_path + key[len(from_path):]
                updated[new_key] = value
            else:
                updated[key] = value
        _save_notes_meta(updated)
        # Remove from old parent order
        leaf = from_path.rsplit('/', 1)[-1] if '/' in from_path else from_path
        _remove_from_order(from_parent, leaf)
        # Rename order keys
        order = _load_order()
        for old_k in [k for k in order if k == from_path or k.startswith(from_path + '/')]:
            order[to_path + old_k[len(from_path):]] = order.pop(old_k)
        _save_order(order)
        # Update current name
        if hasattr(ctx, 'current_name') and ctx.current_name:
            cn = ctx.current_name
            if cn.startswith(from_path + '/'):
                ctx.current_name = to_path + cn[len(from_path):]

    def execute(self, ctx: object) -> None:
        self._do_move(self._old_path, self._new_path, self._old_parent, ctx)

    def undo(self, ctx: object) -> None:
        self._do_move(self._new_path, self._old_path, self._new_parent, ctx)
        # Restore old order position
        leaf = self._old_path.rsplit('/', 1)[-1] if '/' in self._old_path else self._old_path
        _insert_into_order(self._old_order_folder, leaf, self._old_order_index)
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=self._old_path, select_type='folder')


class ReorderCmd(UndoCommand):
    """Undo command for reordering items within a folder."""

    def __init__(
        self, folder: str,
        old_order: list[str], new_order: list[str],
    ) -> None:
        super().__init__(description=f"Reorder in '{folder or 'root'}'")
        self._folder = folder
        self._old_order = old_order
        self._new_order = new_order

    def execute(self, ctx: object) -> None:
        order = _load_order()
        order[self._folder] = list(self._new_order)
        _save_order(order)

    def undo(self, ctx: object) -> None:
        order = _load_order()
        order[self._folder] = list(self._old_order)
        _save_order(order)
        if hasattr(ctx, 'refresh_tree'):
            ctx.refresh_tree()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestMoveNoteCmd tests/test_notes_undo.py::TestMoveFolderCmd tests/test_notes_undo.py::TestReorderCmd -v`
Expected: All pass

- [ ] **Step 5: Wire into _move_note**

In `_move_note()` in `notes_dialog.py` (line 2216), after the successful rename (line 2234), replace lines 2229-2243:

```python
        self._save_current()
        src_folder = note_name.rsplit('/', 1)[0] if '/' in note_name else ''
        leaf_name = note_name.rsplit('/', 1)[-1] if '/' in note_name else note_name
        order = _load_order().get(src_folder, [])
        pos = order.index(leaf_name) if leaf_name in order else len(order)

        from leap.monitor.dialogs.notes_undo import MoveNoteCmd
        cmd = MoveNoteCmd(
            old_name=note_name, new_name=new_name,
            old_folder=src_folder, new_folder=target_folder,
            old_order_position=(src_folder, pos),
        )
        self._undo_stack.push(cmd, self._cmd_ctx)
        self._refresh_tree(select_name=new_name)
        self._on_item_changed(self._tree.currentItem(), None)
        return True
```

- [ ] **Step 6: Wire into _move_folder**

In `_move_folder()` (line 2246), replace lines 2263-2280:

```python
        self._save_current()
        src_parent = folder_path.rsplit('/', 1)[0] if '/' in folder_path else ''
        leaf_name = folder_path.rsplit('/', 1)[-1] if '/' in folder_path else folder_path
        order = _load_order().get(src_parent, [])
        pos = order.index(leaf_name) if leaf_name in order else len(order)

        from leap.monitor.dialogs.notes_undo import MoveFolderCmd
        cmd = MoveFolderCmd(
            old_path=folder_path, new_path=new_path,
            old_parent=src_parent, new_parent=target_folder,
            old_order_position=(src_parent, pos),
        )
        self._undo_stack.push(cmd, self._cmd_ctx)
        self._refresh_tree(select_name=new_path, select_type='folder')
        return True
```

- [ ] **Step 7: Wire into _reorder_in_folder**

In `_reorder_in_folder()` (line 2333), snapshot the old order before reordering. Replace the method body:

```python
    def _reorder_in_folder(self, src_path: str, src_type: str,
                           folder: str, before_path: str) -> None:
        """Reorder an item within its current folder."""
        src_leaf = (src_path.rsplit('/', 1)[-1]
                    if '/' in src_path else src_path)
        before_leaf = (before_path.rsplit('/', 1)[-1]
                       if before_path else '')

        old_order = list(self._effective_order(folder))
        order = list(old_order)
        if src_leaf not in order:
            return
        order.remove(src_leaf)
        if before_leaf and before_leaf in order:
            order.insert(order.index(before_leaf), src_leaf)
        else:
            order.append(src_leaf)

        if order == old_order:
            return

        from leap.monitor.dialogs.notes_undo import ReorderCmd
        cmd = ReorderCmd(folder=folder, old_order=old_order, new_order=order)
        self._undo_stack.push(cmd, self._cmd_ctx)

        select_type = 'folder' if src_type == 'folder' else 'note'
        self._refresh_tree(select_name=src_path, select_type=select_type)
```

- [ ] **Step 8: Run all tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All pass

- [ ] **Step 9: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py src/leap/monitor/dialogs/notes_dialog.py tests/test_notes_undo.py
git commit -m "feat(notes): undo for move note/folder and reorder"
```

---

### Task 11: ModeSwitchCmd

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add command
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — wire into `_on_mode_changed`
- Modify: `tests/test_notes_undo.py` — add tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_notes_undo.py`:

```python
from leap.monitor.dialogs.notes_undo import ModeSwitchCmd


class TestModeSwitchCmd:
    def test_switch_to_checklist_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'Note.txt').write_text('hello world', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'Note': {'mode': 'text'},
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = ModeSwitchCmd(
            note_name='Note', old_mode='text', new_mode='checklist',
            old_content='hello world', new_content='- [ ] hello world',
        )
        cmd.execute(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta['Note']['mode'] == 'checklist'
        assert (notes_dir / 'Note.txt').read_text(encoding='utf-8') == '- [ ] hello world'

        cmd.undo(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta['Note']['mode'] == 'text'
        assert (notes_dir / 'Note.txt').read_text(encoding='utf-8') == 'hello world'

    def test_switch_to_text_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'CL.txt').write_text('- [x] done\n- [ ] todo', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'CL': {'mode': 'checklist'},
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = ModeSwitchCmd(
            note_name='CL', old_mode='checklist', new_mode='text',
            old_content='- [x] done\n- [ ] todo', new_content='done\ntodo',
        )
        cmd.execute(ctx)
        assert (notes_dir / 'CL.txt').read_text(encoding='utf-8') == 'done\ntodo'

        cmd.undo(ctx)
        assert (notes_dir / 'CL.txt').read_text(encoding='utf-8') == '- [x] done\n- [ ] todo'
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta['CL']['mode'] == 'checklist'
```

- [ ] **Step 2: Run tests to verify failure**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestModeSwitchCmd -v`
Expected: FAIL

- [ ] **Step 3: Implement ModeSwitchCmd**

Add to `src/leap/monitor/dialogs/notes_undo.py`:

```python
class ModeSwitchCmd(UndoCommand):
    """Undo command for switching between text and checklist mode."""

    def __init__(
        self,
        note_name: str,
        old_mode: str, new_mode: str,
        old_content: str, new_content: str,
    ) -> None:
        super().__init__(description=f"Switch to {new_mode}")
        self._note_name = note_name
        self._old_mode = old_mode
        self._new_mode = new_mode
        self._old_content = old_content
        self._new_content = new_content

    def _apply(self, mode: str, content: str, ctx: object) -> None:
        # Update mode in metadata
        meta = _load_notes_meta()
        meta.setdefault(self._note_name, {})['mode'] = mode
        _save_notes_meta(meta)
        # Write content to disk
        path = _note_path(self._note_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        # Update editor state via context
        if hasattr(ctx, 'saved_text'):
            ctx.saved_text = content
        if hasattr(ctx, 'load_note_into_editor'):
            ctx.load_note_into_editor(self._note_name, content, mode)
        if hasattr(ctx, 'set_mode_combo'):
            idx = 1 if mode == 'checklist' else 0
            ctx.set_mode_combo(idx)

    def execute(self, ctx: object) -> None:
        self._apply(self._new_mode, self._new_content, ctx)

    def undo(self, ctx: object) -> None:
        self._apply(self._old_mode, self._old_content, ctx)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestModeSwitchCmd -v`
Expected: All pass

- [ ] **Step 5: Wire into _on_mode_changed**

Replace `_on_mode_changed` in `notes_dialog.py` (line 2149):

```python
    def _on_mode_changed(self, index: int) -> None:
        if self._switching_mode or not self._current_name:
            return

        old_mode = 'text' if index == self._MODE_CHECKLIST else 'checklist'
        new_mode = 'checklist' if index == self._MODE_CHECKLIST else 'text'

        if index == self._MODE_CHECKLIST:
            old_content = self._editor.get_note_content()
            self._checklist._pasted_images |= self._editor.take_pasted_images()
            items = _parse_checklist(old_content) if old_content.strip() else []
            new_content = _serialize_checklist(items)
        else:
            items = self._checklist.get_items()
            self._editor._pasted_images |= self._checklist.take_pasted_images()
            old_content = _serialize_checklist(self._checklist.get_items())
            lines = [item['text'] for item in items if item['text']]
            new_content = '\n'.join(lines)

        from leap.monitor.dialogs.notes_undo import ModeSwitchCmd
        cmd = ModeSwitchCmd(
            note_name=self._current_name,
            old_mode=old_mode, new_mode=new_mode,
            old_content=old_content, new_content=new_content,
        )
        # Don't use push() — we need custom UI handling
        self._undo_stack._commands = self._undo_stack._commands[:self._undo_stack._cursor]
        self._undo_stack._commands.append(cmd)
        self._undo_stack._cursor = len(self._undo_stack._commands)
        if len(self._undo_stack._commands) > self._undo_stack._limit:
            excess = len(self._undo_stack._commands) - self._undo_stack._limit
            del self._undo_stack._commands[:excess]
            self._undo_stack._cursor -= excess

        # Apply the mode switch (existing logic)
        if index == self._MODE_CHECKLIST:
            self._checklist.set_items(items)
            self._stack.setCurrentIndex(self._MODE_CHECKLIST)
            _set_note_mode(self._current_name, 'checklist')
            self._save_current()
        else:
            self._editor.set_note_content(new_content)
            self._editor.setEnabled(True)
            self._stack.setCurrentIndex(self._MODE_TEXT)
            _set_note_mode(self._current_name, 'text')
            self._save_current()
        self._update_action_visibility(self._current_name is not None)
```

- [ ] **Step 6: Run all tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py src/leap/monitor/dialogs/notes_dialog.py tests/test_notes_undo.py
git commit -m "feat(notes): undo for mode switch (text/checklist)"
```

---

### Task 12: NoteContentChangeCmd

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add command
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — wire into `_on_item_changed`
- Modify: `tests/test_notes_undo.py` — add tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_notes_undo.py`:

```python
from leap.monitor.dialogs.notes_undo import NoteContentChangeCmd


class TestNoteContentChangeCmd:
    def test_undo_restores_old_text(self, notes_dir: Path) -> None:
        (notes_dir / 'A.txt').write_text('new text', encoding='utf-8')
        ctx = _StubCtx()
        cmd = NoteContentChangeCmd(
            note_name='A', old_text='old text', new_text='new text',
            mode='text',
        )
        cmd.execute(ctx)
        assert (notes_dir / 'A.txt').read_text(encoding='utf-8') == 'new text'

        cmd.undo(ctx)
        assert (notes_dir / 'A.txt').read_text(encoding='utf-8') == 'old text'

    def test_redo_restores_new_text(self, notes_dir: Path) -> None:
        (notes_dir / 'B.txt').write_text('old', encoding='utf-8')
        ctx = _StubCtx()
        stack = NotesUndoStack(limit=50)
        stack.push(NoteContentChangeCmd(
            note_name='B', old_text='old', new_text='new', mode='text',
        ), ctx=ctx)
        stack.undo(ctx=ctx)
        assert (notes_dir / 'B.txt').read_text(encoding='utf-8') == 'old'
        stack.redo(ctx=ctx)
        assert (notes_dir / 'B.txt').read_text(encoding='utf-8') == 'new'
```

- [ ] **Step 2: Run tests to verify failure**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestNoteContentChangeCmd -v`
Expected: FAIL

- [ ] **Step 3: Implement NoteContentChangeCmd**

Add to `src/leap/monitor/dialogs/notes_undo.py`:

```python
class NoteContentChangeCmd(UndoCommand):
    """Undo command for note content changes (pushed on note switch)."""

    def __init__(
        self,
        note_name: str,
        old_text: str, new_text: str,
        mode: str,
    ) -> None:
        leaf = note_name.rsplit('/', 1)[-1] if '/' in note_name else note_name
        super().__init__(description=f"Edit '{leaf}'")
        self._note_name = note_name
        self._old_text = old_text
        self._new_text = new_text
        self._mode = mode

    def _write(self, text: str, ctx: object) -> None:
        path = _note_path(self._note_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        if hasattr(ctx, 'saved_text'):
            ctx.saved_text = text
        if hasattr(ctx, 'load_note_into_editor'):
            ctx.load_note_into_editor(self._note_name, text, self._mode)

    def execute(self, ctx: object) -> None:
        self._write(self._new_text, ctx)

    def undo(self, ctx: object) -> None:
        self._write(self._old_text, ctx)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestNoteContentChangeCmd -v`
Expected: All pass

- [ ] **Step 5: Wire into _on_item_changed**

In `_on_item_changed` (line 2089), we need to push a content change command when switching away from a note that has unsaved changes. Modify the beginning of the method, replacing `self._save_current()` (line 2094) with:

```python
        # Snapshot content change before switching
        if self._current_name:
            try:
                if self._current_mode() == self._MODE_CHECKLIST:
                    live_text = _serialize_checklist(self._checklist.get_items())
                else:
                    live_text = self._editor.get_note_content()
            except RuntimeError:
                live_text = self._saved_text
            if live_text != self._saved_text:
                from leap.monitor.dialogs.notes_undo import NoteContentChangeCmd
                mode = _get_note_mode(self._current_name)
                cmd = NoteContentChangeCmd(
                    note_name=self._current_name,
                    old_text=self._saved_text,
                    new_text=live_text,
                    mode=mode,
                )
                # Record without executing — the text is already in the widget
                self._undo_stack._commands = self._undo_stack._commands[:self._undo_stack._cursor]
                self._undo_stack._commands.append(cmd)
                self._undo_stack._cursor = len(self._undo_stack._commands)
                if len(self._undo_stack._commands) > self._undo_stack._limit:
                    excess = len(self._undo_stack._commands) - self._undo_stack._limit
                    del self._undo_stack._commands[:excess]
                    self._undo_stack._cursor -= excess
        self._save_current()
```

- [ ] **Step 6: Add a push_without_execute method to NotesUndoStack**

The above step directly manipulates stack internals, which is fragile. Add a proper method to `NotesUndoStack`:

```python
    def record(self, cmd: UndoCommand) -> None:
        """Record *cmd* on the stack without calling execute().

        Use when the operation has already been performed and you only
        need undo support.
        """
        del self._commands[self._cursor:]
        self._commands.append(cmd)
        self._cursor = len(self._commands)
        if len(self._commands) > self._limit:
            excess = len(self._commands) - self._limit
            del self._commands[:excess]
            self._cursor -= excess
```

Then replace the direct stack manipulation in `_on_item_changed` and `_on_mode_changed` with `self._undo_stack.record(cmd)`.

- [ ] **Step 7: Run all tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py src/leap/monitor/dialogs/notes_dialog.py tests/test_notes_undo.py
git commit -m "feat(notes): undo for note content changes on switch"
```

---

### Task 13: Checklist undo commands

**Files:**
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — add 3 checklist commands
- Modify: `src/leap/monitor/dialogs/notes_dialog.py` — wire into `_ChecklistWidget` methods
- Modify: `tests/test_notes_undo.py` — add tests

- [ ] **Step 1: Write failing tests**

Add to `tests/test_notes_undo.py`:

```python
from leap.monitor.dialogs.notes_undo import (
    ChecklistDeleteItemCmd, ChecklistReorderCmd, ChecklistToggleCmd,
)


class _StubChecklistCtx(_StubCtx):
    """Stub that also tracks checklist item changes."""

    def __init__(self) -> None:
        super().__init__()
        self._items: list[dict] = []

    def get_checklist_items(self) -> list[dict]:
        return [dict(d) for d in self._items]

    def set_checklist_items(self, items: list[dict]) -> None:
        self._items = [dict(d) for d in items]


class TestChecklistToggleCmd:
    def test_toggle_and_undo(self) -> None:
        ctx = _StubChecklistCtx()
        ctx._items = [
            {'text': 'a', 'checked': False},
            {'text': 'b', 'checked': False},
        ]
        cmd = ChecklistToggleCmd(item_index=0, old_checked=False)
        cmd.execute(ctx)
        assert ctx._items[0]['checked'] is True
        cmd.undo(ctx)
        assert ctx._items[0]['checked'] is False


class TestChecklistDeleteItemCmd:
    def test_delete_and_undo(self) -> None:
        ctx = _StubChecklistCtx()
        ctx._items = [
            {'text': 'a', 'checked': False},
            {'text': 'b', 'checked': True},
            {'text': 'c', 'checked': False},
        ]
        cmd = ChecklistDeleteItemCmd(
            item_index=1, item_text='b', item_checked=True)
        cmd.execute(ctx)
        assert len(ctx._items) == 2
        assert ctx._items[0]['text'] == 'a'
        assert ctx._items[1]['text'] == 'c'
        cmd.undo(ctx)
        assert len(ctx._items) == 3
        assert ctx._items[1] == {'text': 'b', 'checked': True}


class TestChecklistReorderCmd:
    def test_reorder_and_undo(self) -> None:
        ctx = _StubChecklistCtx()
        ctx._items = [
            {'text': 'a', 'checked': False},
            {'text': 'b', 'checked': False},
            {'text': 'c', 'checked': False},
        ]
        # Move item 2 to position 0
        cmd = ChecklistReorderCmd(src_index=2, dst_index=0)
        cmd.execute(ctx)
        assert [d['text'] for d in ctx._items] == ['c', 'a', 'b']
        cmd.undo(ctx)
        assert [d['text'] for d in ctx._items] == ['a', 'b', 'c']
```

- [ ] **Step 2: Run tests to verify failure**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestChecklistToggleCmd tests/test_notes_undo.py::TestChecklistDeleteItemCmd tests/test_notes_undo.py::TestChecklistReorderCmd -v`
Expected: FAIL

- [ ] **Step 3: Implement checklist commands**

Add to `src/leap/monitor/dialogs/notes_undo.py`:

```python
class ChecklistToggleCmd(UndoCommand):
    """Undo command for toggling a checklist item's checked state."""

    def __init__(self, item_index: int, old_checked: bool) -> None:
        super().__init__(description='Toggle checklist item')
        self._index = item_index
        self._old_checked = old_checked

    def execute(self, ctx: object) -> None:
        if hasattr(ctx, 'get_checklist_items'):
            items = ctx.get_checklist_items()
            if 0 <= self._index < len(items):
                items[self._index]['checked'] = not self._old_checked
                ctx.set_checklist_items(items)

    def undo(self, ctx: object) -> None:
        if hasattr(ctx, 'get_checklist_items'):
            items = ctx.get_checklist_items()
            if 0 <= self._index < len(items):
                items[self._index]['checked'] = self._old_checked
                ctx.set_checklist_items(items)


class ChecklistDeleteItemCmd(UndoCommand):
    """Undo command for deleting a checklist item."""

    def __init__(self, item_index: int, item_text: str, item_checked: bool) -> None:
        super().__init__(description='Delete checklist item')
        self._index = item_index
        self._text = item_text
        self._checked = item_checked

    def execute(self, ctx: object) -> None:
        if hasattr(ctx, 'get_checklist_items'):
            items = ctx.get_checklist_items()
            if 0 <= self._index < len(items):
                del items[self._index]
                ctx.set_checklist_items(items)

    def undo(self, ctx: object) -> None:
        if hasattr(ctx, 'get_checklist_items'):
            items = ctx.get_checklist_items()
            items.insert(self._index, {
                'text': self._text, 'checked': self._checked,
            })
            ctx.set_checklist_items(items)


class ChecklistReorderCmd(UndoCommand):
    """Undo command for reordering a checklist item."""

    def __init__(self, src_index: int, dst_index: int) -> None:
        super().__init__(description='Reorder checklist item')
        self._src = src_index
        self._dst = dst_index

    def _move(self, src: int, dst: int, ctx: object) -> None:
        if hasattr(ctx, 'get_checklist_items'):
            items = ctx.get_checklist_items()
            if 0 <= src < len(items):
                item = items.pop(src)
                effective_dst = dst if dst <= src else dst - 1
                items.insert(effective_dst, item)
                ctx.set_checklist_items(items)

    def execute(self, ctx: object) -> None:
        self._move(self._src, self._dst, ctx)

    def undo(self, ctx: object) -> None:
        # Reverse: figure out where the item ended up and move it back
        effective_dst = self._dst if self._dst <= self._src else self._dst - 1
        # Now move from effective_dst back to src
        if hasattr(ctx, 'get_checklist_items'):
            items = ctx.get_checklist_items()
            if 0 <= effective_dst < len(items):
                item = items.pop(effective_dst)
                items.insert(self._src, item)
                ctx.set_checklist_items(items)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestChecklistToggleCmd tests/test_notes_undo.py::TestChecklistDeleteItemCmd tests/test_notes_undo.py::TestChecklistReorderCmd -v`
Expected: All pass

- [ ] **Step 5: Wire checklist commands into _ChecklistWidget**

The `_ChecklistWidget` needs access to the undo stack. Add a method and wire it from `NotesDialog.__init__`.

In `_ChecklistWidget` (around line 996), add after `__init__`:

```python
    def set_undo_stack(self, stack: 'NotesUndoStack',
                       ctx: 'NotesCmdContext') -> None:
        """Set the undo stack and context for checklist undo commands."""
        self._undo_stack = stack
        self._cmd_ctx = ctx
```

In `NotesDialog.__init__`, after `self._checklist = _ChecklistWidget()` (line 1873), add:

```python
        self._checklist.set_undo_stack(self._undo_stack, self._cmd_ctx)
```

Now modify the checklist mutation methods:

**`_on_toggle`** (line 1343):

```python
    def _on_toggle(self, index: int, checked: bool) -> None:
        if index < 0 or index >= len(self._items):
            return
        old_checked = self._items[index]['checked']
        self._items[index]['checked'] = checked
        self._rebuild()
        self.content_changed.emit()
        if hasattr(self, '_undo_stack'):
            from leap.monitor.dialogs.notes_undo import ChecklistToggleCmd
            self._undo_stack.record(ChecklistToggleCmd(
                item_index=index, old_checked=old_checked))
```

**`_on_delete`** (line 1356):

```python
    def _on_delete(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            return
        item = self._items[index]
        del self._items[index]
        self._rebuild()
        self.content_changed.emit()
        if hasattr(self, '_undo_stack'):
            from leap.monitor.dialogs.notes_undo import ChecklistDeleteItemCmd
            self._undo_stack.record(ChecklistDeleteItemCmd(
                item_index=index, item_text=item['text'],
                item_checked=item['checked']))
```

**`_move_item`** (line 1331):

```python
    def _move_item(self, src: int, dst: int) -> None:
        """Move an item from src index to before dst index in self._items."""
        if hasattr(self, '_undo_stack'):
            from leap.monitor.dialogs.notes_undo import ChecklistReorderCmd
            self._undo_stack.record(ChecklistReorderCmd(
                src_index=src, dst_index=dst))
        item = self._items.pop(src)
        if dst > src:
            dst -= 1
        self._items.insert(dst, item)
        self._rebuild()
        self.content_changed.emit()
```

- [ ] **Step 6: Update NotesCmdContext to support checklist operations for undo**

The checklist commands' `execute()`/`undo()` use `ctx.get_checklist_items()` and `ctx.set_checklist_items()`. These already exist in `NotesCmdContext`. However, for checklist undo, the commands need to operate on the widget's `_items` directly. Update the context methods:

`get_checklist_items` should return the raw internal items (with placeholders), and `set_checklist_items` should set them and rebuild. Update in `NotesCmdContext`:

```python
    def get_checklist_items(self) -> list[dict]:
        """Get current checklist items (raw internal format)."""
        return [dict(d) for d in self._d._checklist._items]

    def set_checklist_items(self, items: list[dict]) -> None:
        """Set checklist items (raw internal format) and rebuild."""
        self._d._checklist._items = [dict(d) for d in items]
        self._d._checklist._rebuild()
        self._d._checklist.content_changed.emit()
```

- [ ] **Step 7: Run all tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add src/leap/monitor/dialogs/notes_undo.py src/leap/monitor/dialogs/notes_dialog.py tests/test_notes_undo.py
git commit -m "feat(notes): undo for checklist toggle, delete item, reorder"
```

---

### Task 14: Deferred image cleanup tests

**Files:**
- Modify: `tests/test_notes_undo.py` — add image lifecycle tests

- [ ] **Step 1: Write tests**

Add to `tests/test_notes_undo.py`:

```python
class TestDeferredImageCleanup:
    def test_deferred_images_not_deleted_during_session(self, notes_dir: Path) -> None:
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'aaa.png').write_bytes(b'image1')
        (notes_dir / 'PicNote.txt').write_text('![image](aaa.png)', encoding='utf-8')

        ctx = _StubCtx()
        cmd = DeleteNoteCmd(
            name='PicNote', content='![image](aaa.png)',
            metadata={}, order_position=('', 0),
            image_refs={'aaa.png'},
        )
        cmd.execute(ctx)
        # File still exists — only deferred
        assert (img_dir / 'aaa.png').exists()
        assert 'aaa.png' in ctx.pending_image_deletes

    def test_undo_removes_from_deferred(self, notes_dir: Path) -> None:
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'bbb.png').write_bytes(b'image2')
        (notes_dir / 'PicNote.txt').write_text('![image](bbb.png)', encoding='utf-8')

        ctx = _StubCtx()
        cmd = DeleteNoteCmd(
            name='PicNote', content='![image](bbb.png)',
            metadata={}, order_position=('', 0),
            image_refs={'bbb.png'},
        )
        cmd.execute(ctx)
        assert 'bbb.png' in ctx.pending_image_deletes

        cmd.undo(ctx)
        assert 'bbb.png' not in ctx.pending_image_deletes
        # Image file should still exist
        assert (img_dir / 'bbb.png').exists()

    def test_image_shared_across_notes_survives_delete(
        self, notes_dir: Path,
    ) -> None:
        """If two notes reference the same image and one is deleted,
        the image should remain (not even be deferred)."""
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'shared.png').write_bytes(b'shared')
        (notes_dir / 'A.txt').write_text('![image](shared.png)', encoding='utf-8')
        (notes_dir / 'B.txt').write_text('![image](shared.png)', encoding='utf-8')

        ctx = _StubCtx()
        # Delete note A — shared.png is still used by B
        cmd = DeleteNoteCmd(
            name='A', content='![image](shared.png)',
            metadata={}, order_position=('', 0),
            image_refs={'shared.png'},
        )
        cmd.execute(ctx)
        # shared.png is in pending_image_deletes but would be saved by
        # the finalize safety check (which cross-refs all notes on disk)
        assert (img_dir / 'shared.png').exists()
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/test_notes_undo.py::TestDeferredImageCleanup -v`
Expected: All pass (these test the already-implemented behavior)

- [ ] **Step 3: Commit**

```bash
git add tests/test_notes_undo.py
git commit -m "test(notes): add deferred image cleanup tests"
```

---

### Task 15: Add record() test and final cleanup

**Files:**
- Modify: `tests/test_notes_undo.py` — add record() test
- Modify: `src/leap/monitor/dialogs/notes_undo.py` — final exports check

- [ ] **Step 1: Add test for record()**

Add to `TestUndoStack` in `tests/test_notes_undo.py`:

```python
    def test_record_does_not_execute(self) -> None:
        log: list[str] = []
        stack = NotesUndoStack(limit=50)
        stack.record(_FakeCmd('a', log))
        assert log == []  # not executed
        assert stack.can_undo()
        stack.undo(ctx=None)
        assert log == ['undo:a']
```

- [ ] **Step 2: Run all tests**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All pass

- [ ] **Step 3: Verify all exports from notes_undo.py**

Ensure the `__all__` or top-level exports include every command class. No `__all__` is needed since the module uses explicit imports, but verify all classes are importable.

- [ ] **Step 4: Commit**

```bash
git add tests/test_notes_undo.py src/leap/monitor/dialogs/notes_undo.py
git commit -m "test(notes): add record() test, verify final exports"
```

---

### Task 16: Final integration verification

- [ ] **Step 1: Run the full test suite**

Run: `cd /Users/Nevo.Mashiach/workspace/leap && poetry run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: Manual smoke test checklist**

Verify these scenarios work end-to-end by running the monitor:

1. Create a note → Cmd+Z → note disappears
2. Delete a note → Cmd+Z → note is back with content
3. Rename a note → Cmd+Z → old name restored
4. Drag-reorder in tree → Cmd+Z → original order
5. Switch text→checklist → Cmd+Z → back to text with original content
6. Delete checklist item → Cmd+Z → item reappears
7. Toggle checklist checkbox → Cmd+Z → unchecked again
8. Close and reopen notes → undo stack is empty
9. Delete note with pasted image → Cmd+Z → note and image restored
10. Cmd+Z inside text editor → Qt undo (not structural undo)
11. Cmd+Shift+Z → redo works

- [ ] **Step 3: Commit any fixes from smoke testing**

```bash
git add -A
git commit -m "fix(notes): integration fixes from smoke testing"
```
