# Add a New CLI Provider

Guide for adding a new AI CLI backend to Leap (e.g., a new coding assistant).

Leap uses a **Strategy pattern** — each CLI backend implements the `CLIProvider` abstract class. The provider defines identity, state detection, input protocol, menu handling, and hook configuration. The rest of the system (server, client, monitor, state tracker) uses the provider interface generically.

## Key Constants & Enums

These are the global constants you should use throughout the codebase — **never hardcode provider names or state strings**:

```python
# Provider registry (cli_providers/registry.py)
from leap.cli_providers.registry import DEFAULT_PROVIDER, get_provider, list_providers
DEFAULT_PROVIDER  # = 'claude' — used as fallback when provider name is missing

# State enums (cli_providers/states.py) — extend str for JSON transparency
from leap.cli_providers.states import AutoSendMode, CLIState

CLIState.IDLE             # == 'idle'
CLIState.RUNNING          # == 'running'
CLIState.NEEDS_PERMISSION # == 'needs_permission'
CLIState.NEEDS_INPUT      # == 'needs_input'
CLIState.INTERRUPTED      # == 'interrupted'

AutoSendMode.PAUSE        # == 'pause'
AutoSendMode.ALWAYS       # == 'always'

# Pre-built frozen sets for common checks
from leap.cli_providers.states import WAITING_STATES, SIGNAL_STATES, PROMPT_STATES
```

**Rules:**
- Use `CLIState.IDLE` instead of `'idle'` in comparisons and dict keys
- Use `AutoSendMode.PAUSE` instead of `'pause'` in comparisons and defaults
- Use `DEFAULT_PROVIDER` instead of `'claude'` for fallback defaults
- Use `WAITING_STATES` instead of `('needs_permission', 'needs_input', 'interrupted')`
- The socket protocol uses `cli_state` (not `claude_state`) and `cli_running` (not `claude_running`)

## Overview of Touchpoints

Adding a new CLI provider requires changes in these areas:

1. **Provider class** — The core implementation (`cli_providers/`)
2. **Registry** — Register the new provider (`cli_providers/registry.py`)
3. **Package exports** — Update `__init__.py` exports
4. **Hook configuration** — How the CLI reports state changes to Leap
5. **Shell launcher** — Optional per-CLI shortcut script
6. **Makefile** — Hook cleanup on uninstall
7. **ASCII banner** — Automatically handled (uses `display_name`)
8. **Monitor table** — Automatically handled (uses `display_name`)
9. **CLI selector** — Automatically handled (reads from registry)
10. **Shell flags** — Automatically handled (generated from registry)
11. **Documentation** — Update CLAUDE.md and README.md

## Step-by-Step

### 1. Create the Provider Class

Create `src/leap/cli_providers/<name>.py` inheriting from `CLIProvider`.

```python
"""
<Display Name> CLI provider.

Implements the CLIProvider interface for <CLI tool description>.
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider


class <Name>Provider(CLIProvider):
    """Provider for <CLI tool> (<TUI type>, <language>)."""

    # -- Identity --------------------------------------------------------

    @property
    def name(self) -> str:
        return '<name>'  # lowercase, used in config/metadata

    @property
    def command(self) -> str:
        return '<binary>'  # binary name in PATH (e.g. 'mycli')

    @property
    def display_name(self) -> str:
        return '<Display Name>'  # human-readable (e.g. 'My CLI Tool')

    # -- State detection patterns ----------------------------------------

    @property
    def interrupted_pattern(self) -> bytes:
        # Byte string that appears in ANSI-stripped PTY output when interrupted.
        # Run the CLI, press Ctrl+C/Escape, and observe what text appears.
        return b'<pattern>'

    @property
    def dialog_patterns(self) -> list[bytes]:
        # Compact patterns (ANSI-stripped, spaces removed) that indicate
        # a permission/question dialog. ALL must be present for a match.
        # Return [] to disable PTY-based dialog detection (rely on hooks).
        #
        # To find these: run the CLI, trigger a permission dialog, then
        # examine the PTY output after stripping ANSI codes and spaces.
        return [b'<pattern1>', b'<pattern2>']

    # -- Hook configuration ----------------------------------------------

    @property
    def hook_config_dir(self) -> Path:
        # Directory where the hook script will be installed.
        return Path.home() / '.<cli_config_dir>'

    @property
    def requires_binary_for_hooks(self) -> bool:
        # Return True if this CLI is optional (hooks skipped if not installed).
        # Return False if this CLI should always have hooks configured.
        return True

    def configure_hooks(self, hook_script_path: str) -> None:
        """Install hooks into the CLI's configuration file."""
        # See ClaudeProvider or CodexProvider for reference implementations.
        # Key responsibilities:
        # 1. Load the CLI's config file (JSON, TOML, YAML, etc.)
        # 2. Remove any old Leap hook entries (marker: "leap-hook.sh")
        # 3. Add new entries that call hook_script_path with state args
        # 4. Write the config back
        ...
```

