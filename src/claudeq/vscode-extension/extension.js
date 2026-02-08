/**
 * ClaudeQ Terminal Selector Extension
 *
 * Allows ClaudeQ Monitor to programmatically select a terminal tab by name.
 * Watches ~/.claudeq-terminal-request file for terminal selection requests.
 */

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const REQUEST_FILE = path.join(os.homedir(), '.claudeq-terminal-request');

/**
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
    // Register command (for manual use via command palette)
    let disposable = vscode.commands.registerCommand('claudeq.selectTerminal', async (terminalName) => {
        selectTerminalByName(terminalName);
    });

    context.subscriptions.push(disposable);

    // Watch for request file changes
    let watcher;
    try {
        watcher = fs.watch(path.dirname(REQUEST_FILE), (eventType, filename) => {
            if (filename === '.claudeq-terminal-request' && fs.existsSync(REQUEST_FILE)) {
                try {
                    const content = fs.readFileSync(REQUEST_FILE, 'utf8').trim();
                    if (content) {
                        if (content.startsWith('close:')) {
                            const terminalName = content.substring(6);
                            closeTerminalByName(terminalName);
                        } else {
                            selectTerminalByName(content);
                        }
                        // Delete the request file after processing
                        fs.unlinkSync(REQUEST_FILE);
                    }
                } catch (err) {
                    console.error('ClaudeQ: Error processing request:', err);
                }
            }
        });
    } catch (err) {
        console.error('ClaudeQ: Error setting up file watcher:', err);
    }

    context.subscriptions.push({ dispose: () => watcher && watcher.close() });
}

function closeTerminalByName(terminalName) {
    const terminals = vscode.window.terminals;

    if (!terminals || terminals.length === 0) {
        return;
    }

    const terminal = terminals.find(t => t.name && t.name.includes(terminalName));

    if (terminal) {
        terminal.dispose();
    }
}

function selectTerminalByName(terminalName) {
    const terminals = vscode.window.terminals;

    if (!terminals || terminals.length === 0) {
        return;
    }

    // Find terminal by name (supports partial matching)
    const terminal = terminals.find(t => t.name && t.name.includes(terminalName));

    if (terminal) {
        // Show and focus the terminal
        terminal.show(false); // false = don't take focus from editor
    }
}

function deactivate() {}

module.exports = {
    activate,
    deactivate
};
