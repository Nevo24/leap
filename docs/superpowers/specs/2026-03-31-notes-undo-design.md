# Notes Dialog Undo/Redo (Cmd+Z / Cmd+Shift+Z)

## Summary

Add undo/redo support to the Notes dialog so users can revert accidental operations (delete, rename, move, reorder, mode switch, checklist mutations). Closing the dialog commits all changes permanently — no undo across sessions.

## Architecture

### New file: `src/leap/monitor/dialogs/notes_undo.py`

Contains all undo infrastructure. The existing `notes_dialog.py` imports from it and wires commands into its existing methods.

### UndoStack

```
class NotesUndoStack:
    _commands: list[UndoCommand]
    _cursor: int          # points past the last executed command
    _limit: int = 50

    push(cmd)             # execute + append; truncates redo tail
    undo()                # call _commands[--cursor].undo()
    redo()                # call _commands[cursor++].execute()
    can_undo() -> bool
    can_redo() -> bool
    clear()               # discard everything
```

- Pushing after an undo truncates the redo tail (standard behavior).
- Cap at 50: when exceeded, the oldest command is dropped.

### UndoCommand (abstract base)

```
class UndoCommand(ABC):
    description: str      # e.g. "Delete note 'TODO'"
    
    @abstractmethod
    def execute(self, ctx: NotesCmdContext) -> None: ...
    
    @abstractmethod
    def undo(self, ctx: NotesCmdContext) -> None: ...
```

### NotesCmdContext

Thin interface passed to commands — decouples them from the full dialog API:

```
class NotesCmdContext:
    # State access
    current_name: Optional[str]      # get/set
    saved_text: str                   # get/set
    pending_image_deletes: set[str]   # direct reference
    
    # UI refresh
    refresh_tree(select_name, select_type) -> None
    trigger_item_changed() -> None
    set_mode_combo(index: int) -> None
    rebuild_checklist(items: list[dict]) -> None
    set_editor_content(text: str) -> None
```

Created once in `NotesDialog.__init__`, passed to all commands.

## Command Classes

### Structural (tree) operations

#### CreateNoteCmd
- **State**: `name: str`, `folder: str`
- **execute()**: Create file + metadata + order entry (logic extracted from `_on_new`)
- **undo()**: Delete file, remove metadata, remove from order

#### CreateFolderCmd
- **State**: `folder_path: str`
- **execute()**: Create directory + order entry (logic extracted from `_on_new_folder`)
- **undo()**: Remove directory, remove from order

#### DeleteNoteCmd
- **State**: `name: str`, `content: str`, `metadata: dict`, `order_position: (folder, index)`, `image_refs: set[str]`
- **execute()**: Delete file, metadata, order entry. Add image refs to `pending_image_deletes`.
- **undo()**: Restore file with content, restore metadata, reinsert into order at saved position. Remove image refs from `pending_image_deletes`.

#### DeleteFolderCmd
- **State**: `folder_path: str`, `notes: dict[str, str]` (name -> content), `metadata_entries: dict`, `order_entries: dict[str, list[str]]`, `image_refs: set[str]`, `subfolder_paths: list[str]`
- **execute()**: Delete folder tree, all metadata, all order entries. Add image refs to `pending_image_deletes`.
- **undo()**: Recreate folder structure, restore all note files, metadata, order entries. Remove image refs from `pending_image_deletes`.

#### RenameNoteCmd
- **State**: `old_name: str`, `new_name: str`, `parent_folder: str`, `old_leaf: str`, `new_leaf: str`
- **execute()**: Rename file + metadata + order entry
- **undo()**: Reverse rename — same operations with old/new swapped

#### RenameFolderCmd
- **State**: `old_path: str`, `new_path: str`, `parent_folder: str`, `old_leaf: str`, `new_leaf: str`, `old_order_keys: dict[str, list[str]]`
- **execute()**: Rename directory + metadata + order keys
- **undo()**: Reverse rename, restore old order keys

#### MoveNoteCmd
- **State**: `note_name: str`, `old_folder: str`, `new_folder: str`, `old_order_position: (folder, index)`, `new_name: str`
- **execute()**: Move file, update metadata, update order
- **undo()**: Move back, restore old order position

#### MoveFolderCmd
- **State**: `folder_path: str`, `old_parent: str`, `new_parent: str`, `old_order_position: (folder, index)`, `new_path: str`, `old_order_keys: dict`
- **execute()**: Move directory, update metadata, update order keys
- **undo()**: Move back, restore old order keys and position

#### ReorderCmd
- **State**: `folder: str`, `old_order: list[str]`, `new_order: list[str]`
- **execute()**: Save new order
- **undo()**: Save old order

### Editor operations

#### ModeSwitchCmd
- **State**: `note_name: str`, `old_mode: int`, `new_mode: int`, `old_content: str`
- **execute()**: Switch mode, persist metadata
- **undo()**: Restore old mode + content, persist metadata

#### NoteContentChangeCmd
- **State**: `note_name: str`, `old_text: str`, `new_text: str`
- **execute()**: Write new_text to file, update `_saved_text`
- **undo()**: Write old_text to file, update `_saved_text`, reload editor

Pushed when the user **switches away from a note** that has unsaved text changes — not on every keystroke.

#### ChecklistToggleCmd
- **State**: `note_name: str`, `item_index: int`, `old_checked: bool`
- **execute()**: Toggle checked state, rebuild
- **undo()**: Restore old checked state, rebuild

