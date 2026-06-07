PACKAGE_NAME     := leap
PYTHON_VERSION   := "3.12"
REPO_PATH        := $(shell git rev-parse --show-toplevel)
PROMPT_PREFIX    := "→"
SRC_DIR          := $(REPO_PATH)/src
SCRIPTS_DIR      := $(SRC_DIR)/scripts

# Ensure ~/.local/bin is in PATH for all recipes (Poetry installer puts poetry there)
export PATH := $(HOME)/.local/bin:$(PATH)

# Strip env vars that can poison every recipe's Python before it even
# starts.  PYTHONHOME from a stale/abandoned venv triggers
# ``Fatal Python error: Failed to import encodings module`` — it makes
# Python look for the stdlib in the wrong place.  VIRTUAL_ENV would
# make poetry try to use whatever venv the user happens to have active,
# instead of Leap's.  These don't affect the user's interactive shell —
# only commands launched from this Makefile.
unexport PYTHONHOME
unexport PYTHONPATH
unexport VIRTUAL_ENV

# Colors for output
GREEN  := \033[0;32m
YELLOW := \033[1;33m
RED    := \033[0;31m
NC     := \033[0m

# Shell helper: ensure Poetry 2.x is available, upgrade if needed
define ENSURE_POETRY2
POETRY_VER=$$(poetry --version 2>/dev/null | grep -oE '[0-9]+' | head -1); \
if [ -n "$$POETRY_VER" ] && [ "$$POETRY_VER" -lt 2 ]; then \
	echo "$(YELLOW)⚠ Poetry 2.x required (found $$(poetry --version)). Upgrading...$(NC)"; \
	curl -sSL https://install.python-poetry.org | python3 -; \
	export PATH="$$HOME/.local/bin:$$PATH"; \
	POETRY_VER=$$(poetry --version 2>/dev/null | grep -oE '[0-9]+' | head -1); \
	if [ -n "$$POETRY_VER" ] && [ "$$POETRY_VER" -lt 2 ]; then \
		echo "$(RED)✗ Poetry upgrade failed. Please upgrade manually: pip install 'poetry>=2'$(NC)"; \
		exit 1; \
	fi; \
	echo "$(GREEN)✓ Poetry upgraded to $$(poetry --version)$(NC)"; \
fi
endef

# Shell helper: detect and set RC_FILE
define GET_RC_FILE
SHELL_NAME=$$(basename $$SHELL); \
if [ "$$SHELL_NAME" = "zsh" ]; then \
	RC_FILE="$$HOME/.zshrc"; \
elif [ "$$SHELL_NAME" = "bash" ]; then \
	RC_FILE="$$HOME/.bashrc"; \
else \
	RC_FILE=""; \
fi
endef

# Shell helper: remove Leap/ClaudeQ config from RC file
define REMOVE_SHELL_CONFIG
if grep -q "Leap Configuration START" "$$RC_FILE"; then \
	sed -i.bak '/Leap Configuration START/,/Leap Configuration END/d' "$$RC_FILE"; \
	rm -f "$$RC_FILE.bak"; \
elif grep -q "# Leap" "$$RC_FILE"; then \
	sed -i.bak '/# Leap/,/# End Leap/d' "$$RC_FILE"; \
	sed -i.bak '/# Leap/,/^alias claudel/d' "$$RC_FILE"; \
	rm -f "$$RC_FILE.bak"; \
fi; \
if grep -q "ClaudeQ Configuration START" "$$RC_FILE"; then \
	sed -i.bak '/ClaudeQ Configuration START/,/ClaudeQ Configuration END/d' "$$RC_FILE"; \
	rm -f "$$RC_FILE.bak"; \
fi
endef

# Shell helper: build and install monitor app.
#
# After py2app builds the bundle we re-sign it with our "Leap Self-Signed"
# code-signing cert, which .gen-codesign-cert keeps in a DEDICATED keychain
# (not the login keychain) so codesign signs silently - no keychain prompt and
# no "Always Allow".  We sign by the cert's SHA-1 (looked up from that keychain)
# and pass --keychain, so signing stays unambiguous even when an older install
# left a same-named cert in the login keychain.  This makes the bundle's
# designated requirement stable across rebuilds:
#
#   designated => identifier "com.leap.monitor" and certificate leaf = H"<cert-sha1>"
#
# macOS TCC keys Accessibility grants on the designated requirement, so a
# rebuild that changes the cdhash but keeps the same signing cert
# preserves the user's Accessibility approval — no more re-granting on
# every update.  The previous ad-hoc signing produced a cdhash-based
# requirement that invalidated TCC on every rebuild, which is why this
# macro used to end with `tccutil reset Accessibility` (now removed).
#
# We sign with `--deep` because the bundle ships ~230 nested Mach-O
# objects (Python.framework, the MacOS/python interpreter, and many
# .dylib/.so files).  codesign refuses to seal the bundle when any
# nested object is unsigned ("code object is not signed at all / In
# subcomponent: .../python").  Whether the nested binaries arrive
# ad-hoc-signed depends on which interpreter py2app copied in — Apple
# Silicon framework pythons are ad-hoc-signed, but a python.org or
# custom-built interpreter can be fully unsigned, which made the sign
# fail on those machines.  `--deep` re-signs every nested object with
# our cert in one pass.  `--identifier com.leap.monitor` is stamped on
# every signed object (nested ones included), but TCC only matches the
# TOP bundle's designated requirement, and that DR is derived from the
# bundle identifier + signing cert — both unchanged — so the requirement
# above stays byte-identical and Accessibility grants survive.
#
# Install strategy: we mirror the freshly-built bundle into /Applications
# with `rsync -a --delete <src>/ <dst>/` - tried first without sudo, then
# (only if that fails) ONCE under sudo.
#
# Why a single rsync instead of `sudo rm` + `sudo cp`: on a non-admin Mac
# /Applications is root:admin, so the install needs elevation; and the
# managed/MDM `sudo` wrappers on these fleets both (a) re-prompt for
# credentials on EVERY invocation (no ~5-min tty cache) AND (b) BLOCK some
# commands by policy - notably `sudo rm -rf` and `sudo sh -c` BOTH return
# "Sudo Command Blocked by IT Support", while `sudo rsync`, `sudo cp`,
# `sudo ln` are allowed.  So we can neither run two separate `sudo rm` +
# `sudo cp` (two prompts) NOR coalesce them with `sudo sh -c '...'`
# (blocked outright).  A single `rsync` does the whole replace in one
# allowed command = one prompt.
#
# rsync also fixes the `cs_invalid_page` hazard for free.  `--delete`
# prunes files the new build dropped, and rsync writes each CHANGED file
# to a temp name then renames it into place, which gives the changed
# Mach-O objects a NEW inode.  That fresh inode is the point: macOS caches
# a code-signing blob per inode, so overwriting a file's bytes in place
# (what `cp -R` over an existing bundle does) leaves the kernel validating
# NEW bytes against the OLD cached signature → the process runs as
# `<ID of InvalidCode>` → `usernotificationsd` refuses
# `requestAuthorization` (notifications/Accessibility silently die).
# rsync's rename-per-changed-file sidesteps that without a separate remove
# step.  `rsync -a` preserves the embedded code signature (verified with
# `codesign --verify --deep --strict`); `--no-owner --no-group` keeps the
# installed bundle root-owned under sudo (like the old `sudo cp`) rather
# than chowning it to the build user.  Admin users (writable
# /Applications) take the non-sudo branch and are never prompted.  If even
# `sudo rsync` fails (fully locked-down fleet), we fall back to installing
# under ~/Applications (a user-writable location).
#
# Build-validity guard: before any rsync we verify the freshly-built
# bundle has its main executable (`Contents/MacOS/Leap Monitor`, non-empty).
# This is critical precisely BECAUSE of `--delete`: if py2app silently
# produced an empty/half-built `.dist` bundle, `rsync --delete <empty>/ <dst>/`
# would mirror-empty the destination and WIPE the installed app (a missing
# src is safe - rsync bails - but an empty src is not).  So we abort
# (exit 1) on a non-runnable build.  The guard sits BEFORE the
# quit-running-Monitor step, so a bad build leaves both the installed app
# and a still-running Monitor untouched.
#
# Re-launch: if the Monitor was running at the start, we close it
# before the install, then `open` the freshly-installed bundle at the
# end.  The re-launch step *always* quits-then-launches (not just
# launches) because the user can manually click Spotlight / Dock
# while we're sitting on the `sudo` password prompt — at that point
# the disk still has the OLD bundle, LaunchServices launches it, and
# the just-spawned process holds open file handles to the OLD bundle's
# inodes, which the install (rsync) then unlinks as it renames the fresh
# files into place.  macOS keeps those unlinked inodes alive for the
# running process, so it goes on executing the OLD code; a bare `open`
# would just bring that stale process to front.
# Quit-then-launch forces a fresh spawn against the new bundle on disk.
define BUILD_MONITOR_APP
if [ "$$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] \
	&& [ "$$(cd $(REPO_PATH) && poetry run python -c 'import platform; print(platform.machine())' 2>/dev/null)" = "x86_64" ] \
	&& [ "$$LEAP_ALLOW_ROSETTA_BUILD" != "1" ]; then \
	echo "$(RED)✗ Architecture mismatch: this is an Apple Silicon Mac, but the build Python is Intel (x86_64).$(NC)"; \
	echo "  The Monitor would be built for x86_64 and run under Rosetta, where macOS (AMFI) rejects"; \
	echo "  our self-signed binaries (error -423) and silently blocks its Notifications + Accessibility."; \
	echo "  Fix: from the Leap repo, run 'make install' (installs an arm64 Python 3.12 and rebuilds natively):"; \
	echo "      cd \"$(REPO_PATH)\" && make install"; \
	echo "  (Set LEAP_ALLOW_ROSETTA_BUILD=1 to build for x86_64 anyway.)"; \
	exit 1; \
fi; \
echo "$(PROMPT_PREFIX) Building Leap Monitor.app with py2app..."; \
cd $(REPO_PATH) && poetry run python setup.py py2app --dist-dir .dist > /dev/null 2>&1; \
echo "$(PROMPT_PREFIX) Signing Leap Monitor.app with Leap Self-Signed cert..."; \
LEAP_KC="$$HOME/Library/Keychains/leap-codesign.keychain-db"; \
LEAP_KC_PASS="$$HOME/Library/Keychains/.leap-codesign.pass"; \
if [ -f "$$LEAP_KC_PASS" ]; then \
	if command -v perl >/dev/null 2>&1; then \
		perl -e 'alarm shift @ARGV; exec @ARGV' 8 security unlock-keychain -p "$$(cat "$$LEAP_KC_PASS")" "$$LEAP_KC" >/dev/null 2>&1 || true; \
	else \
		security unlock-keychain -p "$$(cat "$$LEAP_KC_PASS")" "$$LEAP_KC" >/dev/null 2>&1 || true; \
	fi; \
fi; \
CERT_SHA1=$$(security find-certificate -c "Leap Self-Signed" -Z "$$LEAP_KC" 2>/dev/null | awk '/SHA-1 hash:/{print $$NF}'); \
if [ -z "$$CERT_SHA1" ]; then \
	echo "$(YELLOW)  ⚠ Leap Self-Signed cert not found in the dedicated keychain - skipping cert signing.$(NC)"; \
	SIGN_RC=1; \
else \
	SIGN_OUT=$$(codesign --force --deep --keychain "$$LEAP_KC" --sign "$$CERT_SHA1" \
		--identifier com.leap.monitor \
		"$(REPO_PATH)/.dist/Leap Monitor.app" 2>&1); \
	SIGN_RC=$$?; \
	echo "$$SIGN_OUT" | grep -v "replacing existing signature" || true; \
fi; \
if [ "$$SIGN_RC" -ne 0 ] || ! codesign --verify "$(REPO_PATH)/.dist/Leap Monitor.app" >/dev/null 2>&1; then \
	echo "$(YELLOW)  ⚠ Cert-based signing failed - bundle still has its py2app ad-hoc signature.$(NC)"; \
	echo "  Accessibility will be lost on the next update.  Re-run the cert setup with:"; \
	echo "    bash $(SCRIPTS_DIR)/leap-codesign-setup.sh"; \
fi; \
SRC="$(REPO_PATH)/.dist/Leap Monitor.app"; \
DST="/Applications/Leap Monitor.app"; \
if [ ! -s "$$SRC/Contents/MacOS/Leap Monitor" ]; then \
	echo "$(RED)✗ Build produced no runnable bundle - aborting; existing app left untouched.$(NC)"; \
	exit 1; \
fi; \
WAS_RUNNING=0; \
if pgrep -f "Leap Monitor.app/Contents/MacOS/Leap Monitor" > /dev/null 2>&1; then \
	WAS_RUNNING=1; \
	echo "$(PROMPT_PREFIX) Closing running Leap Monitor..."; \
	osascript -e 'quit app "Leap Monitor"' 2>/dev/null || true; \
	sleep 1; \
	pkill -f "Leap Monitor.app/Contents/MacOS/Leap Monitor" 2>/dev/null || true; \
fi; \
echo "$(PROMPT_PREFIX) Installing Leap Monitor.app..."; \
INSTALL_PATH=""; \
if rsync -a --no-owner --no-group --delete "$$SRC/" "$$DST/" 2>/dev/null \
	|| sudo rsync -a --no-owner --no-group --delete "$$SRC/" "$$DST/" 2>/dev/null; then \
	echo "$(GREEN)✓ Installed to /Applications$(NC)"; \
	INSTALL_PATH="$$DST"; \
	if [ -d "$$HOME/Applications/Leap Monitor.app" ]; then \
		echo "$(PROMPT_PREFIX) Removing stale ~/Applications copy..."; \
		rm -rf "$$HOME/Applications/Leap Monitor.app"; \
	fi; \
else \
	echo "$(YELLOW)  ⚠ Could not install to /Applications (blocked by system policy).$(NC)"; \
	echo "  Falling back to ~/Applications..."; \
	mkdir -p "$$HOME/Applications"; \
	if [ -d "$$HOME/Applications/Leap Monitor.app" ]; then \
		rm -rf "$$HOME/Applications/Leap Monitor.app"; \
	fi; \
	if cp -R "$$SRC" "$$HOME/Applications/"; then \
		echo "$(GREEN)✓ Installed to ~/Applications$(NC)"; \
		echo "  To launch: open ~/Applications in Finder, or search 'Leap Monitor' in Spotlight."; \
		INSTALL_PATH="$$HOME/Applications/Leap Monitor.app"; \
	else \
		echo "$(YELLOW)⚠ Installation to ~/Applications also failed. Check disk space and permissions.$(NC)"; \
		exit 1; \
	fi; \
fi; \
if [ "$$WAS_RUNNING" = "1" ] && [ -n "$$INSTALL_PATH" ]; then \
	if pgrep -f "Leap Monitor.app/Contents/MacOS/Leap Monitor" > /dev/null 2>&1; then \
		osascript -e 'quit app "Leap Monitor"' 2>/dev/null || true; \
		sleep 1; \
		pkill -f "Leap Monitor.app/Contents/MacOS/Leap Monitor" 2>/dev/null || true; \
		sleep 1; \
	fi; \
	echo "$(PROMPT_PREFIX) Re-launching Leap Monitor from $$INSTALL_PATH..."; \
	open "$$INSTALL_PATH" 2>/dev/null || echo "$(YELLOW)  ⚠ Could not auto-relaunch - open Leap Monitor manually.$(NC)"; \
fi
endef

# Set up the "Leap Self-Signed" code-signing cert in a DEDICATED keychain (if
# missing) and make sure that keychain is unlocked and on the search list.
# Runs as a prereq of every monitor build.  leap-codesign-setup.sh is
# idempotent and self-healing: it generates the cert only when absent (then
# clears stale Accessibility entries and prints the one-time re-grant notice),
# and otherwise just re-unlocks the keychain and re-asserts the search-list
# entry.  Full rationale (dedicated keychain vs login keychain, signing by
# SHA-1) is documented in that script and the monitor-code-signing skill.
.PHONY: .gen-codesign-cert
.gen-codesign-cert: check-macos
	@bash $(SCRIPTS_DIR)/leap-codesign-setup.sh || exit $$?

.PHONY: default
default: install

.PHONY: check-macos
check-macos:
	@if [ "$$(uname)" != "Darwin" ]; then \
		echo "$(YELLOW)⚠ Leap is only supported on macOS$(NC)"; \
		exit 1; \
	fi

.PHONY: check-python
check-python:
	@REQUIRED=$(PYTHON_VERSION); \
	IS_ARM=$$(sysctl -n hw.optional.arm64 2>/dev/null); \
	FOUND_PYTHON=""; \
	for BIN in python$$REQUIRED python3; do \
		if command -v $$BIN &>/dev/null; then \
			VER=$$($$BIN -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null); \
			if [ "$$VER" = "$$REQUIRED" ]; then \
				if [ "$$IS_ARM" = "1" ]; then \
					MACH=$$($$BIN -c "import platform; print(platform.machine())" 2>/dev/null); \
					if [ "$$MACH" != "arm64" ]; then \
						echo "  Ignoring $$BIN ($$MACH): need a native arm64 Python on Apple Silicon"; \
						continue; \
					fi; \
				fi; \
				FOUND_PYTHON="$$BIN"; \
				break; \
			fi; \
		fi; \
	done; \
	if [ -n "$$FOUND_PYTHON" ]; then \
		echo "  ✓ Python $$REQUIRED found ($$FOUND_PYTHON)"; \
	else \
		echo "$(YELLOW)⚠ Python $$REQUIRED is required but not found$(NC)"; \
		CURRENT=$$(python3 --version 2>/dev/null || echo "not installed"); \
		echo "  Current: $$CURRENT"; \
		echo ""; \
		if command -v brew &>/dev/null; then \
			printf "  Install Python $$REQUIRED via Homebrew? [Y/n] "; \
			read answer; \
			case "$${answer}" in \
				[nN]*) \
					echo ""; \
					echo "Please install Python $$REQUIRED manually and retry."; \
					exit 1; \
					;; \
				*) \
					echo "$(PROMPT_PREFIX) Installing Python $$REQUIRED via Homebrew..."; \
					brew install python@$$REQUIRED; \
					eval "$$(brew shellenv 2>/dev/null)"; \
					BREW_PREFIX=$$(brew --prefix 2>/dev/null); \
					if [ -n "$$BREW_PREFIX" ]; then \
						export PATH="$$BREW_PREFIX/opt/python@$$REQUIRED/libexec/bin:$$BREW_PREFIX/bin:$$PATH"; \
					fi; \
					hash -r 2>/dev/null; \
					echo "$(GREEN)✓ Python $$REQUIRED installed$(NC)"; \
					;; \
			esac; \
		else \
			echo "  Install Homebrew first: https://brew.sh"; \
			echo "  Then run: brew install python@$$REQUIRED"; \
			exit 1; \
		fi; \
	fi

.PHONY: install
install: check-macos check-python .env .migrate-from-claudeq install-core ensure-storage write-install-metadata configure-shell .configure-hooks
	@echo "$(GREEN)✓ Leap installed successfully!$(NC)"
	@echo ""
	@echo "To start using Leap:"
	@echo "  1. Reload your shell: source ~/.zshrc  (or ~/.bashrc)"
	@echo "  2. Run: leap <tag-name>"
	@echo ""
	@echo "Note: The venv is automatically used by leap commands."
	@echo ""
	@printf "Would you like to install the Monitor GUI? [Y/n] "; \
	read answer; \
	case "$${answer}" in \
		[nN]*) \
			echo ""; \
			echo "You can install it later with:"; \
			echo "  make install-monitor"; \
			echo ""; \
			;; \
		*) \
			$(MAKE) install-monitor; \
			;; \
	esac
	@if [ -f "$(REPO_PATH)/.storage/slack/config.json" ]; then \
		echo "$(GREEN)✓ Slack integration already configured$(NC)"; \
		echo ""; \
	else \
		printf "Would you like to install the Slack integration? [y/N] "; \
		read answer; \
		case "$${answer}" in \
			[yY]*) \
				$(MAKE) install-slack-app; \
				;; \
			*) \
				echo ""; \
				echo "You can install it later with:"; \
				echo "  make install-slack-app"; \
				echo ""; \
				;; \
		esac; \
	fi

