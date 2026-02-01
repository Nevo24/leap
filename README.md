# ClaudeQ

**Message queueing and image support for Claude Code - queue prompts with images and send when Claude is ready.**

Queue multiple prompts with images in one tab while Claude works in another. Auto-sends queued messages when ready for seamless workflow.

## Installation

```bash
git clone https://github.com/nevo24/claudeq.git
cd claudeq
chmod +x install.sh
./install.sh
source ~/.zshrc  # or ~/.bashrc for bash users
```

**Requirements:** tmux, Python 3, Claude CLI

**Note:** ClaudeQ runs directly from the project directory (no file copying). Don't move or delete the `claudeq` folder after installation.

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
You: How do I fix this bug?                    # Direct message
You: :q Refactor the authentication            # Queue for later
You: :ip                                       # Paste image, type message
🖼️ Image pasted from clipboard!
[📸] You: What's wrong with this UI?           # Send with image
```

**Queue with Images:**
```
You: :q :ip Explain this error                 # Queue message + image
📝 Queued with image: Explain this error (1 total)
You: :l                                        # View queue
📋 Queue (1 messages):
   1. [📸] Explain this error
You: :s                                        # Send from queue
```

See responses in Tab 1 in real-time!

## Features

### 📝 Message Queueing
Queue messages and send them automatically when Claude is ready:
```
You: :q Review this code
You: :q Add error handling
You: :l                # Show queue
You: :sa               # Send all
```

### 🖼️ Image Support
Paste images from clipboard and send with your messages:
```
You: :ip               # Paste image, then type message
You: :q :ip Explain this screenshot
```

Images are automatically sent to Claude CLI and can be queued for later.

**Auto-queue** (default) automatically sends queued messages when Claude is ready.

## Client Commands

All commands are **case-insensitive** (`:Q`, `:IP`, `:SEND` all work).

| Command | Description |
|---------|-------------|
| 💬 Type message | Send directly to Claude |
| 🖼️ `:ip` or `:imagepaste` | Paste image from clipboard |
| 📝 `:q <msg>` or `:queue <msg>` | Queue message for later |
| 📝 `:q :ip <msg>` | Queue message with image |
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
