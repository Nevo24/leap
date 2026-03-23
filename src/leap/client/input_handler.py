"""
Input handling for Leap client.

Handles prompt_toolkit or readline-based input with history.
"""

import atexit
import readline
import sys
from pathlib import Path
from typing import Callable, Optional

# Try to import prompt_toolkit for better input handling
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.input import ansi_escape_sequences as _ansi_seq
    HAS_PROMPT_TOOLKIT = True

    # Register Kitty keyboard protocol (CSI u) sequences that prompt_toolkit
    # doesn't know about natively.  These are sent by VS Code, Kitty, Ghostty,
    # Alacritty, Warp, and iTerm2 (with CSI u enabled).
    _SHIFT_ENTER_SENTINEL = '\x80'
    _ansi_seq.ANSI_SEQUENCES['\x1b[13u'] = Keys.ControlM       # Enter
    _ansi_seq.ANSI_SEQUENCES['\x1b[13;2u'] = _SHIFT_ENTER_SENTINEL  # Shift+Enter
    _ansi_seq.ANSI_SEQUENCES['\x1b[127u'] = Keys.Backspace      # Backspace
    _ansi_seq.ANSI_SEQUENCES['\x1b[127;2u'] = Keys.Backspace    # Shift+Backspace
    _ansi_seq.ANSI_SEQUENCES['\x1b[9u'] = Keys.Tab              # Tab
    _ansi_seq.ANSI_SEQUENCES['\x1b[9;2u'] = Keys.BackTab        # Shift+Tab
    _ansi_seq.ANSI_SEQUENCES['\x1b[27u'] = Keys.Escape          # Escape
except ImportError:
    HAS_PROMPT_TOOLKIT = False
    PromptSession = None
    patch_stdout = None
    FileHistory = None
    KeyBindings = None
    _SHIFT_ENTER_SENTINEL = None


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
                multiline=True,
            )
            # NOTE: We intentionally do NOT activate the Kitty keyboard protocol
            # (\x1b[>1u) at runtime, because some terminals (VS Code) then encode
            # ALL keys as CSI u sequences, breaking normal input.  Instead:
            # - iTerm2: configured via plist during `make install` (sends \n for Shift+Enter)
            # - VS Code/Kitty/Ghostty: handle CSI u natively; if they send CSI u
            #   sequences, the ANSI_SEQUENCES registrations above will parse them.
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
        kb = KeyBindings()

        # Enter submits (override multiline default where Enter=newline)
        @kb.add('enter')
        def _submit(event: object) -> None:
            event.current_buffer.validate_and_handle()  # type: ignore[union-attr]

        # Shift+Enter inserts newline — iTerm2 (with CSI u) sends 0x0a for
        # Shift+Enter vs 0x0d for plain Enter.  0x0a = Ctrl+J in prompt_toolkit.
        @kb.add('c-j')
        def _newline_shift(event: object) -> None:
            event.current_buffer.insert_text('\n')  # type: ignore[union-attr]

        # Meta+Enter / Escape+Enter inserts newline (works in all terminals)
        @kb.add('escape', 'enter')
        def _newline(event: object) -> None:
            event.current_buffer.insert_text('\n')  # type: ignore[union-attr]

        # Shift+Enter via full Kitty CSI u sequence (Warp, Kitty, Ghostty, Alacritty)
        if _SHIFT_ENTER_SENTINEL:
            @kb.add(_SHIFT_ENTER_SENTINEL)
            def _newline_kitty(event: object) -> None:
                event.current_buffer.insert_text('\n')  # type: ignore[union-attr]

        if on_paste_image:
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
