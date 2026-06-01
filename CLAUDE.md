# Leap

PTY-based client-server system for managing AI CLI sessions (Claude Code, OpenAI Codex, Cursor Agent, Gemini CLI) with message queueing, image support, and native IDE scrolling.

## Quick Start

```bash
make install                # Install core
make install-monitor        # Install GUI (optional)
source ~/.zshrc             # Reload shell

leap mytag                       # Terminal 1: Select CLI + start server
leap mytag                       # Terminal 2: Connect client
leap                             # Interactive: choose CLI + session name

leap --reconfigure               # After installing a new CLI/IDE/terminal post-Leap
```

**Installed a new CLI / IDE / terminal after Leap?** Run `leap --reconfigure`. The install-time configures (`make install`) skip anything that wasn't on disk at the time, so newly-installed tools have no hook integration. The session-start gate in `leap-server.py` will refuse to spawn the server for a CLI whose hooks aren't wired up, with a stderr error pointing here.

## Project Structure

```
src/
├── scripts/                     # Entry point scripts
│   ├── leap-main.sh          # Main launcher (called by 'leap' command)
│   ├── leap-resume.py        # `leap --resume` picker (interactive + pre-pick GUI modes; cwd-choice for cwd-bound CLIs)
│   ├── leap-hook-process.py  # Hook processor (session recording, Slack last-message extraction)
│   ├── leap-cleanup.sh       # Dead session cleanup
│   ├── _leap                 # zsh completion for user-facing flags
│   ├── leap-server.py        # Thin launcher → LeapServer
│   ├── leap-client.py        # Thin launcher → LeapClient
│   ├── leap-monitor.py       # Thin launcher → MonitorWindow
│   ├── leap-slack.py         # Thin launcher → SlackBot
│   ├── leap_monitor_launcher.py  # py2app entry point
│   ├── setup-slack-app.sh       # Interactive Slack app setup wizard
│   ├── configure_jetbrains_xml.py   # JetBrains IDE auto-configuration
│   ├── configure_hooks.py           # Unified hook config (delegates to provider.configure_hooks())
│   ├── configure_claude_hooks.py    # Legacy Claude hook config
│   ├── configure_codex_hooks.py     # Legacy Codex hook config
│   └── leap-hook.sh             # CLI hook script (writes state to signal file)
│
└── leap/                     # Main Python package
    ├── __init__.py              # Version, exports
    ├── main.py                  # Package entry point
    │
    ├── cli_providers/           # CLI backend abstraction (Strategy pattern)
    │   ├── __init__.py          # Package exports, get_provider(), list_providers()
    │   ├── base.py              # CLIProvider ABC (patterns, timings, hooks, input)
    │   ├── claude.py            # Claude Code provider (Ink TUI, numbered menus)
    │   ├── codex.py             # OpenAI Codex provider (Ratatui TUI, y/n approval)
    │   ├── cursor_agent.py     # Cursor Agent provider (Ink TUI, menu approval)
    │   ├── gemini.py            # Gemini CLI provider (Ink TUI, radio-button approval)
    │   ├── registry.py          # Provider registry (name → class lookup)
    │   └── states.py            # CLIState enum + state groupings (WAITING/SIGNAL/PROMPT)
    │
    ├── utils/                   # Shared utilities
    │   ├── constants.py         # QUEUE_DIR, SOCKET_DIR, timing, colors, is_valid_tag()
    │   ├── terminal.py          # Terminal title, banner
    │   ├── ide_detection.py     # IDE detection, git branch
    │   ├── line_buffer.py       # Cursor-aware line editing buffer (raw-terminal prompts)
    │   ├── menu.py              # Numbered-menu parser (extract_menu_options, shared by server + monitor)
    │   ├── socket_utils.py      # Shared Unix socket send/recv helper
    │   ├── resume_store.py      # Read/write/prune of cli_sessions/<cli>/<tag>.json (used by hook + picker)
    │   ├── relocation.py        # Shared primitives for cross-cwd session moves (signals_blocked, stage/commit, verify, snapshots)
    │   ├── claude_session_move.py  # Claude cross-cwd move (jsonl + optional sidecar dir)
    │   ├── gemini_session_move.py  # Gemini cross-cwd move (jsonl + projects.json registry)
    │   └── cursor_session_move.py  # Cursor cross-cwd move (whole chat directory tree)
    │
    ├── server/                  # PTY Server
    │   ├── server.py            # LeapServer - main orchestrator
    │   ├── pty_handler.py       # CLI PTY (pexpect, provider-driven)
    │   ├── socket_handler.py    # Unix socket server
    │   ├── queue_manager.py     # Message queue persistence
    │   └── metadata.py          # Session metadata (IDE, project, branch, cli_provider)
    │
    ├── client/                  # Interactive Client
    │   ├── client.py            # LeapClient - main class
    │   ├── socket_client.py     # Unix socket client
    │   ├── input_handler.py     # Prompt toolkit / readline
    │   └── image_handler.py     # Clipboard image handling
    │
    ├── monitor/                 # GUI Monitor (PyQt5)
    │   ├── app.py               # MonitorWindow (core window + UI init + lifecycle)
    │   ├── server_launcher.py   # PR server clone/checkout/start flow
    │   ├── session_manager.py   # Session discovery + read_client_pid()
    │   ├── scm_polling.py       # SCM poller + background workers (SessionRefreshWorker also scans Cursor GUI agent tabs when enabled)
    │   ├── cursor_gui_scan.py   # Read-only scan of Cursor editor Agent/Composer tabs (SQLite on disk) -> synthetic monitor rows
    │   ├── leap_sender.py       # Socket sender for /leap commands + message bundles
    │   ├── navigation.py        # IDE terminal navigation (+ focus_cursor_window / close_cursor_composer for Cursor GUI rows)
    │   ├── monitor_utils.py     # Utilities (icon finder, lock removal)
    │   ├── themes.py            # Visual theme definitions (9 built-in themes, manager API)
    │   ├── permissions.py       # macOS Accessibility + Notifications permission checks
    │   ├── sleep_guard.py       # SleepGuard (caffeinate) + LidCloseGuard (pmset disablesleep)
    │   ├── sudo_manager.py      # Saved sudo password for LidCloseGuard (.storage/sudo_pass.b64, base64 mode 0600)
    │   │
    │   ├── _mixins/             # MonitorWindow mixin classes
    │   │   ├── actions_menu_mixin.py  # Git menu (branch col) + Path menu (Open in Terminal/IDE, Move-to-IDE)
    │   │   ├── scm_config_mixin.py    # SCM provider init, setup dialogs, toggles
    │   │   ├── session_mixin.py       # Session merge, navigate, close, delete
    │   │   ├── pr_tracking_mixin.py   # PR tracking, polling, thread send, add-row
    │   │   ├── pr_display_mixin.py    # PR column styling, dock badge, banners
    │   │   ├── notifications_mixin.py # User notification handling
    │   │   └── table_builder_mixin.py # Table build, refresh, settings
    │   │
    │   ├── dialogs/             # Dialog windows
    │   │   ├── git_changes_dialog.py  # Git diff viewer (local, commit, vs main)
    │   │   ├── settings_dialog.py     # Settings (terminal, repos dir, diff tool, etc.)
    │   │   ├── notifications_dialog.py # Per-type notification config (dock/banner)
    │   │   ├── scm_setup_dialog.py    # Abstract SCM setup base dialog
    │   │   ├── gitlab_setup_dialog.py # GitLab connection dialog
    │   │   ├── github_setup_dialog.py # GitHub connection dialog
    │   │   ├── scm_template_dialog.py # Preset editor dialog (PR context + message bundles)
    │   │   ├── add_local_dialog.py    # Add session from local path dialog
    │   │   ├── resume_session_dialog.py # GUI `leap --resume` picker (returns (cli, tag, SessionRecord))
    │   │   ├── branch_picker_dialog.py # Branch picker for git difftool comparison
    │   │   ├── queue_edit_dialog.py   # Queue message editor dialog
    │   │   ├── send_comments_dialog.py # PR comments picker (filter / mode / context-preset)
    │   │   ├── whats_new_dialog.py    # "See what's new" dialog (lists HEAD..origin/main commits)
    │   │   ├── notes_dialog.py        # NotesDialog class (helpers in notes/ sub-package)
    │   │   ├── notes_undo.py          # Undo/redo command-pattern stack for Notes dialog
    │   │   └── notes/                 # Notes-dialog sub-package
    │   │       ├── __init__.py             # Package skeleton
    │   │       ├── rtl.py                  # Directional-text detection for QLineEdits
    │   │       ├── persistence.py          # FS helpers (note paths, listing, mtime, meta)
    │   │       ├── ordering.py             # Folder + per-folder child ordering
    │   │       ├── text_helpers.py         # Markdown link/bold helpers + URL highlighter
    │   │       ├── image_helpers.py        # Note-image save / refs / cleanup / preview popup
    │   │       ├── note_text_edit.py       # _NoteTextEdit rich editor (image paste, links, Cmd+B/C)
    │   │       ├── checklist_io.py         # _parse_checklist / _serialize_checklist round-trip
    │   │       ├── checklist_widgets.py    # Google Keep-style checklist editor (4 inter-referencing classes)
    │   │       ├── tree_widget.py          # _NotesTreeWidget — left-panel QTreeWidget with custom DnD
    │   │       └── session_picker.py       # _SessionPickerDialog — modal picker for "Run in Session"
    │   │
    │   ├── ui/                  # UI components
    │   │   ├── ui_widgets.py    # PulsingLabel, IndicatorLabel
    │   │   ├── dock_badge.py    # Dock icon badge overlay + notification event detection
    │   │   ├── image_text_edit.py # ImageTextEdit (clipboard image paste) + SendMessageDialog + SendPresetDialog
    │   │   ├── log_history.py   # Log history (in-memory + dialog)
    │   │   └── table_helpers.py # Qt helper widgets (separators, tooltip overrides, ColorPickerPopup)
    │   │
    │   ├── pr_tracking/         # PR tracking subsystem
    │   │   ├── base.py          # Abstract SCMProvider, PRState, PRStatus, PRDetails
    │   │   ├── config.py        # GitLab/monitor prefs + pinned sessions persistence
    │   │   ├── gitlab_provider.py # GitLab API implementation
    │   │   ├── github_provider.py # GitHub API implementation
    │   │   ├── git_utils.py     # Git remote URL parsing + PR URL parsing
    │   │   └── leap_command.py    # /leap command data model + formatting
    │   └── resources/
    │       └── activate_terminal.groovy  # JetBrains script
    │
    ├── slack/                   # Slack Integration
    │   ├── __init__.py          # Package init
    │   ├── bot.py               # SlackBot main class (Socket Mode)
    │   ├── config.py            # Slack config + session persistence
    │   ├── output_capture.py    # Capture hook response, write .last_response for Slack bot
    │   ├── output_watcher.py    # Poll .last_response files → post to Slack
    │   └── message_router.py    # Route Slack messages → Leap sessions
    │
    └── vscode-extension/        # VS Code / Cursor Extension
        ├── package.json         # Extension metadata
        ├── extension.js         # Terminal selector logic
        └── README.md            # Extension documentation

tests/
├── __init__.py
└── test_state_tracker.py        # CLIStateTracker state machine tests

assets/
├── leap-icon.png             # Source icon (1024x1024)
├── leap-icon.icns            # macOS icon bundle
├── leap-simple-icon.png      # Alternate flat icon
└── leap-exclusive-icon.png   # Alternate exclusive icon
```

## Key Classes

