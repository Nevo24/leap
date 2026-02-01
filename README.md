# ClaudeQ

**Message queueing for Claude Code - queue prompts and send them when Claude is ready.**

Queue multiple prompts in one tab while Claude works in another. Auto-sends queued messages when ready for seamless workflow.

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
claudeq backend
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
claudeq backend
```
→ Tab shows: `claude-server backend (tmux)`

**Tab 2 - Client:**
```bash
claudeq backend
```
→ Tab shows: `claude-client backend (Python)`

Type messages in Tab 2, see responses in Tab 1!

## Message Queueing

Queue messages for later:

```
You: q:Review this code
You: q:Add error handling
You: :list              # Show queue
You: :sendall           # Send all
```

**Auto-queue** (default) automatically sends when Claude is ready.

## Client Commands

| Command | Description |
|---------|-------------|
| `q:<message>` | Queue message |
| `:send` or `:s` | Send next queued |
| `:sendall` or `:sa` | Send all queued |
| `:list` or `:l` | Show queue |
| `:clear` | Clear queue |
| `:quit` or `Ctrl+D` | Exit |

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
