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
