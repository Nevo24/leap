"""
PTY handling for ClaudeQ server.

Manages spawning and interacting with a CLI process (Claude, Codex, etc.).
"""

import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pexpect

from claudeq.cli_providers.base import CLIProvider
from claudeq.cli_providers.registry import get_provider


class PTYHandler:
    """Handles PTY spawning and interaction with a CLI process."""

    def __init__(
        self,
        flags: Optional[list[str]] = None,
        tag: Optional[str] = None,
        signal_dir: Optional[Path] = None,
        provider: Optional[CLIProvider] = None,
    ) -> None:
        """
        Initialize PTY handler.

        Args:
            flags: Command-line flags to pass to the CLI.
            tag: Session tag name (injected as CQ_TAG env var).
            signal_dir: Directory for signal files (injected as CQ_SIGNAL_DIR).
            provider: CLI provider instance. Defaults to Claude.
        """
        self.flags = flags or []
        self._tag = tag
        self._signal_dir = signal_dir
        self._provider = provider or get_provider()
        self.process: Optional[pexpect.spawn] = None
        self._send_lock = threading.Lock()
        self._output_received = threading.Event()

    @property
    def provider(self) -> CLIProvider:
        """The CLI provider for this PTY session."""
        return self._provider

    def spawn(self) -> None:
        """
        Spawn the CLI process.

        Raises:
            SystemExit: If CLI is not found in PATH.
        """
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        print(f"\U0001f4cf Terminal: {cols}x{rows}\n")

        cli_path = self._provider.find_cli()
        if not cli_path:
            print(f"Error: '{self._provider.command}' command not found")
            sys.exit(1)

        env = dict(os.environ)
        env.update(self._provider.get_spawn_env(self._tag, self._signal_dir))

        self.process = pexpect.spawn(
            cli_path,
            args=self.flags,
            dimensions=(rows, cols),
            env=env,
        )

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
        Send raw data to the CLI (thread-safe, handles partial writes).

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

        After sending input, the CLI echoes it back (TUI renders the text
        at the prompt).  This method first waits for the echo to *start*
        (at least one output event), then waits for it to *finish* (no
        output for *settle_time* seconds).

        The two-phase approach prevents a false "settled" when the echo
        hasn't arrived yet.

        Args:
            settle_time: Quiet-period duration in seconds.
            timeout: Overall maximum wait in seconds.
        """
        deadline = time.monotonic() + timeout

        # Phase 1: wait for the first echo output
        self._output_received.clear()
        if not self._output_received.wait(timeout=2.0):
            return

        # Phase 2: wait for the echo to settle
        while time.monotonic() < deadline:
            self._output_received.clear()
            if not self._output_received.wait(timeout=settle_time):
                return  # No output for settle_time — settled

    def sendline(self, message: str) -> None:
        """Send message + carriage return to the CLI.

        Uses the provider's send_message() for CLI-specific timing.

        Args:
            message: Message to send.
        """
        with self._send_lock:
            self._provider.send_message(
                self.process, message, self._send_lock,
                self._write_all, self._wait_for_output_settled,
            )

    def send_image_message(self, message: str) -> None:
        """Send image attachment message using provider's protocol.

        Args:
            message: Image message (e.g. '@path' for Claude).
        """
        with self._send_lock:
            self._provider.send_image_message(
                self.process, message, self._send_lock,
                self._write_all, self._wait_for_output_settled,
            )

    def notify_output_received(self) -> None:
        """Signal that PTY output was received from the child process.

        Called from the server's output filter to wake up
        send_image_message when it's waiting for the CLI to react.
        """
        self._output_received.set()

    def is_alive(self) -> bool:
        """Check if the CLI process is still running."""
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

    def interact(
        self,
        output_filter: Optional[Callable[[bytes], bytes]] = None,
        input_filter: Optional[Callable[[bytes], bytes]] = None,
    ) -> None:
        """
        Enter interactive mode with the CLI.

        Args:
            output_filter: Optional function to filter output before display.
            input_filter: Optional function to filter keyboard input.
        """
        if self.process:
            self.process.interact(
                output_filter=output_filter,
                input_filter=input_filter,
            )

    def terminate(self) -> None:
        """Terminate the CLI process."""
        if self.process and self.process.isalive():
            try:
                self.process.terminate(force=True)
            except OSError:
                pass

    @property
    def pid(self) -> Optional[int]:
        """Get the PID of the CLI process."""
        return self.process.pid if self.process else None
