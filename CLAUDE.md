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
│   ├── claudeq_monitor_launcher.py  # py2app entry point
│   └── configure_jetbrains_xml.py   # JetBrains IDE auto-configuration
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
    │   ├── app.py               # MonitorWindow
    │   ├── server_launcher.py   # MR server clone/checkout/start flow
    │   ├── session_manager.py   # Session discovery + read_client_pid()
    │   ├── scm_polling.py       # SCM poller + background workers
    │   ├── cq_sender.py         # Socket sender for /cq commands
    │   ├── navigation.py        # IDE terminal navigation
    │   ├── monitor_utils.py     # Utilities (icon finder, lock removal)
    │   │
    │   ├── dialogs/             # Dialog windows
    │   │   ├── settings_dialog.py     # Settings (terminal, repos dir, cleanup)
    │   │   ├── scm_setup_dialog.py    # Abstract SCM setup base dialog
    │   │   ├── gitlab_setup_dialog.py # GitLab connection dialog
    │   │   ├── github_setup_dialog.py # GitHub connection dialog
    │   │   └── scm_context_dialog.py  # Context editor dialog (named presets)
    │   │
    │   ├── ui/                  # UI components
    │   │   ├── ui_widgets.py    # PulsingLabel, IndicatorLabel
    │   │   ├── dock_badge.py    # Dock icon badge overlay (notification counter)
    │   │   └── status_log.py    # Status log history (in-memory + dialog)
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
    └── vscode-extension/        # VS Code Extension
        ├── package.json         # Extension metadata
        ├── extension.js         # Terminal selector logic
        └── README.md            # Extension documentation

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
| `MonitorWindow` | `monitor/app.py` | PyQt5 GUI for session management |
| `ContextEditorDialog` | `monitor/dialogs/scm_context_dialog.py` | Context preset editor dialog |
| `ServerLauncher` | `monitor/server_launcher.py` | MR server clone/checkout/start flow |
| `StatusLog` | `monitor/ui/status_log.py` | In-memory status message log + viewer dialog |
| `SettingsDialog` | `monitor/dialogs/settings_dialog.py` | Settings: terminal, repos dir, cleanup unused repos |
| `GitLabProvider` | `monitor/mr_tracking/gitlab_provider.py` | GitLab MR thread tracking |
| `GitHubProvider` | `monitor/mr_tracking/github_provider.py` | GitHub PR thread tracking |
| `DockBadge` | `monitor/ui/dock_badge.py` | Dock icon badge overlay (MR + session status changes) |
| `send_socket_request()` | `utils/socket_utils.py` | Shared Unix socket send/recv utility |
| `is_valid_tag()` | `utils/constants.py` | Shared tag validation (alphanumeric + hyphens + underscores) |
| `parse_mr_url()` | `monitor/mr_tracking/git_utils.py` | Parse GitLab/GitHub MR/PR URLs |

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
| Pinned sessions | `.storage/pinned_sessions.json` |
| Monitor prefs | `.storage/monitor_prefs.json` |

## File Cleanup & Lifecycle

ClaudeQ has multiple cleanup mechanisms. This table shows **exactly** which function cleans which files and when it runs:

### Cleanup Functions

| Function Name | Location | Files Cleaned | Server Up | Server Down | Client Up | Client Down | Manual `cq-cleanup` | Monitor Up | Monitor Down |
|--------------|----------|---------------|-----------|-------------|-----------|-------------|---------------------|------------|--------------|
| `ClaudeQServer.cleanup()` | `server/server.py:434` | `.sock`<br>`.meta`<br>`.queue` (if empty)<br>PTY process | | ✅ | | | | | ✅ (via shutdown msg) |
| `ClaudeQClient._cleanup_lock()` | `client/client.py:103` | `.client.lock` | | | | ✅ | | | |
| `ClaudeQClient._cleanup_temp_images()` | `client/client.py:117` | `/tmp/*.png` (temp images) | | | | ✅ | | | |
| `ClaudeQServer._cleanup_old_history_files()` | `server/server.py:262` | `.history` (older than TTL) | ✅ | | | | | | |
| `cleanup_dead_sockets()` | `claudeq-main.sh:148` | `.sock` (dead)<br>`.queue` (dead)<br>`.meta` (dead)<br>`.client.lock` (dead) | ✅ (background) | | | | | | |
| `cq-cleanup` script | `claudeq-cleanup.sh` | `.sock` (dead)<br>`.queue` (dead)<br>`.meta` (dead)<br>`.client.lock` (dead) | | | | | ✅ | | |

