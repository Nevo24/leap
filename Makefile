PACKAGE_NAME     := claudeq
PYTHON_VERSION   := "3.12"
REPO_PATH        := $(shell git rev-parse --show-toplevel)
PROMPT_PREFIX    := "→"
SRC_DIR          := $(REPO_PATH)/src
SCRIPTS_DIR      := $(SRC_DIR)/scripts

# Colors for output
GREEN  := \033[0;32m
YELLOW := \033[1;33m
NC     := \033[0m

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

# Shell helper: remove ClaudeQ config from RC file
define REMOVE_SHELL_CONFIG
if grep -q "ClaudeQ Configuration START" "$$RC_FILE"; then \
	cp "$$RC_FILE" "$$RC_FILE.backup-$$(date +%Y%m%d-%H%M%S)"; \
	sed -i.bak '/ClaudeQ Configuration START/,/ClaudeQ Configuration END/d' "$$RC_FILE"; \
	rm -f "$$RC_FILE.bak"; \
elif grep -q "# ClaudeQ" "$$RC_FILE"; then \
	cp "$$RC_FILE" "$$RC_FILE.backup-$$(date +%Y%m%d-%H%M%S)"; \
	sed -i.bak '/# ClaudeQ/,/# End ClaudeQ/d' "$$RC_FILE"; \
	sed -i.bak '/# ClaudeQ/,/^alias cq/d' "$$RC_FILE"; \
	rm -f "$$RC_FILE.bak"; \
fi
endef

# Shell helper: build and install monitor app
define BUILD_MONITOR_APP
echo "$(PROMPT_PREFIX) Building ClaudeQ Monitor.app with py2app..."; \
cd $(REPO_PATH) && poetry run python setup.py py2app --dist-dir .dist > /dev/null 2>&1; \
echo "$(PROMPT_PREFIX) Installing ClaudeQ Monitor.app to /Applications..."; \
if [ -d "/Applications/ClaudeQ Monitor.app" ]; then \
	sudo rm -rf "/Applications/ClaudeQ Monitor.app"; \
fi; \
sudo cp -R "$(REPO_PATH)/.dist/ClaudeQ Monitor.app" /Applications/; \
tccutil reset Accessibility com.claudeq.monitor 2>/dev/null || true
endef

.PHONY: default
default: install

.PHONY: install
install: .env install-core ensure-storage write-install-metadata configure-shell .configure-claude-hooks
	@echo "$(GREEN)✓ ClaudeQ installed successfully!$(NC)"
	@echo ""
	@echo "To start using ClaudeQ:"
	@echo "  1. Reload your shell: source ~/.zshrc  (or ~/.bashrc)"
	@echo "  2. Run: cq <tag-name>"
	@echo ""
	@echo "Note: The venv is automatically used by claudeq commands."
	@echo ""
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

.PHONY: install-core
install-core:
	@echo "$(PROMPT_PREFIX) Installing core dependencies..."
	@poetry install --no-root --without monitor

.PHONY: ensure-storage
ensure-storage:
	@mkdir -p "$(REPO_PATH)/.storage" \
		"$(REPO_PATH)/.storage/sockets" \
		"$(REPO_PATH)/.storage/queues" \
		"$(REPO_PATH)/.storage/history" \
		"$(REPO_PATH)/.storage/slack"

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
	@# Ask about Slack BEFORE building the .app so Slack deps get bundled
	@if [ -f "$(REPO_PATH)/.storage/slack/config.json" ]; then \
		echo "$(GREEN)✓ Slack integration already configured$(NC)"; \
		echo "$(PROMPT_PREFIX) Including Slack dependencies in monitor build..."; \
		poetry install --no-root --with slack; \
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
	@$(BUILD_MONITOR_APP)
	@if [ ! -f "$(REPO_PATH)/.storage/cq_contexts.json" ]; then \
		echo '{"default": "Please try to solve all the issues that are discussed in the following threads:"}' \
			> "$(REPO_PATH)/.storage/cq_contexts.json"; \
	fi
	@echo "$(GREEN)✓ Monitor installed successfully!$(NC)"
	@echo ""
	@echo "Launch ClaudeQ Monitor from:"
	@echo "  • Spotlight: Search 'ClaudeQ Monitor'"
	@echo "  • Applications: Double-click ClaudeQ Monitor.app"
	@echo "  • Dock: Pin it for quick access"
	@echo ""

.PHONY: install-slack-app
install-slack-app: .env ensure-storage write-install-metadata
	@echo "$(PROMPT_PREFIX) Installing Slack integration dependencies..."
	@poetry install --no-root --with slack
	@mkdir -p "$(REPO_PATH)/.storage/slack"
	@chmod +x $(SCRIPTS_DIR)/setup-slack-app.sh
	@$(SCRIPTS_DIR)/setup-slack-app.sh "$(REPO_PATH)"

.PHONY: run-monitor
run-monitor:
	@PYTHONPATH=$(SRC_DIR) poetry run python -c "from claudeq.monitor.app import main; main()"

.PHONY: run-cleanup-sessions
run-cleanup-sessions:
	@$(SCRIPTS_DIR)/claudeq-cleanup.sh

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
	@poetry lock --no-update

.PHONY: update
update:
	@echo "$(PROMPT_PREFIX) Updating ClaudeQ..."
	@$(GET_RC_FILE); \
	if [ ! -f "$$RC_FILE" ] || ! grep -q "ClaudeQ Configuration" "$$RC_FILE"; then \
		echo "$(YELLOW)⚠ ClaudeQ does not appear to be installed$(NC)"; \
		echo "  No ClaudeQ configuration found in $$RC_FILE"; \
		echo ""; \
		echo "Please run 'make install' first to install ClaudeQ."; \
		echo "After installation, you can use 'make update' to update to newer versions."; \
		exit 1; \
	fi
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "$(YELLOW)⚠ You have uncommitted local changes:$(NC)"; \
		git status --short; \
		echo ""; \
		echo "Please commit or stash your changes before updating."; \
		exit 1; \
	fi
	@UPSTREAM=$$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null); \
	if [ -n "$$UPSTREAM" ]; then \
		LOCAL=$$(git rev-parse HEAD); \
		REMOTE=$$(git rev-parse "$$UPSTREAM" 2>/dev/null); \
		BASE=$$(git merge-base HEAD "$$UPSTREAM" 2>/dev/null); \
		if [ "$$LOCAL" != "$$REMOTE" ] && [ "$$REMOTE" = "$$BASE" ]; then \
			echo "$(YELLOW)⚠ You have local commits that haven't been pushed:$(NC)"; \
			git log --oneline "$$UPSTREAM"..HEAD; \
			echo ""; \
			read -p "  Continue updating anyway? Your commits may conflict. (y/N) " -n 1 -r REPLY; \
			echo; \
			if [ "$$REPLY" != "y" ] && [ "$$REPLY" != "Y" ]; then \
				echo "Update cancelled. Push your changes first, then retry."; \
				exit 1; \
			fi; \
		fi; \
	fi
	@echo "$(PROMPT_PREFIX) Pulling latest code from git..."
	@git pull || (echo "$(YELLOW)⚠ Git pull failed. Please resolve conflicts and try again.$(NC)" && exit 1)
	@echo "$(GREEN)✓ Code updated$(NC)"
	@echo ""
	@echo "$(PROMPT_PREFIX) Updating core dependencies..."
	@poetry install --no-root --without monitor
	@echo "$(GREEN)✓ Core dependencies updated$(NC)"
	@$(MAKE) write-install-metadata
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
	@if [ -d "/Applications/ClaudeQ Monitor.app" ]; then \
		echo ""; \
		echo "$(PROMPT_PREFIX) Detected ClaudeQ Monitor installation"; \
		echo "$(PROMPT_PREFIX) Updating monitor dependencies..."; \
		poetry install --no-root --with monitor; \
		$(BUILD_MONITOR_APP); \
		echo "$(GREEN)✓ Monitor updated$(NC)"; \
	else \
		echo ""; \
		echo "  Monitor not installed. To install it, run: make install-monitor"; \
	fi
	@echo ""
	@echo "$(PROMPT_PREFIX) Updating IDE configurations..."
	@$(MAKE) .configure-vscode
	@$(MAKE) .configure-jetbrains
	@echo "$(GREEN)✓ IDE configurations updated$(NC)"
	@$(MAKE) .configure-claude-hooks
	@echo ""
	@$(GET_RC_FILE); \
	if [ -f "$$RC_FILE" ] && grep -q "ClaudeQ Configuration START" "$$RC_FILE"; then \
		echo "$(YELLOW)⚠ Shell configuration detected$(NC)"; \
		echo "  Your shell config is managed between START/END markers."; \
		echo "  If the claudeq function has changed, you may want to update it."; \
		echo ""; \
		read -p "  Update shell configuration? (y/N) " -n 1 -r REPLY; \
		echo; \
		if [ "$$REPLY" = "y" ] || [ "$$REPLY" = "Y" ]; then \
			sed -i.bak '/ClaudeQ Configuration START/,/ClaudeQ Configuration END/d' "$$RC_FILE"; \
			rm -f "$$RC_FILE.bak"; \
			echo "$(GREEN)  Removed old configuration$(NC)"; \
			$(MAKE) .detect-shell; \
		else \
			echo "  Skipped shell configuration update."; \
			echo "  To update manually later, run: make install"; \
		fi; \
	fi; \
	echo ""; \
	echo "$(GREEN)✓ ClaudeQ updated successfully!$(NC)"; \
	echo ""; \
	echo "Changes applied:"; \
	echo "  • Core code and dependencies updated"; \
	if [ -d "/Applications/ClaudeQ Monitor.app" ]; then \
		echo "  • Monitor app rebuilt"; \
	fi; \
	if [ -f "$(REPO_PATH)/.storage/slack/config.json" ]; then \
		echo "  • Slack dependencies updated"; \
	fi; \
	echo "  • IDE configurations refreshed"; \
	echo ""; \
	echo "Note: Reload your shell if config was updated: source ~/.zshrc"

.PHONY: update-deps
update-deps: .env
	@echo "$(PROMPT_PREFIX) Updating dependencies only (no code pull)..."
	@poetry update

# Internal targets

.PHONY: .env
.env:
	@if ! command -v poetry &> /dev/null; then \
		echo "$(YELLOW)⚠ Poetry not found, installing...$(NC)"; \
		curl -sSL https://install.python-poetry.org | python3 -; \
		export PATH="$$HOME/.local/bin:$$PATH"; \
	fi
	@if [ "$$(poetry config virtualenvs.create)" = "true" ]; then \
		poetry env use $(PYTHON_VERSION); \
	else \
		echo "Skipping .env target because virtualenv creation is disabled"; \
	fi

.PHONY: configure-shell
configure-shell:
	@echo "$(PROMPT_PREFIX) Configuring shell..."
	@chmod +x $(SCRIPTS_DIR)/claudeq-main.sh
	@chmod +x $(SCRIPTS_DIR)/claudeq-server.py
	@chmod +x $(SCRIPTS_DIR)/claudeq-client.py
	@chmod +x $(SCRIPTS_DIR)/claudeq-monitor.py
	@$(MAKE) .configure-vscode
	@$(MAKE) .configure-jetbrains
	@$(MAKE) .detect-shell

.PHONY: .configure-vscode
.configure-vscode:
	@# Configure VS Code CLI and settings
	@if [ -d "/Applications/Visual Studio Code.app" ]; then \
		echo "$(PROMPT_PREFIX) Configuring VS Code..."; \
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
			TITLE_VALUE=$$(python3 -c "import json; data=json.load(open('$$VSCODE_SETTINGS')); print(data.get('terminal.integrated.tabs.title', 'NOT_SET'))" 2>/dev/null); \
			SHELL_INT=$$(python3 -c "import json; data=json.load(open('$$VSCODE_SETTINGS')); print(data.get('terminal.integrated.shellIntegration.enabled', 'NOT_SET'))" 2>/dev/null); \
			NEEDS_UPDATE=false; \
			[ "$$TITLE_VALUE" != "\$${sequence}" ] && NEEDS_UPDATE=true; \
			[ "$$SHELL_INT" != "True" ] && NEEDS_UPDATE=true; \
			if [ "$$NEEDS_UPDATE" = "true" ]; then \
				echo "  Updating VS Code settings for terminal titles..."; \
				cp "$$VSCODE_SETTINGS" "$$VSCODE_SETTINGS.backup-$$(date +%Y%m%d-%H%M%S)"; \
				python3 -c "import json, sys; \
					data = json.load(open('$$VSCODE_SETTINGS')); \
					data['terminal.integrated.tabs.title'] = '\$${sequence}'; \
					data['terminal.integrated.shellIntegration.enabled'] = True; \
					json.dump(data, open('$$VSCODE_SETTINGS', 'w'), indent=4)" 2>/dev/null && \
				echo "$(GREEN)  ✓ VS Code settings updated (backup created)$(NC)" || \
				echo "$(YELLOW)  ⚠ Could not update VS Code settings$(NC)"; \
			else \
				echo "  ✓ VS Code terminal title settings already configured"; \
			fi; \
		elif [ -d "$$HOME/Library/Application Support/Code/User" ]; then \
			echo "  Creating VS Code settings.json..."; \
			mkdir -p "$$HOME/Library/Application Support/Code/User" && \
			printf '{\n    "terminal.integrated.tabs.title": "\$${sequence}",\n    "terminal.integrated.shellIntegration.enabled": true\n}' > "$$VSCODE_SETTINGS" && \
			echo "$(GREEN)  ✓ VS Code settings.json created$(NC)" || \
			echo "$(YELLOW)  ⚠ Could not create settings.json$(NC)"; \
		fi; \
		\
		echo "  Installing ClaudeQ Terminal Selector extension..."; \
		CODE_PATH=$$(which code 2>/dev/null); \
		NPM_PATH=$$(which npm 2>/dev/null); \
		if [ -n "$$CODE_PATH" ]; then \
			REPO_VERSION=$$(python3 -c "import json; print(json.load(open('$(REPO_PATH)/src/claudeq/vscode-extension/package.json'))['version'])" 2>/dev/null || echo "0.0.0"); \
			INSTALLED_VERSION=$$($$CODE_PATH --list-extensions --show-versions 2>/dev/null | grep "claudeq.claudeq-terminal-selector@" | sed 's/.*@//' || echo "0.0.0"); \
			if [ "$$REPO_VERSION" != "$$INSTALLED_VERSION" ]; then \
				if [ -n "$$NPM_PATH" ]; then \
					cd "$(REPO_PATH)/src/claudeq/vscode-extension" && \
					npx --yes @vscode/vsce package --out claudeq-terminal-selector.vsix >/dev/null 2>&1 && \
					$$CODE_PATH --install-extension claudeq-terminal-selector.vsix --force >/dev/null 2>&1 && \
					rm -f claudeq-terminal-selector.vsix && \
					echo "$(GREEN)  ✓ ClaudeQ extension installed (v$$REPO_VERSION)$(NC)" && \
					echo "$(YELLOW)    → Reload VS Code: Cmd+Shift+P → 'Developer: Reload Window'$(NC)" || \
					echo "$(YELLOW)  ⚠ Could not install extension$(NC)"; \
				else \
					echo "$(YELLOW)  ⚠ npm not found, skipping extension install$(NC)"; \
				fi; \
			else \
				echo "  ✓ ClaudeQ extension up to date (v$$INSTALLED_VERSION)"; \
			fi; \
		else \
			echo "$(YELLOW)  ⚠ code command not found, skipping extension install$(NC)"; \
		fi; \
	fi

.PHONY: .configure-jetbrains
.configure-jetbrains:
	@# Configure JetBrains IDEs terminal settings
	@if [ -d "$$HOME/Library/Application Support/JetBrains" ]; then \
		echo "$(PROMPT_PREFIX) Configuring JetBrains IDEs..."; \
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
					python3 "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" terminal "$$TERMINAL_XML"; \
					\
					if [ -f "$$ADVANCED_XML" ]; then \
						cp "$$ADVANCED_XML" "$$ADVANCED_XML.backup-$$(date +%Y%m%d-%H%M%S)"; \
					fi; \
					python3 "$(SCRIPTS_DIR)/configure_jetbrains_xml.py" advanced "$$ADVANCED_XML"; \
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

.PHONY: .configure-claude-hooks
.configure-claude-hooks:
	@echo "$(PROMPT_PREFIX) Configuring Claude Code hooks..."
	@mkdir -p "$$HOME/.claude/hooks"
	@cp "$(SCRIPTS_DIR)/claudeq-hook.sh" "$$HOME/.claude/hooks/claudeq-hook.sh"
	@chmod +x "$$HOME/.claude/hooks/claudeq-hook.sh"
	@python3 "$(SCRIPTS_DIR)/configure_claude_hooks.py" "$$HOME/.claude/hooks/claudeq-hook.sh"
	@echo "$(GREEN)  ✓ Claude Code hooks configured$(NC)"

.PHONY: .detect-shell
.detect-shell:
	@chmod +x $(SCRIPTS_DIR)/configure-shell-helper.sh
	@$(SCRIPTS_DIR)/configure-shell-helper.sh $(REPO_PATH)

.PHONY: uninstall-monitor
uninstall-monitor:
	@echo "$(PROMPT_PREFIX) Uninstalling ClaudeQ Monitor..."
	@if [ -d "/Applications/ClaudeQ Monitor.app" ]; then \
		sudo rm -rf "/Applications/ClaudeQ Monitor.app"; \
		echo "$(GREEN)✓ Removed ClaudeQ Monitor.app from /Applications$(NC)"; \
	else \
		echo "  ClaudeQ Monitor.app not found in /Applications"; \
	fi
	@rm -rf build .dist
	@echo "$(GREEN)✓ Monitor uninstalled successfully!$(NC)"

.PHONY: uninstall
uninstall:
	@echo "$(PROMPT_PREFIX) Uninstalling ClaudeQ..."
	@chmod +x $(SCRIPTS_DIR)/uninstall-helper.sh
	@$(SCRIPTS_DIR)/uninstall-helper.sh $(REPO_PATH)
	@echo "$(PROMPT_PREFIX) Removing Poetry virtual environment..."
	@poetry env remove --all 2>/dev/null || true
	@echo "$(GREEN)✓ Removed Poetry venv$(NC)"
	@echo "$(PROMPT_PREFIX) Cleaning up data and cache directories..."
	@HAD_SLACK=0; [ -f ".storage/slack/config.json" ] && HAD_SLACK=1; \
	rm -rf .storage; \
	rm -rf .pytest_cache .coverage coverage.xml .ruff_cache .mypy_cache; \
	rm -rf build .dist; \
	echo "$(GREEN)✓ Cleaned up all data and cache directories$(NC)"; \
	if [ "$$HAD_SLACK" = "1" ]; then \
		echo ""; \
		echo "$(YELLOW)⚠ Slack app still exists on Slack's side$(NC)"; \
		echo "  To remove: visit https://api.slack.com/apps and delete the ClaudeQ app"; \
	fi
	@echo "$(PROMPT_PREFIX) Removing ClaudeQ Monitor.app from /Applications..."
	@if [ -d "/Applications/ClaudeQ Monitor.app" ]; then \
		sudo rm -rf "/Applications/ClaudeQ Monitor.app"; \
		echo "$(GREEN)✓ Removed ClaudeQ Monitor.app$(NC)"; \
	else \
		echo "  ClaudeQ Monitor.app not found in /Applications"; \
	fi
	@echo "$(PROMPT_PREFIX) Removing VS Code configuration..."
	@CODE_SYMLINK="/usr/local/bin/code"; \
	if [ -L "$$CODE_SYMLINK" ] && [ "$$(readlink "$$CODE_SYMLINK")" = "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" ]; then \
		sudo rm -f "$$CODE_SYMLINK" 2>/dev/null && \
		echo "$(GREEN)✓ Removed VS Code CLI symlink$(NC)" || \
		echo "$(YELLOW)⚠ Could not remove code symlink (may need sudo)$(NC)"; \
	fi; \
	VSCODE_SETTINGS="$$HOME/Library/Application Support/Code/User/settings.json"; \
	if [ -f "$$VSCODE_SETTINGS" ] && grep -q "terminal.integrated.tabs.title" "$$VSCODE_SETTINGS"; then \
		echo "$(YELLOW)⚠ VS Code settings.json still contains ClaudeQ setting$(NC)"; \
		echo "  To remove: Open VS Code settings.json and delete 'terminal.integrated.tabs.title' line"; \
		echo "  (Backup files: $$VSCODE_SETTINGS.backup-*)"; \
	fi
	@echo ""
	@echo "$(GREEN)✓ ClaudeQ fully uninstalled!$(NC)"
	@echo "Project is now in clean state (like just cloned)"
