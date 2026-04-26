PACKAGE_NAME     := leap
PYTHON_VERSION   := "3.12"
REPO_PATH        := $(shell git rev-parse --show-toplevel)
PROMPT_PREFIX    := "→"
SRC_DIR          := $(REPO_PATH)/src
SCRIPTS_DIR      := $(SRC_DIR)/scripts

# Ensure ~/.local/bin is in PATH for all recipes (Poetry installer puts poetry there)
export PATH := $(HOME)/.local/bin:$(PATH)

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

# Shell helper: build and install monitor app
define BUILD_MONITOR_APP
echo "$(PROMPT_PREFIX) Building Leap Monitor.app with py2app..."; \
cd $(REPO_PATH) && poetry run python setup.py py2app --dist-dir .dist > /dev/null 2>&1; \
if pgrep -f "Leap Monitor" > /dev/null 2>&1; then \
	echo "$(PROMPT_PREFIX) Closing running Leap Monitor..."; \
	osascript -e 'quit app "Leap Monitor"' 2>/dev/null || true; \
	sleep 1; \
	pkill -f "Leap Monitor" 2>/dev/null || true; \
fi; \
echo "$(PROMPT_PREFIX) Installing Leap Monitor.app..."; \
rm -rf "$(REPO_PATH)/.dist/Leap Monitor.app/Contents/_CodeSignature" 2>/dev/null || true; \
if [ -d "/Applications/Leap Monitor.app" ]; then \
	rm -rf "/Applications/Leap Monitor.app" 2>/dev/null || sudo rm -rf "/Applications/Leap Monitor.app" 2>/dev/null || true; \
fi; \
if cp -R "$(REPO_PATH)/.dist/Leap Monitor.app" /Applications/ 2>/dev/null || sudo cp -R "$(REPO_PATH)/.dist/Leap Monitor.app" /Applications/ 2>/dev/null; then \
	if [ -d "/Applications/Leap Monitor.app/Contents/_CodeSignature" ]; then \
		sudo rm -rf "/Applications/Leap Monitor.app/Contents/_CodeSignature" 2>/dev/null || true; \
	fi; \
	if [ -d "/Applications/Leap Monitor.app/Contents/_CodeSignature" ]; then \
		echo "$(YELLOW)  ⚠ Stale codesignature in /Applications can't be removed (requires IT/admin).$(NC)"; \
		echo "  Installing clean copy to ~/Applications instead..."; \
		rm -rf "/Applications/Leap Monitor.app" 2>/dev/null || true; \
		mkdir -p "$$HOME/Applications"; \
		rm -rf "$$HOME/Applications/Leap Monitor.app"; \
		if cp -R "$(REPO_PATH)/.dist/Leap Monitor.app" "$$HOME/Applications/"; then \
			echo "$(GREEN)✓ Installed to ~/Applications$(NC)"; \
			echo "  Launch Leap Monitor from Spotlight or ~/Applications in Finder."; \
		else \
			echo "$(YELLOW)⚠ Installation to ~/Applications also failed. Check disk space and permissions.$(NC)"; \
			exit 1; \
		fi; \
	else \
		echo "$(GREEN)✓ Installed to /Applications$(NC)"; \
		if [ -d "$$HOME/Applications/Leap Monitor.app" ]; then \
			echo "$(PROMPT_PREFIX) Removing stale ~/Applications copy..."; \
			rm -rf "$$HOME/Applications/Leap Monitor.app"; \
		fi; \
	fi; \
else \
	echo "$(YELLOW)  ⚠ Could not install to /Applications (blocked by system policy).$(NC)"; \
	echo "  Falling back to ~/Applications..."; \
	mkdir -p "$$HOME/Applications"; \
	if [ -d "$$HOME/Applications/Leap Monitor.app" ]; then \
		rm -rf "$$HOME/Applications/Leap Monitor.app"; \
	fi; \
	if cp -R "$(REPO_PATH)/.dist/Leap Monitor.app" "$$HOME/Applications/"; then \
		echo "$(GREEN)✓ Installed to ~/Applications$(NC)"; \
		echo "  To launch: open ~/Applications in Finder, or search 'Leap Monitor' in Spotlight."; \
	else \
		echo "$(YELLOW)⚠ Installation to ~/Applications also failed. Check disk space and permissions.$(NC)"; \
		exit 1; \
	fi; \
