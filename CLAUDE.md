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
    │   ├── constants.py         # QUEUE_DIR, SOCKET_DIR, timing, colors
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
    │   ├── session_manager.py   # Session discovery
    │   ├── cq_sender.py         # Socket sender for /cq commands
    │   ├── gitlab_setup_dialog.py # GitLab connection dialog
    │   ├── navigation.py        # IDE terminal navigation
    │   ├── mr_tracking/         # MR tracking subsystem
    │   │   ├── base.py          # Abstract SCMProvider, MRState, MRStatus
    │   │   ├── config.py        # GitLab/monitor preferences persistence
    │   │   ├── gitlab_provider.py # GitLab API implementation
    │   │   ├── git_utils.py     # Git remote URL parsing
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
| `GitLabProvider` | `monitor/mr_tracking/gitlab_provider.py` | GitLab MR thread tracking |
| `send_socket_request()` | `utils/socket_utils.py` | Shared Unix socket send/recv utility |

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
| `!status` | Server status |
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
