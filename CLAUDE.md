# Leap

PTY-based client-server system for managing AI CLI sessions (Claude Code, OpenAI Codex, Cursor Agent, Gemini CLI) with message queueing, image support, and native IDE scrolling.

## Quick Start

```bash
make install                # Install core
make install-monitor        # Install GUI (optional)
source ~/.zshrc             # Reload shell

leap mytag                       # Terminal 1: Select CLI + start server
leap mytag                       # Terminal 2: Connect client
leap                             # Interactive: choose CLI + session name

leap --reconfigure               # After installing a new CLI/IDE/terminal post-Leap
```

**Installed a new CLI / IDE / terminal after Leap?** Run `leap --reconfigure`. The install-time configures (`make install`) skip anything that wasn't on disk at the time, so newly-installed tools have no hook integration. The session-start gate in `leap-server.py` will refuse to spawn the server for a CLI whose hooks aren't wired up, with a stderr error pointing here.

## Reference Skills

Deep subsystem docs live as on-demand skills in `.claude/skills/` - Claude loads them automatically when a task is relevant, so they stay out of this always-loaded file. Reach for them when working in that area:

| Skill | Use when... |
|-------|-------------|
| `codebase-map` | locating a file / class / module (full source tree + key-classes table) |
| `auto-approve-architecture` | touching Claude auto-approve, the CLI state machine, or hook handling |
| `monitor-pr-tracking` | working on monitor SCM/PR tracking or the session table |
| `cursor-editor-agent-tabs` | working on the read-only Cursor editor Agent-tab rows |
| `monitor-code-signing` | touching Makefile signing, the py2app build, or TCC/Accessibility |
| `add-cli-provider` | adding a new CLI backend (provider) |
| `add-dialog` | adding a new monitor dialog / window |
| `add-client-command` | adding a new client (`!`) command |
| `add-monitor-theme` | adding a new monitor color theme |
| `create-macos-icon` | creating / updating the macOS app icon |

## Project Structure

High-level map only. Full annotated tree + the key-classes table: see the `codebase-map` skill.

```
src/
├── scripts/              # Entry points: leap-main.sh, leap-server.py, leap-client.py,
│                         #   leap-monitor.py, leap-slack.py, hooks, resume picker, installers
└── leap/
    ├── cli_providers/    # CLI backends (Strategy pattern): claude / codex / cursor_agent /
    │                     #   gemini + base.py, registry.py, states.py
    ├── utils/            # constants, terminal, menu, socket_utils, resume_store,
    │                     #   relocation + per-CLI session-move helpers
    ├── server/           # PTY server: server.py, pty_handler.py, socket_handler.py,
    │                     #   queue_manager.py, metadata.py
    ├── client/           # client.py, socket_client.py, input_handler.py, image_handler.py
    ├── monitor/          # PyQt5 GUI: app.py + _mixins/ dialogs/ ui/ pr_tracking/ resources/,
    │                     #   themes, navigation, scm_polling, cursor_gui_scan, sleep_guard
    ├── slack/            # bot, config, output_capture, output_watcher, message_router
    └── vscode-extension/ # VS Code / Cursor terminal-selector extension

tests/   # pytest - tests/unit/ (fake-clock) + tests/integration/ (real-PTY)
assets/  # leap-icon.png/.icns + themed logo variants
```

## Key Classes

Full class/function -> file -> purpose table: see the `codebase-map` skill. The load-bearing few: `CLIProvider` (`cli_providers/base.py`), `LeapServer` (`server/server.py`), `LeapClient` (`client/client.py`), `MonitorWindow` (`monitor/app.py`), `get_provider()` (`cli_providers/registry.py`).

## Runtime Data Files

All runtime data is stored in the centralized `.storage` directory at the project root:

| File | Location |
|------|----------|
| Settings | `.storage/settings.json` |
| Queue | `.storage/queues/<tag>.queue` |
| History | `.storage/history/<tag>.history` |
| Socket | `.storage/sockets/<tag>.sock` |
| Metadata | `.storage/sockets/<tag>.meta` |
| Client lock | `.storage/sockets/<tag>.client.lock` |
| Server lock | `.storage/sockets/<tag>.server.lock/` (directory) |
| Pinned sessions | `.storage/pinned_sessions.json` |
| Monitor prefs | `.storage/monitor_prefs.json` (includes `row_order`, `row_sort_mode`, `aliases`) |
| Notification seen state | `.storage/notification_seen.json` |
| PR context preset selection | `.storage/leap_selected_preset` |
| Auto-fetch /leap preset selection | `.storage/leap_auto_fetch_preset` |
| Message bundle preset selection | `.storage/leap_selected_direct_preset` |
| Preset definitions | `.storage/leap_presets.json` |
| Queue images | `.storage/queue_images/<hash>.png` (MD5-deduped, cleaned on server startup) |
| Note images | `.storage/note_images/<hash>.png` (MD5-deduped, persistent) |
| Signal file | `.storage/sockets/<tag>.signal` |
| Last response (Slack) | `.storage/sockets/<tag>.last_response` |
| Slack config | `.storage/slack/config.json` |
| Saved messages | `.storage/saved_messages.json` |
| Slack sessions | `.storage/slack/sessions.json` |
| CLI session tracking | `.storage/cli_sessions/<cli>/<tag>.json` (list of `{session_id, transcript_path, cwd, last_seen}` recorded by `leap-hook-process.py`; drives `leap --resume`. One subdir per provider — `claude/`, `codex/`, `cursor-agent/`, `gemini/`, plus any custom CLI that implements the Leap Resume interface) |
| CLI PID map | `.storage/pid_maps/<cli_pid>.json` (written by server when spawning the CLI: `{tag, signal_dir, python, cli_provider}`. Lets `leap-hook.sh` recover context via a PPID walk when a CLI strips env vars from hook subprocesses — the project dir itself is recovered from `$LEAP_PROJECT_DIR` or the `export LEAP_PROJECT_DIR=` line in `~/.zshrc`/`~/.bashrc`. Swept by `leap-main.sh`'s `cleanup_dead_sockets` using `kill -0`) |
| Sudo password (lid-close) | `.storage/sudo_pass.b64` (mode 0600, base64-encoded — NOT encrypted; only present while the lid-close override is enabled, deleted the moment the user unticks the box) |
| Disable-sleep marker | `.storage/disablesleep.marker` (zero-byte sentinel; present iff Leap currently holds `pmset disablesleep=1`. Drives crash recovery on next launch — if the marker is on disk but the monitor isn't running, the next startup attempts a silent `pmset disablesleep 0` using the saved password, or pops a manual-fix dialog if that fails) |
| Update-in-progress marker | `.storage/update_in_progress` (JSON: `{"pre_pull_sha": "<40-hex>", "started_at": <epoch>}`. Written by `leap-update.sh` BEFORE its `git pull`, removed by `.update-after-pull` at end of phase 2 on success; an EXIT trap in the script cleans it up on phase 1 abort. Read by `WhatsNewDialog` to keep showing the pulled commits — `<pre_pull_sha>..origin/main` instead of `HEAD..origin/main` — and by `UpdateCheckWorker` to skip its background fetch while the update is running. 30-min stale-timestamp fallback in both readers covers the phase-2-crash case where the marker is orphaned) |

## Server Queue Shortcut

Type `^^` in the server terminal to queue a message. Double-caret (`^^`) activates capture mode — characters are hidden from the CLI and shown in a `[Leap Q]` prompt on the input line. Works at any point: type `^^msg` to start fresh, or type `hello` then `^^` to convert already-typed text into a queued message. Press Enter to queue, Escape or Ctrl+C to cancel.

**Saved messages**: Type `^^` inside capture mode to save the current message to history and clear the buffer. Browse saved messages with arrow up/down. History persists across sessions in `.storage/saved_messages.json` (max 100 entries, shared across all CLIs/sessions). Editing a recalled message does not modify the saved history — only explicit `^^` save does.

**CLI input-history recall (↑/↓ outside capture)**: Leap intercepts ↑/↓ at the CLI's input prompt and drives recall itself by reading the CLI's own on-disk history (Claude: `~/.claude/history.jsonl` filtered by `project == cwd`; Codex: `~/.codex/history.jsonl`; Cursor: `~/.cursor/prompt_history.json`; Gemini: `~/.gemini/tmp/<slug>/logs.json`). Without the intercept the recalled text lives only in the CLI's TUI render and never enters Leap's input mirror — so a subsequent `^^` would snapshot an empty buffer. With it, `^^` after ↑ captures the recalled message (including paste content for Claude entries: `[Pasted text #N]` placeholders are resolved inline from `pastedContents` so the actual content reaches the LLM on submit, not the placeholder string). The cache invalidates on Enter / Ctrl+C / queue dispatch / capture exit so just-submitted messages show up immediately on the next ↑. Providers opt in via `CLIProvider.input_history(cwd)`; returning `None` falls back to passthrough (CLI handles ↑/↓ natively).

## Auto-Approve Architecture (Claude)

ALWAYS-mode auto-approve is hook-based: Claude's `PermissionRequest` hook returns "allow" with no dialog rendered, so Leap's state machine stays RUNNING throughout. A legacy TUI-menu path (`_try_auto_approve`, types `1\r`) is the fallback for older Claude builds. `AskUserQuestion` is deliberately excluded from the matcher so its dialog still reaches the user. Per-session `auto_send_mode` isolation, the up/down-arrow handling during dialogs and slash-command pickers, and the dialog-detection rules are subtle - full reference in the `auto-approve-architecture` skill.

## Client Commands

| Command | Action |
|---------|--------|
| `!h` or `!help` | Show help |
| `<message>` | Queue message (auto-sends when ready) |
| `!d <msg>` or `!direct <msg>` | Send directly (bypass queue) |
| `!e <index>` or `!edit <index>` | Edit queued message by index (0=first) |
| `!l` or `!list` | Show queue |
| `!c` or `!clear` | Clear queue |
| `!f` or `!force` | Force-send next queued message |
| `!autosend` or `!as` | Toggle auto-send mode (pause/always) |
| `!slack` or `!slack on/off` | Show status or toggle Slack for this session |
| `!x` or `!quit` (`Ctrl+D`) | Exit client |

## Adding Features

- **New CLI provider** → See the `add-cli-provider` skill for a comprehensive step-by-step guide. Key files: create `cli_providers/<name>.py`, register in `registry.py`, implement `configure_hooks()` and `hooks_installed()` (the latter must be the symmetric inverse of the former — both halves checked, never raises). The CLI selector, monitor table, ASCII banner, and shell flags are all dynamic and require no changes.

  **All custom CLIs are variants of one of the four base CLIs** (Claude / Codex / Cursor Agent / Gemini). `CustomCLIProvider` (in `registry.py`) wraps a base provider and delegates everything via `__getattribute__` — including `hooks_installed()` and `base_type`. Custom-CLI authors don't set `base_type` themselves; they pass `base_provider=ClaudeProvider()` (or one of the other three) to `CustomCLIProvider.__init__`, and `base_type` follows automatically (it resolves to the base's `name` via the `__getattribute__` delegation). The session-start gate uses `get_provider(provider.base_type).hooks_installed()` so custom CLIs share their base's hook setup automatically. There is no path for a custom CLI that's not built atop one of the four — design accordingly.
- **New monitor dialog / window** → See the `add-dialog` skill. Covers `ZoomMixin` setup, dialog geometry persistence, theme integration, the font-size cascade quirk, and — critically — the **prefs persistence model** (`MonitorWindow._DIALOG_OWNED_KEYS` and why `save_monitor_prefs(self._prefs)` must NOT be called outside `_save_prefs`). Skipping that last part is the most common way dialog state silently gets clobbered.
- **Adding / removing / reordering a session-table column** → The `COL_*` constants in `monitor/app.py` are referenced by *four* positional-index sites that DON'T import them — change any column and you MUST update all four in the same diff, or they silently drift off-by-one (wrong separators, wrong monospace columns, wrong alignment):
  1. `_HEADER_LABELS` in `monitor/app.py` (header strings, parallel to `COL_*` order)
  2. `_CENTER_COLS` in `_mixins/table_builder_mixin.py` (data cols whose plain-text cells center)
  3. `_MONO_COLS` in `_mixins/table_builder_mixin.py` (cols rendered in monospace font)
  4. `COLUMN_GROUPS` in `ui/table_helpers.py` (drives the inter-/intra-group vertical separators)
- **Utils** → `src/leap/utils/`
- **Server** → `src/leap/server/`, update `LeapServer`
- **Client** → `src/leap/client/`, update `LeapClient`
- **Monitor** → `src/leap/monitor/`, update `MonitorWindow`
- **Socket communication** → Use `send_socket_request()` from `utils/socket_utils.py` for any new code that needs to talk to a Leap server via Unix socket. Do not duplicate the connect/send/recv pattern. Incoming messages are capped at `MAX_MESSAGE_SIZE` (1 MB) in `socket_handler.py`; larger payloads are rejected.
- **New third-party dependencies** → Add to `pyproject.toml` under the appropriate group: `[tool.poetry.dependencies]` for core, `[tool.poetry.group.monitor.dependencies]` for GUI-only deps. Run `poetry lock && poetry install` after. All imports must be at module top level (no inline imports except optional deps).
- **New `.storage` subdirectories** → If you add a new subdirectory under `.storage/`, you **must** update three places:
  1. Add the constant in `utils/constants.py` (next to `QUEUE_DIR`, `SOCKET_DIR`, `HISTORY_DIR`)
  2. Add a `.mkdir()` call in `ensure_storage_dirs()` in `utils/constants.py`
  3. Add the path to the `ensure-storage` target in `Makefile`
- **Theming** → Use `current_theme()` from `monitor/themes.py` to access colors. Never hardcode colors in monitor code — use theme properties (e.g. `t.accent_green`, `t.text_primary`). Theme colors are applied via `QPalette` (preserves native macOS widget rendering) + minimal QSS. Cell button styles use `close_btn_style()` / `active_btn_style()` / `menu_btn_style()` from `table_helpers.py`. Theme persists as `"theme"` in `monitor_prefs.json` (default: `"Nord"`). Twelve built-in themes: Leap, Amber, Midnight, Cosmos, Velvet, Ocean, Monokai, Nord, Solarized Dark, Synthwave, Dawn, Barbie.
- **New assets (images, icons, themed variants)** → Any new asset file in `assets/` that the monitor uses at runtime **must** also be added to `DATA_FILES` in `setup.py`. The py2app bundle only includes explicitly listed files — assets missing from `setup.py` will work in `make run-monitor` (dev mode) but silently fail in the installed app. Logo text variants use `glob('assets/leap-text*.png')` so new theme logos are auto-included, but other new assets need manual addition.

## Testing

```bash
make test                         # All tests (unit + integration)
make test-unit                    # Fast unit tests only (fake clock)
make test-integration             # Real-PTY integration tests (~2 min)
poetry run pytest tests/ -v       # All tests with verbose output
```

- Tests use `pytest` (dev dependency, `poetry install --with dev`)
- `tests/unit/` — fake-clock tracker tests and other in-process units
- `tests/integration/` — real bash-via-pexpect PTY + pyte rendering; shared `PTYFixture` lives in `tests/conftest.py`
- `ClaudeStateTracker` uses an injectable `clock` parameter — tests pass a fake clock (`lambda: t[0]`) for deterministic time control
- Use `tmp_path` fixture for signal files
- Test file naming: `tests/unit/test_<module>.py` or `tests/integration/test_<topic>.py`

## Code Conventions

- **No em-dashes in user-visible text**: Never put an em-dash `—` (or its `\u2014` escape) in anything the user sees: GUI labels/titles/tooltips, terminal banners, log messages, Slack text, install/update output (echoes), README, help text. Use a plain hyphen `-` instead. (Code comments and docstrings are exempt.)
- **Type hints**: 100% coverage on all function signatures and return types. Use `Optional[X]` (not `X | None`) for consistency.
- **Imports**: **Every `import` and `from X import Y` statement MUST live at the top of the module.** No inline imports inside `def` bodies, methods, class bodies, `if/for/while` blocks, or anywhere other than the module header — not for "lazy loading", not for "avoiding startup cost", not as a hotfix to dodge a circular import. Violating this rule has bitten us multiple times (stale references, import-error masking, duplication of the same import in 15 different methods); treat it as a hard ban.
  - **Only two allowed exceptions**, and both live at module top level:
    1. **Optional-dependency fallback**: a top-level `try: import X except ImportError:` block that sets a sentinel (e.g. `WebClient = None`) so the rest of the module can guard on it. Used today for `prompt_toolkit`, `slack_sdk`/`slack_bolt`, `tomllib`/`tomli`, and `AppKit` when the module needs to import on non-macOS.
    2. **Type-only circular-import break**: a top-level `if TYPE_CHECKING:` block for imports used *only* in type annotations. If you hit a real runtime circular import, the fix is to restructure the modules (extract the shared code) — not to sneak an inline import back in.
  - Before adding a new top-level import, check for an existing one — don't duplicate. When moving an inline alias (e.g. `import time as _time`), replace every `_time.` call site with the bare name.
  - Stdlib → third-party → `leap.*`, each group alphabetized.
- **Client commands**: Each command handler is extracted into a private `_handle_*` method on `LeapClient`. The `_process_command` dispatcher delegates to these handlers.
- **Socket pattern**: `SocketClient._send_request()` is the single source of truth for client→server socket communication. `send_socket_request()` in `utils/socket_utils.py` is the lightweight variant for monitor/session_manager code that doesn't need rate-limited error reporting.

## SCM Polling & PR Tracking

The monitor polls GitLab/GitHub for PR status + user notifications, renders PR markers and merged/closed badges, sends PR comments and `/leap` commands into sessions, manages pinned/persistent rows and managed-clone dirty-tree sync, validates branches at startup, and owns the session-table UX (sort modes + drag-reorder, row colors, tag aliases, live filter, new-change fire indicator). Token modes: direct or env-var; GitHub Enterprise URLs are normalized to `/api/v3`. Key timeouts: 15s/request, 30s/poll-cycle, 60s stuck-poll reset, 30s default interval. Full subsystem reference: see the `monitor-pr-tracking` skill.

## Slack Integration

Optional Slack app for bidirectional Leap ↔ Slack communication. Each session gets a thread in the user's DM.

```bash
make install-slack-app   # Install deps + guided setup wizard
leap --slack                 # Start the bot daemon
```

**Data flow**: Claude finishes → hook reads transcript JSONL → writes to signal file → `OutputCapture` writes `.last_response` → `OutputWatcher` posts to Slack. Replies: Slack thread → `MessageRouter` → queue or direct message via socket.

Bot can also be started/stopped from the monitor's **Slack Bot** button. Dependencies: `slack-bolt`, `slack-sdk` (optional poetry group).

## IDE Setup

### JetBrains (PyCharm, IntelliJ, etc.)
**Automatically configured during `make install`** — Terminal Engine set to Classic, "Show application title" enabled. Restart IDEs after installation.

### VS Code / Cursor
**Automatically configured during `make install`** — Terminal selector extension auto-installed, tabs show numbered labels. Extension also configures Shift+Enter to send a distinct CSI u sequence so the client can distinguish it from plain Enter. Cursor (VS Code fork) is detected separately via `__CFBundleIdentifier` and uses its own CLI (`cursor`), settings path, and AppleScript app name. The same `.vsix` extension is installed into both editors.

### iTerm2
**Automatically configured during `make install`** — CSI u (Kitty keyboard protocol) enabled in all profiles so Shift+Enter sends a distinct sequence. Restart iTerm2 after installation for the change to take effect.

### WezTerm
**Automatically configured during `make install`** — `enable_csi_u_key_encoding = true` added to Lua config (`~/.wezterm.lua` or `~/.config/wezterm/wezterm.lua`) so Shift+Enter sends a distinct CSI u sequence. Creates a new config file if none exists. Restart WezTerm after installation for the change to take effect. Full monitor navigation support via `wezterm cli` (navigate, close, open tabs).

### cmux
**No install-time configuration needed.** cmux is a Ghostty-based macOS terminal (`com.cmuxterm.app`); it speaks the Kitty keyboard protocol natively, so Shift+Enter works without a CSI u config step. It appears in the monitor's **Default terminal** dropdown (`_detect_installed_terminals` in `settings_dialog.py`, path + Spotlight on the bundle id).

Runtime detection: cmux inherits Ghostty's `TERM_PROGRAM=ghostty` but also exports `CMUX_*` identifiers (`CMUX_SURFACE_ID`/`CMUX_WORKSPACE_ID`/`CMUX_SOCKET_PATH`/…). `detect_ide()` checks those **before** the `ghostty` branch so cmux sessions are tagged `'cmux'` (navigable) rather than plain `'Ghostty'`.

Monitor navigation/open/close go through cmux's **AppleScript dictionary** (`cmux.sdef`: `application > windows > tabs(workspaces) > terminals`) — **not** the bundled `cmux` CLI, because cmux's control socket defaults to `socketControlMode: cmuxOnly`, which rejects an outside process like the monitor. Three cmux quirks the helpers account for (all verified against a live app): (1) the app-level `terminals` element is unreliable (reports count 0), so they walk `windows > tabs`; (2) cmux does **not** expose a per-surface title — every `terminal.name` reads as a generic "Terminal"; (3) a shell's OSC title (`lps <tag>`, set by the server) is surfaced only as the **workspace (tab) name**, and only for the workspace's **active** surface. So `_navigate_cmux` uses two passes: first match the workspace name (hits when the session's surface is active or is the workspace's only surface), then — if that misses, meaning the target surface is inactive and its title is hidden — **probe**: `focus` each surface in a multi-surface workspace and re-read the workspace name, landing on the match (or restoring the workspace's original focus if none). `_close_cmux` matches the workspace name only (no probe — closing an inactive surface is rare and probing-to-close is more disruptive than the miss). Helpers in `navigation.py`: `_navigate_cmux` / `_close_cmux` (AppleScript, guarded on cmux already running via `_get_app_pid` so the fallback chains never cold-launch it) and `_open_cmux_terminal` (tries the CLI's `new-workspace --command` first for users who can reach the socket — it sends text+Enter — then falls back to AppleScript `new tab` + `input text` + return, which cmux runs as a normal command, verified against a live app — `input text` is not held by bracketed paste).

## Cursor Editor Agent Tabs (read-only monitor rows)

Optional, on by default (`show_cursor_gui_agents`): the monitor shows one read-only row per open Cursor *editor* Agent/Composer tab - live status, PR tracking, and one-click jump to the exact tab. It is a pure display overlay (no PTY, server, or queueing); rows carry `row_type == 'cursor_agent_gui'`. Full design - the on-disk SQLite scan, status mapping, tab focus/jump via the Cursor extension, synthetic-row reconciliation, and the two close buttons - is in the `cursor-editor-agent-tabs` skill.

## Monitor Code Signing

Leap Monitor.app is signed with a per-user self-signed cert (CN `Leap Self-Signed`) kept in a **dedicated keychain** (`~/Library/Keychains/leap-codesign.keychain-db`), not the login keychain. codesign signs silently (no "codesign wants to access key" / "Always Allow" prompt) because the setup script runs `security set-key-partition-list` on that keychain authorized with a password **we** generate - on the login keychain that call would need the user's login password, which is the whole reason for a dedicated keychain. macOS Accessibility/Notification grants survive every `make update` / `leap --update` because TCC keys on the bundle's designated requirement (identifier + cert leaf), which stays stable across rebuilds. The build signs by the cert's SHA-1 (unambiguous vs any old same-named login-keychain cert) with `--keychain`. Full mechanism, the search-list handling, the `--deep` build re-sign, migration, and uninstall notes: see the `monitor-code-signing` skill.

## Troubleshooting

**"Another client already connected"** → `rm .storage/sockets/<tag>.client.lock`

**Stale sockets** → `leap-cleanup`

**`✗ Leap's hooks aren't configured for <CLI>` at session start** → The session-start gate (`leap-server.py:_enforce_hooks_installed_or_exit`) ran `provider.base_type`'s `hooks_installed()` and got False. Almost always means the user installed that CLI / IDE / terminal *after* `make install` ran (so install-time hook configuration silently skipped it). Fix: `leap --reconfigure`. Same flag also recovers from "user wiped `~/.<cli>/settings.json`" or any other partial-config drift.

**Code-signing / TCC / Apple-Silicon build failures** (`⚠ Cert-based signing failed`, Accessibility dies after an update, `✗ Architecture mismatch` / Rosetta) → see the `monitor-code-signing` skill.

## Make Commands

```bash
make install           # Install core + configure shell
make install-monitor   # Build and install GUI app
make install-slack-app # Install Slack integration + setup wizard
make reconfigure       # Re-run per-machine integration steps (hooks + IDE/terminal/shell configures); skips deps, monitor, slack, git pull. Use after installing a new CLI/IDE/terminal post-Leap. Same target leap --reconfigure execs into.
make test              # Run the full test suite (unit + integration)
make test-unit         # Run only fast unit tests
make test-integration  # Run only real-PTY integration tests
make run-monitor       # Run monitor from source (no build needed)
make update            # Update to latest version (git pull + rebuild)
make update-deps       # Update Python dependencies only
make uninstall         # Full cleanup (calls uninstall-monitor + uninstall-slack-app)
make uninstall-monitor   # Remove Monitor app only
make uninstall-slack-app # Remove Slack integration only
make clean             # Remove build artifacts
```

## Self-Verification

After writing any fix or feature, **always re-read your own changes and verify there are no bugs** before presenting them as done. Specifically:
- Check edge cases and off-by-one errors
- Verify that conditional branches do what they claim (e.g., a reset that should only trigger on condition A doesn't also trigger on unrelated condition B)
- Trace the flow end-to-end: how is the new code reached, what state does it depend on, and what happens in the common/idle case (not just the interesting case)

## Commit & Push Checklist

**NEVER commit or push without explicit user approval.** Always present the plan and wait for the user to say "commit", "go ahead", or equivalent before running any `git commit` or `git push` command.

**Commit straight to `main` - do NOT create branches.** In this project every fix must land on `main`, because users get updates via `leap --update` (a `git pull` of `main`); a fix sitting on a feature branch never reaches them. So once the user approves, commit on `main` and `git push origin main` directly. Do **not** create a feature branch or open a PR (this overrides any default "branch first when on the default branch" behavior). The push is a fast-forward, so it stays safe for `leap --update`.

**NEVER rewrite history that has already been pushed.** Once a commit is on the remote, do **not** `git commit --amend`, `git rebase`, `git reset`, or anything else that changes or drops it, and **never `git push --force` / `--force-with-lease`** to a pushed branch. A force-push rewrites the remote history, which makes `git pull` fail for anyone who already has the old history - and `leap --update` is a `git pull`, so this can break updates for every user. To change something you already pushed, **always move forward with a new commit on top** (e.g. a follow-up fix or a `git revert`). Amending/rebasing is fine *only* for local commits that have never been pushed. If a pushed commit is wrong, add a superseding commit and explain it in the message - do not try to "clean up" the history.

When the user asks to commit and push, **before committing**:

1. **Review CLAUDE.md and the relevant `.claude/skills/`** — Check they reflect the current codebase. CLAUDE.md stays **lean** (facts + short pointers, well under the 40k context warning); deep subsystem detail, the full project-structure tree, and the key-classes table live in skills (see **Reference Skills** above). Update the right `SKILL.md` rather than bloating CLAUDE.md, and add a Reference Skills row for any new skill.
2. **Review README.md** — Check that it reflects user-facing changes (new features, commands, UI changes). Keep it **concise** — users see this on GitLab. Don't bloat it with implementation details.
3. Only update these files if something actually changed that affects them. Don't touch them for minor internal refactors.