.PHONY: install-core
install-core:
	@echo "$(PROMPT_PREFIX) Installing core dependencies..."
	@$(ENSURE_POETRY2); \
	poetry install --no-root --without monitor

.PHONY: ensure-storage
ensure-storage:
	@mkdir -p "$(REPO_PATH)/.storage" \
		"$(REPO_PATH)/.storage/sockets" \
		"$(REPO_PATH)/.storage/queues" \
		"$(REPO_PATH)/.storage/history" \
		"$(REPO_PATH)/.storage/queue_images" \
		"$(REPO_PATH)/.storage/notes" \
		"$(REPO_PATH)/.storage/note_images" \
		"$(REPO_PATH)/.storage/slack" \
		"$(REPO_PATH)/.storage/icon_cache" \
		"$(REPO_PATH)/.storage/state_logs" \
		"$(REPO_PATH)/.storage/cli_sessions" \
		"$(REPO_PATH)/.storage/cli_sessions/claude"

.PHONY: write-install-metadata
write-install-metadata: ensure-storage
	@echo "$(PROMPT_PREFIX) Writing installation metadata to .storage/..."
	@# Atomic write: capture poetry output to a temp file in .storage/,
	@# validate it's a real path that resolves to a python3 binary, then
	@# rename over the destination.  Without this, a `poetry env info`
	@# that exits 0 with empty stdout (happens when poetry's tracked
	@# venv was wiped — e.g. by a Homebrew Python upgrade) silently
	@# blanks .storage/venv-path, breaking every subsequent `leap` call.
	@if ! TMP_VP="$$(mktemp "$(REPO_PATH)/.storage/.venv-path.XXXXXX")"; then \
	    echo "$(RED)✗ Could not create temp file in .storage/$(NC)" >&2; \
	    echo "  Existing .storage/venv-path left unchanged." >&2; \
	    exit 1; \
	fi; \
	if poetry env info --path > "$$TMP_VP" 2>/dev/null \
	   && [ -s "$$TMP_VP" ] \
	   && [ -x "$$(cat "$$TMP_VP")/bin/python3" ]; then \
	    if ! mv "$$TMP_VP" "$(REPO_PATH)/.storage/venv-path"; then \
	        rm -f "$$TMP_VP"; \
	        echo "$(RED)✗ Could not rename temp venv-path into place$(NC)" >&2; \
	        echo "  Existing .storage/venv-path left unchanged." >&2; \
	        exit 1; \
	    fi; \
	else \
	    POETRY_OUT="$$(cat "$$TMP_VP" 2>/dev/null)"; \
	    rm -f "$$TMP_VP"; \
	    echo "$(RED)✗ poetry env info --path returned no usable venv$(NC)" >&2; \
	    echo "  (got: '$$POETRY_OUT' - empty/invalid means poetry's tracked venv is missing)" >&2; \
	    echo "  Existing .storage/venv-path left unchanged." >&2; \
	    echo "  Fix: 'poetry env use $(PYTHON_VERSION)' (or 'make install' from scratch), then retry." >&2; \
	    exit 1; \
	fi
	@echo "$(REPO_PATH)" > "$(REPO_PATH)/.storage/project-path"
	@echo "   Saved venv: $$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"
	@echo "   Saved project: $$(cat $(REPO_PATH)/.storage/project-path)"

