---
name: codebase-map
description: Detailed map of the Leap codebase - the full src/ directory tree annotated with each module and script's role, plus the key-classes reference table mapping every important class or function to its file and purpose. Use this to locate a file, class, module, or helper, or to understand where functionality lives in Leap.
user-invocable: false
---

# Codebase Map

Full annotated source tree and key-classes reference for Leap. (Extracted from CLAUDE.md to keep that file lean; consult this when locating code.)

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
│   ├── leap-hook.sh             # CLI hook script (writes state to signal file)
│   └── leap-copilot-statusline.py  # Copilot status line: writes <tag>.context for the monitor Context column (chains any existing status line)
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
    │   ├── context_usage.py     # Per-CLI context-window % from transcript usage (monitor Context column; Claude/Codex/Gemini parsers)
    │   ├── cost_usage.py        # Cumulative token + USD estimate from the whole transcript (Claude only; dedups split-line entries; incremental accumulator; non-blocking cached wrapper for the GUI thread). Feeds the Context-cell tooltip's "Last message"/"Session total" lines
    │   ├── pricing.py           # Data-driven model pricing (NOT hardcoded): loads vendored assets/model_prices.json (Claude-only trim of LiteLLM's model_prices_and_context_window.json), background-refreshes it into .storage/model_prices.json, overlays cache>vendored. price_for() / turn_cost_usd() (per-token; data-driven >200K premium for Sonnet) / format_usd() / trim_claude() / ensure_fresh_prices()
    │   ├── resume_store.py      # Read/write/prune of cli_sessions/<cli>/<tag>.json (used by hook + picker; + latest_transcript_for)
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
    │   │       ├── note_text_edit.py       # _NoteTextEdit rich editor (image paste, links, Cmd+B/C; RTL box/pipe-table fix: per-block LTR pin + per-cell FSI isolation, stripped on save)
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