#### ChecklistDeleteItemCmd
- **State**: `note_name: str`, `item_index: int`, `item_text: str`, `item_checked: bool`
- **execute()**: Remove item, rebuild
- **undo()**: Reinsert item at index, rebuild

#### ChecklistReorderCmd
- **State**: `note_name: str`, `src_index: int`, `dst_index: int`
- **execute()**: Move item from src to dst
- **undo()**: Move item from dst back to src

## Deferred Image Deletion

### Problem
Currently `_cleanup_orphaned_images()` deletes image files immediately. Undo of a note delete needs those images to still exist.

### Solution
- New dialog-level `_pending_image_deletes: set[str]`
- During session: `_cleanup_orphaned_images()` gains a `deferred: bool` parameter. When True, adds filenames to `_pending_image_deletes` instead of unlinking.
- Undo commands remove entries from `_pending_image_deletes` when restoring notes.
- On close: `_finalize_image_cleanup()` does a final safety check (scan all notes on disk), then unlinks images that are still in `_pending_image_deletes` and truly orphaned.
- This runs synchronously in `done()`/`closeEvent()` before `super()`, so reopening the dialog sees clean disk state.

## Cmd+Z / Cmd+Shift+Z Routing

In `NotesDialog.keyPressEvent`:

```python
if mods & Qt.ControlModifier and event.key() == Qt.Key_Z:
    focus = QApplication.focusWidget()
    if isinstance(focus, (_NoteTextEdit, _ItemLineEdit, QTextEdit)):
        # Let Qt's built-in text undo handle it
        super().keyPressEvent(event)
        return
    if mods & Qt.ShiftModifier:
        self._undo_stack.redo()
    else:
        self._undo_stack.undo()
    return
```

- Text editor / checklist line edit focused: Qt built-in undo (per-character)
- Tree or dialog focused: our structural undo stack

## Integration Points in NotesDialog

### New state in `__init__`:
- `self._undo_stack = NotesUndoStack(limit=50)`
- `self._pending_image_deletes: set[str] = set()`
- `self._cmd_ctx = NotesCmdContext(self)`

### Modified methods:
Each existing mutation method creates the appropriate command, snapshots state before the operation, and pushes the command to the stack.

- `_on_new()` → push `CreateNoteCmd`
- `_on_new_folder()` → push `CreateFolderCmd`
- `_on_delete()` → push `DeleteNoteCmd` / `DeleteFolderCmd` (keep confirmation dialog)
- `_on_rename()` → push `RenameNoteCmd` / `RenameFolderCmd`
- `_move_note()` → push `MoveNoteCmd`
- `_move_folder()` → push `MoveFolderCmd`
- `_reorder_in_folder()` → push `ReorderCmd`
- `_on_mode_changed()` → push `ModeSwitchCmd`
- `_on_item_changed()` → push `NoteContentChangeCmd` when switching away with unsaved changes
- `_ChecklistWidget._on_toggle()` → push `ChecklistToggleCmd`
- `_ChecklistWidget._on_delete()` → push `ChecklistDeleteItemCmd`
- `_ChecklistWidget._move_item()` → push `ChecklistReorderCmd`

### Modified close path:
`done()` and `closeEvent()` — after `_save_current()`, call `_finalize_image_cleanup()` then `_undo_stack.clear()`.

### Checklist widget access to undo stack:
`_ChecklistWidget` needs to push commands. Pass `_undo_stack` and `_cmd_ctx` references to it (set via a method after construction, or passed to `set_items()`). Commands will save/rebuild checklist items via the context.

## Testing

All tests in `tests/test_notes_undo.py`. Tests use `tmp_path` with monkeypatched `NOTES_DIR` and `NOTE_IMAGES_DIR`.

### Stack mechanics:
- `test_undo_stack_push_undo_redo` — basic push/undo/redo cursor behavior
- `test_undo_stack_cap_at_50` — push 55, verify only last 50 remain
- `test_undo_stack_truncates_redo_on_push` — undo 2, push new, redo tail gone
- `test_undo_stack_clear` — can_undo and can_redo both False after clear

### Command tests (each verifies disk state + metadata + order):
- `test_delete_note_undo` — delete, verify gone, undo, verify restored
- `test_delete_folder_undo` — delete subtree, undo, verify all restored
- `test_rename_note_undo` — rename, undo, old name back
- `test_rename_folder_undo` — rename with child paths updated, undo
- `test_move_note_undo` — move to subfolder, undo, back at original position
- `test_move_folder_undo` — same with folder
- `test_reorder_undo` — reorder, undo, previous order restored
- `test_create_note_undo` — create, undo, file gone
- `test_create_folder_undo` — create, undo, dir gone
- `test_mode_switch_undo` — switch text<->checklist, undo, original mode+content
- `test_checklist_toggle_undo` — toggle, undo, previous state
- `test_checklist_delete_item_undo` — delete item, undo, restored at index
- `test_checklist_reorder_undo` — move item, undo, back at original index
- `test_content_change_undo` — simulate note switch, undo, old text restored

### Image lifecycle:
- `test_deferred_image_cleanup` — delete note with images, images survive, finalize deletes them
- `test_deferred_image_cleanup_undo` — delete note, undo, finalize, images survive

### Testing approach:
- Commands tested against real filesystem via `tmp_path`
- `NotesCmdContext` stubbed for UI methods (refresh_tree, etc.)
- Stack is pure logic, no mocking needed
- No GUI tests

## Scope exclusions
- No undo across sessions (close + reopen = fresh start)
- No undo of "Run in session" or "Save as preset" (external side effects)
- In-editor text undo uses Qt's built-in mechanism, not our stack
