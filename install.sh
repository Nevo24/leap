#!/bin/bash
#
# ClaudeQ Installation Script
#

set -e  # Exit on error

echo "================================================"
echo "  ClaudeQ Installer"
echo "================================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
INSTALL_DIR="$HOME/.local/bin"

echo "📋 Checking dependencies..."

# Check for tmux
if ! command -v tmux &> /dev/null; then
    echo -e "${RED}✗ tmux is not installed${NC}"
    echo "  Install with: brew install tmux"
    exit 1
fi
echo -e "${GREEN}✓ tmux found${NC}"

# Check for Python 3
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ python3 is not installed${NC}"
    echo "  Install with: brew install python3"
    exit 1
fi
echo -e "${GREEN}✓ python3 found${NC}"

# Check for Claude CLI
if ! command -v claude &> /dev/null; then
    echo -e "${YELLOW}⚠ Claude CLI not found${NC}"
    echo "  ClaudeQ requires the Claude CLI to be installed."
    echo "  Install from: https://docs.anthropic.com/en/docs/claude-code"
    read -p "  Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
else
    echo -e "${GREEN}✓ Claude CLI found${NC}"
fi

echo ""
echo "📦 Installing ClaudeQ scripts..."

# Create install directory if it doesn't exist
mkdir -p "$INSTALL_DIR"

# Copy scripts
cp "$SCRIPT_DIR/src/claudeq-auto.sh" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/src/claudeq-server.sh" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/src/claudeq-client.py" "$INSTALL_DIR/"

# Make scripts executable
chmod +x "$INSTALL_DIR/claudeq-auto.sh"
chmod +x "$INSTALL_DIR/claudeq-server.sh"
chmod +x "$INSTALL_DIR/claudeq-client.py"

echo -e "${GREEN}✓ Scripts installed to $INSTALL_DIR${NC}"

# Detect shell
SHELL_NAME=$(basename "$SHELL")
if [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
else
    echo -e "${YELLOW}⚠ Unknown shell: $SHELL_NAME${NC}"
    echo "  Please manually add the configuration to your shell RC file."
    RC_FILE=""
fi

if [ -n "$RC_FILE" ]; then
    echo ""
    echo "⚙️  Configuring shell ($SHELL_NAME)..."

    # Check if ClaudeQ config already exists
    if grep -q "# ClaudeQ - Multi-session Claude" "$RC_FILE" 2>/dev/null; then
        echo -e "${YELLOW}⚠ ClaudeQ configuration already exists in $RC_FILE${NC}"
        read -p "  Overwrite? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            # Remove old config
            sed -i.bak '/# ClaudeQ - Multi-session Claude/,/^alias claude_client=/d' "$RC_FILE"
            echo -e "${GREEN}✓ Removed old configuration${NC}"
        else
            echo "  Skipping shell configuration."
            RC_FILE=""
        fi
    fi

    if [ -n "$RC_FILE" ]; then
        # Backup RC file
        cp "$RC_FILE" "$RC_FILE.backup-$(date +%Y%m%d-%H%M%S)"
        echo -e "${GREEN}✓ Backed up $RC_FILE${NC}"

        # Add ClaudeQ configuration
        cat >> "$RC_FILE" << 'EOF'

# ClaudeQ - Multi-session Claude with auto-detection and message queueing
claudeq() {
    ~/.local/bin/claudeq-auto.sh "$@"
}
# Keep claude as direct access (no queueing)
alias claude='command claude --dangerously-skip-permissions'
alias claude_server='~/.local/bin/claudeq-server.sh'
alias claude_client='~/.local/bin/claudeq-client.py'
EOF
        echo -e "${GREEN}✓ Added ClaudeQ configuration to $RC_FILE${NC}"
    fi
fi

# Check if ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo ""
    echo -e "${YELLOW}⚠ $HOME/.local/bin is not in your PATH${NC}"
    echo "  Add this to your $RC_FILE:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "================================================"
echo -e "${GREEN}✓ ClaudeQ installed successfully!${NC}"
echo "================================================"
echo ""
echo "To start using ClaudeQ:"
echo "  1. Reload your shell: source $RC_FILE"
echo "  2. Run: claudeq <tag-name>"
echo ""
echo "Examples:"
echo "  claude              # Run Claude directly (no queueing)"
echo "  claudeq backend     # Start/connect to 'backend' session"
echo "  claudeq frontend    # Start/connect to 'frontend' session"
echo ""
echo "For more info, visit: https://github.com/nevo24/claudeq"
echo ""
