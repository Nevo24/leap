"""Atomic write helpers for settings files.

Used by every provider's ``configure_hooks()`` so a concurrent reader
(e.g. the session-start gate calling ``hooks_installed()``) never sees
a half-written settings file mid-rewrite.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int = 2,
    trailing_newline: bool = True,
    **json_kwargs: Any,
) -> None:
    """Write ``data`` as JSON to ``path`` atomically.

    Writes to a temp file in the same directory, fsyncs, then renames
    over the destination.  The rename is atomic on POSIX, so any reader
    sees either the old contents or the new — never a half-written
    truncation.

    Args:
        path: Destination file.
        data: JSON-serialisable value.
        indent: ``json.dump`` indent.
        trailing_newline: Append a final ``\\n`` (matches the existing
            file conventions across providers).
        **json_kwargs: Extra arguments forwarded to ``json.dump``
            (e.g. ``ensure_ascii=False`` for the presets file, which
            must preserve unicode contents byte-exact).

    ``except BaseException`` (not just ``Exception``) so a
    ``KeyboardInterrupt`` mid-write still unlinks the temp file before
    propagating — leaving an orphan ``.<name>.…tmp`` would be a slow
    leak on every Ctrl+C.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent, **json_kwargs)
            if trailing_newline:
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
