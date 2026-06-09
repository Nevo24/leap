"""CLI state and mode enumerations for Leap.

Defines the canonical set of CLI states, auto-send modes, and commonly
used groupings.  Using ``str, Enum`` so members compare equal to their
string values (e.g. ``CLIState.IDLE == 'idle'``) and serialize to JSON
transparently.
"""

from enum import Enum
from typing import FrozenSet


class CLIState(str, Enum):
    """Possible states of a CLI session."""

    IDLE = 'idle'
    RUNNING = 'running'
    NEEDS_PERMISSION = 'needs_permission'
    NEEDS_INPUT = 'needs_input'
    INTERRUPTED = 'interrupted'
    # Turn ended (idle prompt shown, ready for input) but a background task -
    # Claude Code's `Monitor` - is still active and will re-invoke the session.
    # Idle-derived and Claude-only: surfaced distinctly so a "watching CI"
    # session is not shown identical to a "done, awaiting you" one.  Computed at
    # the get_state boundary from a screen marker; never stored in _state and
    # never written to / read from the hook signal file (not in SIGNAL_STATES).
    CHURNING = 'churning'


class AutoSendMode(str, Enum):
    """Queue auto-send behavior.

    PAUSE:  Only send queued messages when CLI is idle.
    ALWAYS: Send queued messages when idle and auto-approve permission
            prompts (select "Yes" if available).  Questions wait.
    """

    PAUSE = 'pause'
    ALWAYS = 'always'


class ChurnQueueMode(str, Enum):
    """Auto-send behavior while a session is CHURNING (idle, but a background
    monitor is still active and will re-invoke it).

    SEND: dispatch the next queued message while churning - the session is
          idle/ready, so sending is safe and simply starts a new turn.
    WAIT: hold queued messages until the monitor finishes and the session
          fully idles.  The default (a queued follow-up usually means "after
          the background work", not "alongside it"); ``!force`` overrides.
    """

    SEND = 'send'
    WAIT = 'wait'


# CLI is waiting for user action (not producing output).
WAITING_STATES: FrozenSet[CLIState] = frozenset({
    CLIState.NEEDS_PERMISSION,
    CLIState.NEEDS_INPUT,
    CLIState.INTERRUPTED,
})

# States that can appear in the signal file (written by hooks or by
# the state tracker itself for states the hook cannot express).
SIGNAL_STATES: FrozenSet[CLIState] = frozenset({
    CLIState.IDLE,
    CLIState.NEEDS_PERMISSION,
    CLIState.NEEDS_INPUT,
    CLIState.INTERRUPTED,
})

# Showing a permission or input prompt dialog.
PROMPT_STATES: FrozenSet[CLIState] = frozenset({
    CLIState.NEEDS_PERMISSION,
    CLIState.NEEDS_INPUT,
})

# Backward-compatible alias: old hooks may still write 'has_question'.
SIGNAL_ALIASES: dict[str, str] = {'has_question': 'needs_input'}
