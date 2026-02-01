# ClaudeQ

**Multi-session Claude Code with auto-detection and message queueing**

ClaudeQ allows you to run multiple Claude Code sessions simultaneously in different terminal tabs, each tagged with a unique name. It automatically detects whether to start a new server or connect as a client, and includes a message queueing system for seamless workflow.

## Features

✨ **Multi-session support** - Run multiple Claude sessions with unique tags
🔄 **Auto-detection** - Automatically switches between server and client mode
📨 **Message queueing** - Queue messages and send them when Claude is ready
🏷️ **Tab naming** - Terminal tabs automatically show session names
🧹 **Smart cleanup** - Detects stale sessions (especially in iTerm2)
⚡ **Zero config** - Works out of the box after installation

## Prerequisites

- **tmux** - Terminal multiplexer
- **Python 3** - For the client script
- **Claude CLI** - Anthropic's Claude Code CLI tool
- **macOS** or **Linux** (tested on macOS)

## Installation

### Quick Install

```bash
git clone https://github.com/yourusername/claudeq.git
cd claudeq
chmod +x install.sh
./install.sh
source ~/.zshrc  # or ~/.bashrc for bash users
```

### Manual Installation

1. Copy scripts to `~/.local/bin/`:
   ```bash
   cp src/* ~/.local/bin/
   chmod +x ~/.local/bin/claudeq-*.sh ~/.local/bin/claudeq-*.py
   ```

2. Add to your `~/.zshrc` (or `~/.bashrc`):
   ```bash
   # ClaudeQ - Multi-session Claude with auto-detection and message queueing
   claude() {
       if [ $# -eq 0 ]; then
           # No arguments - run Claude directly
           command claude --dangerously-skip-permissions
       else
           # Has arguments - use ClaudeQ auto-detection with tag
           ~/.local/bin/claudeq-auto.sh "$@"
       fi
   }
   alias claude_server='~/.local/bin/claudeq-server.sh'
   alias claude_client='~/.local/bin/claudeq-client.py'
   ```

3. Reload your shell:
   ```bash
   source ~/.zshrc
   ```

## Usage

### Basic Usage

**Run Claude directly (no session):**
```bash
claude
```

**Start/connect to a tagged session:**
```bash
claude backend
```

This automatically:
- Starts a new server if no session exists
- Connects as a client if a session is already running

### Advanced Usage

**Force server mode:**
```bash
claude_server backend
```

**Force client mode:**
```bash
claude_client backend
```

### Typical Workflow

**Tab 1 - Start server:**
```bash
claude backend
```
→ Starts Claude in server mode. Tab shows: `claude-server backend (tmux)`

**Tab 2 - Connect as client:**
```bash
claude backend
```
→ Auto-detects existing session, connects as client. Tab shows: `claude-client backend (Python)`

Type messages in Tab 2, see responses in Tab 1!

### Message Queueing

The client supports queueing messages:

```
You: q:Review this code later
You: q:Add error handling
You: :list              # Show queued messages
You: :send              # Send next queued message
You: :sendall           # Send all queued messages
```

**Auto-queue mode** (enabled by default) automatically sends queued messages when Claude is ready.

### Client Commands

- `q:<message>` - Queue message for later
- `:s` or `:send` - Send next queued message
- `:sa` or `:sendall` - Send all queued messages
- `:l` or `:list` - Show queued messages
- `:clear` - Clear queue
- `:quit` or `Ctrl+D` - Exit client

## How It Works

1. **Server Mode**: Runs Claude Code in a tmux session with a unique tag
2. **Client Mode**: Sends input to the tmux session and displays responses
3. **Auto-detection**: Checks if a session exists and whether Claude is running
4. **Stale Session Detection**: Handles iTerm2's quirks by checking if terminal processes are active

## Project Structure

```
claudeq/
├── README.md                 # This file
├── LICENSE                   # MIT License
├── install.sh                # Installation script
├── uninstall.sh              # Uninstallation script
└── src/
    ├── claudeq-auto.sh       # Auto-detection script
    ├── claudeq-server.sh     # Server mode script
    └── claudeq-client.py     # Client mode script
```

## Troubleshooting

### Session doesn't close when I close the tab (iTerm2)

ClaudeQ includes special detection for iTerm2's behavior. If issues persist:
1. Make sure you're running the latest version
2. Try manually killing the session: `tmux kill-session -t claude-<tag>`

### "Claude CLI not found"

Install the Claude Code CLI:
```bash
npm install -g @anthropic-ai/claude-code
```

Or visit: https://docs.anthropic.com/en/docs/claude-code

### Scripts not found after installation

Ensure `~/.local/bin` is in your PATH:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add this to your `~/.zshrc` or `~/.bashrc`.

## Uninstallation

```bash
./uninstall.sh
```

Or manually:
1. Remove scripts: `rm ~/.local/bin/claudeq-*`
2. Remove configuration from `~/.zshrc` (search for "ClaudeQ")
3. Remove queue data: `rm -rf ~/.claude-queues/`

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT License - see LICENSE file for details

## Author

Created by Nevo Mashiach

## Acknowledgments

- Built on top of [Claude Code](https://docs.anthropic.com/en/docs/claude-code) by Anthropic
- Uses [tmux](https://github.com/tmux/tmux) for session management
