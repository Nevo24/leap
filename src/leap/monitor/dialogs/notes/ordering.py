"""Folder listing, folder-meta cleanup, and per-folder child ordering.

The Notes dialog presents notes inside folders, with two pieces of
state per folder: the on-disk directory layout (handled by ``_list_folders``
and folder-meta helpers) and a stored child ordering (under the special
``_order`` key in ``.notes_meta.json``) so the user-visible order
survives renames and reorders.
"""

from leap.utils.constants import NOTES_DIR

from leap.monitor.dialogs.notes.persistence import (
    _load_notes_meta, _save_notes_meta,
)


# ── Folder helpers ──────────────────────────────────────────────────

def _list_folders() -> list[str]:
    """Return all folder paths relative to NOTES_DIR, sorted alphabetically."""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    folders: list[str] = []
    for p in sorted(NOTES_DIR.rglob('*')):
        if p.is_dir():
            folders.append(str(p.relative_to(NOTES_DIR)))
    return folders


def _rename_folder_meta(old_prefix: str, new_prefix: str) -> None:
    """Update metadata keys when a folder is renamed."""
    meta = _load_notes_meta()
    updated: dict = {}
    for key, value in meta.items():
        if key.startswith(old_prefix + '/') or key == old_prefix:
            new_key = new_prefix + key[len(old_prefix):]
            updated[new_key] = value
        else:
            updated[key] = value
    if updated != meta:
        _save_notes_meta(updated)


def _delete_folder_meta(prefix: str) -> None:
    """Remove metadata entries for all notes under a folder."""
    meta = _load_notes_meta()
    keys = [k for k in meta if k.startswith(prefix + '/') or k == prefix]
    if keys:
        for k in keys:
            del meta[k]
        _save_notes_meta(meta)


# ── Item ordering ───────────────────────────────────────────────────

def _load_order() -> dict[str, list[str]]:
    """Load per-folder child ordering from metadata.

    Returns dict mapping folder paths ('' for root) to ordered lists
    of child leaf names (notes and subfolders mixed).
    """
    return _load_notes_meta().get('_order', {})


def _save_order(order: dict[str, list[str]]) -> None:
    """Persist per-folder child ordering."""
    meta = _load_notes_meta()
    if order:
        meta['_order'] = order
    else:
        meta.pop('_order', None)
    _save_notes_meta(meta)


def _rename_in_order(folder: str, old_leaf: str, new_leaf: str) -> None:
    """Rename an item in its parent folder's stored ordering."""
    order = _load_order()
    lst = order.get(folder, [])
    if old_leaf in lst:
        lst[lst.index(old_leaf)] = new_leaf
        order[folder] = lst
        _save_order(order)


def _remove_from_order(folder: str, leaf: str) -> None:
    """Remove *leaf* from *folder*'s stored order list."""
    order = _load_order()
    lst = order.get(folder, [])
    if leaf in lst:
        lst.remove(leaf)
        if lst:
            order[folder] = lst
        else:
            order.pop(folder, None)
        _save_order(order)


def _rename_order_keys(old_prefix: str, new_prefix: str) -> None:
    """Rename a folder's and its sub-folders' keys in the _order dict."""
    order = _load_order()
    changed = False
    if old_prefix in order:
        order[new_prefix] = order.pop(old_prefix)
        changed = True
    pfx = old_prefix + '/'
    for old_k in [k for k in order if k.startswith(pfx)]:
        order[new_prefix + old_k[len(old_prefix):]] = order.pop(old_k)
        changed = True
    if changed:
        _save_order(order)


def _delete_order_keys(prefix: str) -> None:
    """Delete a folder's and its sub-folders' keys from the _order dict."""
    order = _load_order()
    keys = [k for k in order if k == prefix or k.startswith(prefix + '/')]
    if keys:
        for k in keys:
            del order[k]
        _save_order(order)
