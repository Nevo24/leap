"""
PTY handling for ClaudeQ server.

Manages spawning and interacting with the Claude CLI process.
"""

import os
import shutil
import sys
import threading
import time
from typing import Callable, Optional

import pexpect


class PTYHandler:
    """Handles PTY spawning and interaction with Claude CLI."""

    def __init__(self, flags: Optional[list[str]] = None):
        """
        Initialize PTY handler.

        Args:
            flags: Command-line flags to pass to Claude CLI.
        """
        self.flags = flags or []
        self.process: Optional[pexpect.spawn] = None
        self._send_lock = threading.Lock()
        self._output_received = threading.Event()

    def spawn(self) -> None:
        """
        Spawn the Claude CLI process.

        Raises:
            SystemExit: If Claude CLI is not found in PATH.
        """
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        print(f"📏 Terminal: {cols}x{rows}\n")

        claude_path = self._find_claude_cli()
        if not claude_path:
            print("Error: 'claude' command not found")
            sys.exit(1)

        self.process = pexpect.spawn(
            claude_path,
            args=self.flags,
            dimensions=(rows, cols)
        )

    def _find_claude_cli(self) -> Optional[str]:
        """
        Find the Claude CLI executable in PATH.

        Returns:
            Path to Claude CLI or None if not found.
        """
        for path_dir in os.environ.get('PATH', '').split(':'):
            candidate = os.path.join(path_dir, 'claude')
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _write_all(self, data: str) -> None:
        """Write all bytes to PTY, retrying on partial writes.

        Must be called with _send_lock held.

        Args:
            data: String data to write.

        Raises:
            OSError: If PTY write fails.
        """
        if not self.process:
            return
        encoded = data.encode('utf-8')
        fd = self.process.child_fd
        total = 0
        while total < len(encoded):
            n = os.write(fd, encoded[total:])
            if n == 0:
                raise OSError("PTY write returned 0 bytes")
            total += n

    def send(self, message: str) -> None:
        """
        Send raw data to the Claude CLI (thread-safe, handles partial writes).

        Args:
            message: Data to send.
        """
        with self._send_lock:
            self._write_all(message)

    def _wait_for_output_settled(
        self, settle_time: float = 0.15, timeout: float = 5.0
    ) -> None:
        """Wait until PTY output settles (no new output for *settle_time*).

        Must be called with _send_lock held.

        After sending multi-line input, Claude CLI enters paste-mode
        detection and produces rendering output in chunks.  This method
        loops until there is a quiet period of *settle_time* seconds
        with no output, which signals that the CLI has finished
        processing and is ready for new input (e.g. the submit CR).

        Args:
            settle_time: Quiet-period duration in seconds.
            timeout: Overall maximum wait in seconds.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._output_received.clear()
            if not self._output_received.wait(timeout=settle_time):
                return  # No output for settle_time — settled

    def sendline(self, message: str) -> None:
        """Send message + carriage return to the CLI.

        Always sends the text and CR as separate writes with a quiet
        gap between them.  Claude CLI detects rapid character bursts as
        pastes and absorbs a trailing CR as content rather than treating
        it as submit.  Waiting for the output to settle ensures the CR
        arrives as a distinct input event.

        Multi-line messages need a longer settle time because they
        trigger paste-mode processing in the CLI.

        Args:
            message: Message to send.
        """
        with self._send_lock:
            settle = 0.15 if '\n' in message else 0.05
            self._write_all(message)
            self._wait_for_output_settled(settle_time=settle)
            self._write_all('\r')

    def send_image_message(self, message: str) -> None:
        """Send @-prefixed image message with file confirmation.

        Holds the send lock for the entire three-part sequence (text,
        confirm file, submit) to prevent interleaved writes.  Waits
        for CLI output to settle between each step.

        Args:
            message: Image message starting with '@'.
        """
        with self._send_lock:
            self._write_all(message)
            self._wait_for_output_settled()
            self._write_all('\r')  # Confirm file selection
            self._wait_for_output_settled()
            self._write_all('\r')  # Submit message

    def notify_output_received(self) -> None:
        """Signal that PTY output was received from the child process.

        Called from the server's output filter to wake up
        send_image_message when it's waiting for Claude CLI to react.
        """
        self._output_received.set()

    def is_alive(self) -> bool:
        """Check if the Claude process is still running."""
        return self.process is not None and self.process.isalive()

    def resize(self, rows: int, cols: int) -> None:
        """
        Resize the PTY window.

        Args:
            rows: Number of rows.
            cols: Number of columns.
        """
        if self.process:
            try:
                self.process.setwinsize(rows, cols)
            except OSError:
                pass

    def interact(self, output_filter: Optional[Callable[[bytes], bytes]] = None) -> None:
        """
        Enter interactive mode with the Claude CLI.

        Args:
            output_filter: Optional function to filter output before display.
        """
        if self.process:
            self.process.interact(output_filter=output_filter)

    def terminate(self) -> None:
        """Terminate the Claude CLI process."""
        if self.process and self.process.isalive():
            try:
                self.process.terminate(force=True)
            except OSError:
                pass

    @property
    def pid(self) -> Optional[int]:
        """Get the PID of the Claude process."""
        return self.process.pid if self.process else None
