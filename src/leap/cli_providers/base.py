"""
Abstract base class for CLI providers.

Each provider defines the patterns, timings, and behaviors specific to
a CLI tool (Claude Code, Codex, GitHub Copilot, Cursor Agent, Gemini CLI, etc.) so that the PTY handler, state
tracker, and server can work with any registered CLI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

# pexpect is used only in type annotations (``process: pexpect.spawn``)
# for send_message / send_image_message — the spawned object is created
# by the server's pty_handler and passed in.  Importing pexpect at
# module top makes this module unimportable on Pythons that don't have
# it installed, which breaks the hook processor: hooks run under
# whatever Python the hook script picks (``${LEAP_PYTHON:-python3}``),
# and when LEAP_PYTHON isn't propagated by the parent CLI we land on
# system ``python3`` — which usually lacks the venv's pexpect.  Symptom
# was every hook logging ``record-skip / no-leap-import`` and `leap
# --resume` never showing new sessions.  Combined with the
# ``from __future__ import annotations`` above, type checkers still
# resolve the annotation; runtime never tries to import pexpect here.
if TYPE_CHECKING:
    import pexpect

    from leap.utils.context_usage import ContextUsage
    from leap.utils.cost_usage import CostInfo

from leap.cli_providers.states import SIGNAL_ALIASES, SIGNAL_STATES

# Generic selection-dialog detection (see CLIProvider.screen_shows_selection_dialog).
# Nav-hint tokens matched on the compacted (spaces-removed, lowercased) rows;
# they catch "Esc to cancel", "Press enter to confirm or esc to go back",
# "↑/↓ to navigate · enter to select · esc to cancel", etc.
_SELECTION_DIALOG_TAIL_ROWS = 8
_SELECTION_DIALOG_FOOTER_TOKENS = (
    'esctocancel', 'esctogoback', 'entertoconfirm', 'entertoselect',
    'tonavigate', '↑/↓', '↑↓',
)
# Footer-style separators that mark a hint-dense footer line (vs. prose).
_SELECTION_DIALOG_SEPARATORS = ('·', '•', '⋅')
# A single-hint row is treated as a footer only if it's short (just the hint),
# not a long prose sentence that happens to quote the phrase.
_SELECTION_DIALOG_FOOTER_MAX_LEN = 40
# A selection cursor on a numbered option: "› 1.", "❯ 2)", "▶ 3.". A bare
# cursor is excluded (it appears in idle input prompts). Glyph-independent
# footer detection (below) covers pickers whose cursor isn't one of these.
_SELECTION_CURSOR_RE = re.compile(r'[›❯▸▶]\s*\d+[.)]')


class CLIProvider(ABC):
    """Abstract interface for a CLI backend."""

    # -- Identity --------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier used in config/metadata (e.g. 'claude', 'codex', 'copilot', 'cursor-agent', 'gemini')."""

    @property
    @abstractmethod
    def command(self) -> str:
        """Binary name to search for in PATH (e.g. 'claude', 'codex', 'copilot', 'cursor-agent', 'gemini')."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name (e.g. 'Claude Code', 'OpenAI Codex', 'GitHub Copilot', 'Cursor Agent', 'Gemini CLI')."""

    def is_installed(self) -> bool:
        """Check whether the CLI binary is available on PATH."""
        return shutil.which(self.command) is not None

    @property
    def base_type(self) -> str:
        """Built-in CLI this provider is a variant of.

        Returns one of ``'claude'`` / ``'codex'`` / ``'copilot'`` /
        ``'cursor-agent'`` / ``'gemini'``.  All custom CLIs are variants
        of one of the five built-in providers — they share the same
        hook-config dir and
        settings-file layout, so the gate at session start can use the
        base provider's ``hooks_installed()`` rather than requiring every
        custom author to re-implement that check.

        Built-in providers return their own ``name`` (the default
        implementation below).  ``CustomCLIProvider`` doesn't override
        this — it inherits the base's value via ``__getattribute__``
        delegation, so a custom wrapper around ``ClaudeProvider``
        automatically reports ``base_type == 'claude'``.
        """
        return self.name

    # -- State detection patterns ----------------------------------------

    @property
    def trust_dialog_patterns(self) -> list[bytes]:
        """Compact patterns (ANSI-stripped, spaces removed) for startup trust dialog.

        Return empty list if the CLI has no trust dialog.
        Any match triggers detection.
        """
        return [
            b'Yes,Itrustthisfolder',
            b'Doyoutrustthecontentsofthisdirectory?',
        ]

    @property
    @abstractmethod
    def interrupted_pattern(self) -> bytes:
        """Text that appears in PTY output when the user interrupts."""

    @property
    def confirmed_interrupt_pattern(self) -> Optional[bytes]:
        """Specific pattern (ANSI-stripped, spaces removed) that confirms
        a real interrupt prompt — not just the word in conversation text.

        Checked against compact output (ANSI stripped + spaces removed).
        Must be specific enough to avoid false positives.  Used as a
        fallback when the Escape/Ctrl+C input bypasses on_input().

        Return None to rely solely on the escape-time-based check.
        """
        return None

    @property
    @abstractmethod
    def dialog_patterns(self) -> list[bytes]:
        """Compact patterns (ANSI-stripped, spaces removed) that indicate
        a permission/question dialog.  ALL must be present for a match."""

    def has_dialog_indicator(self, compact_text: str) -> bool:
        """Lenient dialog check: any single indicator is sufficient.

        Used by the Late Notification guard to *verify* a hook signal —
        the hook already confirmed a dialog, so a weak match suffices.
        Default: any single ``dialog_patterns`` entry is present
        (catches edit-confirmation dialogs that only show "Esc to cancel"
        without "Enter to select").

        Override for CLIs with additional dialog formats (e.g. numbered
        menus) that don't contain the standard dialog footer patterns.

        Args:
            compact_text: Screen text with spaces and newlines removed.
        """
        return any(
            p.decode('utf-8', errors='replace') in compact_text
            for p in self.dialog_patterns
        )

    def is_dialog_certain(self, compact_text: str) -> bool:
        """Strict dialog check: high confidence that a dialog is visible.

        Used for *proactive* detection (running→idle, startup) where no
        hook signal exists yet and false positives are costly (state gets
        stuck in needs_permission until the 60s safety timeout).

        Default: ALL ``dialog_patterns`` must be present.
        Override to add provider-specific high-confidence indicators
        (e.g. numbered menu cursor character).

        Args:
            compact_text: Screen text with spaces and newlines removed.
        """
        patterns = self.dialog_patterns
        return bool(patterns) and all(
            p.decode('utf-8', errors='replace') in compact_text
            for p in patterns
        )

    def is_idle_prompt_visible(self, display_lines: list[str]) -> bool:
        """Return True iff the CLI's standard idle input box is rendered
        at the bottom of the screen.

        ``screen_has_active_dialog`` uses ``not is_idle_prompt_visible``
        as the catch-all "something interactive is taking over the
        bottom of the screen" signal: when the idle prompt is gone,
        either a permission dialog or a slash-command picker
        (``/resume``, ``/mcp``, ``/agents``, …) is on screen and ↑/↓
        belong to it, not to history recall.  This is intentionally
        structural so a new Claude picker added next month works
        without us enumerating its footer text.

        Default: True (assume the prompt is visible — provider has no
        structural detector wired up, so ``screen_has_active_dialog``
        falls back to the strict ``is_dialog_certain`` check alone,
        which is the legacy behaviour).  Override per provider with a
        real detector to enable picker-detection.

        Trade-offs for an override:
        * False positives (claims idle when it isn't) cost ↑/↓ being
          stolen for history recall — same as today's bug.
        * False negatives (claims not-idle when it is) cost a single
          missed history-recall keystroke.  Bias detectors toward
          false-negatives.

        Args:
            display_lines: All non-blank rows of the live pyte screen,
                top-to-bottom.  The provider is responsible for
                looking at whichever tail it cares about.
        """
        return True

    def has_selection_cursor(self, display_lines: list[str]) -> bool:
        """True iff a menu/picker selection cursor is on the recent screen.

        Used (only when ``is_idle_prompt_visible`` is False) to tell a genuine
        interactive UI — a picker/dialog with a selection cursor on a focused
        option — apart from plain response text (e.g. a numbered list) that
        merely lacks the idle input box.  Default False; overridden by
        providers whose TUI draws a selection cursor (Claude).
        """
        del display_lines
        return False

    def has_interactive_footer(self, display_lines: list[str]) -> bool:
        """True iff the bottom row is a picker/dialog nav/dismiss footer.

        Companion to :meth:`has_selection_cursor` for interactive UIs that
        render no selection cursor on a focused row (e.g. a tabbed view) but
        DO show a nav/dismiss footer at the bottom.  Default False; overridden
        by providers with such footers (Claude).
        """
        del display_lines
        return False

    def screen_shows_selection_dialog(self, display_lines: list[str]) -> bool:
        """True iff the bottom of the screen looks like an arrow-navigable
        selection dialog/picker (permission prompt, trust dialog, model/theme
        picker, …) that should receive ↑/↓ rather than Leap's history recall.

        Generic, CLI-agnostic detector used ONLY by
        ``CLIStateTracker.screen_has_active_dialog`` (the ↑/↓ input filter), so
        a false positive is cheap: the arrow simply reaches the CLI's native
        handling instead of driving Leap recall.  It catches pickers whose
        footers aren't in a provider's ``dialog_patterns`` (e.g. Codex, whose
        ``dialog_patterns`` is empty, and Gemini/Cursor non-permission pickers),
        without each provider enumerating every footer.

        Signals, scoped to the last few non-blank rows:
        * a selection cursor on a numbered option (``› 1.`` / ``❯ 2)`` /
          ``▶ 3.``) — the strongest standalone signal (real Codex dialogs
          render this), or
        * a dialog **footer line** carrying a confirm/cancel/navigate hint
          (``esc to cancel`` / ``enter to confirm`` / ``to navigate`` / ``↑/↓``
          …) that *looks like a footer* rather than prose quoting the phrase:
          it has ≥2 distinct hints, or a footer separator (``·`` / ``•``), or
          is a short hint-only line.

        The footer check is **cursor-glyph independent**, so it catches pickers
        whose selection marker isn't one of the cursor glyphs above (Gemini /
        Cursor pickers, future CLIs). The "looks like a footer" gate is what
        keeps a long response sentence that merely mentions "esc to cancel"
        from matching. This method is used only by the ↑/↓ input filter, so a
        false positive is cheap (the arrow just reaches the CLI's native
        handling).
        """
        if not display_lines:
            return False
        tail = display_lines[-_SELECTION_DIALOG_TAIL_ROWS:]
        # Per-row (not on the joined string) so a row ending in `›` followed by
        # a row starting `1.` can't cross-match into a phantom `› 1.` cursor.
        if any(_SELECTION_CURSOR_RE.search(row) for row in tail):
            return True
        for row in tail:
            stripped = row.strip()
            compact = stripped.replace(' ', '').lower()
            hits = sum(tok in compact for tok in _SELECTION_DIALOG_FOOTER_TOKENS)
            if hits == 0:
                continue
            if (hits >= 2
                    or any(sep in stripped for sep in _SELECTION_DIALOG_SEPARATORS)
                    or len(stripped) <= _SELECTION_DIALOG_FOOTER_MAX_LEN):
                return True
        return False

    def screen_shows_selection_dialog_strict(
        self, display_lines: list[str],
    ) -> bool:
        """Prose-proof variant of :meth:`screen_shows_selection_dialog` for
        use in STATE TRANSITIONS (the running→idle interactive-UI hold),
        where a false positive is NOT cheap: it would wrongly keep the
        session RUNNING (and suppress the idle notification) for up to the
        safety-silence cap.

        Two differences from the lenient version, both to reject ordinary
        response prose that merely mentions a keyboard affordance:

        * The footer leg drops the "short single-hint line" clause.  A
          response line like ``- Press Enter to confirm`` (≤40 chars, one
          hint, no separator) is prose, not a dialog footer; a real Claude
          dialog footer puts the affordances on ONE line with a ``·``/``•``
          separator (``Enter to confirm · Esc to cancel``) or carries ≥2
          hints, so it is still matched.
        * The numbered-cursor leg scans the WHOLE screen, not just the
          bottom rows.  A tall many-option dialog renders its focused
          ``❯ N.`` cursor on an option that has scrolled ABOVE the bottom
          window, yet the dialog is unmistakably live.  The numbered cursor
          glyph (``❯``/``›``/``▶``/``▸`` immediately before ``N.``/``N)``) is
          an Ink render artifact that does not occur in response prose, so a
          full-screen scan stays prose-proof.

        Used by the cursor+silence ``running→idle`` guard so it agrees with
        the ↑/↓ ``screen_has_active_dialog()`` gate for tall pickers/dialogs
        WITHOUT the lenient leg's prose false-positives.
        """
        if not display_lines:
            return False
        if self.screen_shows_numbered_selection_cursor(display_lines):
            return True
        # A footer line that LOOKS like a footer (≥2 hints or a separator),
        # not a prose sentence quoting a single affordance.
        for row in display_lines[-_SELECTION_DIALOG_TAIL_ROWS:]:
            stripped = row.strip()
            compact = stripped.replace(' ', '').lower()
            hits = sum(tok in compact for tok in _SELECTION_DIALOG_FOOTER_TOKENS)
            if hits >= 2 or any(
                    sep in stripped for sep in _SELECTION_DIALOG_SEPARATORS):
                return True
        return False

    def screen_shows_numbered_selection_cursor(
        self, display_lines: list[str],
    ) -> bool:
        """True iff a numbered selection cursor (``❯``/``›``/``▶``/``▸``
        immediately before ``N.`` / ``N)``) appears ANYWHERE on screen.

        This glyph-before-a-number is an Ink render artifact of a live
        menu/picker/question dialog; it does NOT occur in ordinary response
        prose (verified empirically against captured sessions - the only
        non-option-row occurrences are the trust dialog's own option list).
        That makes it a PROSE-PROOF, high-confidence "a selectable dialog is
        open" signal, safe to drive a state transition off.

        Scans the whole screen (not just the bottom rows) on purpose: a tall
        many-option question renders its focused ``❯ N.`` cursor on an option
        that has scrolled ABOVE the bottom window while the dialog is still
        very much open.  Used to PROMOTE such a dialog to a waiting state so
        ↑/↓ keep reaching it indefinitely (no idle-timeout cap).
        """
        return any(_SELECTION_CURSOR_RE.search(row) for row in display_lines)

    @property
    def valid_signal_states(self) -> frozenset[str]:
        """States that can appear in the hook signal file."""
        return SIGNAL_STATES

    @property
    def running_indicator_patterns(self) -> list[bytes]:
        """Compact patterns (ANSI-stripped, spaces+newlines removed) that,
        when visible on screen, mean the CLI is actively processing even
        though no hook has fired to say so.

        Primary use case: long-running operations the CLI does without
        emitting a Stop/Notification event, e.g. Claude's "Compacting
        conversation…" during /compact and auto-compact.  When any
        pattern matches the compact screen text, the state tracker:

        - Transitions idle → running if detected while idle
        - Ignores a running → idle signal (keeps running)
        - Skips the running → idle cursor+silence fallback
        - Skips the silence-timeout safety fallback

        Return empty to opt out (default).
        """
        return []

    @property
    def idle_indicator_patterns(self) -> list[bytes]:
        """Compact patterns (ANSI-stripped, spaces+newlines removed) that,
        in the bottom rows, mean the CLI has returned to its idle prompt.

        Set this for CLIs whose idle prompt is NOT quiescent — e.g.
        GitHub Copilot animates its input box and emits PTY output
        continuously even when idle, so the tracker's silence-based
        running→idle fallbacks never fire and the session sticks in
        RUNNING.  When non-empty, the state tracker drives RUNNING
        transitions off the footer instead of output silence: the idle
        indicator appearing (with the running indicator gone, and no
        dialog footer) ends the turn, and the cursor-based auto-resume
        heuristic is disabled (the cursor toggles during the idle
        animation and would otherwise false-resume idle→running).

        Return empty to opt out (default) — the silence/cursor
        heuristics are used as before.
        """
        return []

    @property
    def input_dialog_patterns(self) -> list[bytes]:
        """Compact patterns matched on the bottom rows; if ANY is
        present the dialog is the CLI asking the USER a question /
        awaiting free input — i.e. ``needs_input`` rather than a
        tool-permission prompt (``needs_permission``, via
        :attr:`dialog_patterns`).

        The distinction matters: ``needs_permission`` is auto-approved in
        ALWAYS mode, but a question must reach the user (mirrors the
        AskUserQuestion exclusion).  GitHub Copilot's ``ask_user`` dialogs
        say "enter to confirm" (menu) or "enter to submit" (free-text),
        either of which marks a question, vs a permission prompt's "enter
        to select", so the two are separable.

        Default empty — providers that don't separate the two report
        ``needs_permission`` for every detected dialog.
        """
        return []

    @property
    def cursor_hidden_while_idle(self) -> bool:
        """Whether the CLI keeps the terminal cursor hidden during idle.

        Full-screen TUIs (Ratatui) hide the cursor permanently and
        manage their own cursor rendering.  When True, the auto-resume
        cursor visibility check is disabled (cursor hidden doesn't
        indicate processing).  Defaults to False (Ink TUIs show cursor
        when idle).
        """
        return False

    @property
    def dialogs_hide_cursor(self) -> bool:
        """Whether the CLI hides the terminal cursor while a
        permission/menu dialog is on screen.

        Drives whether the running→needs_permission proactive detection
        in the cursor+silence fallback trusts a *certain* dialog footer
        even when the cursor is hidden.  For most Ink CLIs the cursor is
        VISIBLE at a dialog and a *hidden* cursor instead means the CLI
        is still processing — so dialog-ish text on screen is transient
        render, not a real prompt (see
        ``tests/integration/test_dialog_false_positives.py``).  The
        default False keeps that detection cursor-gated.

        Full-screen TUIs like GitHub Copilot HIDE the cursor while their
        menu dialogs are up; for them the cursor is not a reliable
        "still working" signal, so they override this to True and a
        certain dialog footer promotes regardless of cursor visibility.
        """
        return False

    @property
    def silence_timeout(self) -> Optional[float]:
        """Override the default silence timeout (seconds) for this CLI.

        Return None to use the global SAFETY_SILENCE_TIMEOUT constant.
        Full-screen TUIs (Ratatui) that output constantly during processing
        can use a shorter timeout since any output gap indicates idle.
        """
        return None

    # -- Transcript-based idle detection ---------------------------------

    @property
    def transcript_sessions_dir(self) -> Optional[Path]:
        """Directory where the CLI stores session transcripts.

        When set, the state tracker polls the most recent transcript
        for completion events, enabling near-instant idle detection
        instead of relying on the silence timeout.

        Return None if the CLI doesn't have accessible transcripts.
        """
        return None

    def read_transcript_completion(self, since: float = 0) -> Optional[str]:
        """Check the CLI's transcript for a task-completion event.

        Reads the tail of the most recently modified transcript file
        and looks for a ``task_complete`` event whose ISO timestamp is
        newer than ``since`` (Unix epoch).  This prevents detecting
        stale completions from previous turns when the transcript is
        incrementally updated (user message written before task_complete).

        Called every poll cycle (~0.5s), so must be fast:
        - Only scans today's date directory (not full rglob)
        - Reads only the last 32KB of the file

        Args:
            since: Unix timestamp.  Only return completions with an
                ISO timestamp strictly after this.

        Returns:
            The last assistant message text, or None if not found.
        """
        sessions_dir = self.transcript_sessions_dir
        if sessions_dir is None or not sessions_dir.exists():
            return None
        try:
            transcript = self._find_active_transcript(sessions_dir)
            if transcript is None:
                return None
            if time.time() - transcript.stat().st_mtime > 30:
                return None
            file_size = transcript.stat().st_size
            chunk_size = 32768
            with open(transcript, 'rb') as f:
                start = max(0, file_size - chunk_size)
                f.seek(start)
                tail = f.read()
            for raw_line in reversed(tail.split(b'\n')):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                    payload = entry.get('payload', {})
                    if payload.get('type') == 'task_complete':
                        # Check the entry's timestamp against 'since'
                        ts_str = entry.get('timestamp', '')
                        if ts_str and since > 0:
                            entry_dt = datetime.fromisoformat(
                                ts_str.replace('Z', '+00:00'),
                            )
                            entry_ts = entry_dt.timestamp()
                            if entry_ts <= since:
                                return None  # Stale completion
                        msg = payload.get('last_agent_message', '')
                        return msg.strip() if msg else None
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        except OSError:
            pass
        return None

    def _find_active_transcript(self, sessions_dir: Path) -> Optional[Path]:
        """Find the most recently modified transcript in today's directory.

        Much faster than rglob — only lists files in today's date dir.
        Falls back to yesterday if today's dir doesn't exist yet.
        """
        today = date.today()
        for d in (today, today - timedelta(days=1)):
            day_dir = sessions_dir / d.strftime('%Y/%m/%d')
            if not day_dir.is_dir():
                continue
            best: Optional[Path] = None
            best_mtime: float = 0
            try:
                for f in day_dir.iterdir():
                    if f.suffix == '.jsonl':
                        mt = f.stat().st_mtime
                        if mt > best_mtime:
                            best = f
                            best_mtime = mt
            except OSError:
                continue
            if best is not None:
                return best
        return None

    def transcript_says_running(
        self,
        since: float,
        cwd: str,
        tag: str = '',
        storage_dir: Optional[Path] = None,
    ) -> bool:
        """Return True iff the transcript proves the agent is still in
        a tool loop after ``since``.

        Used as a final gate before flipping RUNNING → IDLE: when a hook
        signal or screen heuristic claims idle but the transcript shows
        an unanswered ``tool_use`` block (with timestamp > ``since``),
        block the flip — the agent is still working.

        Default: False (no transcript awareness).  Providers that have
        accessible per-session transcripts override this.

        Args:
            since: Unix timestamp.  Only entries strictly newer than
                this count.  Typically ``_running_since``.
            cwd: The session's working directory (used to locate the
                transcript file for cwd-bound CLIs like Claude).
            tag: The Leap session tag.  Used together with
                ``storage_dir`` to look up the recorded ``session_id``
                in ``<storage_dir>/cli_sessions/<cli>/<tag>.json`` so
                we can pinpoint the transcript file precisely.  Empty
                string falls back to mtime-based file selection.
            storage_dir: The project's ``.storage`` directory.
                ``None`` falls back to mtime-based file selection.
        """
        return False

    def transcript_says_interrupted(
        self,
        since: float,
        cwd: str,
        tag: str = '',
        storage_dir: Optional[Path] = None,
    ) -> bool:
        """Return True iff the transcript proves the agent loop was
        cancelled by the user mid-turn — i.e. a user-interrupt marker
        appears after the most recent assistant tool_use of the current
        turn.

        Used as a positive signal in cursor+silence and signal=idle
        paths to flip RUNNING → INTERRUPTED when the Ink TUI's redraw
        prevented pyte from capturing the on-screen "Interrupted" text.

        Default: False (no transcript awareness).  Same arguments as
        :meth:`transcript_says_running`.
        """
        return False

    # -- Menu / option parsing -------------------------------------------

    @property
    def has_numbered_menus(self) -> bool:
        """Whether the CLI uses numbered menu options for prompts."""
        return True

    @property
    def menu_option_regex(self) -> Optional[re.Pattern[str]]:
        """Regex to extract numbered options from prompt output.

        Must have groups: (1) option number, (2) option label.
        Return None if the CLI doesn't use numbered menus.
        """
        return None

    @property
    def free_text_option_prefix(self) -> Optional[str]:
        """Label prefix for the 'type your own answer' option."""
        return None

    @property
    def below_separator_option_prefix(self) -> Optional[str]:
        """Label prefix for options below a separator that need arrow-key nav."""
        return None

    @property
    def custom_answer_targets_composer(self) -> bool:
        """Whether ``send_custom_answer`` types directly into the CLI composer.

        Drives the server's input-preservation wrap in
        ``_run_dialog_action``:

        * **True** (default) — answer chars + ``\\r`` are written
          into the focused composer.  When the user has typed but
          not submitted text, those chars concatenate onto it and
          the whole line gets submitted as one message.  The wrap
          must **pre-clear** the composer before the action types.

        * **False** — answer is routed through a menu first
          (Claude navigates to the "Type something" option, after
          which Ink switches the highlighted row into a text-input
          subdialog that absorbs the chars).  The composer is
          untouched, so pre-clear isn't needed; the wrap uses the
          post-clear/restore mode for defense against trailing-CR
          leaks.

        The default derives from ``free_text_option_prefix``:
        providers with a free-text menu option route through the
        menu (Claude); others type into the composer.
        """
        return self.free_text_option_prefix is None

    # -- Input protocol --------------------------------------------------

    @property
    def interrupt_key(self) -> bytes:
        """Key sequence that interrupts/cancels a running turn.

        Sent to the PTY (and fed to the state tracker's ``on_input``)
        when a Leap client/monitor requests an interrupt.  Defaults to
        Escape (``\\x1b``), which cancels in Claude / Codex / Cursor
        Agent / Gemini.  Override for CLIs that cancel on a different
        key — e.g. GitHub Copilot ignores Escape mid-turn and cancels on
        Ctrl+C (``\\x03``).

        Must be a sequence that ``on_input`` recognises as an interrupt
        (Escape ``\\x1b`` or Ctrl+C ``\\x03``) so the tracker arms
        ``_interrupt_pending`` alongside the keystroke reaching the CLI.
        """
        return b'\x1b'

    @property
    def paste_settle_time(self) -> float:
        """Settle time (seconds) after sending multi-line text."""
        return 0.15

    @property
    def single_settle_time(self) -> float:
        """Settle time (seconds) after sending single-line text."""
        return 0.05

    @property
    def image_prefix(self) -> str:
        """Prefix character for image file attachments (e.g. '@')."""
        return '@'

    @property
    def supports_image_attachments(self) -> bool:
        """Whether the CLI supports inline image file attachments."""
        return False

    # -- Input history (CLI ↑/↓ recall) ----------------------------------

    def input_history(self, cwd: str) -> Optional[list[str]]:
        """Return the CLI's persisted input history for the given cwd.

        Leap intercepts ↑/↓ at the input box so it can drive history
        recall itself — without this, the recalled text lives only in
        the CLI's TUI render and never enters Leap's input mirror, so
        a subsequent ``^^`` would snapshot an empty buffer.  By
        re-implementing recall on top of the CLI's on-disk history,
        Leap can inject the recalled text into the input line, keep
        its mirror in sync, and have ``^^`` capture exactly what the
        user sees.

        The returned list is ordered **oldest → newest** (so the
        last element is what plain ↑ should select first), and must
        match what the CLI itself would surface for ↑/↓ in this cwd —
        e.g. Claude filters by ``project == cwd``, Gemini stores per
        project, Codex/Cursor are global.

        Return ``None`` to opt out — Leap then leaves ↑/↓ as a
        passthrough to the CLI (the recalled text stays invisible to
        Leap's mirror, but the CLI's own recall keeps working).
        Return ``[]`` when history is supported but empty.

        Implementations should be cheap (called once per ↑/↓ press
        with an mtime guard on top), tolerate format drift gracefully
        (catch exceptions, return ``None`` on parse failure), and
        avoid raising — a crash here would propagate into the input
        filter.

        Args:
            cwd: The session's current working directory, used by
                providers that scope history per project.
        """
        return None

    # -- Hook configuration ----------------------------------------------

    @property
    @abstractmethod
    def hook_config_dir(self) -> Path:
        """Directory where the CLI stores its configuration/hooks.

        The leap-hook.sh script will be copied into this directory
        during installation.  E.g. ``~/.claude/hooks``, ``~/.codex``, ``~/.cursor``, or ``~/.gemini``.
        """

    @property
    def requires_binary_for_hooks(self) -> bool:
        """Whether hook configuration should be skipped if the CLI binary is not found.

        Return True if hooks should only be configured when the CLI
        is actually installed (e.g. Codex).  Return False to always
        configure hooks (e.g. Claude Code, which is the primary CLI).
        """
        return False

    @abstractmethod
    def configure_hooks(self, hook_script_path: str) -> None:
        """Install hooks into the CLI's configuration.

        Args:
            hook_script_path: Absolute path to the leap-hook.sh script.
        """

    @abstractmethod
    def hooks_installed(self) -> bool:
        """Return True iff Leap's hooks are wired up for this CLI.

        Mirror image of :meth:`configure_hooks` — checks both that the
        hook script exists at ``hook_config_dir / 'leap-hook.sh'`` AND
        that the CLI's settings file references it.  Both halves must
        be present; if either is missing or the settings file is
        unreadable / malformed, return False (not raise).

        Used by the session-start gate to detect "user installed this
        CLI after Leap" (or "user wiped their config") and point them
        at ``leap --reconfigure`` before the server spawns.

        The check is intentionally lenient about *which* hook entries
        are present — any single entry whose ``command`` references
        ``leap-hook.sh`` counts.  This way, adding new hook events to
        ``configure_hooks()`` later doesn't retroactively flag older
        installs as broken.
        """

    def deconfigure_hooks(self) -> None:
        """Remove Leap's hook scripts from the CLI's config directory.

        Removes ``leap-hook.sh`` and ``leap-hook-process.py`` from
        :attr:`hook_config_dir`.  Best-effort: never raises.

        Providers that also write Leap entries into the CLI's settings or
        config files must override this method to undo those changes, then
        call ``super().deconfigure_hooks()`` to clean up the script files.
        """
        for name in ("leap-hook.sh", "leap-hook-process.py"):
            try:
                (self.hook_config_dir / name).unlink(missing_ok=True)
            except OSError:
                pass

    # -- CLI binary lookup -----------------------------------------------

    def find_cli(self) -> Optional[str]:
        """Find the CLI executable in PATH.

        Returns:
            Absolute path to the CLI binary, or None if not found.
        """
        for path_dir in os.environ.get('PATH', '').split(':'):
            candidate = os.path.join(path_dir, self.command)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    # -- Environment variables -------------------------------------------

    def get_spawn_env(
        self, tag: Optional[str], signal_dir: Optional[Path],
    ) -> dict[str, str]:
        """Build extra environment variables for the spawned CLI process.

        Args:
            tag: Session tag name.
            signal_dir: Directory for signal files.

        Returns:
            Dict of environment variables to merge into os.environ.
        """
        env: dict[str, str] = {}
        if tag:
            env['LEAP_TAG'] = tag
        if signal_dir:
            env['LEAP_SIGNAL_DIR'] = str(signal_dir)
        # Pass the current Python interpreter path so hook scripts can use
        # the venv Python instead of relying on a bare `python3` in PATH.
        env['LEAP_PYTHON'] = sys.executable
        # Tell the hook which provider is firing so recording is routed to
        # the right `.storage/cli_sessions/<cli>/` subdir without the hook
        # needing to probe every provider's transcript-path signature.
        env['LEAP_CLI_PROVIDER'] = self.name
        return env

    # -- Resume support (leap --resume) ----------------------------------

    @property
    def supports_resume(self) -> bool:
        """Whether this CLI supports resuming via `leap --resume`.

        Default ``False``.  Override and return ``True`` after implementing
        :meth:`extract_session_id` and :meth:`resume_args`.
        """
        return False

    def extract_session_id(self, hook_data: dict) -> Optional[str]:
        """Return the CLI's session id from a hook payload, or ``None``.

        ``hook_data`` is the JSON the CLI sent to leap-hook.sh on stdin
        (Stop/Notification events).  Different CLIs surface the id
        differently — e.g. Claude encodes it in the ``transcript_path``
        filename, Codex passes it directly as ``session_id``.  Return
        ``None`` when the payload isn't one of this CLI's sessions so the
        hook knows to skip recording.
        """
        return None

    def resume_args(self, session_id: str) -> list[str]:
        """CLI argv tokens to resume the given session.

        These are **prepended** to the user's CLI flags before the server
        spawns the binary, so positional subcommand forms (Codex's
        ``resume <id>``) stay in the right spot.  Return empty list to
        opt out.  Example implementations::

            # Claude: ``claude --resume=<id>``
            return [f'--resume={session_id}']

            # Codex: ``codex resume <id>``
            return ['resume', session_id]
        """
        return []

    def session_exists(self, session_id: str, cwd: str) -> bool:
        """Whether this CLI's session is still resumable on disk.

        Called by the picker to filter out records pointing at sessions
        that have been deleted out-of-band (e.g. user ran ``rm -rf``
        on the CLI's storage dir).  Default returns ``True`` — used by
        CLIs that key sessions by id alone (Codex) where we can't
        cheaply verify without invoking the CLI itself.

        Override in providers whose storage layout lets us cheaply
        stat the session's home dir (Cursor's ``~/.cursor/chats/<hash>/<id>/``,
        Claude / Gemini already self-filter via the picker's
        ``transcript_path`` existence check).
        """
        return True

    @property
    def requires_cwd_bound_resume(self) -> bool:
        """Whether the recorded cwd matters when resuming this CLI's session.

        Set ``True`` when the CLI stores transcripts in a cwd-derived
        location (Claude's slug, Gemini's slug-registry) and
        ``<cli> resume <id>`` only finds the session when run from that
        cwd — leap then needs to either ``chdir`` into the recorded cwd
        or relocate the transcript via :meth:`relocate_session`.

        Default ``False``: the CLI keys sessions by UUID alone (Codex), so
        ``leap --resume`` skips its cwd-choice prompt and lets the CLI take
        over from the user's current working directory.  (Cursor Agent,
        despite resuming by chat UUID, stores its chats in a cwd-derived
        directory, so it overrides this to ``True`` and relocates via
        :meth:`relocate_session`.)
        """
        return False

    def relocate_session(
        self,
        session_id: str,
        src_cwd: str,
        dst_cwd: str,
        *,
        transcript_path: str = '',
        on_committed: Optional[Any] = None,
    ) -> Optional[str]:
        """Move this CLI's on-disk session state from ``src_cwd`` to ``dst_cwd``.

        Used by ``leap --resume`` when the user picks a session that was
        recorded in directory A but is currently working in directory B
        — instead of forcing a ``cd`` into A, the resume picker calls
        this to relocate the session's transcript so the CLI can find
        it under B's slug.

        Returns the new transcript path on success, or ``None`` if this
        CLI doesn't support cross-cwd relocation (the picker will fall
        back to ``chdir`` into the original cwd).  Raise an exception
        on real failure — callers exit non-zero.

        ``transcript_path`` is the path the picker recorded for this
        session.  Most providers (Claude/Gemini/Cursor) compute their
        own source paths from ``src_cwd`` + ``session_id`` and don't
        need it; Codex stores sessions at a date+UUID path that's not
        derivable from cwd, so its no-op "logical move" implementation
        uses this value to pass the unchanged path through to the
        ``on_committed`` callback.

        ``on_committed`` is invoked with the new path *after* the
        destination is verified in-place but *before* the source is
        deleted, so caller-side bookkeeping happens inside the same
        signal-blocked critical section the file move uses.

        Only called when :attr:`requires_cwd_bound_resume` is ``True``
        and the user picks "current cwd" in the picker — overriding it
        without also setting ``requires_cwd_bound_resume = True`` is a
        no-op.
        """
        return None

    # -- Hook payload extraction -----------------------------------------

    def extract_last_assistant_message(self, hook_data: dict) -> str:
        """Return the last assistant-generated text from a hook payload.

        Most CLIs (Codex, Cursor, Gemini) pass the string directly as
        ``hook_data['last_assistant_message']``.  Claude Code writes its
        output to a JSONL transcript and expects consumers to tail it —
        :class:`ClaudeProvider` overrides this to do that.  Consumed by
        the Slack integration to preview the last reply.
        """
        msg = hook_data.get('last_assistant_message', '')
        return msg if isinstance(msg, str) else ''

    def extract_last_user_prompt(
        self,
        cwd: str,
        tag: str,
        storage_dir: Optional[Path],
        cli_name: str = '',
    ) -> str:
        """Best-effort: return the user's most recent prompt as the CLI
        recorded it (transcript or equivalent).

        ``cli_name`` is the recorded CLI/provider key (the ``cli_sessions``
        subdir), so a *custom* CLI built atop a base provider resolves its own
        transcript dir rather than the base's.  Defaults to ``''`` for
        backward compatibility; implementers fall back to ``self.name``.

        Used by the monitor's "Last Msg" column to surface prompts that
        bypassed Leap's PTY input path — most importantly Claude Code's
        ``@file:lines`` references injected via the VS Code / JetBrains
        plugin's Cmd+Option+K shortcut, which travels through an IDE
        side-channel and never appears in Leap's ``recently_sent``.

        Default returns ``''`` — providers without a readable user-message
        transcript opt out and the monitor falls back to ``recently_sent``.
        :class:`leap.cli_providers.claude.ClaudeProvider` overrides this
        to walk the JSONL transcript.
        """
        return ''

    @property
    def supports_context_usage(self) -> bool:
        """Whether this CLI can report context-window usage at all.

        Drives the monitor's "Context" column for the *unsupported* case:
        when ``False`` the cell renders ``N/A`` (the CLI fundamentally can't
        expose usage - e.g. Cursor), distinct from a ``None`` result from
        :meth:`context_usage` on a *supported* CLI, which renders blank
        ("supported, but no data yet").  Providers that implement
        :meth:`context_usage` override this to ``True``.
        """
        return False

    def context_usage(self, cli_name: str, tag: str,
                      storage_dir: Path) -> Optional['ContextUsage']:
        """Context-window usage for this session, or None if not available yet.

        Powers the monitor's "Context" column: how full the model's context
        window currently is (so the user sees how close a session is to
        auto-compaction).  Each provider locates its own source from
        ``(cli_name, tag, storage_dir)`` -- transcript CLIs resolve their
        transcript with ``latest_transcript_for(storage_dir, cli_name, tag)``
        (``cli_name`` is the session's *recorded* name, so custom CLIs hit the
        right ``cli_sessions/<name>/`` subdir) and parse it; Copilot reads the
        per-tag state file its status line writes under ``storage_dir/sockets``.

        Default returns ``None``.  Override together with
        :attr:`supports_context_usage` -> ``True``.  See
        :mod:`leap.utils.context_usage` for the per-CLI parsers.
        """
        return None

    @property
    def supports_cost(self) -> bool:
        """Whether this CLI can estimate a session's token spend / USD cost.

        Drives the extra "Last message" / "Session total" lines on the
        monitor's Context-cell tooltip.  ``False`` (the default) means the
        tooltip shows only the context-window line; providers that implement
        :meth:`session_cost` override this to ``True``.  Independent of
        :attr:`supports_context_usage`: a CLI can report window occupancy
        without enough per-turn / cumulative data to price a session.
        """
        return False

    def session_cost(self, cli_name: str, tag: str,
                     storage_dir: Path) -> Optional['CostInfo']:
        """Cumulative token + USD estimate for this session, or ``None``.

        Powers the cost lines on the Context-cell tooltip (last-message tokens
        and cost, whole-session tokens and cost).  Resolves the same source as
        :meth:`context_usage` -- transcript CLIs use
        ``latest_transcript_for(storage_dir, cli_name, tag)``.  Default returns
        ``None``; override together with :attr:`supports_cost` -> ``True``.
        See :mod:`leap.utils.cost_usage` and :mod:`leap.utils.pricing`.
        """
        return None

    # -- CLI-specific input behaviors ------------------------------------

    def send_message(
        self,
        process: pexpect.spawn,
        message: str,
        send_lock: Any,
        write_fn: Any,
        wait_fn: Any,
    ) -> None:
        """Send a regular message to the CLI.

        Default implementation: write text, wait for settle, send CR.

        Args:
            process: The pexpect process.
            message: Message text to send.
            send_lock: Threading lock (already held by caller).
            write_fn: Callable to write raw data to PTY.
            wait_fn: Callable to wait for output settle.
        """
        settle = self.paste_settle_time if '\n' in message else self.single_settle_time
        write_fn(message)
        wait_fn(settle_time=settle)
        write_fn('\r')

    def send_image_message(
        self,
        process: pexpect.spawn,
        message: str,
        send_lock: Any,
        write_fn: Any,
        wait_fn: Any,
    ) -> None:
        """Send an image attachment message.

        Uses fixed sleeps instead of ``wait_fn`` to avoid its Phase 1
        timeout (up to 2 s) which can give file-picker autocomplete
        time to open and capture the CR. Two CRs are sent: the first
        confirms any autocomplete, the second submits.

        Args:
            process: The pexpect process.
            message: Message text (may include image reference).
            send_lock: Threading lock (already held by caller).
            write_fn: Callable to write raw data to PTY.
            wait_fn: Callable to wait for output settle.
        """
        write_fn(message)
        time.sleep(1.5)   # Let autocomplete fully render
        write_fn('\r')    # Confirm file selection
        time.sleep(1.0)   # Let TUI process the selection
        write_fn('\r')    # Submit the message

    def is_image_message(self, message: str) -> bool:
        """Check if a message is an image attachment.

        Args:
            message: The message to check.

        Returns:
            True if this message requires special image handling.
        """
        return self.supports_image_attachments and message.startswith(self.image_prefix)

    def select_option(
        self,
        option_num: int,
        options: dict[int, str],
        pty_send: Any,
        pty_sendline: Any,
    ) -> dict[str, Any]:
        """Select a numbered option in a permission/question dialog.

        Args:
            option_num: The option number to select.
            options: Dict of {number: label} for available options.
            pty_send: Callable to send raw data to PTY.
            pty_sendline: Callable to send data + CR to PTY.

        Returns:
            Response dict with 'status' key.
        """
        return {'status': 'error', 'error': 'option selection not supported'}

    def send_custom_answer(
        self,
        text: str,
        options: dict[int, str],
        pty_send: Any,
    ) -> dict[str, Any]:
        """Send a free-form text answer to a question dialog.

        Args:
            text: The user's text answer.
            options: Dict of {number: label} for available options.
            pty_send: Callable to send raw data to PTY.

        Returns:
            Response dict with 'status' key.
        """
        return {'status': 'error', 'error': 'custom answers not supported'}

    # -- Hook signal file parsing ----------------------------------------

    def parse_signal_file(self, raw: str) -> Optional[str]:
        """Parse the signal file content and return the state.

        Default implementation: parse JSON with 'state' key.

        Args:
            raw: Raw file content.

        Returns:
            A valid state string, or None.
        """
        try:
            data = json.loads(raw)
            state = data.get('state', '')
            # Backward compat: old hooks may write 'has_question'
            state = SIGNAL_ALIASES.get(state, state)
            if state in self.valid_signal_states:
                return state
        except (json.JSONDecodeError, AttributeError):
            pass
        return None
