"""
PTY handling for ClaudeQ server.

Manages spawning and interacting with the Claude CLI process.
"""

import os
import shutil
import sys
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

    def send(self, message: str) -> None:
        """
        Send a message to the Claude CLI.

        Args:
            message: Message to send.
        """
        if self.process:
            self.process.send(message)

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
