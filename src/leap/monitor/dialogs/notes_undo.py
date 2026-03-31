"""Undo/redo system for the Notes dialog.

Provides a command-pattern undo stack and concrete command classes for
all structural operations (create, delete, rename, move, reorder,
mode switch, checklist mutations).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from leap.monitor.dialogs.notes_dialog import NotesDialog


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