.PHONY: install-monitor
install-monitor: .env ensure-storage write-install-metadata .gen-codesign-cert
	@echo "$(PROMPT_PREFIX) Installing monitor dependencies..."
	@poetry install --no-root --with monitor
	@$(BUILD_MONITOR_APP)
	@if [ ! -f "$(REPO_PATH)/.storage/leap_contexts.json" ]; then \
		echo '{"default": "Please try to solve all the issues that are discussed in the following threads:"}' \
			> "$(REPO_PATH)/.storage/leap_contexts.json"; \
	fi
	@echo "$(GREEN)✓ Monitor installed successfully!$(NC)"
	@echo ""
	@echo "Launch Leap Monitor from:"
	@echo "  • Spotlight: Search 'Leap Monitor'"
	@echo "  • Finder: Open Leap Monitor.app from Applications or ~/Applications"
	@echo "  • Dock: Pin it for quick access"
	@echo ""
	@echo "$(YELLOW)Note:$(NC) Leap Monitor will prompt for Accessibility and Notifications"
	@echo "      permissions on first launch. Grant them from the in-app banners -"
	@echo "      this is a one-time step that persists across future updates."

.PHONY: install-slack-app
install-slack-app: .env ensure-storage write-install-metadata
	@echo "$(PROMPT_PREFIX) Installing Slack integration dependencies..."
	@poetry install --no-root --with slack
	@mkdir -p "$(REPO_PATH)/.storage/slack"
	@chmod +x $(SCRIPTS_DIR)/setup-slack-app.sh
	@$(SCRIPTS_DIR)/setup-slack-app.sh "$(REPO_PATH)"