| Class / Function | File | Purpose |
|------------------|------|---------|
| `CLIState` | `cli_providers/states.py` | State enum (`idle`, `running`, `needs_permission`, `needs_input`, `interrupted`) |
| `CLIProvider` | `cli_providers/base.py` | Abstract base for CLI backends (patterns, hooks, input) |
| `ClaudeProvider` | `cli_providers/claude.py` | Claude Code CLI (Ink TUI, numbered menus, Notification + PermissionRequest hooks) |
| `CodexProvider` | `cli_providers/codex.py` | OpenAI Codex CLI (Ratatui TUI, y/n approval, Stop hook only) |
| `CursorAgentProvider` | `cli_providers/cursor_agent.py` | Cursor Agent CLI (Ink TUI, menu approval, Stop hook only) |
| `GeminiProvider` | `cli_providers/gemini.py` | Gemini CLI (Ink TUI, radio-button approval, AfterAgent/Notification hooks) |
| `get_provider()` | `cli_providers/registry.py` | Provider lookup by name (`'claude'`, `'codex'`, `'cursor-agent'`, `'gemini'`) |
| `LeapServer` | `server/server.py` | Orchestrates PTY, socket, queue, metadata |
| `LeapClient` | `client/client.py` | Interactive client with image support |
| `SocketClient` | `client/socket_client.py` | Client-side socket communication (shared `_send_request`) |
| `MonitorWindow` | `monitor/app.py` | PyQt5 GUI core window (uses mixins for methods) |
| `ServerLauncher` | `monitor/server_launcher.py` | PR server clone/force-align/start flow (gates dirty managed clones on a 3-way dialog: Clone-into-next / Discard / Cancel) |
| `_dirty_files()` | `monitor/server_launcher.py` | Returns the list of local files a force-align would discard (`git status --porcelain`); `None` on scan failure so the consent gate stays armed |
| `_commits_ahead_of_origin()` | `monitor/server_launcher.py` | Counts commits on HEAD not in `origin/<branch>` (`git rev-list --count origin/<branch>..HEAD`); `None` on scan failure |
| `_detached_head_sha()` | `monitor/server_launcher.py` | Returns the short SHA if HEAD is detached, else `None` — surfaced in the dialog so commit-URL re-opens don't read as "you have N new commits" |
| `_dir_index()` | `monitor/server_launcher.py` | Numeric suffix of a managed-clone dir name (`<name>` → 0, `<name>_1` → 1, …) — drives the "next slot" logic |
| `GitLabProvider` | `monitor/pr_tracking/gitlab_provider.py` | GitLab PR thread tracking + user notifications |
| `GitHubProvider` | `monitor/pr_tracking/github_provider.py` | GitHub PR thread tracking + user notifications |
| `ActionsMenuMixin` | `monitor/_mixins/actions_menu_mixin.py` | Git menu + Path menu (Open in Terminal / Open in IDE / Move session to IDE) |
| `detect_supported_ide_for_move()` | `monitor/navigation.py` | Classify a `.app` for Move-to-IDE: a canonical JetBrains key / `'VS Code'` / `'Cursor'` (VS Code fork, driven via the `cursor` CLI through `_open_vscode_terminal`) / `None` |
| `GitChangesDialog` | `monitor/dialogs/git_changes_dialog.py` | Git diff viewer (local, commit, vs main) |
| `CommitListDialog` | `monitor/dialogs/git_changes_dialog.py` | Commit picker for diff comparison (More-info button lazy-fetches full body) |
| `WhatsNewDialog` | `monitor/dialogs/whats_new_dialog.py` | Read-only commit viewer for `HEAD..origin/main`, launched from update banner |
| `BranchPickerDialog` | `monitor/dialogs/branch_picker_dialog.py` | Branch picker for difftool comparison |
| `QueueEditDialog` | `monitor/dialogs/queue_edit_dialog.py` | View/edit queued messages for a session |
| `NotesDialog` | `monitor/dialogs/notes_dialog.py` | Notes with folders, search, text/checklist, DnD reorder, save as preset, run in session |
| `ImageTextEdit` | `monitor/ui/image_text_edit.py` | QTextEdit with clipboard image paste → `[Image #N]` placeholders |
| `SendMessageDialog` | `monitor/ui/image_text_edit.py` | Message dialog with image paste + Next/To-End queue-position toggle |
| `SendPresetDialog` | `monitor/ui/image_text_edit.py` | Picker for a message-bundle preset + Next/To-End queue-position toggle |
| `SendCommentsDialog` | `monitor/dialogs/send_comments_dialog.py` | PR-comments picker (filter / mode / context preset) |
| `ResumeSessionDialog` | `monitor/dialogs/resume_session_dialog.py` | GUI `leap --resume` picker — returns `(cli, tag, SessionRecord)` |
| `_TagSessionPicker` | `monitor/dialogs/resume_session_dialog.py` | Sub-dialog for tags with >1 recorded session |
| `SCMSetupDialog` | `monitor/dialogs/scm_setup_dialog.py` | Abstract base: Save / Connect-Disconnect / Cancel actions |
| `ColorPickerPopup` | `monitor/ui/table_helpers.py` | Row color picker popup (grid of swatches + clear) |
| `DockBadge` | `monitor/ui/dock_badge.py` | Dock icon badge overlay + notification event detection |
| `Theme` / `current_theme()` | `monitor/themes.py` | Theme dataclass + manager API (9 built-in themes) |
| `ensure_contrast()` | `monitor/themes.py` | WCAG contrast safety-net (returns black/white if ratio < 4.5:1) |
| `SleepGuard` | `monitor/sleep_guard.py` | Holds `caffeinate -i -w <monitor-pid>` child while any session is RUNNING |
| `LidCloseGuard` | `monitor/sleep_guard.py` | Optional companion to SleepGuard — also runs `sudo pmset -a disablesleep 1/0` |
| `SudoManager` | `monitor/sudo_manager.py` | Saved sudo password helpers (`.storage/sudo_pass.b64`, base64 mode 0600) |
| `SlackBot` | `slack/bot.py` | Main Slack bot (Socket Mode + event handlers) |
| `OutputCapture` | `slack/output_capture.py` | Read hook response from signal file, write .last_response |
| `LineBuffer` | `utils/line_buffer.py` | Cursor-aware line editing buffer (insert, delete, move, home/end, delete-word) |
| `extract_menu_options()` | `utils/menu.py` | Numbered-menu parser shared by server auto-approve and monitor permission menu |
| `relocation.py` primitives | `utils/relocation.py` | Shared cross-cwd move primitives (signals_blocked, stage/commit, verify, snapshot) |
| `relocate_claude_session()` | `utils/claude_session_move.py` | Claude transcript move (jsonl + optional sidecar dir) |
| `relocate_gemini_session()` | `utils/gemini_session_move.py` | Gemini transcript move (jsonl + projects.json registry) |
| `relocate_cursor_session()` | `utils/cursor_session_move.py` | Cursor chat-dir move; also exposes `find_chat_dir()` for `session_exists` |
| `relocate_records()` | `utils/resume_store.py` | Rewrites transcript paths in `cli_sessions/<cli>/*.json` after a cross-cwd move |
| `CLIProvider.requires_cwd_bound_resume` | `cli_providers/base.py` | True for Claude/Gemini/Cursor (cwd-derived storage); False for Codex |
| `CLIProvider.session_exists()` | `cli_providers/base.py` | Existence check for the picker (default: `transcript_path`; Cursor scans chat dir) |
| `CLIProvider.relocate_session()` | `cli_providers/base.py` | Optional hook — implemented by Claude/Gemini/Cursor; Codex inherits None |
| `CLIProvider.hooks_installed()` | `cli_providers/base.py` | Whether Leap's hooks are wired up; gate-checked at session start; must never raise |
| `CLIProvider.input_history()` | `cli_providers/base.py` | Returns the CLI's persisted ↑-recall history for `cwd`, ordered oldest→newest; None opts out → passthrough |
| `_handle_history_recall()` | `server/server.py` | Drives ↑/↓ recall: reads provider history, clears CLI input, injects recalled text, keeps mirror in sync |
| `CLIProvider.base_type` | `cli_providers/base.py` | Built-in CLI this provider is a variant of; custom providers inherit via `__getattribute__` |
| `atomic_write_json()` | `utils/atomic_write.py` | Write JSON to a temp file in the same dir, fsync, atomic rename |
| `_enforce_hooks_installed_or_exit()` | `server/server.py` | Session-start gate — exits with code 1 if `hooks_installed()` returns False |
| `_resolve_cli_flags()` | `server/pty_handler.py` | Merge stored/env-var default flags with explicit CLI flags |
| `send_socket_request()` | `utils/socket_utils.py` | Shared Unix socket send/recv utility |
| `resolve_scm_token()` | `monitor/pr_tracking/config.py` | Resolve token from config (supports env var mode) |
| `normalize_github_api_url()` | `monitor/pr_tracking/config.py` | Canonicalize a GitHub base URL (GitHub Enterprise → `/api/v3`; github.com/api.github.com → default). Applied in-memory on `load_github_config` (no write-back — runs on poll-worker threads) and persisted on `save_github_config` |
| `find_latest_closed_pr()` | `monitor/pr_tracking/base.py` | Most-recent closed/merged PR for a branch (`ClosedPRInfo`); powers the Track-PR "no open PR found" fallback's "Open in Browser" button. Default returns None; GitLab/GitHub override |
| `parse_pr_url()` | `monitor/pr_tracking/git_utils.py` | Parse GitLab/GitHub PR URLs |
| `send_to_leap_session()` | `monitor/leap_sender.py` | Send message to Leap session (prepends PR context) |
| `scan_open_cursor_agents()` | `monitor/cursor_gui_scan.py` | Read-only scan of Cursor editor Agent tabs (workspace.json + workspace/global `state.vscdb`) -> one synthetic `row_type='cursor_agent_gui'` row per open tab (status from `generatingBubbleIds`/`hasUnreadMessages`/`status`) |
| `focus_cursor_window()` | `monitor/navigation.py` | "Jump": raise the Cursor window matching a folder via the System Events AX bridge, then (if a `composer_id` is given) write `focusComposer:<id>` to `~/.leap-terminal-request` (via `_write_terminal_request`, atomic temp+`os.replace`) for the Leap Cursor extension to focus that exact Agent tab |
| `_write_terminal_request()` | `monitor/navigation.py` | Atomic (temp file + `os.replace`) write of a request line to `~/.leap-terminal-request`. Atomicity matters: the extension reads with a plain `readFileSync` on an fs.watch + 500 ms poll, so a non-atomic truncate-then-write could be read half-formed - missing the `focusComposer:`/`closeComposer:` prefix, falling through to the extension's catch-all "select terminal by name", and getting `unlink`-ed (silently dropping the request). Used by `focus_cursor_window`/`close_cursor_composer` |
| `_build_cursor_gui_row()` | `monitor/_mixins/table_builder_mixin.py` | Renders a Cursor-GUI overlay row (all columns painted explicitly; alias-aware Tag cell; QUEUE = mono dimmed "N/A"; CLI badge "Cursor Editor"; Server cell = close-"×" + "Open" jump, mirroring a running row's `[× | Terminal]`); bypasses the normal server-row cell pipeline. Two close buttons: leftmost "×" = `_close_cursor_tab_and_untrack` (stop tracking AND close the tab → row drops); Server-cell "×" = `_close_cursor_tab` (close only the tab → a tracked row survives as a synthesized "tab closed" row) |
| `_reconcile_cursor_gui_rows()` | `monitor/_mixins/table_builder_mixin.py` | Sets `_cursor_gui_rows` from a fresh scan + synthesizes a `_tab_closed` row (from `_cursor_row_cache`) for each tracked Cursor tag whose tab is gone, so a PR-tracked tab survives being closed (like a dead-but-tracked regular row); prunes per-tag PR state + the row cache for tags no longer shown/tracked. Pure transform, unit-tested |
| `configure_hooks.py` | `scripts/configure_hooks.py` | Unified hook config (iterates providers, calls `provider.configure_hooks()`) |

## Runtime Data Files

All runtime data is stored in the centralized `.storage` directory at the project root:

| File | Location |
|------|----------|
| Settings | `.storage/settings.json` |
| Queue | `.storage/queues/<tag>.queue` |
| History | `.storage/history/<tag>.history` |
| Socket | `.storage/sockets/<tag>.sock` |
| Metadata | `.storage/sockets/<tag>.meta` |
| Client lock | `.storage/sockets/<tag>.client.lock` |
| Server lock | `.storage/sockets/<tag>.server.lock/` (directory) |
| Pinned sessions | `.storage/pinned_sessions.json` |
| Monitor prefs | `.storage/monitor_prefs.json` (includes `row_order`, `aliases`) |
| Notification seen state | `.storage/notification_seen.json` |
| PR context preset selection | `.storage/leap_selected_preset` |
| Auto-fetch /leap preset selection | `.storage/leap_auto_fetch_preset` |
| Message bundle preset selection | `.storage/leap_selected_direct_preset` |
| Preset definitions | `.storage/leap_presets.json` |
| Queue images | `.storage/queue_images/<hash>.png` (MD5-deduped, cleaned on server startup) |
| Note images | `.storage/note_images/<hash>.png` (MD5-deduped, persistent) |
| Signal file | `.storage/sockets/<tag>.signal` |
| Last response (Slack) | `.storage/sockets/<tag>.last_response` |
| Slack config | `.storage/slack/config.json` |
| Saved messages | `.storage/saved_messages.json` |
| Slack sessions | `.storage/slack/sessions.json` |
| CLI session tracking | `.storage/cli_sessions/<cli>/<tag>.json` (list of `{session_id, transcript_path, cwd, last_seen}` recorded by `leap-hook-process.py`; drives `leap --resume`. One subdir per provider — `claude/`, `codex/`, `cursor-agent/`, `gemini/`, plus any custom CLI that implements the Leap Resume interface) |
| CLI PID map | `.storage/pid_maps/<cli_pid>.json` (written by server when spawning the CLI: `{tag, signal_dir, python, cli_provider}`. Lets `leap-hook.sh` recover context via a PPID walk when a CLI strips env vars from hook subprocesses — the project dir itself is recovered from `$LEAP_PROJECT_DIR` or the `export LEAP_PROJECT_DIR=` line in `~/.zshrc`/`~/.bashrc`. Swept by `leap-main.sh`'s `cleanup_dead_sockets` using `kill -0`) |
| Sudo password (lid-close) | `.storage/sudo_pass.b64` (mode 0600, base64-encoded — NOT encrypted; only present while the lid-close override is enabled, deleted the moment the user unticks the box) |
| Disable-sleep marker | `.storage/disablesleep.marker` (zero-byte sentinel; present iff Leap currently holds `pmset disablesleep=1`. Drives crash recovery on next launch — if the marker is on disk but the monitor isn't running, the next startup attempts a silent `pmset disablesleep 0` using the saved password, or pops a manual-fix dialog if that fails) |
| Update-in-progress marker | `.storage/update_in_progress` (JSON: `{"pre_pull_sha": "<40-hex>", "started_at": <epoch>}`. Written by `leap-update.sh` BEFORE its `git pull`, removed by `.update-after-pull` at end of phase 2 on success; an EXIT trap in the script cleans it up on phase 1 abort. Read by `WhatsNewDialog` to keep showing the pulled commits — `<pre_pull_sha>..origin/main` instead of `HEAD..origin/main` — and by `UpdateCheckWorker` to skip its background fetch while the update is running. 30-min stale-timestamp fallback in both readers covers the phase-2-crash case where the marker is orphaned) |

## Server Queue Shortcut

Type `^^` in the server terminal to queue a message. Double-caret (`^^`) activates capture mode — characters are hidden from the CLI and shown in a `[Leap Q]` prompt on the input line. Works at any point: type `^^msg` to start fresh, or type `hello` then `^^` to convert already-typed text into a queued message. Press Enter to queue, Escape or Ctrl+C to cancel.

**Saved messages**: Type `^^` inside capture mode to save the current message to history and clear the buffer. Browse saved messages with arrow up/down. History persists across sessions in `.storage/saved_messages.json` (max 100 entries, shared across all CLIs/sessions). Editing a recalled message does not modify the saved history — only explicit `^^` save does.

**CLI input-history recall (↑/↓ outside capture)**: Leap intercepts ↑/↓ at the CLI's input prompt and drives recall itself by reading the CLI's own on-disk history (Claude: `~/.claude/history.jsonl` filtered by `project == cwd`; Codex: `~/.codex/history.jsonl`; Cursor: `~/.cursor/prompt_history.json`; Gemini: `~/.gemini/tmp/<slug>/logs.json`). Without the intercept the recalled text lives only in the CLI's TUI render and never enters Leap's input mirror — so a subsequent `^^` would snapshot an empty buffer. With it, `^^` after ↑ captures the recalled message (including paste content for Claude entries: `[Pasted text #N]` placeholders are resolved inline from `pastedContents` so the actual content reaches the LLM on submit, not the placeholder string). The cache invalidates on Enter / Ctrl+C / queue dispatch / capture exit so just-submitted messages show up immediately on the next ↑. Providers opt in via `CLIProvider.input_history(cwd)`; returning `None` falls back to passthrough (CLI handles ↑/↓ natively).

## Auto-Approve Architecture (Claude)

ALWAYS-mode auto-approve has two layers — the primary is hook-based and never renders a dialog; the fallback is the legacy TUI-menu path that types "1\r" into a rendered prompt.

**Primary: `PermissionRequest` hook (`hook_script auto_approve`).** Configured in `ClaudeProvider.configure_hooks` with matcher `^(?!AskUserQuestion$).*` (every tool EXCEPT `AskUserQuestion` — see "AskUserQuestion exclusion" below). The hook script handler `_handle_auto_approve()` in `leap-hook-process.py` reads the session's `auto_send_mode` from `.storage/pinned_sessions.json[tag]` (with global fallback to `.storage/settings.json`) and, in ALWAYS mode, emits `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}` to stdout, then `sys.exit(0)` so the trailing `print('{}')` in `__main__` doesn't append a second JSON object after the decision. `SystemExit` inherits from `BaseException` (not `Exception`), so it propagates past the `except Exception` block and the trailing `print('{}')` never runs — leaving exactly one JSON object on stdout. PAUSE mode returns normally so the trailing `print('{}')` runs, telling Claude "no decision" so the dialog renders normally.

**AskUserQuestion exclusion.** `AskUserQuestion` is the one tool whose entire purpose is to elicit a user choice. If `PermissionRequest` returns `"allow"` for it, Claude interprets that as "skip user interaction" — the question dialog is never rendered and the tool returns an empty answer set to the model ("Allowed by PermissionRequest hook" with no selections), corrupting the very flow the user invoked it for. The negative-lookahead matcher excludes the exact tool name `AskUserQuestion` so its `PermissionRequest` goes unanswered, Claude renders the dialog, and the user actually picks. Pinned by `test_claude_permission_request_matcher_excludes_ask_user_question`.

**Per-session isolation of `auto_send_mode`.** The hook fallback (`per-tag pin → global → 'pause'`) is the protocol the hook script uses, but in steady state the global fallback is almost never hit — four guarantees in the server + monitor keep per-tag pins authoritative:

1. **Snapshot at `LeapServer.__init__`** (`server.py:134-145`). Resolves `global → per-tag pin` and writes the result back into the pin BEFORE `CLIStateTracker` initialises, so later changes to the Settings dialog's global default can't retroactively flip this session's hook behavior.
2. **`set_auto_send_mode` handler writes ONLY the per-tag pin** — never the global (`server.py:582-602`). The original cross-session leak ("I toggled one session and all of them auto-approved") came from a stray `save_settings(...)` in this handler; per-session toggles must stay per-session. Pinned structurally by `test_save_settings_not_imported_by_server` and behaviourally by `test_handler_does_not_modify_settings_file` in `test_auto_send_mode_persistence.py`.
3. **`SessionMixin._merge_sessions` preserves `auto_send_mode` in `pin_data`** by pulling it from the live server's status response (`s.get('auto_send_mode')`) — without this, the monitor's first auto-pin write for a brand-new session would build `pin_data` from a stale in-memory cache that lacked the field, blowing away the server's snapshot. Pinned by `test_merge_sessions_auto_send.py`.
4. **All monitor-side pin writes are per-tag** (no `save_pinned_sessions(self._pinned_sessions)` full-state saves anywhere). Three targeted helpers in `monitor/pr_tracking/config.py` — `update_pinned_session_field(tag, field, value)`, `write_pinned_session_entry(tag, entry)`, `remove_pinned_session_tag(tag)` — each does a read-modify-write that touches only the requested tag's entry. The `write_pinned_session_entry` helper additionally treats `auto_send_mode` as server-owned: disk's value always wins over the caller's (possibly stale) in-memory copy, so a monitor refresh that ships an old `auto_send_mode` in `pin_data` can't clobber a fresh server-side toggle. Pinned by `test_set_auto_send_mode_isolation.py` and `test_pinned_sessions_corruption.py`. Residual narrow race: if a different writer mutates a *different tag* between this helper's read and write (~5–10ms window), that writer's change is lost; full elimination would need `fcntl.flock` — not currently in. All three helpers (and the server's symmetric `_save_pinned_auto_send_mode`) also have a corrupt-disk recovery path: a malformed pin file is treated as empty on read so the next write produces a valid JSON file again, restoring the self-healing behavior the pre-fix `save_pinned_sessions` had.

Pin-file readers on the `__init__` critical path (`_load_pinned_auto_send_mode`, `validate_pinned_session`, `build_auth_fetch_url`, `load_settings`, `load_pinned_sessions`) catch `(OSError, ValueError)` rather than `(OSError, JSONDecodeError)` — `UnicodeDecodeError` is a `ValueError` and the narrower except let a malformed pin file crash session startup. They also `isinstance`-guard non-dict roots / entries and validate `auto_send_mode` is one of `(PAUSE, ALWAYS)` before propagating into `CLIStateTracker`. Tested in `test_pinned_sessions_corruption.py`.

**↑/↓ during mid-RUNNING dialogs and slash-command pickers.** Two distinct classes of in-CLI UI keep the state at `RUNNING` while ↑/↓ need to reach the CLI:

1. `AskUserQuestion`'s question dialog fires no Notification hook (it's a built-in tool, not an MCP elicitation), so state stays `RUNNING` until the 5 s cursor+silence fallback flips it to `NEEDS_PERMISSION`.
2. Slash-command pickers (`/resume`, `/mcp`, `/agents`, `/config`, `/effort`, `/model`, `/memory`, `/login`, `/doctor`, `/usage`, `/bug`, `/permissions`, …) fire no hook at all and leave state in `RUNNING` for the entire time the picker is open.

In both cases the server's input filter (`server.py:3506`) would normally see ↑/↓ as RUNNING-state arrows and steal them for history recall, leaving the user unable to navigate the picker. The fix is a screen check: the input filter calls `CLIStateTracker.screen_has_active_dialog()` and passes ↑/↓ through whenever it returns True. Two complementary predicates make up the check:

- `provider.is_dialog_certain(tail_compact)` — strict permission-dialog footer (`Entertoselect` + `Esctocancel` in the compact form of the last 5 non-blank rows) or numbered-menu cursor (`❯1.`). Kept strict because the same predicate gates state transitions where false positives stick state in `NEEDS_PERMISSION` for 60 s.
- `not provider.is_idle_prompt_visible(filled_rows)` — **structural** detection of Claude's standard idle input box: a `─ HR / ❯ row / ─ HR` sandwich within the last ~8 non-blank rows (HR rows must be ≥60 chars and ≥95% `─` to ignore short inline `──` widgets like the `/effort` slider axis). When the sandwich is gone from the bottom of the screen *something* is taking it over — a slash-command picker, the trust dialog, a permission dialog that didn't match the strict footer — and ↑/↓ belong to that something, not to history recall. Intentionally structural so new Claude pickers added next month work without us enumerating their footer text. Falls back to True (assume idle visible) when the screen has fewer than `_IDLE_DETECT_MIN_ROWS` non-blank rows, so transient / boot-time screens preserve the legacy strict-dialog-only behaviour.

Non-Claude providers (Codex / Cursor / Gemini) inherit `is_idle_prompt_visible` defaulting to True, so their behaviour is unchanged. Pinned by `TestScreenHasActiveDialog` in `test_state_tracker.py` (idle-visible / picker shapes / dialog scrolled-out cases) and by `TestClaudeProvider::test_claude_idle_prompt_*` in `test_provider_behaviors.py` (sandwich detection, picker shapes, short-inline-rule rejection, picker-focused-row rejection).

**Holding a hookless dialog at `NEEDS_PERMISSION` (no Permission↔Idle oscillation).** Because `AskUserQuestion` writes no permission signal, the rendered footer on screen is the *only* reliable evidence it's still pending — a first-action question is never even written to the transcript while it waits. Two rules keep the promoted state from flickering back to Idle: (1) the cursor+silence running→needs_permission promotion does **not** `_reset_screen()` (matching the `_handle_idle_output` proactive promotion) — resetting desyncs pyte from Ink, which then only *partially* repaints and never restores the footer, so the waiting→idle dismissal checks would falsely read "dialog gone" and demote; (2) the 60 s stuck-waiting safety timeout keeps the waiting state while `has_dialog_indicator` still matches the live screen, scoped to `PROMPT_STATES` so a stuck `INTERRUPTED` still recovers. Pinned by `test_incremental_repaint_after_promotion_keeps_dialog` (faithful Ink-style incremental-repaint repro: demotes to idle pre-fix, holds post-fix) plus the `test_*_dialog*on_screen` cases in `TestSafetyTimeouts`.

Critically: the auto_approve state does NOT touch the signal file. It's a pure hook decision; Leap's state machine stays RUNNING throughout, as if no permission had ever been needed.

This hook **fires for subagent (Task tool) tool calls too**, which the older Notification path could silently miss — Claude's `Stop` hook does not fire for subagents, so an entire multi-agent turn stayed RUNNING with `_last_running_snapshot == []`, and the Late Notification guard had no fallback content to verify the dialog against. The `PermissionRequest` hook sidesteps every TUI race because no dialog is ever rendered.

**Fallback: TUI menu auto-approve (`_try_auto_approve` in `server.py`).** Still wired up for two scenarios:
1. **Older Claude versions** that don't support `PermissionRequest` — the new hook entry is silently ignored by them, and approval falls back to detecting `❯ 1. Yes` on the rendered menu and typing `1\r`.
2. **Defense-in-depth race** — if `PermissionRequest` somehow doesn't fire (e.g. a future Claude bug, or an unrecognized matcher edge case), `Notification(permission_prompt)` still fires, the state tracker transitions to `NEEDS_PERMISSION`, and `_try_auto_approve` picks up the dialog.

The `_try_auto_approve` path itself was strengthened: the Late Notification guard at `state_tracker.py:get_state` formerly rejected RUNNING→prompt signals when no dialog patterns were on screen AND `_last_running_snapshot` was empty — that's exactly the multi-agent subagent shape. The guard now distinguishes the post-Enter stale signal (empty screen + empty snapshot, the freshly-answered-via-Enter signature) from a fresh subagent signal (screen has accumulated subagent output, snapshot empty because no idle transition during the turn). Only the empty-and-empty pair is treated as stale; anything else lets the signal through.

**What auto-approve does NOT auto-handle.** MCP `Elicitation` (Notification matcher `elicitation_dialog`) is *not* auto-approved — these are free-form input requests where Leap can't guess what to type. They surface to the user via `NEEDS_INPUT`. Permission-to-USE the elicitation tool is auto-approved (it's a tool call), but the resulting question dialog stays user-facing — that's the right asymmetry.

**Other CLIs (Codex, Cursor, Gemini).** The bug above is Claude-specific because Claude is the only one with subagents. Codex/Cursor have no permission hook at all (state tracker uses TUI detection); Gemini uses `Notification(ToolPermission)` but has no subagent concept. None of them get a `PermissionRequest` hook — the test `test_other_providers_do_not_install_permission_request` pins this.

## Client Commands

| Command | Action |
|---------|--------|
| `!h` or `!help` | Show help |
| `<message>` | Queue message (auto-sends when ready) |
| `!d <msg>` or `!direct <msg>` | Send directly (bypass queue) |
| `!e <index>` or `!edit <index>` | Edit queued message by index (0=first) |
| `!l` or `!list` | Show queue |
| `!c` or `!clear` | Clear queue |
| `!f` or `!force` | Force-send next queued message |
| `!autosend` or `!as` | Toggle auto-send mode (pause/always) |
| `!slack` or `!slack on/off` | Show status or toggle Slack for this session |
| `!x` or `!quit` (`Ctrl+D`) | Exit client |

## Adding Features

- **New CLI provider** → See the `.claude/skills/add-cli-provider.md` skill for a comprehensive step-by-step guide. Key files: create `cli_providers/<name>.py`, register in `registry.py`, implement `configure_hooks()` and `hooks_installed()` (the latter must be the symmetric inverse of the former — both halves checked, never raises). The CLI selector, monitor table, ASCII banner, and shell flags are all dynamic and require no changes.

  **All custom CLIs are variants of one of the four base CLIs** (Claude / Codex / Cursor Agent / Gemini). `CustomCLIProvider` (in `registry.py`) wraps a base provider and delegates everything via `__getattribute__` — including `hooks_installed()` and `base_type`. Custom-CLI authors don't set `base_type` themselves; they pass `base_provider=ClaudeProvider()` (or one of the other three) to `CustomCLIProvider.__init__`, and `base_type` follows automatically (it resolves to the base's `name` via the `__getattribute__` delegation). The session-start gate uses `get_provider(provider.base_type).hooks_installed()` so custom CLIs share their base's hook setup automatically. There is no path for a custom CLI that's not built atop one of the four — design accordingly.
- **New monitor dialog / window** → See the `.claude/skills/add-dialog.md` skill. Covers `ZoomMixin` setup, dialog geometry persistence, theme integration, the font-size cascade quirk, and — critically — the **prefs persistence model** (`MonitorWindow._DIALOG_OWNED_KEYS` and why `save_monitor_prefs(self._prefs)` must NOT be called outside `_save_prefs`). Skipping that last part is the most common way dialog state silently gets clobbered.
- **Adding / removing / reordering a session-table column** → The `COL_*` constants in `monitor/app.py` are referenced by *four* positional-index sites that DON'T import them — change any column and you MUST update all four in the same diff, or they silently drift off-by-one (wrong separators, wrong monospace columns, wrong alignment):
  1. `_HEADER_LABELS` in `monitor/app.py` (header strings, parallel to `COL_*` order)
  2. `_CENTER_COLS` in `_mixins/table_builder_mixin.py` (data cols whose plain-text cells center)
  3. `_MONO_COLS` in `_mixins/table_builder_mixin.py` (cols rendered in monospace font)
  4. `COLUMN_GROUPS` in `ui/table_helpers.py` (drives the inter-/intra-group vertical separators)
- **Utils** → `src/leap/utils/`
- **Server** → `src/leap/server/`, update `LeapServer`
- **Client** → `src/leap/client/`, update `LeapClient`
- **Monitor** → `src/leap/monitor/`, update `MonitorWindow`
- **Socket communication** → Use `send_socket_request()` from `utils/socket_utils.py` for any new code that needs to talk to a Leap server via Unix socket. Do not duplicate the connect/send/recv pattern. Incoming messages are capped at `MAX_MESSAGE_SIZE` (1 MB) in `socket_handler.py`; larger payloads are rejected.
- **New third-party dependencies** → Add to `pyproject.toml` under the appropriate group: `[tool.poetry.dependencies]` for core, `[tool.poetry.group.monitor.dependencies]` for GUI-only deps. Run `poetry lock && poetry install` after. All imports must be at module top level (no inline imports except optional deps).
- **New dialogs** → All new resizable dialogs (except simple warning/error/info popups) must save/restore their size using `load_dialog_geometry(key)` / `save_dialog_geometry(key, w, h)` from `monitor/pr_tracking/config.py`. Call `load_dialog_geometry()` in `__init__` to restore. For persistence: if the dialog closes via `accept()`/`reject()`, save in `done()`. If it closes via `close()` or the X button, save in `closeEvent()` instead — `done()` is **not** called for `close()`/X.

  **Button row layout — Cancel bottom-left, primary bottom-right.** Project convention for every monitor `QDialog` with a Cancel button: add Cancel first, then `addStretch()`, then the primary action(s) on the right:

  ```python
  btn_row = QHBoxLayout()
  cancel_btn = QPushButton('Cancel')
  cancel_btn.clicked.connect(self.reject)
  btn_row.addWidget(cancel_btn)
  btn_row.addStretch()
  ok_btn = QPushButton('OK')  # or 'Send' / 'Save' / 'Confirm' / etc.
  ok_btn.setDefault(True)
  ok_btn.clicked.connect(self.accept)
  btn_row.addWidget(ok_btn)
  layout.addLayout(btn_row)
  ```

  Do **not** use `QDialogButtonBox(Ok | Cancel)` for new dialogs — on macOS it groups Cancel next to OK on the right, which violates the convention. For 3-button cases (e.g. Cancel + secondary + primary), keep Cancel on the outside-left and group the other two on the right of the stretch — see `_mixins/actions_menu_mixin.py` and `dialogs/git_changes_dialog.py:CommitListDialog`. Close-labeled dismissal buttons (one-button viewer dialogs like `WhatsNewDialog`, `NotesDialog`) are not covered by this rule — they're a different paradigm ("I'm done viewing" vs "discard my edits").

  **Font zoom (Cmd+scroll / Cmd+±/0):** Every new dialog must inherit from `ZoomMixin` (`monitor/dialogs/zoom_mixin.py`) and call `_init_zoom(...)` at the end of `__init__`. Two forms are supported:

  * **Single-target** — for form dialogs with no distinct "content" area (inputs, combos, checkboxes, and buttons only):

    ```python
    class MyDialog(ZoomMixin, QDialog):
        def __init__(self, ...):
            super().__init__(...)
            # ... build UI ...
            self._init_zoom('my_dialog_font_size')
    ```

  * **Split-target (REQUIRED when the dialog has a primary content area** — QTextEdit, QListWidget, QTreeView, QTableWidget, message cards, a diff viewer, etc.) — so the user can enlarge the content without blowing up the buttons/chrome, and vice versa:

    ```python
    class MyDialog(ZoomMixin, QDialog):
        def __init__(self, ...):
            super().__init__(...)
            self._editor = QTextEdit()
            self._list = QListWidget()
            # ... build UI ...
            self._init_zoom(
                pref_key='my_dialog_font_size',             # buttons/chrome
                content_pref_key='my_dialog_text_font_size',  # content area
                content_widgets=[self._editor, self._list],
            )
    ```

  For dialogs that rebuild content widgets dynamically (e.g. message cards recreated on save), pass a **callable** as `content_widgets` — the mixin calls it on every event so new widgets are picked up automatically — and call `self._zoom_reapply_content()` at the end of the rebuild method so the new widgets render at the current content size.

  **Close hooks:** Font sizes are persisted per-dialog in `monitor_prefs.json` and flushed by `done()` automatically. If your dialog closes via `closeEvent()` instead of `done()`, call `self._zoom_flush()` explicitly in `closeEvent()`. Font sizes are NOT cleared by the "reset window sizes" button.

  **Hint labels:** Any inline `setStyleSheet(... font-size: ... )` on a hint/label will override the dialog's cascade and NOT scale with zoom. Leave `font-size` out of the inline stylesheet (set only `color:`) so ZoomMixin's cascade applies.

  **Popups** (QMessageBox / QInputDialog / QMenu / QFileDialog / tooltips) are handled globally by `PopupZoomManager` (`monitor/popup_zoom.py`) — one shared `popup_font_size` pref. You don't need to do anything for popups shown from your dialog.
- **New `.storage` subdirectories** → If you add a new subdirectory under `.storage/`, you **must** update three places:
  1. Add the constant in `utils/constants.py` (next to `QUEUE_DIR`, `SOCKET_DIR`, `HISTORY_DIR`)
  2. Add a `.mkdir()` call in `ensure_storage_dirs()` in `utils/constants.py`
  3. Add the path to the `ensure-storage` target in `Makefile`
- **Theming** → Use `current_theme()` from `monitor/themes.py` to access colors. Never hardcode colors in monitor code — use theme properties (e.g. `t.accent_green`, `t.text_primary`). Theme colors are applied via `QPalette` (preserves native macOS widget rendering) + minimal QSS. Cell button styles use `close_btn_style()` / `active_btn_style()` / `menu_btn_style()` from `table_helpers.py`. Theme persists as `"theme"` in `monitor_prefs.json` (default: `"Midnight"`). Nine built-in themes: Leap, Amber, Midnight, Cosmos, Ocean, Monokai, Nord, Solarized Dark, Dawn.
- **New assets (images, icons, themed variants)** → Any new asset file in `assets/` that the monitor uses at runtime **must** also be added to `DATA_FILES` in `setup.py`. The py2app bundle only includes explicitly listed files — assets missing from `setup.py` will work in `make run-monitor` (dev mode) but silently fail in the installed app. Logo text variants use `glob('assets/leap-text*.png')` so new theme logos are auto-included, but other new assets need manual addition.

## Testing

```bash
make test                         # All tests (unit + integration)
make test-unit                    # Fast unit tests only (fake clock)
make test-integration             # Real-PTY integration tests (~2 min)
poetry run pytest tests/ -v       # All tests with verbose output
```

- Tests use `pytest` (dev dependency, `poetry install --with dev`)
- `tests/unit/` — fake-clock tracker tests and other in-process units
- `tests/integration/` — real bash-via-pexpect PTY + pyte rendering; shared `PTYFixture` lives in `tests/conftest.py`
- `ClaudeStateTracker` uses an injectable `clock` parameter — tests pass a fake clock (`lambda: t[0]`) for deterministic time control
- Use `tmp_path` fixture for signal files
- Test file naming: `tests/unit/test_<module>.py` or `tests/integration/test_<topic>.py`

## Code Conventions

- **No em-dashes in user-visible text**: Never put an em-dash `—` (or its `\u2014` escape) in anything the user sees: GUI labels/titles/tooltips, terminal banners, log messages, Slack text, install/update output (echoes), README, help text. Use a plain hyphen `-` instead. (Code comments and docstrings are exempt.)
- **Type hints**: 100% coverage on all function signatures and return types. Use `Optional[X]` (not `X | None`) for consistency.
- **Imports**: **Every `import` and `from X import Y` statement MUST live at the top of the module.** No inline imports inside `def` bodies, methods, class bodies, `if/for/while` blocks, or anywhere other than the module header — not for "lazy loading", not for "avoiding startup cost", not as a hotfix to dodge a circular import. Violating this rule has bitten us multiple times (stale references, import-error masking, duplication of the same import in 15 different methods); treat it as a hard ban.
  - **Only two allowed exceptions**, and both live at module top level:
    1. **Optional-dependency fallback**: a top-level `try: import X except ImportError:` block that sets a sentinel (e.g. `WebClient = None`) so the rest of the module can guard on it. Used today for `prompt_toolkit`, `slack_sdk`/`slack_bolt`, `tomllib`/`tomli`, and `AppKit` when the module needs to import on non-macOS.
    2. **Type-only circular-import break**: a top-level `if TYPE_CHECKING:` block for imports used *only* in type annotations. If you hit a real runtime circular import, the fix is to restructure the modules (extract the shared code) — not to sneak an inline import back in.
  - Before adding a new top-level import, check for an existing one — don't duplicate. When moving an inline alias (e.g. `import time as _time`), replace every `_time.` call site with the bare name.
  - Stdlib → third-party → `leap.*`, each group alphabetized.
- **Client commands**: Each command handler is extracted into a private `_handle_*` method on `LeapClient`. The `_process_command` dispatcher delegates to these handlers.
- **Socket pattern**: `SocketClient._send_request()` is the single source of truth for client→server socket communication. `send_socket_request()` in `utils/socket_utils.py` is the lightweight variant for monitor/session_manager code that doesn't need rate-limited error reporting.

## SCM Polling & PR Tracking

The monitor polls GitLab/GitHub for PR status updates and user notifications. Key timeouts:

- **GitLab client timeout**: 15s per HTTP request
- **Poll cycle timeout**: 30s for all `ThreadPoolExecutor` futures
- **Stuck-poll safeguard**: Force-resets `_scm_polling` after 60s
- **Poll interval**: Configurable via `poll_interval` in config (default: 30s)

Polling flow: `_scm_poll_timer` → `_start_scm_poll()` → `SCMPollerWorker` (QThread) → `get_pr_status()` per session → `_on_scm_results()` → `_update_pr_column()`.

### Sending PR Comments to Leap

Left-click the PR status label (when any comment is unresponded) for a 2-item menu: **Go to first comment** (opens the comment in the browser) and **Send comment/s to session** (opens `SendCommentsDialog`). The dialog exposes two binary choices — filter (`all` / `leap`-tag-only) and mode (`each` message / `combined`) — plus a single-message "PR context preset" combo that's persisted via `save_selected_preset_name()` in `.storage/leap_selected_preset` (same file that `leap_sender.send_to_leap_session` reads to prepend context to every outgoing comment). When `auto_fetch_leap` is on, the whole "Which comments to send" section is omitted from the dialog — the filter is effectively forced to `all` since `/leap`-tagged comments are already auto-queued. Picks persist via `send_comments_filter` / `send_comments_mode` in `monitor_prefs.json`. On dispatch, `IndicatorLabel._open_send_comments_dialog()` does a pre-flight dead-server check (clear popup, no worker launched) and routes to one of four `_send_*_to_leap()` handlers by `(filter, mode)` pair. All four share `CollectThreadsWorker` (Phase 1), then diverge: `SendThreadsWorker` (one-by-one) or `SendThreadsCombinedWorker` (concatenated). All modes acknowledge comments on SCM side after send.

### /leap Auto-Fetch

"Auto '/leap' fetch" checkbox: when ON, `SCMPollerWorker` auto-scans for `/leap` tags each poll cycle. A `/leap` comment does **not** count as a user response — only the bot ack (`[Leap bot] on it!`) marks a comment as handled. When auto-fetch is on, the `SendCommentsDialog` hides its entire "Which comments to send" section (those comments are already queued automatically). Setting persisted as `auto_fetch_leap` in monitor prefs.

**Auto-fetch preset**: a separate preset combobox sits next to the checkbox in the main window (visible only while the checkbox is on). Its selection — persisted in `.storage/leap_auto_fetch_preset` — is loaded by `load_auto_fetch_leap_preset()` and passed through `send_to_leap_session(tag, msg, preset=…)` in `scm_polling._handle_leap_commands`. This is **independent** of `.storage/leap_selected_preset` which is used by manual sends from `SendCommentsDialog`. The combo's popup refreshes itself on open (`_RefreshableComboBox.showPopup`) so preset edits made elsewhere show up next time the user opens the dropdown; it also self-heals a stale saved selection if the preset was deleted or grew to multi-message.

### Environment Variable Token Mode

SCM tokens support two modes: `token_mode: "direct"` (stored in config) or `"env_var"` (resolved from `os.environ`). Resolution via `resolve_scm_token()` in `config.py`. On startup, env var tokens are validated — invalid ones disable the provider until re-tested via the setup dialog. Tracked rows survive provider disconnection (they retain `pr_tracked: True` in `pinned_sessions.json` and auto-reconnect once the provider is restored).

### GitHub Enterprise URL Handling

GitHub Enterprise Server serves its REST API under `https://<host>/api/v3` (and GraphQL under `https://<host>/api/graphql`). `GitHubProvider.__init__` already assumes the stored base URL carries the `/api/v3` suffix when deriving the GraphQL endpoint, so a user who enters just `https://<host>` would get a broken REST client *and* broken resolved-thread queries. `normalize_github_api_url()` canonicalizes the URL: github.com/api.github.com map to the default (empty `base_url` → PyGithub uses api.github.com), and any other host gets `/api/v3` appended. It's applied in-memory on every `load_github_config` (NOT persisted there — `load_github_config` runs on the SCM poll worker's `ThreadPoolExecutor` threads via `refine_scm_type`, so a write-back could race a main-thread `save_github_config`; the canonical form is persisted whenever the user next saves). The companion gotcha: `detect_scm_type()` strips a trailing `/api/v3` from the saved URL before substring-matching it against the bare host from the git remote, so the suffix doesn't break SCM-type detection for self-hosted hosts.

