"""Undo/redo system for the Notes dialog.

Provides a command-pattern undo stack and concrete command classes for
all structural operations (create, delete, rename, move, reorder,
mode switch, checklist mutations).
"""

from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from leap.utils.constants import NOTE_IMAGES_DIR, NOTES_DIR

if TYPE_CHECKING:
    from leap.monitor.dialogs.notes_dialog import NotesDialog

_NOTES_META_FILE: Path = NOTES_DIR / '.notes_meta.json'


class NotesCmdContext:
    """Thin interface between undo commands and the NotesDialog."""

    def __init__(self, dialog: NotesDialog) -> None:
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
        kwargs: dict = {}
        if select_name is not None:
            kwargs['select_name'] = select_name
        if select_type is not None:
            kwargs['select_type'] = select_type
        self._d._refresh_tree(**kwargs)

    def trigger_item_changed(self) -> None:
        self._d._on_item_changed(self._d._tree.currentItem(), None)

    def select_and_load(self, name: Optional[str] = None,
                        select_type: str = 'note') -> None:
        if name is not None:
            self.refresh_tree(select_name=name, select_type=select_type)
        else:
            self.refresh_tree()
        self.trigger_item_changed()

    def select_first_or_none(self) -> None:
        first = self._d._find_first_note(self._d._tree.invisibleRootItem())
        if first:
            self._d._tree.setCurrentItem(first)
        else:
            self._d._on_item_changed(None, None)

    def set_mode_combo(self, index: int) -> None:
        self._d._switching_mode = True
        self._d._mode_combo.setCurrentIndex(index)
        self._d._switching_mode = False

    def load_note_into_editor(self, name: str, text: str, mode: str) -> None:
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
        return [dict(d) for d in self._d._checklist._items]

    def set_checklist_items(self, items: list[dict]) -> None:
        self._d._checklist._items = [dict(d) for d in items]
        self._d._checklist._rebuild()
        self._d._checklist.content_changed.emit()

    def get_editor_content(self) -> str:
        return self._d._editor.get_note_content()

    def set_editor_content(self, text: str) -> None:
        self._d._editor.set_note_content(text)


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
        self._cursor: int = 0
        self._limit = limit

    def push(self, cmd: UndoCommand, ctx: object) -> None:
        """Execute *cmd* and push it onto the stack."""
        del self._commands[self._cursor:]
        cmd.execute(ctx)
        self._commands.append(cmd)
        self._cursor = len(self._commands)
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


# ---------------------------------------------------------------------------
# Helper functions (mirror notes_dialog.py equivalents for command use)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Concrete command classes
# ---------------------------------------------------------------------------

class CreateNoteCmd(UndoCommand):
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
        try:
            _note_path(self._name).unlink(missing_ok=True)
        except OSError:
            pass
        leaf = self._name.rsplit('/', 1)[-1] if '/' in self._name else self._name
        _remove_from_order(self._folder, leaf)
        meta = _load_notes_meta()
        if meta.pop(self._name, None) is not None:
            _save_notes_meta(meta)
        if hasattr(ctx, 'select_first_or_none'):
            ctx.select_first_or_none()


class CreateFolderCmd(UndoCommand):
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


class DeleteNoteCmd(UndoCommand):
    def __init__(self, name: str, content: str, metadata: dict,
                 order_position: tuple[str, int], image_refs: set[str]) -> None:
        leaf = name.rsplit('/', 1)[-1] if '/' in name else name
        super().__init__(description=f"Delete note '{leaf}'")
        self._name = name
        self._content = content
        self._metadata = metadata
        self._order_folder = order_position[0]
        self._order_index = order_position[1]
        self._image_refs = image_refs

    def execute(self, ctx: object) -> None:
        try:
            _note_path(self._name).unlink(missing_ok=True)
        except OSError:
            pass
        meta = _load_notes_meta()
        meta.pop(self._name, None)
        _save_notes_meta(meta)
        leaf = self._name.rsplit('/', 1)[-1] if '/' in self._name else self._name
        _remove_from_order(self._order_folder, leaf)
        if hasattr(ctx, 'pending_image_deletes'):
            ctx.pending_image_deletes |= self._image_refs

    def undo(self, ctx: object) -> None:
        path = _note_path(self._name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._content, encoding='utf-8')
        if self._metadata:
            meta = _load_notes_meta()
            meta[self._name] = dict(self._metadata)
            _save_notes_meta(meta)
        leaf = self._name.rsplit('/', 1)[-1] if '/' in self._name else self._name
        _insert_into_order(self._order_folder, leaf, self._order_index)
        if hasattr(ctx, 'pending_image_deletes'):
            ctx.pending_image_deletes -= self._image_refs
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=self._name)


