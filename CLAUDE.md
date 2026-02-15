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
    │   ├── app.py               # MonitorWindow (core window + UI init + lifecycle)
    │   ├── server_launcher.py   # MR server clone/checkout/start flow
    │   ├── session_manager.py   # Session discovery + read_client_pid()
    │   ├── scm_polling.py       # SCM poller + background workers
    │   ├── cq_sender.py         # Socket sender for /cq commands
    │   ├── navigation.py        # IDE terminal navigation
    │   ├── monitor_utils.py     # Utilities (icon finder, lock removal)
    │   │
    │   ├── _mixins/             # MonitorWindow mixin classes
    │   │   ├── scm_config_mixin.py    # SCM provider init, setup dialogs, toggles
    │   │   ├── session_mixin.py       # Session merge, navigate, close, delete
    │   │   ├── mr_tracking_mixin.py   # MR tracking, polling, thread send, add-row
    │   │   ├── mr_display_mixin.py    # MR column styling, dock badge, banners
    │   │   ├── notifications_mixin.py # User notification handling
    │   │   └── table_builder_mixin.py # Table build, refresh, settings
    │   │
    │   ├── dialogs/             # Dialog windows
    │   │   ├── settings_dialog.py     # Settings (terminal, repos dir, cleanup)
    │   │   ├── notifications_dialog.py # Per-type notification config (dock/banner)
    │   │   ├── scm_setup_dialog.py    # Abstract SCM setup base dialog
    │   │   ├── gitlab_setup_dialog.py # GitLab connection dialog
    │   │   ├── github_setup_dialog.py # GitHub connection dialog
    │   │   └── scm_context_dialog.py  # Context editor dialog (named presets)
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
| `MonitorWindow` | `monitor/app.py` | PyQt5 GUI core window (uses mixins for methods) |
| `SCMConfigMixin` | `monitor/_mixins/scm_config_mixin.py` | SCM provider init, setup dialogs, toggles |
| `SessionMixin` | `monitor/_mixins/session_mixin.py` | Session merge, navigate, close, delete |
| `MRTrackingMixin` | `monitor/_mixins/mr_tracking_mixin.py` | MR tracking, polling, thread send, add-row |
| `MRDisplayMixin` | `monitor/_mixins/mr_display_mixin.py` | MR column styling, dock badge, banners |
| `NotificationsMixin` | `monitor/_mixins/notifications_mixin.py` | User notification handling |
| `TableBuilderMixin` | `monitor/_mixins/table_builder_mixin.py` | Table build, refresh, settings |
| `ContextEditorDialog` | `monitor/dialogs/scm_context_dialog.py` | Context preset editor dialog |
| `ServerLauncher` | `monitor/server_launcher.py` | MR server clone/force-align/start flow |
| `StatusLog` | `monitor/ui/status_log.py` | In-memory status message log + viewer dialog |
| `SettingsDialog` | `monitor/dialogs/settings_dialog.py` | Settings: terminal, repos dir, cleanup unused repos |
| `GitLabProvider` | `monitor/mr_tracking/gitlab_provider.py` | GitLab MR thread tracking + user notifications (Todos) |
| `GitHubProvider` | `monitor/mr_tracking/github_provider.py` | GitHub PR thread tracking + user notifications |
| `UserNotification` | `monitor/mr_tracking/base.py` | Dataclass for SCM user notifications (GitLab Todos / GitHub notifications) |
| `DockBadge` | `monitor/ui/dock_badge.py` | Dock icon badge overlay + notification event detection |
| `NotificationType` | `monitor/ui/dock_badge.py` | Enum of notification event types |
| `NotificationEvent` | `monitor/ui/dock_badge.py` | Dataclass for detected notification events |
| `NotificationsDialog` | `monitor/dialogs/notifications_dialog.py` | Per-type notification config (dock/banner toggles) |
| `get_notification_prefs()` | `monitor/mr_tracking/config.py` | Merge saved notification prefs with defaults |
| `load_notification_seen()` | `monitor/mr_tracking/config.py` | Load seen notification IDs per SCM type |
| `save_notification_seen()` | `monitor/mr_tracking/config.py` | Persist seen notification IDs per SCM type |
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
| Server lock | `.storage/sockets/<tag>.server.lock/` (directory) |
| Pinned sessions | `.storage/pinned_sessions.json` |
| Monitor prefs | `.storage/monitor_prefs.json` |
| Notification seen state | `.storage/notification_seen.json` |