#### Required Properties (Abstract)

These MUST be implemented — the class won't instantiate without them:

| Property | Type | Purpose |
|----------|------|---------|
| `name` | `str` | Short ID for config/metadata (e.g. `'claude'`, `'codex'`) |
| `command` | `str` | Binary name to find in PATH |
| `display_name` | `str` | Human-readable name for UI |
| `interrupted_pattern` | `bytes` | Text indicating user interrupted the CLI |
| `dialog_patterns` | `list[bytes]` | Patterns indicating a permission/input dialog |
| `hook_config_dir` | `Path` | Directory for hook script installation |
| `configure_hooks()` | method | Installs hooks into CLI config |

#### Optional Properties (Have Defaults)

Override these only if the CLI differs from the defaults:

| Property | Default | When to Override |
|----------|---------|-----------------|
| `trust_dialog_patterns` | Claude's trust dialog | Different startup dialog, or `[]` if no trust dialog |
| `output_triggers_running` | `True` | Set `False` for full-screen TUIs (Ratatui) where redraws look like output |
| `enter_triggers_running` | `False` | Set `True` for full-screen TUIs where Enter is the submit signal |
| `silence_timeout` | `None` (uses 15s global) | Shorter timeout for TUIs that output constantly during processing |
| `has_numbered_menus` | `True` | Set `False` if the CLI uses y/n prompts instead |
| `menu_option_regex` | `None` | Regex with groups (number, label) for numbered menus |
| `free_text_option_prefix` | `None` | Label prefix for "type your answer" options |
| `below_separator_option_prefix` | `None` | Label prefix for options needing arrow-key nav |
| `paste_settle_time` | `0.15` | Adjust if the CLI needs more/less time after paste |
| `single_settle_time` | `0.05` | Adjust for single-line input settle |
| `image_prefix` | `'@'` | Change if CLI uses different image attachment syntax |
| `supports_image_attachments` | `False` | Set `True` if CLI supports inline image files |
| `requires_binary_for_hooks` | `False` | Set `True` if hooks should only configure when CLI is installed |
| `valid_signal_states` | `SIGNAL_STATES` | Override if the CLI writes different states to signal files |

#### Optional Methods (Have Defaults)

| Method | Default Behavior | When to Override |
|--------|-----------------|-----------------|
| `send_message()` | Write text + settle + CR | Custom input protocol (e.g. char-by-char for raw mode) |
| `send_image_message()` | Same as `send_message()` | CLI has special image confirmation flow |
| `is_image_message()` | Check `supports_image_attachments` + prefix | Different image detection logic |
| `select_option()` | Returns error | Implement for numbered menus, y/n prompts, etc. |
| `send_custom_answer()` | Returns error | Implement for free-text input in dialogs |
| `find_cli()` | Searches PATH for `self.command` | Custom binary location logic |
| `get_spawn_env()` | Sets `LEAP_TAG`, `LEAP_SIGNAL_DIR` | Additional env vars needed by the CLI |
| `parse_signal_file()` | Parses JSON `{"state": "..."}` | Different signal file format |

