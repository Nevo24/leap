#!/bin/bash
#
# ClaudeQ PTY Installation Script
#

set -e  # Exit on error

echo "================================================"
echo "  ClaudeQ PTY Installer"
echo "================================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SRC_DIR="$SCRIPT_DIR/src"

echo "📋 Checking dependencies..."

# Check for Python 3
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ python3 is not installed${NC}"
    echo "  Install with: brew install python3"
    exit 1
fi
echo -e "${GREEN}✓ python3 found${NC}"

# Install Python dependencies
echo "  Installing Python dependencies from requirements.txt..."
if pip3 install -r "$SCRIPT_DIR/requirements.txt" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Python dependencies installed${NC}"
else
    echo -e "${YELLOW}⚠ Could not install via pip3${NC}"
    echo "  Trying with user install..."
    if pip3 install --user -r "$SCRIPT_DIR/requirements.txt"; then
        echo -e "${GREEN}✓ Python dependencies installed (user)${NC}"
    else
        echo -e "${RED}✗ Failed to install dependencies${NC}"
        echo "  Try manually: pip3 install -r $SCRIPT_DIR/requirements.txt"
        exit 1
    fi
fi

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
echo "📦 Setting up ClaudeQ PTY..."

# Make scripts executable
chmod +x "$SRC_DIR/claudeq-main-pty.sh"
chmod +x "$SRC_DIR/claudeq-server-pty-socket.py"
chmod +x "$SRC_DIR/claudeq-client-pty.py"

echo -e "${GREEN}✓ Scripts configured in $SRC_DIR${NC}"
echo "  ClaudeQ will run directly from project directory (no file copying)"

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
    if grep -q "# ClaudeQ PTY" "$RC_FILE" 2>/dev/null; then
        echo -e "${YELLOW}⚠ ClaudeQ configuration already exists in $RC_FILE${NC}"
        read -p "  Overwrite? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            # Remove old config
            sed -i.bak '/# ClaudeQ PTY/,/^}$/d' "$RC_FILE"
            echo -e "${GREEN}✓ Removed old configuration${NC}"
        else
            echo "  Skipping shell configuration."
            RC_FILE=""
        fi
    fi

    if [ -n "$RC_FILE" ]; then
        # Backup RC file if it exists
        if [ -f "$RC_FILE" ]; then
            cp "$RC_FILE" "$RC_FILE.backup-$(date +%Y%m%d-%H%M%S)"
            echo -e "${GREEN}✓ Backed up $RC_FILE${NC}"
        else
            echo -e "${GREEN}✓ Creating new $RC_FILE${NC}"
        fi

        # Add ClaudeQ configuration
        cat >> "$RC_FILE" << EOF

# ClaudeQ PTY - Scrollable in IntelliJ! 🎯
# Uses PTY (no tmux) with native scrolling
# Server in IntelliJ, client in any terminal
# Usage: cq <tag> [message] or claudeq <tag> [message]
cq() {
    if [ \$# -eq 0 ]; then
        echo "Error: Tag is required"
        echo "Usage: cq <tag> [message]"
        echo "Example (server): cq my-feature"
        echo "Example (client): cq my-feature 'hello Claude'"
        return 1
    fi
    $SRC_DIR/claudeq-main-pty.sh "\$@"
}

# Alias for convenience
alias claudeq='cq'
EOF
        echo -e "${GREEN}✓ Added ClaudeQ configuration to $RC_FILE${NC}"
        echo "  Using project directory: $SRC_DIR"
    fi
fi

echo ""
echo "================================================"
echo -e "${GREEN}✓ ClaudeQ PTY installed successfully!${NC}"
echo "================================================"
echo ""
echo "To start using ClaudeQ:"
echo "  1. Reload your shell: source $RC_FILE"
echo "  2. Run: cq <tag-name>"
echo ""
echo "Examples:"
echo "  Terminal 1 (IntelliJ): cq my-feature     # Start server with scrolling"
echo "  Terminal 2 (any):      cq my-feature     # Interactive client"
echo "  Terminal 3 (any):      cq my-feature 'msg'  # Send message"
echo ""
echo "✨ Features:"
echo "  🖱️  Native scrolling in IntelliJ"
echo "  📝 Smart message queueing"
echo "  🖼️  Image support (:ip command)"
echo "  🔌 Client-server via Unix sockets"
echo ""
echo "⚠️  Important: Keep the ClaudeQ project in its current location:"
echo "   $SCRIPT_DIR"
echo "   Moving or deleting it will break ClaudeQ."
echo ""
echo "For more info, visit: https://github.com/nevo24/claudeq"
echo ""