## File Cleanup & Lifecycle

ClaudeQ has multiple cleanup mechanisms. This table shows **exactly** which function cleans which files and when it runs:

### Cleanup Functions

| Function Name | Location | Files Cleaned | Server Up | Server Down | Client Up | Client Down | Manual `cq-cleanup` | Monitor Up | Monitor Down |
|--------------|----------|---------------|-----------|-------------|-----------|-------------|---------------------|------------|--------------|
| `ClaudeQServer.cleanup()` | `server/server.py:434` | `.sock`<br>`.meta`<br>`.queue` (if empty)<br>PTY process | | ✅ | | | | | ✅ (via shutdown msg) |
| `ClaudeQClient._cleanup_lock()` | `client/client.py:103` | `.client.lock` | | | | ✅ | | | |
| `ClaudeQClient._cleanup_temp_images()` | `client/client.py:117` | `/tmp/*.png` (temp images) | | | | ✅ | | | |
| `ClaudeQServer._cleanup_old_history_files()` | `server/server.py:262` | `.history` (older than TTL) | ✅ | | | | | | |
| `cleanup_dead_sockets()` | `claudeq-main.sh:148` | `.sock` (dead)<br>`.queue` (dead)<br>`.meta` (dead)<br>`.client.lock` (dead)<br>`.server.lock/` (dead) | ✅ (background) | | | | | | |
| `cq-cleanup` script | `claudeq-cleanup.sh` | `.sock` (dead)<br>`.queue` (dead)<br>`.meta` (dead)<br>`.client.lock` (dead)<br>`.server.lock/` (dead) | | | | | ✅ | | |

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
| `.server.lock/` | Server starting (shell) | Shell trap on exit, dead session cleanup | Temporary |
| `/tmp/*.png` | Ctrl+V image paste | Client exit | Temporary |

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
- **Socket communication** → Use `send_socket_request()` from `utils/socket_utils.py` for any new code that needs to talk to a CQ server via Unix socket. Do not duplicate the connect/send/recv pattern. Incoming messages are capped at `MAX_MESSAGE_SIZE` (1 MB) in `socket_handler.py`; larger payloads are rejected.

## Code Conventions

- **Type hints**: 100% coverage on all function signatures and return types. Use `Optional[X]` (not `X | None`) for consistency.
- **Imports**: All imports at module top level. No inline imports except for optional dependencies (`prompt_toolkit`, `gitlab`).
- **Client commands**: Each command handler is extracted into a private `_handle_*` method on `ClaudeQClient`. The `_process_command` dispatcher delegates to these handlers.
- **Socket pattern**: `SocketClient._send_request()` is the single source of truth for client→server socket communication. `send_socket_request()` in `utils/socket_utils.py` is the lightweight variant for monitor/session_manager code that doesn't need rate-limited error reporting.

## SCM Polling (MR Tracking & User Notifications)

The monitor polls GitLab/GitHub for MR status updates on tracked sessions and user-level notifications. Key timeouts and safeguards:

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

A `/cq` comment on a thread does **not** count as a user response for unresponded thread detection — only the bot acknowledgment reply (`[ClaudeQ bot] on it!`) marks a thread as handled. The ack only covers `/cq` commands that appear **before** it in the thread; a new `/cq` posted after an existing ack is treated as a fresh trigger. Setting persisted in `.storage/monitor_prefs.json` as `auto_fetch_cq`.

### User Notifications (GitLab Todos / GitHub Notifications)

The monitor can poll for user-level notifications from GitLab and GitHub — these are independent of MR tracking and cover the user's entire SCM account (review requests, assignments, mentions).

- **Per-provider enable/disable**: Each SCM provider has an `enable_notifications` checkbox in its setup dialog (Settings > Connect to GitLab / Connect to GitHub). Internally tracked as `notif_scm_types: set[str]` — only providers in the set are polled
- **Polling**: `SCMPollerWorker` calls `get_user_notifications()` on each enabled provider each poll cycle. Returns `List[UserNotification]` with reason, title, URL, author
- **Deduplication**: Seen notification IDs are tracked per SCM type in `_notification_seen` and persisted to `.storage/notification_seen.json` across monitor restarts. Only unseen notifications trigger dock badge / banner events
- **First-run seeding**: On first enable for a provider, all existing notifications are marked as seen (seeded) so only new ones trigger alerts
- **Auth error handling**: 403 errors from notification APIs (e.g., missing token scope) trigger a blocking popup and auto-disable notifications for that provider. The user must re-enable via the setup dialog after fixing their token
- **Notification types**: `review_requested`, `assigned`, `mentioned` map to `NotificationType` enum values and are independently configurable in the Notifications dialog (dock badge + banner toggles)

