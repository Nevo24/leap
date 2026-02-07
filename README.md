# ClaudeQ

**Multi-session Claude Code with message queueing and image support - works perfectly in IntelliJ and VS Code with native scrolling!**

Queue multiple prompts with images in one terminal while Claude works in another. Auto-sends queued messages when ready for a seamless workflow.

## ✨ Key Features

- 📝 **Smart message queueing** - Auto-sends when Claude is ready
- 🖥️ **Real-time GUI queue monitoring** - See messages being processed + Jump to sessions across IDEs and projects

## How It Works

ClaudeQ uses a **PTY-based client-server model**:

1. **Terminal 1 (Server)**: `cq my-feature` → Starts Claude with scrolling
2. **Terminal 2 (Client)**: `cq my-feature` → Interactive client for queueing messages

**Note:** Only one client can connect to a server at a time

The same command auto-detects whether to start a server or connect as a client based on socket existence.

## Platform Compatibility

**macOS**: Full support (all features)
**Linux**: Core features work (queueing, auto-send). Image support and monitor navigation require adaptation.
**Windows**: Not supported (PTY/Unix sockets incompatible)

## Installation

### Core Installation

```bash
# 1. Install Claude CLI (required)
npm install -g @anthropic-ai/claude-code

# 2. Clone and install ClaudeQ
git clone https://github.com/nevo24/claudeq.git
cd claudeq
make install

# 3. Reload shell
source ~/.zshrc  # or ~/.bashrc for bash
```

### Monitor GUI (Optional)

The monitor is a native macOS app that shows all active sessions and lets you jump to them.

```bash
make install-monitor
```

This will:
- Build the app with py2app (PyQt5)
- Install to `/Applications/ClaudeQ Monitor.app`
- Launch from Spotlight, Applications folder, or pin to Dock

**Requirements:**
- Python 3.8+
- Poetry (auto-installed by Makefile)
- Node.js and Claude CLI
- macOS (for clipboard image support)

## Usage

### Quick Start

```bash
# Terminal 1 (IntelliJ terminal) - Start server
cq my-feature

# Terminal 2 (any terminal) - Queue messages
cq my-feature
You: How do I fix this bug?          # Queued
You: Refactor the authentication     # Queued
```

Messages auto-send to Claude when ready. Watch responses in Terminal 1!

### With Images

```bash
# Copy image to clipboard, then:
You: !ip What's wrong with this UI?        # Queue with image
You: !d !ip Explain this error now         # Send directly with image

# Or attach first:
You: !ip                                   # Attach image from clipboard
You: What's wrong with this UI?            # Type message
```

### Direct Send (Bypass Queue)

```bash
You: !d Urgent! Need answer now           # Send immediately
You: !d !ip Fix this error                # Send with image immediately
```

## Client Commands

All commands are **case-insensitive**.

| Command | Description |
|---------|-------------|
| 💬 `message` | Queue message (auto-sends) |
| 🖼️ `!ip <msg>` | Queue with clipboard image |
| ⚡ `!d <msg>` | Send directly (bypass queue) |
| ⚡ `!d !ip <msg>` | Send directly with image |
| ⚡ `!f` | Force-send next queued message |
| 📋 `!l` | Show queue |
| 🗑️ `!c` | Clear queue |
| 📊 `!status` | Server status |
| 👋 `!x` or `Ctrl+D` | Exit client |

### 💡 IDE Configuration

**Terminal tab naming is automatically configured during installation!**

**JetBrains IDEs:** Automatically configured by `make install` ✅
- Sets **Terminal Engine** to **Classic**
- Enables **Show application title** in Advanced Settings
- Configures all installed JetBrains IDEs (IntelliJ, PyCharm, GoLand, WebStorm, etc.)
- **Restart your JetBrains IDEs** for changes to take effect

**VS Code:** Automatically configured by `make install` ✅
- Installs `code` CLI command
- Adds `terminal.integrated.tabs.title` setting to settings.json
- Installs the "ClaudeQ Terminal Selector" extension
- Restart VS Code if it was already open

## Monitor GUI

Launch the GUI monitor to view all active sessions and quickly jump to them.

After running `make install-monitor`, launch from:
- Spotlight: Search "ClaudeQ Monitor"
- Applications folder: Double-click `ClaudeQ Monitor.app`
- Dock: Pin the app for quick access

The monitor shows:
- All active ClaudeQ sessions
- Queue size for each session
- Click buttons to jump to correct IDE → Project → Terminal

**Supports:** PyCharm, IntelliJ IDEA, GoLand, WebStorm, VS Code, Terminal.app, iTerm2

**Notes:**
- **JetBrains IDEs**: Jumps to specific terminal tab automatically (requires Classic terminal + "Show application title" setting)
- **VS Code**: Jumps to specific terminal tab automatically (auto-configured during installation)
- **Terminal.app/iTerm2**: Jumps to specific tab automatically

## Example Workflow