### User Notifications

Per-provider enable/disable via setup dialog. Polls `get_user_notifications()` each cycle. Seen IDs deduplicated via `.storage/notification_seen.json`. First-run seeds all existing notifications as seen. 403 errors auto-disable notifications for that provider.

### Persistent Rows & Pinned Sessions

Rows persist via `pinned_sessions.json`. Key rules:
- Every active session is auto-pinned on discovery
- Row survives if it has a running server OR `pr_tracked: True` set in pinned data OR pinned PR Branch data (`remote_project_path` + non-empty `branch`, mirroring the PR Branch column display rule — Stop PR Tracking leaves these in the pin so the X-to-clear UI still works) OR an in-flight transient flag (`_tracked_tags`, `_checking_tags`, `_starting_tags`, `_moving_tags`)
- Dead rows that are no longer being tracked AND have no displayed PR Branch are auto-removed on the next merge tick (so a row with no PR + no PR Branch + no server never appears in the table)
- PR auto-reconnects on monitor restart for rows with `pr_tracked: True` — that flag is also what keeps the row alive across the startup window before `_auto_track_pr_pinned` populates `_tracked_tags`/`_checking_tags`
- `_deleted_tags` set prevents auto-refresh from re-pinning just-deleted rows

### PR Status Markers, Approval Icons & Merged/Closed Badges

