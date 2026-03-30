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
```

## Project Structure

```
src/
├── scripts/                     # Entry point scripts
│   ├── leap-main.sh          # Main launcher (called by 'leap' command)
│   ├── leap-cleanup.sh       # Dead session cleanup
│   ├── leap-server.py        # Thin launcher → LeapServer
│   ├── leap-client.py        # Thin launcher → LeapClient
│   ├── leap-monitor.py       # Thin launcher → MonitorWindow
│   ├── leap-slack.py         # Thin launcher → SlackBot
│   ├── leap_monitor_launcher.py  # py2app entry point
│   ├── setup-slack-app.sh       # Interactive Slack app setup wizard
│   ├── configure_jetbrains_xml.py   # JetBrains IDE auto-configuration
│   ├── configure_hooks.py           # Unified hook config (delegates to provider.configure_hooks())
│   ├── configure_claude_hooks.py    # Legacy Claude hook config (kept for reference)
│   ├── configure_codex_hooks.py     # Legacy Codex hook config (kept for reference)
│   └── leap-hook.sh             # CLI hook script (writes state to signal file)
│
└── leap/                     # Main Python package
    ├── __init__.py              # Version, exports
    ├── main.py                  # Package entry point
    │
    ├── cli_providers/           # CLI backend abstraction (Strategy pattern)
    │   ├── __init__.py          # Package exports, get_provider(), list_providers()
    │   ├── base.py              # CLIProvider ABC (patterns, timings, hooks, input)
    │   ├── claude.py            # Claude Code provider (Ink TUI, numbered menus)
    │   ├── codex.py             # OpenAI Codex provider (Ratatui TUI, y/n approval)
    │   ├── cursor_agent.py     # Cursor Agent provider (Ink TUI, menu approval)
    │   ├── gemini.py            # Gemini CLI provider (Ink TUI, radio-button approval)
    │   ├── registry.py          # Provider registry (name → class lookup)
    │   └── states.py            # CLIState enum + state groupings (WAITING/SIGNAL/PROMPT)
    │
    ├── utils/                   # Shared utilities
    │   ├── constants.py         # QUEUE_DIR, SOCKET_DIR, timing, colors, is_valid_tag()
    │   ├── terminal.py          # Terminal title, banner
    │   ├── ide_detection.py     # IDE detection, git branch
    │   └── socket_utils.py     # Shared Unix socket send/recv helper
    │
    ├── server/                  # PTY Server
    │   ├── server.py            # LeapServer - main orchestrator
    │   ├── pty_handler.py       # CLI PTY (pexpect, provider-driven)
    │   ├── socket_handler.py    # Unix socket server
    │   ├── queue_manager.py     # Message queue persistence
    │   └── metadata.py          # Session metadata (IDE, project, branch, cli_provider)
    │
    ├── client/                  # Interactive Client
    │   ├── client.py            # LeapClient - main class
    │   ├── socket_client.py     # Unix socket client
    │   ├── input_handler.py     # Prompt toolkit / readline
    │   └── image_handler.py     # Clipboard image handling
    │
    ├── monitor/                 # GUI Monitor (PyQt5)
    │   ├── app.py               # MonitorWindow (core window + UI init + lifecycle)
    │   ├── server_launcher.py   # PR server clone/checkout/start flow
    │   ├── session_manager.py   # Session discovery + read_client_pid()
    │   ├── scm_polling.py       # SCM poller + background workers
    │   ├── leap_sender.py         # Socket sender for /leap commands + message bundles
    │   ├── navigation.py        # IDE terminal navigation
    │   ├── monitor_utils.py     # Utilities (icon finder, lock removal)
    │   ├── themes.py            # Visual theme definitions (9 built-in themes, manager API)
    │   │
    │   ├── _mixins/             # MonitorWindow mixin classes
    │   │   ├── actions_menu_mixin.py  # Git menu (branch col) + Path menu (Open Terminal/IDE)
    │   │   ├── scm_config_mixin.py    # SCM provider init, setup dialogs, toggles
    │   │   ├── session_mixin.py       # Session merge, navigate, close, delete
    │   │   ├── pr_tracking_mixin.py   # PR tracking, polling, thread send, add-row
    │   │   ├── pr_display_mixin.py    # PR column styling, dock badge, banners
    │   │   ├── notifications_mixin.py # User notification handling
    │   │   └── table_builder_mixin.py # Table build, refresh, settings
    │   │
    │   ├── dialogs/             # Dialog windows
    │   │   ├── git_changes_dialog.py  # Git diff viewer (local, commit, vs main)
    │   │   ├── settings_dialog.py     # Settings (terminal, repos dir, diff tool, new change indicator, cleanup)
    │   │   ├── notifications_dialog.py # Per-type notification config (dock/banner)
    │   │   ├── scm_setup_dialog.py    # Abstract SCM setup base dialog (URL hidden behind "Self-hosted" toggle)
    │   │   ├── gitlab_setup_dialog.py # GitLab connection dialog
    │   │   ├── github_setup_dialog.py # GitHub connection dialog
    │   │   ├── scm_template_dialog.py # Preset editor dialog (PR context + message bundles)
    │   │   ├── add_local_dialog.py    # Add session from local path dialog
    │   │   ├── branch_picker_dialog.py # Branch picker for git difftool comparison
    │   │   ├── queue_edit_dialog.py   # Queue message editor dialog
    │   │   └── notes_dialog.py        # Notes with folders, search, text/checklist, DnD reorder, save as preset, run in session
    │   │
    │   ├── ui/                  # UI components
    │   │   ├── ui_widgets.py    # PulsingLabel, IndicatorLabel
    │   │   ├── dock_badge.py    # Dock icon badge overlay + notification event detection
    │   │   ├── image_text_edit.py # ImageTextEdit (clipboard image paste) + SendMessageDialog
    │   │   ├── log_history.py   # Log history (in-memory + dialog)
    │   │   └── table_helpers.py # Qt helper widgets (separators, tooltip overrides, ColorPickerPopup)
    │   │
    │   ├── pr_tracking/         # PR tracking subsystem
    │   │   ├── base.py          # Abstract SCMProvider, PRState, PRStatus, PRDetails
    │   │   ├── config.py        # GitLab/monitor prefs + pinned sessions persistence
    │   │   ├── gitlab_provider.py # GitLab API implementation
    │   │   ├── github_provider.py # GitHub API implementation
    │   │   ├── git_utils.py     # Git remote URL parsing + PR URL parsing
    │   │   └── leap_command.py    # /leap command data model + formatting
    │   └── resources/
    │       └── activate_terminal.groovy  # JetBrains script
    │
    ├── slack/                   # Slack Integration
    │   ├── __init__.py          # Package init
    │   ├── bot.py               # SlackBot main class (Socket Mode)
    │   ├── config.py            # Slack config + session persistence
    │   ├── output_capture.py    # Capture hook response, write .last_response for Slack bot
    │   ├── output_watcher.py    # Poll .last_response files → post to Slack
    │   └── message_router.py    # Route Slack messages → Leap sessions
    │
    └── vscode-extension/        # VS Code / Cursor Extension
        ├── package.json         # Extension metadata
        ├── extension.js         # Terminal selector logic
        └── README.md            # Extension documentation

