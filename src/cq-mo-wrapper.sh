#!/bin/bash
#
# ClaudeQ Monitor Wrapper - Checks if monitor dependencies are installed
#

# Check if FreeSimpleGUI is installed
if ! "$CLAUDEQ_PYTHON" -c "import FreeSimpleGUI" 2>/dev/null; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ClaudeQ Monitor (cq-mo) - Not Installed"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "The monitor GUI requires additional dependencies."
    echo ""
    echo "To install the monitor, run:"
    echo "  cd $CLAUDEQ_PROJECT_DIR"
    echo "  make install-monitor"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 1
fi

# Dependencies are installed, run the monitor
"$CLAUDEQ_PYTHON" "$CLAUDEQ_PROJECT_DIR/src/claudeq-monitor.py"