The PR column surfaces more than open/responded state. `PRStatus` (in `pr_tracking/base.py`) carries four qualitative flags, each populated from data the providers already fetch:

| Field | GitHub source | GitLab source |
|-------|---------------|---------------|
| `draft` | `pr.draft` | `draft` / legacy `work_in_progress` |
| `has_conflicts` | `mergeable_state == 'dirty'` | `_mr_has_conflicts` (`has_conflicts` / `merge_status=='cannot_be_merged'` / `detailed_merge_status=='conflict'`) |
| `changes_requested` | latest review per reviewer == `CHANGES_REQUESTED` | `detailed_merge_status == 'requested_changes'` (best-effort: single top-reason, older servers omit it) |
| `checks_failed` | `_github_checks_failed` (head-commit check-runs + legacy combined status), **gated on `mergeable_state in ('unstable','blocked')`** so clean PRs cost no extra API call; distinguishes failed from pending | `_mr_pipeline_failed` (`head_pipeline.status == 'failed'` only - never running/pending) |

**Rendering** (`_apply_pr_status` in `pr_display_mixin.py`; cell built by `_render_tracked_pr_cell`). An open tracked PR cell is `[× | 👍/👎 | 📝 ⚠ 🔴 | ✓ / 💬 N | 🔥]`:
- **Status**: `✓` (green, all responded) or `💬 N` (pulsing orange, N unresponded); `No PR` / `N/A` muted.
- **Markers** (📝 draft, ⚠ conflict, 🔴 CI/pipeline) are **standalone `IndicatorLabel`s**, NOT text glued onto the status - so each has its own hover tooltip ("Draft PR" / "Has merge conflicts" / "Pipeline failed") and its own color: the conflict ⚠ is `accent_orange` while the `✓` stays green. Found in the cell by `objectName` (`_draftMarker`/`_conflictMarker`/`_checksMarker`) and **ride on `pr_widget`'s lifecycle** (stashed as `pr_widget._pr_markers`, reused + reparented across cache-miss rebuilds with `set_preserve_popup`, so a rebuild mid-hover doesn't orphan a tooltip popup). Only shown on `ALL_RESPONDED`/`UNRESPONDED` (meaningless without an open PR).
- **Approval** indicator: `👍` approved or `👎` changes-requested; `👎` takes priority when a PR is both.
- `set_pulsing(False)` clears the widget stylesheet, so each non-pulsing branch calls it **before** `setStyleSheet(color)` - otherwise the color is wiped to default (the bug that made `✓` render white).