.PHONY: run-monitor
run-monitor:
	@PYTHONPATH=$(SRC_DIR) poetry run python -c "from leap.monitor.app import main; main()"

.PHONY: run-cleanup-sessions
run-cleanup-sessions:
	@$(SCRIPTS_DIR)/leap-cleanup.sh

.PHONY: test
test:
	@echo "$(PROMPT_PREFIX) Running tests..."
	@poetry run pytest tests/

.PHONY: test-unit
test-unit:
	@echo "$(PROMPT_PREFIX) Running unit tests..."
	@poetry run pytest tests/unit/

.PHONY: test-integration
test-integration:
	@echo "$(PROMPT_PREFIX) Running integration tests..."
	@poetry run pytest tests/integration/

.PHONY: clean
clean:
	@echo "$(PROMPT_PREFIX) Cleaning up..."
	@poetry env remove --all
	@rm -rf .pytest_cache .coverage coverage.xml .ruff_cache .mypy_cache
	@rm -rf .storage
	@rm -rf build .dist
	@echo "$(GREEN)✓ Cleaned up build artifacts$(NC)"

.PHONY: lock
lock: .env
	@echo "$(PROMPT_PREFIX) Locking dependencies..."
	@poetry lock

.PHONY: update
update: .env
	@if [ ! -f "$(REPO_PATH)/.storage/venv-path" ]; then \
		echo "$(YELLOW)⚠ Leap is not installed. Run: make install$(NC)"; \
		exit 1; \
	fi
	@$(MAKE) .update-after-pull

.PHONY: .update-after-pull
.update-after-pull:
	@# Run ClaudeQ → Leap migration (no-op if already on Leap)
	@$(MAKE) .migrate-from-claudeq
	@echo "$(PROMPT_PREFIX) Updating core dependencies..."
	@# `&&` (not `;`) so a `poetry install` failure surfaces — with `;`
	@# the success-line `echo` would still run, swallow poetry's
	@# non-zero exit, and let make continue into write-install-metadata
	@# while poetry's venv was in a broken state.  That's the exact
	@# silent-corruption path that blanked users' venv-path file.
	@$(ENSURE_POETRY2) && \
	poetry install --no-root --without monitor && \
	echo "$(GREEN)✓ Core dependencies updated$(NC)"
	@$(MAKE) write-install-metadata
	@echo ""
	@echo "$(PROMPT_PREFIX) Updating shell configuration..."
	@$(MAKE) .detect-shell-update
	@if [ -f "$(REPO_PATH)/.storage/slack/config.json" ]; then \
		echo ""; \
		echo "$(PROMPT_PREFIX) Detected Slack integration"; \
		echo "$(PROMPT_PREFIX) Updating Slack dependencies..."; \
		poetry install --no-root --with slack && \
		echo "$(GREEN)✓ Slack updated$(NC)"; \
	else \
		echo ""; \
		echo "  Slack not installed. To install it, run: make install-slack-app"; \
	fi
	@if [ -d "/Applications/Leap Monitor.app" ] || [ -d "$$HOME/Applications/Leap Monitor.app" ]; then \
		echo ""; \
		echo "$(PROMPT_PREFIX) Detected Leap Monitor installation"; \
		echo "$(PROMPT_PREFIX) Updating monitor dependencies..."; \
		poetry install --no-root --with monitor || exit $$?; \
		$(MAKE) .gen-codesign-cert || exit $$?; \
		$(BUILD_MONITOR_APP) || exit $$?; \
		echo "$(GREEN)✓ Monitor updated$(NC)"; \
	elif [ -f "$(REPO_PATH)/.storage/.migration_had_monitor" ]; then \
		echo ""; \
		echo "$(PROMPT_PREFIX) Old ClaudeQ Monitor was removed during migration"; \
		echo "$(PROMPT_PREFIX) Rebuilding as Leap Monitor..."; \
		rm -f "$(REPO_PATH)/.storage/.migration_had_monitor"; \
		poetry install --no-root --with monitor || exit $$?; \
		$(MAKE) .gen-codesign-cert || exit $$?; \
		$(BUILD_MONITOR_APP) || exit $$?; \
		echo "$(GREEN)✓ Leap Monitor installed$(NC)"; \
	else \
		echo ""; \
		echo "  Monitor not installed. To install it, run: make install-monitor"; \
	fi
	@echo ""
	@echo "$(PROMPT_PREFIX) Updating IDE/terminal configurations..."
	@$(MAKE) .configure-vscode
	@$(MAKE) .configure-cursor
	@$(MAKE) .configure-jetbrains
	@$(MAKE) .configure-iterm2
	@$(MAKE) .configure-wezterm
	@echo "$(GREEN)✓ IDE/terminal configurations updated$(NC)"
	@$(MAKE) .configure-hooks
	@# Remove the update-in-progress marker that leap-update.sh wrote
	@# before its `git pull`.  Phase 2 has now completed successfully, so
	@# WhatsNewDialog should fall back to showing `HEAD..origin/main` and
	@# UpdateCheckWorker should resume its background fetches.  If phase 2
	@# aborts before reaching this line, the marker is left in place and
	@# the 30-min stale-timestamp fallback in the readers handles it.
	@# `make update` (without leap-update.sh) harmlessly no-ops on missing file.
	@rm -f "$(REPO_PATH)/.storage/update_in_progress"
	@echo ""; \
	echo "$(GREEN)✓ Leap updated successfully!$(NC)"; \
	echo ""; \
	echo "Changes applied:"; \
	echo "  • Core code and dependencies updated"; \
	echo "  • Shell configuration updated (flags preserved)"; \
	if [ -d "/Applications/Leap Monitor.app" ] || [ -d "$$HOME/Applications/Leap Monitor.app" ]; then \
		echo "  • Monitor app rebuilt"; \
	fi; \
	if [ -f "$(REPO_PATH)/.storage/slack/config.json" ]; then \
		echo "  • Slack dependencies updated"; \
	fi; \
	echo "  • IDE configurations refreshed"; \
	echo ""; \
	echo "Note: Reload your shell: source ~/.zshrc"

.PHONY: update-deps
update-deps: .env
	@echo "$(PROMPT_PREFIX) Updating dependencies only (no code pull)..."
	@poetry update

# Re-run the per-machine integration steps without pulling code or
# rebuilding heavy artifacts.  Use this after installing a new CLI,
# IDE, or terminal post-Leap (the install-time configures skipped
# whatever wasn't on disk).  Idempotent and safe to re-run.
#
# In scope: migration (no-op for Leap users), install-metadata refresh,
# shell config (only the fenced Leap block), all five IDE/terminal
# configures, CLI hooks.
# Out of scope: git pull, poetry install, monitor rebuild, Slack deps.
.PHONY: reconfigure
reconfigure:
	@echo "$(PROMPT_PREFIX) Re-configuring Leap..."
	@$(MAKE) .migrate-from-claudeq
	@$(MAKE) write-install-metadata
	@$(MAKE) .detect-shell-update
	@$(MAKE) .configure-vscode
	@$(MAKE) .configure-cursor
	@$(MAKE) .configure-jetbrains
	@$(MAKE) .configure-iterm2
	@$(MAKE) .configure-wezterm
	@$(MAKE) .configure-hooks
	@echo ""
	@echo "$(GREEN)✓ Leap re-configured$(NC)"
	@echo "  Reload your shell if .zshrc/.bashrc was updated: source ~/.zshrc"

# Internal targets

