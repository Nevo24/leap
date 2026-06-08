"""Headless (offscreen) tests for the checklist "Add item" draft.

These drive the real ``_ChecklistWidget`` - the same widget the Notes
dialog uses - to prove the half-typed-but-not-submitted "Add item" text
behaves correctly across the operations that recreate the field.  No
window is shown (``QT_QPA_PLATFORM=offscreen``).

The bug these guard against: typing into "Add item" without pressing
Enter, then doing anything that rebuilds the list (checking a box,
switching notes, closing the window) silently dropped the text, because
``_rebuild`` recreates the QLineEdit from scratch and nothing carried the
unsubmitted text across.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from PyQt5.QtWidgets import QApplication

from leap.monitor.dialogs.notes.checklist_widgets import _ChecklistWidget


@pytest.fixture(scope='module')
def _qapp() -> Any:
    app = QApplication.instance() or QApplication([])
    yield app


def _make(items: list[dict] | None = None) -> _ChecklistWidget:
    w = _ChecklistWidget()
    w.set_items(items or [])
    return w


class TestDraftSurvivesRebuild:
    def test_typing_updates_draft(self, _qapp: Any) -> None:
        w = _make()
        # Setting the field text emits textChanged, exactly like a keystroke.
        w._add_field.setText('disappearing')
        assert w._add_draft == 'disappearing'
        assert w.get_add_draft() == 'disappearing'

    def test_draft_survives_explicit_rebuild(self, _qapp: Any) -> None:
        w = _make([{'text': 'a', 'checked': False, 'bold': False}])
        w._add_field.setText('half typed')
        old_field = w._add_field

        w._rebuild()

        # A real rebuild replaces the QLineEdit object...
        assert w._add_field is not old_field
        # ...but the unsubmitted text is re-seeded into the new one.
        assert w._add_field.text() == 'half typed'
        assert w._add_draft == 'half typed'

    def test_draft_survives_toggling_an_item(self, _qapp: Any) -> None:
        # Checking a box rebuilds the list - the most common way the old
        # code dropped the draft.
        w = _make([{'text': 'a', 'checked': False, 'bold': False}])
        w._add_field.setText('keep me')
        w._items[0]['checked'] = True
        w._rebuild()
        assert w._add_field.text() == 'keep me'


class TestAddItemClearsDraft:
    def test_submitting_consumes_and_clears(self, _qapp: Any) -> None:
        w = _make()
        w._add_field.setText('new item')
        w._on_add_item()

        # The text became a real item...
        assert any(i['text'] == 'new item' for i in w._items)
        # ...and the draft is cleared so the field comes back empty.
        assert w._add_draft == ''
        assert w._add_field.text() == ''

    def test_blank_draft_adds_nothing(self, _qapp: Any) -> None:
        w = _make()
        w._add_field.setText('   ')
        w._on_add_item()
        assert w._items == []
        # Whitespace-only text isn't a real item, but it also isn't a
        # draft worth persisting.
        assert w.get_add_draft().strip() == ''


class TestPopupClearEmptiesDraft:
    """The expanded multi-line add editor must propagate a *clear* back to
    the draft - deleting the text and leaving the field should empty it,
    not resurrect the old value (the inverse of the persistence bug).
    """

    def test_clearing_popup_then_leaving_empties_draft(self, _qapp: Any) -> None:
        w = _make([{'text': 'a', 'checked': False, 'bold': False}])
        w._add_field.setText('disappearing')
        assert w._add_draft == 'disappearing'

        w._expand_add_field()
        assert w._add_popup is not None
        # User deletes everything in the expanded editor...
        w._add_popup.setPlainText('')
        # ...then clicks away (focus-out dismisses with save=True).
        w._dismiss_add_popup(save=True)

        assert w._add_field.text() == ''
        assert w._add_draft == ''
        assert w.get_add_draft() == ''

    def test_unchanged_popup_preserves_draft(self, _qapp: Any) -> None:
        w = _make([{'text': 'a', 'checked': False, 'bold': False}])
        w._add_field.setText('keep me')
        w._expand_add_field()
        # Leave without editing.
        w._dismiss_add_popup(save=True)
        assert w._add_field.text() == 'keep me'
        assert w._add_draft == 'keep me'

    def test_edited_popup_updates_draft(self, _qapp: Any) -> None:
        w = _make()
        w._add_field.setText('before')
        w._expand_add_field()
        w._add_popup.setPlainText('after')
        w._dismiss_add_popup(save=True)
        assert w._add_field.text() == 'after'
        assert w._add_draft == 'after'


class TestSetGetRoundTrip:
    def test_restore_then_read_back(self, _qapp: Any) -> None:
        w = _make([{'text': 'a', 'checked': False, 'bold': False}])
        w.set_add_draft('restored text')
        assert w._add_field.text() == 'restored text'
        assert w.get_add_draft() == 'restored text'

    def test_restore_empty_clears_field(self, _qapp: Any) -> None:
        w = _make()
        w._add_field.setText('stale')
        w.set_add_draft('')
        assert w._add_field.text() == ''
        assert w.get_add_draft() == ''

    def test_full_reopen_cycle(self, _qapp: Any) -> None:
        # Mirror the dialog's save/close -> reopen path: read the draft off
        # one widget (what _save_current persists) and restore it onto a
        # fresh widget (what _on_item_changed does on load).
        w1 = _make([{'text': 'a', 'checked': False, 'bold': False}])
        w1._add_field.setText('survives close')
        stored = w1.get_add_draft()

        w2 = _make([{'text': 'a', 'checked': False, 'bold': False}])
        w2.set_add_draft(stored)
        assert w2._add_field.text() == 'survives close'