**Merged / Closed badges** (`_render_closed_pr_cell` in `table_builder_mixin.py`). When a tracked PR's open lookup returns NO_PR but `find_latest_closed_pr` finds a merged/closed PR for the branch, `_persist_closed_pr` writes `pr_merged`/`pr_closed` + `pr_url`/`pr_iid`/`pr_title` to the pin. The untracked-row PR cell then renders a soft-tinted badge (`active_btn_style`, same look as the green Terminal button): violet `Merged` (theme `pr_merged_color`, `#a371f7` dark / `#7c3aed` light) or red `Closed`, with a git-merge / pr-closed icon (`git_merge_icon`/`git_pr_closed_icon`, recolored SVGs in `table_helpers.py`). Clicking opens the PR. Two X buttons mirror a tracked row: leftmost `×` (`_stop_tracking_closed_pr`) drops the merged/closed flags (row reverts to Track PR); the PR-Branch `×` (`_clear_pinned_pr_data`) clears all pinned PR data.

**Re-open detection.** Merged/closed PR-pinned rows keep being polled - `_revisit_tags` / `_revisit_poll_sessions` builds `_pr_only` status-only watcher dicts (so they never participate in `/leap` delivery while closed). A non-NO_PR result drives `_reopen_tracked_pr` (promote back to live tracking, drop the stale flags). The inverse - a tracked PR going NO_PR - schedules `_check_pr_closed_after_no_pr` (one background `find_latest_closed_pr` per edge) → `_on_polled_pr_closed_lookup` → badge. `_sync_scm_poll_timer` keeps the poll timer alive while any merged/closed row needs watching; `_on_polled_pr_closed_lookup` is `_shutting_down`-guarded (it can fire after the window starts closing).

