# ClaudeQ

**Message queueing and image support for Claude Code - queue prompts with images and send when Claude is ready.**

Queue multiple prompts with images in one tab while Claude works in another. Auto-sends queued messages when ready for seamless workflow.

## How It Works

ClaudeQ uses a **client-server model**:
1. **First `claudeq my-cool-feature`** → Starts server (Claude CLI process that receives messages)
2. **Second `claudeq my-cool-feature`** → Connects as client (sends queued messages to server)

The same command auto-detects whether to start a server or connect as a client. You can always use `claude` directly for standard Claude CLI behavior without queueing.

## Installation

```bash
# 1. Install dependencies
brew install tmux python3 node
npm install -g @anthropic-ai/claude-code

# 2. Install ClaudeQ
git clone https://github.com/nevo24/claudeq.git
cd claudeq
chmod +x install.sh
./install.sh

# 3. Reload shell
source ~/.zshrc  # or ~/.bashrc for bash users
```

**Requirements:** tmux, Python 3, Node.js (for Claude CLI), Claude CLI

## Usage

### Start/Connect to Tagged Session

```bash
claudeq my-cool-new-feature
```

Automatically starts server if new, or connects as client if session exists.

### Run Claude Without Queueing

```bash
claude
```

Standard Claude Code behavior (unchanged).

### Example Workflow

**Tab 1 - Server:**
```bash
claudeq my-cool-new-feature
```
→ Claude CLI running in tmux session

**Tab 2 - Client:**
```bash
claudeq my-cool-new-feature
```
→ Client interface with queueing and image support

**Basic Usage:**
```
You: How do I fix this bug?                    # Queued automatically
You: Refactor the authentication               # Queued automatically
You: :d Urgent! Need answer now                # Send directly, bypass queue
```

**With Images:**
```
You: :ip What's wrong with this UI?            # Queue with image in one command
You: :ip                                       # Or attach image first...
🖼️ Image attached! Type message or press Enter to queue
You: What's wrong with this UI?                # ...then queue with message

You: :d :ip Explain this error now             # Send directly with image
```

**Queue Management:**
```
You: :l                                        # View queue
📋 Queue (2 messages):
   1. [📸] What's wrong with this UI?
   2. Refactor the authentication
You: :s                                        # Send next from queue
You: :sa                                       # Send all remaining
```

See responses in Tab 1 in real-time!

## Features

### 📝 Smart Message Queueing (Default Behavior)
**Messages are queued by default** and sent automatically when Claude is ready:
```
You: Review this code           # Queued automatically
You: Add error handling          # Queued automatically
You: :l                          # Show queue
You: :sa                         # Send all
```

Need to send immediately? Use `:d`:
```
You: :d Urgent question!         # Sends directly, bypasses queue
```

### 🖼️ Image Support
Paste images from clipboard:
```
You: :ip Explain this screenshot # Queue with image in one command
You: :ip                         # Or attach image, then type message
You: Explain this screenshot     # Queued with image

You: :d :ip Fix this now         # Send directly with image (bypass queue)
```

Images are automatically sent to Claude CLI.

**Auto-queue** automatically sends queued messages when Claude is ready - no manual intervention needed!

## Client Commands

All commands are **case-insensitive** (`:D`, `:IP`, `:SEND` all work).

| Command | Description |
|---------|-------------|
| 💬 Type message | **Queue message** (auto-sends when ready) |
| 🖼️ `:ip <msg>` or `:imagepaste <msg>` | Queue with image from clipboard |
| ⚡ `:d <msg>` or `:direct <msg>` | **Send directly** (bypass queue) |
| ⚡ `:d :ip <msg>` or `:direct :ip <msg>` | Send directly with image |
| 📤 `:s` or `:send` | Send next queued message |
| 📨 `:sa` or `:sendall` | Send all queued messages |
| 📋 `:l` or `:list` | Show queue contents |
| 🗑️ `:c` or `:clear` | Clear queue |
| 👋 `:x` or `:quit` | Exit client (or `Ctrl+D`) |

## Available Commands

| Command | Description |
|---------|-------------|
| `claudeq <tag>` | Start/connect to tagged session (auto-detects) |
| `claude` | Run Claude directly (no queueing) |

## Troubleshooting

**Scrolling in the server tab:**
Your terminal's native scrollback works with mouse wheel for recent content. For older history (up to 50,000 lines), press `Ctrl+B` then `[` to enter copy mode, then use arrow keys or Page Up/Down to scroll. Press `q` to exit.

**Copying text from the server tab:**
Select text with your mouse and copy normally (`Cmd+C` or right-click).

**Claude CLI not found?**
```bash
npm install -g @anthropic-ai/claude-code
```

**Scripts not found?** Add to shell config:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

**Kill stale session:**
```bash
tmux kill-session -t claude-<tag>
```

## Uninstall

```bash
cd claudeq
./uninstall.sh
```

## License

MIT License - see [LICENSE](LICENSE)

---

**Links:** [GitHub](https://github.com/nevo24/claudeq) • [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
