---
name: monitor-pr-tracking
description: Internals of the Leap Monitor SCM/PR-tracking subsystem and session table - GitLab/GitHub/Bitbucket polling and timeouts, PR status markers and merged/closed badges, sending PR comments, /leap auto-fetch, environment-variable tokens, GitHub Enterprise URL handling, the Bitbucket Cloud/Server dual-API provider, user notifications, persistent and pinned rows, the managed-clone dirty-tree sync dialog, the Add-Row flows, branch-mismatch and startup validation, and session-table UX (row ordering, row colors, tag aliases, live filter). Use this when working on monitor PR tracking, SCM polling, or session-table behavior.
user-invocable: false
---

# SCM Polling & PR Tracking

The monitor polls GitLab/GitHub/Bitbucket for PR status updates and user notifications. Key timeouts:

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

### Bitbucket Provider (Cloud + Server/DC in one class)

`BitbucketProvider` (`pr_tracking/bitbucket_provider.py`) covers **both Bitbucket products behind one Connect button**: bitbucket.org (Cloud, REST 2.0 at `api.bitbucket.org/2.0`) and self-hosted Server/Data Center (REST 1.0 at `<host>/rest/api/1.0`). The flavor is decided once in `__init__` from the URL (`is_bitbucket_cloud_url`) and every endpoint helper branches on `self._is_cloud`; both backends normalize comments into the same internal *thread* dicts so the unresponded//leap logic is written once. Plain `requests` (already a transitive dep) - no SDK.