### 2. Register the Provider

Edit `src/leap/cli_providers/registry.py`:

```python
from leap.cli_providers.<name> import <Name>Provider

_PROVIDERS: dict[str, CLIProvider] = {
    'claude': ClaudeProvider(),
    'codex': CodexProvider(),
    '<name>': <Name>Provider(),  # <-- Add here
}
```

### 3. Update Package Exports

Edit `src/leap/cli_providers/__init__.py`:

```python
from leap.cli_providers.<name> import <Name>Provider

__all__ = [
    ...
    '<Name>Provider',
    ...
]
```

### 4. Hook Configuration

The `configure_hooks()` method on your provider class IS the hook configuration. The unified `src/scripts/configure_hooks.py` script automatically discovers all registered providers and calls their `configure_hooks()` method during `make install` and `make update`.

**What your `configure_hooks()` must do:**

1. Load the CLI's config file
2. Remove old Leap entries (search for `"leap-hook.sh"` marker)
3. Add entries that call the hook script with state arguments:
   - **Stop hook**: `<hook_path> idle` — Called when CLI finishes processing
   - **Notification hooks** (if supported): `<hook_path> needs_permission`, `<hook_path> needs_input`
4. Write the config back

**The hook script** (`leap-hook.sh`) is shared across all CLIs. It:
- Reads `LEAP_TAG` and `LEAP_SIGNAL_DIR` env vars (set by `get_spawn_env()`)
- Writes `{"state": "<state>"}` to `.storage/sockets/<tag>.signal`
- For idle state, also extracts the last assistant message from the transcript

If your CLI doesn't support hooks at all, you can implement a no-op `configure_hooks()`, but state detection will rely entirely on PTY output patterns and silence timeout, which is less reliable.

### 5. Optional: Shell Launcher Script

Create `src/scripts/<name>-leap-main.sh` for a direct shortcut:

```bash
#!/bin/bash
# <Display Name> launcher — delegates to leap-main.sh with CLI preset
export LEAP_CLI="<name>"
exec "$(dirname "${BASH_SOURCE[0]}")/leap-main.sh" "$@"
```

Make it executable in the Makefile `configure-shell` target:

```makefile
@chmod +x $(SCRIPTS_DIR)/<name>-leap-main.sh
```

### 6. Makefile: Hook Cleanup on Uninstall

Add hook file cleanup to the `uninstall` target in `Makefile`:

```makefile
@rm -f "$$HOME/.<cli_config_dir>/leap-hook.sh" 2>/dev/null || true
```

This is the ONE place that can't be fully dynamic (uninstall must know exact paths even if the provider code is gone).

### 7-10. Automatic — No Changes Needed

These are handled automatically by the abstractions:

- **ASCII banner**: `print_banner()` uses `provider.display_name`
- **Monitor table**: `table_builder_mixin.py` reads `provider.display_name`
- **CLI selector**: `leap-select-cli.py` reads from `list_providers()` + `get_provider().display_name`
- **Shell flags**: `configure-shell-helper.sh` generates `LEAP_<NAME>_FLAGS` from `list_providers()`

### 11. Documentation & String References

Many files contain hardcoded provider names in docstrings, comments, error messages, and user-facing text. When adding a new provider, **grep the entire codebase** for existing provider names (e.g. `claude`, `codex`, `Claude Code`, `OpenAI Codex`) and update every list that enumerates providers. Common locations:

**CLAUDE.md** — Update:
- Description (line 3): Add new CLI to the list
- Project Structure: Add the new provider file under `cli_providers/`
- Key Classes table: Add the new provider class
- `get_provider()` row: Add the new provider name
- IDE Setup section (if the new CLI has IDE-specific config)