**GitHub vs GitLab nuance.** `find_latest_closed_pr` diverges intentionally (pinned by `test_prefers_merged_over_closed`): GitHub returns the *most-recently-updated* closed PR; GitLab *prefers merged*. So a reused branch can show `Closed` on GitHub but `Merged` on GitLab.

**Tooltip popups** (`IndicatorPopup`): word-wrap `QLabel`s have a flaky `sizeHint`, so the popup pins its width to the widest line's natural width (capped at 280px) - short tips stay on one line instead of collapsing to one word per row.

### Add Row (+ Button)

Three options:
- **From Git URL** — PR URLs or plain project URLs → parse, pin, clone/track.
- **From Local Path** — clone to repos dir or open directly.
- **From Resume** — GUI does only the picking + already-running guard, then hands off to a new terminal. `_add_row_from_resume()` (in `pr_tracking_mixin.py`) opens `ResumeSessionDialog`; when the user picks `(cli, tag, SessionRecord)`, if the same CLI session UUID is already running under a live Leap tag it offers **"Jump to it?"** (Yes default) and on Yes calls `_focus_session(owner_tag, 'server')` — the same "Jump to server terminal" navigation the row's Terminal button uses — instead of launching a duplicate. Otherwise it calls `ServerLauncher.open_resume_in_terminal(cli=…, tag=…, session_id=…)` which spawns a terminal running `leap --resume --cli=<X> --tag=<Y> --session=<Z>`. From there the CLI flow takes over: `leap-resume.py` skips its picker (pre-pick mode), runs the live-owners + `_server_alive` checks, prompts the user for cwd choice if `provider.requires_cwd_bound_resume` is True and the recorded cwd ≠ the terminal's cwd, then execs `leap-main.sh` with `LEAP_RESUME_*` env vars set. The server reads those and prepends `provider.resume_args(<id>)` to the CLI argv. The monitor row appears via auto-discovery once the server starts.

  **Already-running → jump (both CLI and GUI).** When a picked session is already live under a Leap tag, neither path dead-ends. The CLI picker (`leap-resume.py`) shows an arrow-key Yes/No "Jump to it?" prompt (`_ask_jump_to`, default Yes); on Yes it focuses the running session's terminal via `_jump_to_running(tag)` → `leap.monitor.navigation.find_terminal_with_title`. That helper needs only pyobjc (a **core** dep, not the PyQt5 GUI stack) — `leap/monitor/__init__.py` deliberately does **not** eager-import `app`, so importing `navigation` stays cheap and works on core-only installs; the import is still guarded as optional (`None` → a "navigation unavailable in this environment" message). The GUI uses a `QMessageBox.question` for the same choice and reuses `_focus_session`.

Tag validation via shared `_ask_tag()` helper.

### Managed Clone Sync (Dirty-Tree Dialog)

Clicking Terminal on a PR-pinned row syncs the managed clone in `<repos_dir>/<project>` to `origin/<branch>` before opening Leap. The sync is destructive (`git reset --hard` + `git clean -fd`) because managed clones are throwaway state — but if the clone has uncommitted edits we now prompt before destroying them.

Flow (`ServerLauncher._dirty_check_then_align` → `_on_dirty_check` → `_ask_dirty_action`):

1. `BackgroundCallWorker` does: ensure auth on `origin`, `git fetch origin <branch>`, `git status --porcelain`, `git rev-list --count origin/<branch>..HEAD`, `git symbolic-ref --quiet HEAD` (detached check).
2. Clean working tree AND zero commits ahead AND HEAD on a branch → straight to `_server_force_align`, no dialog.
3. Otherwise → 3-way `QDialog` with Cancel pinned bottom-left and two action buttons bottom-right. The bullet list goes synthetic-entries-first (detached HEAD, fetch-fail, ahead-count, scan failures) then dirty files, so the dialog's `items[:5]` truncation can't hide a critical entry behind "…and N more":
   - **Clone into `<name>_<i+1>`** (default) — leaves the dirty/ahead dir untouched, picks the lowest free slot at or after `i+1` via `_find_available_project_dir(start_index=…)`, then re-enters `_start_server_from_pr`. If that slot is *also* dirty the dialog re-fires; if it's in use by another Leap server it auto-skips. Slot 100 is the hardcoded fallback (always clones fresh).
   - **Discard && sync** — calls `_server_force_align`. `_align()` does a best-effort `git merge|rebase|cherry-pick|revert --abort`, then `reset --hard HEAD` + `clean -fd`, then the branch checkout. The pre-clean exists because plain `git checkout <branch>` refuses to switch with conflicting local changes. The subsequent `reset --hard origin/<branch>` is what wipes ahead commits.
   - **Cancel** — `_cancel_start(tag)`, status banner updates to `Cancelled — '<dir>' left as-is`, `pinned['project_path']` is preserved (next click retries the same dir).

We deliberately surface the dialog even when the pre-fetch failed (with a synthetic `(could not fetch — local state may already diverge from origin/<branch>)` entry) rather than deferring to `_align`'s fetch-failed handler. Deferring opens a silent-destruction window: pre-fetch could fail transiently while `_align`'s retry succeeds (network recovered, auth re-resolved), and `_align` would then run `reset --hard` without any consent prompt.

Detached HEAD is detected separately and surfaced as a distinct entry — without it, commit-URL re-opens (which leave HEAD detached at the pinned SHA after the prior session) would read the "N commits ahead" entry as "you have N new commits", which is misleading. The pre-check fetch is duplicated by `_align`'s own fetch — acceptable: git fetches against unchanged refs are sub-second, and the duplication keeps `_align` self-contained for the post-clone path (which skips the dirty gate).

Safety guards:
- `pinned['remote_project_path']` rsplit must yield a non-empty project name — otherwise `<repos_dir>/''` would resolve to `repos_dir` itself and the clone path's `shutil.rmtree` would wipe every managed clone. Both `_start_server_from_pr` and `_on_dirty_check` bail out cleanly on empty.
- Tag deletion during the dialog is rechecked twice (entry to `_on_dirty_check` *and* after the modal returns) — without these, `_server_finish` would resurrect a tag the user explicitly dropped.
- `Discard && sync`'s autoDefault is forced off so tabbing onto it and pressing Enter doesn't silently destroy local edits; Enter falls through to the safe default.

### New Change Indicator

A fire icon (🔥) appears on the far right of the Status and PR columns when the value recently changed. Controlled by `new_status_seconds` in monitor prefs (default: 60, 0 = disabled). Click the indicator to dismiss it; dismissal resets when the value changes again.

- **Status column**: Never shown for `running` or `interrupted` states. Tracked in `_state_changed_at` and `_dismissed_new_status` on `MonitorWindow`.
- **PR column**: Triggers on changes to PR state, unresponded count, approval status, who approved, changes-requested, or failing-checks. First-time discovery is seeded with epoch 0 (no fire on startup). Tracked in `_pr_changed_at` and `_dismissed_pr_new_status` on `MonitorWindow`.

### Branch Mismatch & Server Startup Validation

- **Runtime mismatch**: Monitor shows `⚠ Server` in orange when live branch differs from expected PR branch
- **Startup validation** (`_validate_pinned_session()` in `server.py`): Checks repo match, branch match, behind-remote status. Fails 1-3 block startup; ahead/dirty is a warning only. Skipped for non-PR-pinned rows

### Row Ordering (Drag-and-Drop)

Rows are ordered by insertion time (not alphabetical). Users can drag any cell to reorder rows; the order is persisted as a `row_order` list in `monitor_prefs.json`. New sessions are appended at the end.

- **Drag detection**: App-level event filter (`eventFilter` in `app.py`) intercepts `MouseButtonPress`/`MouseMove` on cell widgets to initiate a `QDrag`
- **Drop indicator**: A 2px theme-colored line shows the drop position during drag
- **Auto-refresh paused** during drag (`timer.stop()` / `timer.start()`) to prevent table rebuilds from interrupting the gesture
- **Cleanup**: When rows are deleted, `_remove_from_row_order()` in `session_mixin.py` removes the tag from the persisted list

### Row Colors

Per-row background colors selectable via a droplet icon button in the Tag column. Persisted as `row_colors: {tag: "#hex"}` in `monitor_prefs.json`.

- **Picker**: `ColorPickerPopup` (in `table_helpers.py`) — 4x4 grid of muted color swatches + Clear button, opened via `_show_color_picker()` in `table_builder_mixin.py`
- **Rendering**: `SeparatorDelegate.paint()` reads `_row_colors` / `_row_tags` table properties and `fillRect`s the row background before the hover overlay
- **Text contrast**: `ensure_contrast()` adjusts text foreground against the row color for both `QTableWidgetItem` cells and child `QLabel`s in widget cells (skips `PulsingLabel`/`IndicatorLabel`)
- **Cleanup**: `_remove_pinned_session()` in `session_mixin.py` deletes the color entry when a row is removed

