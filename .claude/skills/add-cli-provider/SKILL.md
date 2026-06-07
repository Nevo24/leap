---
name: add-cli-provider
description: Step-by-step guide for adding a new AI CLI backend (provider) to Leap, such as a new coding assistant. Covers the CLIProvider Strategy pattern, state detection, input protocol, menu handling, configure_hooks and hooks_installed, and registry wiring, including custom CLI variants of the five base CLIs. Use when adding, implementing, or registering a new CLI provider.
---

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
        # 4. Write the config back ATOMICALLY (use leap.utils.atomic_write)

    def hooks_installed(self) -> bool:
        """True iff Leap's hooks are wired up for this CLI."""
        # Mirror image of configure_hooks(). Both halves must be true:
        # 1. self.hook_config_dir / "leap-hook.sh" exists on disk
        # 2. The CLI's settings file references "leap-hook.sh" from any
        #    hook entry. Wrap parse in try/except — corrupt or missing
        #    files return False (do NOT raise).
        # See ClaudeProvider or CodexProvider for reference impls.
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
| `configure_hooks()` | method | Installs hooks into CLI config (use atomic writes) |
| `hooks_installed()` | method | Returns True iff Leap's hooks are currently wired up — used by the session-start gate to refuse to spawn the server when integration is missing (e.g. CLI installed after Leap). Mirror image of `configure_hooks()` |

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
| `base_type` | `self.name` | For **built-in** providers, leave the default — it returns the provider's own `name`. **Custom** providers (`CustomCLIProvider`) inherit the value from their wrapped base automatically via `__getattribute__` delegation; you don't write `base_type` yourself. The session-start gate uses `get_provider(provider.base_type).hooks_installed()` so custom CLIs share their base's hook setup. **All custom CLIs must wrap one of the five base CLIs** — there is no path for a custom CLI that's not a variant of a built-in. |
| `valid_signal_states` | `SIGNAL_STATES` | Override if the CLI writes different states to signal files |
| `supports_resume` | `False` | Set `True` when you wire up the **Leap Resume** feature (see below) |
| `requires_cwd_bound_resume` | `False` | Set `True` if resuming this CLI requires running from the recorded cwd (see **Cross-cwd resume — the "move" mechanism** below). Drives the picker's *Original / Current* prompt. |
| `supports_context_usage` | `False` | Set `True` when you implement `context_usage()`. `False` renders the monitor's Context cell as `N/A` (CLI can't report usage, e.g. Cursor); `True` shows the % or blank. See **Context-usage column** below. |

#### Optional Methods (Have Defaults)

| Method | Default Behavior | When to Override |
|--------|-----------------|-----------------|
| `send_message()` | Write text + settle + CR | Custom input protocol (e.g. char-by-char for raw mode) |
| `send_image_message()` | Same as `send_message()` | CLI has special image confirmation flow |
| `is_image_message()` | Check `supports_image_attachments` + prefix | Different image detection logic |
| `select_option()` | Returns error | Implement for numbered menus, y/n prompts, etc. |
| `send_custom_answer()` | Returns error | Implement for free-text input in dialogs |
| `find_cli()` | Searches PATH for `self.command` | Custom binary location logic |
| `get_spawn_env()` | Sets `LEAP_TAG`, `LEAP_SIGNAL_DIR`, `LEAP_PYTHON`, `LEAP_CLI_PROVIDER` | Additional env vars needed by the CLI |
| `parse_signal_file()` | Parses JSON `{"state": "..."}` | Different signal file format |
| `extract_session_id()` | Returns `None` (no resume) | Implement for **Leap Resume** — pull the session id out of the hook payload |
| `resume_args()` | Returns `[]` | Implement for **Leap Resume** — return the argv tokens that resume the given session id |
| `relocate_session()` | Returns `None` (no cross-cwd) | Implement for the **move mechanism** — physically (or logically) bring the session's on-disk state under the user's chosen cwd. Required when `requires_cwd_bound_resume = True`. |
| `session_exists()` | Returns `True` | Override if your CLI records sessions with empty `transcript_path` so the picker's path-based stale-check can't filter them — return `False` when the session's on-disk state has been deleted out-of-band. |
| `context_usage(cli_name, tag, storage_dir)` | Returns `None` | Implement (with `supports_context_usage = True`) to show the monitor's **Context** column — locate your own source (a transcript via `latest_transcript_for`, or Copilot's status-line state file) and return prompt-tokens vs the context window. See **Context-usage column** below. |
| `deconfigure_hooks()` | Removes `leap-hook.sh` + `leap-hook-process.py` from `hook_config_dir` | Override if `configure_hooks()` also writes into a CLI settings/config file — undo those changes surgically, write back atomically, then call `super().deconfigure_hooks()`. See **`deconfigure_hooks()`** section in step 4. |

