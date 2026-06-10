"""Background daemon-thread loops for :class:`LeapServer`.

Extracted verbatim from ``server.py``. Three long-lived threads started by
``LeapServer.run``: ``_auto_sender_loop`` (dispatches queued messages when the
CLI is idle / in the right auto-send mode), ``_title_keeper_loop`` (keeps the
terminal/OSC title set), and ``_stdin_watchdog_loop``. Pure method container:
all state lives in ``LeapServer.__init__`` and is accessed via ``self``;
``LeapServer`` inherits this mixin so every call resolves unchanged.
"""

import os
import signal
import sys
import time
import traceback

from leap.cli_providers.states import AutoSendMode, CLIState, WAITING_STATES
from leap.utils.constants import POLL_INTERVAL, TITLE_RESET_INTERVAL
from leap.utils.terminal import set_terminal_title


class BackgroundLoopsMixin:
    """Long-lived daemon-thread loops mixed into LeapServer."""

    def _auto_sender_loop(self) -> None:
        """Background thread to auto-send queued messages."""
        prev_state = CLIState.IDLE
        # Delayed write for prompt/idle states: wait for TUI to finish
        # rendering (prompts) or for the hook to update the signal file
        # with the assistant message text (idle, for Slack).
        delayed_write_due: float = 0.0
        delayed_prev_state: str = ''
        delayed_queue_has_next: bool = False
        delayed_target_state: str = ''
        # Dispatch debounce: count consecutive polls in a dispatchable ready
        # state.  A queued message is only sent once the state has been ready
        # for two consecutive polls, so a single-poll false-idle (a transient
        # glitch in a provider's silence heuristic) can't type the message
        # into a turn that is actually still running.  Reset whenever the
        # state is not dispatchable; bypassed by a force-dispatch.
        idle_confirm_polls = 0
        while self.running:
            # Wait for a producer's wake-up signal or for the periodic
            # state-poll timeout — whichever comes first.  Short wait
            # (200 ms) when we have queued work, long wait
            # (POLL_INTERVAL) when fully idle.  The short wait bounds
            # the worst-case impact of a wake-event race (a
            # producer's ``set()`` consumed by ``clear()`` between
            # ``wait()`` returning and ``clear()`` running) so
            # back-to-back queued messages still dispatch reasonably
            # fast.  200 ms is chosen to comfortably exceed the
            # ~60 ms SIGWINCH-completion delay used by capture-mode
            # ``signal_dispatch`` — without that margin, the
            # auto-sender's short-poll timeout can race-fire BEFORE
            # the SIGWINCH thread sets the wake-event, restoring the
            # paste-write/Ink-repaint race that the SIGWINCH-first
            # ordering exists to prevent.
            wait = 0.2 if not self.queue.is_empty else POLL_INTERVAL
            self._dispatch_wake.clear()
            self._dispatch_wake.wait(timeout=wait)

            try:
                current_state = self.state.get_state(
                    self.pty.is_alive(),
                    has_pending_input=bool(self._terminal_input_buf)
                    or self._queue_capture_mode,
                )

                # Detect state transitions for Slack output capture
                if current_state != prev_state:
                    # Cancel any pending delayed write on state change
                    delayed_write_due = 0.0
                    queue_has_next = (
                        not self.queue.is_empty
                        and current_state == CLIState.IDLE
                        and self.state.auto_send_mode in (AutoSendMode.PAUSE, AutoSendMode.ALWAYS)
                    )
                    # Any transition to IDLE means whatever real query
                    # was running (if any) has finished — clear the
                    # ``_query_in_flight`` flag so the dispatcher's
                    # next phantom-RUNNING check correctly reads as
                    # "no real query in flight".  Centralising the
                    # reset here avoids touching all ~10 call sites
                    # in state_tracker that flip to IDLE.
                    if current_state == CLIState.IDLE:
                        self.state._query_in_flight = False
                    if current_state in WAITING_STATES:
                        # Delay writing: let PTY output accumulate so the
                        # full permission dialog / input prompt is captured.
                        delayed_write_due = time.time() + 0.2
                        delayed_prev_state = prev_state
                        delayed_queue_has_next = queue_has_next
                        delayed_target_state = current_state
                    elif (
                        current_state == CLIState.IDLE
                        and prev_state == CLIState.RUNNING
                    ):
                        # Delay writing so the hook can populate the signal
                        # file with last_assistant_message.  If the signal
                        # file already has the response (e.g. transcript-
                        # based detection wrote it), use a short delay.
                        signal_has_response = self._signal_file_has_response()
                        delay = 0.2 if signal_has_response else 2.0
                        delayed_write_due = time.time() + delay
                        delayed_prev_state = prev_state
                        delayed_queue_has_next = queue_has_next
                        delayed_target_state = CLIState.IDLE
                    else:
                        self.output_capture.on_state_change(
                            current_state, prev_state, queue_has_next,
                        )
                    prev_state = current_state

                # Delayed Slack output write
                if delayed_write_due and time.time() >= delayed_write_due:
                    try:
                        cs = self.state.current_state
                        if delayed_target_state in WAITING_STATES and cs in WAITING_STATES:
                            prompt_output = self.state.get_prompt_output()
                            self.output_capture.on_state_change(
                                cs, delayed_prev_state,
                                delayed_queue_has_next, prompt_output,
                            )
                        elif delayed_target_state == CLIState.IDLE:
                            self.output_capture.on_state_change(
                                delayed_target_state, delayed_prev_state,
                                delayed_queue_has_next,
                            )
                    finally:
                        delayed_write_due = 0.0

                # Auto-approve permissions in Always-send mode.
                # Wait until delayed Slack write is flushed so the
                # prompt output is captured before on_send() clears it.
                if (
                    current_state == CLIState.NEEDS_PERMISSION
                    and self.state.auto_send_mode == AutoSendMode.ALWAYS
                    and not delayed_write_due
                ):
                    if self._try_auto_approve():
                        # on_send() moved state to RUNNING — update
                        # prev_state so the next idle transition is
                        # seen as running→idle (needed for Slack
                        # delayed-write to capture the response).
                        prev_state = CLIState.RUNNING
                    continue

                if self.queue.is_empty:
                    idle_confirm_polls = 0
                    continue
                # Never dispatch through a permission / input prompt
                # or an interrupted state — those are real "waiting
                # for the user" states.  RUNNING is allowed when
                # ``_capture_force_dispatch`` is set (user typed
                # ^^ + Enter); otherwise stick to IDLE-only.
                if current_state in WAITING_STATES:
                    idle_confirm_polls = 0
                    continue
                if (not self._capture_force_dispatch
                        and not self.state.is_ready_for_state(current_state)):
                    idle_confirm_polls = 0
                    continue
                # Never type a queued message into a half-typed prompt: if
                # the user has unsubmitted text in the input box (or is
                # composing a ^^ message), skip this dispatch.  The
                # composing-aware state usually holds RUNNING here, but that
                # hold is capped (so a long compose-pause can idle), so guard
                # the dispatch directly too.  An explicit ^^+Enter
                # force-dispatch bypasses this (the capture cleared the box).
                if (not self._capture_force_dispatch
                        and (self._terminal_input_buf
                             or self._queue_capture_mode)):
                    idle_confirm_polls = 0
                    continue
                # Dispatch debounce: require the ready state to persist for two
                # consecutive polls before sending, so a single-poll false-idle
                # (a transient glitch in a provider's silence heuristic) can't
                # type a queued message into a turn that is actually still
                # running.  A force-dispatch (^^ + Enter) bypasses it - the
                # user explicitly asked to send now.
                if not self._capture_force_dispatch:
                    idle_confirm_polls += 1
                    if idle_confirm_polls < 2:
                        continue

                # Flush pending Slack write BEFORE sending the next
                # message — on_send() deletes the signal file, so the
                # output text would be lost if we wait.  The hook may
                # not have written last_assistant_message yet (< 2s),
                # but a partial capture is better than losing it.
                if delayed_write_due:
                    try:
                        if delayed_target_state == CLIState.IDLE:
                            self.output_capture.on_state_change(
                                delayed_target_state, delayed_prev_state,
                                delayed_queue_has_next,
                            )
                        elif delayed_target_state in WAITING_STATES:
                            cs = self.state.current_state
                            if cs in WAITING_STATES:
                                prompt_output = self.state.get_prompt_output()
                                self.output_capture.on_state_change(
                                    cs, delayed_prev_state,
                                    delayed_queue_has_next, prompt_output,
                                )
                    except Exception:
                        pass
                    delayed_write_due = 0.0

                message = self.queue.pop()
                if not message:
                    idle_confirm_polls = 0
                    continue

                try:
                    self._send_to_cli(message)
                    self.queue.track_sent(message)
                except Exception as e:
                    print(f"Error sending to CLI, requeuing: {e}", file=sys.stderr, flush=True)
                    self.queue.requeue(message)
                # Re-arm the debounce: the next queued message must independently
                # observe two consecutive ready polls (the session is RUNNING
                # again right after a send anyway).
                idle_confirm_polls = 0
            except Exception:
                print(
                    "Error in auto-sender loop iteration:",
                    file=sys.stderr, flush=True,
                )
                traceback.print_exc(file=sys.stderr)

    def _title_keeper_loop(self) -> None:
        """Background thread to maintain terminal title.

        Skips the write when CLI output was received recently to avoid
        interleaving OSC escape sequences with the TUI rendering, which
        can corrupt colors and produce visual artefacts.  This silence
        guard is never bypassed: the title-keeper writes to stdout from
        this thread while the relay writes the CLI's output to the same
        fd, so a write mid-render races and corrupts the frame.

        A JetBrains tab that Leap has ever renamed is "pinned" - its
        display name stops tracking the OSC application title - so if the
        tab gets knocked back to a bare name mid-turn, only the ideScript
        rename (which rides inside set_terminal_title) restores it.  While
        the CLI streams, the guard suppresses that rename, so the tab
        stays bare for the whole active stretch.  To heal it promptly
        without bypassing the guard, the busy->idle edge shortens the poll
        until output falls quiet, then re-asserts on the very next quiet
        tick instead of waiting up to a full interval.
        """
        prev_state = self.state.current_state
        # Deadline until which we poll quickly so a post-idle re-assert
        # lands as soon as output goes quiet (0 = no pending re-assert).
        idle_assert_deadline = 0.0
        while self.running:
            cur_state = self.state.current_state
            if prev_state != CLIState.IDLE and cur_state == CLIState.IDLE:
                idle_assert_deadline = time.time() + 3.0
            elif cur_state != CLIState.IDLE:
                # Back to work before output quiesced - the pending
                # post-idle re-assert is moot; a fresh edge will arm it
                # again when this turn ends.
                idle_assert_deadline = 0.0
            prev_state = cur_state

            output_quiet = time.time() - self._last_output_time > 0.2
            if output_quiet:
                try:
                    set_terminal_title(f"lps {self.tag}", vscode_rename=False)
                except Exception:
                    pass
                idle_assert_deadline = 0.0
            # Interruptible wait so cleanup() can stop this loop promptly
            # (and join it) before it writes the bare tab name on exit.
            if not output_quiet and time.time() < idle_assert_deadline:
                self._title_keeper_stop.wait(0.1)
            else:
                self._title_keeper_stop.wait(TITLE_RESET_INTERVAL)

    def _stdin_watchdog_loop(self) -> None:
        """Background thread to detect when the terminal is closed.

        pexpect.spawn() creates a new PTY session, so the server may
        not receive SIGHUP when the original terminal tab is closed.
        Poll the original terminal fd to detect the loss and trigger
        a clean shutdown.
        """
        try:
            stdin_fd = sys.stdin.fileno()
        except (AttributeError, ValueError):
            return  # Not a real fd — nothing to watch
        while self.running:
            time.sleep(2)
            try:
                # tcgetpgrp raises OSError/EIO when the terminal is gone
                os.tcgetpgrp(stdin_fd)
            except OSError:
                os.kill(os.getpid(), signal.SIGTERM)
                return
