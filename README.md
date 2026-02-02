# ClaudeQ

**Multi-session Claude Code with message queueing and image support - works perfectly in IntelliJ with native scrolling!**

Queue multiple prompts with images in one terminal while Claude works in another. Auto-sends queued messages when ready for a seamless workflow.

## ✨ Key Features

- 📝 **Smart message queueing** - Auto-sends when Claude is ready
- 🖼️ **Image support** - Paste images from clipboard
- 🔌 **Client-server architecture** - Multiple clients per session
- 🧹 **Auto-cleanup** - Proper socket management
- 📊 **Real-time queue monitoring** - See messages being processed
- 🖱️ **Native scrolling in IntelliJ/JetBrains IDEs** - No tmux needed!

## How It Works

ClaudeQ uses a **PTY-based client-server model**:

1. **Terminal 1 (Server)**: `cq my-feature` → Starts Claude with scrolling
2. **Terminal 2+ (Clients)**: `cq my-feature` → Interactive client for queueing messages

The same command auto-detects whether to start a server or connect as a client based on socket existence.

## Installation

```bash
# 1. Install Claude CLI (required)
npm install -g @anthropic-ai/claude-code

# 2. Clone and install ClaudeQ
git clone https://github.com/nevo24/claudeq.git
cd claudeq
./install.sh

# 3. Reload shell
source ~/.zshrc  # or ~/.bashrc for bash
```

**Requirements:**
- Python 3 (install.sh will install Python dependencies automatically via pip3)
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
You: :ip What's wrong with this UI?        # Queue with image
You: :d :ip Explain this error now         # Send directly with image

# Or attach first:
You: :ip                                   # Attach image from clipboard
You: What's wrong with this UI?            # Type message
```

### Direct Send (Bypass Queue)

```bash
You: :d Urgent! Need answer now           # Send immediately
You: :d :ip Fix this error                # Send with image immediately
```

## Client Commands

All commands are **case-insensitive**.

| Command | Description |
|---------|-------------|
| 💬 `message` | Queue message (auto-sends) |
| 🖼️ `:ip <msg>` | Queue with clipboard image |
| ⚡ `:d <msg>` | Send directly (bypass queue) |
| ⚡ `:d :ip <msg>` | Send directly with image |
| 📋 `:l` | Show queue |
| 🗑️ `:c` | Clear queue |
| 📊 `:status` | Server status |
| 👋 `:x` or `Ctrl+D` | Exit client |

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

You: :ip What's wrong with this screenshot?
🖼️ Image attached!
📝 Queued with image: What's wrong with this screenshot? (1 total)
```

## Architecture

```
┌─────────────────────────┐
│  Terminal 1 (IntelliJ)  │
│                         │
│  PTY Server             │
│  ├─ Claude CLI          │
│  ├─ Socket Server       │
│  └─ Auto-sender         │
│                         │
│  ✅ Scrolling works!    │
└─────────────────────────┘
            ↑
            │ Unix Socket
            │
    ┌───────┴────────┐
    │                │
┌───────┐      ┌───────┐
│ Tab 2 │      │ Tab 3 │
│       │      │       │
│Client │      │Client │
└───────┘      └───────┘
```

## Troubleshooting

### Scrolling in IntelliJ

**Native mouse scrolling works automatically!** 🖱️

The PTY architecture ensures IntelliJ's native scrolling works perfectly without any special configuration.

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

## Technical Details

### Files

- `claudeq-main-pty.sh` - Smart launcher (auto-detects server/client)
- `claudeq-server-pty-socket.py` - PTY server with socket listener
- `claudeq-client-pty.py` - Interactive client with image support

### How Auto-Send Works

The server monitors Claude's child processes to detect when it's busy executing tools (Bash, Read, etc.). Messages are only auto-sent when Claude has no active child processes, ensuring they don't interrupt ongoing work.

### Image Format

Images are sent to Claude CLI using the `@path` syntax with a required trailing space. The server adds a 0.5s delay after sending the attachment path to allow Claude time to recognize the file before submitting the message.

## Uninstall

To uninstall ClaudeQ:
1. Remove the ClaudeQ configuration from your shell config (`~/.zshrc` or `~/.bashrc`)
2. Delete the project directory: `rm -rf ~/workspace/claudeq`
3. Clean up data: `rm -rf ~/.claude-queues ~/.claude-sockets`

## License

MIT License - see [LICENSE](LICENSE)

---

**Links:** [GitHub](https://github.com/nevo24/claudeq) • [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
