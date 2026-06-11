"""Terminal byte-stream filter methods for :class:`LeapServer`.

Extracted verbatim from ``server.py``. These are the input and output filters
that sit in the PTY hot path: ``_input_filter`` / ``_input_filter_impl`` decide
what the user's keystrokes do (dispatch into capture mode, drive ↑/↓ history
recall, gate auto-approve, mirror the input buffer) and ``_output_filter`` /
``_output_filter_impl`` post-process CLI output (e.g. strip OSC title escapes).
Pure method container: all state lives in ``LeapServer.__init__`` and the
``_OSC_TITLE_RE`` class attribute, accessed via ``self``; ``LeapServer``
inherits this mixin so every call resolves unchanged.
"""

import threading
import time

from leap.cli_providers.states import PROMPT_STATES


class IOFilterMixin:
    """PTY input/output byte-stream filters mixed into LeapServer."""

    def _input_filter(self, data: bytes) -> bytes:
        """Track user keyboard input for state detection.

        Also accumulates typed text so that messages entered directly
        in the server terminal are captured as the current task.

        **Queue from server**: When the user types ``^`` at the start of
        a line, capture mode activates — subsequent chars are swallowed
        (the CLI never sees them).  On Enter the message is added to
        the queue.  A notification is injected into the output stream
        as confirmation.

        Args:
            data: Raw input bytes from keyboard.

        Returns:
            Input bytes to forward to the CLI (swallowed in capture mode).
        """
        # Wrap the entire filter in try/except — any unhandled exception
        # here propagates to pexpect's interact loop and kills the PTY.
        try:
            return self._input_filter_impl(data)
        except Exception:
            if self._queue_capture_mode:
                return b''  # don't leak capture text to CLI
            return data

    def _input_filter_impl(self, data: bytes) -> bytes:
        """Implementation of _input_filter (separated for crash protection)."""
        # Block all input while a queue message is being sent to the CLI.
        # Without this, user keystrokes could interleave with the
        # Ctrl+E/Ctrl+U clear and the message paste, corrupting the send.
        # Buffer the raw bytes so they can be replayed after the send.
        if self._queue_sending:
            self._queue_sending_held.extend(data)
            return b''

        # Flush any keystrokes that were held during a queue send.
        # Prepend them so they're processed through the full filter
        # (tracking, escape handling, ^^ detection, etc.).
        if self._queue_sending_held:
            data = bytes(self._queue_sending_held) + data
            self._queue_sending_held.clear()

        # Apply deferred SIGWINCH resize outside the signal context.
        self._apply_pending_resize()

        # Safety net: if a held "^" has been pending for >200ms and
        # the timer hasn't flushed it yet, treat it as a literal now.
        # This prevents a stale _pending_caret from combining with a
        # "^" typed much later.
        if (self._pending_caret
                and not self._queue_capture_mode
                and time.time() - self._pending_caret_time > 0.2):
            if self._pending_caret_timer is not None:
                self._pending_caret_timer.cancel()
                self._pending_caret_timer = None
            self._pending_caret = False
            # The timer may have already flushed via pty.send — check
            # if the buf already has the ^.  If not, the ^ was lost
            # (timer raced), so we skip the buf append.  The CLI may
            # or may not have it depending on timer timing — either
            # way, clearing _pending_caret is the safe thing to do.

        # Note: on_input() is called AFTER the byte loop (see end of
        # method) with only the bytes that reach the CLI.  This prevents
        # capture-mode keystrokes from affecting state tracker flags
        # (e.g. false idle→running on Enter, or false _user_responded).

        current_state = self.state.current_state
        in_prompt = current_state in PROMPT_STATES

        self._prev_filter_state = current_state

        out = bytearray()
        i = 0
        capture_dirty = False  # deferred display update for pastes
        chunk_has_paste = self._detect_paste(data)
        # Flush held "^" that was dropped by paste detection.
        if self._pending_caret_flush:
            self._pending_caret_flush = False
            out.append(0x5e)
            self._terminal_buf_insert(0x5e)
            self._chars_sent_to_cli += 1

        # Split-marker repair: ``_detect_paste`` recognizes paste markers
        # split across two reads (via its scan tail), but the byte loop's
        # marker handlers below only fire on a marker fully inside this
        # chunk — the split bytes travel through the partial-escape
        # continuation path instead.  Reconcile the accumulator off the
        # split flags so a split end marker can't leave it accumulating
        # forever (swallowing the next real Enter), and a split start
        # marker still collapses the paste to a placeholder.  Runs after
        # the caret flush (like the in-loop marker handlers) so a flushed
        # pre-paste ``^`` stays outside the paste snapshot.
        if self._split_paste_end and self._paste_accumulator is not None:
            self._finalize_paste_capture()
        if (self._split_paste_start
                and self._paste_accumulator is None
                and not self._queue_capture_mode):
            self._paste_accumulator = bytearray()
            self._paste_buf_snapshot_len = len(self._terminal_input_buf)
            self._paste_cursor_snapshot = self._terminal_input_cursor
            self._paste_chars_snapshot = self._chars_sent_to_cli

        # Check if the very first byte is "^" and _pending_caret is set
        # from the previous chunk → double-caret capture trigger.
        # Skip if we're inside a bracketed paste.
        if (not self._queue_capture_mode
                and not chunk_has_paste
                and i < len(data)
                and data[i] == 0x5e
                and self._pending_caret):
            # Second "^" arrived in a new chunk.  Enter capture mode.
            # The first "^" was held (never sent to CLI), so there is
            # no stale caret to clean up.
            if self._pending_caret_timer is not None:
                self._pending_caret_timer.cancel()
                self._pending_caret_timer = None
            self._partial_escape = None
            self._enter_capture_mode(
                stale_cli_input=bool(self._terminal_input_buf),
                stale_caret=False)
            i += 1
        # Note: we used to eagerly flush a held "^" here when the new
        # chunk didn't start with "^".  That broke ^^ detection under
        # kitty keyboard protocol (e.g. Codex/Ratatui), where each "^"
        # press is followed by a CSI-u key-release escape sequence in
        # its own chunk — the flush ran before the second "^" press
        # could arrive.  The byte loop's own ``elif self._pending_caret``
        # at line ~2926 already flushes correctly when a real non-"^"
        # byte (not an escape sequence) is encountered, and the 200ms
        # timer + the >0.2s safety-net above handle the
        # nothing-came-after case.

        # If a previous call ended mid-escape, skip continuation bytes.
        if self._partial_escape == 'csi':
            # CSI was already started (\x1b[ consumed in previous chunk).
            # Continue consuming parameter bytes and the final byte.
            self._partial_escape = None
            while i < len(data) and 0x20 <= data[i] <= 0x3f:
                out.append(data[i])
                i += 1
            if i < len(data):
                out.append(data[i])  # final byte (0x40-0x7e)
                i += 1
            else:
                # Still truncated — remain in CSI state
                self._partial_escape = 'csi'
        elif self._partial_escape == 'esc':
            # Bare \x1b was at end of previous chunk — need type byte.
            self._partial_escape = None
            if i < len(data) and data[i] == 0x5b:
                # CSI: skip introducer, parameter bytes, and final byte
                out.append(data[i])  # '['
                i += 1
                while i < len(data) and 0x20 <= data[i] <= 0x3f:
                    out.append(data[i])
                    i += 1
                if i < len(data):
                    out.append(data[i])  # final byte
                    i += 1
                else:
                    # CSI truncated — switch to csi state
                    self._partial_escape = 'csi'
            elif i < len(data) and data[i] == 0x4f:
                # SS3: skip 'O' + one final byte
                out.append(data[i])
                i += 1
                if i < len(data):
                    out.append(data[i])
                    i += 1
            else:
                # Two-byte escape: only consume if the byte is a valid
                # final byte (0x40-0x5F, e.g. ESC M for reverse index).
                # Otherwise the \x1b was a standalone Escape key press
                # and the current byte is new input — leave it alone.
                if i < len(data) and 0x40 <= data[i] <= 0x5f:
                    out.append(data[i])
                    i += 1

        while i < len(data):
            b = data[i]

            # --- Escape sequences ---
            if b == 0x1b:
                esc_start = i
                is_standalone_esc = False
                i += 1
                if i >= len(data):
                    # ESC at end of chunk — mark partial, pass through
                    is_standalone_esc = True
                    if not self._queue_capture_mode:
                        self._partial_escape = 'esc'
                        out.append(b)
                    else:
                        # In capture mode: Escape cancels capture
                        self._capture_cancel()
                    continue
                kind = data[i]
                if kind == 0x5b:  # CSI
                    i += 1
                    while i < len(data) and 0x20 <= data[i] <= 0x3f:
                        i += 1
                    if i < len(data):
                        i += 1
                    else:
                        # CSI truncated at end of chunk
                        if not self._queue_capture_mode:
                            self._partial_escape = 'csi'
                elif kind in (0x5d, 0x50, 0x58, 0x5e, 0x5f):
                    i += 1
                    while i < len(data):
                        if data[i] == 0x07:
                            i += 1
                            break
                        if data[i] == 0x1b and i + 1 < len(data) and data[i + 1] == 0x5c:
                            i += 2
                            break
                        i += 1
                elif kind == 0x4f:  # SS3 (e.g. \x1bOP for F1)
                    i += 1
                    if i < len(data):
                        i += 1  # consume the final byte
                elif 0x40 <= kind <= 0x5f:
                    # Valid two-byte escape (e.g. ESC M = reverse index).
                    i += 1
                elif kind in (0x62, 0x66):
                    # ESC-b / ESC-f (Meta word left/right).
                    # Consume the byte so it's included in seq.
                    i += 1
                else:
                    # Not a recognized escape introducer — treat \x1b as
                    # a standalone Escape key press.
                    is_standalone_esc = True

                esc_seq = data[esc_start:i]
                if self._queue_capture_mode:
                    self._capture_handle_escape(
                        esc_seq, is_standalone_esc)
                elif esc_seq == b'\x1b[200~':
                    # Bracketed paste start — begin accumulating so we
                    # can collapse large pastes to a placeholder.  If
                    # a previous paste never received its end marker
                    # (malformed stream), force-finalize it first so
                    # its accumulated bytes aren't silently dropped
                    # and _in_bracketed_paste doesn't stay stuck.
                    if self._paste_accumulator is not None:
                        self._finalize_paste_capture()
                    self._paste_accumulator = bytearray()
                    self._paste_buf_snapshot_len = len(
                        self._terminal_input_buf)
                    self._paste_cursor_snapshot = (
                        self._terminal_input_cursor)
                    self._paste_chars_snapshot = self._chars_sent_to_cli
                    out.extend(esc_seq)
                elif esc_seq == b'\x1b[201~':
                    # Bracketed paste end — finalize (maybe collapse).
                    self._finalize_paste_capture()
                    out.extend(esc_seq)
                elif (not in_prompt
                      and self._is_csi_u_cancel(esc_seq)):
                    # CSI-u Ctrl+C outside capture — clear input
                    # buf just like the raw 0x03 handler does.
                    self._terminal_input_buf.clear()
                    self._terminal_input_cursor = 0
                    self._chars_sent_to_cli = 0
                    self._preserved_input_buf.clear()
                    self._preserved_chars_sent = 0
                    self._pending_paste_images.clear()
                    self._reset_history_recall()
                    out.extend(esc_seq)
                elif (not in_prompt
                      and not chunk_has_paste
                      and self._is_csi_u_paste(esc_seq)):
                    # CSI-u Ctrl+V outside capture — save clipboard
                    # image so the next ^^ picks it up at the right
                    # position (cursor position, not end-of-buf).
                    path = self._save_clipboard_image()
                    if path:
                        self._pending_paste_images.append(
                            (self._terminal_input_cursor, path))
                    out.extend(esc_seq)
                elif (not in_prompt
                      and not chunk_has_paste
                      and self._is_csi_u_newline(esc_seq)):
                    # CSI-u Shift/Cmd+Enter outside capture — the
                    # CLI's TUI inserts a newline in its input box,
                    # but the raw escape leaves no trace in
                    # ``_terminal_input_buf``.  Mirror it as a literal
                    # ``\n`` at the cursor so ``_stale_visual_rows``
                    # counts the wrap correctly when the user later
                    # types ^^ — otherwise multi-line typed input
                    # (especially mixed with Ctrl+V images) under-
                    # counts visual rows and leaves residue on Enter.
                    self._terminal_buf_insert(0x0a)
                    self._chars_sent_to_cli += 1
                    out.extend(esc_seq)
                elif (not in_prompt
                      and not chunk_has_paste
                      and self._paste_accumulator is None
                      and esc_seq in (b'\x1b[A', b'\x1bOA',
                                      b'\x1b[B', b'\x1bOB')
                      and not self.state.screen_has_active_dialog()):
                    # ↑/↓ outside capture — try Leap-managed history
                    # recall.  Reads the CLI's own persistent history
                    # via the provider and injects the recalled text
                    # back into the input box so a subsequent ``^^``
                    # captures the recalled message instead of an
                    # empty buffer.
                    #
                    # The ``screen_has_active_dialog`` gate covers the
                    # window where a dialog is rendered on screen but
                    # the state tracker hasn't yet flipped to
                    # ``NEEDS_PERMISSION`` (notably ``AskUserQuestion``,
                    # which fires no Notification hook and only flips
                    # via the 5 s cursor+silence fallback).  Without
                    # this check, arrows pressed during that window
                    # would be stolen for history recall and the user
                    # couldn't navigate the dialog for several seconds
                    # ("stuck for a moment, then unstuck" reports).
                    #
                    # Pre-flush: if the same chunk carried typed
                    # bytes ahead of the arrow (e.g. ``hello\x1b[A``
                    # arrives in one ``read()``), those bytes are
                    # still sitting in ``out`` waiting for pexpect
                    # to write them AFTER ``_input_filter`` returns.
                    # If we then call ``pty.send`` for our clear +
                    # inject, the CLI receives our writes first and
                    # the typed bytes land AFTER the recalled text
                    # — input box ends up as ``newesthello`` while
                    # the mirror correctly holds ``newest``.  Flush
                    # ``out`` directly via ``pty.send`` to lock in
                    # the [typed → clear → inject] order.
                    #
                    # The flushed bytes also need to be visible to
                    # ``state.on_input`` — without it, an Enter or
                    # Ctrl+C arriving in the same chunk before the
                    # arrow would slip past the state tracker (the
                    # end-of-filter ``state.on_input(out)`` only
                    # sees what's still in ``out``, and we just
                    # cleared it).  Fire on_input on the flushed
                    # slice so state transitions (idle→running on
                    # Enter, interrupt-pending on Ctrl+C) still fire.
                    if out:
                        flushed = bytes(out)
                        try:
                            self.pty.send(flushed)
                        except OSError:
                            pass
                        self.state.on_input(flushed)
                        out.clear()
                    direction = -1 if esc_seq in (b'\x1b[A', b'\x1bOA') else 1
                    if not self._handle_history_recall(direction):
                        # Provider opted out — preserve passthrough
                        # by emitting the escape to the CLI.  Typed
                        # bytes were already flushed above, so the
                        # CLI sees them before the arrow.
                        out.extend(esc_seq)
                else:
                    # Mirror cursor motion escapes so our
                    # _terminal_input_buf stays in sync with Claude.
                    if esc_seq == b'\x1b[D':  # Left
                        self._terminal_cursor_left()
                    elif esc_seq == b'\x1b[C':  # Right
                        self._terminal_cursor_right()
                    elif esc_seq in (b'\x1b[H', b'\x1b[1~'):  # Home
                        self._terminal_input_cursor = 0
                    elif esc_seq in (b'\x1b[F', b'\x1b[4~'):  # End
                        self._terminal_input_cursor = len(
                            self._terminal_input_buf)
                    elif esc_seq == b'\x1b[3~':  # Delete (forward)
                        self._terminal_buf_delete_forward()
                    out.extend(esc_seq)
                continue

            # --- Queue-capture mode: swallow input, queue on Enter ---
            if self._queue_capture_mode:
                i, dirty = self._capture_handle_char(
                    b, data, i, chunk_has_paste)
                capture_dirty |= dirty
                continue

            # --- Active bracketed paste: short-circuit all special-key
            # handlers so the pasted content reaches the accumulator
            # byte-for-byte.  Without this, characters like ``^``,
            # backspace, Ctrl+C, and other control bytes inside a
            # paste trigger their normal semantics (delete, clear buf,
            # etc.) and the raw content in the accumulator ends up
            # missing those bytes — the saved/resolved paste no
            # longer matches what the user actually pasted.
            if self._paste_accumulator is not None:
                self._paste_accumulator.append(b)
                out.append(b)
                # Track only printable chars in the terminal buf for
                # later truncation-to-snapshot in _finalize.  Control
                # chars that Claude renders as invisible (e.g. \t, \r)
                # don't bump visible-char counters.  Insert at the
                # mirrored cursor so pastes placed mid-line end up
                # in the correct position in our buf.
                if 0x20 <= b < 0x7f or b >= 0x80:
                    self._terminal_buf_insert(b)
                    self._chars_sent_to_cli += 1
                i += 1
                continue

            # "^^" (double caret) → queue capture mode.
            # First "^" is held as literal.  If the next byte is also
            # "^", capture triggers.  Otherwise the first "^" stays
            # as a literal character.
            # Skip trigger inside bracketed paste to prevent accidental
            # activation from pasted text containing "^^".
            if b == 0x5e:
                if chunk_has_paste:
                    # Inside bracketed paste — emit "^" literally and
                    # bypass the pending-caret state machine so pasted
                    # "^^" isn't mangled into a single "^".
                    out.append(0x5e)
                    self._terminal_buf_insert(0x5e)
                    self._chars_sent_to_cli += 1
                    i += 1
                    continue
                if self._pending_caret:
                    # Second "^" → capture (same chunk).
                    # The first "^" was held (never added to out or
                    # buf), so no stale caret on CLI.
                    if self._pending_caret_timer is not None:
                        self._pending_caret_timer.cancel()
                        self._pending_caret_timer = None
                    self._enter_capture_mode(
                        stale_cli_input=bool(self._terminal_input_buf),
                        stale_caret=False,
                    )
                    i += 1
                    continue
                else:
                    # First "^" — hold it, wait for second.
                    # Do NOT add to out or buf yet — if the next byte
                    # is also "^", capture triggers and the CLI never
                    # sees the "^" (no stale caret to clean up).
                    # Start a timer to flush as literal after 200ms.
                    self._pending_caret = True
                    self._pending_caret_time = time.time()
                    if self._pending_caret_timer is not None:
                        self._pending_caret_timer.cancel()
                    self._pending_caret_timer = threading.Timer(
                        0.2, self._flush_pending_caret)
                    self._pending_caret_timer.daemon = True
                    self._pending_caret_timer.start()
                    i += 1
                    continue

            # If we were waiting for a second "^" but got something
            # else, the pending caret was a literal — flush it now.
            elif self._pending_caret:
                if self._pending_caret_timer is not None:
                    self._pending_caret_timer.cancel()
                    self._pending_caret_timer = None
                self._pending_caret = False
                out.append(0x5e)
                self._terminal_buf_insert(0x5e)
                self._chars_sent_to_cli += 1

            if in_prompt:
                out.append(b)
                i += 1
                continue

            # --- Normal handling ---
            # Paste-mode bytes were handled earlier by the
            # active-paste short-circuit, so any \r here is a real
            # Enter keypress outside a paste.
            if b == 0x0d:  # Enter
                self._user_has_typed = True
                if self._terminal_input_buf:
                    msg = self._terminal_input_buf.decode(
                        'utf-8', errors='replace').strip()
                    if msg:
                        self.queue.track_sent(msg)
                    self._terminal_input_buf.clear()
                self._terminal_input_cursor = 0
                self._chars_sent_to_cli = 0
                # User committed input — discard any preserved text.
                self._preserved_input_buf.clear()
                self._preserved_chars_sent = 0
                # Clear pending paste images — the user committed
                # the current input to the CLI.  Keeping stale images
                # across Enter presses causes them to silently
                # accumulate and get injected into a later ^^ message.
                self._pending_paste_images.clear()
                self._gc_paste_text_map()
                # Reset CLI history-recall so the next ↑ re-reads the
                # provider's history file (the just-submitted message
                # is now on disk) and re-snapshots the live buffer.
                self._reset_history_recall()
                out.append(b)
            elif b == 0x7f:  # Backspace
                self._terminal_buf_backspace()
                out.append(b)
                self._chars_sent_to_cli = max(
                    0, self._chars_sent_to_cli - 1)
            elif b == 0x03:  # Ctrl+C — discard buffer
                self._terminal_input_buf.clear()
                self._terminal_input_cursor = 0
                self._chars_sent_to_cli = 0
                self._preserved_input_buf.clear()
                self._preserved_chars_sent = 0
                self._pending_paste_images.clear()
                self._gc_paste_text_map()
                self._reset_history_recall()
                out.append(b)
            elif b == 0x16:  # Ctrl+V — save clipboard image for next ^^
                if not chunk_has_paste:
                    path = self._save_clipboard_image()
                    if path:
                        pos = self._terminal_input_cursor
                        self._pending_paste_images.append((pos, path))
                out.append(b)
            elif b == 0x15:  # Ctrl+U — kill line from cursor to start
                # Mirror Claude's kill-line behavior.  Only drops
                # the chars before the cursor; anything after stays.
                if self._terminal_input_cursor > 0:
                    del self._terminal_input_buf[
                        :self._terminal_input_cursor]
                    self._chars_sent_to_cli = max(
                        0,
                        self._chars_sent_to_cli
                        - self._terminal_input_cursor,
                    )
                    self._terminal_input_cursor = 0
                out.append(b)
            elif b == 0x01:  # Ctrl+A — cursor to start of line
                self._terminal_input_cursor = 0
                out.append(b)
            elif b == 0x05:  # Ctrl+E — cursor to end of line
                self._terminal_input_cursor = len(
                    self._terminal_input_buf)
                out.append(b)
            elif 0x20 <= b < 0x7f or b >= 0x80:
                # Insert at cursor so text typed between placeholders
                # appears in the right order in our mirror of Claude's
                # input line.
                self._terminal_buf_insert(b)
                out.append(b)
                self._chars_sent_to_cli += 1
            else:
                out.append(b)
            i += 1

        # Deferred display update after paste in capture mode — one
        # refresh instead of one per character.
        if capture_dirty and self._queue_capture_mode:
            self._capture_display(self._capture_text())

        # Track input for state detection using only the bytes that
        # actually reach the CLI.  Capture-mode keystrokes are excluded
        # so they don't affect interrupt detection or trigger
        # idle→running on Enter.
        if out:
            self.state.on_input(bytes(out))

        return bytes(out)

    def _output_filter(self, data: bytes) -> bytes:
        """
        Filter PTY output to inject notifications and strip title escapes.

        Wrapped in try/except — any crash here kills pexpect's interact
        loop and terminates the PTY.

        Args:
            data: Raw output bytes.

        Returns:
            Filtered output bytes with title sequences removed and
            notifications injected.
        """
        try:
            return self._output_filter_impl(data)
        except Exception:
            if self._queue_capture_mode or time.monotonic() < self._suppress_send_until:
                return b''
            return data

    def _output_filter_impl(self, data: bytes) -> bytes:
        """Implementation of _output_filter (separated for crash protection)."""
        # Apply deferred SIGWINCH resize outside the signal context
        # (signal handlers must not acquire locks).
        self._apply_pending_resize()

        # Strip OSC title-change sequences so the CLI cannot override
        # the "lps <tag>" tab name used by the monitor for navigation.
        data = self._OSC_TITLE_RE.sub(b'', data)

        # Delegate state detection to the state tracker.
        self.state.on_output(data)

        # Signal PTY handler that output was received (used by
        # send_image_message to replace fixed sleeps with event waits).
        self.pty.notify_output_received()

        # Suppress during capture (TUI redraws on exit) and during
        # message send (hides echo so delivery is invisible).
        if self._queue_capture_mode or time.monotonic() < self._suppress_send_until:
            return b''

        # Track last output time so _title_keeper_loop can avoid
        # writing to stdout while the CLI is actively rendering.
        self._last_output_time = time.time()

        return data
