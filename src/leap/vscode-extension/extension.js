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
 * True only when this extension host is Cursor (not VS Code).
 * The same .vsix is installed in both editors and they share one
 * request file, so any Cursor-specific behavior must gate on this.
 */
function isCursor() {
    try {
        const name = (vscode.env.appName || '').toLowerCase();
        return name.includes('cursor') || vscode.env.uriScheme === 'cursor';
    } catch (e) {
        return false;
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
        } else if (content.startsWith('focusComposer:')) {
            // Cursor only: focus a specific Agent/Composer tab by id.
            // The request file is shared with VS Code, so a VS Code
            // window must NOT consume it — return early (before the
            // unlink below) and leave it for a Cursor window to handle.
            // requireFocus (above) plus this guard mean only the
            // foreground Cursor window (the one the monitor just raised,
            // which owns the tab) acts on it.
            if (!isCursor()) {
                return;
            }
            focusComposer(content.substring('focusComposer:'.length));
        } else if (content.startsWith('closeComposer:')) {
            // Cursor only (same shared-file rationale as focusComposer):
            // a VS Code window must not consume it.
            if (!isCursor()) {
                return;
            }
            closeComposer(content.substring('closeComposer:'.length));
        } else if (content.startsWith('focusChatSession:')) {
            // VS Code only (the mirror image of focusComposer): open a
            // Copilot Chat session by id. A Cursor window must NOT
            // consume the shared request file - leave it for the
            // VS Code window the monitor just raised.
            if (isCursor()) {
                return;
            }
            focusChatSession(content.substring('focusChatSession:'.length));
        } else if (content.startsWith('renameChatSession:')) {
            // VS Code only (same shared-file rationale as above).
            if (isCursor()) {
                return;
            }
            renameChatSession(content.substring('renameChatSession:'.length));
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
    log('Leap extension v1.7.0 activated');
    log(`Watching for: ${REQUEST_FILE}`);

    // Register command (for manual use via command palette)
    const disposable = vscode.commands.registerCommand('leap.selectTerminal', async (terminalName) => {
        const terminal = findTerminal(terminalName);
        if (terminal) {
            terminal.show(false);
        }
    });
    context.subscriptions.push(disposable);

    // Manual test command: prompts for a composer id and focuses that
    // Agent tab. Lets you verify the Cursor focus-by-id commands work in
    // your Cursor build, independent of the monitor wiring.
    const focusDisposable = vscode.commands.registerCommand('leap.focusCursorComposer', async () => {
        const id = await vscode.window.showInputBox({
            prompt: 'Composer id to focus (from a Leap monitor Cursor row)',
            placeHolder: 'e.g. c00c071b-ec6e-46f7-bc26-dd10cf8b458f',
            ignoreFocusOut: true,
        });
        if (id && id.trim()) {
            await focusComposer(id.trim());
        }
    });
    context.subscriptions.push(focusDisposable);

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
            log('Terminal auto-rename: onDidWriteTerminalData unavailable; using Terminal.name matching');
        }
    } catch (err) {
        // onDidWriteTerminalData is a *proposed* API.  Installed extensions
        // (not dev-mode, not built-in) can't use it in VS Code OR Cursor, so
        // the call above throws - this is expected, not a real failure.  Tab
        // auto-rename then relies on Terminal.name matching (VS Code/Cursor
        // already surface the shell's OSC `lps <tag>` title in the tab name,
        // which is what findTerminal() matches on).  Detect that specific
        // case and log it calmly instead of dumping a scary stack.
        const msg = String(err && err.message ? err.message : err);
        if (msg.indexOf('terminalDataWriteEvent') !== -1
            || msg.indexOf('API proposal') !== -1) {
            log('Terminal auto-rename: onDidWriteTerminalData proposed API '
                + 'is not available to installed extensions (expected); '
                + 'using Terminal.name matching instead');
        } else {
            log(`Could not register terminal data listener: ${err}`);
        }
    }
}

/**
 * Cursor only: focus an existing Agent/Composer tab by its composer id.
 *
 * Cursor registers id-based composer-focus commands in its command
 * registry (`composer.openComposer` takes `{type:'local', id}`;
 * `glass.openAgentById` takes the bare id and looks the tab up). Both
 * are reachable via the standard extension command API. We try them in
 * order and stop at the first that doesn't throw. There is no public
 * API for this, so it is best-effort: a Cursor version that renames or
 * gates these commands simply leaves the monitor at window-level focus.
 */
