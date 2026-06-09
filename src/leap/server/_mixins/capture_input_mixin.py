"""Capture-mode and terminal input-mirror methods for :class:`LeapServer`.

Extracted verbatim from ``server.py`` to shrink that god-class. This is the
"^^" queue-message capture editor plus the terminal input-buffer mirror,
bracketed-paste / image handling, saved-message history, and the stale-CLI
input clearing helpers. Pure method container: all instance state lives in
``LeapServer.__init__`` and is accessed via ``self``; ``LeapServer`` inherits
this mixin so every ``self._capture_*`` / ``self._terminal_*`` call resolves
unchanged.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import termios
import threading
import time
import unicodedata
from typing import Optional, Union

try:
    from AppKit import (
        NSBitmapImageRep,
        NSPasteboard,
        NSPasteboardTypePNG,
        NSPasteboardTypeTIFF,
        NSPNGFileType,
    )
    HAS_APPKIT = True
except ImportError:  # non-macOS or pyobjc missing
    HAS_APPKIT = False

from leap.cli_providers.states import CLIState
from leap.server.state_tracker import CLIStateTracker
from leap.utils.atomic_write import atomic_write_json
from leap.utils.constants import QUEUE_IMAGES_DIR, STORAGE_DIR


class CaptureInputMixin:
    """Capture-mode + terminal input-mirror methods mixed into LeapServer."""

    def _capture_display(self, text: Optional[str] = None) -> None:
        """Show queue-capture buffer on the TUI's input line.

        Writes the text and positions the terminal cursor at the
        capture cursor position so the user sees where they're editing.
        Handles multi-line wrapping: tracks how many terminal lines the
        previous render occupied and clears them before redrawing.
        """
        try:
            # Move up and clear any wrapped lines from previous render
            clear = ''
            if self._capture_prev_lines > 0:
                clear = (f'\r\x1b[K'
                         + (f'\x1b[A\r\x1b[K' * self._capture_prev_lines))

            if text is None:
                # Hide cursor to prevent ghost cursors during the gap
                # between capture-end and the CLI's TUI repaint.
                hide = '\x1b[?25l'
                # The generic `clear` built above walks UP from the
                # cursor position, so it misses wrapped lines that lie
                # BELOW the cursor (e.g. after the user pressed Home).
                # Move down to the last wrapped line first so every
                # overlay line is erased.
                if self._capture_prev_lines > 0:
                    try:
                        cols = shutil.get_terminal_size(
                            fallback=(80, 24)).columns
                        cursor_abs = len('[Leap Q] ') + self._capture_cursor_pos
                        cursor_line = (cursor_abs // cols
                                       if cols > 0 else 0)
                        down_lines = self._capture_prev_lines - cursor_line
                        if down_lines > 0:
                            clear = (f'\x1b[{down_lines}B\r\x1b[K'
                                     + f'\x1b[A\r\x1b[K'
                                     * self._capture_prev_lines)
                    except Exception:
                        pass
                os.write(sys.stdout.fileno(),
                         (hide + (clear or '\r\x1b[K')).encode())
                self._capture_prev_lines = 0
            else:
                # Replace newlines (from pasted multi-line text) with a
                # visual marker for the single-line display.  The actual
                # capture buffer retains real newlines for the queued msg.
                text = text.replace('\n', '\u23ce')
                q_size = self.queue.size
                prefix = '[Leap Q] '
                hint = (f' \x1b[2m({q_size} queued \u2022 Enter=queue'
                        f' \u2022 !!=force-send next'
                        f' \u2022 Esc=cancel \u2022 ^^=save'
                        f' \u2022 \u2191\u2193=history \u2022 Ctrl+V=image'
                        f' \u2022 CLI runs in bg)\x1b[33m'
                        if self._capture_show_hint else '')
                full_line = f'{prefix}{text}{hint}'
                visible_len = len(re.sub(r'\x1b\[[0-9;]*m', '', full_line))
                cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                wrapped = max(0, (visible_len - 1) // cols) if cols > 0 else 0
                # Position cursor correctly within wrapped text.
                # After writing the full line, the terminal cursor is
                # on the last wrapped line.  Move up to the cursor's
                # line and set the column within that line.
                cursor_abs = len(prefix) + self._capture_cursor_pos
                if cols > 0:
                    cursor_line = cursor_abs // cols
                    cursor_col = cursor_abs % cols
                else:
                    cursor_line = 0
                    cursor_col = cursor_abs
                lines_up = wrapped - cursor_line
                move_up = f'\x1b[{lines_up}A' if lines_up > 0 else ''
                move_right = f'\x1b[{cursor_col}C' if cursor_col > 0 else ''
                payload = (
                    f"{clear}\r\x1b[K"
                    f"\x1b[33m{prefix}{text}{hint}\x1b[0m"
                    f"{move_up}\r{move_right}"
                    f"\x1b[?25h"
                ).encode()
                os.write(sys.stdout.fileno(), payload)
                self._capture_prev_lines = wrapped
        except OSError:
            pass

    def _capture_text(self) -> str:
        """Decode the capture buffer as a string."""
        return self._queue_capture_buf.decode('utf-8', errors='replace')

    def _capture_insert(self, ch: str) -> None:
        """Insert character(s) at the cursor position."""
        self._saved_msg_index = -1  # editing resets history browsing
        text = self._capture_text()
        text = text[:self._capture_cursor_pos] + ch + text[self._capture_cursor_pos:]
        self._queue_capture_buf = bytearray(text.encode('utf-8'))
        self._capture_cursor_pos += len(ch)

    # -- Saved message history ------------------------------------------------

    _SAVED_MESSAGES_FILE = STORAGE_DIR / 'saved_messages.json'
    _SAVED_MESSAGES_MAX = 100

    def _load_saved_messages(self) -> list[str]:
        """Load saved messages from disk."""
        try:
            if self._SAVED_MESSAGES_FILE.exists():
                data = json.loads(self._SAVED_MESSAGES_FILE.read_text())
                if isinstance(data, list):
                    return data[-self._SAVED_MESSAGES_MAX:]
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _persist_saved_messages(self) -> None:
        """Write saved messages to disk."""
        try:
            atomic_write_json(
                self._SAVED_MESSAGES_FILE,
                self._saved_messages[-self._SAVED_MESSAGES_MAX:],
            )
        except OSError:
            pass

    def _save_capture_message(self) -> None:
        """Save current capture buffer to history, clear buffer."""
        msg = self._capture_text().strip()
        if not msg:
            return
        # Expand [Paste #N] placeholders FIRST — a recalled paste may
        # contain [Image #M] tokens inside its raw content, which the
        # subsequent image resolution must see.
        if self._paste_text_map:
            msg = self._capture_resolve_pastes(msg)
        # Resolve image placeholders → @path refs, preserving the
        # text-image interleaving so recalled messages read the way
        # the user typed them.
        if self._capture_image_map:
            msg = self._capture_resolve_images(msg)
        # Remove duplicate if already at the end
        if self._saved_messages and self._saved_messages[-1] == msg:
            pass
        else:
            self._saved_messages.append(msg)
            if len(self._saved_messages) > self._SAVED_MESSAGES_MAX:
                self._saved_messages = self._saved_messages[
                    -self._SAVED_MESSAGES_MAX:]
        self._persist_saved_messages()
        # Clear buffer and show saved hint
        self._queue_capture_buf.clear()
        self._capture_cursor_pos = 0
        self._capture_utf8_buf.clear()
        self._saved_msg_index = -1
        self._capture_show_hint = False
        self._capture_display()  # clear old wrapped lines
        self._capture_prev_lines = 0
        # NOTE: intentionally do NOT reset _capture_initial_text here.
        # After save, the buffer is empty but initial still holds the
        # pre-capture content.  cancel's ``was_edited`` check will see
        # capture != initial and run the slow path, which clears
        # Claude's CLI + sends the (empty / newly-typed) content —
        # exactly what the user wants for save+Esc and save+type+Esc.
        # Show a "Saved!" hint on the capture line
        try:
            payload = (
                '\r\x1b[K'
                '\x1b[33m[Leap Q] \x1b[32mSaved!'
                ' \x1b[2m(any key to continue \u2022 \u2191\u2193 to browse)\x1b[0m'
            ).encode()
            os.write(sys.stdout.fileno(), payload)
        except OSError:
            pass
        self._capture_show_saved_hint = True

    def _capture_display_force_confirm(self) -> None:
        """Display the force-send confirmation prompt."""
        try:
            payload = (
                '\r\x1b[K'
                '\x1b[33m[Leap Q] \x1b[0m'
                'Force-send next queued message'
                ' \x1b[2m- Enter to confirm • any key to cancel\x1b[0m'
            ).encode()
            os.write(sys.stdout.fileno(), payload)
        except OSError:
            pass

    def _browse_saved_history(self, direction: int) -> None:
        """Browse saved messages. direction: -1=up (older), +1=down (newer)."""
        if not self._saved_messages:
            return
        count = len(self._saved_messages)
        if self._saved_msg_index == -1:
            # Not browsing yet
            if direction == -1:
                # Start at most recent
                self._saved_msg_index = count - 1
            else:
                return  # Already past end, nothing to do
        else:
            new_idx = self._saved_msg_index + direction
            if new_idx < 0:
                return  # Already at oldest
            if new_idx >= count:
                # Past newest → back to empty buffer
                self._saved_msg_index = -1
                self._queue_capture_buf.clear()
                self._capture_cursor_pos = 0
                self._capture_display(self._capture_text())
                return
            self._saved_msg_index = new_idx

        # Load the message at current index, converting @path refs
        # back to [Image #N] placeholders and substantial multi-line
        # text into a [Paste #N] placeholder so browsing stays
        # scannable.  Original paste boundaries aren't preserved, so
        # a saved message containing multiple pastes collapses into a
        # single placeholder on recall.
        msg = self._saved_messages[self._saved_msg_index]
        msg = self._capture_unresolve_images(msg)
        msg = self._capture_unresolve_pastes(msg)
        self._queue_capture_buf = bytearray(msg.encode('utf-8'))
        self._capture_cursor_pos = len(msg)
        # Update initial text to the recalled content so cancel's
        # fast-path edit detection (``capture_text vs initial_text``)
        # compares against what the user sees after recall, not the
        # stale pre-recall state.  Without this, editing a recalled
        # message always falls to the slow clear+re-paste round-trip.
        self._capture_initial_text = self._capture_text()
        self._capture_display(self._capture_text())

    @staticmethod
    def _is_csi_u_cancel(seq: bytes) -> bool:
        """Check if a CSI sequence is Ctrl+C in kitty/xterm encoding."""
        return CLIStateTracker._is_csi_u_interrupt(seq)

    @staticmethod
    def _is_csi_u_paste(seq: bytes) -> bool:
        """Check if a CSI sequence is Ctrl+V in any known encoding."""
        if len(seq) < 4:
            return False
        final = seq[-1]
        params = seq[2:-1]
        parts = params.split(b';')
        try:
            if final == 0x75:  # Kitty: \x1b[118;5u
                cp = int(parts[0].split(b':')[0])
                mod = int(parts[1].split(b':')[0]) if len(parts) > 1 else 1
                return cp == 118 and (mod - 1) & 0x04 != 0
            if final == 0x7e and len(parts) >= 3:  # Legacy: \x1b[27;5;118~
                prefix = int(parts[0].split(b':')[0])
                mod = int(parts[1].split(b':')[0])
                keycode = int(parts[2].split(b':')[0])
                return prefix == 27 and keycode == 118 and (mod - 1) & 0x04 != 0
        except (ValueError, IndexError):
            pass
        return False

    @staticmethod
    def _is_csi_u_newline(seq: bytes) -> bool:
        """Check if a CSI sequence is Shift/Cmd+Enter (newline-in-input).

        Kitty: ``\\x1b[13;<mod>u`` with mod != 1 (mod 1 = no modifier
        i.e. plain Enter).  Legacy xterm: ``\\x1b[27;<mod>;13~``.
        These sequences are emitted by terminals (iTerm2, WezTerm,
        VS Code via the Leap extension) when CSI u keyboard
        encoding is active and the user wants to insert a newline
        in the CLI's input box without submitting.
        """
        if len(seq) < 4:
            return False
        final = seq[-1]
        params = seq[2:-1]
        parts = params.split(b';')
        try:
            if final == 0x75:  # Kitty: \x1b[13;<mod>u
                cp = int(parts[0].split(b':')[0])
                mod = int(parts[1].split(b':')[0]) if len(parts) > 1 else 1
                return cp == 13 and mod != 1
            if final == 0x7e and len(parts) >= 3:  # \x1b[27;<mod>;13~
                prefix = int(parts[0].split(b':')[0])
                mod = int(parts[1].split(b':')[0])
                keycode = int(parts[2].split(b':')[0])
                return prefix == 27 and keycode == 13 and mod != 1
        except (ValueError, IndexError):
            pass
        return False

    def _capture_backspace(self) -> bool:
        """Delete character before cursor. Returns False if at start.

        Treats ``[Paste #N]`` / ``[Image #N]`` placeholders atomically:
        if the cursor sits immediately after one, the whole token is
        removed in a single backspace — preventing users from breaking
        a placeholder by editing inside it.
        """
        if self._capture_cursor_pos <= 0:
            return False
        self._saved_msg_index = -1  # editing resets history browsing
        text = self._capture_text()
        ph_end = self._capture_cursor_pos
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_start = ph_end - len(ph)
                if ph_start >= 0 and text[ph_start:ph_end] == ph:
                    text = text[:ph_start] + text[ph_end:]
                    self._queue_capture_buf = bytearray(
                        text.encode('utf-8'))
                    self._capture_cursor_pos = ph_start
                    return True
        text = text[:self._capture_cursor_pos - 1] + text[self._capture_cursor_pos:]
        self._queue_capture_buf = bytearray(text.encode('utf-8'))
        self._capture_cursor_pos -= 1
        return True

    def _capture_delete(self) -> None:
        """Delete character at cursor (forward delete).

        Atomic placeholder handling: if the cursor sits at the start
        of a ``[Paste #N]`` / ``[Image #N]`` token, delete the whole
        token as one operation.
        """
        text = self._capture_text()
        if self._capture_cursor_pos < len(text):
            self._saved_msg_index = -1  # editing resets history browsing
            ph_start = self._capture_cursor_pos
            for ph_map in (self._paste_text_map, self._capture_image_map):
                for ph in ph_map:
                    ph_end = ph_start + len(ph)
                    if text[ph_start:ph_end] == ph:
                        text = text[:ph_start] + text[ph_end:]
                        self._queue_capture_buf = bytearray(
                            text.encode('utf-8'))
                        return
            text = text[:self._capture_cursor_pos] + text[self._capture_cursor_pos + 1:]
            self._queue_capture_buf = bytearray(text.encode('utf-8'))

    def _gc_paste_text_map(self) -> None:
        """Drop ``_paste_text_map`` entries no longer referenced by
        any live buffer.

        Without this, the dict accumulates entries for the lifetime of
        the server — every paste >200 chars or with embedded newlines
        gets a permanent entry, so a long-running session leaks paste
        content.  Called at known-safe points (Enter / Ctrl+C outside
        capture, after each ``_send_to_cli``) where the live buffers
        have just settled, so anything not referenced is genuinely
        orphaned.
        """
        if not self._paste_text_map:
            return
        live = b''.join(
            bytes(b)
            for b in (
                self._terminal_input_buf,
                self._capture_pre_input_buf,
                self._preserved_input_buf,
                self._queue_capture_buf,
            )
        )
        for ph in list(self._paste_text_map.keys()):
            if ph.encode('utf-8') not in live:
                del self._paste_text_map[ph]

    @staticmethod
    def _line_cells(line: str) -> int:
        """Approximate terminal cell width of a single line of text.

        Uses ``unicodedata.east_asian_width`` to give CJK Wide/Fullwidth
        characters two cells.  Most emoji are classified Neutral and
        return 1 cell — that under-counts in the safe direction (the
        Ctrl+U clear over-shoots, extra presses are no-ops on an empty
        line), so a stdlib-only approximation is fine here without
        pulling in the ``wcwidth`` package.
        """
        cells = 0
        for ch in line:
            eaw = unicodedata.east_asian_width(ch)
            cells += 2 if eaw in ('F', 'W') else 1
        return cells

    def _stale_buf_text(
        self, buf: Optional[Union[bytes, bytearray]] = None,
    ) -> str:
        """Decoded buffer with ``[Paste #N]`` placeholders expanded.

        Defaults to ``_capture_pre_input_buf``.  Returns ``''`` for
        an empty buffer.  Snapshots ``_paste_text_map`` via ``list()``
        because other threads (input thread via
        ``_finalize_paste_capture``, auto-sender via
        ``_gc_paste_text_map``) can mutate it concurrently and
        iterating during mutation raises RuntimeError.
        """
        if buf is None:
            buf = self._capture_pre_input_buf
        if not buf:
            return ''
        text = bytes(buf).decode('utf-8', errors='replace')
        for placeholder, raw in list(self._paste_text_map.items()):
            text = text.replace(placeholder, raw)
        return text

    def _stale_visual_rows(
        self, buf: Optional[Union[bytes, bytearray]] = None,
    ) -> int:
        """Visual rows the CLI is rendering for the given input buffer.

        Counts wrapped rows per ``\\n``-separated line at the current
        terminal width.  Cell widths come from ``_line_cells`` so
        CJK Wide/Fullwidth chars contribute two cells each.

        Returns 0 for an empty buffer.
        """
        text = self._stale_buf_text(buf)
        if not text:
            return 0
        try:
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        except OSError:
            cols = 80
        cols = max(1, cols)
        rows = 0
        for line in text.split('\n'):
            line = line.rstrip('\r')
            cells = self._line_cells(line)
            rows += 1 + max(0, (cells - 1) // cols)
        return rows

    def _stale_logical_lines(
        self, buf: Optional[Union[bytes, bytearray]] = None,
    ) -> int:
        """Logical line count of stale CLI input (``\\n``-separated).

        Drives the Ctrl+U + Backspace pattern in
        ``_clear_stale_cli_input``: in IDLE, Ink's Ctrl+U is line-bound
        and does NOT cross newlines, so we need one Ctrl+U per logical
        line plus a Backspace between each pair to delete the joining
        ``\\n`` and place the cursor at the end of the previous line.

        Returns 0 for an empty buffer.
        """
        text = self._stale_buf_text(buf)
        if not text:
            return 0
        return text.count('\n') + 1

    def _clear_stale_cli_input(self, lines: int, rows: int) -> None:
        """Clear stale CLI input left on the TUI before ``^^`` entry.

        Sends ``End`` (cursor to end of input) followed by N
        back-to-back Ctrl+Us, where N covers both interpretations
        of Ink's Ctrl+U behavior plus safety:

        * If Ctrl+U is **line-bound and progresses cursor up after
          each kill**: N Ctrl+Us clears N logical lines.
        * If Ctrl+U is **row-bound** (RUNNING-mode streaming): N
          Ctrl+Us kills N visual rows.

        Either way, ``max(lines, rows) + 3`` Ctrl+Us is enough to
        clear everything plus margin.  Extra Ctrl+Us on empty input
        are no-ops in both Ink and Ratatui.

        RUNNING gets a second ``End`` for drop-defense against the
        streaming render race that can swallow the first.
        """
        if lines <= 0 and rows <= 0:
            return
        self.pty.send('\x1b[F')  # End: cursor to end of input
        time.sleep(0.02)
        if self.state.current_state != CLIState.IDLE:
            self.pty.send('\x1b[F')  # second End: drop-defense
            time.sleep(0.02)
        n = max(lines, rows) + 3
        self.pty.send('\x15' * n)
        time.sleep(0.03)

    def _trigger_sigwinch_repaint(self) -> None:
        """Force Ink to do an immediate full-screen repaint via a
        same-cycle terminal resize.  macOS only sends SIGWINCH when
        the size actually changes, so we shrink by one row, let the
        child handle it, then restore.

        Required for Ink to clear visual residue from the [Leap Q]
        overlay AND to maintain the alternate-screen / full-screen
        layout — without a SIGWINCH after capture exit, Claude's TUI
        fragments over the Leap server welcome screen.
        """
        def _deferred_resize() -> None:
            try:
                cols, rows = shutil.get_terminal_size(fallback=(80, 24))
                self.pty.resize(max(1, rows - 1), cols)
                time.sleep(0.05)
                self.pty.resize(rows, cols)
            except OSError:
                pass
        threading.Thread(target=_deferred_resize, daemon=True).start()

    def _capture_flush(
        self, cancel: bool = False, defer_sigwinch: bool = False,
    ) -> None:
        """End capture mode: handle stale CLI input, force TUI redraw.

        When ``defer_sigwinch=True`` the SIGWINCH-driven Ink full
        repaint is NOT fired here.  Instead, ``_send_to_cli`` fires
        it after the auto-sender's paste-and-submit completes.  This
        keeps the SIGWINCH-induced render output out of the dispatch
        window — without that, the render storm keeps Ink emitting
        bytes that hold ``_wait_for_output_settled`` busy and flip
        Leap's state tracker to RUNNING (gating dispatch), which
        adds many seconds of latency to every queued message on a
        long conversation transcript.
        """
        # Handle stale ^ from cross-chunk ^^ entry.
        if self._capture_stale_caret:
            self._capture_stale_caret = False
            if cancel:
                self.pty.send('\x7f')  # best-effort backspace
            # On send: _send_to_cli's Ctrl+C clears the full line.
        # On cancel (Escape/Ctrl+C), discard the stale count so the
        # text stays on the CLI — the user wants to keep it.
        if cancel:
            self._capture_stale_visual_rows = 0
            self._capture_stale_logical_lines = 0
        # Clear pending caret so a single ^ after exit doesn't
        # accidentally trigger capture mode.
        self._pending_caret = False
        self._queue_capture_mode = False
        # Reset history-recall — the buf is now in a fresh state (empty
        # on submit, restored cancel_text on cancel), and on cancel the
        # ``_capture_cancel`` background thread re-types the text into
        # the CLI; the next ↑ should snapshot whatever ends up on the
        # input line, not whatever was there before ``^^``.
        self._reset_history_recall()
        if defer_sigwinch:
            self._pending_sigwinch = True
            return
        self._trigger_sigwinch_repaint()

    def _save_clipboard_image(self) -> Optional[str]:
        """Save clipboard image to disk and return its path.

        Returns ``None`` when the clipboard has no image or on failure.
        Uses PyObjC (AppKit) directly — no subprocess, so terminal raw
        mode settings are not corrupted.
        """
        if not HAS_APPKIT:
            return None
        pb = NSPasteboard.generalPasteboard()
        png_data = pb.dataForType_(NSPasteboardTypePNG)
        if png_data is None:
            tiff_data = pb.dataForType_(NSPasteboardTypeTIFF)
            if tiff_data is None:
                return None
            try:
                rep = NSBitmapImageRep.imageRepWithData_(tiff_data)
                if rep is None:
                    return None
                png_data = rep.representationUsingType_properties_(NSPNGFileType, None)
                if png_data is None:
                    return None
            except Exception:
                return None
        raw_bytes = bytes(png_data)
        content_hash = hashlib.md5(raw_bytes).hexdigest()[:12]
        QUEUE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        dest = QUEUE_IMAGES_DIR / f'{content_hash}.png'
        if not dest.is_file():
            dest.write_bytes(raw_bytes)
        return str(dest)

    def _capture_paste_image(self) -> bool:
        """Try to paste a clipboard image into the capture buffer."""
        path = self._save_clipboard_image()
        if not path:
            return False
        # Reuse existing placeholder if same image was already pasted
        for existing_ph, existing_path in self._capture_image_map.items():
            if existing_path == path:
                self._capture_insert(existing_ph)
                return True
        self._capture_image_counter += 1
        placeholder = f'[Image #{self._capture_image_counter}]'
        self._capture_image_map[placeholder] = path
        self._capture_insert(placeholder)
        return True

    def _capture_resolve_images(self, message: str) -> str:
        """Replace ``[Image #N]`` placeholders with ``@path`` references.

        Replacement is in-place so the text-image interleaving the
        user typed is preserved on send.  ``_has_image_ref`` detects
        ``@path`` tokens anywhere in the message, so routing through
        the image send protocol is unaffected by position.
        """
        for placeholder, path in self._capture_image_map.items():
            message = message.replace(placeholder, f'@{path}')
        return message

    def _capture_resolve_pastes(self, message: str) -> str:
        """Replace ``[Paste #N]`` placeholders with the raw pasted text.

        Collapsed-paste placeholders stored in ``_paste_text_map`` are
        expanded back to their original multi-line content before the
        message is queued or saved, so downstream consumers (queue,
        dispatcher, history) see the full text.  In-place replacement
        preserves ordering with surrounding text and image refs.
        """
        # Snapshot via list() — auto-sender thread may GC the dict
        # concurrently; iterating during mutation raises RuntimeError.
        for placeholder, text in list(self._paste_text_map.items()):
            message = message.replace(placeholder, text)
        return message

    def _finalize_paste_capture(self) -> None:
        """Called at ``\\x1b[201~`` — collapse large pastes to a placeholder.

        The raw paste bytes have already been accumulated in
        ``_paste_accumulator`` and forwarded to the CLI in real time
        (so Claude's TUI has the full content).  If the paste is
        substantial (has newlines or is long), truncate the printable
        bytes we added to ``_terminal_input_buf`` during the paste
        and replace them with a short ``[Paste #N]`` placeholder —
        ^^ will then capture the placeholder instead of a sprawling
        raw-text buffer.  Short pastes are left as raw text.
        """
        if self._paste_accumulator is None:
            return
        content = bytes(self._paste_accumulator).decode(
            'utf-8', errors='replace')
        self._paste_accumulator = None
        # Sanitize any stray bracketed-paste markers inside the content
        # (e.g. user pasted bracketed-paste output from another TUI).
        # If we re-wrap this content on send, nested markers would
        # confuse Claude's Ink parser and corrupt the message.
        content = content.replace('\x1b[200~', '').replace('\x1b[201~', '')
        is_substantial = (
            '\n' in content or '\r' in content or len(content) > 200
        )
        if not is_substantial:
            return  # leave raw text in buf
        # Remove the paste bytes we inserted at the cursor during
        # the paste, then substitute a placeholder at that position.
        snap_cursor = self._paste_cursor_snapshot
        cur_cursor = self._terminal_input_cursor
        if cur_cursor > snap_cursor:
            del self._terminal_input_buf[snap_cursor:cur_cursor]
            self._terminal_input_cursor = snap_cursor
        self._chars_sent_to_cli = self._paste_chars_snapshot
        placeholder = self._paste_placeholder_for(content)
        self._paste_text_map[placeholder] = content
        ph_bytes = placeholder.encode('utf-8')
        # Insert placeholder at cursor and advance cursor past it.
        self._terminal_input_buf[snap_cursor:snap_cursor] = ph_bytes
        self._terminal_input_cursor = snap_cursor + len(ph_bytes)
        # Count placeholder as 1 visual token on the CLI (matches
        # Claude's own collapsed [Pasted text #N] rendering).
        self._chars_sent_to_cli += 1

    def _capture_unresolve_pastes(self, message: str) -> str:
        """Collapse substantial raw text into a ``[Paste #N]`` placeholder.

        Used when recalling a saved history message: if the message
        has newlines or is long, wrap the whole thing into a fresh
        placeholder stored in ``_paste_text_map`` — so capture display
        shows a short token instead of a sprawling block, keeping the
        browse (↑↓) experience scannable.  Original paste boundaries
        are not preserved in history, so multi-paste saves collapse
        into a single placeholder.  Short single-line messages pass
        through unchanged.
        """
        is_substantial = (
            '\n' in message or '\r' in message or len(message) > 200
        )
        if not is_substantial:
            return message
        # Sanitize any stray bracketed-paste markers — the re-send
        # will wrap in our own markers and nested pairs would confuse
        # Claude's Ink parser.
        message = message.replace(
            '\x1b[200~', '').replace('\x1b[201~', '')
        placeholder = self._paste_placeholder_for(message)
        self._paste_text_map[placeholder] = message
        return placeholder

    @staticmethod
    def _paste_placeholder_for(content: str) -> str:
        """Stable placeholder for a paste: ``[Paste #<hash8>]``.

        The ID is the first 8 hex chars of md5(content) so the same
        content always produces the same placeholder — deduplicating
        repeat pastes and surviving save/recall cycles.
        """
        digest = hashlib.md5(content.encode('utf-8')).hexdigest()[:8]
        return f'[Paste #{digest}]'

    def _capture_unresolve_images(self, message: str) -> str:
        """Replace ``@path`` image refs with ``[Image #N]`` placeholders.

        The reverse of :meth:`_capture_resolve_images`.  Populates
        ``_capture_image_map`` so the placeholders can be resolved back
        when the message is sent or saved.
        """
        images_dir = str(QUEUE_IMAGES_DIR)
        tokens = message.split()
        changed = False
        for i, token in enumerate(tokens):
            if not token.startswith('@'):
                continue
            path_part = token[1:]
            try:
                if not os.path.realpath(path_part).startswith(images_dir):
                    continue
            except (OSError, ValueError):
                continue
            # Check if already mapped (same path from a previous recall)
            existing_ph = None
            for ph, p in self._capture_image_map.items():
                if p == path_part:
                    existing_ph = ph
                    break
            if existing_ph:
                tokens[i] = existing_ph
            else:
                self._capture_image_counter += 1
                placeholder = f'[Image #{self._capture_image_counter}]'
                self._capture_image_map[placeholder] = path_part
                tokens[i] = placeholder
            changed = True
        return ' '.join(tokens) if changed else message

    def _capture_reset_images(self) -> None:
        """Reset image state for the next capture session."""
        self._capture_image_counter = 0
        self._capture_image_map.clear()

    def _terminal_cursor_left(self) -> None:
        """Move the mirrored CLI cursor one step left, atomic over placeholders.

        Skips back over UTF-8 continuation bytes so cursor never lands
        in the middle of a multi-byte character.
        """
        buf = self._terminal_input_buf
        pos = self._terminal_input_cursor
        if pos <= 0:
            self._terminal_input_cursor = 0
            return
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_bytes = ph.encode('utf-8')
                start = pos - len(ph_bytes)
                if start >= 0 and bytes(buf[start:pos]) == ph_bytes:
                    self._terminal_input_cursor = start
                    return
        # Step back one char — may be multiple bytes for UTF-8.
        new_pos = pos - 1
        while new_pos > 0 and (buf[new_pos] & 0xC0) == 0x80:
            new_pos -= 1
        self._terminal_input_cursor = new_pos

    def _terminal_cursor_right(self) -> None:
        """Move the mirrored CLI cursor one step right, atomic over placeholders.

        Skips forward over UTF-8 continuation bytes.
        """
        buf = self._terminal_input_buf
        buf_len = len(buf)
        pos = self._terminal_input_cursor
        if pos >= buf_len:
            self._terminal_input_cursor = buf_len
            return
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_bytes = ph.encode('utf-8')
                end = pos + len(ph_bytes)
                if end <= buf_len and bytes(buf[pos:end]) == ph_bytes:
                    self._terminal_input_cursor = end
                    return
        # Step forward one char — handle UTF-8 lead byte → skip continuations.
        lead = buf[pos]
        if lead < 0x80:
            char_len = 1
        elif lead & 0xE0 == 0xC0:
            char_len = 2
        elif lead & 0xF0 == 0xE0:
            char_len = 3
        elif lead & 0xF8 == 0xF0:
            char_len = 4
        else:
            char_len = 1  # invalid lead byte, bail
        self._terminal_input_cursor = min(buf_len, pos + char_len)

    def _terminal_buf_insert(self, b: int) -> None:
        """Insert a byte at the mirrored cursor position."""
        pos = self._terminal_input_cursor
        self._terminal_input_buf.insert(pos, b)
        self._terminal_input_cursor = pos + 1

    def _terminal_buf_delete_forward(self) -> None:
        """Delete the char at the cursor (forward Delete key).

        Atomic over placeholders: if the cursor is at the start of a
        token, the whole token is removed.
        """
        buf = self._terminal_input_buf
        pos = self._terminal_input_cursor
        if pos >= len(buf):
            return
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_bytes = ph.encode('utf-8')
                end = pos + len(ph_bytes)
                if end <= len(buf) and bytes(buf[pos:end]) == ph_bytes:
                    del buf[pos:end]
                    return
        # Single char delete — find UTF-8 char length.
        lead = buf[pos]
        if lead < 0x80:
            char_len = 1
        elif lead & 0xE0 == 0xC0:
            char_len = 2
        elif lead & 0xF0 == 0xE0:
            char_len = 3
        elif lead & 0xF8 == 0xF0:
            char_len = 4
        else:
            char_len = 1
        del buf[pos:pos + char_len]

    def _terminal_buf_backspace(self) -> None:
        """Delete the char before the cursor (UTF-8-aware, placeholder-atomic).

        If the cursor sits immediately after a ``[Paste #N]`` or
        ``[Image #N]`` placeholder, the whole placeholder is deleted
        as one unit so the token can't be corrupted by a stray
        backspace.
        """
        buf = self._terminal_input_buf
        pos = self._terminal_input_cursor
        if pos <= 0:
            return
        # Atomic placeholder check — if cursor ends a placeholder,
        # remove the whole token.
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_bytes = ph.encode('utf-8')
                start = pos - len(ph_bytes)
                if start >= 0 and bytes(buf[start:pos]) == ph_bytes:
                    del buf[start:pos]
                    self._terminal_input_cursor = start
                    return
        # Strip trailing UTF-8 continuation bytes before the cursor.
        while (self._terminal_input_cursor > 0
               and (buf[self._terminal_input_cursor - 1]
                    & 0xC0) == 0x80):
            del buf[self._terminal_input_cursor - 1]
            self._terminal_input_cursor -= 1
        if self._terminal_input_cursor > 0:
            del buf[self._terminal_input_cursor - 1]
            self._terminal_input_cursor -= 1

    def _resolve_chunk_for_cancel(
        self,
        chunk: str,
        cancel_paste_map: dict,
        cancel_image_map: Optional[dict] = None,
    ) -> str:
        """Render a prefix/suffix chunk as ready-to-send PTY bytes.

        Each ``[Paste #N]`` placeholder is replaced with its own
        bracketed-paste marker block.  Each ``[Image #N]`` placeholder
        is replaced with its ``@path`` string.  Plain text runs
        between placeholders wrap in bracketed-paste markers when
        they contain ``\\n``/``\\r`` so Claude's Ink treats those
        bytes as paste content, not submit-Enters.

        ``cancel_image_map`` is a snapshot taken BEFORE the caller
        resets the live image map — without it the image map would
        already be empty by the time this helper runs.
        """
        if not chunk:
            return ''
        image_map = (cancel_image_map
                     if cancel_image_map is not None
                     else self._capture_image_map)
        # Collect all placeholder spans in order of appearance.
        spans: list[tuple[int, int, str]] = []  # (start, end, payload)
        for ph, content in cancel_paste_map.items():
            start = 0
            while True:
                i = chunk.find(ph, start)
                if i < 0:
                    break
                spans.append(
                    (i, i + len(ph),
                     '\x1b[200~' + content + '\x1b[201~'),
                )
                start = i + len(ph)
        for ph, path in image_map.items():
            start = 0
            while True:
                i = chunk.find(ph, start)
                if i < 0:
                    break
                spans.append((i, i + len(ph), '@' + path))
                start = i + len(ph)
        spans.sort(key=lambda s: s[0])
        # Emit text between spans, wrapping multi-line runs.
        def _wrap_run(run: str) -> str:
            # Strip any embedded bracketed-paste markers so a wrap
            # here doesn't produce nested pairs that confuse Ink.
            safe = run.replace(
                '\x1b[200~', '').replace('\x1b[201~', '')
            if '\n' in safe or '\r' in safe:
                return '\x1b[200~' + safe + '\x1b[201~'
            return safe
        result: list[str] = []
        cursor = 0
        for start, end, payload in spans:
            if start > cursor:
                result.append(_wrap_run(chunk[cursor:start]))
            result.append(payload)
            cursor = end
        if cursor < len(chunk):
            result.append(_wrap_run(chunk[cursor:]))
        return ''.join(result)

    def _capture_cancel(self) -> None:
        """Cancel capture mode — transfer text back to CLI input."""
        self._capture_display()
        capture_text = self._capture_text()
        pre_text = self._capture_pre_input_buf.decode(
            'utf-8', errors='replace')
        # Detect edits using the placeholder-form text (what the user
        # saw in capture), not the resolved text — otherwise an
        # unchanged paste-placeholder would look "different" after
        # expansion to its raw content.
        was_edited = capture_text != self._capture_initial_text
        # For cancel we resolve images (→ @path) but leave
        # [Paste #N] placeholders intact.  At send time below, each
        # placeholder is replaced by its OWN bracketed-paste marker
        # block so every paste re-appears as a separate collapsed
        # label on Claude's side, with typed text between them
        # appearing as literal chars.  (Wrapping the whole re-type
        # in a single pair of markers — as we used to — caused
        # typed text like "hello" to vanish into the paste label.)
        resolved_text = capture_text
        # Snapshot paste map entries that are actually referenced by
        # the current capture text.  We need these even though we
        # reset the image map below — the expansion happens inside
        # the background thread, after _capture_reset_images runs.
        cancel_paste_map = {
            ph: text
            for ph, text in self._paste_text_map.items()
            if ph in capture_text
        }
        had_pastes = bool(cancel_paste_map)
        # Snapshot the image map BEFORE _capture_reset_images clears
        # it below — the fast path's chunk resolver runs later and
        # needs to see the mappings that existed at cancel time.
        cancel_image_map = dict(self._capture_image_map)
        if self._capture_image_map:
            resolved_text = self._capture_resolve_images(resolved_text)
        has_images = bool(self._capture_image_map)
        self._pending_bang = False
        self._capture_force_confirm = False
        self._queue_capture_buf.clear()
        self._capture_cursor_pos = 0
        self._capture_utf8_buf.clear()
        self._queue_capture_mode = False
        self._capture_flush(cancel=True)
        self._capture_reset_images()
        # Typed text between placeholders must not contain \n (would
        # auto-submit); pastes' \n is safe because it lives inside
        # the placeholder and gets wrapped in paste markers below.
        safe_text = resolved_text.replace('\n', ' ')
        if has_images:
            # Images present — send resolved text to CLI.
            # But if resolved text is empty (user deleted all images
            # and text), just restore the pre-capture state.
            if not safe_text:
                self._terminal_input_buf = bytearray(
                    self._capture_pre_input_buf)
                self._terminal_input_cursor = min(
                    self._capture_pre_input_cursor,
                    len(self._terminal_input_buf),
                )
                self._chars_sent_to_cli = self._capture_pre_chars_sent
                return
            cancel_text = safe_text
        else:
            # No images — skip re-type when the capture buffer matches
            # the initial state (user didn't edit after ^^).
            if not was_edited:
                self._terminal_input_buf = bytearray(
                    self._capture_pre_input_buf)
                self._terminal_input_cursor = min(
                    self._capture_pre_input_cursor,
                    len(self._terminal_input_buf),
                )
                self._chars_sent_to_cli = self._capture_pre_chars_sent
                return
            cancel_text = safe_text
        self._capture_cancel_pending = True
        # Hold user keystrokes during the cancel send so they can't
        # interleave with our clear + bracketed-paste bytes on the
        # PTY — held bytes replay on the next filter call after the
        # send completes (see _queue_sending_held).  Only flip the
        # flag if it wasn't already set (the queue dispatcher uses
        # the same flag), so we don't clobber its reset.
        held_queue_sending = not self._queue_sending
        if held_queue_sending:
            self._queue_sending = True

        pre_chars = self._capture_pre_chars_sent
        # Logical lines + visual rows for the cancel slow-path clear.
        # Compute here (before the thread spawns) so the closure
        # captures stable values — _capture_pre_input_buf is still
        # intact at this point (only re-set on the next capture entry).
        pre_lines = self._stale_logical_lines(self._capture_pre_input_buf)
        pre_rows = self._stale_visual_rows(self._capture_pre_input_buf)
        # Fast path: if the capture buffer is the initial text
        # surrounded by new prefix/suffix chunks (initial text
        # itself untouched), transfer ONLY the added chunks to the
        # CLI and leave Claude's existing input untouched.  Avoids
        # the clear + re-paste round-trip whose bracketed-paste
        # start markers can race-drop under streaming and cause the
        # original paste to vanish.
        #
        # Each chunk is resolved in place:
        #   [Image #N]   → @path (Claude treats as attachment ref)
        #   [Paste #N]   → bracketed-paste block wrapping raw content
        #   plain \n/\r  → the run of plain text between placeholders
        #                  wraps in bracketed-paste markers so its
        #                  newlines don't submit-Enter.
        # Prefix chunks are bracketed by Home (\x1b[H) and End
        # (\x1b[F) so Claude inserts them before its existing
        # attachment and leaves cursor at end of line.
        initial = self._capture_initial_text
        fast_path_payload: Optional[str] = None
        if (initial in capture_text
                and capture_text != initial):
            # Works even when initial == "" (user entered Leap Q on an
            # empty CLI): before == "" and after == capture_text, so
            # the whole content becomes a clean bracketed-paste block
            # instead of the slow path's \n→space flattening.
            idx = capture_text.find(initial) if initial else 0
            before = capture_text[:idx]
            after = capture_text[idx + len(initial):]
            before_payload = self._resolve_chunk_for_cancel(
                before, cancel_paste_map, cancel_image_map)
            after_payload = self._resolve_chunk_for_cancel(
                after, cancel_paste_map, cancel_image_map)
            parts: list[str] = []
            if before_payload:
                # Home → insert payload before original → End.
                parts.append('\x1b[H' + before_payload + '\x1b[F')
            if after_payload:
                parts.append(after_payload)
            if parts:
                fast_path_payload = ''.join(parts)

        def _apply_cancel_text() -> None:
            try:
                if fast_path_payload is not None:
                    # Claude's input already shows the original content.
                    # Just type the new prefix/suffix chunks around it;
                    # no clear, no re-paste of the original.
                    self.pty.send(fast_path_payload)
                    return
                # Slow path: full clear + re-paste round-trip.
                # Clear Claude's CLI input regardless of state.  During
                # RUNNING, Ctrl+U alone can race with Ink's render loop,
                # so _clear_stale_cli_input adds N backspaces as an
                # idempotent fallback.
                if pre_chars > 0:
                    self._clear_stale_cli_input(pre_lines, pre_rows)
                    time.sleep(0.1)
                if cancel_text:
                    text_to_send = cancel_text
                    if had_pastes:
                        # Replace each [Paste #N] individually with
                        # its own bracketed-paste marker block so
                        # Claude re-collapses each paste as its own
                        # label and preserves typed text in between.
                        for ph, paste_content in cancel_paste_map.items():
                            text_to_send = text_to_send.replace(
                                ph,
                                '\x1b[200~' + paste_content + '\x1b[201~',
                            )
                    self.pty.send(text_to_send)
            except OSError:
                pass
            finally:
                if held_queue_sending:
                    self._queue_sending = False
                self._capture_cancel_pending = False
        self._terminal_input_buf = bytearray(
            cancel_text.encode('utf-8'))
        self._terminal_input_cursor = len(self._terminal_input_buf)
        self._chars_sent_to_cli = len(cancel_text)
        threading.Thread(
            target=_apply_cancel_text, daemon=True).start()

    def _enter_capture_mode(self, stale_cli_input: bool,
                            stale_caret: bool) -> None:
        """Enter queue-capture mode with the current input buffer."""
        # Wait for any pending cancel-text send to finish so the CLI
        # has the correct text before we snapshot it.
        if self._capture_cancel_pending:
            deadline = time.time() + 0.3
            while self._capture_cancel_pending and time.time() < deadline:
                time.sleep(0.01)
        # Clean slate for image tracking — previous capture sessions
        # (especially cancelled ones or exceptions) may have left
        # stale entries in the map.
        self._capture_reset_images()
        # Snapshot the pre-capture input so _capture_cancel can restore
        # it if the user toggles back out without sending.
        self._capture_pre_input_buf = bytearray(self._terminal_input_buf)
        self._capture_pre_chars_sent = self._chars_sent_to_cli  # for cancel restore
        # Snapshot the terminal cursor so a no-edit Esc can restore it.
        self._capture_pre_input_cursor = self._terminal_input_cursor
        self._queue_capture_buf = bytearray(self._terminal_input_buf)
        # Map the terminal-buf byte cursor onto the decoded
        # capture-text char cursor so Leap Q opens with the cursor
        # at the same position Claude was showing.
        try:
            prefix = self._terminal_input_buf[
                :self._terminal_input_cursor]
            self._capture_cursor_pos = len(
                prefix.decode('utf-8', errors='replace'))
        except Exception:
            self._capture_cursor_pos = len(self._capture_text())
        self._terminal_input_buf.clear()
        self._terminal_input_cursor = 0
        self._queue_capture_mode = True
        self._capture_show_hint = True
        self._capture_stale_caret = stale_caret
        # Only count chars actually sent to CLI (not held during RUNNING).
        # Snapshot pre-capture row + line counts.  Visual rows drive
        # the walk-up below + RUNNING-mode Ctrl+U sequence; logical
        # lines drive the IDLE-mode Ctrl+U + Backspace pattern.
        # Add one per pending Ctrl+V image: those don't appear in
        # _terminal_input_buf (Ctrl+V outside capture saves the image
        # to _pending_paste_images without inserting bytes), but the
        # CLI still rendered an image-attachment row+line for each.
        n_images = len(self._pending_paste_images)
        self._capture_stale_visual_rows = (
            (self._stale_visual_rows() + n_images)
            if stale_cli_input else 0
        )
        self._capture_stale_logical_lines = (
            (self._stale_logical_lines() + n_images)
            if stale_cli_input else 0
        )
        self._chars_sent_to_cli = 0
        # tcflush to discard any stale text still in the PTY buffer.
        if self._capture_stale_visual_rows > 0:
            try:
                termios.tcflush(self.pty.process.child_fd,
                                termios.TCOFLUSH)
            except Exception:
                pass
        self._pending_caret = False
        self._capture_prev_lines = 0
        # Clear the wrap rows that the CLI rendered for the pre-capture
        # text — without this, a long typed or pasted message that
        # spans multiple terminal rows leaves its upper rows visible
        # above the [Leap Q] line (which only clears its own current
        # row).  Walk up clearing, then walk back down so
        # _capture_display below draws on the original cursor row,
        # not the top of the cleared region.
        #
        # ``rows_above`` is the count of visual rows ABOVE the cursor
        # row, so we subtract 1 from the total visual-row count of the
        # stale input (the cursor sits on the bottom row).  The
        # half-viewport safety cap bounds the worst case: a 1000-line
        # paste can't blank more than half the visible terminal — any
        # cosmetic over-clear self-heals via the SIGWINCH-triggered
        # full repaint that ``_capture_flush`` schedules on submit /
        # cancel.
        rows_above = max(0, self._capture_stale_visual_rows - 1)
        if rows_above > 0:
            try:
                term = shutil.get_terminal_size(fallback=(80, 24))
                rows_above = min(rows_above, max(1, term.lines // 2))
                walk_up = '\x1b[A\r\x1b[K' * rows_above
                walk_down = f'\x1b[{rows_above}B'
                os.write(
                    sys.stdout.fileno(),
                    (walk_up + walk_down).encode(),
                )
            except OSError:
                pass
        self._saved_msg_index = -1
        self._capture_show_saved_hint = False
        # Inject clipboard images saved by prior Ctrl+V presses.
        # Each entry carries the BYTE offset from ``_terminal_input_buf``
        # at the time Ctrl+V fired — we convert to CHAR offset in the
        # decoded capture text so images land at the right position
        # for multi-byte UTF-8 content.
        # Entries with pos=-1 go at the end.
        if self._pending_paste_images:
            text = self._capture_text()
            text_len = len(text)
            # Build byte→char mapping from the capture buffer (which is
            # a copy of terminal_input_buf pre-clear, so positions
            # still line up with what was recorded at Ctrl+V time).
            buf_bytes = bytes(self._queue_capture_buf)

            def _byte_to_char(byte_pos: int) -> int:
                byte_pos = max(0, min(byte_pos, len(buf_bytes)))
                try:
                    return len(buf_bytes[:byte_pos].decode(
                        'utf-8', errors='replace'))
                except Exception:
                    return byte_pos

            # Split into positioned (pos >= 0) and end-append (pos < 0).
            # Track original index for stable ordering at same position.
            positioned: list[tuple[int, int, str]] = []
            at_end: list[str] = []
            for idx, (pos, path) in enumerate(self._pending_paste_images):
                if pos >= 0:
                    char_pos = _byte_to_char(pos)
                    positioned.append(
                        (min(char_pos, text_len), idx, path))
                else:
                    at_end.append(path)

            # Two-pass injection: assign counters left-to-right so
            # #1 is the leftmost image, then insert right-to-left so
            # earlier offsets stay valid after each insertion.
            positioned.sort(key=lambda x: (x[0], x[1]))
            placeholders: list[tuple[int, str]] = []
            for pos, _, path in positioned:
                ph = None
                for k, v in self._capture_image_map.items():
                    if v == path:
                        ph = k
                        break
                if not ph:
                    self._capture_image_counter += 1
                    ph = f'[Image #{self._capture_image_counter}]'
                    self._capture_image_map[ph] = path
                placeholders.append((pos, ph))
            for pos, ph in reversed(placeholders):
                text = text[:pos] + ph + text[pos:]

            # Append end-positioned images (from cancel round-trip).
            for path in at_end:
                ph = None
                for k, v in self._capture_image_map.items():
                    if v == path:
                        ph = k
                        break
                if not ph:
                    self._capture_image_counter += 1
                    ph = f'[Image #{self._capture_image_counter}]'
                    self._capture_image_map[ph] = path
                text += ph

            self._queue_capture_buf = bytearray(text.encode('utf-8'))
            self._capture_cursor_pos = len(text)
            self._pending_paste_images.clear()
            self._capture_show_hint = False
        self._capture_initial_text = self._capture_text()
        self._capture_display(self._capture_initial_text)

    def _capture_cursor_left(self, pos: int) -> int:
        """One-step Left that skips over a placeholder as one unit."""
        if pos <= 0:
            return 0
        text = self._capture_text()
        # If cursor is right after a placeholder, jump to before it.
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                start = pos - len(ph)
                if start >= 0 and text[start:pos] == ph:
                    return start
        return pos - 1

    def _capture_cursor_right(self, pos: int) -> int:
        """One-step Right that skips over a placeholder as one unit."""
        text = self._capture_text()
        if pos >= len(text):
            return pos
        # If cursor is at the start of a placeholder, jump past it.
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                end = pos + len(ph)
                if text[pos:end] == ph:
                    return end
        return pos + 1

    def _capture_word_move(self, direction: int) -> None:
        """Move capture cursor by one word. direction: -1=left, +1=right.

        Placeholders (``[Paste #N]``, ``[Image #N]``) are treated as
        single atomic word-units so Opt+Left/Right never lands the
        cursor inside a placeholder (which would otherwise happen
        because placeholders contain a space between ``Paste``/``#``).
        """
        text = self._capture_text()
        p = self._capture_cursor_pos

        def _placeholder_at(pos: int, rev: bool) -> Optional[int]:
            """Return opposite end of a placeholder adjacent to pos."""
            for ph_map in (self._paste_text_map,
                           self._capture_image_map):
                for ph in ph_map:
                    if rev:
                        start = pos - len(ph)
                        if start >= 0 and text[start:pos] == ph:
                            return start
                    else:
                        end = pos + len(ph)
                        if text[pos:end] == ph:
                            return end
            return None

        if direction < 0:
            # Skip trailing spaces.
            while p > 0 and text[p - 1] == ' ':
                p -= 1
            # Jump over placeholder if one ends here, else skip word.
            ph_start = _placeholder_at(p, rev=True)
            if ph_start is not None:
                p = ph_start
            else:
                while p > 0 and text[p - 1] != ' ':
                    p -= 1
                    # But don't stop inside a placeholder.
                    ph_start2 = _placeholder_at(p, rev=True)
                    if ph_start2 is not None and ph_start2 < p:
                        p = ph_start2
                        break
        else:
            # Jump over placeholder if one starts here, else skip word.
            ph_end = _placeholder_at(p, rev=False)
            if ph_end is not None:
                p = ph_end
            else:
                while p < len(text) and text[p] != ' ':
                    p += 1
                    ph_end2 = _placeholder_at(p, rev=False)
                    if ph_end2 is not None:
                        p = ph_end2
                        break
            # Skip trailing spaces.
            while p < len(text) and text[p] == ' ':
                p += 1
        self._capture_cursor_pos = p
        self._capture_display(text)

    def _capture_handle_escape(self, seq: bytes,
                               is_standalone_esc: bool) -> None:
        """Handle an escape sequence while in capture mode.

        Dispatches editing keys (arrows, Home/End, Delete, word
        movement), cancels on standalone Escape or CSI-u Ctrl+C,
        and silently drops unrecognized sequences.
        """
        if self._capture_show_saved_hint:
            self._capture_show_saved_hint = False
            self._capture_display(self._capture_text())
        if self._capture_force_confirm:
            self._capture_force_confirm = False
            self._pending_bang = False
            self._capture_display(self._capture_text())
            return
        if seq in (b'\x1bb', b'\x1bf'):
            # Meta word movement (ESC-b / ESC-f)
            self._capture_word_move(-1 if seq == b'\x1bb' else 1)
        elif is_standalone_esc:
            self._capture_cancel()
        elif self._is_csi_u_cancel(seq):
            self._capture_cancel()
        elif seq == b'\x1b[D':  # Left arrow — jumps over placeholders
            self._capture_cursor_pos = self._capture_cursor_left(
                self._capture_cursor_pos)
            self._capture_display(self._capture_text())
        elif seq == b'\x1b[C':  # Right arrow — jumps over placeholders
            self._capture_cursor_pos = self._capture_cursor_right(
                self._capture_cursor_pos)
            self._capture_display(self._capture_text())
        elif seq == b'\x1b[1;3D':  # Opt+Left
            self._capture_word_move(-1)
        elif seq == b'\x1b[1;3C':  # Opt+Right
            self._capture_word_move(1)
        elif seq in (b'\x1b[H', b'\x1b[1~'):  # Home
            self._capture_cursor_pos = 0
            self._capture_utf8_buf.clear()
            self._capture_display(self._capture_text())
        elif seq in (b'\x1b[F', b'\x1b[4~'):  # End
            self._capture_cursor_pos = len(self._capture_text())
            self._capture_display(self._capture_text())
        elif seq == b'\x1b[3~':  # Delete
            self._capture_show_hint = False
            self._capture_delete()
            self._capture_display(self._capture_text())
        elif seq == b'\x1b[A':  # Up arrow — browse saved msgs
            self._capture_show_hint = False
            self._browse_saved_history(-1)
        elif seq == b'\x1b[B':  # Down arrow — browse saved msgs
            self._capture_show_hint = False
            self._browse_saved_history(1)
        elif self._is_csi_u_paste(seq):  # CSI u Ctrl+V
            self._capture_show_hint = False
            if self._capture_paste_image():
                self._capture_display(self._capture_text())
        # Other CSI/OSC/SS3 sequences silently dropped.

    def _flush_pending_caret(self) -> None:
        """Timer callback: flush the held ``^`` to the CLI.

        Called from a background thread after ~200ms if no second
        ``^`` arrived.  Writes the ``^`` directly to the PTY so it
        appears on the CLI's input line.
        """
        if not self._pending_caret:
            return
        self._pending_caret = False
        try:
            self.pty.send('^')
        except OSError:
            pass
        self._terminal_buf_insert(0x5e)
        self._chars_sent_to_cli += 1

    def _detect_paste(self, data: bytes) -> bool:
        """Detect bracketed paste markers in input data.

        Returns True if this chunk contains pasted content.  Also
        updates ``_in_bracketed_paste`` for cross-chunk tracking and
        clears ``_pending_caret`` to prevent a stale ``^`` typed
        before the paste from combining with ``^`` inside it.
        """
        _BP_START = b'\x1b[200~'
        _BP_END = b'\x1b[201~'
        # Use rfind so ``_in_bracketed_paste`` reflects the LAST
        # marker in the chunk — a chunk with ``start…end…start``
        # ends inside a new paste (True), and ``end…start…end``
        # ends outside (False).
        bp_start = data.rfind(_BP_START)
        bp_end = data.rfind(_BP_END)
        chunk_has_paste = (
            self._in_bracketed_paste
            or bp_start >= 0
            or bp_end >= 0
        )
        if bp_start > bp_end:
            self._in_bracketed_paste = True
        elif bp_end > bp_start:
            self._in_bracketed_paste = False
        # (both -1 → no markers, leave flag as-is)
        if chunk_has_paste and self._pending_caret:
            # Flush the held "^" — it was a literal, not a capture
            # trigger.  We can't add to `out` here (no access), so
            # set a flag for the caller to handle.
            self._pending_caret_flush = True
            self._pending_caret = False
        return chunk_has_paste

    def _capture_handle_char(self, b: int, data: bytes, i: int,
                             chunk_has_paste: bool) -> tuple[int, bool]:
        """Process one byte in capture mode.

        Returns ``(new_i, display_dirty)`` — the caller should set
        ``capture_dirty |= display_dirty`` and ``continue``.
        """
        dirty = False

        def _display_or_defer() -> None:
            nonlocal dirty
            if chunk_has_paste:
                dirty = True
            else:
                self._capture_display(self._capture_text())

        # Dismiss "Saved!" hint on any key
        if self._capture_show_saved_hint and b != 0x5e:
            self._capture_show_saved_hint = False
            self._capture_display(self._capture_text())

        # Dismiss force-send confirm on any non-Enter key; clear pending
        # bang on any non-'!' key so it only fires on an immediate double.
        if b not in (0x0d, 0x0a):
            if self._capture_force_confirm:
                self._capture_force_confirm = False
                self._capture_display(self._capture_text())
            if b != 0x21:
                self._pending_bang = False

        if b in (0x0d, 0x0a):  # Enter / LF
            # Detect pasted newlines: bracketed paste markers or
            # fallback — a typed Enter is a tiny chunk (1–2 bytes);
            # pasted multi-line text arrives as a large chunk.
            # Use (len - i) so pre-capture bytes (e.g. "hello^^")
            # don't inflate the count when ^^ and Enter share a chunk.
            if chunk_has_paste or (len(data) - i) > 4:
                # Insert literal newline; skip \n after \r to avoid
                # doubles from \r\n pairs.
                if b == 0x0d:
                    self._capture_insert('\n')
                    dirty = True
                elif b == 0x0a:
                    if not (i > 0 and data[i - 1] == 0x0d):
                        self._capture_insert('\n')
                        dirty = True
            else:
                self._user_has_typed = True
                self._capture_display()  # clear
                if self._capture_force_confirm:
                    # !! confirmed — force-send next queued message
                    self._capture_force_confirm = False
                    self._pending_bang = False
                    message = self.queue.pop()
                    if message:
                        self._send_to_cli(message)
                        self.queue.track_sent(message)
                    self._capture_flush()
                    self._queue_capture_buf.clear()
                    self._capture_cursor_pos = 0
                    self._capture_utf8_buf.clear()
                    self._queue_capture_mode = False
                    self._capture_reset_images()
                    self._terminal_input_buf.clear()
                    self._terminal_input_cursor = 0
                    return i + 1, dirty
                msg = self._capture_text().strip()
                # Pastes first — a recalled paste may embed image
                # placeholders that the subsequent image resolution
                # must see.
                if self._paste_text_map:
                    msg = self._capture_resolve_pastes(msg)
                if self._capture_image_map:
                    msg = self._capture_resolve_images(msg)
                if msg:
                    # Detect REAL RUNNING (an in-flight query the
                    # user submitted) vs PHANTOM RUNNING (state
                    # tracker is RUNNING because of paste-echo /
                    # Ctrl+U render / cursor blink, but no actual
                    # query is being processed).  ``_query_in_flight``
                    # is set True only on ``on_send`` (Leap-dispatched
                    # message) and ``on_input`` Enter (real Enter into
                    # Claude's input) — NOT on paste echoes.  The
                    # auto-sender resets it whenever it observes a
                    # transition to IDLE, so its value here is the
                    # clean "is there a real query running?" signal.
                    has_real_query = (
                        self.state.current_state != CLIState.IDLE
                        and self.state._query_in_flight
                    )
                    # Clear stale text typed before ^^.
                    if self._capture_stale_visual_rows > 0:
                        self._clear_stale_cli_input(
                            self._capture_stale_logical_lines,
                            self._capture_stale_visual_rows)
                        self._capture_stale_visual_rows = 0
                        self._capture_stale_logical_lines = 0
                    self._send_clear_queue.append(False)
                    self.queue.add(msg)
                    if not has_real_query:
                        self._capture_force_dispatch = True
                    self._dispatch_wake.set()
                    # Defer SIGWINCH so its Ink full repaint doesn't
                    # block the dispatch's paste-and-submit; ``_send_to_cli``
                    # fires the resize itself once the message is on its way.
                    self._capture_flush(defer_sigwinch=True)
                else:
                    # Empty Enter — clear stale text unconditionally.
                    # This path is reached when the user saved their
                    # message via ^^ inside capture mode (which empties
                    # the buffer): their original typed text is already
                    # in history, so leaving it on the CLI input line
                    # would just be misleading.
                    if self._capture_stale_visual_rows > 0:
                        try:
                            termios.tcflush(self.pty.process.child_fd,
                                            termios.TCOFLUSH)
                        except Exception:
                            pass
                        self._clear_stale_cli_input(
                            self._capture_stale_logical_lines,
                            self._capture_stale_visual_rows)
                        self._capture_stale_visual_rows = 0
                        self._capture_stale_logical_lines = 0
                    self._capture_flush()
                self._queue_capture_buf.clear()
                self._capture_cursor_pos = 0
                self._capture_utf8_buf.clear()
                self._queue_capture_mode = False
                self._capture_reset_images()
                self._terminal_input_buf.clear()
                self._terminal_input_cursor = 0
        elif b == 0x16:  # Ctrl+V — paste clipboard image
            if self._pending_caret:
                self._pending_caret = False
            self._capture_show_hint = False
            if self._capture_paste_image():
                self._capture_display(self._capture_text())
        elif b == 0x7f:  # Backspace
            self._capture_show_hint = False
            if self._capture_backspace():
                self._capture_display(self._capture_text())
        elif b == 0x03:  # Ctrl+C — cancel capture
            self._capture_cancel()
        elif b == 0x5e:  # "^" in capture mode
            if self._capture_show_saved_hint:
                self._capture_show_saved_hint = False
            if self._pending_caret and not chunk_has_paste:
                # Double "^" → save message
                self._pending_caret = False
                text = self._capture_text()
                p = self._capture_cursor_pos
                if p > 0 and text[p - 1] == '^':
                    text = text[:p - 1] + text[p:]
                    self._queue_capture_buf = bytearray(
                        text.encode('utf-8'))
                    self._capture_cursor_pos = p - 1
                self._save_capture_message()
                if not self._capture_show_saved_hint:
                    _display_or_defer()
            else:
                self._pending_caret = True
                self._capture_show_hint = False
                self._capture_insert('^')
                _display_or_defer()
        elif b == 0x21:  # '!' — fast !! triggers force-send confirm (empty buffer only)
            if self._pending_caret:
                self._pending_caret = False
            self._capture_show_hint = False
            if (self._pending_bang
                    and not chunk_has_paste
                    and time.time() - self._pending_bang_time < 0.2
                    and self._queue_capture_buf == bytearray(b'!')):
                # Second '!' arrived fast with only '!' in buffer → confirm mode
                self._pending_bang = False
                self._queue_capture_buf.clear()
                self._capture_cursor_pos = 0
                self._capture_force_confirm = True
                self._capture_display_force_confirm()
            else:
                # Start pending-bang only when buffer was empty before this '!'
                if not self._queue_capture_buf:
                    self._pending_bang = True
                    self._pending_bang_time = time.time()
                else:
                    self._pending_bang = False
                self._capture_insert('!')
                _display_or_defer()
        elif 0x20 <= b < 0x7f:  # ASCII printable
            if self._pending_caret:
                self._pending_caret = False
            if self._capture_show_saved_hint:
                self._capture_show_saved_hint = False
            self._capture_show_hint = False
            self._capture_insert(chr(b))
            _display_or_defer()
        elif b >= 0x80:  # Multi-byte UTF-8
            if self._pending_caret:
                self._pending_caret = False
            self._capture_show_hint = False
            self._capture_utf8_buf.append(b)
            try:
                char = self._capture_utf8_buf.decode('utf-8')
                self._capture_insert(char)
                self._capture_utf8_buf.clear()
            except UnicodeDecodeError:
                pass
            _display_or_defer()
        else:
            if self._pending_caret:
                self._pending_caret = False

        return i + 1, dirty
