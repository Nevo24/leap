"""End-to-end realistic user flows.

Each test walks the state machine through a full sequence that could
actually happen in a live session, exercising multiple code paths in
order.  They're the closest thing we have to "real-world smoke tests"
without a real Claude CLI.
"""

import time

from tests.conftest import PTYFixture


_DIALOG = (
    b'Allow tool?\r\n1. Yes\r\n2. No\r\n'
    b'Enter to select  Esc to cancel\r\n'
)
_INDICATOR = b'\xe2\x9c\xbb Compacting conversation... (5s)'


def _advance(pty: PTYFixture, seconds: float) -> None:
    base = pty.tracker._clock()
    pty.tracker._clock = lambda: base + seconds


class TestHappyPathFlow:
    def test_first_time_trust_then_send(
        self, pty: PTYFixture,
    ) -> None:
        """Fresh install: trust dialog → answer → idle → send →
        response → idle."""
        # Trust dialog on startup.
        pty.feed_output(
            b'Is this a project you trust?\r\n'
            b'> 1. Yes, I trust this folder\r\n'
            b'  2. No, exit\r\n',
        )
        assert pty.tracker.current_state == 'needs_permission'

        # User answers.
        pty.tracker.on_input(b'\r')
        time.sleep(2.5)
        pty.write_signal('idle')
        assert pty.get_state() == 'idle'

        # First message.
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        # Response arrives, Stop fires.
        pty.write_signal('idle', last_assistant_message='Hello!')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_standard_turn_without_permission(
        self, pty: PTYFixture,
    ) -> None:
        """idle → send → running → Stop fires → idle."""
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_turn_with_permission_prompt(
        self, pty: PTYFixture,
    ) -> None:
        """idle → send → permission → answer → running → idle."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'1')
        # CLI resumes — cursor hidden.
        pty.feed_output(b'\x1b[?25l\x1b[2J\x1b[HRunning tool...')
        assert pty.get_state() == 'running'

        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'


class TestInterruptAndRecover:
    def test_interrupt_then_new_prompt_via_client(
        self, pty: PTYFixture,
    ) -> None:
        """User interrupts a response, then sends a new message via
        the client (on_send) — which escapes the interrupted state."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.feed_output(b'Interrupted')
        assert pty.get_state() == 'interrupted'

        # Client sends a new message — on_send unconditionally moves
        # to running.
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_interrupt_then_stop_hook_returns_idle(
        self, pty: PTYFixture,
    ) -> None:
        """User interrupts, answers the 'what should I do' prompt,
        Stop fires → idle."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.feed_output(b'Interrupted')
        assert pty.get_state() == 'interrupted'

        pty.tracker.on_input(b'abort\r')
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'


class TestCompactMidConversation:
    def test_normal_turn_then_compact_then_resume(
        self, pty: PTYFixture,
    ) -> None:
        """Full sequence: send → respond → idle → auto-compact fires →
        running (via indicator) → indicator gone → idle."""
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        # Auto-compact starts.
        pty.feed_output(_INDICATOR)
        assert pty.get_state() == 'running'

        # Compact ends.
        pty.feed_output(b'\x1b[2J\x1b[H\x1b[?25h> ')
        _advance(pty, 6.0)
        assert pty.get_state() == 'idle'

        # User sends a new message — check via current_state to avoid
        # the silence fallback re-firing under our advanced fake clock.
        pty.tracker.on_send()
        assert pty.tracker.current_state == 'running'


class TestPermissionThenInterrupt:
    def test_escape_from_permission_prompt(
        self, pty: PTYFixture,
    ) -> None:
        """Send → permission prompt → user hits Escape → 'Interrupted'
        pattern → interrupted."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'\x1b')
        pty.feed_output(b'\x1b[2J\x1b[HInterrupted \xc2\xb7 What next?')
        assert pty.get_state() == 'interrupted'


class TestRepeatedSendCycles:
    def test_five_consecutive_turns(
        self, pty: PTYFixture,
    ) -> None:
        """Basic stability: five back-to-back idle ↔ running cycles."""
        for _ in range(5):
            pty.tracker.on_send()
            assert pty.tracker.current_state == 'running'
            pty.write_signal('idle')
            assert pty.wait_for_state('idle', timeout=1.0) == 'idle'


class TestClientInteractionPatterns:
    def test_queue_autosend_after_idle(
        self, pty: PTYFixture,
    ) -> None:
        """Simulates auto-sender: poll returns idle → client sends next
        queued message → state running."""
        pty.tracker.on_send()
        pty.write_signal('idle')
        state = pty.wait_for_state('idle', timeout=1.0)
        assert state == 'idle'
        assert pty.tracker.is_ready_for_state(state) is True

        # Auto-sender consumes the queue.
        pty.tracker.on_send()
        assert pty.tracker.current_state == 'running'
        assert pty.tracker.is_ready_for_state(pty.tracker.current_state) is False

    def test_server_terminal_typing_flow(
        self, pty: PTYFixture,
    ) -> None:
        """Typing directly in the server terminal: each char is sent to
        the CLI via on_input; Enter transitions to running; Stop signal
        returns to idle."""
        for ch in b'hello world':
            pty.tracker.on_input(bytes([ch]))
        assert pty.tracker.current_state == 'idle'

        pty.tracker.on_input(b'\r')
        assert pty.tracker.current_state == 'running'

        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'


class TestLongInactiveSession:
    def test_idle_session_stays_idle_without_events(
        self, pty: PTYFixture,
    ) -> None:
        """No input, no output, no signals — idle must stay idle
        forever."""
        pty.tracker.on_input(b'x')
        # Leave the tracker alone, advance clock a lot.
        _advance(pty, 3600.0)
        assert pty.get_state() == 'idle'
