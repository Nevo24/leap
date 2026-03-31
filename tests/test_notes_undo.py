"""Tests for Notes dialog undo/redo system."""

import json
from pathlib import Path

import pytest

import leap.monitor.dialogs.notes_undo as nu
from leap.monitor.dialogs.notes_undo import (
    CreateFolderCmd,
    CreateNoteCmd,
    DeleteFolderCmd,
    DeleteNoteCmd,
    NotesUndoStack,
    UndoCommand,
)


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


# ---------------------------------------------------------------------------
# Fixtures and helpers for concrete command tests
# ---------------------------------------------------------------------------

@pytest.fixture
def notes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / 'notes'
    d.mkdir()
    img_d = tmp_path / 'note_images'
    img_d.mkdir()
    monkeypatch.setattr(nu, 'NOTES_DIR', d)
    monkeypatch.setattr(nu, '_NOTES_META_FILE', d / '.notes_meta.json')
    monkeypatch.setattr(nu, 'NOTE_IMAGES_DIR', img_d)
    return d


class _StubCtx:
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


# ---------------------------------------------------------------------------
# CreateNoteCmd tests
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CreateFolderCmd tests
# ---------------------------------------------------------------------------

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
        assert (notes_dir / 'parent').is_dir()


# ---------------------------------------------------------------------------
# DeleteNoteCmd tests
# ---------------------------------------------------------------------------

class TestDeleteNoteCmd:
    def test_delete_and_undo_restores_note(self, notes_dir: Path) -> None:
        (notes_dir / 'Todo.txt').write_text('buy milk', encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(name='Todo', content='buy milk', metadata={'mode': 'text'},
                            order_position=('', 0), image_refs=set())
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
        cmd = DeleteNoteCmd(name='Img', content='![image](abc123.png)', metadata={},
                            order_position=('', 0), image_refs={'abc123.png'})
        cmd.execute(ctx)
        assert (img_dir / 'abc123.png').exists()
        assert 'abc123.png' in ctx.pending_image_deletes

    def test_undo_restores_image_from_deferred(self, notes_dir: Path) -> None:
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'abc123.png').write_bytes(b'fake')
        (notes_dir / 'Img.txt').write_text('![image](abc123.png)', encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(name='Img', content='![image](abc123.png)', metadata={},
                            order_position=('', 0), image_refs={'abc123.png'})
        cmd.execute(ctx)
        cmd.undo(ctx)
        assert 'abc123.png' not in ctx.pending_image_deletes
        assert (notes_dir / 'Img.txt').exists()

    def test_delete_restores_metadata(self, notes_dir: Path) -> None:
        (notes_dir / 'CL.txt').write_text('- [ ] a', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({'CL': {'mode': 'checklist'}}), encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(name='CL', content='- [ ] a', metadata={'mode': 'checklist'},
                            order_position=('', 0), image_refs=set())
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
        meta_file.write_text(json.dumps({'_order': {'': ['A', 'B', 'C']}}), encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(name='B', content='', metadata={},
                            order_position=('', 1), image_refs=set())
        cmd.execute(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert 'B' not in meta.get('_order', {}).get('', [])
        cmd.undo(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        order = meta.get('_order', {}).get('', [])
        assert order.index('B') == 1


# ---------------------------------------------------------------------------
# DeleteFolderCmd tests
# ---------------------------------------------------------------------------

class TestDeleteFolderCmd:
    def test_delete_and_undo_restores_folder(self, notes_dir: Path) -> None:
        folder = notes_dir / 'Work'
        folder.mkdir()
        (folder / 'Task1.txt').write_text('do stuff', encoding='utf-8')
        (folder / 'Task2.txt').write_text('more stuff', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'Work/Task1': {'mode': 'text'}, 'Work/Task2': {'mode': 'checklist'},
            '_order': {'': ['Work'], 'Work': ['Task1', 'Task2']},
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteFolderCmd(
            folder_path='Work', notes={'Work/Task1': 'do stuff', 'Work/Task2': 'more stuff'},
            metadata_entries={'Work/Task1': {'mode': 'text'}, 'Work/Task2': {'mode': 'checklist'}},
            order_entries={'Work': ['Task1', 'Task2']}, subfolder_paths=[],
            parent_order_position=('', 0), image_refs=set())
        cmd.execute(ctx)
        assert not folder.exists()
        cmd.undo(ctx)
        assert folder.is_dir()
        assert (folder / 'Task1.txt').read_text(encoding='utf-8') == 'do stuff'
        assert (folder / 'Task2.txt').read_text(encoding='utf-8') == 'more stuff'
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta.get('Work/Task1') == {'mode': 'text'}
        assert 'Work' in meta.get('_order', {}).get('', [])

    def test_delete_folder_defers_images(self, notes_dir: Path) -> None:
        folder = notes_dir / 'Pics'
        folder.mkdir()
        (folder / 'Note.txt').write_text('![image](img1.png)', encoding='utf-8')
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'img1.png').write_bytes(b'fake')
        ctx = _StubCtx()
        cmd = DeleteFolderCmd(
            folder_path='Pics', notes={'Pics/Note': '![image](img1.png)'},
            metadata_entries={}, order_entries={}, subfolder_paths=[],
            parent_order_position=('', 0), image_refs={'img1.png'})
        cmd.execute(ctx)
        assert 'img1.png' in ctx.pending_image_deletes
        cmd.undo(ctx)
        assert 'img1.png' not in ctx.pending_image_deletes
