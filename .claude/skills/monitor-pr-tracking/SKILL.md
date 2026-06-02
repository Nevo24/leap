---
name: monitor-pr-tracking
description: Internals of the Leap Monitor SCM/PR-tracking subsystem and session table - GitLab/GitHub polling and timeouts, PR status markers and merged/closed badges, sending PR comments, /leap auto-fetch, environment-variable tokens, GitHub Enterprise URL handling, user notifications, persistent and pinned rows, the managed-clone dirty-tree sync dialog, the Add-Row flows, branch-mismatch and startup validation, and session-table UX (row ordering, row colors, tag aliases, live filter). Use this when working on monitor PR tracking, SCM polling, or session-table behavior.
user-invocable: false
---

# SCM Polling & PR Tracking

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