async function focusComposer(composerId) {
    if (!composerId) {
        return;
    }
    if (!isCursor()) {
        // The composer commands only exist in Cursor; never attempt them
        // in VS Code (defensive - the manual test command routes here too).
        log('focusComposer: not running in Cursor, ignoring');
        return;
    }
    // Order matters: only `composer.openComposer` with the BARE id string
    // reaches Cursor's `openComposerImpl` fast-path
    //   selectedComposerIds.includes(id) -> showAndFocus(id)
    // which is the actual visible tab switch. (Passing an object skips
    // that branch - which is why glass.openAgentById "succeeds" but never
    // switches: internally it calls openComposer({type,id}).) The
    // notification command is Cursor's own "return to this chat" action.
    const attempts = [
        ['composer.openComposer', composerId],
        ['composer.openComposerFromNotification', { composerId: composerId }],
        ['glass.openAgentById', composerId],
    ];
    for (const [cmd, arg] of attempts) {
        try {
            await vscode.commands.executeCommand(cmd, arg);
            log(`focusComposer: ${cmd} succeeded for ${composerId}`);
            return;
        } catch (err) {
            log(`focusComposer: ${cmd} failed: ${err}`);
        }
    }
    log(`focusComposer: no command succeeded for ${composerId}`);
}

/**
 * VS Code only: open an existing Copilot Chat session by its session id.
 *
 * VS Code addresses local chat sessions as
 * `vscode-chat-session://local/<base64url(sessionId)>` resources and
 * registers a chat editor for that scheme, so a plain `vscode.open`
 * shows the session (as an editor tab). There is no public chat-session
 * API, so it is best-effort: a VS Code version that changes the scheme
 * simply leaves the monitor at window-level focus.
 */
async function focusChatSession(sessionId) {
    if (!sessionId) {
        return;
    }
    if (isCursor()) {
        log('focusChatSession: running in Cursor, ignoring');
        return;
    }
    const uri = chatSessionUri(sessionId);
    try {
        await vscode.commands.executeCommand('vscode.open', uri);
        log(`focusChatSession: opened ${sessionId}`);
    } catch (err) {
        log(`focusChatSession: vscode.open failed for ${sessionId}: ${err}`);
    }
}

/**
 * VS Code addresses local chat sessions as
 * `vscode-chat-session://local/<base64url(sessionId)>`.
 */
function chatSessionUri(sessionId) {
    const b64 = Buffer.from(sessionId, 'utf8').toString('base64')
        .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return vscode.Uri.parse(`vscode-chat-session://local/${b64}`);
}

/**
 * VS Code only: open VS Code's own rename input for a chat session.
 *
 * `agentSession.rename` resolves its target from a marshalled
 * `{$mid: 25, session: {resource}}` argument (a structural check on the
 * main-thread side), shows the native input box pre-filled with the
 * current title, and persists via `setChatSessionTitle`. The new title
 * lands in the chat-session store, so the Leap monitor row label
 * follows on its next scan. Best-effort, like every command here that
 * leans on an undocumented internal: a VS Code that changes the
 * argument shape just logs and does nothing.
 */
async function renameChatSession(sessionId) {
    if (!sessionId) {
        return;
    }
    if (isCursor()) {
        log('renameChatSession: running in Cursor, ignoring');
        return;
    }
    const uri = chatSessionUri(sessionId);
    try {
        await vscode.commands.executeCommand(
            'agentSession.rename', { $mid: 25, session: { resource: uri } });
        log(`renameChatSession: rename flow opened for ${sessionId}`);
    } catch (err) {
        log(`renameChatSession: failed for ${sessionId}: ${err}`);
    }
}

/**
 * Cursor only: close an Agent/Composer tab by its composer id.
 *
 * `composer.closeComposerTab` takes the bare id (falls back to the active
 * composer if not a string) and closes that tab - the chat stays in
 * Cursor's history (this is a close, not the destructive deleteComposer).
 */
async function closeComposer(composerId) {
    if (!composerId) {
        return;
    }
    if (!isCursor()) {
        log('closeComposer: not running in Cursor, ignoring');
        return;
    }
    try {
        await vscode.commands.executeCommand('composer.closeComposerTab', composerId);
        log(`closeComposer: composer.closeComposerTab succeeded for ${composerId}`);
    } catch (err) {
        log(`closeComposer: composer.closeComposerTab failed: ${err}`);
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