class DeleteFolderCmd(UndoCommand):
    def __init__(self, folder_path: str, notes: dict[str, str],
                 metadata_entries: dict[str, dict], order_entries: dict[str, list[str]],
                 subfolder_paths: list[str], parent_order_position: tuple[str, int],
                 image_refs: set[str]) -> None:
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
        for name in self._notes:
            try:
                _note_path(name).unlink(missing_ok=True)
            except OSError:
                pass
        meta = _load_notes_meta()
        for name in self._notes:
            meta.pop(name, None)
        _save_notes_meta(meta)
        order = _load_order()
        for k in list(order):
            if k == self._folder_path or k.startswith(self._folder_path + '/'):
                del order[k]
        _save_order(order)
        leaf = self._folder_path.rsplit('/', 1)[-1] if '/' in self._folder_path else self._folder_path
        _remove_from_order(self._parent_folder, leaf)
        target = NOTES_DIR / self._folder_path
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        if hasattr(ctx, 'pending_image_deletes'):
            ctx.pending_image_deletes |= self._image_refs

    def undo(self, ctx: object) -> None:
        (NOTES_DIR / self._folder_path).mkdir(parents=True, exist_ok=True)
        for sf in self._subfolder_paths:
            (NOTES_DIR / sf).mkdir(parents=True, exist_ok=True)
        for name, content in self._notes.items():
            path = _note_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        if self._metadata_entries:
            meta = _load_notes_meta()
            for name, entry in self._metadata_entries.items():
                meta[name] = dict(entry)
            _save_notes_meta(meta)
        if self._order_entries:
            order = _load_order()
            for k, v in self._order_entries.items():
                order[k] = list(v)
            _save_order(order)
        leaf = self._folder_path.rsplit('/', 1)[-1] if '/' in self._folder_path else self._folder_path
        _insert_into_order(self._parent_folder, leaf, self._parent_index)
        if hasattr(ctx, 'pending_image_deletes'):
            ctx.pending_image_deletes -= self._image_refs
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=self._folder_path, select_type='folder')


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


class RenameNoteCmd(UndoCommand):
    def __init__(self, old_name: str, new_name: str, parent_folder: str,
                 old_leaf: str, new_leaf: str) -> None:
        super().__init__(description=f"Rename note '{old_leaf}' to '{new_leaf}'")
        self._old_name = old_name
        self._new_name = new_name
        self._parent_folder = parent_folder
        self._old_leaf = old_leaf
        self._new_leaf = new_leaf

    def _do_rename(self, from_name: str, to_name: str, from_leaf: str, to_leaf: str, ctx: object) -> None:
        try:
            _note_path(from_name).rename(_note_path(to_name))
        except OSError:
            return
        meta = _load_notes_meta()
        if from_name in meta:
            meta[to_name] = meta.pop(from_name)
            _save_notes_meta(meta)
        order = _load_order()
        lst = order.get(self._parent_folder, [])
        if from_leaf in lst:
            lst[lst.index(from_leaf)] = to_leaf
            order[self._parent_folder] = lst
            _save_order(order)
        if hasattr(ctx, 'current_name') and ctx.current_name == from_name:
            ctx.current_name = to_name
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=to_name)

    def execute(self, ctx: object) -> None:
        self._do_rename(self._old_name, self._new_name, self._old_leaf, self._new_leaf, ctx)
    def undo(self, ctx: object) -> None:
        self._do_rename(self._new_name, self._old_name, self._new_leaf, self._old_leaf, ctx)


