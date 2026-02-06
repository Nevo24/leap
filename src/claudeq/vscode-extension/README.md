# ClaudeQ Terminal Selector Extension

Minimal VS Code extension that allows ClaudeQ Monitor to programmatically select terminal tabs by name.

## What it does

Provides a single command `claudeq.selectTerminal` that:
- Takes a terminal name as an argument
- Finds the matching terminal tab
- Focuses that terminal in the VS Code UI

## Installation

This extension is automatically installed when you run `make install` in the ClaudeQ project.

Manual installation (if needed):
```bash
code --install-extension /path/to/claudeq/src/claudeq/vscode-extension
```

## Usage

The extension is used internally by ClaudeQ Monitor. When you click a VS Code session in the monitor, it executes:

```bash
code --command claudeq.selectTerminal --args "cq-server mytag"
```

This switches to the terminal tab with that name.

## Uninstallation

```bash
code --uninstall-extension claudeq.claudeq-terminal-selector
```

Or uninstall via VS Code Extensions panel: Search for "ClaudeQ Terminal Selector" and click Uninstall.
