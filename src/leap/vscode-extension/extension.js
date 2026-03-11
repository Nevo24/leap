/**
 * Leap Terminal Selector Extension
 *
 * Allows Leap Monitor to programmatically select a terminal tab by name.
 * Watches ~/.leap-terminal-request file for terminal selection requests.
 */

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const REQUEST_FILE = path.join(os.homedir(), '.leap-terminal-request');
let outputChannel;

function log(msg) {
    if (outputChannel) {
        outputChannel.appendLine(`[${new Date().toISOString()}] ${msg}`);
    }
}

/**
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
    outputChannel = vscode.window.createOutputChannel('Leap');
    log('Leap extension v1.2.0 activated');
    log(`Watching for: ${REQUEST_FILE}`);

    // Register command (for manual use via command palette)
    let disposable = vscode.commands.registerCommand('leap.selectTerminal', async (terminalName) => {
        selectTerminalByName(terminalName);
    });

    context.subscriptions.push(disposable);

    // Watch for request file changes
    let watcher;
    try {
        watcher = fs.watch(path.dirname(REQUEST_FILE), (eventType, filename) => {
            if (filename === '.leap-terminal-request' && fs.existsSync(REQUEST_FILE)) {
                try {
                    const content = fs.readFileSync(REQUEST_FILE, 'utf8').trim();
                    log(`Request received: "${content}" (event: ${eventType})`);
                    if (content) {
                        if (content.startsWith('close:')) {
                            const terminalName = content.substring(6);
                            closeTerminalByName(terminalName);
                        } else if (content.startsWith('open:')) {
                            const command = content.substring(5);
                            log(`Opening terminal with command: "${command}"`);
                            openTerminalWithCommand(command);
                        } else {
                            selectTerminalByName(content);
                        }
                        // Delete the request file after processing
                        fs.unlinkSync(REQUEST_FILE);
                    }
                } catch (err) {
                    log(`Error processing request: ${err}`);
                    console.error('Leap: Error processing request:', err);
                }
            }
        });
        log('File watcher started on home directory');
    } catch (err) {
        log(`Error setting up file watcher: ${err}`);
        console.error('Leap: Error setting up file watcher:', err);
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

function openTerminalWithCommand(command) {
    try {
        const terminal = vscode.window.createTerminal();
        terminal.sendText(command);
        terminal.show();
        log(`Terminal created and shown with command: "${command}"`);
    } catch (err) {
        log(`Error creating terminal: ${err}`);
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
        // Show the terminal panel (switches away from any plugin panel)
        // and focus it. preserveFocus=false means the terminal gets focus.
        terminal.show(false);
    }
}

function deactivate() {}

module.exports = {
    activate,
    deactivate
};