**IntelliJ Terminal (Server with scrolling):**
```bash
cq bug-fix

   _____ _                 _       ___
  / ____| |               | |     / _ \
 | |    | | __ _ _   _  __| | ___| | | |
 | |    | |/ _` | | | |/ _` |/ _ \ | | |
 | |____| | (_| | |_| | (_| |  __/ |_| |
  \_____|_|\__,_|\__,_|\__,_|\___|\___\

======================================================================
  PTY SERVER - Session: bug-fix
======================================================================
  All responses will appear HERE in this window.

  ✅ Native scrolling in IntelliJ
  ✅ Full terminal width
  ✅ No tmux needed!
```

**Any Other Terminal (Client):**
```bash
cq bug-fix

You: Find all TODO comments
📝 Queued: Find all TODO comments (1 total)

🤖 Server auto-sent 1 message(s) - 0 remaining in queue

You: !ip What's wrong with this screenshot?
🖼️ Image attached!
📝 Queued with image: What's wrong with this screenshot? (1 total)
```

## Troubleshooting

### Scrolling in IntelliJ

**Native mouse scrolling works automatically!** 🖱️

The PTY architecture ensures IntelliJ's native scrolling works perfectly without any special configuration.

### Terminal Tab Titles in IDEs

ClaudeQ automatically sets terminal tab titles to help you identify sessions:
- Server tabs: `cq-server <tag>`
- Client tabs: `cq-client <tag>`

#### JetBrains IDEs (IntelliJ, PyCharm, WebStorm, etc.)

✅ **Automatically configured during installation!**

The `make install` command automatically configures these settings for all installed JetBrains IDEs:
1. **Terminal Engine**: Set to **"Classic"**
2. **Show application title**: Enabled in Advanced Settings

**After installation, restart your JetBrains IDEs** for the changes to take effect.

💡 *These settings enable automatic terminal tab naming and allow ClaudeQ Monitor to track and navigate to your sessions correctly!*

Supports JetBrains 2024.2+ and newer versions.

#### VS Code

✅ **Automatically configured during installation!**

When you run `make install`, ClaudeQ will:
1. Install the `code` CLI command (creates symlink to `/usr/local/bin/code`)
2. Update your VS Code settings.json with: `"terminal.integrated.tabs.title": "${sequence}"`
3. Install the "ClaudeQ Terminal Selector" extension (enables automatic tab switching)
4. Create a backup of your settings before modifying

**After installation:**
- Restart VS Code if it was already running
- Terminal tabs will automatically be named `cq-server <tag>` and `cq-client <tag>`
- **Monitor navigation will jump to the correct project AND select the correct terminal tab**
- View the extension: Cmd+Shift+X → Search "ClaudeQ"

**Requirements:**
- Node.js and npm (for extension packaging)
- VS Code installed in `/Applications`

💡 *Tip: The extension runs silently in the background watching for terminal selection requests from the monitor!*

### Stale Socket

If you see "Socket connection failed", the server might have crashed:

```bash
# Just run the command again - it auto-detects and starts a new server
cq my-feature
```

The launcher automatically removes stale sockets and starts fresh.

### Claude CLI Not Found

```bash
npm install -g @anthropic-ai/claude-code
```

### Commands Not Working

If `cq` or `claudeq` commands aren't found after installation:
```bash
# Reload your shell configuration
source ~/.zshrc  # or ~/.bashrc for bash
```

If still not working, make sure ClaudeQ is in the expected location:
```bash
# Check if scripts exist
ls ~/workspace/claudeq/src/
```

If you moved the project directory, update the path in your shell config (~/.zshrc or ~/.bashrc).

## Additional Commands

```bash
cq-cleanup    # Remove dead sessions (or: cqc)
```

## Technical Details

### Files

- `claudeq-main.sh` - Smart launcher (auto-detects server/client)
- `claudeq-server.py` - PTY server with socket listener and metadata tracking
- `claudeq-client.py` - Interactive client with image support
- `claudeq-monitor.py` - GUI monitor for session management
- `activate_terminal.groovy` - JetBrains IDE automation script

### How Auto-Send Works

The server monitors Claude's child processes to detect when it's busy executing tools (Bash, Read, etc.). Messages are only auto-sent when Claude has no active child processes, ensuring they don't interrupt ongoing work.

### Image Format

Images are sent to Claude CLI using the `@path` syntax with a required trailing space. The server adds a 0.5s delay after sending the attachment path to allow Claude time to recognize the file before submitting the message.

## Uninstall

### Uninstall Monitor Only

Removes only the monitor app from `/Applications` and cleans build artifacts:

```bash
make uninstall-monitor
```

### Uninstall Everything

Removes all ClaudeQ components (core + monitor):

```bash
make uninstall
```

This removes:
- Shell configuration from `.zshrc`/`.bashrc`
- Poetry virtual environment
- ClaudeQ Monitor.app from `/Applications`
- Session data (`.storage/`)
- Build artifacts (`build/`, `dist/`)

## License

MIT License - see [LICENSE](LICENSE)

---

**Links:** [GitHub](https://github.com/nevo24/claudeq) • [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
