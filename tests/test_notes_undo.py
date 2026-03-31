"""Tests for Notes dialog undo/redo system."""

import json
from pathlib import Path

import pytest

import leap.monitor.dialogs.notes_undo as nu
from leap.monitor.dialogs.notes_undo import (
    ChecklistDeleteItemCmd,
    ChecklistReorderCmd,
    ChecklistToggleCmd,
    CreateFolderCmd,
    CreateNoteCmd,
    DeleteFolderCmd,
    DeleteNoteCmd,
    ModeSwitchCmd,
    MoveFolderCmd,
    MoveNoteCmd,
    NoteContentChangeCmd,
    RenameFolderCmd,
    RenameNoteCmd,
    ReorderCmd,
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


# ---------------------------------------------------------------------------
# RenameNoteCmd tests
# ---------------------------------------------------------------------------

class TestRenameNoteCmd:
    def test_rename_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'Old.txt').write_text('content', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({'Old': {'mode': 'text'}, '_order': {'': ['Old']}}), encoding='utf-8')
        ctx = _StubCtx()
        cmd = RenameNoteCmd(old_name='Old', new_name='New', parent_folder='', old_leaf='Old', new_leaf='New')
        cmd.execute(ctx)
        assert not (notes_dir / 'Old.txt').exists()
        assert (notes_dir / 'New.txt').exists()
        cmd.undo(ctx)
        assert (notes_dir / 'Old.txt').exists()
        assert not (notes_dir / 'New.txt').exists()


# ---------------------------------------------------------------------------
# RenameFolderCmd tests
# ---------------------------------------------------------------------------

class TestRenameFolderCmd:
    def test_rename_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'OldDir').mkdir()
        (notes_dir / 'OldDir' / 'Note.txt').write_text('hi', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'OldDir/Note': {'mode': 'text'}, '_order': {'': ['OldDir'], 'OldDir': ['Note']},
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = RenameFolderCmd(old_path='OldDir', new_path='NewDir', parent_folder='', old_leaf='OldDir', new_leaf='NewDir')
        cmd.execute(ctx)
        assert not (notes_dir / 'OldDir').exists()
        assert (notes_dir / 'NewDir').is_dir()
        cmd.undo(ctx)
        assert (notes_dir / 'OldDir').is_dir()
        assert not (notes_dir / 'NewDir').exists()


# ---------------------------------------------------------------------------
# MoveNoteCmd tests
# ---------------------------------------------------------------------------

class TestMoveNoteCmd:
    def test_move_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'Work').mkdir()
        (notes_dir / 'Task.txt').write_text('hello', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({'Task': {'mode': 'text'}, '_order': {'': ['Task', 'Work']}}), encoding='utf-8')
        ctx = _StubCtx()
        cmd = MoveNoteCmd(old_name='Task', new_name='Work/Task', old_folder='', new_folder='Work', old_order_position=('', 0))
        cmd.execute(ctx)
        assert not (notes_dir / 'Task.txt').exists()
        assert (notes_dir / 'Work' / 'Task.txt').exists()
        cmd.undo(ctx)
        assert (notes_dir / 'Task.txt').exists()
        assert not (notes_dir / 'Work' / 'Task.txt').exists()


# ---------------------------------------------------------------------------
# MoveFolderCmd tests
# ---------------------------------------------------------------------------

class TestMoveFolderCmd:
    def test_move_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'A').mkdir()
        (notes_dir / 'B').mkdir()
        (notes_dir / 'A' / 'Note.txt').write_text('x', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({
            'A/Note': {'mode': 'text'}, '_order': {'': ['A', 'B'], 'A': ['Note']},
        }), encoding='utf-8')
        ctx = _StubCtx()
        cmd = MoveFolderCmd(old_path='A', new_path='B/A', old_parent='', new_parent='B', old_order_position=('', 0))
        cmd.execute(ctx)
        assert not (notes_dir / 'A').exists()
        assert (notes_dir / 'B' / 'A').is_dir()
        cmd.undo(ctx)
        assert (notes_dir / 'A').is_dir()
        assert not (notes_dir / 'B' / 'A').exists()