.PHONY: .env
.env:
	@# Ensure Homebrew Python is in PATH (needed when brew installed Python
	@# during check-python — that was a different shell, so PATH was lost).
	@if command -v brew &>/dev/null; then \
		eval "$$(brew shellenv 2>/dev/null)"; \
		BREW_PREFIX=$$(brew --prefix 2>/dev/null); \
		if [ -n "$$BREW_PREFIX" ]; then \
			export PATH="$$BREW_PREFIX/opt/python@$(PYTHON_VERSION)/libexec/bin:$$BREW_PREFIX/bin:$$PATH"; \
		fi; \
	fi; \
	if ! command -v poetry &> /dev/null; then \
		echo "$(YELLOW)⚠ Poetry not found, installing...$(NC)"; \
		curl -sSL https://install.python-poetry.org | python3 -; \
		export PATH="$$HOME/.local/bin:$$PATH"; \
	fi; \
	$(ENSURE_POETRY2); \
	if [ "$$(poetry config virtualenvs.create)" = "true" ]; then \
		if [ "$$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then \
			ENV_PATH=$$(poetry env info --path 2>/dev/null); \
			if [ -n "$$ENV_PATH" ] && [ -x "$$ENV_PATH/bin/python" ] \
				&& [ "$$("$$ENV_PATH/bin/python" -c 'import platform; print(platform.machine())' 2>/dev/null)" = "x86_64" ]; then \
				echo "$(PROMPT_PREFIX) Existing virtualenv is Intel (x86_64) on Apple Silicon; recreating as arm64..."; \
				poetry env remove --all 2>/dev/null || true; \
			fi; \
		fi; \
		poetry env use $(PYTHON_VERSION); \
	else \
		echo "Skipping .env target because virtualenv creation is disabled"; \
	fi

.PHONY: configure-shell
configure-shell:
	@echo "$(PROMPT_PREFIX) Configuring shell..."
	@chmod +x $(SCRIPTS_DIR)/leap-main.sh
	@chmod +x $(SCRIPTS_DIR)/claude-leap-main.sh
	@chmod +x $(SCRIPTS_DIR)/codex-leap-main.sh
	@chmod +x $(SCRIPTS_DIR)/copilot-leap-main.sh
	@chmod +x $(SCRIPTS_DIR)/cursor-agent-leap-main.sh
	@chmod +x $(SCRIPTS_DIR)/gemini-leap-main.sh
	@chmod +x $(SCRIPTS_DIR)/leap-update.sh
	@chmod +x $(SCRIPTS_DIR)/leap-select.sh
	@chmod +x $(SCRIPTS_DIR)/leap-select-cli.py
	@chmod +x $(SCRIPTS_DIR)/leap-server.py
	@chmod +x $(SCRIPTS_DIR)/leap-client.py
	@chmod +x $(SCRIPTS_DIR)/leap-monitor.py
	@$(MAKE) .configure-vscode
	@$(MAKE) .configure-cursor
	@$(MAKE) .configure-jetbrains
	@$(MAKE) .configure-iterm2
	@$(MAKE) .configure-wezterm
	@$(MAKE) .detect-shell

# Create the /usr/local/bin/{code,cursor} CLI symlinks for whichever
# editors are installed but lack their symlink, in ONE allowed privileged
# command.  Tried without sudo first (writable /usr/local/bin -> no prompt
# at all), then ONCE under sudo if that fails.
#
# We use a single multi-target `ln -sf <srcs...> /usr/local/bin/` - ln
# links each source into the dir under its basename (VS Code's binary is
# named `code`, Cursor's `cursor`, exactly the link names we want).  This
# matters on non-admin Macs where /usr/local/bin is root:wheel and the
# managed `sudo` wrapper (a) re-prompts on every invocation (no caching)
# and (b) BLOCKS `sudo sh -c` outright ("Sudo Command Blocked by IT
# Support") - so we cannot coalesce with a shell loop; `sudo ln` is
# allowed, and one `ln` with multiple sources = one prompt for both
# editors.  `-f` overwrites a stale/dangling link; we only pass sources
# whose link is missing (the `! -e` guards) so a valid existing link is
# left untouched.  Idempotent: the re-run-on-update case builds an empty
# source list and does nothing (no echo, no sudo).  Invoked from the top
# of BOTH .configure-vscode and .configure-cursor; whichever runs first
# creates every needed link, the second is a silent no-op.
define ENSURE_CLI_SYMLINKS
set --; \
VSCODE_BIN="/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"; \
CURSOR_BIN="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"; \
if [ -f "$$VSCODE_BIN" ] && [ ! -e "/usr/local/bin/code" ]; then \
	set -- "$$@" "$$VSCODE_BIN"; \
fi; \
if [ -f "$$CURSOR_BIN" ] && [ ! -e "/usr/local/bin/cursor" ]; then \
	set -- "$$@" "$$CURSOR_BIN"; \
fi; \
if [ "$$#" -gt 0 ]; then \
	echo "$(PROMPT_PREFIX) Installing editor CLI command(s) (code/cursor)..."; \
	if ln -sf "$$@" /usr/local/bin/ 2>/dev/null \
		|| sudo ln -sf "$$@" /usr/local/bin/ 2>/dev/null; then \
		echo "$(GREEN)  ✓ Editor CLI command(s) installed$(NC)"; \
	else \
		echo "$(YELLOW)  ⚠ Could not install code/cursor command(s) (may need admin)$(NC)"; \
	fi; \
fi
endef

.PHONY: .configure-vscode
.configure-vscode:
	@# Configure VS Code CLI and settings
	@$(ENSURE_CLI_SYMLINKS)
	@if [ -d "/Applications/Visual Studio Code.app" ]; then \
		echo "$(PROMPT_PREFIX) Configuring VS Code..."; \
		\
		VENV_PY=""; \
		if [ -f "$(REPO_PATH)/.storage/venv-path" ]; then \
			VENV_PY="$$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"; \
		fi; \
		PY=$${VENV_PY:-python3}; \
		\
		\
		VSCODE_SETTINGS="$$HOME/Library/Application Support/Code/User/settings.json"; \
		if [ -f "$$VSCODE_SETTINGS" ]; then \
			TITLE_VALUE=$$($$PY -c "import json; data=json.load(open('$$VSCODE_SETTINGS')); print(data.get('terminal.integrated.tabs.title', 'NOT_SET'))" 2>/dev/null); \
			if [ "$$TITLE_VALUE" = "\$${sequence}" ]; then \
				echo "  Removing Leap's terminal.integrated.tabs.title override..."; \
				cp "$$VSCODE_SETTINGS" "$$VSCODE_SETTINGS.backup-$$(date +%Y%m%d-%H%M%S)"; \
				$$PY -c "import json; \
					data = json.load(open('$$VSCODE_SETTINGS')); \
					data.pop('terminal.integrated.tabs.title', None); \
					json.dump(data, open('$$VSCODE_SETTINGS', 'w'), indent=4)" 2>/dev/null && \
				echo "$(GREEN)  ✓ Removed tabs.title override (backup created)$(NC)" || \
				echo "$(YELLOW)  ⚠ Could not update VS Code settings$(NC)"; \
			fi; \
		fi; \
		\
		echo "  Installing Leap Terminal Selector extension..."; \
		CODE_PATH=$$(which code 2>/dev/null); \
		NPM_PATH=$$(which npm 2>/dev/null); \
		if [ -n "$$CODE_PATH" ]; then \
			$$CODE_PATH --uninstall-extension claudeq.claudeq-terminal-selector 2>/dev/null && \
				echo "$(GREEN)  ✓ Removed old ClaudeQ VS Code extension$(NC)" || true; \
			REPO_VERSION=$$($$PY -c "import json; print(json.load(open('$(REPO_PATH)/src/leap/vscode-extension/package.json'))['version'])" 2>/dev/null || echo "0.0.0"); \
			INSTALLED_VERSION=$$($$CODE_PATH --list-extensions --show-versions 2>/dev/null | grep "leap.leap-terminal-selector@" | sed 's/.*@//' || echo "0.0.0"); \
			if [ "$$REPO_VERSION" != "$$INSTALLED_VERSION" ]; then \
				if [ -n "$$NPM_PATH" ]; then \
					cd "$(REPO_PATH)/src/leap/vscode-extension" && \
					$$PY -c "import subprocess,sys; sys.exit(subprocess.run(['npx','--yes','@vscode/vsce','package','--out','leap-terminal-selector.vsix'],capture_output=True,timeout=60).returncode)" 2>/dev/null && \
					$$CODE_PATH --install-extension leap-terminal-selector.vsix --force < /dev/null >/dev/null 2>&1 && \
					rm -f leap-terminal-selector.vsix && \
					echo "$(GREEN)  ✓ Leap extension installed (v$$REPO_VERSION)$(NC)" && \
					echo "$(YELLOW)    → Reload VS Code: Cmd+Shift+P → 'Developer: Reload Window'$(NC)" || \
					echo "$(YELLOW)  ⚠ Could not install extension$(NC)"; \
				else \
					echo "$(YELLOW)  ⚠ npm not found, skipping extension install$(NC)"; \
				fi; \
			else \
				echo "  ✓ Leap extension up to date (v$$INSTALLED_VERSION)"; \
			fi; \
		else \
			echo "$(YELLOW)  ⚠ code command not found, skipping extension install$(NC)"; \
		fi; \
	fi

.PHONY: .configure-cursor
.configure-cursor:
	@# Configure Cursor IDE (VS Code fork) — same extension, different paths
	@$(ENSURE_CLI_SYMLINKS)
	@if [ -d "/Applications/Cursor.app" ]; then \
		echo "$(PROMPT_PREFIX) Configuring Cursor..."; \
		\
		VENV_PY=""; \
		if [ -f "$(REPO_PATH)/.storage/venv-path" ]; then \
			VENV_PY="$$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"; \
		fi; \
		PY=$${VENV_PY:-python3}; \
		\
		\
		CURSOR_SETTINGS="$$HOME/Library/Application Support/Cursor/User/settings.json"; \
		if [ -f "$$CURSOR_SETTINGS" ]; then \
			TITLE_VALUE=$$($$PY -c "import json; data=json.load(open('$$CURSOR_SETTINGS')); print(data.get('terminal.integrated.tabs.title', 'NOT_SET'))" 2>/dev/null); \
			if [ "$$TITLE_VALUE" = "\$${sequence}" ]; then \
				echo "  Removing Leap's terminal.integrated.tabs.title override..."; \
				cp "$$CURSOR_SETTINGS" "$$CURSOR_SETTINGS.backup-$$(date +%Y%m%d-%H%M%S)"; \
				$$PY -c "import json; \
					data = json.load(open('$$CURSOR_SETTINGS')); \
					data.pop('terminal.integrated.tabs.title', None); \
					json.dump(data, open('$$CURSOR_SETTINGS', 'w'), indent=4)" 2>/dev/null && \
				echo "$(GREEN)  ✓ Removed tabs.title override (backup created)$(NC)" || \
				echo "$(YELLOW)  ⚠ Could not update Cursor settings$(NC)"; \
			fi; \
		fi; \
		\
		echo "  Installing Leap Terminal Selector extension..."; \
		CURSOR_PATH=$$(which cursor 2>/dev/null); \
		NPM_PATH=$$(which npm 2>/dev/null); \
		if [ -n "$$CURSOR_PATH" ]; then \
			$$CURSOR_PATH --uninstall-extension claudeq.claudeq-terminal-selector 2>/dev/null && \
				echo "$(GREEN)  ✓ Removed old ClaudeQ extension$(NC)" || true; \
			REPO_VERSION=$$($$PY -c "import json; print(json.load(open('$(REPO_PATH)/src/leap/vscode-extension/package.json'))['version'])" 2>/dev/null || echo "0.0.0"); \
			INSTALLED_VERSION=$$($$CURSOR_PATH --list-extensions --show-versions 2>/dev/null | grep "leap.leap-terminal-selector@" | sed 's/.*@//' || echo "0.0.0"); \
			if [ "$$REPO_VERSION" != "$$INSTALLED_VERSION" ]; then \
				if [ -n "$$NPM_PATH" ]; then \
					cd "$(REPO_PATH)/src/leap/vscode-extension" && \
					$$PY -c "import subprocess,sys; sys.exit(subprocess.run(['npx','--yes','@vscode/vsce','package','--out','leap-terminal-selector.vsix'],capture_output=True,timeout=60).returncode)" 2>/dev/null && \
					$$CURSOR_PATH --install-extension leap-terminal-selector.vsix --force < /dev/null >/dev/null 2>&1 && \
					rm -f leap-terminal-selector.vsix && \
					echo "$(GREEN)  ✓ Leap extension installed (v$$REPO_VERSION)$(NC)" && \
					echo "$(YELLOW)    → Reload Cursor: Cmd+Shift+P → 'Developer: Reload Window'$(NC)" || \
					echo "$(YELLOW)  ⚠ Could not install extension$(NC)"; \
				else \
					echo "$(YELLOW)  ⚠ npm not found, skipping extension install$(NC)"; \
				fi; \
			else \
				echo "  ✓ Leap extension up to date (v$$INSTALLED_VERSION)"; \
			fi; \
		else \
			echo "$(YELLOW)  ⚠ cursor command not found, skipping extension install$(NC)"; \
		fi; \
	fi

.PHONY: .configure-jetbrains
.configure-jetbrains:
	@# Configure JetBrains IDEs terminal settings
	@if [ -d "$$HOME/Library/Application Support/JetBrains" ]; then \
		echo "$(PROMPT_PREFIX) Configuring JetBrains IDEs..."; \
		VENV_PY=""; \
		if [ -f "$(REPO_PATH)/.storage/venv-path" ]; then \
			VENV_PY="$$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"; \
		fi; \
		PY=$${VENV_PY:-python3}; \
		CONFIGURED_IDES=""; \
		for IDE_DIR in "$$HOME/Library/Application Support/JetBrains"/*20*; do \
			if [ -d "$$IDE_DIR/options" ]; then \
				IDE_NAME=$$(basename "$$IDE_DIR"); \
				TERMINAL_XML="$$IDE_DIR/options/terminal.xml"; \
				ADVANCED_XML="$$IDE_DIR/options/advancedSettings.xml"; \
				NEEDS_UPDATE=false; \
				\
				if [ -f "$$TERMINAL_XML" ]; then \
					CURRENT_ENGINE=$$(grep 'name="terminalEngine"' "$$TERMINAL_XML" 2>/dev/null | grep -o 'value="[^"]*"' | head -1 | cut -d'"' -f2); \
					if [ "$$CURRENT_ENGINE" != "CLASSIC" ]; then \
						NEEDS_UPDATE=true; \
					fi; \
				else \
					NEEDS_UPDATE=true; \
				fi; \
				\
				if [ -f "$$ADVANCED_XML" ]; then \
					SHOW_TITLE=$$(grep 'terminal.show.application.title' "$$ADVANCED_XML" 2>/dev/null | grep -o 'value="[^"]*"' | cut -d'"' -f2); \
					if [ "$$SHOW_TITLE" != "true" ]; then \
						NEEDS_UPDATE=true; \
					fi; \
				else \
					NEEDS_UPDATE=true; \
				fi; \
				\
				if [ "$$NEEDS_UPDATE" = "true" ]; then \
					mkdir -p "$$IDE_DIR/options"; \
					\
					if [ -f "$$TERMINAL_XML" ]; then \
						cp "$$TERMINAL_XML" "$$TERMINAL_XML.backup-$$(date +%Y%m%d-%H%M%S)"; \
					fi; \
					CFG_RC=0; \
					$$PY "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" terminal "$$TERMINAL_XML" || CFG_RC=1; \
					\
					if [ -f "$$ADVANCED_XML" ]; then \
						cp "$$ADVANCED_XML" "$$ADVANCED_XML.backup-$$(date +%Y%m%d-%H%M%S)"; \
					fi; \
					$$PY "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" advanced "$$ADVANCED_XML" || CFG_RC=1; \
					\
					if [ "$$CFG_RC" = "0" ]; then \
						echo "  $(GREEN)✓ Configured $$IDE_NAME$(NC)"; \
						if [ -z "$$CONFIGURED_IDES" ]; then \
							CONFIGURED_IDES="$$IDE_NAME"; \
						else \
							CONFIGURED_IDES="$$CONFIGURED_IDES|$$IDE_NAME"; \
						fi; \
					else \
						echo "  $(YELLOW)⚠ Could not fully configure $$IDE_NAME (see warning above).$(NC)"; \
						echo "     Set manually in $$IDE_NAME: Settings > Tools > Terminal engine = Classic, and Advanced Settings > Terminal > Show application title."; \
					fi; \
				else \
					echo "  ✓ $$IDE_NAME already configured"; \
				fi; \
			fi; \
		done; \
		\
		if [ -n "$$CONFIGURED_IDES" ]; then \
			RUNNING_IDES=""; \
			OLD_IFS=$$IFS; \
			IFS='|'; \
			for IDE in $$CONFIGURED_IDES; do \
				if ps aux | grep -i "$$IDE" | grep -v grep > /dev/null 2>&1; then \
					if [ -z "$$RUNNING_IDES" ]; then \
						RUNNING_IDES="$$IDE"; \
					else \
						RUNNING_IDES="$$RUNNING_IDES|$$IDE"; \
					fi; \
				fi; \
			done; \
			IFS=$$OLD_IFS; \
			\
			if [ -n "$$RUNNING_IDES" ]; then \
				echo "  $(YELLOW)⚠ Please restart these running IDEs for changes to take effect:$(NC)"; \
				OLD_IFS=$$IFS; \
				IFS='|'; \
				for IDE in $$RUNNING_IDES; do \
					echo "     • $$IDE"; \
				done; \
				IFS=$$OLD_IFS; \
			else \
				echo "  $(GREEN)✓ Configured IDEs are not currently running - changes will apply on next launch$(NC)"; \
			fi; \
		fi; \
	fi
	@# Configure Android Studio (config lives under Google/, not JetBrains/)
	@if [ -d "$$HOME/Library/Application Support/Google" ]; then \
		for IDE_DIR in "$$HOME/Library/Application Support/Google"/AndroidStudio*; do \
			if [ -d "$$IDE_DIR/options" ]; then \
				IDE_NAME=$$(basename "$$IDE_DIR"); \
				TERMINAL_XML="$$IDE_DIR/options/terminal.xml"; \
				ADVANCED_XML="$$IDE_DIR/options/advancedSettings.xml"; \
				NEEDS_UPDATE=false; \
				\
				if [ -f "$$TERMINAL_XML" ]; then \
					CURRENT_ENGINE=$$(grep 'name="terminalEngine"' "$$TERMINAL_XML" 2>/dev/null | grep -o 'value="[^"]*"' | head -1 | cut -d'"' -f2); \
					if [ "$$CURRENT_ENGINE" != "CLASSIC" ]; then \
						NEEDS_UPDATE=true; \
					fi; \
				else \
					NEEDS_UPDATE=true; \
				fi; \
				\
				if [ -f "$$ADVANCED_XML" ]; then \
					SHOW_TITLE=$$(grep 'terminal.show.application.title' "$$ADVANCED_XML" 2>/dev/null | grep -o 'value="[^"]*"' | cut -d'"' -f2); \
					if [ "$$SHOW_TITLE" != "true" ]; then \
						NEEDS_UPDATE=true; \
					fi; \
				else \
					NEEDS_UPDATE=true; \
				fi; \
				\
				if [ "$$NEEDS_UPDATE" = "true" ]; then \
					echo "$(PROMPT_PREFIX) Configuring Android Studio..."; \
					mkdir -p "$$IDE_DIR/options"; \
					\
					if [ -f "$$TERMINAL_XML" ]; then \
						cp "$$TERMINAL_XML" "$$TERMINAL_XML.backup-$$(date +%Y%m%d-%H%M%S)"; \
					fi; \
					CFG_RC=0; \
					$$PY "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" terminal "$$TERMINAL_XML" || CFG_RC=1; \
					\
					if [ -f "$$ADVANCED_XML" ]; then \
						cp "$$ADVANCED_XML" "$$ADVANCED_XML.backup-$$(date +%Y%m%d-%H%M%S)"; \
					fi; \
					$$PY "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" advanced "$$ADVANCED_XML" || CFG_RC=1; \
					\
					if [ "$$CFG_RC" = "0" ]; then \
						echo "  $(GREEN)✓ Configured $$IDE_NAME$(NC)"; \
						if ps aux | grep -i "studio" | grep -v grep > /dev/null 2>&1; then \
							echo "  $(YELLOW)⚠ Please restart Android Studio for changes to take effect$(NC)"; \
						fi; \
					else \
						echo "  $(YELLOW)⚠ Could not fully configure $$IDE_NAME (see warning above).$(NC)"; \
						echo "     Set manually in $$IDE_NAME: Settings > Tools > Terminal engine = Classic, and Advanced Settings > Terminal > Show application title."; \
					fi; \
				else \
					echo "  ✓ $$IDE_NAME already configured"; \
				fi; \
			fi; \
		done; \
	fi

.PHONY: .configure-iterm2
.configure-iterm2:
	@if [ -d "/Applications/iTerm.app" ] || [ -d "$$HOME/Applications/iTerm.app" ]; then \
		echo "$(PROMPT_PREFIX) Configuring iTerm2..."; \
		VENV_PY=""; \
		if [ -f "$(REPO_PATH)/.storage/venv-path" ]; then \
			VENV_PY="$$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"; \
		fi; \
		PY=$${VENV_PY:-python3}; \
		$$PY "$(SCRIPTS_DIR)/configure_iterm2_csi_u.py"; \
	fi

.PHONY: .configure-wezterm
.configure-wezterm:
	@if [ -d "/Applications/WezTerm.app" ] || [ -d "$$HOME/Applications/WezTerm.app" ] || command -v wezterm >/dev/null 2>&1 || mdfind 'kMDItemCFBundleIdentifier == "com.github.wez.wezterm"' 2>/dev/null | grep -q .; then \
		echo "$(PROMPT_PREFIX) Configuring WezTerm..."; \
		VENV_PY=""; \
		if [ -f "$(REPO_PATH)/.storage/venv-path" ]; then \
			VENV_PY="$$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"; \
		fi; \
		PY=$${VENV_PY:-python3}; \
		$$PY "$(SCRIPTS_DIR)/configure_wezterm_csi_u.py"; \
	fi

.PHONY: .configure-hooks
.configure-hooks:
	@echo "$(PROMPT_PREFIX) Configuring CLI hooks..."
	@PYTHONPATH="$(SRC_DIR):$$PYTHONPATH" "$$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3" "$(SCRIPTS_DIR)/configure_hooks.py" --all "$(SCRIPTS_DIR)/leap-hook.sh"
	@echo "$(GREEN)  ✓ CLI hooks configured$(NC)"

.PHONY: .migrate-from-claudeq
.migrate-from-claudeq:
	@chmod +x $(SCRIPTS_DIR)/migrate-from-claudeq.sh
	@$(SCRIPTS_DIR)/migrate-from-claudeq.sh $(REPO_PATH)

.PHONY: .detect-shell
.detect-shell:
	@chmod +x $(SCRIPTS_DIR)/configure-shell-helper.sh
	@$(SCRIPTS_DIR)/configure-shell-helper.sh $(REPO_PATH)

.PHONY: .detect-shell-update
.detect-shell-update:
	@chmod +x $(SCRIPTS_DIR)/configure-shell-helper.sh
	@$(SCRIPTS_DIR)/configure-shell-helper.sh --update $(REPO_PATH)

.PHONY: uninstall-monitor
uninstall-monitor:
	@echo "$(PROMPT_PREFIX) Uninstalling Leap Monitor..."
	@if pgrep -f "Leap Monitor" > /dev/null 2>&1; then \
		echo "$(PROMPT_PREFIX) Closing running Leap Monitor..."; \
		osascript -e 'quit app "Leap Monitor"' 2>/dev/null || true; \
		sleep 1; \
		pkill -f "Leap Monitor" 2>/dev/null || true; \
	fi
	@REMOVED=no; \
	if [ -d "/Applications/Leap Monitor.app" ]; then \
		if rm -rf "/Applications/Leap Monitor.app" 2>/dev/null || sudo rm -r "/Applications/Leap Monitor.app" 2>/dev/null; then \
			echo "$(GREEN)✓ Removed Leap Monitor.app from /Applications$(NC)"; \
			REMOVED=yes; \
		else \
			echo "$(YELLOW)⚠ Could not remove /Applications/Leap Monitor.app (try manually)$(NC)"; \
		fi; \
	fi; \
	if [ -d "$$HOME/Applications/Leap Monitor.app" ]; then \
		if rm -rf "$$HOME/Applications/Leap Monitor.app"; then \
			echo "$(GREEN)✓ Removed Leap Monitor.app from ~/Applications$(NC)"; \
			REMOVED=yes; \
		else \
			echo "$(YELLOW)⚠ Could not remove ~/Applications/Leap Monitor.app (try manually)$(NC)"; \
		fi; \
	fi; \
	if [ -d "/Applications/ClaudeQ Monitor.app" ]; then \
		if rm -rf "/Applications/ClaudeQ Monitor.app" 2>/dev/null || sudo rm -r "/Applications/ClaudeQ Monitor.app" 2>/dev/null; then \
			echo "$(GREEN)✓ Removed ClaudeQ Monitor.app from /Applications$(NC)"; \
			REMOVED=yes; \
		else \
			echo "$(YELLOW)⚠ Could not remove /Applications/ClaudeQ Monitor.app (try manually)$(NC)"; \
		fi; \
	fi; \
	if [ "$$REMOVED" = "no" ]; then \
		echo "  Monitor app not found"; \
	fi
	@bash $(SCRIPTS_DIR)/leap-codesign-setup.sh --remove || true
	@rm -rf build .dist
	@echo "$(GREEN)✓ Monitor uninstalled successfully!$(NC)"

.PHONY: uninstall-slack-app
uninstall-slack-app:
	@echo "$(PROMPT_PREFIX) Uninstalling Slack integration..."
	@if [ -d "$(REPO_PATH)/.storage/slack" ]; then \
		rm -rf "$(REPO_PATH)/.storage/slack"; \
		echo "$(GREEN)✓ Removed Slack config and session data$(NC)"; \
		echo ""; \
		echo "$(YELLOW)⚠ Slack app still exists on Slack's side$(NC)"; \
		echo "  To remove: visit https://api.slack.com/apps and delete the Leap app"; \
	else \
		echo "  Slack integration not found (no .storage/slack/)"; \
	fi
	@echo "$(GREEN)✓ Slack integration uninstalled!$(NC)"

.PHONY: uninstall
uninstall:
	@echo "$(PROMPT_PREFIX) Uninstalling Leap..."
	@echo "$(PROMPT_PREFIX) Removing CLI hook configurations..."
	@VENV_PY=""; \
	if [ -f "$(REPO_PATH)/.storage/venv-path" ]; then \
		VENV_PY="$$(cat "$(REPO_PATH)/.storage/venv-path")/bin/python3"; \
	fi; \
	if [ -n "$$VENV_PY" ] && [ ! -x "$$VENV_PY" ]; then \
		VENV_PY=""; \
	fi; \
	PY=$${VENV_PY:-python3}; \
	PYTHONPATH="$(SRC_DIR):$$PYTHONPATH" "$$PY" "$(SCRIPTS_DIR)/unconfigure_hooks.py" --all 2>/dev/null || true
	@chmod +x $(SCRIPTS_DIR)/uninstall-helper.sh
	@$(SCRIPTS_DIR)/uninstall-helper.sh $(REPO_PATH)
	@echo "$(PROMPT_PREFIX) Removing Poetry virtual environment..."
	@poetry env remove --all 2>/dev/null || true
	@echo "$(GREEN)✓ Removed Poetry venv$(NC)"
	@$(MAKE) uninstall-monitor
	@$(MAKE) uninstall-slack-app
	@echo "$(PROMPT_PREFIX) Cleaning up cache directories..."
	@rm -rf .pytest_cache .coverage coverage.xml .ruff_cache .mypy_cache
	@rm -f "$(REPO_PATH)/.storage/venv-path" "$(REPO_PATH)/.storage/project-path"
	@echo "$(GREEN)✓ Cleaned up cache directories$(NC)"
	@echo "$(PROMPT_PREFIX) Removing VS Code configuration..."
	@CODE_SYMLINK="/usr/local/bin/code"; \
	if [ -L "$$CODE_SYMLINK" ] && [ "$$(readlink "$$CODE_SYMLINK")" = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" ]; then \
		sudo rm -f "$$CODE_SYMLINK" 2>/dev/null && \
		echo "$(GREEN)✓ Removed VS Code CLI symlink$(NC)" || \
		echo "$(YELLOW)⚠ Could not remove code symlink (may need sudo)$(NC)"; \
	fi; \
	if command -v code >/dev/null 2>&1; then \
		code --uninstall-extension leap.leap-terminal-selector 2>/dev/null && \
			echo "$(GREEN)✓ Removed Leap VS Code extension$(NC)" || true; \
		code --uninstall-extension claudeq.claudeq-terminal-selector 2>/dev/null && \
			echo "$(GREEN)✓ Removed old ClaudeQ VS Code extension$(NC)" || true; \
	fi; \
	VSCODE_SETTINGS="$$HOME/Library/Application Support/Code/User/settings.json"; \
	if [ -f "$$VSCODE_SETTINGS" ]; then \
		TITLE_VALUE=$$(python3 -c "import json; data=json.load(open('$$VSCODE_SETTINGS')); print(data.get('terminal.integrated.tabs.title', 'NOT_SET'))" 2>/dev/null); \
		if [ "$$TITLE_VALUE" = "\$${sequence}" ]; then \
			echo "  Removing Leap's terminal.integrated.tabs.title override..."; \
			python3 -c "import json; \
				data = json.load(open('$$VSCODE_SETTINGS')); \
				data.pop('terminal.integrated.tabs.title', None); \
				json.dump(data, open('$$VSCODE_SETTINGS', 'w'), indent=4)" 2>/dev/null && \
			echo "$(GREEN)✓ Removed Leap VS Code settings$(NC)" || \
			echo "$(YELLOW)⚠ Could not update VS Code settings$(NC)"; \
		fi; \
	fi
	@echo "$(PROMPT_PREFIX) Removing Cursor configuration..."
	@CURSOR_SYMLINK="/usr/local/bin/cursor"; \
	if [ -L "$$CURSOR_SYMLINK" ] && [ "$$(readlink "$$CURSOR_SYMLINK")" = "/Applications/Cursor.app/Contents/Resources/app/bin/cursor" ]; then \
		sudo rm -f "$$CURSOR_SYMLINK" 2>/dev/null && \
		echo "$(GREEN)✓ Removed Cursor CLI symlink$(NC)" || \
		echo "$(YELLOW)⚠ Could not remove cursor symlink (may need sudo)$(NC)"; \
	fi; \
	if command -v cursor >/dev/null 2>&1; then \
		cursor --uninstall-extension leap.leap-terminal-selector 2>/dev/null && \
			echo "$(GREEN)✓ Removed Leap Cursor extension$(NC)" || true; \
		cursor --uninstall-extension claudeq.claudeq-terminal-selector 2>/dev/null && \
			echo "$(GREEN)✓ Removed old ClaudeQ Cursor extension$(NC)" || true; \
	fi; \
	CURSOR_SETTINGS="$$HOME/Library/Application Support/Cursor/User/settings.json"; \
	if [ -f "$$CURSOR_SETTINGS" ]; then \
		TITLE_VALUE=$$(python3 -c "import json; data=json.load(open('$$CURSOR_SETTINGS')); print(data.get('terminal.integrated.tabs.title', 'NOT_SET'))" 2>/dev/null); \
		if [ "$$TITLE_VALUE" = "\$${sequence}" ]; then \
			echo "  Removing Leap's terminal.integrated.tabs.title override..."; \
			python3 -c "import json; \
				data = json.load(open('$$CURSOR_SETTINGS')); \
				data.pop('terminal.integrated.tabs.title', None); \
				json.dump(data, open('$$CURSOR_SETTINGS', 'w'), indent=4)" 2>/dev/null && \
			echo "$(GREEN)✓ Removed Leap Cursor settings$(NC)" || \
			echo "$(YELLOW)⚠ Could not update Cursor settings$(NC)"; \
		fi; \
	fi
	@echo "$(PROMPT_PREFIX) Removing hook script files (safety net)..."
	@rm -f "$$HOME/.claude/hooks/leap-hook.sh" "$$HOME/.claude/hooks/leap-hook-process.py" "$$HOME/.claude/hooks/claudeq-hook.sh" 2>/dev/null || true
	@rm -f "$$HOME/.codex/leap-hook.sh" "$$HOME/.codex/leap-hook-process.py" "$$HOME/.codex/claudeq-hook.sh" 2>/dev/null || true
	@rm -f "$$HOME/.cursor/leap-hook.sh" "$$HOME/.cursor/leap-hook-process.py" 2>/dev/null || true
	@rm -f "$$HOME/.gemini/leap-hook.sh" "$$HOME/.gemini/leap-hook-process.py" 2>/dev/null || true
	@rm -f "$$HOME/.copilot/leap-hook.sh" "$$HOME/.copilot/leap-hook-process.py" "$$HOME/.copilot/leap-copilot-statusline.py" "$$HOME/.copilot/leap-statusline-chain" 2>/dev/null || true
	@echo "$(GREEN)✓ Removed hook files$(NC)"
	@echo ""
	@echo "$(GREEN)✓ Leap fully uninstalled!$(NC)"