- **Auth** - config key `auth_user` (the dialog's "Email / username" field): non-empty → HTTP Basic (`email + API token` or `username + app password` on Cloud; `username + token` on Server), empty → `Bearer` header (Cloud workspace/repo access tokens, Server HTTP access tokens). Config file: `.storage/bitbucket_config.json` (`bitbucket_url`, `token`, `auth_user`, `username`, `token_mode`, `poll_interval`, `enable_notifications`).
- **Cloud identity matching** - Cloud comment/participant user objects carry NO username (only `uuid`/`account_id`/`nickname`), so `_is_me` matches on uuid/account_id resolved lazily from `/2.0/user` (`_ensure_self_ids`, cached, 60s retry backoff, never caches an empty identity). Server matches on `user['name']`. Failed identity resolution falls back to nickname matching and flips the scan-unreliable flag (count carried forward).
- **Capability matrix (platform limits, not omissions)** - `draft`: Cloud always; Data Center since 9.3 (older Servers omit the key → False). Cloud's PR `state` enum includes `DRAFT`/`QUEUED` alongside `OPEN` - the open-PR lookup treats all three as open and the branch-listing BBQL excludes the closed states (`state != "MERGED" AND …`) instead of matching `state = "OPEN"`. `has_conflicts`: Server only (`GET …/merge` → `conflicted`); Cloud's `mergeable` field was evaluated and rejected - it means "passes ALL merge checks" (approvals/builds too), so it would false-flag the conflict marker. `changes_requested`: Cloud `participants[].state == 'changes_requested'`, Server `reviewers/participants[].status == 'NEEDS_WORK'`. `checks_failed`: Cloud `…/pullrequests/<id>/statuses`, Server `rest/build-status/1.0/commits/<sha>` - FAILED only, like GitLab. Comment reactions have no public API on either flavor, so the "user reacted = responded" heuristic is skipped (reply or thread-resolve required). Notifications are **Server-only** (reviewer dashboard `rest/api/1.0/dashboard/pull-requests?role=REVIEWER&state=OPEN`, mapped to `review_requested`; skips self-authored and already-approved PRs); Cloud has no notifications API → `supports_notifications()` False; the setup dialog keeps the checkbox enabled and checked-by-default (same look as GitLab/GitHub) and only swaps the tooltip - the saved setting simply has no runtime effect on Cloud (`_get_notif_scm_types` gates Bitbucket on `supports_notifications()` so the poll timer isn't kept alive for it).
- **Path normalization** - canonical project path is `workspace/slug` (Cloud) / `KEY/slug` (Server). `_split_project` also accepts Server HTTPS-clone form `scm/KEY/slug` and web form `projects/KEY/repos/slug`. Git speaks `/scm/KEY/slug.git` on Server (NOT the web path) - `ServerLauncher._build_clone_url` and `validation.build_auth_fetch_url` insert the prefix; `validation._bitbucket_project_key` casefolds + strips prefixes for the startup repo-match (clone URLs lowercase the project key, web URLs uppercase it).
- **Threads** - Cloud: paginated `…/comments`, replies linked by `parent.id` (deleted/pending drafts skipped, `resolution` on the root = resolved). Server: `…/activities` filtered to `COMMENTED` (the root embeds the whole reply tree; duplicate roots from edit activities deduped by id; resolved = `threadResolved` on the root (normal "Resolve thread") OR `state == 'RESOLVED'` (tasks/blockers); anchor from the activity's `commentAnchor` with the comment's own `anchor` as fallback). Flattened Server threads are re-sorted by `createdDate` - depth-first tree order isn't chronological when sub-replies interleave, and the unresponded rule reasons about "the last comment by someone else". Comment deep-links: Cloud `#comment-<id>`, Server `/overview?commentId=<id>`. Server context-path installs (`https://host/stash`) are supported end-to-end: URL parsing keeps the context in `host_url`, and `_split_project` / `_bitbucket_repo_parts` match the `scm/`/`projects/…/repos/…` forms suffix-anchored.
- **403 on notifications** raises `BitbucketAuthError` with `.status = 403` because the poll worker detects auth errors via `getattr(exc, 'status', None)` (PyGithub convention) / `response_code` (python-gitlab).
- **PR URL shapes** (`git_utils`): Cloud `https://bitbucket.org/<ws>/<repo>/pull-requests/<id>`, Server `https://<host>/projects/<KEY>/repos/<slug>/pull-requests/<id>` (parsed first - it would otherwise mis-match the generic Cloud pattern), commits use `/commits/<sha>` (NOT GitLab/GitHub's `/commit/`). Hostname heuristics treat `bitbucket`/`stash` substrings as Bitbucket; the Server web/scm project-URL branches are gated on the host NOT detecting as GitLab/GitHub so a GitLab group literally named `projects` keeps generic handling.

### User Notifications

Per-provider enable/disable via setup dialog. Polls `get_user_notifications()` each cycle. Seen IDs deduplicated via `.storage/notification_seen.json`. First-run seeds all existing notifications as seen. 403 errors auto-disable notifications for that provider.

Banners for these events fire **regardless of monitor-window focus**: they are one-shot (consumed into the seen-set the moment they're detected, never re-fired), so the focused-window suppression that session/PR banners keep would drop them permanently - the launch-time first poll batches everything that arrived while the monitor was off and lands while the freshly opened window is still focused. The foreground-presentation delegate (`willPresentNotification` in `pr_display_mixin.py`) returns Banner|Sound|List so a banner missed or suppressed (Do Not Disturb / Focus) still persists in Notification Center. By design, GitLab todos authored by the user themselves are skipped at the provider (`author == username`) - so self-triggered review requests (e.g. created by automation running under the user's own token) never log or notify. `check_notifications()` (`permissions.py`) detects macOS-side disabled notifications via `ncprefs.plist` bit 25 on macOS <= 15 and via live `UNNotificationSettings.authorizationStatus` on macOS 26+ (the plist no longer exists there).

### Persistent Rows & Pinned Sessions

Rows persist via `pinned_sessions.json`. Key rules:
- Every active session is auto-pinned on discovery
- Row survives if it has a running server OR `pr_tracked: True` set in pinned data OR pinned PR Branch data (`remote_project_path` + non-empty `branch`, mirroring the PR Branch column display rule — Stop PR Tracking leaves these in the pin so the X-to-clear UI still works) OR an in-flight transient flag (`_tracked_tags`, `_checking_tags`, `_starting_tags`, `_moving_tags`)
- Dead rows that are no longer being tracked AND have no displayed PR Branch are auto-removed on the next merge tick (so a row with no PR + no PR Branch + no server never appears in the table)
- PR auto-reconnects on monitor restart for rows with `pr_tracked: True` — that flag is also what keeps the row alive across the startup window before `_auto_track_pr_pinned` populates `_tracked_tags`/`_checking_tags`
- `_deleted_tags` set prevents auto-refresh from re-pinning just-deleted rows

### PR Status Markers, Approval Icons & Merged/Closed Badges

The PR column surfaces more than open/responded state. `PRStatus` (in `pr_tracking/base.py`) carries four qualitative flags, each populated from data the providers already fetch:

| Field | GitHub source | GitLab source | Bitbucket source |
|-------|---------------|---------------|------------------|
| `draft` | `pr.draft` | `draft` / legacy `work_in_progress` | Cloud `pr['draft']`; always False on Server (no draft concept) |
| `has_conflicts` | `mergeable_state == 'dirty'` | `_mr_has_conflicts` (`has_conflicts` / `merge_status=='cannot_be_merged'` / `detailed_merge_status=='conflict'`) | Server `GET …/merge` → `conflicted`; always False on Cloud (no API signal) |
| `changes_requested` | latest review per reviewer == `CHANGES_REQUESTED` | `detailed_merge_status == 'requested_changes'` (best-effort: single top-reason, older servers omit it) | Cloud `participants[].state == 'changes_requested'`; Server `reviewers/participants[].status == 'NEEDS_WORK'` |
| `checks_failed` | `_github_checks_failed` (head-commit check-runs + legacy combined status), **gated on `mergeable_state in ('unstable','blocked')`** so clean PRs cost no extra API call; distinguishes failed from pending | `_mr_pipeline_failed` (`head_pipeline.status == 'failed'` only - never running/pending) | Cloud `…/statuses`, Server `rest/build-status` commit statuses - `FAILED` only (never in-progress) |

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

### Row Ordering (Sort Modes + Drag-and-Drop)

A **Sort** control (toolbar `QToolButton` + checkable, exclusive `QActionGroup`, `objectName` `_leapSortBtn`, built next to the Filter box in `app.py`) picks one of six modes, persisted as `row_sort_mode` in `monitor_prefs.json` (default `'manual'`). `_set_sort_mode()` saves the pref, calls `_refresh_sort_button()` (button caption + checkmark + tooltip), and re-renders; `_SORT_MODE_ORDER` / `_SORT_MODE_LABELS` / `_SORT_MODE_SHORT` (class consts on `MonitorWindow`, order `manual, recent, project, app, cli, tag`) drive the menu order/captions, with a separator after `manual`.

The single authoritative display sort lives in `TableBuilderMixin._sort_for_display(combined)` (called by `_update_table` on the combined sessions + Cursor-row list, replacing the old inline `row_order` sort). It always tops up `row_order` with any not-yet-seen tag (so manual order stays complete in every mode), then sorts:

- **`manual`** (default) — by the drag-arranged `row_order` (insertion time seeded; not alphabetical). New sessions append at the end.
- **`recent`** ("Recently active") — by the latest "fire" timestamp, descending. `_recent_activity_ts(tag)` = `max(_state_changed_at[tag][1], _pr_changed_at[tag][1])` (the same signals that drive the Status/PR fire indicators). One-tick lag is expected (the body pass sets the timestamps *after* the sort reads them); at startup every timestamp is 0 so it degrades to manual order.
- **`project` / `app` / `cli`** — the **categorical / grouped** modes (`_GROUPED_SORT_MODES`, a module const in `table_builder_mixin.py`). Sort key is `_category_sort_key(s, mode)` = `(has_value, value.casefold())`, where `_category_value(s, mode)` returns exactly what the matching column shows: `project` → `s['project']` (`N/A`→blank); `app` → `s['ide']`; `cli` → `get_display_name(cli_provider)` (pinned-provider fallback for dead rows, fixed `'Cursor Editor'` for Cursor rows). Blank/unknown values (flag `1`) sink to the bottom. Each of these modes draws a **thick horizontal divider** (2px `border_solid`, matching the inter-group *column* separator) across the top of every row that starts a new group: `_group_boundaries(sessions)` returns those row indices (keyed by the same `_category_sort_key`, so a divider lands exactly on a sort-group edge; row 0 never qualifies), stashed on the table as the `_group_boundary_rows` property and rendered by `SeparatorDelegate.paint` via `fillRect`. The property is empty in every non-grouped mode (and reset in the empty-table branch), so dividers appear only in Project/App/CLI.
- **`tag`** ("Tag (A-Z)") — `_tag_sort_key` = alias if set, else the Cursor chat `display_label` for `cursor_agent_gui` rows, else the tag (all casefolded). Matches the Tag-column text.

Every automatic mode uses the manual `row_order` position as a **stable tiebreaker**, so equal-key rows keep their manual order (no thrash) and a round-trip through any auto mode and back to Manual preserves the user's arrangement.

**Drag-and-drop** (Manual mode only):
- **Drag detection**: App-level event filter (`eventFilter` in `app.py`) intercepts `MouseButtonPress`/`MouseMove` on cell widgets to initiate a `QDrag`
- **Drop indicator**: A 2px theme-colored line shows the drop position during drag
- **Auto-refresh paused** during drag (`timer.stop()` / `timer.start()`) to prevent table rebuilds from interrupting the gesture
- **Disabled in auto-sort modes**: both `_perform_row_drag` and `_on_row_moved` bail when `_row_sort_mode != 'manual'` (mirroring the existing `_search_query` bail) — the visible order no longer maps to the writable `row_order`, so a drop would have nothing meaningful to persist
- **Cleanup**: When rows are deleted, `_remove_from_row_order()` in `session_mixin.py` removes the tag from the persisted list

Pure-logic coverage: `tests/unit/test_row_sort.py` exercises `_sort_for_display`, `_group_boundaries`, `_category_value`, and the key helpers (project/app/cli/tag/recent) on a `TableBuilderMixin` stub subclass (no QApplication).

### Row Colors

Per-row background colors selectable via a droplet icon button in the Tag column. Persisted as `row_colors: {tag: "#hex"}` in `monitor_prefs.json`.

- **Picker**: `ColorPickerPopup` (in `table_helpers.py`) — 4x4 grid of muted color swatches + Clear button, opened via `_show_color_picker()` in `table_builder_mixin.py`
- **Rendering**: `SeparatorDelegate.paint()` reads `_row_colors` / `_row_tags` table properties and `fillRect`s the row background before the hover overlay
- **Text contrast**: `ensure_contrast()` adjusts text foreground against the row color for both `QTableWidgetItem` cells and child `QLabel`s in widget cells (skips `PulsingLabel`/`IndicatorLabel`)
- **Cleanup**: color is **keyed by tag**, so it must be deleted when the row is removed or it bleeds onto the next `leap <same-tag>`. All removal paths route through `_cleanup_row_state()` (color + alias + row_order + fire-state). There are **five**: `_merge_sessions` auto-remove, `_remove_dead_untracked_row`, `_stop_tracking`'s removal block, `_remove_pinned_session` (Delete-row X), and `_close_server`'s `will_remove` block (the Close-server × button on a non-PR row — the one historically missed, which orphaned colors). Move-to-IDE and Delete-row call `_close_server(_from_delete=True)` so `will_remove` stays False there (Move-to-IDE deliberately *keeps* the color). Because the cleanup only fires while the monitor is running to observe the removal, a removal during a monitor-down window strands the entry on disk (orphans are sticky). `_prune_orphan_row_prefs()` backstops this at **startup** (after the first `_merge_sessions`): any `row_colors`/`aliases` tag not in `_pinned_sessions` / in-flight guards / a `cursor-gui:` prefix is a ghost and gets dropped — a running session is pinned, so a restart never strips a live row's color.

### Tag Aliases

Display aliases for tags, set via right-click context menu on the Tag column. Persisted as `aliases: {tag: "display name"}` in `monitor_prefs.json`.

- **Display**: Aliased tags show the alias in *italic*; the real tag is unchanged everywhere else (files, sockets, server, client)
- **Tooltip**: Aliased tags always show "Alias: X / Tag: Y" (regardless of tooltip setting). Regular tags show on hover when truncated or when "Show hover explanations" is on
- **Context menu**: Right-click tag cell → "Set alias" / "Rename alias" / "Remove alias" via `_show_tag_context_menu()` in `table_builder_mixin.py`
- **Cleanup**: same mechanism as row colors above — aliases are tag-keyed, cleaned via `_cleanup_row_state()` on all five removal paths and backstopped by `_prune_orphan_row_prefs()` at startup, so a reused tag doesn't inherit a removed row's alias

### Live Filter (Search Box)

Substring filter next to the "+ Add Session" button. Per-row matching is `_row_match_rank(s, q)` - the first field (priority Tag → Project → App → CLI → Path) whose lowercased value substring-matches `q`, returned as a rank 0-4 (or `None`). Tag matches also check the user's alias and a Cursor row's `display_label` (so a filter on the visible chat name works). Rows that match nothing are dropped.

**How matches are *ordered* is sort-mode-aware** (`_apply_search_filter`): in **Manual** mode the filter buckets by rank (tag matches first, path last) - a Resume-dialog-style relevance ranking, fine when there's no inherent order. In **any automatic mode** (Recently active / Project / Name) the list is already deliberately ordered, so re-bucketing would override the user's sort (and, in Project mode, split one project across buckets so the group dividers stop lining up) - there the filter preserves the sorted order and only drops non-matches. Regression guard: `test_filter_preserves_sort_order_in_auto_modes`, `test_filter_buckets_in_manual_mode`, `test_filter_project_dividers_stay_clean_under_filter` in `tests/unit/test_row_sort.py`.

- **Wiring**: `QLineEdit` (`self._search_edit`) in the table toolbar; `textChanged` → `_on_search_changed` (in `table_builder_mixin.py`) → updates `self._search_query` → calls `_update_table()`.
- **Filter execution**: `_apply_search_filter(sessions)` returns the filtered view. `_update_table` swaps `self.sessions` for the filtered list via try/finally so the rest of the table-build code path is unchanged; `_update_table_body` (split out from `_update_table`) renders against whatever the wrapper installed. Every other code path on the monitor — drag-drop, PR tracking, sleep guard — sees the full session list because the swap is undone before they read it.
- **Chosen order survives**: in Manual mode each bucket keeps rows in their original `self.sessions` order (drag-reorder isn't reshuffled within a bucket); in an automatic mode the whole sorted order is preserved verbatim.
- **Drag-drop disabled while filter is active**: visible row indices no longer map 1:1 to `self.sessions` when rows are hidden, so reordering would silently move the wrong session. `_perform_row_drag` and `_on_row_moved` both bail out when `self._search_query` is non-empty; user has to clear the filter first.
- **Empty-state copy**: when the filter yields zero rows, the placeholder shows "No matching sessions" (not "No active sessions") so it's clear the filter — not the absence of servers — is what hid everything.
- **Column-width preservation across empty round-trip**: ResizeToContents on COL_DELETE and the `_on_section_resized` redistribute handler both shrink columns when the table empties. Empty branch snapshots widths + COL_DELETE's resize mode, switches COL_DELETE to Interactive, and applies saved widths — all *before* `setRowCount(1)` / `removeCellWidget`, with `_resizing_columns = True` to block the redistribute handler. On the empty→populated transition the saved mode is restored at the start of the populated branch; the existing `resizeColumnToContents(COL_DELETE)` at the end refits the X-button widget. Same mode-toggle pattern in the Resume dialog (`_populate`), where every column gets switched to Interactive on the populated→empty transition and restored on the way back. Not persisted across monitor restarts — filter clears every launch.

