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
    │   ├── terminal.py          # Terminal title, colors, banner
    │   └── ide_detection.py     # IDE detection, git branch
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
    │   ├── navigation.py        # IDE terminal navigation
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

| Class | File | Purpose |
|-------|------|---------|
| `ClaudeQServer` | `server/server.py` | Orchestrates PTY, socket, queue, metadata |
| `ClaudeQClient` | `client/client.py` | Interactive client with image support |
| `MonitorWindow` | `monitor/app.py` | PyQt5 GUI for session management |

## Runtime Data Files

All runtime data is stored in the centralized `.storage` directory at the project root:

| File | Location |
|------|----------|
| Settings | `.storage/settings.json` |
| Queue | `.storage/queues/<tag>.queue` |
| History | `.storage/queues/<tag>.history` |
| Socket | `.storage/sockets/<tag>.sock` |
| Metadata | `.storage/sockets/<tag>.meta` |
| Client lock | `.storage/sockets/<tag>.client.lock` |

## Client Commands

| Command | Action |
|---------|--------|
| `<message>` | Queue message (auto-sends when ready) |
| `!ip <msg>` | Queue with clipboard image |
| `!d <msg>` | Send directly (bypass queue) |
| `!e <index>` | Edit queued message by index (0=first) |
| `!f` | Force-send next queued message |
| `!l` | Show queue |
| `!status` | Server status |
| `!x` | Exit client |

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
