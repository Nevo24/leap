# Leap Terminal Selector Extension

Minimal VS Code extension that allows Leap Monitor to programmatically select terminal tabs by name.

## What it does

Provides a single command `leap.selectTerminal` that:
- Takes a terminal name as an argument
- Finds the matching terminal tab
- Focuses that terminal in the VS Code UI

## Installation

This extension is automatically installed when you run `make install` in the Leap project.

Manual installation (if needed):
```bash
code --install-extension /path/to/leap/src/leap/vscode-extension
```

## Usage

The extension is used internally by Leap Monitor. When you click a VS Code session in the monitor, it executes:

```bash
code --command leap.selectTerminal --args "lps mytag"
```

This switches to the terminal tab with that name.

## Uninstallation

```bash
code --uninstall-extension leap.leap-terminal-selector
```

Or uninstall via VS Code Extensions panel: Search for "Leap Terminal Selector" and click Uninstall.
