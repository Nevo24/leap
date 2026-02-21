"""
Input handling for ClaudeQ client.

Handles prompt_toolkit or readline-based input with history.
"""

import readline
from pathlib import Path
from typing import Callable, Optional

# Try to import prompt_toolkit for better input handling
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False
    PromptSession = None
    patch_stdout = None
    FileHistory = None
    KeyBindings = None


class InputHandler:
    """Handles command-line input with history support."""

    def __init__(
        self,
        history_file: Path,
        prompt_getter: Callable[[], str],
        on_paste_image: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        """
        Initialize input handler.

        Args:
            history_file: Path to command history file.
            prompt_getter: Callable that returns the current prompt string.
            on_paste_image: Optional callback that checks clipboard for an image,
                saves it, and returns the file path (or None if no image).
        """
        self.history_file = history_file
        self.prompt_getter = prompt_getter
        self.prompt_session: Optional[PromptSession] = None

        if HAS_PROMPT_TOOLKIT:
            kb = self._build_key_bindings(on_paste_image)
            self.prompt_session = PromptSession(
                history=FileHistory(str(history_file)),
                key_bindings=kb,
            )
        else:
            self._load_readline_history()

    @staticmethod
    def _build_key_bindings(
        on_paste_image: Optional[Callable[[], Optional[str]]],
    ) -> Optional[KeyBindings]:
        """Build prompt_toolkit key bindings.

        Args:
            on_paste_image: Callback to check/save clipboard image.

        Returns:
            KeyBindings instance, or None.
        """
        if not on_paste_image:
            return None

        kb = KeyBindings()

        @kb.add('c-v')
        def _paste(event: object) -> None:
            text = on_paste_image()
            if text:
                event.current_buffer.insert_text(text)  # type: ignore[union-attr]

        return kb

    def _load_readline_history(self) -> None:
        """Load history for readline fallback."""
        if self.history_file.exists():
            try:
                readline.read_history_file(str(self.history_file))
            except OSError:
                pass

    def save_history(self) -> None:
        """Save command history (only needed for readline)."""
        if not HAS_PROMPT_TOOLKIT:
            try:
                readline.write_history_file(str(self.history_file))
            except OSError:
                pass

    def get_input(self) -> str:
        """
        Get input from user.

        Returns:
            User input string.

        Raises:
            EOFError: When user presses Ctrl+D.
        """
        if HAS_PROMPT_TOOLKIT and self.prompt_session:
            return self.prompt_session.prompt(self.prompt_getter)
        else:
            return input(self.prompt_getter())

    @property
    def has_advanced_input(self) -> bool:
        """Check if prompt_toolkit is available."""
        return HAS_PROMPT_TOOLKIT

    def get_context_manager(self) -> Optional[object]:
        """
        Get the stdout patch context manager if available.

        Returns:
            patch_stdout context manager or None.
        """
        if HAS_PROMPT_TOOLKIT:
            return patch_stdout()
        return None