### Dock Badge & Banner Notifications

The monitor has two notification channels, independently configurable per event type via **Settings > Notifications...**:

- **Dock badge**: Red badge overlay on the dock icon with a change count (default: on)
- **macOS banners**: Native macOS banner notifications with descriptive text (default: off, opt-in)

**Notification types:**

| Type | Trigger | Banner text example |
|------|---------|-------------------|
| `mr_unresponded` | MR state changed to unresponded or count increased | `"MR !42 'Fix auth' has 3 unresponded thread(s)"` |
| `mr_all_responded` | MR went from unresponded to all responded | `"MR !42 'Fix auth' — all threads responded"` |
| `mr_approved` | MR approved (False→True) | `"MR !42 'Fix auth' approved by John, Jane"` |
| `session_completed` | Running→Idle (busy for at least 1.5s) | `"Claude finished processing"` |
| `review_requested` | User requested to review an MR/PR | `"Review requested on MR !42 'Fix auth' by John"` |
| `assigned` | User assigned to an MR/PR | `"You are assigned to MR !42 'Fix auth'"` |
| `mentioned` | User mentioned in a discussion | `"You were mentioned in thread on MR !42"` |

The first four types come from MR tracking (per-session). The last three come from user-level SCM notifications (GitLab Todos / GitHub notifications) — these fire regardless of whether the MR is tracked in ClaudeQ.

Dock badge counts sum into a single number. Focusing the monitor window resets all counts.

**Banner implementation:** Uses `NSUserNotification` via PyObjC (`pyobjc-framework-Cocoa`). Requires macOS notification permissions: System Settings > Notifications > ClaudeQ Monitor (or "Python" when running from source). The `_identityImage` private API overrides the app icon in notifications when running from source.

**Preferences:** Stored in `.storage/monitor_prefs.json` under the `notifications` key. `get_notification_prefs()` in `config.py` merges saved prefs with defaults.

### Persistent Rows & Pinned Sessions

Monitor rows persist across server/client lifecycle and monitor restarts via `pinned_sessions.json`. Key behaviors:

- **Auto-pinning**: Every active session is automatically pinned on discovery
- **Row survival rule**: A row must have a running server OR active MR tracking. Dead rows without MR tracking are auto-removed on the next refresh cycle
- **Track MR enrichment**: When "Track MR" finds an MR on an auto-pinned row, the pinned session is enriched with `remote_project_path`, `host_url`, `scm_type`, `branch`, `mr_title`, `mr_url`, `mr_tracked` — making the row survive server death
- **MR auto-reconnect on startup**: When the monitor restarts, it silently re-connects MR tracking for rows that had `mr_tracked: True` when the monitor last ran. Popups are suppressed. If reconnection fails (no MR found or API error) and no server is running, the row is silently removed
- **Dead rows**: A row whose CQ server is no longer running. Shows N/A for Status/Queue/Server Branch but preserves Project info. The Server button offers to (re)start the server. For MR-pinned dead rows, starting the server triggers force-align (fetch + hard reset to remote). Track MR button is disabled on dead rows
- **Close server prompt**: If a session has no MR tracking, closing the server warns the user the row will be removed and offers to close the client too
- **Stop MR tracking prompt**: If the server is dead, stopping MR tracking warns the user the row will be removed and offers to close the client too
- **Delete button**: Each row has a delete (X) button in the leftmost column (replacing row indices). Always prompts for confirmation. If processes are running, warns they will be closed
- **`_deleted_tags` set**: Prevents auto-refresh from re-pinning rows that were just deleted

### Add Row from MR/PR URL

The "+" button adds a monitored row from a GitLab/GitHub MR URL:

1. User pastes MR/PR URL → `parse_mr_url()` extracts SCM type, project path, MR number
2. Fetches MR details via `get_mr_details()` (branch name, title)
3. Asks user for a CQ session tag (validated by `is_valid_tag()`)
4. Pins the row with remote MR info and auto-starts MR tracking
5. MR column shows tracking status immediately, MR Branch shows the MR source branch

Input validation loops: invalid tag or duplicate tag loops back to the input dialog instead of stopping the flow.

### Column Layout

Columns are grouped: **[X, Tag, Project]** | **[Server, Server Branch, Status, Queue]** | **[Client]** | **[MR, MR Branch]**. Solid white vertical lines separate groups; semi-transparent white lines separate columns within a group. The X column contains the delete button (no row indices). Close (X) buttons for Server, Client, and MR appear on the left side of their respective cells.

- **Server Branch**: Always shows the live git branch the server is running on. For dead rows, shows the last known branch.
- **MR Branch**: Shows the MR's source branch when MR tracking is active. "N/A" otherwise.
- **Track MR button**: Shown in the MR column only when not tracked; MR Branch shows "N/A". When tracked, MR column shows status and MR Branch shows the source branch. Clicking the MR X button restores the Track MR button and N/A branch.

### Branch Mismatch & Validation

Two scenarios can cause branch-related issues on MR-pinned rows:

1. **Branch changes while server is running** — user switches branch in another terminal, an IDE auto-checks out, or local falls behind remote. The monitor detects this on each table refresh and shows `⚠ Server` in orange with a tooltip: "Branch mismatch: expected 'feature-x', got 'master'". This is a visual warning only; the server keeps running.
2. **MR merged and branch deleted on remote** — detected when starting/syncing a dead MR row via `server_launcher.py`. The user is prompted: "Branch was deleted on remote (MR merged?). Open on stale local state?"

Other mismatches (wrong directory, wrong repo, wrong branch at startup) are **blocked before the server starts** by `_validate_pinned_session()` in `server.py` — see "Server Startup Validation" below.

### Server Start from MR Row

When clicking "Server" on an MR-pinned dead row:

1. If saved `project_path` is in use by another CQ server → clears it, finds a free directory
2. Looks in `repos_dir` (Settings, default `/tmp/claudeq-repos`) for the project
3. Checks `repo-name`, `repo-name_1`, `repo-name_2`... — skips any dir with a running CQ server
4. If no available dir exists → clones fresh with next numeric suffix
5. If available dir found → **force-aligns** to remote: fetch + checkout + `git reset --hard origin/<branch>` + `git clean -fd`
6. If branch deleted on remote → user prompted to open on stale state
7. Opens `cq '<tag>'` in the default terminal at the project directory

These are managed clones (not user workspaces), so local changes are always discarded in favour of the remote state.

### Tag Validation

Tags must match `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` (letters, numbers, hyphens, underscores). Validated by `is_valid_tag()` in `utils/constants.py` — shared between the shell launcher and the monitor GUI.

### Server Startup Validation (MR-Pinned Sessions)

When a CQ server starts (`cq <tag>`), it checks `.storage/pinned_sessions.json` for MR-pinned rows matching the tag. A row is MR-pinned if it has a `remote_project_path` field. Auto-pinned rows (no `remote_project_path`) skip validation entirely.

Validation checks (in order):
1. **Repo match**: Parses `git remote.origin.url` and compares project path with pinned `remote_project_path`
2. **Branch match**: Compares `git branch --show-current` with pinned `branch` (skipped if branch is empty or `N/A`)
3. **Behind remote**: Runs `git fetch` (with SCM token injection via `_build_auth_fetch_url()`) then `git merge-base --is-ancestor` to verify local is not behind remote
4. **Ahead / dirty warnings** (non-fatal): If local has commits ahead of remote or uncommitted changes, prints a yellow warning but allows startup

Checks 1-3 fail with a red error and exit. Check 4 is a non-fatal warning. Network failures during fetch are tolerated (don't block startup).

Implemented in `ClaudeQServer._validate_pinned_session()` (`server/server.py`), called early in `__init__` before socket/PTY setup.

### Monitor Settings

Settings dialog (`monitor/settings_dialog.py`) accessible via the Settings button:

- **Default terminal**: Terminal.app or iTerm2 — used when opening new CQ servers
- **Repositories dir**: Where ClaudeQ clones repos for MR rows (default: `/tmp/claudeq-repos`)
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
