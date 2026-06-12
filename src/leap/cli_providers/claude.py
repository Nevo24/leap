"""
Claude Code CLI provider.

Implements the CLIProvider interface for Anthropic's Claude Code CLI.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from leap.cli_providers.base import CLIProvider
from leap.utils.atomic_write import atomic_write_json
from leap.utils.claude_session_move import relocate_claude_session, slugify
from leap.utils.context_usage import (
    ContextUsage,
    claude_context_usage,
    claude_statusline_context_usage,
)
from leap.utils.cost_usage import CostInfo, claude_session_cost_cached
from leap.utils.menu import MENU_OPTION_RE
from leap.utils.resume_store import latest_transcript_for


_TRANSCRIPT_TAIL_BYTES = 32768
_TRANSCRIPT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Matches ``[Pasted text #N]`` / ``[Pasted text #N +K lines]`` in
# history ``display`` strings.  Captures the paste id so the resolver
# can look it up in the entry's ``pastedContents`` dict.
_PASTED_TEXT_RE: re.Pattern[str] = re.compile(
    r'\[Pasted text #(\d+)(?:\s+\+\d+\s+lines?)?\]'
)

# Reverse-chunk reading bounds for user-prompt extraction.  A 32 KiB
# tail (the assistant-message convention) routinely buries user prompts
# during heavy tool use — verified on a real session where the user's
# last text landed 260 KiB before EOF after a burst of tool calls.  We
# read backward in chunks until we find a real prompt, capped at 4 MiB
# so a pathological transcript can't make us page in the entire file.
_USER_PROMPT_REVERSE_CHUNK = 65536
_USER_PROMPT_REVERSE_MAX = 4 * 1024 * 1024

# (path_str, mtime_ns, size) → extracted prompt.  Keyed on identity-
# changing facts only, so any actual write invalidates.  Lifetime is
# the process — the monitor restarts often enough that unbounded
# growth from stale paths isn't a concern in practice, and each
# session contributes at most one entry.
_USER_PROMPT_CACHE: dict[tuple[str, int, int], str] = {}


class ClaudeProvider(CLIProvider):
    """Provider for Claude Code CLI (Ink TUI, TypeScript)."""

    # -- Identity --------------------------------------------------------

    @property
    def name(self) -> str:
        return 'claude'

    @property
    def command(self) -> str:
        return 'claude'

    @property
    def display_name(self) -> str:
        return 'Claude Code'

    # -- State detection patterns ----------------------------------------

    @property
    def interrupted_pattern(self) -> bytes:
        return b'Interrupted'

    @property
    def confirmed_interrupt_pattern(self) -> Optional[bytes]:
        # Disabled: pattern matching on raw PTY buffers is unreliable for
        # Ink TUI — full-screen redraws include scrollback content, and
        # after ANSI stripping + space removal, unrelated text (commit
        # messages, code, conversation) containing "Interrupted" near a
        # middle dot (common TUI decoration) falsely matches.
        #
        # Interrupt detection for Claude relies on the _interrupt_pending
        # flag (requires Escape/Ctrl+C before the Stop hook fires).
        # Self-interrupts (tool timeouts) are covered by the Notification
        # hook writing needs_input for the interrupt dialog.
        return None

    @property
    def dialog_patterns(self) -> list[bytes]:
        return [b'Entertoselect', b'Esctocancel']

    @property
    def running_indicator_patterns(self) -> list[bytes]:
        # Claude's "Compacting conversation…" spinner is shown during
        # both the /compact slash command and auto-compact between turns.
        # No hook fires for compaction, and between-turns auto-compact
        # starts immediately after a Stop hook has already written
        # ``idle`` — without this indicator the session would read as
        # idle even though Claude is still working.  In compact form
        # (spaces+newlines removed), "Compactingconversation" is
        # specific enough to avoid colliding with conversational text.
        return [b'Compactingconversation']

    def _has_numbered_menu(self, compact_text: str) -> bool:
        """Check for numbered menu cursor indicator (❯ or ›) before option 1."""
        # ❯ = U+276F, › = U+203A — both used by Ink TUI
        return '\u276f1.' in compact_text or '\u203a1.' in compact_text

    def has_dialog_indicator(self, compact_text: str) -> bool:
        """Lenient: standard footer patterns OR numbered menu cursor."""
        if super().has_dialog_indicator(compact_text):
            return True
        return self._has_numbered_menu(compact_text)

    def is_dialog_certain(self, compact_text: str) -> bool:
        """Strict: all standard footer patterns OR numbered menu cursor."""
        if super().is_dialog_certain(compact_text):
            return True
        return self._has_numbered_menu(compact_text)

    # Structural fingerprint of Claude's standard idle input box:
    #     ─────────────────────────  ← top horizontal rule
    #     ❯ <input text or empty>     ← input row
    #     ─────────────────────────  ← bottom horizontal rule
    # When the user invokes any slash-command picker (``/resume``,
    # ``/mcp``, ``/agents``, ``/config``, ``/doctor``, ``/effort``,
    # ``/login``, ``/memory``, ``/model``, ``/permissions``, ``/usage``,
    # ``/bug``, …) the picker UI takes over the bottom of the screen
    # and the sandwich is gone.  Detecting the sandwich (rather than
    # enumerating picker footers) means new Claude pickers work without
    # any code change.
    _HR_CHAR: str = '─'   # ─
    _PROMPT_CHAR: str = '❯'  # ❯
    # Selection-cursor glyphs the Ink TUI draws on the focused row of a
    # menu / picker / dialog (❯ = U+276F, › = U+203A).
    _SELECTION_CURSORS: tuple = ('❯', '›')
    # Minimum length of a horizontal-rule line.  Real Claude renders
    # the input-box HR at terminal width minus padding (≈ COLS chars
    # of ``─``).  40 is the lowest reasonable terminal width Claude
    # renders at — below that the UI breaks visually anyway.  The
    # ``/effort`` slider's ``──────▲──────`` axis is ~42 chars but
    # contains a non-``─`` glyph and is rejected by the strict purity
    # check below regardless of length, so 40 doesn't sacrifice
    # protection against inline rule widgets.
    _MIN_HR_LEN: int = 40
    # A short text label may be drawn INTO the input-box border (active
    # model / skill / plan-mode badge), e.g.
    # ``────psakdin-case-law-source────``.  Such a border is still the idle
    # box's rule; only its embedded text differs from a bare rule.  Cap the
    # label length so a prose line that merely contains a long ``─`` run is
    # not mistaken for a border.
    _HR_MAX_LABEL_LEN: int = 40
    # Punctuation (besides letters/digits) allowed inside a border badge -
    # the chars that appear in model / skill / plan-mode names and versions.
    _HR_LABEL_PUNCT: frozenset = frozenset('-_./()')

    def _is_hr_label_safe(self, label: str) -> bool:
        """True iff *label* (the non-``─``, non-space chars of a candidate
        border) is a plain TEXT badge - letters, digits, and a few name
        punctuation marks - rather than graphics.

        This is an ALLOWLIST, not a blocklist, on purpose: any graphical glyph
        (box-drawing table borders, block-element progress bars ``████░░░░``,
        geometric shapes, slider axes, arrows, percent bars) is rejected.  The
        failure mode is asymmetric - a border mis-classified as "not a border"
        merely falls back to the cap (idles after the safety timeout), whereas
        a too-lenient accept would false-IDLE a live screen.  So we err strict:
        a real badge with an unexpected char just loses the fast path, never
        causes a wrong idle.  ``─`` itself is never passed in (it's the rule
        glyph)."""
        return all(ch.isalnum() or ch in self._HR_LABEL_PUNCT for ch in label)

    def _is_prompt_box_hr(self, line: str) -> bool:
        """Line is a horizontal-rule border of the idle input box.

        A border is a terminal-wide run of ``─`` that may carry a short text
        label drawn into it (model / skill / plan-mode badge), e.g.
        ``────psakdin-case-law-source────``.  It must START with ``─`` and
        contain at least ``_MIN_HR_LEN`` rule glyphs.  Rejected:
        * slider widgets like ``──────▲──────`` (``▲`` is a widget glyph)
        * table / box borders like ``┌─────┬─────┐`` / ``├──┼──┤`` (box-
          drawing glyphs other than ``─``)
        * prose lines that merely contain a long ``─`` run (label longer
          than ``_HR_MAX_LABEL_LEN``, or the line not led by the rule glyph).

        An earlier version required *every* non-space char to be ``─``, which
        rejected a labelled border outright; combined with the cursor+silence
        guard (which holds RUNNING when the idle box is absent yet a ``❯`` is
        on screen) that wedged the session RUNNING whenever Claude drew a
        badge into the border and no Stop hook followed.
        """
        stripped = line.strip()
        if len(stripped) < self._MIN_HR_LEN:
            return False
        if not stripped.startswith(self._HR_CHAR):
            return False
        label = ''.join(
            ch for ch in stripped
            if ch != self._HR_CHAR and ch != ' '
        )
        if len(label) > self._HR_MAX_LABEL_LEN:
            return False
        if not self._is_hr_label_safe(label):
            return False
        return stripped.count(self._HR_CHAR) >= self._MIN_HR_LEN

    def _is_prompt_box_input_row(self, line: str) -> bool:
        """Line is the ``❯ ...`` input row at the centre of the box.

        Accepts ``❯`` alone (empty input) or ``❯`` followed by *any*
        whitespace + content.  Claude's TUI renders the gap between
        ``❯`` and the placeholder/typed text as U+00A0 (NBSP), not a
        plain space — ``str.isspace()`` covers both (and any other
        whitespace Claude might pick in the future).
        """
        stripped = line.lstrip()
        if not stripped.startswith(self._PROMPT_CHAR):
            return False
        rest = stripped[len(self._PROMPT_CHAR):]
        return not rest or rest[0].isspace()

    # Minimum non-blank rows required before the absence of the
    # sandwich is treated as evidence of a picker.  Real Claude pickers
    # always render 5+ rows (header + list/content + footer); below
    # that we're seeing a transient or boot-time screen, and defaulting
    # to ``idle visible`` (return True) preserves the legacy
    # strict-dialog-only behaviour.
    _IDLE_DETECT_MIN_ROWS: int = 5

    # Window of recent non-blank rows scanned for the input-box
    # sandwich.  Widened to 10 to cover multi-line input (Shift+Enter)
    # which inserts continuation rows between the ``❯`` row and the
    # bottom HR border.
    _IDLE_TAIL_WINDOW: int = 10

    def is_idle_prompt_visible(self, display_lines: list[str]) -> bool:
        """True iff Claude's idle input box is rendered at the bottom.

        The box looks like::

            ─────────────────────  ← top HR border
            ❯ <input or placeholder text>     ← input row
            [<continuation row 1>]            ← present iff multi-line
            [<continuation row 2>]              input has wrapped
            [─────────────────────]  ← bottom HR border (NOT on every build)
            <optional hint footer>             ← e.g. ``? for shortcuts``

        Detection: scan the last ``_IDLE_TAIL_WINDOW`` rows for an HR row
        *immediately* followed by a ``❯`` input row.  That top-HR→``❯``
        pairing is the box's defining signature.

        The closing **bottom** HR is present on some Claude builds but
        ABSENT on others — there the footer (e.g. ``⏵⏵ bypass permissions
        on (shift+tab to cycle) · ← for agents``) sits directly under the
        input row with no second rule.  An earlier version required a
        two-HR sandwich, so it classified those single-rule builds as "no
        idle box"; combined with the cursor+silence guard in the state
        tracker (which keeps RUNNING when the idle box is absent yet a
        ``❯``/selection cursor is on screen), that wedged the session in
        RUNNING forever after any no-Stop-hook idle — most visibly after a
        slash command like ``/cost`` that fires no Stop hook.  The ``❯``
        the guard then saw was this very input row's own prompt char.  So
        the bottom HR is now optional; only the top-HR→``❯`` pairing is
        required.

        Requiring ``❯`` to be the row directly after an HR (rather than
        anywhere on screen) is what prevents false positives from Claude's
        response text containing markdown ``---`` separators — the row
        after such a rule is prose, which never starts with ``❯``.  Real
        pickers/dialogs (``/model``, ``/resume``, ``/mcp``, permission
        prompts) never place a full-width pure-``─`` rule immediately above
        their focused ``❯`` option — their borders are rounded box-drawing
        glyphs (``╭``/``│``/``╰``) that ``_is_prompt_box_hr`` rejects — so
        they stay correctly classified as "idle box absent".

        Returns True (assume idle) when the screen has too little
        content to confidently call it a picker (``< _IDLE_DETECT_MIN_ROWS``
        non-blank rows).  This makes empty/transient screens behave
        like the legacy strict-dialog-only check.
        """
        tail = display_lines[-self._IDLE_TAIL_WINDOW:]
        for i, ln in enumerate(tail):
            if (self._is_prompt_box_hr(ln) and i + 1 < len(tail)
                    and self._is_prompt_box_input_row(tail[i + 1])):
                return True
        return len(display_lines) < self._IDLE_DETECT_MIN_ROWS

    def has_selection_cursor(self, display_lines: list[str]) -> bool:
        """True iff a ❯/› menu-selection cursor is in the recent tail rows.

        Distinguishes a real interactive UI (a picker/dialog with the cursor
        on a focused option) from plain response text such as a numbered list,
        which carries no selection glyph.  Consulted only when the idle box is
        absent, to decide whether the cursor+silence fallback holds RUNNING
        rather than idle+reset an on-screen UI.
        """
        return any(c in ln
                   for ln in display_lines[-self._IDLE_TAIL_WINDOW:]
                   for c in self._SELECTION_CURSORS)

    # Distinctive nav/dismiss markers from picker/dialog footers.  Chosen to
    # not appear in ordinary response prose (unlike a bare "Enter to select"),
    # and only ever matched against the bottom row (see has_interactive_footer).
    _INTERACTIVE_FOOTER_MARKERS: tuple = (
        'to navigate', 'esc to cancel', 'esc to close',
        'space to toggle', 'enter to confirm',
    )

    def has_interactive_footer(self, display_lines: list[str]) -> bool:
        """True iff the bottom row is a picker/dialog nav/dismiss footer.

        Covers interactive UIs that draw no ❯/› on a focused row but render a
        footer at the bottom (e.g. the /agents tabbed view: ``↑/↓ to navigate
        · Esc to close``).  Only the last non-blank row is checked, with
        markers distinctive enough that response text merely mentioning
        ``Enter to select`` mid-sentence (with a normal ``> `` prompt on the
        last row) does not match.
        """
        if not display_lines:
            return False
        last = display_lines[-1].lower()
        return any(m in last for m in self._INTERACTIVE_FOOTER_MARKERS)

    # Footer / mode-line text Claude renders while a background ``Monitor`` task
    # is active after the turn ends (idle prompt visible).  Validated against a
    # live churn (Claude Code v2.1.162): the persistent mode line reads
    # ``bypass permissions on · 1 monitor · ← for agents`` and the activity line
    # ``✻ <word> for Ns · 1 monitor still running``.  The activity word is
    # randomized (Cogitated / Cooked / Brewed / Baked / Crunched), so it is NOT
    # matched; the stable ``<n> monitor`` count - present in both lines, absent
    # from the normal ``(shift+tab to cycle)`` idle footer - is.
    _CHURN_MARKERS: tuple = ('monitor still running',)
    _CHURN_COUNT_RE = re.compile(r'\b\d+\s+monitors?\b')

    # Markers of Claude's idle mode line, present whether or not a Monitor is
    # active (``... · 1 monitor · ← for agents`` vs ``... (shift+tab to cycle)
    # · ← for agents``).  Their presence means the idle prompt is genuinely
    # rendered - distinguishing a real "no monitor" idle from a blank/partial
    # screen, which is what makes the cleared (False) verdict safe.
    # The arrow glyph in '← for agents' is load-bearing: a bare 'for agents'
    # also occurs in ordinary response prose, and a prose row masquerading as
    # the mode line would return a false "monitor finished" verdict (False),
    # clearing the sticky churn flag and letting the queue dispatch into a
    # still-churning session.
    _IDLE_MODE_MARKERS: tuple = ('← for agents', 'shift+tab to cycle')

    def background_work_state(self, display_lines: list[str]) -> Optional[bool]:
        """Tri-state: is a background ``Monitor`` active at the idle prompt?

        Claude's turn has ended (Stop hook fired, idle box visible) but a
        background ``Monitor`` may still be running and will re-invoke the
        session.  Detected from the footer / mode-line in the tail rows (e.g.
        ``1 monitor still running`` / ``· 1 monitor ·``).  Used only to refine
        the returned state IDLE -> CHURNING; never stored, never gates the hook
        signal.

        Returns ``True`` (marker present), ``False`` (idle mode line rendered
        with NO marker -> Monitor finished), or ``None`` (ambiguous screen ->
        leave the tracker's sticky flag unchanged).  See
        ``CLIProvider.background_work_state`` for why the tri-state matters.
        """
        if not display_lines:
            return None
        # Anchor the tail to the last NON-BLANK row, not the bottom of the
        # buffer.  In a tall terminal with a short conversation, Claude renders
        # the input box + mode line mid-screen and pads many blank rows below,
        # so a naive ``display_lines[-N:]`` grabs only trailing blanks and never
        # sees the footer.
        last = next((i for i in range(len(display_lines) - 1, -1, -1)
                     if display_lines[i].strip()), None)
        if last is None:
            return None
        tail = display_lines[max(0, last - self._IDLE_TAIL_WINDOW + 1):last + 1]
        joined = '\n'.join(tail).lower()
        # The specific "N monitor still running" phrase (activity line) is safe
        # to match anywhere in the tail - response text won't contain it.
        if any(m in joined for m in self._CHURN_MARKERS):
            return True
        # The bare "N monitor" count lives in the persistent mode line.  Find
        # the mode line(s) in the tail by their stable markers and check the
        # count THERE - not on a fixed row (a trailing clipboard/paste hint can
        # displace the mode line from the last non-blank row), and not across
        # the whole tail (response text mentioning monitors would false-positive
        # and wrongly stick the session in CHURNING, holding its queue).
        mode_rows = [ln for ln in tail
                     if any(m in ln.lower() for m in self._IDLE_MODE_MARKERS)]
        if mode_rows:
            if any(self._CHURN_COUNT_RE.search(ln.lower()) for ln in mode_rows):
                return True
            # Mode line rendered with NO monitor count -> the Monitor finished.
            return False
        # No mode line in the tail (blank buffer just after a screen reset, a
        # partial repaint, or mid-turn response text) -> ambiguous; leave the
        # tracker's sticky flag unchanged.
        return None

    # -- Menu / option parsing -------------------------------------------

    @property
    def has_numbered_menus(self) -> bool:
        return True

    @property
    def menu_option_regex(self) -> Optional[re.Pattern[str]]:
        return MENU_OPTION_RE

    @property
    def free_text_option_prefix(self) -> Optional[str]:
        return 'Type something'

    @property
    def below_separator_option_prefix(self) -> Optional[str]:
        return 'Chat about this'

    # -- Input protocol --------------------------------------------------

    @property
    def paste_settle_time(self) -> float:
        return 0.15

    @property
    def single_settle_time(self) -> float:
        return 0.05

    @property
    def image_prefix(self) -> str:
        return '@'

    @property
    def supports_image_attachments(self) -> bool:
        return True

    # -- Input history (CLI ↑/↓ recall) ----------------------------------

    def input_history(self, cwd: str) -> Optional[list[str]]:
        """Read ``~/.claude/history.jsonl`` and return the entries Claude
        would surface on ↑ in the given cwd, ordered oldest → newest.

        Each line is ``{"display", "pastedContents", "timestamp",
        "project", "sessionId"}``.  Claude's own ↑ filters by
        ``project == cwd``.

        ``display`` is the literal text the user saw in the input box
        (``[Pasted text #N +M lines]`` placeholders for pastes).  We
        expand those placeholders inline from ``pastedContents`` so
        that when Leap's ``^^`` later submits the recalled message,
        the real paste content reaches the LLM — without expansion
        the placeholder string would be sent verbatim and the paste
        would be lost.  Image placeholders (``[Image #N]``) stay
        as-is; they refer to clipboard images that Leap can't restore.
        """
        path = Path.home() / '.claude' / 'history.jsonl'
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        out: list[str] = []
        for line in raw.split(b'\n'):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if entry.get('project') != cwd:
                continue
            display = entry.get('display')
            if not isinstance(display, str) or not display:
                continue
            pasted = entry.get('pastedContents')
            if isinstance(pasted, dict) and _PASTED_TEXT_RE.search(display):
                def _resolve(m: re.Match[str]) -> str:
                    info = pasted.get(m.group(1))
                    if isinstance(info, dict):
                        content = info.get('content')
                        if isinstance(content, str):
                            return content
                    return m.group(0)  # leave placeholder if unresolved
                display = _PASTED_TEXT_RE.sub(_resolve, display)
                if not display:
                    continue  # substitution emptied the entry — skip
            out.append(display)
        return out

    # -- Resume support --------------------------------------------------

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def requires_cwd_bound_resume(self) -> bool:
        # Claude stores transcripts under ~/.claude/projects/<cwd-slug>/<uuid>.jsonl;
        # `claude --resume=<uuid>` only finds the session when run from
        # the matching cwd, so leap must offer the cwd-choice picker.
        return True

    def extract_session_id(self, hook_data: dict) -> Optional[str]:
        """Claude Code's session id is the basename of ``transcript_path``
        (``~/.claude/projects/<slug>/<uuid>.jsonl``).  The ``.claude/projects/``
        substring check guards against cross-contamination if a different
        CLI's hook runs with Claude set as the ``LEAP_CLI_PROVIDER``.
        """
        path = hook_data.get('transcript_path', '') or ''
        if not path or '.claude/projects/' not in path:
            return None
        name = os.path.basename(path)
        if name.endswith('.jsonl'):
            name = name[:-6]
        return name or None

    def resume_args(self, session_id: str) -> list[str]:
        # Must be the single-token `=` form — leap-server.py's flag filter
        # drops any argv element that doesn't start with `--`, so the
        # space-separated form would lose the UUID and make claude open
        # its own picker instead of resuming directly.
        return [f'--resume={session_id}']

    def relocate_session(
        self,
        session_id: str,
        src_cwd: str,
        dst_cwd: str,
        *,
        transcript_path: str = '',  # unused — Claude derives path from slug
        on_committed: Optional[Any] = None,
    ) -> Optional[str]:
        return relocate_claude_session(
            session_id, src_cwd, dst_cwd, on_committed=on_committed,
        )

    # -- Last assistant message (Slack) ----------------------------------

    def extract_last_assistant_message(self, hook_data: dict) -> str:
        """Claude doesn't pass the assistant text in the hook payload —
        tail the transcript JSONL and pull the most recent
        ``type=="assistant"`` entry's concatenated text parts.
        Bounded to the last 32 KiB so very long transcripts stay cheap.
        """
        path = hook_data.get('transcript_path', '') or ''
        if not path or '.claude/projects/' not in path:
            return ''
        try:
            size = os.path.getsize(path)
        except OSError:
            return ''
        try:
            chunk = _TRANSCRIPT_TAIL_BYTES
            with open(path, 'rb') as f:
                f.seek(max(0, size - chunk))
                tail = f.read()
        except OSError:
            return ''
        for raw in reversed(tail.split(b'\n')):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if entry.get('type') != 'assistant':
                continue
            parts = [
                c.get('text', '')
                for c in entry.get('message', {}).get('content', [])
                if c.get('type') == 'text'
            ]
            joined = '\n'.join(p for p in parts if p)
            if joined:
                return joined
        return ''

    @property
    def supports_context_usage(self) -> bool:
        return True

    def context_usage(self, cli_name: str, tag: str,
                      storage_dir: Path) -> Optional[ContextUsage]:
        """Context-window usage, preferring the authoritative status-line value.

        Leap's status line (``leap-claude-statusline.py``) writes the resolved
        window - ``context_window_size`` (1M vs 200K, as decided by Claude for
        the plan/env/selection) - to ``<storage>/sockets/<tag>.context`` every
        render.  We prefer that over parsing the transcript, because the
        transcript records no window and Leap can only *guess* 1M from a
        ``[1m]`` suffix that auto-upgraded (Max/Team/Enterprise) sessions never
        produce.  Falls back to the transcript heuristic when the file is absent
        (status line not installed / not reconfigured, an older Claude build
        that omits ``context_window``, or before the first render).  A recorded
        window smaller than the recorded usage is impossible (the API rejects
        over-window prompts) and is healed to 1M by
        :func:`claude_statusline_context_usage`.
        """
        state = storage_dir / 'sockets' / f'{tag}.context'
        usage = claude_statusline_context_usage(str(state))
        if usage is not None:
            return usage
        transcript = latest_transcript_for(storage_dir, cli_name, tag)
        return claude_context_usage(transcript) if transcript else None

    @property
    def supports_cost(self) -> bool:
        return True

    def session_cost(self, cli_name: str, tag: str,
                     storage_dir: Path) -> Optional[CostInfo]:
        """Cumulative token + USD estimate from the whole transcript.

        Uses the non-blocking cached wrapper: the monitor calls this on the Qt
        GUI thread, and the transcript walk runs in a background pool so a large
        first parse never stalls the table build.
        """
        transcript = latest_transcript_for(storage_dir, cli_name, tag)
        return claude_session_cost_cached(transcript) if transcript else None

    def extract_last_user_prompt(
        self,
        cwd: str,
        tag: str,
        storage_dir: Optional[Path],
        cli_name: str = '',
    ) -> str:
        """Walk the transcript JSONL for the most recent user-typed prompt.

        Anthropic API ``role=user`` covers many on-disk shapes — the
        ones we filter (and why):

        * Plain string content — user keystrokes, including unexpanded
          ``@file:lines`` from the VS Code / JetBrains Cmd+Option+K
          shortcut.  KEPT — this is the target case.
        * Plain string content starting with ``<command-name`` /
          ``<local-command-caveat`` / ``<local-command-stdout`` —
          synthetic blocks Claude writes for slash commands and local
          command output.  REJECTED so "Last Msg" doesn't read
          ``<command-name>/clear</command-name>``.
        * Any entry with ``isMeta: true`` — Claude's clean flag for
          synthetic entries (e.g. ``[Image: source: …]`` breadcrumbs
          attached to a pasted image).  REJECTED.
        * Content-block list with a ``tool_result`` block AND no
          ``text`` blocks — Read/Bash output recorded as ``role=user``
          from the model's perspective.  REJECTED.  When a list has
          BOTH a ``text`` block and a ``tool_result`` (canonical
          tool-feedback pattern), the text is KEPT.
        * Content-block list whose only text blocks are
          ``[Request interrupted by user]`` — synthetic interrupt
          marker.  REJECTED.

        Reads backward in 64 KiB chunks, capped at 4 MiB — far enough
        to clear heavy tool-call bursts (verified: a single session had
        the user's last prompt sitting 260 KiB before EOF after a flurry
        of file-Read tool calls) without paging in an arbitrarily large
        transcript.  Results are cached by ``(path, mtime_ns, size)`` so
        the typical poll cycle is a single ``stat`` syscall.

        Returns ``''`` when no transcript exists, the tail contains no
        qualifying user entry, or any I/O / parse step fails — the
        caller falls back to Leap's PTY-side ``recently_sent`` tracking.
        """
        # We deliberately only consult the absolute ``transcript_path``
        # recorded in ``cli_sessions/claude/<tag>.json`` (written by the
        # hook on every Stop/Notification).  A slug-based fallback
        # using the caller's cwd is tempting but unsafe: when two Leap
        # sessions share a cwd (or a fresh session has no record yet
        # while a recently-touched older session sits in the same
        # ``~/.claude/projects/`` subdir), the most-recently-modified
        # ``*.jsonl`` may belong to a *different* tag — we'd then
        # display another row's prompt.  Before the first hook fires,
        # ``recently_sent`` is the right source anyway; degraded for
        # a few seconds beats wrong.
        transcript = self._latest_recorded_transcript(
            tag, storage_dir, cli_name or 'claude')
        if transcript is None:
            return ''
        try:
            st = transcript.stat()
        except OSError:
            return ''
        cache_key = (str(transcript), st.st_mtime_ns, st.st_size)
        cached = _USER_PROMPT_CACHE.get(cache_key)
        if cached is not None:
            return cached
        try:
            result = self._extract_user_prompt_uncached(
                transcript, st.st_size,
            )
        except OSError:
            return ''
        # Cache success and failure alike — if the transcript's
        # last 4 MiB has no qualifying prompt right now, that won't
        # change until the file is appended to (which bumps mtime/size
        # and invalidates the key).
        _USER_PROMPT_CACHE[cache_key] = result
        return result

    @classmethod
    def _extract_user_prompt_uncached(
        cls,
        transcript: Path,
        size: int,
    ) -> str:
        """Reverse-walk the transcript's tail (up to 4 MiB) for a real
        user prompt.

        Returns the first qualifying prompt found scanning end-to-start,
        or ``''`` when the safety cap is hit without a match (extremely
        long active session where the user hasn't sent anything in a
        very long time).
        """
        for raw in cls._iter_lines_reverse(
            transcript, size,
            chunk=_USER_PROMPT_REVERSE_CHUNK,
            max_bytes=_USER_PROMPT_REVERSE_MAX,
        ):
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if entry.get('type') != 'user':
                continue
            # ``isMeta`` is Claude's own flag for synthetic user
            # entries (image-source breadcrumbs, etc.) — always skip.
            if entry.get('isMeta'):
                continue
            content = entry.get('message', {}).get('content')
            text = cls._user_prompt_text(content)
            if text:
                return text
        return ''

    @staticmethod
    def _iter_lines_reverse(
        path: Path,
        size: int,
        *,
        chunk: int,
        max_bytes: int,
    ) -> Iterator[bytes]:
        """Yield non-empty stripped lines from a file, end → start.

        Reads in fixed chunks moving backward.  Each chunk is prepended
        to a carry-over buffer whose head is treated as a partial line
        (because it may be split mid-line at the chunk boundary).  At
        EOF — when we either hit the start of the file or reach
        ``max_bytes`` — the final buffer is yielded as the last line.

        Yields ``bytes`` per call; callers do their own JSON parsing.
        """
        end = size
        read_total = 0
        carry = b''
        with open(path, 'rb') as f:
            while end > 0 and read_total < max_bytes:
                sz = min(chunk, end, max_bytes - read_total)
                end -= sz
                f.seek(end)
                data = f.read(sz)
                read_total += sz
                buf = data + carry
                # All but the first segment are complete lines from
                # within ``buf``.  The first segment may have been
                # split mid-line at the chunk boundary, so we carry
                # it into the next iteration (or out as the final
                # line when we reach the file start).
                lines = buf.split(b'\n')
                carry = lines[0]
                for ln in reversed(lines[1:]):
                    ln = ln.strip()
                    if ln:
                        yield ln
        # Only yield the carry when we read all the way to the start
        # of the file — otherwise it may be a partial line truncated
        # by the ``max_bytes`` cap, which we don't want to feed to
        # JSON parsing as if it were complete.
        if end == 0:
            tail = carry.strip()
            if tail:
                yield tail

    # Plain-string user content starting with any of these prefixes is
    # synthetic — slash-command echoes, local-command captures.  We
    # also have ``isMeta`` for newer transcripts, but older ones can
    # carry these strings without the flag, so prefix-match too.
    # ``<command-name`` is NOT here — slash commands like ``/clear`` are
    # real user actions worth surfacing; we strip the wrapper instead
    # of rejecting (see ``_extract_slash_command``).  The others are
    # transcript-only synthetic blocks (caveats, stdout/stderr echoes
    # of the local command's output) that shouldn't show in "Last Msg".
    _SYNTHETIC_USER_PREFIXES: tuple[str, ...] = (
        '<command-message',
        '<command-args',
        '<command-stdout',
        '<local-command-caveat',
        '<local-command-stdout',
        '<local-command-stderr',
    )

    # Slash-command wrapper Claude writes to the transcript:
    #   <command-name>/clear</command-name>
    #   <command-message>clear</command-message>
    #   <command-args>some args</command-args>
    # Requiring ``<command-message>`` to follow (always present in real
    # entries — verified against the live transcript format) filters
    # out user-typed prompts that happen to start with literal
    # ``<command-name>X</command-name>`` text (e.g., a question about
    # Claude's XML format) — without this guard those would be
    # extracted as if they were slash commands.
    _COMMAND_NAME_RE: re.Pattern[str] = re.compile(
        r'<command-name>(.*?)</command-name>'
        r'\s*<command-message>.*?</command-message>'
        r'(?:\s*<command-args>(.*?)</command-args>)?',
        re.DOTALL,
    )

    @classmethod
    def _user_prompt_text(cls, content: Any) -> str:
        """Pull the user-typed text out of a single ``role=user`` content.

        Returns ``''`` for entries that aren't actual user prompts —
        synthetic command echoes, tool_result-only payloads, interrupt
        markers, image-only content.  When a content-block list mixes
        ``text`` and ``tool_result`` (canonical tool-feedback pattern),
        the text is kept; the tool_result alone never blocks a sibling
        text block from surfacing.
        """
        if isinstance(content, str):
            text = content.strip()
            if not text or text == '[Request interrupted by user]':
                return ''
            # Slash commands: strip the wrapper, surface the command.
            # Falls through to '' if the wrapper is malformed (no
            # close tag, missing command-message block) or has an
            # empty command name — the walker then tries the
            # next-older entry rather than displaying the raw
            # ``<command-name>...`` tags or a leading-space-only
            # string ("`` args``").
            if text.startswith('<command-name'):
                m = cls._COMMAND_NAME_RE.match(text)
                if m:
                    cmd = m.group(1).strip()
                    if cmd:
                        args = (m.group(2) or '').strip()
                        return f'{cmd} {args}' if args else cmd
                return ''
            if text.startswith(cls._SYNTHETIC_USER_PREFIXES):
                return ''
            return text
        if not isinstance(content, list):
            return ''
        parts: list[str] = []
        has_tool_result = False
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'tool_result':
                has_tool_result = True
                continue
            if btype == 'text':
                t = block.get('text', '')
                if not isinstance(t, str):
                    continue
                if t == '[Request interrupted by user]':
                    continue
                if t.startswith('<command-name'):
                    # Mirror the string-branch behavior so a slash
                    # command embedded in a list-shaped content (rare
                    # but possible if Claude's format evolves) gets
                    # extracted, not surfaced raw.
                    m = cls._COMMAND_NAME_RE.match(t)
                    if m:
                        cmd = m.group(1).strip()
                        if cmd:
                            args = (m.group(2) or '').strip()
                            extracted = f'{cmd} {args}' if args else cmd
                            parts.append(extracted)
                    continue
                if t.startswith(cls._SYNTHETIC_USER_PREFIXES):
                    continue
                parts.append(t)
        joined = '\n'.join(p for p in parts if p).strip()
        # An entry with ONLY a tool_result (no text) is Read/Bash output
        # masquerading as a user message — let the caller keep walking.
        if not joined and has_tool_result:
            return ''
        return joined

    # -- Transcript-based "still running" check --------------------------

    @property
    def transcript_projects_root(self) -> Path:
        """Root directory for Claude session transcripts.

        Claude stores each session at
        ``<root>/<slug(cwd)>/<session_id>.jsonl``.  Tests override this
        property to redirect to a tmp_path; production reads from
        ``~/.claude/projects/``.
        """
        return _TRANSCRIPT_PROJECTS_ROOT

    def transcript_says_running(
        self,
        since: float,
        cwd: str,
        tag: str = '',
        storage_dir: Optional[Path] = None,
    ) -> bool:
        """True iff the transcript shows an in-flight ``tool_use``
        from the current turn.

        Hybrid file lookup:
          1. ``cli_sessions/claude/<tag>.json`` for the most recent
             recorded ``session_id`` (populated by the Stop hook).
          2. mtime fallback: most recently modified ``*.jsonl`` in
             the cwd's slug directory.
        """
        return self._classify_transcript_tail(
            since, cwd, tag, storage_dir,
        ) == 'running'

    def transcript_says_interrupted(
        self,
        since: float,
        cwd: str,
        tag: str = '',
        storage_dir: Optional[Path] = None,
    ) -> bool:
        """True iff the transcript shows a ``[Request interrupted by
        user]`` user entry written after the most recent assistant
        tool_use of the current turn.

        Same lookup strategy as :meth:`transcript_says_running`.
        """
        return self._classify_transcript_tail(
            since, cwd, tag, storage_dir,
        ) == 'interrupted'

    _INTERRUPT_MARKER = '[Request interrupted by user]'

    @classmethod
    def _is_interrupt_entry(cls, entry: dict) -> bool:
        """True iff *entry* is a ``role=user`` row whose payload is the
        Claude CLI's synthetic interrupt marker.

        Tolerates both shapes observed in the wild:
        * ``message.content`` as a plain string (older transcripts)
        * ``message.content`` as a list of typed blocks (newer)

        Mirrors the content-shape handling in
        :meth:`_user_prompt_text` so a stale-format transcript can't
        slip past the classifier.
        """
        msg = entry.get('message')
        if not isinstance(msg, dict):
            return False
        content = msg.get('content')
        if isinstance(content, str):
            return content.strip() == cls._INTERRUPT_MARKER
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict)
                        and block.get('type') == 'text'
                        and block.get('text') == cls._INTERRUPT_MARKER):
                    return True
        return False

    def _classify_transcript_tail(
        self,
        since: float,
        cwd: str,
        tag: str,
        storage_dir: Optional[Path],
    ) -> str:
        """Classify the transcript's recent activity as one of:

        * ``'running'`` — most recent assistant entry of the current
          turn has ``stop_reason='tool_use'`` and was *not* superseded
          by a ``[Request interrupted by user]`` user entry.
        * ``'interrupted'`` — a ``[Request interrupted by user]`` user
          entry appears in the tail above the most recent in-turn
          assistant entry (i.e. the user cancelled the loop after a
          tool_use call).
        * ``''`` — anything else (no transcript, no in-turn assistant
          entry, assistant ``stop_reason='end_turn'``, parse error,
          etc.).

        Single reverse-walk over the tail; both public predicates derive
        from this so the disk I/O isn't doubled per poll cycle.
        """
        project_dir = self.transcript_projects_root / slugify(cwd)
        if not project_dir.is_dir():
            return ''

        transcript = self._resolve_transcript_path(
            project_dir, tag, storage_dir,
        )
        if transcript is None:
            return ''

        try:
            size = transcript.stat().st_size
        except OSError:
            return ''
        try:
            with open(transcript, 'rb') as f:
                f.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
                tail = f.read()
        except OSError:
            return ''

        # Walk back to the most recent assistant entry; its stop_reason
        # tells us whether the agent loop is still in tool-use mode.
        # On the way, watch for a [Request interrupted by user] user
        # entry — if one appears AFTER the most recent in-turn assistant
        # tool_use, the loop was cancelled even though no tool_result
        # was written.
        saw_user_interrupt = False
        for raw in reversed(tail.split(b'\n')):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue

            entry_type = entry.get('type')
            if entry_type == 'user':
                # Skip past user entries on the way back, but flag the
                # interrupt sentinel.  Note: we only care about the
                # FIRST one we encounter going backwards (which is the
                # most recent one in time) — additional matches from
                # older turns shouldn't override.
                if not saw_user_interrupt and self._is_interrupt_entry(entry):
                    saw_user_interrupt = True
                continue

            if entry_type != 'assistant':
                continue

            ts_str = entry.get('timestamp', '')
            if not ts_str:
                return ''
            try:
                ts = datetime.fromisoformat(
                    ts_str.replace('Z', '+00:00'),
                ).timestamp()
            except (ValueError, TypeError):
                return ''
            # Stale entry from a previous turn — current turn hasn't
            # produced an assistant entry yet, so we can't tell.
            if ts <= since:
                return ''
            if saw_user_interrupt:
                return 'interrupted'
            stop_reason = entry.get('message', {}).get('stop_reason', '')
            return 'running' if stop_reason == 'tool_use' else ''
        return ''

    # ``_latest_recorded_transcript`` is inherited from ``CLIProvider`` —
    # it moved to the base class so transcript-completion providers
    # (Codex) can pin their session's transcript the same way.

    def _resolve_transcript_path(
        self,
        project_dir: Path,
        tag: str,
        storage_dir: Optional[Path],
    ) -> Optional[Path]:
        """Pick the active transcript: recorded ``session_id`` first,
        most-recently-modified ``*.jsonl`` second.

        Returns ``None`` when neither yields a readable file.
        """
        if tag and storage_dir is not None:
            tag_file = (
                storage_dir / 'cli_sessions' / 'claude' / f'{tag}.json'
            )
            sid = self._latest_session_id(tag_file)
            if sid:
                candidate = project_dir / f'{sid}.jsonl'
                if candidate.is_file():
                    return candidate

        # Fallback: most recently modified .jsonl in the slug dir.
        try:
            best: Optional[Path] = None
            best_mtime: float = 0
            for f in project_dir.iterdir():
                if f.suffix != '.jsonl':
                    continue
                try:
                    mt = f.stat().st_mtime
                except OSError:
                    continue
                if mt > best_mtime:
                    best = f
                    best_mtime = mt
            return best
        except OSError:
            return None

    @staticmethod
    def _latest_session_id(tag_file: Path) -> str:
        """Read the most recent recorded session_id from a tag file.

        ``cli_sessions/claude/<tag>.json`` is a list of records ordered
        oldest-first by Leap's hook.  Walk from the end and return the
        first entry's ``session_id``.  Empty string on any failure.
        """
        if not tag_file.is_file():
            return ''
        try:
            data = json.loads(tag_file.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            return ''
        if not isinstance(data, list):
            return ''
        for entry in reversed(data):
            if not isinstance(entry, dict):
                continue
            sid = entry.get('session_id', '')
            if isinstance(sid, str) and sid:
                return sid
        return ''

    # -- Hook configuration ----------------------------------------------

    @property
    def hook_config_dir(self) -> Path:
        return Path.home() / ".claude" / "hooks"

    def configure_hooks(self, hook_script_path: str) -> None:
        """Install hooks into ~/.claude/settings.json."""
        settings_path = Path.home() / ".claude" / "settings.json"
        marker = "leap-hook.sh"

        # Load existing settings
        settings: dict[str, Any] = {}
        if settings_path.exists():
            try:
                with open(settings_path, "r") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    settings = loaded
            except (json.JSONDecodeError, OSError):
                pass

        if "hooks" not in settings:
            settings["hooks"] = {}

        hooks = settings["hooks"]

        def make_entry(state: str, matcher: str = "") -> dict[str, Any]:
            entry: dict[str, Any] = {
                "hooks": [{"type": "command", "command": f"{hook_script_path} {state}"}]
            }
            if matcher:
                entry["matcher"] = matcher
            return entry

        def upsert(hook_list: list[dict[str, Any]], new_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
            legacy_marker = "claudeq-hook.sh"
            cleaned = [
                e for e in hook_list
                if not any(
                    marker in h.get("command", "") or legacy_marker in h.get("command", "")
                    for h in e.get("hooks", [])
                )
            ]
            cleaned.extend(new_entries)
            return cleaned

        # Stop hook
        if "Stop" not in hooks:
            hooks["Stop"] = []
        hooks["Stop"] = upsert(hooks["Stop"], [make_entry("idle")])

        # Notification hooks
        if "Notification" not in hooks:
            hooks["Notification"] = []
        hooks["Notification"] = upsert(hooks["Notification"], [
            make_entry("needs_permission", matcher="permission_prompt"),
            make_entry("needs_input", matcher="elicitation_dialog"),
        ])

        # PermissionRequest hook — canonical auto-approve path.  Fires
        # before Claude shows a permission dialog, including for tool
        # calls originating inside subagents (Task tool).  The hook
        # script consults the session's ``auto_send_mode`` and returns
        # ``{"behavior":"allow"}`` only when ALWAYS is set; PAUSE mode
        # emits ``{}`` so Claude renders the dialog normally.
        #
        # Matcher is a negative-lookahead regex that covers every tool
        # EXCEPT ``AskUserQuestion``.  AskUserQuestion is the one tool
        # whose *purpose* is to elicit a user choice — auto-approving
        # its PermissionRequest tells Claude "skip user interaction"
        # and the tool then returns an empty answer set to the model
        # ("Allowed by PermissionRequest hook" with no selections),
        # which corrupts the very flow the user invoked it for.  We
        # leave its PermissionRequest unanswered so Claude renders the
        # question dialog and the user actually picks.  The runtime
        # mode check is the gate for every OTHER tool.
        #
        # Subagent permissions are caught automatically
        # (PermissionRequest fires for them too), which closes the gap
        # where the older Notification-based auto-approve silently
        # dropped the signal during sustained RUNNING (the ``Stop``
        # hook does not fire for subagents, so the state never
        # transitioned out of RUNNING and the Late Notification guard
        # had no ``_last_running_snapshot`` fallback).
        #
        # Note: only the *permission* dialog is auto-handled.  MCP
        # elicitation forms still surface to the user via the
        # ``Notification(elicitation_dialog)`` path above.
        #
        # Backward-compat: Claude Code versions without
        # ``PermissionRequest`` support silently ignore the entry, and
        # the existing TUI-menu auto-approve in ``_try_auto_approve``
        # continues to handle those cases.
        if "PermissionRequest" not in hooks:
            hooks["PermissionRequest"] = []
        hooks["PermissionRequest"] = upsert(hooks["PermissionRequest"], [
            make_entry("auto_approve", matcher="^(?!AskUserQuestion$).*"),
        ])

        # SessionStart(resume) — fires on `/resume` inside a running Claude
        # and on `claude --resume=<id>` startup.  Without it, a user who
        # loads a past session but exits before sending a message never
        # triggers Stop, so the session id is never recorded and
        # `leap --resume` can't see it.  Matcher "startup" is intentionally
        # omitted so abandoned fresh sessions don't clutter the picker.
        if "SessionStart" not in hooks:
            hooks["SessionStart"] = []
        hooks["SessionStart"] = upsert(hooks["SessionStart"], [
            make_entry("idle", matcher="resume"),
        ])

        # Status line: the only place Claude exposes the resolved context
        # window (1M vs 200K).  Register Leap's capture-only status-line
        # script so the monitor reads the true window instead of guessing
        # from a ``[1m]`` suffix (which auto-upgraded plans never produce).
        # Optional enhancement: best-effort, NOT verified by
        # ``hooks_installed`` (so a missing/failed status line never blocks
        # session startup).  Claude allows only one ``statusLine``, so any the
        # user already had is preserved by chaining to it via
        # ``leap-statusline-chain`` (never chain to our own script).
        statusline = Path(hook_script_path).with_name("leap-claude-statusline.py")
        if statusline.is_file():
            existing = settings.get("statusLine")
            existing_cmd = (existing.get("command")
                            if isinstance(existing, dict) else None)
            if (isinstance(existing_cmd, str) and existing_cmd.strip()
                    and "leap-claude-statusline" not in existing_cmd):
                try:
                    statusline.with_name("leap-statusline-chain").write_text(
                        existing_cmd)
                except OSError:
                    pass
            settings["statusLine"] = {
                "type": "command",
                "command": str(statusline),
            }

        atomic_write_json(settings_path, settings)

    def hooks_installed(self) -> bool:
        """True iff ``~/.claude/hooks/leap-hook.sh`` exists AND
        ``~/.claude/settings.json`` references it from any hook entry.

        Wrapped in a broad try/except so any unexpected shape in the
        settings file (e.g. a ``command`` that's a non-string scalar)
        returns False instead of crashing the session-start gate.
        """
        try:
            hook_script = self.hook_config_dir / "leap-hook.sh"
            if not hook_script.is_file():
                return False
            settings_path = Path.home() / ".claude" / "settings.json"
            with open(settings_path, "r") as f:
                settings = json.load(f)
            hooks = settings.get("hooks") if isinstance(settings, dict) else None
            if not isinstance(hooks, dict):
                return False
            for entries in hooks.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    inner = entry.get("hooks")
                    if not isinstance(inner, list):
                        continue
                    for h in inner:
                        if not isinstance(h, dict):
                            continue
                        cmd = h.get("command")
                        if isinstance(cmd, str) and "leap-hook.sh" in cmd:
                            return True
            return False
        except Exception:
            return False

    def deconfigure_hooks(self) -> None:
        """Remove Leap's hook entries and status line from ~/.claude/settings.json.

        Restores the user's prior status-line command from ``leap-statusline-chain``
        if one was saved at install time, else drops the ``statusLine`` key if it
        was ours.  Then removes the chain file and the status-line script.  All
        best-effort; never raises.
        """
        try:
            chain_file = self.hook_config_dir / "leap-statusline-chain"
            prior_cmd: Optional[str] = None
            if chain_file.is_file():
                try:
                    prior_cmd = chain_file.read_text(encoding="utf-8").strip() or None
                except OSError:
                    pass

            settings_path = Path.home() / ".claude" / "settings.json"
            if settings_path.is_file():
                with open(settings_path) as f:
                    settings = json.load(f)
                changed = False
                hooks = settings.get("hooks") if isinstance(settings, dict) else None
                if isinstance(hooks, dict):
                    _MARKERS = ("leap-hook.sh", "claudeq-hook.sh")
                    for event in list(hooks.keys()):
                        entries = hooks.get(event)
                        if not isinstance(entries, list):
                            continue
                        cleaned = [
                            e for e in entries
                            if not (
                                isinstance(e, dict)
                                and any(
                                    isinstance(h, dict)
                                    and any(m in h.get("command", "") for m in _MARKERS)
                                    for h in e.get("hooks", [])
                                )
                            )
                        ]
                        if len(cleaned) != len(entries):
                            if cleaned:
                                hooks[event] = cleaned
                            else:
                                del hooks[event]
                            changed = True
                    if changed and not hooks:
                        settings.pop("hooks", None)
                # Status line: restore the chained prior one, else drop ours.
                if isinstance(settings, dict):
                    existing = settings.get("statusLine")
                    existing_cmd = (existing.get("command")
                                    if isinstance(existing, dict) else None)
                    if (isinstance(existing_cmd, str)
                            and "leap-claude-statusline" in existing_cmd):
                        if prior_cmd:
                            settings["statusLine"] = {
                                "type": "command", "command": prior_cmd}
                        else:
                            settings.pop("statusLine", None)
                        changed = True
                if changed:
                    atomic_write_json(settings_path, settings)

            for name in ("leap-statusline-chain", "leap-claude-statusline.py"):
                try:
                    (self.hook_config_dir / name).unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass
        super().deconfigure_hooks()

    # -- CLI-specific input behaviors ------------------------------------

    # send_image_message: uses base class fixed-sleep protocol

    def select_option(
        self,
        option_num: int,
        options: dict[int, str],
        pty_send: Any,
        pty_sendline: Any,
    ) -> dict[str, Any]:
        """Select a numbered option in Claude's Ink TUI dialog.

        Handles special cases:
        - 'Type something' options: return error asking for text input
        - 'Chat about this' options: use arrow-key navigation
        - Regular options: atomic single write of digit(s) + CR

        **Why an atomic single write (instead of ``pty_sendline``):**
        ``pty_sendline`` writes the digit, runs an output-settle
        wait (50–200 ms), then writes CR.  That gap is wide enough
        that a leaky permission menu — one that auto-confirms on
        the digit and dismisses immediately — releases focus to the
        composer BEFORE the CR arrives, and the CR then lands in
        the composer and submits whatever text the user had typed-
        but-not-submitted.

        Even a small gap (e.g. ``pty.send(digit); time.sleep(0.02);
        pty.send('\\r')``) is unsafe: each call is a separate
        ``write()``, so the CLI's input-handling loop typically
        processes the digit in one ``read()`` and the CR in the
        next — same outcome.

        Sending digit + CR as a single ``write()`` call places both
        bytes in the kernel's PTY buffer atomically; the CLI's next
        ``read(N)`` returns both bytes in the same chunk.  A well-
        behaved menu drains the trailing CR from the post-confirm
        chunk and discards it, so nothing leaks to the composer.

        Multi-digit options (``option_num >= 10``) are written the
        same way (e.g. ``"10\\r"``).  If a future Claude menu
        auto-confirms on the very first digit, the trailing bytes
        of a multi-digit number would be drained alongside the CR
        and the user-selected option might be wrong — but that's a
        provider-design question; for typical 1–9 option menus the
        single-write form is correct.
        """
        label = options.get(option_num)
        if label is not None:
            if self.free_text_option_prefix and label.startswith(self.free_text_option_prefix):
                return {
                    'status': 'error',
                    'error': 'type your answer as text instead',
                }
            if self.below_separator_option_prefix and label.startswith(self.below_separator_option_prefix):
                # Navigate with individual arrow-down keys
                for _ in range(option_num - 1):
                    pty_send('\x1b[B')
                    time.sleep(0.1)
                time.sleep(0.2)
                pty_send('\r')
                return {'status': 'sent'}

        if option_num not in options:
            return {
                'status': 'error',
                'error': f'option {option_num} not found in prompt',
            }
        pty_send(str(option_num) + '\r')
        return {'status': 'sent'}

    def send_custom_answer(
        self,
        text: str,
        options: dict[int, str],
        pty_send: Any,
    ) -> dict[str, Any]:
        """Select 'Type something' and enter free-form text in Claude's Ink TUI."""
        type_option = None
        for num, label in options.items():
            if self.free_text_option_prefix and label.startswith(self.free_text_option_prefix):
                type_option = str(num)
                break
        if not type_option:
            return {'status': 'error', 'error': 'no "Type something" option found'}

        # Step 1: Send digit to navigate to "Type something."
        pty_send(type_option)
        time.sleep(0.5)
        # Step 2: Type char-by-char for Ink raw-mode compatibility
        for ch in text:
            pty_send(ch)
            time.sleep(0.02)
        time.sleep(0.1)
        # Step 3: Submit
        pty_send('\r')
        return {'status': 'sent'}
