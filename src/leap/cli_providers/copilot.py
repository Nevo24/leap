"""
GitHub Copilot CLI provider.

Implements the CLIProvider interface for GitHub's Copilot CLI
(``copilot``) - an agentic coding assistant.  Node.js, full-screen
(alternate-screen) TUI.

Key differences from the other providers:
- **No hook system.**  Copilot exposes no lifecycle / Stop /
  Notification hooks (verified against v1.0.60 - no ``hooks`` config
  setting and no ``hooks`` help topic), so state detection is driven
  entirely by PTY output: an on-screen "running" footer indicator,
  dialog-footer patterns, the interrupt banner, plus the state
  tracker's cursor/silence fallbacks.  ``configure_hooks`` is a no-op
  and ``hooks_installed`` always returns True so the session-start
  gate never blocks.
- **No ``leap --resume`` integration.**  Leap records resumable
  sessions from the hook payload (``extract_session_id``); with no
  hooks firing, nothing is recorded, so ``supports_resume`` stays
  False even though ``copilot`` itself supports ``--resume`` /
  ``--continue`` / ``--session-id`` natively (users can still pass
  those as flags).
- **Alternate-screen TUI, but the cursor stays VISIBLE** while idle
  AND while running (unlike Codex's Ratatui, which hides it).  So
  ``cursor_hidden_while_idle`` stays the default False and the
  cursor+silence idle fallback applies - the cursor is not a
  busy/idle signal here.
- The on-screen **"esc cancel" footer** - shown only while a turn is
  in flight ("◎ Working    esc cancel") - is a reliable running
  indicator.  It stands in for the missing Stop hook and keeps the
  session RUNNING across silent tool calls; it disappears the instant
  the turn ends (footer reverts to the idle prompt).
- Permission / trust dialogs are arrow-navigable numbered menus with
  an "enter to select / esc to cancel" footer.

Config dir: ``~/.copilot`` (``config.json`` is auto-managed; optional
user settings live in ``settings.json``).  Input history:
``~/.copilot/command-history-state.json``.
"""

import json
import time
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider


COPILOT_CONFIG_DIR: Path = Path.home() / ".copilot"


