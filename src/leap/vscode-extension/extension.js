/**
 * Leap Terminal Selector Extension
 *
 * Allows Leap Monitor to programmatically select a terminal tab by name.
 * Watches ~/.leap-terminal-request file for terminal selection requests.
 * Auto-renames Leap terminal tabs by detecting OSC title sequences in output.
 */

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');

const REQUEST_FILE = path.join(os.homedir(), '.leap-terminal-request');
let outputChannel;

/** Map of Terminal → Leap title (for terminals pending rename) */
const leapTerminalNames = new Map();

function log(msg) {
    if (outputChannel) {
        outputChannel.appendLine(`[${new Date().toISOString()}] ${msg}`);
    }
}

/**
 * Find a terminal matching the given name.
 * Checks Terminal.name first, then our tracked Leap names map.
 */
function findTerminal(terminalName) {
    const terminals = vscode.window.terminals;
    if (!terminals || terminals.length === 0) {
        return null;
    }

    // 1. Direct name match (works after rename or if OSC updates Terminal.name)
    const byName = terminals.find(t => t.name && t.name.includes(terminalName));
    if (byName) {
        return byName;
    }

    // 2. Check our tracked names (for terminals detected but not yet renamed)
    for (const [terminal, title] of leapTerminalNames) {
        if (title.includes(terminalName)) {
            return terminal;
        }
    }

    return null;
}

/**
 * Process the request file if it exists.
 * Shared by the fs.watch callback and the polling fallback.
 *
 * @param {boolean} requireFocus - If true, only process rename/open commands
 *   when this VS Code window is focused. This prevents the polling fallback
 *   in a background window from stealing rename requests meant for the
 *   foreground window where the user actually typed `leap`.
 */
function processRequestFile(requireFocus) {
    if (!fs.existsSync(REQUEST_FILE)) {
        return;
    }
    try {
        const content = fs.readFileSync(REQUEST_FILE, 'utf8').trim();
        if (!content) {
            return;
        }

        // When called from the poll timer, only act if this window is focused.
        // This prevents a background VS Code window from grabbing the request
        // and renaming/opening in the wrong window.
        if (requireFocus && !vscode.window.state.focused) {
            return;
        }

        log(`Request received: "${content}"`);
        if (content.startsWith('close:')) {
            closeTerminalByName(content.substring(6));
        } else if (content.startsWith('open:')) {
            log(`Opening terminal with command: "${content.substring(5)}"`);
            openTerminalWithCommand(content.substring(5));
        } else if (content.startsWith('rename:')) {
            renameActiveTerminal(content.substring(7));
        } else {
            selectTerminalByName(content);
        }
        fs.unlinkSync(REQUEST_FILE);
    } catch (err) {
        log(`Error processing request: ${err}`);
    }
}

/**
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
    outputChannel = vscode.window.createOutputChannel('Leap');
    log('Leap extension v1.5.0 activated');
    log(`Watching for: ${REQUEST_FILE}`);

    // Register command (for manual use via command palette)
    const disposable = vscode.commands.registerCommand('leap.selectTerminal', async (terminalName) => {
        const terminal = findTerminal(terminalName);
        if (terminal) {
            terminal.show(false);
        }
    });
    context.subscriptions.push(disposable);

    // Watch for request file changes (monitor → extension communication)
    let watcher;
    try {
        watcher = fs.watch(path.dirname(REQUEST_FILE), (eventType, filename) => {
            if (filename === '.leap-terminal-request') {
                processRequestFile(true);
            }
        });
        log('File watcher started on home directory');
    } catch (err) {
        log(`Error setting up file watcher: ${err}`);
    }
    context.subscriptions.push({ dispose: () => watcher && watcher.close() });

    // Polling fallback — macOS FSEvents on the home directory can miss rapid
    // file create/delete cycles (e.g. server rename then client rename).
    // Poll every 500ms as a safety net.
    const pollInterval = setInterval(() => {
        processRequestFile(true);
    }, 500);
    context.subscriptions.push({ dispose: () => clearInterval(pollInterval) });

    // Auto-rename Leap terminal tabs when OSC title sequences are detected.
    // This avoids needing the global terminal.integrated.tabs.title = "${sequence}"
    // setting, which would affect ALL terminals (showing ugly full paths).
    try {
        if (typeof vscode.window.onDidWriteTerminalData === 'function') {
            const dataListener = vscode.window.onDidWriteTerminalData(e => {
                // Fast check: skip data without OSC sequences
                if (!e.data.includes('\x1b]')) {
                    return;
                }

                // Match OSC 0/2 title: \x1b]0;lps tag\x07
                const match = e.data.match(/\x1b\](?:0|2);(lp[sc] [^\x07\x1b]+)(?:\x07|\x1b\\)/);
                if (!match) {
                    return;
                }

                const title = match[1];

                // Skip if already renamed to this title
                if (leapTerminalNames.get(e.terminal) === title) {
                    return;
                }

                leapTerminalNames.set(e.terminal, title);
                log(`Detected Leap terminal: "${title}"`);

                // Rename the tab if this terminal is currently active
                if (vscode.window.activeTerminal === e.terminal) {
                    vscode.commands.executeCommand(
                        'workbench.action.terminal.renameWithArg',
                        { name: title }
                    );
                }
            });
            context.subscriptions.push(dataListener);

            // Apply pending renames when a terminal becomes active
            const activeListener = vscode.window.onDidChangeActiveTerminal(terminal => {
                if (terminal && leapTerminalNames.has(terminal)) {
                    const title = leapTerminalNames.get(terminal);
                    if (terminal.name !== title) {
                        vscode.commands.executeCommand(
                            'workbench.action.terminal.renameWithArg',
                            { name: title }
                        );
                    }
                }
            });
            context.subscriptions.push(activeListener);

            // Clean up when terminals close
            const closeListener = vscode.window.onDidCloseTerminal(terminal => {
                leapTerminalNames.delete(terminal);
            });
            context.subscriptions.push(closeListener);

            log('Terminal data listener registered (auto-rename enabled)');
        } else {
            log('onDidWriteTerminalData not available, relying on Terminal.name matching');
        }
    } catch (err) {
        log(`Could not register terminal data listener: ${err}`);
    }
}

function renameActiveTerminal(title) {
    const terminal = vscode.window.activeTerminal;
    if (terminal) {
        leapTerminalNames.set(terminal, title);
        log(`Renaming active terminal to: "${title}"`);
        vscode.commands.executeCommand(
            'workbench.action.terminal.renameWithArg',
            { name: title }
        );
    } else {
        log(`No active terminal to rename to: "${title}"`);
    }
}

function closeTerminalByName(terminalName) {
    const terminal = findTerminal(terminalName);
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
    const terminal = findTerminal(terminalName);
    if (terminal) {
        terminal.show(false);
    }
}

function deactivate() {}

module.exports = {
    activate,
    deactivate
};