**Legend:**
- ✅ = Cleanup runs at this event
- (dead) = Only cleans files for sessions with no running server process
- (if empty) = Conditional cleanup

### File Lifecycle Reference

| File | Created When | Cleaned By | Persistence |
|------|--------------|------------|-------------|
| `.sock` | Server starts | Server exit, dead session cleanup | Temporary |
| `.meta` | Server starts | Server exit, dead session cleanup | Temporary |
| `.queue` | First message queued | Server exit (if empty), dead session cleanup, user discard | Until empty or discarded |
| `.history` | First user input | History TTL cleanup on server startup | Deleted after `history_ttl_days` (default: 3) |
| `.client.lock` | Client connects | Client exit, dead session cleanup | Temporary |
| `/tmp/*.png` | `!ip` command | Client exit | Temporary |

### Settings Configuration

Edit `.storage/settings.json` to customize:

```json
{
  "show_auto_sent_notifications": true,  // Show "🤖 Auto-sent" messages
  "history_ttl_days": 3                  // Delete .history files older than N days
}
```

**Common TTL values:**
- `1` = Delete after 1 day (aggressive)
- `3` = Default (balanced)
- `7` = Keep for a week
- `30` = Keep for a month

### Queue Prompt on Server Startup

When server starts with existing queued messages:

```
⚠️  Found 3 unsent messages from previous session:

  [0] Fix the bug in server.py
  [1] Add tests for auth module
  [2] Deploy to staging...

Load these messages? [Y/n/d] (Y=load, n=discard, d=show full):
```

- `Y` (default) → Load and auto-send when ready
- `n` → Permanently discard all messages
- `d` → Show full messages before deciding

### Important Notes

**SIGKILL (kill -9) behavior:**
- Bypasses all cleanup functions (no `atexit` hooks run)
- Files persist until next dead session cleanup
- Use `cq-cleanup` to manually clean up

**Monitor shutdown:**
- Sends `shutdown` socket message to server
- Triggers `ClaudeQServer.cleanup()`
- Falls back to `SIGTERM` if socket fails

**Data loss warning:**
- `cq-cleanup` deletes `.queue` files even with pending messages
- Always check queue first: `cq <tag>` → `!list`

## Client Commands

| Command | Action |
|---------|--------|
| `!h` or `!help` | Show help |
| `<message>` | Queue message (auto-sends when ready) |
| `!ip <msg>` or `!imagepaste <msg>` | Queue with clipboard image |
| `!d <msg>` or `!direct <msg>` | Send directly (bypass queue) |
| `!e <index>` or `!edit <index>` | Edit queued message by index (0=first) |
| `!l` or `!list` | Show queue |
| `!c` or `!clear` | Clear queue |
| `!f` or `!force` | Force-send next queued message |
| `!x` or `!quit` (`Ctrl+D`) | Exit client |

### Message Editing

Each queued message gets a unique 6-character ID (e.g., `a1b2c3`). When listing the queue with `!l`, messages display as:

```
[0] <a1b2c3> Fix the bug in server.py
[1] <d4e5f6> Add tests for auth module
```

**Edit workflow:**
1. Run `!l` to see queue with indices and IDs
2. Run `!e <index>` (e.g., `!e 0`) to edit a message
3. System shows the message ID and content
4. Enter new message (or Ctrl+D to cancel)
5. If the message was already sent (ID not found), you'll see "too late" error

**Key features:**
- Messages tracked by ID, not position
- Safe against race conditions (queue changing during edit)
- Works even if message moves position in queue
- Backward compatible with old queue files (auto-migrates)

### Auto-Sent Notifications

Control whether the client displays notifications when the server auto-sends messages:

```
!auto-sent on     # Enable notifications (default)
!auto-sent off    # Disable notifications
!asm on/off       # Short version
```

When enabled, you'll see: `🤖 Server auto-sent: Your message... (2 remaining)`
When disabled, messages send silently in the background.

## Architecture Flow

```
cq mytag
    ↓
~/.zshrc → claudeq() function
    ↓
src/scripts/claudeq-main.sh
    ↓
[Socket exists?] → Yes → ClaudeQClient
    ↓ No
ClaudeQServer → spawns Claude CLI via PTY
    ↓
Listens on Unix socket for client messages
```