# ---------------------------------------------------------------------------
# ReorderCmd tests
# ---------------------------------------------------------------------------

class TestReorderCmd:
    def test_reorder_and_undo(self, notes_dir: Path) -> None:
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({'_order': {'': ['A', 'B', 'C']}}), encoding='utf-8')
        ctx = _StubCtx()
        cmd = ReorderCmd(folder='', old_order=['A', 'B', 'C'], new_order=['C', 'A', 'B'])
        cmd.execute(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta['_order'][''] == ['C', 'A', 'B']
        cmd.undo(ctx)
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
        assert meta['_order'][''] == ['A', 'B', 'C']


# ---------------------------------------------------------------------------
# _StubChecklistCtx for checklist command tests
# ---------------------------------------------------------------------------

class _StubChecklistCtx(_StubCtx):
    def __init__(self) -> None:
        super().__init__()
        self._items: list[dict] = []

    def get_checklist_items(self) -> list[dict]:
        return [dict(d) for d in self._items]

    def set_checklist_items(self, items: list[dict]) -> None:
        self._items = [dict(d) for d in items]


# ---------------------------------------------------------------------------
# ModeSwitchCmd tests
# ---------------------------------------------------------------------------

class TestModeSwitchCmd:
    def test_switch_to_checklist_and_undo(self, notes_dir: Path) -> None:
        (notes_dir / 'Note.txt').write_text('hello world', encoding='utf-8')
        meta_file = notes_dir / '.notes_meta.json'
        meta_file.write_text(json.dumps({'Note': {'mode': 'text'}}), encoding='utf-8')
        ctx = _StubCtx()
        cmd = ModeSwitchCmd(note_name='Note', old_mode='text', new_mode='checklist',
                            old_content='hello world', new_content='- [ ] hello world')
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
        meta_file.write_text(json.dumps({'CL': {'mode': 'checklist'}}), encoding='utf-8')
        ctx = _StubCtx()
        cmd = ModeSwitchCmd(note_name='CL', old_mode='checklist', new_mode='text',
                            old_content='- [x] done\n- [ ] todo', new_content='done\ntodo')
        cmd.execute(ctx)
        assert (notes_dir / 'CL.txt').read_text(encoding='utf-8') == 'done\ntodo'
        cmd.undo(ctx)
        assert (notes_dir / 'CL.txt').read_text(encoding='utf-8') == '- [x] done\n- [ ] todo'


# ---------------------------------------------------------------------------
# NoteContentChangeCmd tests
# ---------------------------------------------------------------------------

class TestNoteContentChangeCmd:
    def test_undo_restores_old_text(self, notes_dir: Path) -> None:
        (notes_dir / 'A.txt').write_text('new text', encoding='utf-8')
        ctx = _StubCtx()
        cmd = NoteContentChangeCmd(note_name='A', old_text='old text', new_text='new text', mode='text')
        cmd.execute(ctx)
        assert (notes_dir / 'A.txt').read_text(encoding='utf-8') == 'new text'
        cmd.undo(ctx)
        assert (notes_dir / 'A.txt').read_text(encoding='utf-8') == 'old text'

    def test_redo_restores_new_text(self, notes_dir: Path) -> None:
        (notes_dir / 'B.txt').write_text('old', encoding='utf-8')
        ctx = _StubCtx()
        stack = NotesUndoStack(limit=50)
        stack.push(NoteContentChangeCmd(note_name='B', old_text='old', new_text='new', mode='text'), ctx=ctx)
        stack.undo(ctx=ctx)
        assert (notes_dir / 'B.txt').read_text(encoding='utf-8') == 'old'
        stack.redo(ctx=ctx)
        assert (notes_dir / 'B.txt').read_text(encoding='utf-8') == 'new'


# ---------------------------------------------------------------------------
# ChecklistToggleCmd tests
# ---------------------------------------------------------------------------

class TestChecklistToggleCmd:
    def test_toggle_and_undo(self) -> None:
        ctx = _StubChecklistCtx()
        ctx._items = [{'text': 'a', 'checked': False}, {'text': 'b', 'checked': False}]
        cmd = ChecklistToggleCmd(item_index=0, old_checked=False)
        cmd.execute(ctx)
        assert ctx._items[0]['checked'] is True
        cmd.undo(ctx)
        assert ctx._items[0]['checked'] is False


# ---------------------------------------------------------------------------
# ChecklistDeleteItemCmd tests
# ---------------------------------------------------------------------------

class TestChecklistDeleteItemCmd:
    def test_delete_and_undo(self) -> None:
        ctx = _StubChecklistCtx()
        ctx._items = [{'text': 'a', 'checked': False}, {'text': 'b', 'checked': True}, {'text': 'c', 'checked': False}]
        cmd = ChecklistDeleteItemCmd(item_index=1, item_text='b', item_checked=True)
        cmd.execute(ctx)
        assert len(ctx._items) == 2
        assert ctx._items[0]['text'] == 'a'
        assert ctx._items[1]['text'] == 'c'
        cmd.undo(ctx)
        assert len(ctx._items) == 3
        assert ctx._items[1] == {'text': 'b', 'checked': True}


# ---------------------------------------------------------------------------
# ChecklistReorderCmd tests
# ---------------------------------------------------------------------------

class TestChecklistReorderCmd:
    def test_reorder_and_undo(self) -> None:
        ctx = _StubChecklistCtx()
        ctx._items = [{'text': 'a', 'checked': False}, {'text': 'b', 'checked': False}, {'text': 'c', 'checked': False}]
        cmd = ChecklistReorderCmd(src_index=2, dst_index=0)
        cmd.execute(ctx)
        assert [d['text'] for d in ctx._items] == ['c', 'a', 'b']
        cmd.undo(ctx)
        assert [d['text'] for d in ctx._items] == ['a', 'b', 'c']


# ---------------------------------------------------------------------------
# Deferred image cleanup tests
# ---------------------------------------------------------------------------

class TestDeferredImageCleanup:
    def test_deferred_images_not_deleted_during_session(self, notes_dir: Path) -> None:
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'aaa.png').write_bytes(b'image1')
        (notes_dir / 'PicNote.txt').write_text('![image](aaa.png)', encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(name='PicNote', content='![image](aaa.png)', metadata={},
                            order_position=('', 0), image_refs={'aaa.png'})
        cmd.execute(ctx)
        assert (img_dir / 'aaa.png').exists()
        assert 'aaa.png' in ctx.pending_image_deletes

    def test_undo_removes_from_deferred(self, notes_dir: Path) -> None:
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'bbb.png').write_bytes(b'image2')
        (notes_dir / 'PicNote.txt').write_text('![image](bbb.png)', encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(name='PicNote', content='![image](bbb.png)', metadata={},
                            order_position=('', 0), image_refs={'bbb.png'})
        cmd.execute(ctx)
        cmd.undo(ctx)
        assert 'bbb.png' not in ctx.pending_image_deletes
        assert (img_dir / 'bbb.png').exists()

    def test_image_shared_across_notes_survives_delete(self, notes_dir: Path) -> None:
        img_dir = notes_dir.parent / 'note_images'
        (img_dir / 'shared.png').write_bytes(b'shared')
        (notes_dir / 'A.txt').write_text('![image](shared.png)', encoding='utf-8')
        (notes_dir / 'B.txt').write_text('![image](shared.png)', encoding='utf-8')
        ctx = _StubCtx()
        cmd = DeleteNoteCmd(name='A', content='![image](shared.png)', metadata={},
                            order_position=('', 0), image_refs={'shared.png'})
        cmd.execute(ctx)
        assert (img_dir / 'shared.png').exists()


# ---------------------------------------------------------------------------
# record() method tests
# ---------------------------------------------------------------------------

class TestRecordMethod:
    def test_record_does_not_execute(self) -> None:
        log: list[str] = []
        stack = NotesUndoStack(limit=50)
        stack.record(_FakeCmd('a', log))
        assert log == []
        assert stack.can_undo()
        stack.undo(ctx=None)
        assert log == ['undo:a']