### Leap Resume feature (`leap --resume`)

If this CLI supports resuming a previous conversation, implement the three
resume hooks so the tag shows up in the `leap --resume` picker (prefixed
with a `[<display_name>]` badge). All three must be set together:

1. **`supports_resume`** → `True`
2. **`extract_session_id(hook_data: dict) -> Optional[str]`**
   Given the JSON the CLI sends to `leap-hook.sh` on Stop / Notification
   events, return the stable session identifier (UUID / chat id / whatever
   your CLI uses). Return `None` when the payload isn't one of this CLI's
   sessions — the session recorder will then skip it.

   Examples: Claude derives it from `transcript_path` basename; Codex
   reads the `session_id` field directly (and falls back to the first
   JSONL line's `payload.id`).

3. **`resume_args(session_id: str) -> list[str]`**
   Return the argv tokens that, when prepended to the CLI invocation,
   resume the session. The server **prepends** these so positional
   subcommand forms stay in the right spot. Examples:

   ```python
   # Claude: flag-value form, `=` is required so the single token
   # survives leap-server.py's argv pipeline intact
   return [f'--resume={session_id}']

   # Codex: positional subcommand
   return ['resume', session_id]

   # Cursor Agent (hypothetical): bare flag-value
   return ['--resume', session_id]
   ```

**Data flow** — no extra code is needed beyond these three methods:

- The hook (`leap-hook-process.py`) reads `LEAP_CLI_PROVIDER` (set by
  `get_spawn_env`) and calls your provider's `extract_session_id` with the
  raw hook payload. Matching sessions land in
  `.storage/cli_sessions/<name>/<tag>.json`.
- The picker scans `.storage/cli_sessions/*/` and shows each tag as
  `[<display_name>] <tag>`. Custom CLIs appear automatically as long as
  they're registered.
- On selection, `leap-resume.py` sets `LEAP_RESUME_SESSION_ID`,
  `LEAP_RESUME_CLI` and `LEAP_CLI`, execs `leap-main.sh`, and
  `leap-server.py` consults your provider's `resume_args` before the
  PTY spawn.

If the session is tied to a specific working directory (Claude stores
transcripts under a cwd-derived slug), record `cwd` in the hook payload
— the picker `chdir`s there before launch so resume can find the
transcript.

**Gotchas observed in the wild:**

- Some CLIs (Codex 0.121+) require a non-obvious schema — e.g. events
  nested under a top-level `"hooks"` key in `hooks.json`.  If
  implementing `configure_hooks`, verify the resulting JSON actually
  triggers the hook by checking for an entry in
  `.storage/logs/hook-debug.log` (create the `logs/` dir to enable).
- Some CLIs (Cursor Agent) gate hooks behind a server-side feature
  flag — on plans where the flag isn't enabled, the hook silently
  never fires regardless of schema validity.  That's outside our
  control; implement the protocol anyway so users with the flag get
  the feature.
- Some CLIs (Codex) strip env vars when spawning hook subprocesses.
  `leap-hook.sh` already walks the PPID chain looking for a
  `<project>/.storage/pid_maps/<pid>.json` mapping — that mapping is
  written with `cli_provider` so the fallback still identifies the CLI.
  The project path itself is recovered from `$LEAP_PROJECT_DIR` or,
  if that's also been stripped, by regex-reading the install-time
  `export LEAP_PROJECT_DIR="…"` line out of `~/.zshrc` / `~/.bashrc`.
  You get this for free by using `get_spawn_env` (base class) without
  overriding it.

### Cross-cwd resume — the "move" mechanism

When the user picks a session in `leap --resume` (or via the GUI's
"From Resume" / "Open IDE + Move session" flows) from a *different*
cwd than the one the session was originally recorded in, leap shows
an arrow-key prompt:

```
  Where do you want to resume?
  ❯ CD into the original directory:  /Users/me/work/proj
    Stay in the current directory:   /Users/me
```

**Both options must work for every CLI we ship.** This requires every
new resume-capable provider to implement the *move mechanism*:

1. **`requires_cwd_bound_resume`** → `True`
   This flips on the prompt above.  When `False`, leap silently uses
   the current cwd (no prompt) — only correct for CLIs whose resume
   command finds sessions by id alone, regardless of cwd.

2. **`relocate_session(session_id, src_cwd, dst_cwd, *, transcript_path='', on_committed=None) -> Optional[str]`**
   Called when the user picks **"Stay in the current directory"**.
   Must bring the session's on-disk state under `dst_cwd` so
   `<cli> resume <id>` finds it from there.  Two flavors:

   - **File-move (real)** — for CLIs that store sessions in a
     cwd-derived path: physically move the transcript / chat dir
     across cwds.  Use the shared primitives in
     `src/leap/utils/relocation.py` (`signals_blocked`,
     `stage_copy_file/_tree`, `commit_file/_tree`,
     `verify_files_match`, `must_remove_tree`, `make_tmp_path`).
     Wrap your orchestrator function in its own
     `<name>_session_move.py` next to the existing
     `claude_session_move.py` / `gemini_session_move.py` /
     `cursor_session_move.py`.

   - **Logical no-op** — for CLIs that key sessions by UUID alone
     (Codex): no files move.  Just call `on_committed(transcript_path)`
     so leap's recorded cwd in
     `.storage/cli_sessions/<name>/<tag>.json` is bumped immediately,
     and return the unchanged `transcript_path` so the caller treats
     it as a successful relocation.  Skip the file-move primitives
     and the signal-blocking — there's nothing critical to protect.

   Return value contract:
   - **non-`None` string** (the new path, or unchanged path for
     logical moves) → success, caller sets `target_cwd = dst_cwd`.
   - **`None`** → not applicable / can't be located; caller falls
     through to chdir into `src_cwd` (the "Original" path still works).
   - **Raise `RelocationError`** → real disk-side failure; caller
     surfaces the message to the user and exits non-zero.  Source must
     be intact when this raises.

3. **Reference behavior the four resume-capable built-in providers exhibit** —
   pick the one your CLI most resembles and copy the shape:

   | Provider | Storage layout | What `relocate_session` does |
   |----------|----------------|-----------------------------|
   | `ClaudeProvider` | `~/.claude/projects/<cwd-slug>/<uuid>.jsonl` (+ optional `<uuid>/` sidecar dir) | Atomic move of the JSONL **and** the sidecar tree across cwd-derived slugs.  Pre-flight slug check, rogue-writer snapshot guards on both file and tree, rollback on sidecar-rename failure. |
   | `GeminiProvider` | `~/.gemini/tmp/<slug>/chats/session-…jsonl` + `~/.gemini/projects.json` registry mapping `cwd → slug` | Locate src by parsing first-line `sessionId` (filename embeds only an 8-char prefix), claim a fresh dst slug via Gemini's exact `slugify(basename(cwd))` algorithm with `-N` disambiguation, atomically update `projects.json`, roll back the file commit if the registry write fails. |
   | `CursorAgentProvider` | `~/.cursor/chats/<MD5(workspace)>/<chatId>/` (whole directory tree) | Move the full chat dir across MD5 hash dirs.  `find_chat_dir` first tries `MD5(prefer_cwd)` then falls back to scanning every project hash dir for the chatId — cursor's workspace-root walk may have hashed a parent of the recorded cwd.  Snapshot-based rogue-writer guard + best-effort prune of the now-empty src project hash dir. |
   | `CodexProvider` | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` (date+UUID, **cwd-agnostic**) | **No file move.**  Just calls `on_committed(transcript_path)` so leap's recorded cwd is bumped immediately.  Also returns `['-C', os.getcwd(), 'resume', session_id]` from `resume_args` so codex's *own* "Choose working directory to resume" prompt doesn't fire on top of leap's prompt. |

4. **`session_exists(session_id, cwd) -> bool`** *(only if your CLI's
   records have empty `transcript_path`)*
   The picker's stale-record filter normally checks
   `os.path.getsize(transcript_path)` — if your CLI doesn't expose a
   transcript path (e.g. Cursor records `transcript_path: ""`), that
   check can't fire and stale records linger forever.  Override
   `session_exists` to do a cheap on-disk check (e.g. Cursor's
   `find_chat_dir` scans `~/.cursor/chats/<hash>/<id>/`); the picker
   will hide records that return `False`.

**You get these for free — no code needed, but worth knowing:**

- *Records bookkeeping by `session_id`.*  `relocate_records()` in
  `leap.utils.resume_store` rewrites every `cli_sessions/<cli>/<tag>.json`
  entry matching a given `session_id` — not by `transcript_path`, which
  would silently no-op for empty-path records like Cursor's.  The
  shared `_on_committed` callback in `leap-resume.py` calls it for
  you; you just need to invoke `on_committed(new_path)` from your
  `relocate_session`.  Pass the new path for real moves, the
  unchanged path for logical no-op moves, or `''` if your CLI doesn't
  track transcript paths.
- *Hard-fail on dropped resume.*  When `LEAP_RESUME_SESSION_ID` is
  set but the resume can't be honored (unknown provider, no
  `supports_resume`, `--cli` mismatch, etc.), `leap-server.py` exits
  non-zero with a yellow `✗ Refusing to start` stderr message
  instead of silently starting a fresh session — `_apply_resume_or_fail`
  handles this centrally.  Just make sure your `supports_resume`
  accurately reflects whether `relocate_session` + `resume_args` are
  actually implemented.

**TL;DR — minimum overrides for a new resume-capable CLI:**

```python
from typing import Any, Optional

class MyCLIProvider(CLIProvider):
    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def requires_cwd_bound_resume(self) -> bool:
        # True for CLIs whose ``<cli> resume <id>`` only finds the
        # session when run from the recorded cwd; False for ones that
        # find sessions by id alone (e.g. Codex).
        return True

    def extract_session_id(self, hook_data: dict) -> Optional[str]:
        ...  # pull the session id from the hook payload

    def resume_args(self, session_id: str) -> list[str]:
        ...  # build the argv tokens that resume <session_id>

    def relocate_session(
        self,
        session_id: str,
        src_cwd: str,
        dst_cwd: str,
        *,
        transcript_path: str = '',
        on_committed: Optional[Any] = None,
    ) -> Optional[str]:
        # File-move flavor (like Claude/Gemini/Cursor):
        #   write src/leap/utils/<name>_session_move.py using the
        #   relocation.py primitives and call into it here.
        # Logical no-op flavor (like Codex):
        #   if on_committed is not None and transcript_path:
        #       on_committed(transcript_path)
        #   return transcript_path or None
        ...

    def session_exists(self, session_id: str, cwd: str) -> bool:
        # Only override if your CLI's records have empty
        # transcript_path (so the picker's path-based stale filter
        # can't see them).  Default returns True.
        ...
```

### Context-usage column (the monitor's "Context" %)

The monitor's **Context** column shows, per session, how full the model's
context window is (e.g. `43%`) so the user sees how close it is to
auto-compaction. It's **opt-in per provider** via two members:

- **`supports_context_usage`** (property, default `False`) — whether this CLI
  can report usage *at all*. `False` → the cell renders **`N/A`** (the CLI
  fundamentally can't, e.g. Cursor). `True` → the cell shows the % when
  available, or **blank** when `context_usage()` returns `None` (supported, but
  no data yet).
- **`context_usage(self, cli_name, tag, storage_dir)`** (default `None`) — the
  measurement. The provider locates its *own* source; `cli_name` is the row's
  recorded CLI name (pass it to `latest_transcript_for` so **custom CLIs** read
  their own `cli_sessions/<name>/` subdir).

The monitor just calls `get_provider(cli_provider).context_usage(cli_provider,
tag, STORAGE_DIR)` and renders `ContextUsage.percent` color-coded
green→amber→red. You don't touch the monitor.

**Two source shapes** (add a public entry point in `utils/context_usage.py`):

1. **Transcript CLIs (Claude / Codex / Gemini).** Add a per-CLI
   `_<cli>_usage_from_tail(tail: bytes) -> Optional[ContextUsage]` and a thin
   `<cli>_context_usage(path)` = `_context_usage(path, _<cli>_usage_from_tail)`
   (shared stat/mtime cache + 32 KiB tail read + a final exception net — this
   runs on the render thread, so the parser must **never throw**; guard every
   field with `isinstance`). The provider resolves the path itself:
   ```python
   @property
   def supports_context_usage(self) -> bool: return True
   def context_usage(self, cli_name, tag, storage_dir):
       tp = latest_transcript_for(storage_dir, cli_name, tag)
       return mycli_context_usage(tp) if tp else None
   ```

2. **CLIs with no transcript usage but a status line (Copilot).** Copilot's
   transcript exposes no live usage, but its **status line** receives the live
   numbers (`current_context_tokens`, `context_window_size`, `model`) on stdin
   each render. Leap installs `leap-copilot-statusline.py` (registered in
   `~/.copilot/settings.json` by `CopilotProvider.configure_hooks`, chaining any
   existing status line via a `leap-statusline-chain` sidecar); the script
   writes `<storage>/sockets/<tag>.context` (the status-line subprocess inherits
   `LEAP_TAG`/`LEAP_SIGNAL_DIR`). The provider reads that file via
   `copilot_context_usage(state_path)`. To add a status-line CLI: install its
   script in `configure_hooks` (and have `_install_and_configure` in
   `configure_hooks.py` copy it), keep `hooks_installed()` independent of it
   (the status line is optional — don't gate session startup on it), and read
   the state file in `context_usage`.

**What to measure:** the **prompt** size of the latest turn (the conversation
loaded into the window), *not* the model's reply. Token semantics differ:
- **Claude** reports new (uncached) input separately from the cached prefix, so
  prompt = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.
- **Codex / Gemini / Copilot** report `input` (or `current_context_tokens`) as
  the *full* prompt (cached is a subset), so just use it.

**Window (the denominator):** prefer a value the CLI records, and prefer the
*effective* limit the CLI bases its own "% used" / compaction on so Leap's %
matches what the user sees (Codex carries `info.model_context_window`; Copilot's
status line carries `displayed_context_limit` - the limit it shows the user and
auto-compacts against ~80% - preferred over the raw `context_window_size`).
Otherwise map the model id to a window with a sane default (Gemini ~1M; Claude
200k, or 1M via the `~/.claude.json` `[1m]` detection). Add a usage-overflow
fallback if the window is ambiguous (usage above a window size proves a larger one).

**When a CLI genuinely can't (leave `supports_context_usage = False` → N/A):**
**Cursor** — its CLI exposes no token usage at all, records no `transcript_path`,
and stores chats in an opaque content-addressed SQLite blob store. There's no
on-disk number to read, so its Context cell shows `N/A`.

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

The `configure_hooks()` method on your provider class IS the hook configuration. The unified `src/scripts/configure_hooks.py` script automatically discovers all registered providers and calls their `configure_hooks()` method during `make install`, `make update`, and `make reconfigure`.

**What your `configure_hooks()` must do:**

1. Load the CLI's config file
2. Remove old Leap entries (search for `"leap-hook.sh"` marker)
3. Add entries that call the hook script with state arguments:
   - **Stop hook**: `<hook_path> idle` — Called when CLI finishes processing
   - **Notification hooks** (if supported): `<hook_path> needs_permission`, `<hook_path> needs_input`
4. Write the config back **atomically** — use `atomic_write_json()` (or `atomic_write_text()`) from `leap.utils.atomic_write`. The session-start gate reads these settings files concurrently and a non-atomic write can leave a half-truncated file mid-rewrite, which would make `hooks_installed()` return False and falsely block the user.

**The hook script** (`leap-hook.sh`) is shared across all CLIs. It:
- Reads `LEAP_TAG` and `LEAP_SIGNAL_DIR` env vars (set by `get_spawn_env()`)
- Writes `{"state": "<state>"}` to `.storage/sockets/<tag>.signal`
- For idle state, also extracts the last assistant message from the transcript

If your CLI doesn't support hooks at all, you can implement a no-op `configure_hooks()`, but state detection will rely entirely on PTY output patterns and silence timeout, which is less reliable. **You'll still need to implement `hooks_installed()` returning `True` unconditionally** so the session-start gate doesn't block the user.

**`hooks_installed()` — mirror image of `configure_hooks()`:**

The session-start gate (in `leap-server.py:_enforce_hooks_installed_or_exit`) calls `provider.hooks_installed()` before spawning the server. If it returns False, the server refuses to start and points the user at `leap --reconfigure`. This catches the "user installed the CLI after Leap" case (where install-time hook config was skipped because the binary wasn't on PATH yet) plus generic "user wiped their settings file" recovery.

**Implementation pattern (wrap the whole body in a broad try/except):**

```python
def hooks_installed(self) -> bool:
    try:
        hook_script = self.hook_config_dir / "leap-hook.sh"
        if not hook_script.is_file():
            return False
        with open(<your settings file>, "r") as f:
            data = json.load(f)            # or tomllib.load, etc.
        # Walk your CLI's hook config defensively.  Use isinstance()
        # checks at every nesting level — a third-party tool or a
        # hand-edit could leave a valid-JSON-but-wrong-shape file
        # (e.g. ``"command": null`` or ``"hooks": "stringy"``), and
        # the `in` operator on a non-string raises TypeError.
        ...
        return False
    except Exception:
        return False
```

**Critical rules for `hooks_installed()`:**

- Both halves must be true: hook script exists AND settings file references it. Either alone isn't enough (a stale settings file pointing at a wiped script is still broken).
- **Never raise.** Wrap the entire body in `try: ... except Exception: return False`. The gate calls `hooks_installed()` on the hot path of `leap <tag>` — a traceback there would crash the session with no useful remediation, while returning False at least fires the gate's friendly error pointing at `leap --reconfigure`. `BaseException` (KeyboardInterrupt, SystemExit) deliberately propagates.
- Lenient hook-entry check: any single entry referencing `leap-hook.sh` counts. Do NOT require specific events (Stop / Notification / etc.) — that would break older installs whenever new events are added to `configure_hooks()`.
- **`isinstance()` at every nesting level.** Don't trust the JSON shape — `data.get("hooks")` could be a list, `entry.get("command")` could be `None` or an int. Always check before iterating or doing `in` checks.

**Custom (user-defined) CLIs** inherit `hooks_installed()` from their base provider via `CustomCLIProvider.__getattribute__`'s delegation — there's also an explicit `def hooks_installed(self): return self._base.hooks_installed()` on `CustomCLIProvider` to satisfy `ABCMeta` (the abstract-method check happens at class-creation time, before delegation can kick in). Custom-CLI authors don't write either method themselves; they pass `base_provider=ClaudeProvider()` (or one of the other four) to `CustomCLIProvider.__init__` and `base_type` follows automatically. **All custom CLIs are variants of one of the five base CLIs** — this is a hard constraint of the project.

**`deconfigure_hooks()` — symmetric counterpart of `configure_hooks()`:**

The `CLIProvider` base class provides a default `deconfigure_hooks()` that removes `leap-hook.sh` and `leap-hook-process.py` from `hook_config_dir`. The unified `src/scripts/unconfigure_hooks.py --all` script calls this during `make uninstall`.

If your provider writes anything into a CLI settings or config file during `configure_hooks()`, you **must** override `deconfigure_hooks()` to undo those writes. The override must:

1. Surgically remove only Leap's entries from each config file (preserve all other user settings)
2. Write back **atomically** — same `atomic_write_json()` / `atomic_write_text()` pattern as `configure_hooks()`
3. Call `super().deconfigure_hooks()` at the end to clean up the script files
4. **Never raise** — wrap each config-file operation in its own `try: ... except Exception: pass`. Uninstall must complete even if one file is missing or corrupt.

**Implementation pattern:**

```python
def deconfigure_hooks(self) -> None:
    """Remove Leap's hook entries from ~/.<cli>/settings.json."""
    try:
        if SETTINGS_FILE.is_file():
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else None
                if isinstance(hooks, dict):
                    changed = False
                    for event in list(hooks.keys()):
                        entries = hooks.get(event)
                        if not isinstance(entries, list):
                            continue
                        cleaned = [
                            e for e in entries
                            if not (
                                isinstance(e, dict)
                                and "leap-hook.sh" in e.get("command", "")
                            )
                        ]
                        if len(cleaned) != len(entries):
                            hooks[event] = cleaned
                            changed = True
                    if changed:
                        atomic_write_json(SETTINGS_FILE, data)
    except Exception:
        pass
    super().deconfigure_hooks()
```

**Copilot-style state restoration:** If your `configure_hooks()` displaces an existing user value (e.g. a pre-existing status-line command), save the original to a sidecar file at install time and restore it in `deconfigure_hooks()`. See `CopilotProvider.configure_hooks()` / `CopilotProvider.deconfigure_hooks()` and the `leap-statusline-chain` file pattern.

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

**Primary path (automatic):** `make uninstall` calls `src/scripts/unconfigure_hooks.py --all`, which calls `provider.deconfigure_hooks()` for every registered provider. If your `deconfigure_hooks()` override is correct, **no Makefile edit is needed** for the primary cleanup.

**Safety-net `rm -f` lines:** The `uninstall` target also has explicit `rm -f` lines for every provider's hook scripts — these run after the venv is removed and act as a fallback if the Python cleanup fails. Add your provider's script files to that block:

```makefile
@rm -f "$$HOME/.<cli_config_dir>/leap-hook.sh" "$$HOME/.<cli_config_dir>/leap-hook-process.py" 2>/dev/null || true
```

If your `configure_hooks()` copies additional scripts (e.g. a status-line script like Copilot's `leap-copilot-statusline.py`), add those to the safety-net line too.

### 7-10. Automatic — No Changes Needed

These are handled automatically by the abstractions:

- **ASCII banner**: `print_banner()` uses `provider.display_name`
- **Monitor table**: `table_builder_mixin.py` reads `provider.display_name`
- **CLI selector**: `leap-select-cli.py` reads from `list_providers()` + `get_provider().display_name`
- **Shell flags**: `configure-shell-helper.sh` generates `LEAP_<NAME>_FLAGS` from `list_providers()`. Hyphens in provider names are replaced with underscores (e.g. `cursor-agent` → `LEAP_CURSOR_AGENT_FLAGS`). `leap-select.sh` does the same conversion when reading the env var. **If your provider name contains hyphens**, verify both scripts produce matching variable names — a mismatch means the user's custom flags won't be picked up

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

### Real-CLI gotchas (learned adding GitHub Copilot, a hookless animated TUI)

- **The idle prompt may not be quiet.** Some TUIs animate their input box
  and emit PTY output *continuously even while idle* - but only in a
  focused real terminal; a bare pexpect PTY stays quiet, so a headless
  capture lies. Continuous output defeats every silence-based idle
  fallback (the session sticks on RUNNING) and makes cursor visibility a
  useless busy/idle signal. The fix is to drive idle off a stable footer
  string (`idle_indicator_patterns`), not silence. So: verify idle /
  interrupt behavior against a *real focused terminal* (or a live
  `.storage/state_logs/<tag>.log`), never just a headless pexpect run.
- **Once footer-driven, `_reset_screen()` on a transition is a trap.** If
  the footer-detector must later *exit* the state you're entering (e.g.
  INTERRUPTED -> idle), do NOT wipe the screen on the way in: an
  incremental-repaint TUI thinks the footer is already drawn and never
  re-emits it, so the idle footer never returns and the session sticks.
  Keep the screen (gate the reset off for `idle_indicator_patterns`
  providers). Resetting is only safe on a transition *into* idle - the
  resting state the detector no longer needs to leave.
- **Don't assume Escape interrupts.** Copilot ignores Escape mid-turn and
  cancels on Ctrl+C; set `interrupt_key` (it defaults to Escape).
- **A "question" is not a "permission".** If the CLI asks the user
  questions *and* asks tool-permission, map the question to `needs_input`
  (via `input_dialog_patterns`), not `needs_permission` - else ALWAYS-mode
  auto-approve auto-answers it for the user. Their footers can differ
  ("enter to confirm" vs "enter to select").
- **Gate every shared state-tracker change on a provider property that
  defaults to current behavior.** A new CLI's quirks must never alter the
  shared tracker unconditionally - a blanket change regressed Claude this
  session. Defaults must keep the other CLIs byte-identical; prove it with
  the full unit + integration suites before committing.

## Testing Your Provider

### 1. Verify Registration

```bash
poetry run python -c "
from leap.cli_providers.registry import get_provider, list_providers
print(list_providers())
p = get_provider('<name>')
print(f'{p.name}: {p.display_name}, cmd={p.command}')
print(f'hook_dir={p.hook_config_dir}, requires_binary={p.requires_binary_for_hooks}')
print(f'base_type={p.base_type}, hooks_installed={p.hooks_installed()}')
"
```

### 2. Verify Hook Configuration

```bash
PYTHONPATH=src:$PYTHONPATH poetry run python src/scripts/configure_hooks.py <name> src/scripts/leap-hook.sh
```

After running this, `provider.hooks_installed()` must flip from `False` to `True`. If it doesn't, your `hooks_installed()` and `configure_hooks()` aren't symmetric — the gate at session start will block users with no recovery (running `leap --reconfigure` would re-run `configure_hooks()`, which still wouldn't satisfy `hooks_installed()`).

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
- [ ] `configure_hooks()` installs hooks correctly **and writes atomically** (use `leap.utils.atomic_write`)
- [ ] `hooks_installed()` is the symmetric inverse of `configure_hooks()` — both halves checked, never raises, lenient on which hook events are present
- [ ] After running `configure_hooks()`, `hooks_installed()` flips to `True`
- [ ] `deconfigure_hooks()` undoes everything `configure_hooks()` wrote (if you write to settings files); calls `super().deconfigure_hooks()`; never raises; tested manually or via unit test that settings are cleanly restored
- [ ] `hook_config_dir` points to correct location
- [ ] `requires_binary_for_hooks` set correctly
- [ ] **Leap Resume** feature wired (if the CLI supports resume): `supports_resume`, `extract_session_id`, `resume_args` — or explicitly decide to skip
- [ ] **Cross-cwd resume / move mechanism** wired (only when `supports_resume = True`):
      - [ ] `requires_cwd_bound_resume` set correctly (`True` if the CLI's resume needs cwd to match its recorded path)
      - [ ] `relocate_session()` implemented — file-move (Claude/Gemini/Cursor pattern) **or** logical no-op (Codex pattern, just calls `on_committed`)
      - [ ] If file-move: created `src/leap/utils/<name>_session_move.py` using the shared `relocation.py` primitives (`signals_blocked`, `stage_copy_*`, `commit_*`, `must_remove_tree`, `make_tmp_path`)
      - [ ] `session_exists()` overridden if your CLI's records have empty `transcript_path`
      - [ ] Verified the *Original* and *Current* picker options both produce a working resume (manually test from a cwd different than the recorded one)

### Shell & Makefile
- [ ] Shell launcher script created (`src/scripts/<name>-leap-main.sh`)
- [ ] Makefile: `chmod +x` for launcher script in `configure-shell` target
- [ ] Makefile safety-net `rm -f` block updated with any extra scripts your `configure_hooks()` copies (the primary cleanup runs via `unconfigure_hooks.py --all` → `deconfigure_hooks()` automatically)
- [ ] If provider name contains hyphens: verify `LEAP_<NAME>_FLAGS` uses underscores in both `configure-shell-helper.sh` and `leap-select.sh`

### String references (grep for existing provider names!)
- [ ] `cli_providers/__init__.py` — module docstring
- [ ] `cli_providers/base.py` — docstring examples (name, command, display_name, hook_config_dir)
- [ ] `cli_providers/registry.py` — `get_provider()` docstring
- [ ] `server/server.py` — usage messages (grep `Usage:` in `main()`), docstrings
- [ ] `server/metadata.py` — docstring
- [ ] `server/state_tracker.py` — module docstring
- [ ] `server/pty_handler.py` — module docstring, `__init__` docstring
- [ ] `utils/terminal.py` — `print_banner()` docstring
- [ ] `scripts/leap-hook.sh` — header comments (provider list AND per-CLI stdin format comment)
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