tests/
├── __init__.py
└── test_state_tracker.py        # CLIStateTracker state machine tests

assets/
├── leap-icon.png             # Source icon (1024x1024)
├── leap-icon.icns            # macOS icon bundle
├── leap-simple-icon.png      # Alternate flat icon
└── leap-exclusive-icon.png   # Alternate exclusive icon
```

## Key Classes

| Class / Function | File | Purpose |
|------------------|------|---------|
| `CLIState` | `cli_providers/states.py` | State enum (`idle`, `running`, `needs_permission`, `needs_input`, `interrupted`) |
| `CLIProvider` | `cli_providers/base.py` | Abstract base for CLI backends (patterns, hooks, input) |
| `ClaudeProvider` | `cli_providers/claude.py` | Claude Code CLI (Ink TUI, numbered menus, Notification hooks) |
| `CodexProvider` | `cli_providers/codex.py` | OpenAI Codex CLI (Ratatui TUI, y/n approval, Stop hook only) |
| `CursorAgentProvider` | `cli_providers/cursor_agent.py` | Cursor Agent CLI (Ink TUI, menu approval, Stop hook only) |
| `GeminiProvider` | `cli_providers/gemini.py` | Gemini CLI (Ink TUI, radio-button approval, AfterAgent/Notification hooks) |
| `get_provider()` | `cli_providers/registry.py` | Provider lookup by name (`'claude'`, `'codex'`, `'cursor-agent'`, `'gemini'`) |
| `LeapServer` | `server/server.py` | Orchestrates PTY, socket, queue, metadata |
| `LeapClient` | `client/client.py` | Interactive client with image support |
| `SocketClient` | `client/socket_client.py` | Client-side socket communication (shared `_send_request`) |
| `MonitorWindow` | `monitor/app.py` | PyQt5 GUI core window (uses mixins for methods) |
| `ServerLauncher` | `monitor/server_launcher.py` | PR server clone/force-align/start flow |
| `GitLabProvider` | `monitor/pr_tracking/gitlab_provider.py` | GitLab PR thread tracking + user notifications |
| `GitHubProvider` | `monitor/pr_tracking/github_provider.py` | GitHub PR thread tracking + user notifications |
| `ActionsMenuMixin` | `monitor/_mixins/actions_menu_mixin.py` | Git menu (branch col) + Path menu (Open Terminal/IDE) |
| `GitChangesDialog` | `monitor/dialogs/git_changes_dialog.py` | Git diff viewer (local, commit, vs main) |
| `CommitListDialog` | `monitor/dialogs/git_changes_dialog.py` | Commit picker for diff comparison |
| `BranchPickerDialog` | `monitor/dialogs/branch_picker_dialog.py` | Branch picker for difftool comparison |
| `QueueEditDialog` | `monitor/dialogs/queue_edit_dialog.py` | View/edit queued messages for a session |
| `NotesDialog` | `monitor/dialogs/notes_dialog.py` | Notes with folder tree, search (title+content), text/checklist, DnD reorder, save as preset, run in session |
| `ImageTextEdit` | `monitor/ui/image_text_edit.py` | QTextEdit with clipboard image paste → `[Image #N]` placeholders |
| `SendMessageDialog` | `monitor/ui/image_text_edit.py` | Message dialog with image paste (replaces QInputDialog) |
| `ColorPickerPopup` | `monitor/ui/table_helpers.py` | Row color picker popup (grid of swatches + clear) |
| `DockBadge` | `monitor/ui/dock_badge.py` | Dock icon badge overlay + notification event detection |
| `Theme` / `current_theme()` | `monitor/themes.py` | Theme dataclass + manager API (9 built-in themes) |
| `ensure_contrast()` | `monitor/themes.py` | WCAG contrast safety-net (returns black/white if ratio < 4.5:1) |
| `SlackBot` | `slack/bot.py` | Main Slack bot (Socket Mode + event handlers) |
| `OutputCapture` | `slack/output_capture.py` | Read hook response from signal file, write .last_response |
| `send_socket_request()` | `utils/socket_utils.py` | Shared Unix socket send/recv utility |
| `resolve_scm_token()` | `monitor/pr_tracking/config.py` | Resolve token from config (supports env var mode) |
| `parse_pr_url()` | `monitor/pr_tracking/git_utils.py` | Parse GitLab/GitHub PR URLs |
| `send_to_leap_session()` | `monitor/leap_sender.py` | Send message to Leap session (prepends PR context) |
| `configure_hooks.py` | `scripts/configure_hooks.py` | Unified hook config (iterates providers, calls `provider.configure_hooks()`) |

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
| Monitor prefs | `.storage/monitor_prefs.json` (includes `row_order`, `aliases`) |
| Notification seen state | `.storage/notification_seen.json` |
| PR context preset selection | `.storage/leap_selected_preset` |
| Message bundle preset selection | `.storage/leap_selected_direct_preset` |
| Preset definitions | `.storage/leap_presets.json` |
| Queue images | `.storage/queue_images/<hash>.png` (MD5-deduped, cleaned on server startup) |
| Note images | `.storage/note_images/<hash>.png` (MD5-deduped, persistent) |
| Signal file | `.storage/sockets/<tag>.signal` |
| Last response (Slack) | `.storage/sockets/<tag>.last_response` |
| Slack config | `.storage/slack/config.json` |
| Saved messages | `.storage/saved_messages.json` |
| Slack sessions | `.storage/slack/sessions.json` |

## Server Queue Shortcut

Type `^^` in the server terminal to queue a message. Double-caret (`^^`) activates capture mode — characters are hidden from the CLI and shown in a `[Leap Q]` prompt on the input line. Works at any point: type `^^msg` to start fresh, or type `hello` then `^^` to convert already-typed text into a queued message. Press Enter to queue, Escape or Ctrl+C to cancel.

**Saved messages**: Type `^^` inside capture mode to save the current message to history and clear the buffer. Browse saved messages with arrow up/down. History persists across sessions in `.storage/saved_messages.json` (max 100 entries, shared across all CLIs/sessions). Editing a recalled message does not modify the saved history — only explicit `^^` save does.

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

- **New CLI provider** → See the `.claude/skills/add-cli-provider.md` skill for a comprehensive step-by-step guide. Key files: create `cli_providers/<name>.py`, register in `registry.py`, implement `configure_hooks()`. The CLI selector, monitor table, ASCII banner, and shell flags are all dynamic and require no changes.
- **Utils** → `src/leap/utils/`
- **Server** → `src/leap/server/`, update `LeapServer`
- **Client** → `src/leap/client/`, update `LeapClient`
- **Monitor** → `src/leap/monitor/`, update `MonitorWindow`
- **Socket communication** → Use `send_socket_request()` from `utils/socket_utils.py` for any new code that needs to talk to a Leap server via Unix socket. Do not duplicate the connect/send/recv pattern. Incoming messages are capped at `MAX_MESSAGE_SIZE` (1 MB) in `socket_handler.py`; larger payloads are rejected.
- **New third-party dependencies** → Add to `pyproject.toml` under the appropriate group: `[tool.poetry.dependencies]` for core, `[tool.poetry.group.monitor.dependencies]` for GUI-only deps. Run `poetry lock && poetry install` after. All imports must be at module top level (no inline imports except optional deps).
- **New dialogs** → All new resizable dialogs (except simple warning/error/info popups) must save/restore their size using `load_dialog_geometry(key)` / `save_dialog_geometry(key, w, h)` from `monitor/pr_tracking/config.py`. Call `load_dialog_geometry()` in `__init__` to restore. For persistence: if the dialog closes via `accept()`/`reject()`, save in `done()`. If it closes via `close()` or the X button, save in `closeEvent()` instead — `done()` is **not** called for `close()`/X.
- **New `.storage` subdirectories** → If you add a new subdirectory under `.storage/`, you **must** update three places:
  1. Add the constant in `utils/constants.py` (next to `QUEUE_DIR`, `SOCKET_DIR`, `HISTORY_DIR`)
  2. Add a `.mkdir()` call in `ensure_storage_dirs()` in `utils/constants.py`
  3. Add the path to the `ensure-storage` target in `Makefile`
- **Theming** → Use `current_theme()` from `monitor/themes.py` to access colors. Never hardcode colors in monitor code — use theme properties (e.g. `t.accent_green`, `t.text_primary`). Theme colors are applied via `QPalette` (preserves native macOS widget rendering) + minimal QSS. Cell button styles use `close_btn_style()` / `active_btn_style()` / `menu_btn_style()` from `table_helpers.py`. Theme persists as `"theme"` in `monitor_prefs.json` (default: `"Midnight"`). Nine built-in themes: Leap, Amber, Midnight, Cosmos, Ocean, Monokai, Nord, Solarized Dark, Dawn.
- **New assets (images, icons, themed variants)** → Any new asset file in `assets/` that the monitor uses at runtime **must** also be added to `DATA_FILES` in `setup.py`. The py2app bundle only includes explicitly listed files — assets missing from `setup.py` will work in `make run-monitor` (dev mode) but silently fail in the installed app. Logo text variants use `glob('assets/leap-text*.png')` so new theme logos are auto-included, but other new assets need manual addition.

## Testing

```bash
poetry run pytest tests/ -v     # Run all tests
```

- Tests use `pytest` (dev dependency, `poetry install --with dev`)
- `ClaudeStateTracker` uses an injectable `clock` parameter — tests pass a fake clock (`lambda: t[0]`) for deterministic time control
- Use `tmp_path` fixture for signal files
- Test file naming: `tests/test_<module>.py`

## Code Conventions

- **Type hints**: 100% coverage on all function signatures and return types. Use `Optional[X]` (not `X | None`) for consistency.
- **Imports**: All imports at module top level. No inline imports except for optional dependencies (`prompt_toolkit`, `gitlab`).
- **Client commands**: Each command handler is extracted into a private `_handle_*` method on `LeapClient`. The `_process_command` dispatcher delegates to these handlers.
- **Socket pattern**: `SocketClient._send_request()` is the single source of truth for client→server socket communication. `send_socket_request()` in `utils/socket_utils.py` is the lightweight variant for monitor/session_manager code that doesn't need rate-limited error reporting.

## SCM Polling & PR Tracking

The monitor polls GitLab/GitHub for PR status updates and user notifications. Key timeouts:

- **GitLab client timeout**: 15s per HTTP request
- **Poll cycle timeout**: 30s for all `ThreadPoolExecutor` futures
- **Stuck-poll safeguard**: Force-resets `_scm_polling` after 60s
- **Poll interval**: Configurable via `poll_interval` in config (default: 30s)

Polling flow: `_scm_poll_timer` → `_start_scm_poll()` → `SCMPollerWorker` (QThread) → `get_pr_status()` per session → `_on_scm_results()` → `_update_pr_column()`.

### Sending Threads to Leap

Right-click PR status label for send modes: individual threads, combined into one message, or filtered to `/leap` commands only. Both share `CollectThreadsWorker` (Phase 1), then diverge: `SendThreadsWorker` (one-by-one) or `SendThreadsCombinedWorker` (concatenated). All modes acknowledge threads on SCM side after send.

### /leap Auto-Fetch

"Auto '/leap' fetch" checkbox: when ON, `SCMPollerWorker` auto-scans for `/leap` commands each poll cycle. A `/leap` comment does **not** count as a user response — only the bot ack (`[Leap bot] on it!`) marks a thread as handled. Setting persisted as `auto_fetch_leap` in monitor prefs.

### Environment Variable Token Mode

SCM tokens support two modes: `token_mode: "direct"` (stored in config) or `"env_var"` (resolved from `os.environ`). Resolution via `resolve_scm_token()` in `config.py`. On startup, env var tokens are validated — invalid ones disable the provider until re-tested via the setup dialog. PR-pinned rows survive provider disconnection (they retain `remote_project_path` in `pinned_sessions.json`).

### User Notifications

Per-provider enable/disable via setup dialog. Polls `get_user_notifications()` each cycle. Seen IDs deduplicated via `.storage/notification_seen.json`. First-run seeds all existing notifications as seen. 403 errors auto-disable notifications for that provider.

### Persistent Rows & Pinned Sessions

Rows persist via `pinned_sessions.json`. Key rules:
- Every active session is auto-pinned on discovery
- Row survives if it has a running server OR `remote_project_path` (PR-pinned) OR active PR tracking
- Dead rows without PR info are auto-removed
- PR auto-reconnects on monitor restart for rows with `pr_tracked: True`
- `_deleted_tags` set prevents auto-refresh from re-pinning just-deleted rows

### Add Row (+ Button)

Two options: **From Git URL** (PR URLs or plain project URLs → parse, pin, clone/track) and **From Local Path** (clone to repos dir or open directly). Tag validation via shared `_ask_tag()` helper.

### New Change Indicator

A fire icon (🔥) appears on the far right of the Status and PR columns when the value recently changed. Controlled by `new_status_seconds` in monitor prefs (default: 60, 0 = disabled). Click the indicator to dismiss it; dismissal resets when the value changes again.

- **Status column**: Never shown for `running` or `interrupted` states. Tracked in `_state_changed_at` and `_dismissed_new_status` on `MonitorWindow`.
- **PR column**: Triggers on changes to PR state, unresponded count, approval status, or who approved. First-time discovery is seeded with epoch 0 (no fire on startup). Tracked in `_pr_changed_at` and `_dismissed_pr_new_status` on `MonitorWindow`.

### Branch Mismatch & Server Startup Validation

- **Runtime mismatch**: Monitor shows `⚠ Server` in orange when live branch differs from expected PR branch
- **Startup validation** (`_validate_pinned_session()` in `server.py`): Checks repo match, branch match, behind-remote status. Fails 1-3 block startup; ahead/dirty is a warning only. Skipped for non-PR-pinned rows

### Row Ordering (Drag-and-Drop)

Rows are ordered by insertion time (not alphabetical). Users can drag any cell to reorder rows; the order is persisted as a `row_order` list in `monitor_prefs.json`. New sessions are appended at the end.

- **Drag detection**: App-level event filter (`eventFilter` in `app.py`) intercepts `MouseButtonPress`/`MouseMove` on cell widgets to initiate a `QDrag`
- **Drop indicator**: A 2px theme-colored line shows the drop position during drag
- **Auto-refresh paused** during drag (`timer.stop()` / `timer.start()`) to prevent table rebuilds from interrupting the gesture
- **Cleanup**: When rows are deleted, `_remove_from_row_order()` in `session_mixin.py` removes the tag from the persisted list

### Row Colors

Per-row background colors selectable via a droplet icon button in the Tag column. Persisted as `row_colors: {tag: "#hex"}` in `monitor_prefs.json`.

- **Picker**: `ColorPickerPopup` (in `table_helpers.py`) — 4x4 grid of muted color swatches + Clear button, opened via `_show_color_picker()` in `table_builder_mixin.py`
- **Rendering**: `SeparatorDelegate.paint()` reads `_row_colors` / `_row_tags` table properties and `fillRect`s the row background before the hover overlay
- **Text contrast**: `ensure_contrast()` adjusts text foreground against the row color for both `QTableWidgetItem` cells and child `QLabel`s in widget cells (skips `PulsingLabel`/`IndicatorLabel`)
- **Cleanup**: `_remove_pinned_session()` in `session_mixin.py` deletes the color entry when a row is removed

### Tag Aliases

Display aliases for tags, set via right-click context menu on the Tag column. Persisted as `aliases: {tag: "display name"}` in `monitor_prefs.json`.

- **Display**: Aliased tags show the alias in *italic*; the real tag is unchanged everywhere else (files, sockets, server, client)
- **Tooltip**: Aliased tags always show "Alias: X / Tag: Y" (regardless of tooltip setting). Regular tags show on hover when truncated or when "Show hover explanations" is on
- **Context menu**: Right-click tag cell → "Set alias" / "Rename alias" / "Remove alias" via `_show_tag_context_menu()` in `table_builder_mixin.py`
- **Cleanup**: `_remove_pinned_session()` and `_merge_sessions()` in `session_mixin.py` delete the alias entry when a row is removed

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

## Troubleshooting

**"Another client already connected"** → `rm .storage/sockets/<tag>.client.lock`

**Stale sockets** → `leap-cleanup`

## Make Commands

```bash
make install           # Install core + configure shell
make install-monitor   # Build and install GUI app
make install-slack-app # Install Slack integration + setup wizard
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

When the user asks to commit and push, **before committing**:

1. **Review CLAUDE.md** — Check that it reflects the current codebase. Update any outdated sections (project structure, key classes, features, conventions). Keep it detailed — this is the developer reference.
2. **Review README.md** — Check that it reflects user-facing changes (new features, commands, UI changes). Keep it **concise** — users see this on GitLab. Don't bloat it with implementation details.
3. Only update these files if something actually changed that affects them. Don't touch them for minor internal refactors.
