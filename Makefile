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

.PHONY: default
default: install

.PHONY: install
install: .env install-core ensure-storage write-install-metadata configure-shell
	@echo "$(GREEN)✓ ClaudeQ installed successfully!$(NC)"
	@echo ""
	@echo "To start using ClaudeQ:"
	@echo "  1. Reload your shell: source ~/.zshrc  (or ~/.bashrc)"
	@echo "  2. Run: cq <tag-name>"
	@echo ""
	@echo "Note: The venv is automatically used by claudeq commands."
	@echo ""
	@echo "Optional: To install the monitor GUI, run:"
	@echo "  make install-monitor"
	@echo ""

.PHONY: install-core
install-core:
	@echo "$(PROMPT_PREFIX) Installing core dependencies..."
	@poetry install --no-root --without monitor

.PHONY: ensure-storage
ensure-storage:
	@mkdir -p "$(REPO_PATH)/.storage"

.PHONY: write-install-metadata
write-install-metadata: ensure-storage
	@echo "$(PROMPT_PREFIX) Writing installation metadata to .storage/..."
	@poetry env info --path > "$(REPO_PATH)/.storage/venv-path"
	@echo "$(REPO_PATH)" > "$(REPO_PATH)/.storage/project-path"
	@echo "   Saved venv: $$(cat $(REPO_PATH)/.storage/venv-path)/bin/python3"
	@echo "   Saved project: $$(cat $(REPO_PATH)/.storage/project-path)"
	@# Keep legacy .venv-path for backward compatibility (can be removed later)
	@poetry env info --path > "$(REPO_PATH)/.venv-path"

.PHONY: install-monitor
install-monitor: .env ensure-storage write-install-metadata
	@echo "$(PROMPT_PREFIX) Installing monitor dependencies..."
	@poetry install --no-root --with monitor
	@echo "$(PROMPT_PREFIX) Building ClaudeQ Monitor.app with py2app..."
	@cd $(REPO_PATH) && poetry run python setup.py py2app --dist-dir .dist > /dev/null 2>&1
	@echo "$(PROMPT_PREFIX) Installing ClaudeQ Monitor.app to /Applications..."
	@if [ -d "/Applications/ClaudeQ Monitor.app" ]; then \
		sudo rm -rf "/Applications/ClaudeQ Monitor.app"; \
	fi
	@sudo cp -R "$(REPO_PATH)/.dist/ClaudeQ Monitor.app" /Applications/
	@echo "$(GREEN)✓ Monitor installed successfully!$(NC)"
	@echo ""
	@echo "Launch ClaudeQ Monitor from:"
	@echo "  • Spotlight: Search 'ClaudeQ Monitor'"
	@echo "  • Applications: Double-click ClaudeQ Monitor.app"
	@echo "  • Dock: Pin it for quick access"
	@echo ""

.PHONY: run-monitor
run-monitor:
	@PYTHONPATH=$(SRC_DIR) poetry run python -c "from claudeq.monitor.app import main; main()"

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
update: .env
	@echo "$(PROMPT_PREFIX) Updating dependencies..."
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
	@chmod +x $(SCRIPTS_DIR)/claudeq-cleanup.sh
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
			EXT_INSTALLED=$$($$CODE_PATH --list-extensions 2>/dev/null | grep -q "claudeq.claudeq-terminal-selector" && echo "yes" || echo "no"); \
			if [ "$$EXT_INSTALLED" = "no" ]; then \
				if [ -n "$$NPM_PATH" ]; then \
					cd "$(REPO_PATH)/src/claudeq/vscode-extension" && \
					npx --yes @vscode/vsce package --out claudeq-terminal-selector.vsix >/dev/null 2>&1 && \
					$$CODE_PATH --install-extension claudeq-terminal-selector.vsix --force >/dev/null 2>&1 && \
					echo "$(GREEN)  ✓ ClaudeQ extension installed$(NC)" || \
					echo "$(YELLOW)  ⚠ Could not install extension$(NC)"; \
				else \
					echo "$(YELLOW)  ⚠ npm not found, skipping extension install$(NC)"; \
				fi; \
			else \
				echo "  ✓ ClaudeQ extension already installed"; \
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

