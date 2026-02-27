# ClaudeQ

PTY-based client-server system for managing Claude CLI sessions with message queueing, image support, and native IDE scrolling.

## Quick Start

```bash
make install                # Install core
make install-monitor        # Install GUI (optional)
source ~/.zshrc             # Reload shell

cq mytag                    # Terminal 1: Start server
cq mytag                    # Terminal 2: Connect client
```

## Project Structure

```
src/
├── scripts/                     # Entry point scripts
│   ├── claudeq-main.sh          # Main launcher (called by 'cq' alias)
│   ├── claudeq-cleanup.sh       # Dead session cleanup
│   ├── claudeq-server.py        # Thin launcher → ClaudeQServer
│   ├── claudeq-client.py        # Thin launcher → ClaudeQClient
│   ├── claudeq-monitor.py       # Thin launcher → MonitorWindow
│   ├── claudeq-slack.py         # Thin launcher → SlackBot
│   ├── claudeq_monitor_launcher.py  # py2app entry point
│   ├── setup-slack-app.sh       # Interactive Slack app setup wizard
│   ├── configure_jetbrains_xml.py   # JetBrains IDE auto-configuration
│   ├── configure_claude_hooks.py    # Merge ClaudeQ hooks into ~/.claude/settings.json
│   └── claudeq-hook.sh             # Claude Code hook script (writes state to signal file)
│
└── claudeq/                     # Main Python package
    ├── __init__.py              # Version, exports
    ├── main.py                  # Package entry point
    │
    ├── utils/                   # Shared utilities
    │   ├── constants.py         # QUEUE_DIR, SOCKET_DIR, timing, colors, is_valid_tag()
    │   ├── terminal.py          # Terminal title, banner
    │   ├── ide_detection.py     # IDE detection, git branch
    │   └── socket_utils.py     # Shared Unix socket send/recv helper
    │
    ├── server/                  # PTY Server
    │   ├── server.py            # ClaudeQServer - main orchestrator
    │   ├── pty_handler.py       # Claude CLI PTY (pexpect)
    │   ├── socket_handler.py    # Unix socket server
    │   ├── queue_manager.py     # Message queue persistence
    │   └── metadata.py          # Session metadata (IDE, project, branch)
    │
    ├── client/                  # Interactive Client
    │   ├── client.py            # ClaudeQClient - main class
    │   ├── socket_client.py     # Unix socket client
    │   ├── input_handler.py     # Prompt toolkit / readline
    │   └── image_handler.py     # Clipboard image handling
    │
    ├── monitor/                 # GUI Monitor (PyQt5)
    │   ├── app.py               # MonitorWindow (core window + UI init + lifecycle)
    │   ├── server_launcher.py   # MR server clone/checkout/start flow
    │   ├── session_manager.py   # Session discovery + read_client_pid()
    │   ├── scm_polling.py       # SCM poller + background workers
    │   ├── cq_sender.py         # Socket sender for /cq commands + message bundles
    │   ├── navigation.py        # IDE terminal navigation
    │   ├── monitor_utils.py     # Utilities (icon finder, lock removal)
    │   │
    │   ├── _mixins/             # MonitorWindow mixin classes
    │   │   ├── actions_menu_mixin.py  # Git menu (branch col) + Path menu (Open Terminal/IDE)
    │   │   ├── scm_config_mixin.py    # SCM provider init, setup dialogs, toggles
    │   │   ├── session_mixin.py       # Session merge, navigate, close, delete
    │   │   ├── mr_tracking_mixin.py   # MR tracking, polling, thread send, add-row
    │   │   ├── mr_display_mixin.py    # MR column styling, dock badge, banners
    │   │   ├── notifications_mixin.py # User notification handling
    │   │   └── table_builder_mixin.py # Table build, refresh, settings
    │   │
    │   ├── dialogs/             # Dialog windows
    │   │   ├── git_changes_dialog.py  # Git diff viewer (local, commit, vs main)
    │   │   ├── settings_dialog.py     # Settings (terminal, repos dir, diff tool, cleanup)
    │   │   ├── notifications_dialog.py # Per-type notification config (dock/banner)
    │   │   ├── scm_setup_dialog.py    # Abstract SCM setup base dialog (URL hidden behind "Self-hosted" toggle)
    │   │   ├── gitlab_setup_dialog.py # GitLab connection dialog
    │   │   ├── github_setup_dialog.py # GitHub connection dialog
    │   │   ├── scm_template_dialog.py # Preset editor dialog (MR context + message bundles)
    │   │   └── add_local_dialog.py    # Add session from local path dialog
    │   │
    │   ├── ui/                  # UI components
    │   │   ├── ui_widgets.py    # PulsingLabel, IndicatorLabel
    │   │   ├── dock_badge.py    # Dock icon badge overlay + notification event detection
    │   │   ├── status_log.py    # Status log history (in-memory + dialog)
    │   │   └── table_helpers.py # Qt helper widgets (separators, tooltip overrides)
    │   │
    │   ├── mr_tracking/         # MR tracking subsystem
    │   │   ├── base.py          # Abstract SCMProvider, MRState, MRStatus, MRDetails
    │   │   ├── config.py        # GitLab/monitor prefs + pinned sessions persistence
    │   │   ├── gitlab_provider.py # GitLab API implementation
    │   │   ├── github_provider.py # GitHub API implementation
    │   │   ├── git_utils.py     # Git remote URL parsing + MR URL parsing
    │   │   └── cq_command.py    # /cq command data model + formatting
    │   └── resources/
    │       └── activate_terminal.groovy  # JetBrains script
    │
    ├── slack/                   # Slack Integration
    │   ├── __init__.py          # Package init
    │   ├── bot.py               # SlackBot main class (Socket Mode)
    │   ├── config.py            # Slack config + session persistence
    │   ├── output_capture.py    # Capture hook response, write .last_response for Slack bot
    │   ├── output_watcher.py    # Poll .last_response files → post to Slack
    │   └── message_router.py    # Route Slack messages → CQ sessions
    │
    └── vscode-extension/        # VS Code Extension
        ├── package.json         # Extension metadata
        ├── extension.js         # Terminal selector logic
        └── README.md            # Extension documentation

tests/
├── __init__.py
└── test_state_tracker.py        # ClaudeStateTracker state machine tests

assets/
├── claudeq-icon.png             # Source icon (1024x1024)
└── claudeq-icon.icns            # macOS icon bundle
```