fi; \
tccutil reset Accessibility com.leap.monitor 2>/dev/null || true
endef

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
	FOUND_PYTHON=""; \
	for BIN in python$$REQUIRED python3; do \
		if command -v $$BIN &>/dev/null; then \
			VER=$$($$BIN -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null); \
			if [ "$$VER" = "$$REQUIRED" ]; then \
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
	@poetry env info --path > "$(REPO_PATH)/.storage/venv-path"
	@echo "$(REPO_PATH)" > "$(REPO_PATH)/.storage/project-path"
	@echo "   Saved venv: $$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"
	@echo "   Saved project: $$(cat $(REPO_PATH)/.storage/project-path)"

.PHONY: install-monitor
install-monitor: .env ensure-storage write-install-metadata
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
	@echo "$(YELLOW)Optional: Grant macOS permissions for full functionality$(NC)"
	@echo ""
	@echo "  $(YELLOW)Accessibility$(NC) — Required for IDE terminal navigation"
	@read -p "  Open Accessibility settings? (Y/n) " -n 1 -r REPLY_ACC; echo; \
	if [ "$$REPLY_ACC" != "n" ] && [ "$$REPLY_ACC" != "N" ]; then \
		open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"; \
	fi
	@$(MAKE) .prompt-notifications

.PHONY: .prompt-notifications
.prompt-notifications:
	@echo ""
	@echo "$(YELLOW)Notifications — required for banner / Slack / PR alerts$(NC)"
	@if pgrep -f "Leap Monitor.app" > /dev/null 2>&1; then \
		echo "  Leap Monitor is already running — can't probe permission state."; \
		echo "  Quit the app and run 'make install-monitor' again, or enable it"; \
		echo "  directly in System Settings > Notifications > Leap Monitor."; \
	else \
		if [ -f "/Applications/Leap Monitor.app/Contents/MacOS/Leap Monitor" ]; then \
			LEAP_BIN="/Applications/Leap Monitor.app/Contents/MacOS/Leap Monitor"; \
		elif [ -f "$$HOME/Applications/Leap Monitor.app/Contents/MacOS/Leap Monitor" ]; then \
			LEAP_BIN="$$HOME/Applications/Leap Monitor.app/Contents/MacOS/Leap Monitor"; \
		else \
			LEAP_BIN=""; \
		fi; \
		if [ -z "$$LEAP_BIN" ]; then \
			echo "  Could not find Leap Monitor binary to probe notification permissions."; \
			echo "  If notifications are not working, enable them in"; \
			echo "  System Settings > Notifications > Leap Monitor."; \
		else \
			"$$LEAP_BIN" --request-permissions 2>/dev/null; \
			NOTIF_STATUS=$$?; \
			if [ "$$NOTIF_STATUS" = "0" ]; then \
				echo "  $(GREEN)✓ Notifications permission OK.$(NC)"; \
			elif [ "$$NOTIF_STATUS" = "2" ]; then \
				echo "  Notifications declined — you can enable them later in"; \
				echo "  System Settings if you change your mind."; \
			elif [ "$$NOTIF_STATUS" -ge 126 ] 2>/dev/null; then \
				echo "  $(YELLOW)⚠ Notification probe was blocked (process terminated externally).$(NC)"; \
				echo "  If notifications are not working, enable them in"; \
				echo "  System Settings > Notifications > Leap Monitor."; \
			else \
				printf "  Open Notifications settings? (Y/n) "; \
				read -n 1 -r REPLY_NOTIF; echo; \
				if [ "$$REPLY_NOTIF" != "n" ] && [ "$$REPLY_NOTIF" != "N" ]; then \
					open "x-apple.systempreferences:com.apple.Notifications-Settings.extension"; \
				fi; \
			fi; \
		fi; \
	fi

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
	@$(ENSURE_POETRY2); \
	poetry install --no-root --without monitor; \
	echo "$(GREEN)✓ Core dependencies updated$(NC)"
	@$(MAKE) write-install-metadata
	@echo ""
	@echo "$(PROMPT_PREFIX) Updating shell configuration..."
	@$(MAKE) .detect-shell-update
	@if [ -f "$(REPO_PATH)/.storage/slack/config.json" ]; then \
		echo ""; \
		echo "$(PROMPT_PREFIX) Detected Slack integration"; \
		echo "$(PROMPT_PREFIX) Updating Slack dependencies..."; \
		poetry install --no-root --with slack; \
		echo "$(GREEN)✓ Slack updated$(NC)"; \
	else \
		echo ""; \
		echo "  Slack not installed. To install it, run: make install-slack-app"; \
	fi
	@MONITOR_REBUILT=no; \
	if [ -d "/Applications/Leap Monitor.app" ] || [ -d "$$HOME/Applications/Leap Monitor.app" ]; then \
		echo ""; \
		echo "$(PROMPT_PREFIX) Detected Leap Monitor installation"; \
		echo "$(PROMPT_PREFIX) Updating monitor dependencies..."; \
		poetry install --no-root --with monitor; \
		$(BUILD_MONITOR_APP); \
		echo "$(GREEN)✓ Monitor updated$(NC)"; \
		echo ""; \
		echo "$(YELLOW)Note: macOS revokes Accessibility after app rebuild$(NC)"; \
		printf "  Re-open Accessibility settings? (Y/n) "; \
		read -n 1 -r REPLY_ACC; echo; \
		if [ "$$REPLY_ACC" != "n" ] && [ "$$REPLY_ACC" != "N" ]; then \
			open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"; \
		fi; \
		MONITOR_REBUILT=yes; \
	elif [ -f "$(REPO_PATH)/.storage/.migration_had_monitor" ]; then \
		echo ""; \
		echo "$(PROMPT_PREFIX) Old ClaudeQ Monitor was removed during migration"; \
		echo "$(PROMPT_PREFIX) Rebuilding as Leap Monitor..."; \
		rm -f "$(REPO_PATH)/.storage/.migration_had_monitor"; \
		poetry install --no-root --with monitor; \
		$(BUILD_MONITOR_APP); \
		echo "$(GREEN)✓ Leap Monitor installed$(NC)"; \
		echo ""; \
		echo "$(YELLOW)Note: macOS requires Accessibility permission for IDE navigation$(NC)"; \
		printf "  Open Accessibility settings? (Y/n) "; \
		read -n 1 -r REPLY_ACC; echo; \
		if [ "$$REPLY_ACC" != "n" ] && [ "$$REPLY_ACC" != "N" ]; then \
			open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"; \
		fi; \
		MONITOR_REBUILT=yes; \
	else \
		echo ""; \
		echo "  Monitor not installed. To install it, run: make install-monitor"; \
	fi; \
	if [ "$$MONITOR_REBUILT" = "yes" ]; then \
		$(MAKE) .prompt-notifications; \
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