.PHONY: .detect-shell
.detect-shell:
	@SHELL_NAME=$$(basename $$SHELL); \
	if [ "$$SHELL_NAME" = "zsh" ]; then \
		RC_FILE="$$HOME/.zshrc"; \
	elif [ "$$SHELL_NAME" = "bash" ]; then \
		RC_FILE="$$HOME/.bashrc"; \
	else \
		echo "$(YELLOW)⚠ Unknown shell: $$SHELL_NAME$(NC)"; \
		echo "  Please manually add configuration to your shell RC file."; \
		exit 0; \
	fi; \
	if grep -q "# ClaudeQ" "$$RC_FILE" 2>/dev/null; then \
		echo "$(YELLOW)⚠ ClaudeQ configuration already exists in $$RC_FILE$(NC)"; \
		read -p "  Overwrite? (y/N) " -n 1 -r REPLY; \
		echo; \
		if [ "$$REPLY" = "y" ] || [ "$$REPLY" = "Y" ]; then \
			sed -i.bak '/# ClaudeQ/,/^alias cq=/d' "$$RC_FILE"; \
			echo "$(GREEN)✓ Removed old configuration$(NC)"; \
		else \
			echo "  Skipping shell configuration."; \
			exit 0; \
		fi; \
	fi; \
	if [ -f "$$RC_FILE" ]; then \
		cp "$$RC_FILE" "$$RC_FILE.backup-$$(date +%Y%m%d-%H%M%S)"; \
		echo "$(GREEN)✓ Backed up $$RC_FILE$(NC)"; \
	else \
		echo "$(GREEN)✓ Creating new $$RC_FILE$(NC)"; \
	fi; \
	POETRY_VENV=$$(cd $(REPO_PATH) && poetry env info --path); \
	echo "" >> "$$RC_FILE"; \
	echo "# ===== ClaudeQ Configuration START - DO NOT REMOVE (needed for uninstall) =====" >> "$$RC_FILE"; \
	echo "# ClaudeQ - Scrollable in JetBrains IDEs! 🎯" >> "$$RC_FILE"; \
	echo "# Uses PTY (no tmux) with native scrolling" >> "$$RC_FILE"; \
	echo "# Server in JetBrains, client in any terminal" >> "$$RC_FILE"; \
	echo "#" >> "$$RC_FILE"; \
	echo "# Usage: claudeq <tag> [message] (or: cq)" >> "$$RC_FILE"; \
	echo "#        claudeq-cleanup (or: cqc)" >> "$$RC_FILE"; \
	echo "#" >> "$$RC_FILE"; \
	echo "# You can modify the content below, but keep the START/END marker lines" >> "$$RC_FILE"; \
	echo "# for proper uninstallation." >> "$$RC_FILE"; \
	echo "export CLAUDEQ_PROJECT_DIR=\"$(REPO_PATH)\"" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "# Add JetBrains IDE CLI tools to PATH for monitor support" >> "$$RC_FILE"; \
	JETBRAINS_PATHS=""; \
	for pattern in IntelliJ PyCharm WebStorm PhpStorm GoLand RubyMine CLion DataGrip Rider Fleet; do \
		for app in /Applications/$$pattern*.app; do \
			if [ -d "$$app/Contents/MacOS" ]; then \
				JETBRAINS_PATHS="$$JETBRAINS_PATHS:$$app/Contents/MacOS"; \
			fi; \
		done; \
	done; \
	if [ -n "$$JETBRAINS_PATHS" ]; then \
		echo "export PATH=\"\$$PATH$$JETBRAINS_PATHS\"" >> "$$RC_FILE"; \
	fi; \
	echo "" >> "$$RC_FILE"; \
	echo "claudeq() {" >> "$$RC_FILE"; \
	echo "    if [ \$$# -eq 0 ]; then" >> "$$RC_FILE"; \
	echo "        echo \"Error: Tag is required\"" >> "$$RC_FILE"; \
	echo "        echo \"Usage: claudeq <tag> [message]\"" >> "$$RC_FILE"; \
	echo "        echo \"Example (server): claudeq my-feature\"" >> "$$RC_FILE"; \
	echo "        echo \"Example (client): claudeq my-feature 'hello Claude'\"" >> "$$RC_FILE"; \
	echo "        return 1" >> "$$RC_FILE"; \
	echo "    fi" >> "$$RC_FILE"; \
	echo "    # Flags (starting with --) can be passed and will be used by server only" >> "$$RC_FILE"; \
	echo "    # Example: claudeq my-tag --dangerously-skip-permissions" >> "$$RC_FILE"; \
	echo "    \"\$$CLAUDEQ_PROJECT_DIR/src/scripts/claudeq-main.sh\" \"\$$@\"" >> "$$RC_FILE"; \
	echo "}" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "claudeq-cleanup() {" >> "$$RC_FILE"; \
	echo "    \"\$$CLAUDEQ_PROJECT_DIR/src/scripts/claudeq-cleanup.sh\"" >> "$$RC_FILE"; \
	echo "}" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "alias cq='claudeq'" >> "$$RC_FILE"; \
	echo "alias cqc='claudeq-cleanup'" >> "$$RC_FILE"; \
	echo "# ===== ClaudeQ Configuration END - DO NOT REMOVE (needed for uninstall) =====" >> "$$RC_FILE"; \
	echo "$(GREEN)✓ Added ClaudeQ configuration to $$RC_FILE$(NC)"; \
	echo "  Using Poetry venv: $$POETRY_VENV"

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
	@SHELL_NAME=$$(basename $$SHELL); \
	if [ "$$SHELL_NAME" = "zsh" ]; then \
		RC_FILE="$$HOME/.zshrc"; \
	elif [ "$$SHELL_NAME" = "bash" ]; then \
		RC_FILE="$$HOME/.bashrc"; \
	fi; \
	if [ -f "$$RC_FILE" ]; then \
		if grep -q "ClaudeQ Configuration START" "$$RC_FILE"; then \
			cp "$$RC_FILE" "$$RC_FILE.backup-uninstall-$$(date +%Y%m%d-%H%M%S)"; \
			sed -i.bak '/ClaudeQ Configuration START/,/ClaudeQ Configuration END/d' "$$RC_FILE"; \
			rm -f "$$RC_FILE.bak"; \
			echo "$(GREEN)✓ Removed ClaudeQ configuration from $$RC_FILE$(NC)"; \
			echo "  Backup created at $$RC_FILE.backup-uninstall-$$(date +%Y%m%d-%H%M%S)"; \
		elif grep -q "# ClaudeQ" "$$RC_FILE"; then \
			echo "$(YELLOW)⚠ Found legacy ClaudeQ installation$(NC)"; \
			cp "$$RC_FILE" "$$RC_FILE.backup-uninstall-$$(date +%Y%m%d-%H%M%S)"; \
			sed -i.bak '/# ClaudeQ/,/# End ClaudeQ/d' "$$RC_FILE"; \
			sed -i.bak '/# ClaudeQ/,/^alias cq/d' "$$RC_FILE"; \
			rm -f "$$RC_FILE.bak"; \
			echo "$(GREEN)✓ Removed ClaudeQ configuration from $$RC_FILE$(NC)"; \
			echo "  Backup created at $$RC_FILE.backup-uninstall-$$(date +%Y%m%d-%H%M%S)"; \
		else \
			echo "  No ClaudeQ configuration found in $$RC_FILE"; \
		fi; \
	fi
	@echo "$(PROMPT_PREFIX) Removing Poetry virtual environment..."
	@poetry env remove --all 2>/dev/null || true
	@echo "$(GREEN)✓ Removed Poetry venv$(NC)"
	@echo "$(PROMPT_PREFIX) Cleaning up data and cache directories..."
	@rm -rf .storage
	@rm -rf .pytest_cache .coverage coverage.xml .ruff_cache .mypy_cache
	@rm -rf build .dist
	@rm -f "$(REPO_PATH)/.venv-path"
	@echo "$(GREEN)✓ Cleaned up all data and cache directories$(NC)"
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