**README.md** — Update:
- Description line: Add the new CLI name
- Prerequisites: Add link to the new CLI's docs
- Features: Ensure text is generic ("the CLI") not provider-specific
- Links footer: Add link to the new CLI's docs

**Source files with hardcoded provider lists** (grep for `'claude', 'codex'` and `Claude.*Codex`):
- `cli_providers/__init__.py` — Module docstring
- `cli_providers/base.py` — Docstring examples for `name`, `command`, `display_name`, `hook_config_dir`
- `cli_providers/registry.py` — `get_provider()` docstring
- `server/server.py` — Usage messages, `LeapServer.__init__` docstring, `parse_options()` docstring
- `server/metadata.py` — `SessionMetadata.__init__` docstring
- `server/state_tracker.py` — Module docstring
- `server/pty_handler.py` — Module docstring, `__init__` docstring
- `utils/terminal.py` — `print_banner()` docstring example
- `scripts/leap-hook.sh` — Header comments (provider list + stdin format)
- `scripts/leap-main.sh` — Comment listing launcher scripts
- `scripts/leap-select-cli.py` — Error message for no CLIs found
- `scripts/leap-select.sh` — Comment about per-CLI env var flags

**Slack integration** (grep for `Claude` in `src/leap/slack/`):
- `slack/bot.py` — Module docstring, class docstring, comments
- `slack/output_watcher.py` — Module docstring, `_PROVIDER_DISPLAY_NAMES` dict, method docstrings
- `slack/output_capture.py` — Module docstring, method docstrings
- `scripts/setup-slack-app.sh` — Slack app description string

**Other**:
- `__init__.py` (root package) — Module docstring
- `pyproject.toml` — Project description
- `monitor/leap_sender.py` — Docstrings referencing "Claude"

## Understanding State Detection

State detection is the most complex part. There are three mechanisms:

### A. Hook-Based (Primary, Most Reliable)

The CLI calls hook scripts on lifecycle events. The hook writes state to a signal file. The state tracker reads this file.

- **Stop hook** → writes `idle` (CLI finished processing)
- **Notification hooks** → writes `needs_permission` or `needs_input`
- State tracker reads `.storage/sockets/<tag>.signal` each poll cycle

### B. PTY Output Pattern Matching (Secondary)

The state tracker watches raw PTY output for patterns:

- **`trust_dialog_patterns`**: Startup dialog detection (before user input) → `needs_permission`
- **`dialog_patterns`**: Startup dialog detection (before user input, fallback) — checked for ALL patterns present → `needs_permission`
- **`interrupted_pattern`**: ANSI-stripped output after user input → `interrupted`

Note: During running state, permission detection relies solely on Notification hooks (signal file). PTY `dialog_patterns` are only checked at startup.

For full-screen TUIs (Ratatui), PTY output is unreliable because screen redraws produce constant output. Set `output_triggers_running = False` and rely on hooks.

### C. Silence Timeout (Fallback)

If no output for `silence_timeout` seconds while in `running` state → transition to `idle`. This catches cases where hooks don't fire.

### State Machine Summary

```
                  ┌─── hook: idle ──────────────┐
                  │                              │
                  ▼                              │
    ┌──────────┐     send()     ┌───────────┐   │
    │   IDLE   │ ──────────────▶│  RUNNING   │───┘
    │          │◀───────────────│            │
    └──────────┘  silence/hook  └───────────┘
         │                          │    │
         │  Escape                  │    │ hook: needs_*
         │  (race)                  │    │ or PTY pattern
         ▼                          │    ▼
    ┌──────────────┐                │  ┌───────────────────┐
    │ INTERRUPTED  │◀───────────────┘  │ NEEDS_PERMISSION  │
    │              │   PTY pattern     │ NEEDS_INPUT       │
    └──────────────┘                   └───────────────────┘
```

## Testing Your Provider

### 1. Verify Registration

```bash
poetry run python -c "
from leap.cli_providers.registry import get_provider, list_providers
print(list_providers())
p = get_provider('<name>')
print(f'{p.name}: {p.display_name}, cmd={p.command}')
print(f'hook_dir={p.hook_config_dir}, requires_binary={p.requires_binary_for_hooks}')
"
```

