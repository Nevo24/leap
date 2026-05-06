"""Custom ``QTreeWidget`` for the Notes dialog left panel.

Uses Qt's native ``InternalMove`` drag-drop mode for the visual drop
indicator, but intercepts the actual drop so the *dialog* performs the
filesystem move and rebuilds the tree afterwards (Qt's default
rearrangement would corrupt the tree's path/type data roles).
"""

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QAbstractItemView, QTreeWidget, QWidget


class _NotesTreeWidget(QTreeWidget):
    """QTreeWidget that uses Qt's native InternalMove for the drop indicator
    line, but intercepts ``dropEvent`` so the *dialog* can do the real
    filesystem move and rebuild the tree.
    """

    # (source_path, source_type, target_folder, before_path)
    # target_folder '' = root.  before_path '' = append at end.
    item_dropped = pyqtSignal(str, str, str, str)
    rename_requested = pyqtSignal()
    copy_requested = pyqtSignal()
    paste_requested = pyqtSignal()

    _ROLE_PATH = Qt.UserRole
    _ROLE_TYPE = Qt.UserRole + 1

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)

    def keyPressEvent(self, event: 'QKeyEvent') -> None:  # type: ignore[override]
        # Enter on a selected note/folder opens the rename dialog.
        if (event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and self.currentItem() is not None):
            self.rename_requested.emit()
            event.accept()
            return
        if event.key() == Qt.Key_C and event.modifiers() == Qt.ControlModifier:
            self.copy_requested.emit()
            event.accept()
            return
        if event.key() == Qt.Key_V and event.modifiers() == Qt.ControlModifier:
            self.paste_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def dropEvent(self, event: 'QDropEvent') -> None:
        """Intercept drop — compute target folder, emit signal, skip Qt rearrange."""
        dragged = self.selectedItems()
        if not dragged:
            event.ignore()
            return
        source = dragged[0]
        src_path = source.data(0, self._ROLE_PATH) or ''
        src_type = source.data(0, self._ROLE_TYPE) or ''
        if not src_path:
            event.ignore()
            return

        target_item = self.itemAt(event.pos())
        indicator = self.dropIndicatorPosition()

        if target_item is None:
            target_folder = ''
        elif (indicator == QAbstractItemView.OnItem
              and target_item.data(0, self._ROLE_TYPE) == 'folder'):
            # Dropped directly onto a folder → move inside it
            target_folder = target_item.data(0, self._ROLE_PATH) or ''
        else:
            # Above/below an item → use the containing folder
            item_path = target_item.data(0, self._ROLE_PATH) or ''
            if (target_item.data(0, self._ROLE_TYPE) == 'folder'
                    and target_item.parent() is not None):
                # Between folders inside a parent → that parent folder
                parent_path = target_item.parent().data(
                    0, self._ROLE_PATH) or ''
                target_folder = parent_path
            elif target_item.data(0, self._ROLE_TYPE) == 'folder':
                # Between top-level folders → root
                target_folder = ''
            else:
                # Between notes → same folder as the note
                target_folder = (
                    item_path.rsplit('/', 1)[0] if '/' in item_path else '')

        # Compute insertion position for ordering
        before_path = ''
        if indicator == QAbstractItemView.AboveItem and target_item is not None:
            before_path = target_item.data(0, self._ROLE_PATH) or ''
        elif indicator == QAbstractItemView.BelowItem and target_item is not None:
            parent_ti = target_item.parent() or self.invisibleRootItem()
            idx = parent_ti.indexOfChild(target_item)
            # Find next sibling, skipping the dragged item itself
            for j in range(idx + 1, parent_ti.childCount()):
                sibling = parent_ti.child(j)
                if sibling is not source:
                    before_path = (
                        sibling.data(0, self._ROLE_PATH) or '')
                    break

        # Accept without calling super — prevents Qt from rearranging items
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        self.item_dropped.emit(src_path, src_type, target_folder, before_path)