## Key Classes

| Class / Function | File | Purpose |
|------------------|------|---------|
| `ClaudeQServer` | `server/server.py` | Orchestrates PTY, socket, queue, metadata |
| `ClaudeQClient` | `client/client.py` | Interactive client with image support |
| `SocketClient` | `client/socket_client.py` | Client-side socket communication (shared `_send_request`) |
| `MonitorWindow` | `monitor/app.py` | PyQt5 GUI core window (uses mixins for methods) |
| `ServerLauncher` | `monitor/server_launcher.py` | MR server clone/force-align/start flow |
| `GitLabProvider` | `monitor/mr_tracking/gitlab_provider.py` | GitLab MR thread tracking + user notifications |
| `GitHubProvider` | `monitor/mr_tracking/github_provider.py` | GitHub PR thread tracking + user notifications |
| `ActionsMenuMixin` | `monitor/_mixins/actions_menu_mixin.py` | Git menu (branch col) + Path menu (Open Terminal/IDE) |
| `GitChangesDialog` | `monitor/dialogs/git_changes_dialog.py` | Git diff viewer (local, commit, vs main) |
| `CommitListDialog` | `monitor/dialogs/git_changes_dialog.py` | Commit picker for diff comparison |
| `DockBadge` | `monitor/ui/dock_badge.py` | Dock icon badge overlay + notification event detection |
| `SlackBot` | `slack/bot.py` | Main Slack bot (Socket Mode + event handlers) |
| `OutputCapture` | `slack/output_capture.py` | Read hook response from signal file, write .last_response |
| `send_socket_request()` | `utils/socket_utils.py` | Shared Unix socket send/recv utility |
| `resolve_scm_token()` | `monitor/mr_tracking/config.py` | Resolve token from config (supports env var mode) |
| `parse_mr_url()` | `monitor/mr_tracking/git_utils.py` | Parse GitLab/GitHub MR/PR URLs |
| `send_to_cq_session()` | `monitor/cq_sender.py` | Send message to CQ session (prepends MR context) |

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
| Monitor prefs | `.storage/monitor_prefs.json` |
| Notification seen state | `.storage/notification_seen.json` |
| MR context preset selection | `.storage/cq_selected_template` |
| Message bundle preset selection | `.storage/cq_selected_direct_template` |
| Preset definitions | `.storage/cq_templates.json` |
| Signal file | `.storage/sockets/<tag>.signal` |
| Last response (Slack) | `.storage/sockets/<tag>.last_response` |
| Slack config | `.storage/slack/config.json` |
| Slack sessions | `.storage/slack/sessions.json` |

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

- **Utils** → `src/claudeq/utils/`
- **Server** → `src/claudeq/server/`, update `ClaudeQServer`
- **Client** → `src/claudeq/client/`, update `ClaudeQClient`
- **Monitor** → `src/claudeq/monitor/`, update `MonitorWindow`
- **Socket communication** → Use `send_socket_request()` from `utils/socket_utils.py` for any new code that needs to talk to a CQ server via Unix socket. Do not duplicate the connect/send/recv pattern. Incoming messages are capped at `MAX_MESSAGE_SIZE` (1 MB) in `socket_handler.py`; larger payloads are rejected.
- **New third-party dependencies** → Add to `pyproject.toml` under the appropriate group: `[tool.poetry.dependencies]` for core, `[tool.poetry.group.monitor.dependencies]` for GUI-only deps. Run `poetry lock && poetry install` after. All imports must be at module top level (no inline imports except optional deps).
- **New `.storage` subdirectories** → If you add a new subdirectory under `.storage/`, you **must** update three places:
  1. Add the constant in `utils/constants.py` (next to `QUEUE_DIR`, `SOCKET_DIR`, `HISTORY_DIR`)
  2. Add a `.mkdir()` call in `ensure_storage_dirs()` in `utils/constants.py`
  3. Add the path to the `ensure-storage` target in `Makefile`

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
- **Client commands**: Each command handler is extracted into a private `_handle_*` method on `ClaudeQClient`. The `_process_command` dispatcher delegates to these handlers.
- **Socket pattern**: `SocketClient._send_request()` is the single source of truth for client→server socket communication. `send_socket_request()` in `utils/socket_utils.py` is the lightweight variant for monitor/session_manager code that doesn't need rate-limited error reporting.