class CopilotProvider(CLIProvider):
    """Provider for GitHub Copilot CLI (alternate-screen TUI, Node.js)."""

    # -- Identity --------------------------------------------------------

    @property
    def name(self) -> str:
        return 'copilot'

    @property
    def command(self) -> str:
        return 'copilot'

    @property
    def display_name(self) -> str:
        return 'GitHub Copilot'

    # -- State detection patterns ----------------------------------------

    @property
    def trust_dialog_patterns(self) -> list[bytes]:
        # Startup "Confirm folder trust" dialog (verified v1.0.60):
        # "Do you trust the files in this folder?" with a numbered
        # "❯ 1. Yes / 2. ... / 3. No (Esc)" menu.  Either token (any
        # match) flags the startup dialog.
        return [
            b'Doyoutrustthefilesinthisfolder',
            b'Confirmfoldertrust',
        ]

    @property
    def interrupted_pattern(self) -> bytes:
        # Pressing Esc / Ctrl+C during a turn prints
        # "● Operation cancelled by user" (verified v1.0.60).
        # Matched against the compact screen (spaces removed).
        return b'Operationcancelledbyuser'

    @property
    def confirmed_interrupt_pattern(self) -> Optional[bytes]:
        # The "Operation cancelled by user" line lingers in the
        # scrollback after the interrupt, so a flag-free "confirmed"
        # check would re-fire INTERRUPTED on every later idle render.
        # Rely solely on the _interrupt_pending-gated path - Leap
        # routes Esc/Ctrl+C through on_input(), which arms that flag.
        return None

    @property
    def dialog_patterns(self) -> list[bytes]:
        # Permission / trust / selection dialogs share a footer:
        #   "↑/↓ to navigate · enter to select · esc to cancel"
        # (verified on the trust dialog; the approval menu uses the
        # same component).  Requiring BOTH tokens (is_dialog_certain is
        # all-match) distinguishes a real select-dialog from the idle
        # footer ("/ commands · ? help") and the running footer
        # ("esc cancel" - note: no "to", so "esctocancel" never
        # matches it).
        return [b'entertoselect', b'esctocancel']

    @property
    def running_indicator_patterns(self) -> list[bytes]:
        # While a turn is in flight Copilot shows a footer
        #   "◎ Working    esc cancel"
        # (the verb animates; "esc cancel" is the stable interrupt
        # affordance).  Compact "esccancel" appears ONLY while running:
        # not in the idle footer ("/commands·?help") and not in the
        # dialog footer ("esctocancel" has "to" between esc and
        # cancel).  Copilot has no Stop hook, so this on-screen
        # indicator is what keeps the session RUNNING through silent
        # tool calls and stops the silence fallbacks concluding idle
        # mid-turn; it vanishes the instant the turn ends, letting the
        # cursor+silence fallback idle the session.
        return [b'esccancel']

    @property
    def dialogs_hide_cursor(self) -> bool:
        # Copilot HIDES the terminal cursor while a trust/permission menu
        # is up (verified live), yet keeps it VISIBLE while idle and while
        # running.  So the cursor can't gate dialog detection here - a
        # certain dialog footer must promote RUNNING -> needs_permission
        # even though the cursor is hidden, or a hookless session would
        # sit stuck in RUNNING for the whole prompt.
        return True

    # -- Menu / option parsing -------------------------------------------

    @property
    def has_numbered_menus(self) -> bool:
        # Numbered options are shown (1./2./3.) but the menu is
        # arrow-navigable via a ❯ cursor ("↑/↓ to navigate · enter to
        # select").  We drive it with arrow keys (Enter selects the
        # highlighted row - verified) rather than parsing the numbers,
        # same approach as Cursor Agent / Gemini.
        return False

    # -- Input protocol --------------------------------------------------

    @property
    def supports_image_attachments(self) -> bool:
        # `copilot --attachment` is non-interactive (-p) only; the
        # interactive composer has no inline @path attachment syntax.
        return False

    # -- Input history (CLI ↑/↓ recall) ----------------------------------

    def input_history(self, cwd: str) -> Optional[list[str]]:
        """Read ``~/.copilot/command-history-state.json``.

        Copilot keeps a single global history list under the
        ``commandHistory`` key, ordered **newest → oldest** (new
        entries are prepended).  Leap wants oldest → newest (so the
        last element is what plain ↑ selects first), so we reverse it.
        History is global, not cwd-scoped - ``cwd`` is ignored, which
        matches what the CLI's own ↑ surfaces.
        """
        del cwd  # Copilot history is global
        path = COPILOT_CONFIG_DIR / 'command-history-state.json'
        try:
            # Pin UTF-8 and catch decode errors (a ValueError, not an
            # OSError) so a non-UTF-8 locale doesn't silently disable
            # ↑/↓ recall - sibling providers guard this the same way.
            raw = path.read_text(encoding='utf-8')
        except (OSError, ValueError):
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        hist = data.get('commandHistory')
        if not isinstance(hist, list):
            return None
        out = [s for s in hist if isinstance(s, str) and s]
        out.reverse()  # newest-first on disk → oldest-first for Leap
        return out

    # -- Hook configuration (Copilot has no hook system) -----------------

    @property
    def hook_config_dir(self) -> Path:
        return COPILOT_CONFIG_DIR

    @property
    def requires_binary_for_hooks(self) -> bool:
        return True

    def configure_hooks(self, hook_script_path: str) -> None:
        """No-op: Copilot CLI exposes no lifecycle hook mechanism.

        State detection relies entirely on PTY output patterns (the
        running-indicator footer, dialog footers, the interrupt
        banner) plus the state tracker's cursor/silence fallbacks.
        There is nothing to install, so this does nothing - but it
        must exist (abstract method) and must never raise.
        """
        del hook_script_path

    def hooks_installed(self) -> bool:
        """Always True - Copilot has no hooks to install or verify.

        The session-start gate (``leap-server.py``) calls this before
        spawning the server; returning True unconditionally lets
        Copilot sessions start, since there is no hook integration
        that could be "missing" and no ``leap --reconfigure`` step
        that would change anything.
        """
        return True

    # -- CLI-specific input behaviors ------------------------------------

    def select_option(
        self,
        option_num: int,
        options: dict[int, str],
        pty_send: Any,
        pty_sendline: Any,
    ) -> dict[str, Any]:
        """Select an option in Copilot's arrow-navigable menus.

        Copilot permission/trust dialogs render a ❯-cursor menu
        ("↑/↓ to navigate · enter to select").  Enter selects the
        highlighted row (verified: Enter on the trust dialog accepted
        option 1).  So:

        - option_num=1 → Enter (the first row is highlighted by default)
        - option_num>=2 → arrow-down (N-1) times, then Enter
        """
        if option_num == 1:
            pty_send('\r')
            return {'status': 'sent'}
        elif option_num >= 2:
            for _ in range(option_num - 1):
                pty_send('\x1b[B')
                time.sleep(0.1)
            time.sleep(0.2)
            pty_send('\r')
            return {'status': 'sent'}
        return {
            'status': 'error',
            'error': f'invalid option number: {option_num}',
        }

    def send_custom_answer(
        self,
        text: str,
        options: dict[int, str],
        pty_send: Any,
    ) -> dict[str, Any]:
        """Type a free-form answer into Copilot's composer.

        Chars are sent one at a time with a small settle so the Ink
        composer keeps up, then a CR submits - same shape as the other
        TUI providers.
        """
        for ch in text:
            pty_send(ch)
            time.sleep(0.02)
        time.sleep(0.1)
        pty_send('\r')
        return {'status': 'sent'}