### Tag Aliases

Display aliases for tags, set via right-click context menu on the Tag column. Persisted as `aliases: {tag: "display name"}` in `monitor_prefs.json`.

- **Display**: Aliased tags show the alias in *italic*; the real tag is unchanged everywhere else (files, sockets, server, client)
- **Tooltip**: Aliased tags always show "Alias: X / Tag: Y" (regardless of tooltip setting). Regular tags show on hover when truncated or when "Show hover explanations" is on
- **Context menu**: Right-click tag cell → "Set alias" / "Rename alias" / "Remove alias" via `_show_tag_context_menu()` in `table_builder_mixin.py`
- **Cleanup**: `_remove_pinned_session()` and `_merge_sessions()` in `session_mixin.py` delete the alias entry when a row is removed

### Live Filter (Search Box)

Substring filter next to the "+ Add Session" button. Same priority order and case-insensitivity as the Resume dialog's filter: Tag → Project → App → CLI → Path. Each row falls into the first bucket whose field substring-matches the query; rows that match nothing are dropped. Tag matches also check the user's alias (so a filter on an alias works the same as on the underlying tag).

- **Wiring**: `QLineEdit` (`self._search_edit`) in the table toolbar; `textChanged` → `_on_search_changed` (in `table_builder_mixin.py`) → updates `self._search_query` → calls `_update_table()`.
- **Filter execution**: `_apply_search_filter(sessions)` returns the filtered view. `_update_table` swaps `self.sessions` for the filtered list via try/finally so the rest of the table-build code path is unchanged; `_update_table_body` (split out from `_update_table`) renders against whatever the wrapper installed. Every other code path on the monitor — drag-drop, PR tracking, sleep guard — sees the full session list because the swap is undone before they read it.
- **Manual row order survives**: each bucket appends rows in their original `self.sessions` order, so drag-drop reorder isn't reshuffled by filtering.
- **Drag-drop disabled while filter is active**: visible row indices no longer map 1:1 to `self.sessions` when rows are hidden, so reordering would silently move the wrong session. `_perform_row_drag` and `_on_row_moved` both bail out when `self._search_query` is non-empty; user has to clear the filter first.
- **Empty-state copy**: when the filter yields zero rows, the placeholder shows "No matching sessions" (not "No active sessions") so it's clear the filter — not the absence of servers — is what hid everything.
- **Column-width preservation across empty round-trip**: ResizeToContents on COL_DELETE and the `_on_section_resized` redistribute handler both shrink columns when the table empties. Empty branch snapshots widths + COL_DELETE's resize mode, switches COL_DELETE to Interactive, and applies saved widths — all *before* `setRowCount(1)` / `removeCellWidget`, with `_resizing_columns = True` to block the redistribute handler. On the empty→populated transition the saved mode is restored at the start of the populated branch; the existing `resizeColumnToContents(COL_DELETE)` at the end refits the X-button widget. Same mode-toggle pattern in the Resume dialog (`_populate`), where every column gets switched to Interactive on the populated→empty transition and restored on the way back. Not persisted across monitor restarts — filter clears every launch.

## Slack Integration

Optional Slack app for bidirectional Leap ↔ Slack communication. Each session gets a thread in the user's DM.

```bash
make install-slack-app   # Install deps + guided setup wizard
leap --slack                 # Start the bot daemon
```

**Data flow**: Claude finishes → hook reads transcript JSONL → writes to signal file → `OutputCapture` writes `.last_response` → `OutputWatcher` posts to Slack. Replies: Slack thread → `MessageRouter` → queue or direct message via socket.

Bot can also be started/stopped from the monitor's **Slack Bot** button. Dependencies: `slack-bolt`, `slack-sdk` (optional poetry group).

## IDE Setup

### JetBrains (PyCharm, IntelliJ, etc.)
**Automatically configured during `make install`** — Terminal Engine set to Classic, "Show application title" enabled. Restart IDEs after installation.

### VS Code / Cursor
**Automatically configured during `make install`** — Terminal selector extension auto-installed, tabs show numbered labels. Extension also configures Shift+Enter to send a distinct CSI u sequence so the client can distinguish it from plain Enter. Cursor (VS Code fork) is detected separately via `__CFBundleIdentifier` and uses its own CLI (`cursor`), settings path, and AppleScript app name. The same `.vsix` extension is installed into both editors.

### iTerm2
**Automatically configured during `make install`** — CSI u (Kitty keyboard protocol) enabled in all profiles so Shift+Enter sends a distinct sequence. Restart iTerm2 after installation for the change to take effect.

### WezTerm
**Automatically configured during `make install`** — `enable_csi_u_key_encoding = true` added to Lua config (`~/.wezterm.lua` or `~/.config/wezterm/wezterm.lua`) so Shift+Enter sends a distinct CSI u sequence. Creates a new config file if none exists. Restart WezTerm after installation for the change to take effect. Full monitor navigation support via `wezterm cli` (navigate, close, open tabs).

### cmux
**No install-time configuration needed.** cmux is a Ghostty-based macOS terminal (`com.cmuxterm.app`); it speaks the Kitty keyboard protocol natively, so Shift+Enter works without a CSI u config step. It appears in the monitor's **Default terminal** dropdown (`_detect_installed_terminals` in `settings_dialog.py`, path + Spotlight on the bundle id).

Runtime detection: cmux inherits Ghostty's `TERM_PROGRAM=ghostty` but also exports `CMUX_*` identifiers (`CMUX_SURFACE_ID`/`CMUX_WORKSPACE_ID`/`CMUX_SOCKET_PATH`/…). `detect_ide()` checks those **before** the `ghostty` branch so cmux sessions are tagged `'cmux'` (navigable) rather than plain `'Ghostty'`.

Monitor navigation/open/close go through cmux's **AppleScript dictionary** (`cmux.sdef`: `application > windows > tabs(workspaces) > terminals`) — **not** the bundled `cmux` CLI, because cmux's control socket defaults to `socketControlMode: cmuxOnly`, which rejects an outside process like the monitor. Three cmux quirks the helpers account for (all verified against a live app): (1) the app-level `terminals` element is unreliable (reports count 0), so they walk `windows > tabs`; (2) cmux does **not** expose a per-surface title — every `terminal.name` reads as a generic "Terminal"; (3) a shell's OSC title (`lps <tag>`, set by the server) is surfaced only as the **workspace (tab) name**, and only for the workspace's **active** surface. So `_navigate_cmux` uses two passes: first match the workspace name (hits when the session's surface is active or is the workspace's only surface), then — if that misses, meaning the target surface is inactive and its title is hidden — **probe**: `focus` each surface in a multi-surface workspace and re-read the workspace name, landing on the match (or restoring the workspace's original focus if none). `_close_cmux` matches the workspace name only (no probe — closing an inactive surface is rare and probing-to-close is more disruptive than the miss). Helpers in `navigation.py`: `_navigate_cmux` / `_close_cmux` (AppleScript, guarded on cmux already running via `_get_app_pid` so the fallback chains never cold-launch it) and `_open_cmux_terminal` (tries the CLI's `new-workspace --command` first for users who can reach the socket — it sends text+Enter — then falls back to AppleScript `new tab` + `input text` + return, which cmux runs as a normal command, verified against a live app — `input text` is not held by bracketed paste).

## Cursor Editor Agent Tabs (read-only monitor rows)

Optional, **on by default** (Settings -> "Show Cursor editor Agent tabs", persisted as `show_cursor_gui_agents` in `monitor_prefs.json`; the three default reads - the `SessionRefreshWorker` gate + the two settings-dialog plumbing reads - all default to `True`, so a user who never touches the toggle gets the rows; the scan is a cheap no-op when Cursor isn't running). When on, `SessionRefreshWorker` calls `scan_open_cursor_agents()` each refresh tick and the monitor shows one row per **open Cursor (the editor) Agent/Composer tab**. These are NOT Leap sessions - they are a pure display overlay (no PTY, no server, no queueing).

**Why it's read-only (but tab-level jump works).** Cursor's Agent tabs live entirely inside its Electron app - no PTY/socket/public API to *drive* them (so no queueing/sending), and Cursor exposes **nothing clickable to macOS Accessibility** (verified live: 0 AX windows, `AXManualAccessibility` unsupported, the window collapses to nested empty `AXGroup`s). But *focusing* a tab is possible via Cursor's own command registry: `focus_cursor_window(folder, composer_id)` raises the window through the System Events AX bridge (`AXRaise` by window title - Cursor has no usable AppleScript dictionary, but System Events can read native window titles = workspace folder names), then writes `focusComposer:<composer_id>` to `~/.leap-terminal-request`. The **Leap Cursor extension** (`src/leap/vscode-extension/extension.js`, Cursor-gated via `isCursor()`) picks that up in the now-foreground window and calls `vscode.commands.executeCommand('composer.openComposer', composerId)` - the **bare id string** is essential: that reaches Cursor's `openComposerImpl` fast-path `selectedComposerIds.includes(id) -> showAndFocus(id)`, the actual visible tab switch (passing an object skips it, which is why `glass.openAgentById` resolved but never switched). Fallbacks `composer.openComposerFromNotification {composerId}` then `glass.openAgentById` cover builds without that command. Confirmed working live. Best-effort: if the extension isn't loaded or a Cursor build gates the command, the window-level raise still happened. The same `.vsix` ships to VS Code too, so the `focusComposer:` handler and the commands are strictly gated to Cursor (`isCursor()`), and a non-Cursor window leaves the shared request file untouched rather than consuming it.

**Data source (all on-disk SQLite, undocumented/version-fragile -> every read is defensive).** `scan_open_cursor_agents()` joins three stores under `~/Library/Application Support/Cursor/User/`: (1) `workspaceStorage/<hash>/workspace.json` -> `{"folder": "file://.../<project>"}`; (2) that workspace's `state.vscdb` -> `ItemTable['composer.composerData'].selectedComposerIds` (the tabs **open in that window**); (3) global `globalStorage/state.vscdb` -> `cursorDiskKV['composerData:<id>']` per tab (`name`, `status`, `generatingBubbleIds`, `hasUnreadMessages`). **Which workspaces are open** is detected from the `state.vscdb` file handles Cursor holds open (`lsof -p <cursor pids>` -> `workspaceStorage/<hash>/state.vscdb`), TTL-cached. This is **frontmost-independent**: System Events / AX only enumerate Cursor's windows when it's the active app, so a window-title approach made the rows vanish whenever you looked away from Cursor. Reads use a read-only WAL connection (`?mode=ro` + `PRAGMA query_only`) with a copy-to-temp fallback, cached by db mtime signature.

**Status mapping** (`_derive_status`, derived from persisted fields - Cursor's richer in-memory `activityState` isn't on disk): `generatingBubbleIds` non-empty OR `status == 'generating'` -> `running`; else `hasUnreadMessages` -> `unread`; else `idle`. Two deliberate, code-verified choices: (1) **no "Aborted"** - Cursor writes `status='aborted'` whenever a generation *ends* (`updateComposer(h, {chatGenerationUUID: undefined, status:'aborted'})`), including normal completion, so it maps to `idle` not a scary "Aborted"; (2) **`unread`, not "replied"** - `hasUnreadMessages` is Cursor's *manual* "Mark as unread" flag (cleared on view), not an auto "agent replied" signal. Status lags the live GUI by a poll tick (disk-flushed).

**Integration design.** Rows carry `row_type == 'cursor_agent_gui'` and a synthetic tag `cursor-gui:<composerId>`. They are kept in `MonitorWindow._cursor_gui_rows`, **never** in `self.sessions`, so they bypass every server-centric path (pinned-session auto-pin, PR tracking, sleep guard, dock badge, `row_order`/drag-drop) with no guards needed - `_update_table` overlays them after the real sessions (highest row indices, so drag guards `index >= len(self.sessions)` naturally skip them) and restores `self.sessions` in `finally`. `_build_cursor_gui_row()` paints all columns itself (no cell-cache) and the Tag cell is **alias-aware**: right-click -> Set/Rename/Remove alias gives a custom Leap-side label persisted by composer id, independent of Cursor. The composer's own `name` (from Cursor's built-in right-click -> "Rename Chat", which writes `composerData.name`) is the default label and flows in automatically.