## SCM Polling & MR Tracking

The monitor polls GitLab/GitHub for MR status updates and user notifications. Key timeouts:

- **GitLab client timeout**: 15s per HTTP request
- **Poll cycle timeout**: 30s for all `ThreadPoolExecutor` futures
- **Stuck-poll safeguard**: Force-resets `_scm_polling` after 60s
- **Poll interval**: Configurable via `poll_interval` in config (default: 30s)

Polling flow: `_scm_poll_timer` → `_start_scm_poll()` → `SCMPollerWorker` (QThread) → `get_mr_status()` per session → `_on_scm_results()` → `_update_mr_column()`.

### Sending Threads to CQ

Right-click MR status label for send modes: individual threads, combined into one message, or filtered to `/cq` commands only. Both share `CollectThreadsWorker` (Phase 1), then diverge: `SendThreadsWorker` (one-by-one) or `SendThreadsCombinedWorker` (concatenated). All modes acknowledge threads on SCM side after send.

### /cq Auto-Fetch

"Auto '/cq' fetch" checkbox: when ON, `SCMPollerWorker` auto-scans for `/cq` commands each poll cycle. A `/cq` comment does **not** count as a user response — only the bot ack (`[ClaudeQ bot] on it!`) marks a thread as handled. Setting persisted as `auto_fetch_cq` in monitor prefs.

### Environment Variable Token Mode

SCM tokens support two modes: `token_mode: "direct"` (stored in config) or `"env_var"` (resolved from `os.environ`). Resolution via `resolve_scm_token()` in `config.py`. On startup, env var tokens are validated — invalid ones disable the provider until re-tested via the setup dialog. MR-pinned rows survive provider disconnection (they retain `remote_project_path` in `pinned_sessions.json`).

### User Notifications

Per-provider enable/disable via setup dialog. Polls `get_user_notifications()` each cycle. Seen IDs deduplicated via `.storage/notification_seen.json`. First-run seeds all existing notifications as seen. 403 errors auto-disable notifications for that provider.

### Persistent Rows & Pinned Sessions

Rows persist via `pinned_sessions.json`. Key rules:
- Every active session is auto-pinned on discovery
- Row survives if it has a running server OR `remote_project_path` (MR-pinned) OR active MR tracking
- Dead rows without MR info are auto-removed
- MR auto-reconnects on monitor restart for rows with `mr_tracked: True`
- `_deleted_tags` set prevents auto-refresh from re-pinning just-deleted rows

### Add Row (+ Button)

Two options: **From Git URL** (MR/PR URLs or plain project URLs → parse, pin, clone/track) and **From Local Path** (clone to repos dir or open directly). Tag validation via shared `_ask_tag()` helper.

### Branch Mismatch & Server Startup Validation

- **Runtime mismatch**: Monitor shows `⚠ Server` in orange when live branch differs from expected MR branch
- **Startup validation** (`_validate_pinned_session()` in `server.py`): Checks repo match, branch match, behind-remote status. Fails 1-3 block startup; ahead/dirty is a warning only. Skipped for non-MR-pinned rows

## Slack Integration

Optional Slack app for bidirectional CQ ↔ Slack communication. Each session gets a thread in the user's DM.

```bash
make install-slack-app   # Install deps + guided setup wizard
cq --slack               # Start the bot daemon
```

**Data flow**: Claude finishes → hook reads transcript JSONL → writes to signal file → `OutputCapture` writes `.last_response` → `OutputWatcher` posts to Slack. Replies: Slack thread → `MessageRouter` → queue or direct message via socket.

Bot can also be started/stopped from the monitor's **Slack Bot** button. Dependencies: `slack-bolt`, `slack-sdk` (optional poetry group).

## IDE Setup

### JetBrains (PyCharm, IntelliJ, etc.)
**Automatically configured during `make install`** — Terminal Engine set to Classic, "Show application title" enabled. Restart IDEs after installation.

### VS Code
**Automatically configured during `make install`** — Terminal selector extension auto-installed, tabs show numbered labels.

## Troubleshooting

**"Another client already connected"** → `rm .storage/sockets/<tag>.client.lock`

**Stale sockets** → `cq-cleanup`

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

## Commit & Push Checklist

When the user asks to commit and push, **before committing**:

1. **Review CLAUDE.md** — Check that it reflects the current codebase. Update any outdated sections (project structure, key classes, features, conventions). Keep it detailed — this is the developer reference.
2. **Review README.md** — Check that it reflects user-facing changes (new features, commands, UI changes). Keep it **concise** — users see this on GitLab. Don't bloat it with implementation details.
3. Only update these files if something actually changed that affects them. Don't touch them for minor internal refactors.
