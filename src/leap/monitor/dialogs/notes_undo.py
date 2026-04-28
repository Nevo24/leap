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

from leap.utils.constants import NOTE_IMAGES_DIR, NOTES_DIR  # NOTE_IMAGES_DIR is re-exported; tests monkey-patch it here

if TYPE_CHECKING:
    from leap.monitor.dialogs.notes_dialog import NotesDialog

_NOTES_META_FILE: Path = NOTES_DIR / '.notes_meta.json'


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _leaf_name(path: str) -> str:
    """Return the last component of a '/'-separated path."""
    return path.rsplit('/', 1)[-1] if '/' in path else path


def _parent_folder(path: str) -> str:
    """Return the parent folder of a '/'-separated path ('' for root)."""
    return path.rsplit('/', 1)[0] if '/' in path else ''


# ---------------------------------------------------------------------------
# NotesCmdContext — thin interface between undo commands and NotesDialog
# ---------------------------------------------------------------------------

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

    @pending_image_deletes.setter
    def pending_image_deletes(self, value: set) -> None:
        self._d._pending_image_deletes = value

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

    def clear_and_select_first(self) -> None:
        """Clear editor state, refresh tree, select the first note."""
        self.current_name = None
        self.saved_text = ''
        self.refresh_tree()
        self.select_first_or_none()

    def get_checklist_items(self) -> list[dict]:
        return [dict(d) for d in self._d._checklist._items]

    def set_checklist_items(self, items: list[dict]) -> None:
        self._d._checklist._items = [dict(d) for d in items]
        self._d._checklist._rebuild()
        self._d._checklist.content_changed.emit()


class UndoCommand(ABC):
    """Abstract undoable command."""

    def __init__(self, description: str) -> None:
        self.description = description

    @abstractmethod
    def execute(self, ctx: NotesCmdContext) -> None:
        """Perform the operation."""

    @abstractmethod
    def undo(self, ctx: NotesCmdContext) -> None:
        """Reverse the operation."""


class NotesUndoStack:
    """Fixed-size undo/redo stack."""

    def __init__(self, limit: int = 50) -> None:
        self._commands: list[UndoCommand] = []
        self._cursor: int = 0
        self._limit = limit

    def push(self, cmd: UndoCommand, ctx: NotesCmdContext) -> None:
        """Execute *cmd* and push it onto the stack."""
        del self._commands[self._cursor:]
        cmd.execute(ctx)
        self._commands.append(cmd)
        self._cursor = len(self._commands)
        if len(self._commands) > self._limit:
            excess = len(self._commands) - self._limit
            del self._commands[:excess]
            self._cursor -= excess

    def undo(self, ctx: NotesCmdContext) -> None:
        """Undo the last executed command."""
        if not self.can_undo():
            return
        self._cursor -= 1
        self._commands[self._cursor].undo(ctx)

    def redo(self, ctx: NotesCmdContext) -> None:
        """Redo the last undone command."""
        if not self.can_redo():
            return
        self._commands[self._cursor].execute(ctx)
        self._cursor += 1

    def can_undo(self) -> bool:
        return self._cursor > 0

    def can_redo(self) -> bool:
        return self._cursor < len(self._commands)

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

    def drop_trailing_checklist_cmds(self, note_name: str) -> None:
        """Remove trailing checklist commands for *note_name*.

        Called before recording a NoteContentChangeCmd that subsumes
        individual checklist mutations — prevents double-undo.
        """
        _CL_TYPES = (ChecklistToggleCmd, ChecklistAddItemCmd,
                      ChecklistDeleteItemCmd, ChecklistReorderCmd)
        while (self._cursor > 0
               and isinstance(self._commands[self._cursor - 1], _CL_TYPES)
               and self._commands[self._cursor - 1]._note_name == note_name):
            self._cursor -= 1
            self._commands.pop()

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