### 2. Verify Hook Configuration

```bash
PYTHONPATH=src:$PYTHONPATH poetry run python src/scripts/configure_hooks.py <name> src/scripts/leap-hook.sh
```

### 3. Run Existing Tests

```bash
poetry run pytest tests/ -v
```

Existing tests should still pass. Consider adding provider-specific tests to `tests/test_state_tracker.py` following the Codex test patterns.

### 4. Manual Testing

1. Start a server: `leap test-<name> --cli <name>`
2. Verify the ASCII banner shows the correct CLI name
3. Open the monitor — verify the CLI column shows the display name
4. Trigger state transitions and verify detection works:
   - Send a message → state should go to `running`
   - Wait for completion → state should go to `idle`
   - Trigger a permission dialog → state should go to `needs_permission`
   - Press Escape → state should go to `interrupted`

### 5. Write State Tracker Tests

Add a test class in `tests/test_state_tracker.py`:

```python
class TestMyCliProvider:
    """Tests for MyCLI-specific state detection."""

    def test_my_cli_specific_behavior(self, tmp_path):
        from leap.cli_providers.<name> import <Name>Provider
        provider = <Name>Provider()
        t = [0.0]
        tracker = CLIStateTracker(
            signal_file=tmp_path / "test.signal",
            clock=lambda: t[0],
            provider=provider,
        )
        # Test your provider's specific behaviors...
```

## Checklist

### Core implementation
- [ ] Provider class created in `src/leap/cli_providers/<name>.py`
- [ ] All abstract properties and methods implemented
- [ ] Provider registered in `registry.py`
- [ ] Provider exported in `__init__.py`
- [ ] `configure_hooks()` installs hooks correctly
- [ ] `hook_config_dir` points to correct location
- [ ] `requires_binary_for_hooks` set correctly

### Shell & Makefile
- [ ] Shell launcher script created (`src/scripts/<name>-leap-main.sh`)
- [ ] Makefile: `chmod +x` for launcher script in `configure-shell` target
- [ ] Makefile: hook cleanup added to `uninstall` target

### String references (grep for existing provider names!)
- [ ] `cli_providers/__init__.py` — module docstring
- [ ] `cli_providers/base.py` — docstring examples (name, command, display_name, hook_config_dir)
- [ ] `cli_providers/registry.py` — `get_provider()` docstring
- [ ] `server/server.py` — usage messages, docstrings
- [ ] `server/metadata.py` — docstring
- [ ] `server/state_tracker.py` — module docstring
- [ ] `server/pty_handler.py` — module docstring, `__init__` docstring
- [ ] `utils/terminal.py` — `print_banner()` docstring
- [ ] `scripts/leap-hook.sh` — header comments
- [ ] `scripts/leap-main.sh` — comment listing launcher scripts
- [ ] `scripts/leap-select-cli.py` — error message
- [ ] `scripts/leap-select.sh` — env var flags comment
- [ ] `slack/bot.py` — docstrings and comments
- [ ] `slack/output_watcher.py` — `_PROVIDER_DISPLAY_NAMES` dict, docstrings
- [ ] `slack/output_capture.py` — docstrings
- [ ] `scripts/setup-slack-app.sh` — app description
- [ ] `src/leap/__init__.py` — package docstring
- [ ] `pyproject.toml` — project description
- [ ] `monitor/leap_sender.py` — docstrings

### Documentation
- [ ] CLAUDE.md updated (description, Project Structure, Key Classes table)
- [ ] README.md updated (description, prerequisites, links footer)

### Testing & verification
- [ ] Existing tests pass (`poetry run pytest tests/ -v`)
- [ ] Provider-specific tests added
- [ ] Manual testing: server startup, state transitions, monitor display
- [ ] Self-verification: `grep -rn` for old provider names to catch stragglers
