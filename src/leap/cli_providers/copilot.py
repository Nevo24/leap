"""
GitHub Copilot CLI provider.

Implements the CLIProvider interface for GitHub's Copilot CLI
(``copilot``) - an agentic coding assistant.  Node.js, full-screen
(alternate-screen) TUI.

Key differences from the other providers:
- **No lifecycle hook system.**  Copilot exposes no Stop / Notification
  hooks (verified against v1.0.60), so state detection is driven
  entirely by PTY output: an on-screen "running" footer indicator,
  dialog-footer patterns, the interrupt banner, plus the state
  tracker's cursor/silence fallbacks.  ``hooks_installed`` always
  returns True so the session-start gate never blocks.  ``configure_hooks``
  doesn't install lifecycle hooks - instead it installs a **status line**
  (the only place Copilot exposes live context-window usage), so the
  monitor's Context column can show it (see ``configure_hooks`` /
  ``context_usage`` below).
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
from leap.utils.atomic_write import atomic_write_json
from leap.utils.context_usage import ContextUsage, copilot_context_usage


COPILOT_CONFIG_DIR: Path = Path.home() / ".copilot"
COPILOT_SETTINGS_FILE: Path = COPILOT_CONFIG_DIR / "settings.json"


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
    def input_dialog_patterns(self) -> list[bytes]:
        # Copilot ask_user QUESTION dialogs have two footer shapes:
        #   menu:      "↑/↓ to select · enter to confirm · esc to cancel"
        #   free-text: "enter to submit · esc to cancel"  (visible cursor)
        # The verb ("confirm"/"submit") marks a question, vs a permission
        # prompt's "enter to select".  ANY present -> needs_input (not
        # needs_permission) - keeps ALWAYS-mode auto-approve from
        # auto-answering a question, and gets it off "Running" / out of
        # the false "Idle" the visible-cursor free-text field would cause.
        return [b'entertoconfirm', b'entertosubmit']

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
    def idle_indicator_patterns(self) -> list[bytes]:
        # Copilot's idle prompt is NOT quiescent: it animates its input
        # box and emits PTY output continuously even when idle (a focused
        # real terminal drives this; it stays quiet under a bare PTY), so
        # the silence-based running->idle fallbacks never fire and the
        # session sticks in RUNNING.  The idle footer "/ commands · ? help"
        # (compact contains "/commands") is the reliable end-of-turn
        # signal: absent while a turn runs (footer is "esc cancel") and
        # during dialogs ("enter to select · esc to cancel").  Setting
        # this switches Copilot to footer-driven idle detection and
        # disables the cursor auto-resume heuristic.
        return [b'/commands']

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
    def interrupt_key(self) -> bytes:
        # Copilot IGNORES Escape mid-turn (verified live: the response
        # kept streaming and the "Working" footer stayed) - it cancels on
        # Ctrl+C, which prints "Operation cancelled by user".  A second
        # Ctrl+C would exit, so exactly one is sent.  (The default Escape
        # is what the monitor's Interrupt action sends to the others.)
        return b'\x03'

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
        """Install a status line so the monitor can read live context usage.

        Copilot has no lifecycle hooks and exposes no token usage in its
        transcript - the live context-window numbers are only available to a
        **status line** command (Copilot pipes a JSON payload to it on stdin
        every render).  So we register Leap's status-line script
        (``leap-copilot-statusline.py``, installed next to the hook script) in
        ``~/.copilot/settings.json``.

        Copilot allows only one ``statusLine``, so we **preserve** any the user
        already had by saving its command to ``leap-statusline-chain`` (the
        Leap script runs it and echoes its output).  Best-effort and never
        raises: the status line is optional (``hooks_installed`` stays True),
        so a failure here must not break ``make install`` or session startup.
        """
        try:
            statusline = Path(hook_script_path).with_name(
                'leap-copilot-statusline.py')
            if not statusline.is_file():
                return  # installer didn't place the script - nothing to wire
            settings: dict[str, Any] = {}
            if COPILOT_SETTINGS_FILE.is_file():
                try:
                    with open(COPILOT_SETTINGS_FILE) as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        settings = loaded
                except (json.JSONDecodeError, OSError):
                    settings = {}
            existing = settings.get('statusLine')
            existing_cmd = (existing.get('command')
                            if isinstance(existing, dict) else None)
            # Preserve the user's prior status line by chaining to it - but
            # never chain to our own script (a self-reference would loop on
            # reconfigure).
            if (isinstance(existing_cmd, str) and existing_cmd.strip()
                    and 'leap-copilot-statusline' not in existing_cmd):
                try:
                    statusline.with_name('leap-statusline-chain').write_text(
                        existing_cmd)
                except OSError:
                    pass
            settings['statusLine'] = {
                'type': 'command',
                'command': str(statusline),
                'padding': 1,
            }
            atomic_write_json(COPILOT_SETTINGS_FILE, settings)
        except Exception:
            return  # best-effort: the status line is an optional enhancement

    def hooks_installed(self) -> bool:
        """Always True - Copilot has no lifecycle hooks to verify.

        The session-start gate (``leap-server.py``) calls this before spawning
        the server; returning True unconditionally lets Copilot sessions start.
        The status line installed by ``configure_hooks`` is an *optional*
        enhancement (the monitor's Context column), so it is intentionally NOT
        gated here - a missing status line just means a blank Context cell, not
        a refused session.
        """
        return True

    def deconfigure_hooks(self) -> None:
        """Remove Leap's status line from ~/.copilot/settings.json.

        Restores the user's prior status-line command from ``leap-statusline-chain``
        if one was saved at install time, or removes the statusLine key entirely
        if Leap added it from scratch.  Then removes the chain file and the
        status-line script.  Best-effort: never raises.
        """
        try:
            chain_file = self.hook_config_dir / "leap-statusline-chain"
            prior_cmd: Optional[str] = None
            if chain_file.is_file():
                try:
                    prior_cmd = chain_file.read_text(encoding="utf-8").strip() or None
                except OSError:
                    pass

            if COPILOT_SETTINGS_FILE.is_file():
                try:
                    with open(COPILOT_SETTINGS_FILE, encoding="utf-8") as f:
                        settings = json.load(f)
                    if isinstance(settings, dict):
                        existing = settings.get("statusLine")
                        existing_cmd = (
                            existing.get("command") if isinstance(existing, dict) else None
                        )
                        if (
                            isinstance(existing_cmd, str)
                            and "leap-copilot-statusline" in existing_cmd
                        ):
                            if prior_cmd:
                                settings["statusLine"] = {
                                    "type": "command",
                                    "command": prior_cmd,
                                }
                            else:
                                settings.pop("statusLine", None)
                            atomic_write_json(COPILOT_SETTINGS_FILE, settings)
                except (json.JSONDecodeError, OSError):
                    pass

            for name in ("leap-statusline-chain", "leap-copilot-statusline.py"):
                try:
                    (self.hook_config_dir / name).unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass
        super().deconfigure_hooks()

    @property
    def supports_context_usage(self) -> bool:
        return True

    def context_usage(self, cli_name: str, tag: str,
                      storage_dir: Path) -> Optional[ContextUsage]:
        """Context-window usage from the status-line state file.

        Leap's status-line script writes ``<storage>/sockets/<tag>.context``
        (JSON ``{used_tokens, window, model}``) every render; the file is
        absent until the status line first fires (-> blank cell) or if the
        status line isn't installed (run ``leap --reconfigure``).
        """
        state = storage_dir / 'sockets' / f'{tag}.context'
        return copilot_context_usage(str(state))

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