def _rename_meta_keys(from_prefix: str, to_prefix: str) -> None:
    """Rename metadata keys that start with *from_prefix* to *to_prefix*."""
    meta = _load_notes_meta()
    updated: dict = {}
    for key, value in meta.items():
        if key == '_order':
            updated[key] = value
        elif key == from_prefix or key.startswith(from_prefix + '/'):
            updated[to_prefix + key[len(from_prefix):]] = value
        else:
            updated[key] = value
    if updated != meta:
        _save_notes_meta(updated)


def _rename_order_keys(from_prefix: str, to_prefix: str) -> None:
    """Rename _order dict keys from *from_prefix* to *to_prefix*."""
    order = _load_order()
    changed = False
    for old_k in [k for k in order if k == from_prefix or k.startswith(from_prefix + '/')]:
        order[to_prefix + old_k[len(from_prefix):]] = order.pop(old_k)
        changed = True
    if changed:
        _save_order(order)


# ---------------------------------------------------------------------------
# Concrete command classes
# ---------------------------------------------------------------------------

class CreateNoteCmd(UndoCommand):
    def __init__(self, name: str, folder: str) -> None:
        super().__init__(description=f"Create note '{_leaf_name(name)}'")
        self._name = name
        self._folder = folder

    def execute(self, ctx: NotesCmdContext) -> None:
        path = _note_path(self._name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('', encoding='utf-8')
        ctx.select_and_load(name=self._name)

    def undo(self, ctx: NotesCmdContext) -> None:
        try:
            _note_path(self._name).unlink(missing_ok=True)
        except OSError:
            pass
        _remove_from_order(self._folder, _leaf_name(self._name))
        meta = _load_notes_meta()
        if meta.pop(self._name, None) is not None:
            _save_notes_meta(meta)
        ctx.clear_and_select_first()


class CreateFolderCmd(UndoCommand):
    def __init__(self, folder_path: str) -> None:
        super().__init__(description=f"Create folder '{_leaf_name(folder_path)}'")
        self._folder_path = folder_path

    def execute(self, ctx: NotesCmdContext) -> None:
        (NOTES_DIR / self._folder_path).mkdir(parents=True, exist_ok=True)
        ctx.select_and_load(name=self._folder_path, select_type='folder')

    def undo(self, ctx: NotesCmdContext) -> None:
        target = NOTES_DIR / self._folder_path
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        parent = _parent_folder(self._folder_path)
        leaf = _leaf_name(self._folder_path)
        _remove_from_order(parent, leaf)
        ctx.refresh_tree()
        ctx.select_first_or_none()


class DeleteNoteCmd(UndoCommand):
    def __init__(self, name: str, content: str, metadata: dict,
                 order_position: tuple[str, int], image_refs: set[str]) -> None:
        super().__init__(description=f"Delete note '{_leaf_name(name)}'")
        self._name = name
        self._content = content
        self._metadata = metadata
        self._order_folder = order_position[0]
        self._order_index = order_position[1]
        self._image_refs = image_refs

    def execute(self, ctx: NotesCmdContext) -> None:
        try:
            _note_path(self._name).unlink(missing_ok=True)
        except OSError:
            pass
        meta = _load_notes_meta()
        meta.pop(self._name, None)
        _save_notes_meta(meta)
        _remove_from_order(self._order_folder, _leaf_name(self._name))
        ctx.pending_image_deletes.update(self._image_refs)
        # Clear stale state if we just deleted the active note
        if ctx.current_name == self._name:
            ctx.current_name = None
            ctx.saved_text = ''
        ctx.refresh_tree()
        ctx.select_first_or_none()

    def undo(self, ctx: NotesCmdContext) -> None:
        path = _note_path(self._name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._content, encoding='utf-8')
        if self._metadata:
            meta = _load_notes_meta()
            meta[self._name] = dict(self._metadata)
            _save_notes_meta(meta)
        _insert_into_order(self._order_folder, _leaf_name(self._name), self._order_index)
        ctx.pending_image_deletes.difference_update(self._image_refs)
        ctx.select_and_load(name=self._name)


class DeleteFolderCmd(UndoCommand):
    def __init__(self, folder_path: str, notes: dict[str, str],
                 metadata_entries: dict[str, dict], order_entries: dict[str, list[str]],
                 subfolder_paths: list[str], parent_order_position: tuple[str, int],
                 image_refs: set[str]) -> None:
        super().__init__(description=f"Delete folder '{_leaf_name(folder_path)}'")
        self._folder_path = folder_path
        self._notes = notes
        self._metadata_entries = metadata_entries
        self._order_entries = order_entries
        self._subfolder_paths = subfolder_paths
        self._parent_folder = parent_order_position[0]
        self._parent_index = parent_order_position[1]
        self._image_refs = image_refs

    def execute(self, ctx: NotesCmdContext) -> None:
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
        _remove_from_order(self._parent_folder, _leaf_name(self._folder_path))
        target = NOTES_DIR / self._folder_path
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        ctx.pending_image_deletes.update(self._image_refs)
        # Clear stale state if active note was in the deleted folder
        if ctx.current_name:
            cn = ctx.current_name
            if cn in self._notes or cn.startswith(self._folder_path + '/'):
                ctx.current_name = None
                ctx.saved_text = ''
        ctx.refresh_tree()
        ctx.select_first_or_none()

    def undo(self, ctx: NotesCmdContext) -> None:
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
        _insert_into_order(self._parent_folder, _leaf_name(self._folder_path), self._parent_index)
        ctx.pending_image_deletes.difference_update(self._image_refs)
        ctx.select_and_load(name=self._folder_path, select_type='folder')


class BatchDeleteCmd(UndoCommand):
    """Wraps multiple delete commands into a single undo action."""
    def __init__(self, commands: list[UndoCommand], description: str) -> None:
        super().__init__(description=description)
        self._commands = commands
    def execute(self, ctx: NotesCmdContext) -> None:
        for cmd in self._commands:
            cmd.execute(ctx)
    def undo(self, ctx: NotesCmdContext) -> None:
        for cmd in reversed(self._commands):
            cmd.undo(ctx)


class DuplicateNoteCmd(UndoCommand):
    def __init__(self, src_name: str, new_name: str, content: str,
                 metadata: dict, folder: str) -> None:
        super().__init__(description=f"Duplicate note '{_leaf_name(src_name)}'")
        self._new_name = new_name
        self._content = content
        self._metadata = metadata
        self._folder = folder

    def execute(self, ctx: NotesCmdContext) -> None:
        path = _note_path(self._new_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._content, encoding='utf-8')
        if self._metadata:
            meta = _load_notes_meta()
            meta[self._new_name] = dict(self._metadata)
            _save_notes_meta(meta)
        ctx.select_and_load(name=self._new_name)

    def undo(self, ctx: NotesCmdContext) -> None:
        try:
            _note_path(self._new_name).unlink(missing_ok=True)
        except OSError:
            pass
        _remove_from_order(self._folder, _leaf_name(self._new_name))
        meta = _load_notes_meta()
        if meta.pop(self._new_name, None) is not None:
            _save_notes_meta(meta)
        ctx.clear_and_select_first()


class DuplicateFolderCmd(UndoCommand):
    def __init__(self, src_path: str, new_path: str,
                 notes: dict[str, str], metadata_entries: dict[str, dict],
                 order_entries: dict[str, list[str]], subfolder_paths: list[str],
                 parent_folder: str) -> None:
        super().__init__(description=f"Duplicate folder '{_leaf_name(src_path)}'")
        self._new_path = new_path
        self._notes = notes
        self._metadata_entries = metadata_entries
        self._order_entries = order_entries
        self._subfolder_paths = subfolder_paths
        self._parent_folder = parent_folder

    def execute(self, ctx: NotesCmdContext) -> None:
        (NOTES_DIR / self._new_path).mkdir(parents=True, exist_ok=True)
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
        _insert_into_order(self._parent_folder, _leaf_name(self._new_path))
        ctx.select_and_load(name=self._new_path, select_type='folder')

    def undo(self, ctx: NotesCmdContext) -> None:
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
            if k == self._new_path or k.startswith(self._new_path + '/'):
                del order[k]
        _save_order(order)
        _remove_from_order(self._parent_folder, _leaf_name(self._new_path))
        target = NOTES_DIR / self._new_path
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        ctx.refresh_tree()
        ctx.select_first_or_none()


class RenameNoteCmd(UndoCommand):
    def __init__(self, old_name: str, new_name: str, parent_folder: str,
                 old_leaf: str, new_leaf: str) -> None:
        super().__init__(description=f"Rename note '{old_leaf}' to '{new_leaf}'")
        self._old_name = old_name
        self._new_name = new_name
        self._parent_folder = parent_folder
        self._old_leaf = old_leaf
        self._new_leaf = new_leaf

    def _do_rename(self, from_name: str, to_name: str, from_leaf: str, to_leaf: str,
                   ctx: NotesCmdContext) -> None:
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
        if ctx.current_name == from_name:
            ctx.current_name = to_name
        ctx.select_and_load(name=to_name)

    def execute(self, ctx: NotesCmdContext) -> None:
        self._do_rename(self._old_name, self._new_name, self._old_leaf, self._new_leaf, ctx)
    def undo(self, ctx: NotesCmdContext) -> None:
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

    def _do_rename(self, from_path: str, to_path: str, from_leaf: str, to_leaf: str,
                   ctx: NotesCmdContext) -> None:
        try:
            (NOTES_DIR / from_path).rename(NOTES_DIR / to_path)
        except OSError:
            return
        _rename_meta_keys(from_path, to_path)
        order = _load_order()
        lst = order.get(self._parent_folder, [])
        if from_leaf in lst:
            lst[lst.index(from_leaf)] = to_leaf
            order[self._parent_folder] = lst
        for old_k in [k for k in order if k == from_path or k.startswith(from_path + '/')]:
            order[to_path + old_k[len(from_path):]] = order.pop(old_k)
        _save_order(order)
        if ctx.current_name:
            cn = ctx.current_name
            if cn.startswith(from_path + '/'):
                ctx.current_name = to_path + cn[len(from_path):]
        ctx.select_and_load(name=to_path, select_type='folder')

    def execute(self, ctx: NotesCmdContext) -> None:
        self._do_rename(self._old_path, self._new_path, self._old_leaf, self._new_leaf, ctx)
    def undo(self, ctx: NotesCmdContext) -> None:
        self._do_rename(self._new_path, self._old_path, self._new_leaf, self._old_leaf, ctx)


class MoveNoteCmd(UndoCommand):
    def __init__(self, old_name: str, new_name: str, old_folder: str, new_folder: str,
                 old_order_position: tuple[str, int],
                 new_order_position: Optional[int] = None) -> None:
        super().__init__(description=f"Move note '{_leaf_name(old_name)}'")
        self._old_name = old_name
        self._new_name = new_name
        self._old_folder = old_folder
        self._new_folder = new_folder
        self._old_order_folder = old_order_position[0]
        self._old_order_index = old_order_position[1]
        self._new_order_position = new_order_position

    def execute(self, ctx: NotesCmdContext) -> None:
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
        _remove_from_order(self._old_folder, _leaf_name(self._old_name))
        _insert_into_order(self._new_folder, _leaf_name(self._new_name), self._new_order_position)
        if ctx.current_name == self._old_name:
            ctx.current_name = self._new_name
        ctx.select_and_load(name=self._new_name)

    def undo(self, ctx: NotesCmdContext) -> None:
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
        _remove_from_order(self._new_folder, _leaf_name(self._new_name))
        _insert_into_order(self._old_order_folder, _leaf_name(self._old_name), self._old_order_index)
        if ctx.current_name == self._new_name:
            ctx.current_name = self._old_name
        ctx.select_and_load(name=self._old_name)


class MoveFolderCmd(UndoCommand):
    def __init__(self, old_path: str, new_path: str, old_parent: str, new_parent: str,
                 old_order_position: tuple[str, int],
                 new_order_position: Optional[int] = None) -> None:
        super().__init__(description=f"Move folder '{_leaf_name(old_path)}'")
        self._old_path = old_path
        self._new_path = new_path
        self._old_parent = old_parent
        self._new_parent = new_parent
        self._old_order_folder = old_order_position[0]
        self._old_order_index = old_order_position[1]
        self._new_order_position = new_order_position

    def _do_move(self, from_path: str, to_path: str, from_parent: str,
                 to_parent: str, to_position: Optional[int], ctx: NotesCmdContext) -> None:
        src = NOTES_DIR / from_path
        dest = NOTES_DIR / to_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.rename(dest)
        except OSError:
            return
        _rename_meta_keys(from_path, to_path)
        _remove_from_order(from_parent, _leaf_name(from_path))
        _rename_order_keys(from_path, to_path)
        # Insert into target parent order at specified position
        _insert_into_order(to_parent, _leaf_name(to_path), to_position)
        if ctx.current_name:
            cn = ctx.current_name
            if cn.startswith(from_path + '/'):
                ctx.current_name = to_path + cn[len(from_path):]

    def execute(self, ctx: NotesCmdContext) -> None:
        self._do_move(self._old_path, self._new_path, self._old_parent,
                      self._new_parent, self._new_order_position, ctx)
        ctx.select_and_load(name=self._new_path, select_type='folder')
    def undo(self, ctx: NotesCmdContext) -> None:
        self._do_move(self._new_path, self._old_path, self._new_parent,
                      self._old_parent, self._old_order_index, ctx)
        ctx.select_and_load(name=self._old_path, select_type='folder')


class ReorderCmd(UndoCommand):
    def __init__(self, folder: str, old_order: list[str], new_order: list[str]) -> None:
        super().__init__(description=f"Reorder in '{folder or 'root'}'")
        self._folder = folder
        self._old_order = old_order
        self._new_order = new_order

    def execute(self, ctx: NotesCmdContext) -> None:
        order = _load_order()
        order[self._folder] = list(self._new_order)
        _save_order(order)
        ctx.refresh_tree()

    def undo(self, ctx: NotesCmdContext) -> None:
        order = _load_order()
        order[self._folder] = list(self._old_order)
        _save_order(order)
        ctx.refresh_tree()


class ModeSwitchCmd(UndoCommand):
    """Undo command for switching between text and checklist mode."""

    def __init__(self, note_name: str, old_mode: str, new_mode: str,
                 old_content: str, new_content: str) -> None:
        super().__init__(description=f"Switch to {new_mode}")
        self._note_name = note_name
        self._old_mode = old_mode
        self._new_mode = new_mode
        self._old_content = old_content
        self._new_content = new_content

    def _apply(self, mode: str, content: str, ctx: NotesCmdContext) -> None:
        meta = _load_notes_meta()
        meta.setdefault(self._note_name, {})['mode'] = mode
        _save_notes_meta(meta)
        path = _note_path(self._note_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        ctx.saved_text = content
        # Select the note in tree and load into editor — covers redo when
        # the user may have navigated away.
        ctx.select_and_load(name=self._note_name)

    def execute(self, ctx: NotesCmdContext) -> None:
        self._apply(self._new_mode, self._new_content, ctx)

    def undo(self, ctx: NotesCmdContext) -> None:
        self._apply(self._old_mode, self._old_content, ctx)


class NoteContentChangeCmd(UndoCommand):
    """Undo command for note content changes (pushed on note switch)."""

    def __init__(self, note_name: str, old_text: str, new_text: str, mode: str) -> None:
        super().__init__(description=f"Edit '{_leaf_name(note_name)}'")
        self._note_name = note_name
        self._old_text = old_text
        self._new_text = new_text
        self._mode = mode

    def _write(self, text: str, ctx: NotesCmdContext) -> None:
        path = _note_path(self._note_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        ctx.saved_text = text
        # Use select_and_load to refresh tree + select the note (may differ
        # from the currently-selected note if the user switched away).
        ctx.select_and_load(name=self._note_name)

    def execute(self, ctx: NotesCmdContext) -> None:
        self._write(self._new_text, ctx)

    def undo(self, ctx: NotesCmdContext) -> None:
        self._write(self._old_text, ctx)


class ChecklistToggleCmd(UndoCommand):
    """Undo command for toggling a checklist item's checked state."""

    def __init__(self, note_name: str, item_index: int, old_checked: bool) -> None:
        super().__init__(description='Toggle checklist item')
        self._note_name = note_name
        self._index = item_index
        self._old_checked = old_checked

    def execute(self, ctx: NotesCmdContext) -> None:
        if ctx.current_name != self._note_name:
            ctx.select_and_load(name=self._note_name)
        items = ctx.get_checklist_items()
        if 0 <= self._index < len(items):
            items[self._index]['checked'] = not self._old_checked
            ctx.set_checklist_items(items)

    def undo(self, ctx: NotesCmdContext) -> None:
        if ctx.current_name != self._note_name:
            ctx.select_and_load(name=self._note_name)
        items = ctx.get_checklist_items()
        if 0 <= self._index < len(items):
            items[self._index]['checked'] = self._old_checked
            ctx.set_checklist_items(items)


class ChecklistAddItemCmd(UndoCommand):
    """Undo command for adding a checklist item (Enter or Add field)."""

    def __init__(self, note_name: str, item_index: int, item_text: str) -> None:
        super().__init__(description='Add checklist item')
        self._note_name = note_name
        self._index = item_index
        self._text = item_text

    def execute(self, ctx: NotesCmdContext) -> None:
        if ctx.current_name != self._note_name:
            ctx.select_and_load(name=self._note_name)
        items = ctx.get_checklist_items()
        items.insert(self._index, {'text': self._text, 'checked': False})
        ctx.set_checklist_items(items)

    def undo(self, ctx: NotesCmdContext) -> None:
        if ctx.current_name != self._note_name:
            ctx.select_and_load(name=self._note_name)
        items = ctx.get_checklist_items()
        if 0 <= self._index < len(items):
            del items[self._index]
            ctx.set_checklist_items(items)


class ChecklistDeleteItemCmd(UndoCommand):
    """Undo command for deleting a checklist item."""

    def __init__(self, note_name: str, item_index: int, item_text: str,
                 item_checked: bool, item_bold: bool = False) -> None:
        super().__init__(description='Delete checklist item')
        self._note_name = note_name
        self._index = item_index
        self._text = item_text
        self._checked = item_checked
        self._bold = item_bold

    def execute(self, ctx: NotesCmdContext) -> None:
        if ctx.current_name != self._note_name:
            ctx.select_and_load(name=self._note_name)
        items = ctx.get_checklist_items()
        if 0 <= self._index < len(items):
            del items[self._index]
            ctx.set_checklist_items(items)

    def undo(self, ctx: NotesCmdContext) -> None:
        if ctx.current_name != self._note_name:
            ctx.select_and_load(name=self._note_name)
        items = ctx.get_checklist_items()
        items.insert(self._index, {'text': self._text, 'checked': self._checked,
                                   'bold': self._bold})
        ctx.set_checklist_items(items)


class ChecklistReorderCmd(UndoCommand):
    """Undo command for reordering a checklist item."""

    def __init__(self, note_name: str, src_index: int, dst_index: int) -> None:
        super().__init__(description='Reorder checklist item')
        self._note_name = note_name
        self._src = src_index
        self._dst = dst_index

    def execute(self, ctx: NotesCmdContext) -> None:
        if ctx.current_name != self._note_name:
            ctx.select_and_load(name=self._note_name)
        items = ctx.get_checklist_items()
        if 0 <= self._src < len(items):
            item = items.pop(self._src)
            effective_dst = self._dst if self._dst <= self._src else self._dst - 1
            items.insert(effective_dst, item)
            ctx.set_checklist_items(items)

    def undo(self, ctx: NotesCmdContext) -> None:
        if ctx.current_name != self._note_name:
            ctx.select_and_load(name=self._note_name)
        effective_dst = self._dst if self._dst <= self._src else self._dst - 1
        items = ctx.get_checklist_items()
        if 0 <= effective_dst < len(items):
            item = items.pop(effective_dst)
            items.insert(self._src, item)
            ctx.set_checklist_items(items)