.PHONY: .configure-vscode
.configure-vscode:
	@# Configure VS Code CLI and settings
	@if [ -d "/Applications/Visual Studio Code.app" ]; then \
		echo "$(PROMPT_PREFIX) Configuring VS Code..."; \
		\
		VENV_PY=""; \
		if [ -f "$(REPO_PATH)/.storage/venv-path" ]; then \
			VENV_PY="$$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"; \
		fi; \
		PY=$${VENV_PY:-python3}; \
		\
		VSCODE_BIN="/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"; \
		CODE_SYMLINK="/usr/local/bin/code"; \
		\
		if [ -f "$$VSCODE_BIN" ] && [ ! -f "$$CODE_SYMLINK" ]; then \
			echo "  Installing VS Code CLI command..."; \
			sudo ln -s "$$VSCODE_BIN" "$$CODE_SYMLINK" 2>/dev/null && \
			echo "$(GREEN)  ✓ VS Code CLI installed: code command available$(NC)" || \
			echo "$(YELLOW)  ⚠ Could not install code command (may need sudo)$(NC)"; \
		elif [ -f "$$CODE_SYMLINK" ]; then \
			echo "  ✓ VS Code CLI already installed"; \
		fi; \
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
	@if [ -d "/Applications/Cursor.app" ]; then \
		echo "$(PROMPT_PREFIX) Configuring Cursor..."; \
		\
		VENV_PY=""; \
		if [ -f "$(REPO_PATH)/.storage/venv-path" ]; then \
			VENV_PY="$$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"; \
		fi; \
		PY=$${VENV_PY:-python3}; \
		\
		CURSOR_BIN="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"; \
		CURSOR_SYMLINK="/usr/local/bin/cursor"; \
		\
		if [ -f "$$CURSOR_BIN" ] && [ ! -f "$$CURSOR_SYMLINK" ]; then \
			echo "  Installing Cursor CLI command..."; \
			sudo ln -s "$$CURSOR_BIN" "$$CURSOR_SYMLINK" 2>/dev/null && \
			echo "$(GREEN)  ✓ Cursor CLI installed: cursor command available$(NC)" || \
			echo "$(YELLOW)  ⚠ Could not install cursor command (may need sudo)$(NC)"; \
		elif [ -f "$$CURSOR_SYMLINK" ]; then \
			echo "  ✓ Cursor CLI already installed"; \
		fi; \
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
					$$PY "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" terminal "$$TERMINAL_XML"; \
					\
					if [ -f "$$ADVANCED_XML" ]; then \
						cp "$$ADVANCED_XML" "$$ADVANCED_XML.backup-$$(date +%Y%m%d-%H%M%S)"; \
					fi; \
					$$PY "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" advanced "$$ADVANCED_XML"; \
					\
					echo "  $(GREEN)✓ Configured $$IDE_NAME$(NC)"; \
					if [ -z "$$CONFIGURED_IDES" ]; then \
						CONFIGURED_IDES="$$IDE_NAME"; \
					else \
						CONFIGURED_IDES="$$CONFIGURED_IDES|$$IDE_NAME"; \
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
					$$PY "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" terminal "$$TERMINAL_XML"; \
					\
					if [ -f "$$ADVANCED_XML" ]; then \
						cp "$$ADVANCED_XML" "$$ADVANCED_XML.backup-$$(date +%Y%m%d-%H%M%S)"; \
					fi; \
					$$PY "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" advanced "$$ADVANCED_XML"; \
					\
					echo "  $(GREEN)✓ Configured $$IDE_NAME$(NC)"; \
					if ps aux | grep -i "studio" | grep -v grep > /dev/null 2>&1; then \
						echo "  $(YELLOW)⚠ Please restart Android Studio for changes to take effect$(NC)"; \
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
		if rm -rf "/Applications/Leap Monitor.app" 2>/dev/null || sudo rm -rf "/Applications/Leap Monitor.app" 2>/dev/null; then \
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
		if rm -rf "/Applications/ClaudeQ Monitor.app" 2>/dev/null || sudo rm -rf "/Applications/ClaudeQ Monitor.app" 2>/dev/null; then \
			echo "$(GREEN)✓ Removed ClaudeQ Monitor.app from /Applications$(NC)"; \
			REMOVED=yes; \
		else \
			echo "$(YELLOW)⚠ Could not remove /Applications/ClaudeQ Monitor.app (try manually)$(NC)"; \
		fi; \
	fi; \
	if [ "$$REMOVED" = "no" ]; then \
		echo "  Monitor app not found"; \
	fi
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
	@echo "$(PROMPT_PREFIX) Removing hook files and settings..."
	@rm -f "$$HOME/.claude/hooks/leap-hook.sh" "$$HOME/.claude/hooks/leap-hook-process.py" "$$HOME/.claude/hooks/claudeq-hook.sh" 2>/dev/null || true
	@rm -f "$$HOME/.codex/leap-hook.sh" "$$HOME/.codex/leap-hook-process.py" "$$HOME/.codex/claudeq-hook.sh" 2>/dev/null || true
	@rm -f "$$HOME/.cursor/leap-hook.sh" "$$HOME/.cursor/leap-hook-process.py" 2>/dev/null || true
	@rm -f "$$HOME/.gemini/leap-hook.sh" "$$HOME/.gemini/leap-hook-process.py" 2>/dev/null || true
	@python3 -c "\
import json, os, sys; \
p = os.path.expanduser('~/.claude/settings.json'); \
data = json.load(open(p)) if os.path.exists(p) else {}; \
hooks = data.get('hooks', {}); \
changed = False; \
for key in list(hooks.keys()): \
    filtered = [e for e in hooks[key] if not any('leap-hook' in h.get('command','') or 'claudeq-hook' in h.get('command','') for h in e.get('hooks',[]))]; \
    if len(filtered) != len(hooks[key]): \
        changed = True; \
    if filtered: \
        hooks[key] = filtered; \
    else: \
        del hooks[key]; \
if changed: \
    data['hooks'] = hooks; \
    json.dump(data, open(p,'w'), indent=4); \
    print('  Removed Leap hooks from ~/.claude/settings.json'); \
" 2>/dev/null || true
	@echo "$(GREEN)✓ Removed hook files$(NC)"
	@echo ""
	@echo "$(GREEN)✓ Leap fully uninstalled!$(NC)"