## Adding Features

- **Utils** → `src/claudeq/utils/`
- **Server** → `src/claudeq/server/`, update `ClaudeQServer`
- **Client** → `src/claudeq/client/`, update `ClaudeQClient`
- **Monitor** → `src/claudeq/monitor/`, update `MonitorWindow`
- **Socket communication** → Use `send_socket_request()` from `utils/socket_utils.py` for any new code that needs to talk to a CQ server via Unix socket. Do not duplicate the connect/send/recv pattern.

## Code Conventions

- **Type hints**: 100% coverage on all function signatures and return types. Use `Optional[X]` (not `X | None`) for consistency.
- **Imports**: All imports at module top level. No inline imports except for optional dependencies (`prompt_toolkit`, `gitlab`).
- **Client commands**: Each command handler is extracted into a private `_handle_*` method on `ClaudeQClient`. The `_process_command` dispatcher delegates to these handlers.
- **Socket pattern**: `SocketClient._send_request()` is the single source of truth for client→server socket communication. `send_socket_request()` in `utils/socket_utils.py` is the lightweight variant for monitor/session_manager code that doesn't need rate-limited error reporting.

## SCM Polling (GitLab MR Tracking)

The monitor polls GitLab for MR status updates on tracked sessions. Key timeouts and safeguards:

- **GitLab client timeout**: 15s per HTTP request (`gitlab.Gitlab(timeout=15)`)
- **Poll cycle timeout**: 30s for all `ThreadPoolExecutor` futures via `as_completed(timeout=30)`
- **Stuck-poll safeguard**: If `_scm_polling` has been `True` for over 60s, `_start_scm_poll` force-resets it so future polls can proceed
- **Poll interval**: Configurable in `.storage/gitlab_config.json` → `poll_interval` (default: 30s from `GITLAB_POLL_INTERVAL`)

Polling flow: `_scm_poll_timer` fires → `_start_scm_poll()` → `SCMPollerWorker` (QThread) → `get_mr_status()` per session via ThreadPoolExecutor → `results_ready` signal → `_on_scm_results()` updates `_mr_statuses` → `_update_mr_column()` refreshes widgets.

### Sending Threads to CQ

Right-clicking the MR status label (`PulsingLabel` in `ui_widgets.py`) shows a context menu with send modes:

- **"Send each thread to CQ (one per queue message)"** — queues each unresponded thread as a separate message via `SendThreadsWorker`
- **"Send all threads to CQ (combined into one message)"** — concatenates all threads (separated by `---`) into a single queue message via `SendThreadsCombinedWorker`
- **"Send each '/cq' thread to CQ"** — same as above but filtered to only threads with an unacknowledged `/cq` comment
- **"Send all '/cq' threads to CQ (combined)"** — same but combined into one message

Both regular modes share Phase 1 (`CollectThreadsWorker`): resolve provider → collect unresponded threads → match CQ sessions. The `/cq` variants use `CollectThreadsWorker` with `cq_only=True`, which calls `scan_cq_commands()` instead of `collect_unresponded_threads()`. Phase 2 differs: `SendThreadsWorker` sends one-by-one, `SendThreadsCombinedWorker` sends a single concatenated message. All modes acknowledge threads on the SCM side after successful send.

### /cq Auto-Fetch

The "Auto '/cq' fetch" checkbox (bottom bar, next to "Include git bots") controls whether the background poller automatically scans for `/cq` commands in MR threads:

- **ON (default)**: `SCMPollerWorker` calls `scan_cq_commands()` each poll cycle, sends matching threads to CQ, and acknowledges them. The manual `/cq` menu items are greyed out.
- **OFF**: Poller skips `/cq` scanning. User can manually fetch via the right-click menu items.

A `/cq` comment on a thread does **not** count as a user response for unresponded thread detection — only the bot acknowledgment reply (`[ClaudeQ bot] on it!`) marks a thread as handled. Setting persisted in `.storage/monitor_prefs.json` as `auto_fetch_cq`.

### Dock Badge

The dock icon badge tracks two types of changes while the monitor window is unfocused:

- **MR changes**: State diff — badge shows how many MRs are in a different state vs last time the user looked (recomputed each poll)
- **Session status**: Event counter — badge increments each time a session transitions from Running → Idle (accumulates until window is focused)