class RenameFolderCmd(UndoCommand):
    def __init__(self, old_path: str, new_path: str, parent_folder: str,
                 old_leaf: str, new_leaf: str) -> None:
        super().__init__(description=f"Rename folder '{old_leaf}' to '{new_leaf}'")
        self._old_path = old_path
        self._new_path = new_path
        self._parent_folder = parent_folder
        self._old_leaf = old_leaf
        self._new_leaf = new_leaf

    def _do_rename(self, from_path: str, to_path: str, from_leaf: str, to_leaf: str, ctx: object) -> None:
        try:
            (NOTES_DIR / from_path).rename(NOTES_DIR / to_path)
        except OSError:
            return
        meta = _load_notes_meta()
        updated: dict = {}
        for key, value in meta.items():
            if key == '_order':
                updated[key] = value
                continue
            if key.startswith(from_path + '/') or key == from_path:
                updated[to_path + key[len(from_path):]] = value
            else:
                updated[key] = value
        _save_notes_meta(updated)
        order = _load_order()
        lst = order.get(self._parent_folder, [])
        if from_leaf in lst:
            lst[lst.index(from_leaf)] = to_leaf
            order[self._parent_folder] = lst
        for old_k in [k for k in order if k == from_path or k.startswith(from_path + '/')]:
            order[to_path + old_k[len(from_path):]] = order.pop(old_k)
        _save_order(order)
        if hasattr(ctx, 'current_name') and ctx.current_name:
            cn = ctx.current_name
            if cn.startswith(from_path + '/'):
                ctx.current_name = to_path + cn[len(from_path):]
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=to_path, select_type='folder')

    def execute(self, ctx: object) -> None:
        self._do_rename(self._old_path, self._new_path, self._old_leaf, self._new_leaf, ctx)
    def undo(self, ctx: object) -> None:
        self._do_rename(self._new_path, self._old_path, self._new_leaf, self._old_leaf, ctx)


class MoveNoteCmd(UndoCommand):
    def __init__(self, old_name: str, new_name: str, old_folder: str, new_folder: str,
                 old_order_position: tuple[str, int]) -> None:
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
        meta = _load_notes_meta()
        if self._old_name in meta:
            meta[self._new_name] = meta.pop(self._old_name)
            _save_notes_meta(meta)
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
        meta = _load_notes_meta()
        if self._new_name in meta:
            meta[self._old_name] = meta.pop(self._new_name)
            _save_notes_meta(meta)
        leaf = self._new_name.rsplit('/', 1)[-1] if '/' in self._new_name else self._new_name
        _remove_from_order(self._new_folder, leaf)
        old_leaf = self._old_name.rsplit('/', 1)[-1] if '/' in self._old_name else self._old_name
        _insert_into_order(self._old_order_folder, old_leaf, self._old_order_index)
        if hasattr(ctx, 'current_name') and ctx.current_name == self._new_name:
            ctx.current_name = self._old_name
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=self._old_name)


class MoveFolderCmd(UndoCommand):
    def __init__(self, old_path: str, new_path: str, old_parent: str, new_parent: str,
                 old_order_position: tuple[str, int]) -> None:
        leaf = old_path.rsplit('/', 1)[-1] if '/' in old_path else old_path
        super().__init__(description=f"Move folder '{leaf}'")
        self._old_path = old_path
        self._new_path = new_path
        self._old_parent = old_parent
        self._new_parent = new_parent
        self._old_order_folder = old_order_position[0]
        self._old_order_index = old_order_position[1]

    def _do_move(self, from_path: str, to_path: str, from_parent: str, ctx: object) -> None:
        src = NOTES_DIR / from_path
        dest = NOTES_DIR / to_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.rename(dest)
        except OSError:
            return
        meta = _load_notes_meta()
        updated: dict = {}
        for key, value in meta.items():
            if key == '_order':
                updated[key] = value
                continue
            if key.startswith(from_path + '/') or key == from_path:
                updated[to_path + key[len(from_path):]] = value
            else:
                updated[key] = value
        _save_notes_meta(updated)
        leaf = from_path.rsplit('/', 1)[-1] if '/' in from_path else from_path
        _remove_from_order(from_parent, leaf)
        order = _load_order()
        for old_k in [k for k in order if k == from_path or k.startswith(from_path + '/')]:
            order[to_path + old_k[len(from_path):]] = order.pop(old_k)
        _save_order(order)
        if hasattr(ctx, 'current_name') and ctx.current_name:
            cn = ctx.current_name
            if cn.startswith(from_path + '/'):
                ctx.current_name = to_path + cn[len(from_path):]

    def execute(self, ctx: object) -> None:
        self._do_move(self._old_path, self._new_path, self._old_parent, ctx)
    def undo(self, ctx: object) -> None:
        self._do_move(self._new_path, self._old_path, self._new_parent, ctx)
        leaf = self._old_path.rsplit('/', 1)[-1] if '/' in self._old_path else self._old_path
        _insert_into_order(self._old_order_folder, leaf, self._old_order_index)
        if hasattr(ctx, 'select_and_load'):
            ctx.select_and_load(name=self._old_path, select_type='folder')


class ReorderCmd(UndoCommand):
    def __init__(self, folder: str, old_order: list[str], new_order: list[str]) -> None:
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
