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