Both counts sum into a single badge number. Focusing the monitor window resets all counts and snapshots current state.

### Persistent Rows & Pinned Sessions

Monitor rows persist across server/client lifecycle and monitor restarts via `pinned_sessions.json`. Key behaviors:

- **Auto-pinning**: Every active session is automatically pinned on discovery
- **Dead rows**: When a server dies, the row stays with N/A for Status/Queue but shows Project/Branch info
- **Delete column**: Each row has a delete (X) button that always prompts for confirmation. If processes are running, warns they will be closed
- **`_deleted_tags` set**: Prevents auto-refresh from re-pinning rows that were just deleted

### Add Row from MR/PR URL

The "+" button adds a monitored row from a GitLab/GitHub MR URL:

1. User pastes MR/PR URL → `parse_mr_url()` extracts SCM type, project path, MR number
2. Fetches MR details via `get_mr_details()` (branch name, title)
3. Asks user for a CQ session tag (validated by `is_valid_tag()`)
4. Pins the row with remote MR info — no git operations, no auto MR tracking
5. User can click "Track MR" to start MR tracking, or "Server" to start a CQ server

Input validation loops: invalid tag or duplicate tag loops back to the input dialog instead of stopping the flow.

### Server Start from MR Row

When clicking "Server" on an MR-pinned dead row:

1. Looks in `repos_dir` (Settings, default `~/tmp/claudeq-repos`) for the project
2. Checks `repo-name`, `repo-name_1`, `repo-name_2`... — skips any dir with a running CQ server
3. If no available dir exists → clones fresh with next numeric suffix
4. If available dir found → fetches remote, checks if local is up-to-date
5. If branch deleted on remote → opens CQ in project dir anyway
6. If behind and clean → checks out + pulls. If behind and dirty → warns and stops
7. Opens `cq '<tag>'` in the default terminal at the project directory

### Tag Validation

Tags must match `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` (letters, numbers, hyphens, underscores). Validated by `is_valid_tag()` in `utils/constants.py` — shared between the shell launcher and the monitor GUI.

### Monitor Settings

Settings dialog (`monitor/settings_dialog.py`) accessible via the Settings button:

- **Default terminal**: Terminal.app or iTerm2 — used when opening new CQ servers
- **Repositories dir**: Where ClaudeQ clones repos for MR rows (default: `~/tmp/claudeq-repos`)
- **Clean unused repos**: Deletes cloned repos that have no running CQ server (checks resolved paths against active sessions)

Settings persisted in `.storage/monitor_prefs.json`.

## IDE Setup

### JetBrains (PyCharm, IntelliJ, etc.)
**Automatically configured during `make install`** ✅
- Terminal Engine set to **Classic**
- "Show application title" enabled in Advanced Settings
- Configures all installed IDEs (2024.2+)
- **Restart IDEs** after installation

### VS Code
**Automatically configured during `make install`** ✅
- Terminal selector extension auto-installed
- Terminal tabs show numbered labels (1, 2, 3...)
- Monitor can select specific tabs automatically
- View installed extension: Cmd+Shift+X → Search "ClaudeQ"

## Troubleshooting

**"Another client already connected"**
```bash
rm .storage/sockets/<tag>.client.lock
```

**Stale sockets**
```bash
cq-cleanup
```

**Icon not updating**
```bash
sudo rm -rf /Library/Caches/com.apple.iconservices.store
rm -rf ~/Library/Caches/com.apple.iconservices
killall Dock
```

## Make Commands

```bash
make install           # Install core + configure shell
make install-monitor   # Build and install GUI app
make run-monitor       # Run monitor from source (no build needed)
make update            # Update to latest version (git pull + rebuild)
make update-deps       # Update Python dependencies only
make uninstall         # Full cleanup
make clean             # Remove build artifacts
```

## Commit & Push Checklist

When the user asks to commit and push, **before committing**:

1. **Review CLAUDE.md** — Check that it reflects the current codebase. Update any outdated sections (project structure, key classes, features, conventions). Keep it detailed — this is the developer reference.
2. **Review README.md** — Check that it reflects user-facing changes (new features, commands, UI changes). Keep it **concise** — users see this on GitLab. Don't bloat it with implementation details.
3. Only update these files if something actually changed that affects them. Don't touch them for minor internal refactors.
