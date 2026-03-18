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


class AutoSendMode(str, Enum):
    """Queue auto-send behavior.

    PAUSE:  Only send when CLI is idle.
    ALWAYS: Send whenever CLI is not running (even during prompts).
    """

    PAUSE = 'pause'
    ALWAYS = 'always'


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