**Two close buttons (mirrors regular-row semantics).** The leftmost "×" (`_close_cursor_tab_and_untrack`) stops PR tracking AND closes the Agent tab, so the row goes away entirely - the analog of a normal row's delete-X. The Server-cell "×" next to "Open" (`_close_cursor_tab`) closes ONLY the tab and leaves tracking intact - the analog of a normal row's close-server-X that keeps a PR-tracked row alive as a *dead* row. For this to work, `_reconcile_cursor_gui_rows()` (called from `_on_sessions_refreshed`) caches each live row in `MonitorWindow._cursor_row_cache` and, on the next scan, **synthesizes a `_tab_closed` row** (`status_text='○  Tab closed'`) for every tracked Cursor tag whose tab is no longer open - so a tracked tab that's closed via the Server-X stays in the table (and keeps being PR-polled, since the synthesized row carries `project_path`/`branch` and is still in `_tracked_tags`). The synthesized row hides its Server-X (nothing left to close) and its "Open" reopens the chat (best-effort, via the id-based composer command). `_untrack_cursor_pr` drops the cache entry so an untracked closed tab isn't re-synthesized. The stale-prune keeps any tag that's live OR synthesized (tracked-closed), and only cleans up cursor PR state for tags that are neither.

## Monitor Code Signing (the "Leap Self-Signed" cert)

Leap Monitor.app is signed with a per-user self-signed code-signing certificate (CN = `Leap Self-Signed`) stored in the user's login keychain. This is the mechanism that lets macOS Accessibility and Notification grants **survive every `make update` / `leap --update`** — without it, every rebuild changed the bundle's cdhash, which invalidated TCC and forced the user to re-grant Accessibility after every update.

**Why it works.** TCC keys grants on the bundle's *designated requirement*, not its cdhash. With ad-hoc signing (py2app's default), the designated requirement is `cdhash H"..."` — changes on every rebuild. With cert-based signing, it's `identifier "com.leap.monitor" and certificate leaf = H"<cert-sha1>"` — stable across rebuilds because the cert sits unchanged in the keychain.

**One-time generation (`Makefile:.gen-codesign-cert`).** Runs as a prereq for both `install-monitor` and the monitor rebuild in `.update-after-pull`. Idempotent — skips if the cert is already in the keychain. On first generation it also runs `tccutil reset Accessibility com.leap.monitor` to clear any stale cdhash-based entries left by the old ad-hoc scheme. The generation itself is delegated to `src/scripts/leap-codesign-setup.sh`: openssl genrsa → openssl req → openssl pkcs12 -legacy → `security import -T /usr/bin/codesign`. The `-T` ACL is what lets `codesign` use the private key without a "Allow codesign to access key?" dialog on every signing.

**Every build (`Makefile:BUILD_MONITOR_APP`).** Right after `setup.py py2app`, the bundle is re-signed with `codesign --force --deep --sign "Leap Self-Signed" --identifier com.leap.monitor`. The `--deep` is required: the bundle ships ~230 nested Mach-O objects (`Python.framework`, the `MacOS/python` interpreter, many `.dylib`/`.so`), and codesign refuses to seal the bundle when any nested object is unsigned (`code object is not signed at all / In subcomponent: .../python`). Whether the nested binaries arrive ad-hoc-signed depends on which interpreter py2app copied in — Apple-Silicon framework pythons are ad-hoc-signed, but a python.org / custom-built interpreter can be fully unsigned, which made the un-`--deep` sign fail on those machines. `--deep` re-signs every nested object with the cert in one pass; `--identifier com.leap.monitor` is stamped on every signed object, but TCC only matches the **top bundle's** designated requirement, which is derived from the bundle identifier + signing cert (both unchanged) and so stays byte-identical — Accessibility grants survive. The macro then runs `codesign --verify` and prints a clear warning + diagnostic command if signing failed (e.g., cert was removed from keychain). **Do not strip `_CodeSignature` from the installed bundle** — that's the cert signature. Earlier versions of this Makefile stripped it; that's been removed.

**Migration scenario (existing users updating from ad-hoc to cert-signed).** Their existing TCC entry is cdhash-based, won't match the new cert-signed bundle. On their first `leap --update` after this change ships, `.gen-codesign-cert` runs `tccutil reset` once, the new bundle is signed with the fresh cert, and the user re-grants Accessibility once via the in-app banner. After that, all subsequent updates are silent.

**Keychain wipe / new machine.** Cert lives in `~/Library/Keychains/login.keychain-db`. If the user nukes their login keychain or restores from a clean install, `.gen-codesign-cert` regenerates a *new* cert with a different SHA1 → designated requirement changes → one more one-time re-grant. Same on any new machine — the first `make install-monitor` generates a per-machine cert. No cross-machine cert sharing (and we don't want it; it'd require trusting whatever path moved the private key).

**No more install-time permission prompts.** Both `install-monitor` and `.update-after-pull` previously asked "Open Accessibility settings? (Y/n)" and ran a `.prompt-notifications` probe. Both are removed — opening the Settings pane via `x-apple.systempreferences:...` doesn't reliably pre-list the new app (user often has to click `+` and dig through `/Applications`), which is worse UX than the in-app banner flow that uses `AXIsProcessTrustedWithOptions({AXTrustedCheckOptionPrompt: true})` to surface a native macOS dialog with the app pre-selected. The `.prompt-notifications` make target has been deleted entirely.

**Gatekeeper vs TCC.** `spctl --assess` will reject the cert-signed bundle (no Apple Developer ID anchor). That's expected and irrelevant for our use case — Gatekeeper rejection means "macOS warns on first launch from quarantine", but bundles installed via `cp -R` from local builds don't carry the quarantine xattr, so Gatekeeper never runs. TCC operates on a different axis and accepts the self-signed cert just fine.

## Troubleshooting

**"Another client already connected"** → `rm .storage/sockets/<tag>.client.lock`

**Stale sockets** → `leap-cleanup`

**`✗ Leap's hooks aren't configured for <CLI>` at session start** → The session-start gate (`leap-server.py:_enforce_hooks_installed_or_exit`) ran `provider.base_type`'s `hooks_installed()` and got False. Almost always means the user installed that CLI / IDE / terminal *after* `make install` ran (so install-time hook configuration silently skipped it). Fix: `leap --reconfigure`. Same flag also recovers from "user wiped `~/.<cli>/settings.json`" or any other partial-config drift.

**`⚠ Cert-based signing failed — bundle still has its py2app ad-hoc signature`** during build → `.gen-codesign-cert` ran but `codesign --sign "Leap Self-Signed"` couldn't find the cert. Either the user deleted it from Keychain Access, or the import silently failed last time. Check with `security find-certificate -c "Leap Self-Signed" "$HOME/Library/Keychains/login.keychain-db"`. If missing, just re-run `make install-monitor` — `.gen-codesign-cert` will regenerate it (and will also `tccutil reset` so the user re-grants Accessibility once from the in-app banner).

**Accessibility silently fails after update on a machine that should be cert-signed** → Compare `codesign -dr - "/Applications/Leap Monitor.app"` to the TCC entry: `sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" "SELECT hex(csreq) FROM access WHERE client='com.leap.monitor' AND service='kTCCServiceAccessibility';"`. The bundle's designated requirement should be `identifier "com.leap.monitor" and certificate leaf = H"<sha1>"` and the TCC csreq should be its byte-identical encoding. Mismatch usually means the bundle was rebuilt with a different cert (e.g., keychain was wiped between installs). Fix: `tccutil reset Accessibility com.leap.monitor` and have the user re-grant once.

**Notifications/Accessibility silently dead on Apple Silicon (`✗ Architecture mismatch` at build time, or the Monitor never appears in System Settings → Notifications)** → py2app freezes the bundle for the *build interpreter's* architecture, so an **Intel (x86_64) Python on an Apple Silicon Mac** produces an x86_64 app that runs under Rosetta — where AMFI rejects the self-signed binaries (`Error -423 "adhoc signed or signed by an unknown certificate chain"`), the process runs as `<ID of InvalidCode>`, and `usernotificationsd` refuses `requestAuthorization` (so the app never registers; TCC/Accessibility attribution fails the same way). Static `codesign --verify` can still pass — the rejection is at *runtime* (`kernel: cs_invalid_page … tainted:1`). Three layers handle it, all gated on `sysctl -n hw.optional.arm64 == 1` (so Intel Macs are unaffected): (1) `check-python` refuses an x86_64 Python and falls through to `brew install python@3.12` (arm64); (2) `.env` detects an existing Intel venv and does `poetry env remove --all` + `poetry env use` to rebuild it arm64; (3) `BUILD_MONITOR_APP` hard-stops a would-be x86_64 build with the `✗ Architecture mismatch` message. Diagnose with `file "/Applications/Leap Monitor.app/Contents/MacOS/Leap Monitor"` (must say `arm64`) and `cd <repo> && poetry run python -c 'import platform;print(platform.machine())'`. Fix: `cd <repo> && make install` (self-heals — no `make uninstall` needed). Force an x86_64 build with `LEAP_ALLOW_ROSETTA_BUILD=1`.

## Make Commands

```bash
make install           # Install core + configure shell
make install-monitor   # Build and install GUI app
make install-slack-app # Install Slack integration + setup wizard
make reconfigure       # Re-run per-machine integration steps (hooks + IDE/terminal/shell configures); skips deps, monitor, slack, git pull. Use after installing a new CLI/IDE/terminal post-Leap. Same target leap --reconfigure execs into.
make test              # Run the full test suite (unit + integration)
make test-unit         # Run only fast unit tests
make test-integration  # Run only real-PTY integration tests
make run-monitor       # Run monitor from source (no build needed)
make update            # Update to latest version (git pull + rebuild)
make update-deps       # Update Python dependencies only
make uninstall         # Full cleanup (calls uninstall-monitor + uninstall-slack-app)
make uninstall-monitor   # Remove Monitor app only
make uninstall-slack-app # Remove Slack integration only
make clean             # Remove build artifacts
```

## Self-Verification

After writing any fix or feature, **always re-read your own changes and verify there are no bugs** before presenting them as done. Specifically:
- Check edge cases and off-by-one errors
- Verify that conditional branches do what they claim (e.g., a reset that should only trigger on condition A doesn't also trigger on unrelated condition B)
- Trace the flow end-to-end: how is the new code reached, what state does it depend on, and what happens in the common/idle case (not just the interesting case)

## Commit & Push Checklist

**NEVER commit or push without explicit user approval.** Always present the plan and wait for the user to say "commit", "go ahead", or equivalent before running any `git commit` or `git push` command.

When the user asks to commit and push, **before committing**:

1. **Review CLAUDE.md** — Check that it reflects the current codebase. Update any outdated sections (project structure, key classes, features, conventions). Keep it detailed — this is the developer reference.
2. **Review README.md** — Check that it reflects user-facing changes (new features, commands, UI changes). Keep it **concise** — users see this on GitLab. Don't bloat it with implementation details.
3. Only update these files if something actually changed that affects them. Don't touch them for minor internal refactors.
