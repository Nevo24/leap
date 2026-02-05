PACKAGE_NAME     := claudeq
PYTHON_VERSION   := "3.10"
REPO_PATH        := $(shell git rev-parse --show-toplevel)
PROMPT_PREFIX    := "\n→"
SRC_DIR          := $(REPO_PATH)/src

# Colors for output
GREEN  := \033[0;32m
YELLOW := \033[1;33m
NC     := \033[0m

.PHONY: default
default: install

.PHONY: install
install: .env install-core configure-shell
	@echo "$(GREEN)✓ ClaudeQ installed successfully!$(NC)"
	@echo ""
	@echo "To start using ClaudeQ:"
	@echo "  1. Reload your shell: source ~/.zshrc  (or ~/.bashrc)"
	@echo "  2. Activate the venv: cqa  (or: claudeq-activate)"
	@echo "  3. Run: cq <tag-name>"
	@echo ""
	@echo "Note: The venv is automatically used by claudeq commands,"
	@echo "      but you can manually activate it for other purposes."
	@echo ""
	@echo "Optional: To install the monitor GUI, run:"
	@echo "  make install-monitor"
	@echo ""

.PHONY: install-core
install-core:
	@echo "$(PROMPT_PREFIX) Installing core dependencies..."
	@poetry install --no-root --without monitor

.PHONY: install-monitor
install-monitor: .env
	@echo "$(PROMPT_PREFIX) Installing monitor dependencies..."
	@poetry install --no-root --with monitor
	@echo "$(PROMPT_PREFIX) Building ClaudeQ Monitor.app with py2app..."
	@cd $(REPO_PATH) && poetry run python setup.py py2app > /dev/null 2>&1
	@echo "$(PROMPT_PREFIX) Installing ClaudeQ Monitor.app to /Applications..."
	@if [ -d "/Applications/ClaudeQ Monitor.app" ]; then \
		sudo rm -rf "/Applications/ClaudeQ Monitor.app"; \
	fi
	@sudo cp -R "$(REPO_PATH)/dist/ClaudeQ Monitor.app" /Applications/
	@echo "$(GREEN)✓ Monitor installed successfully!$(NC)"
	@echo ""
	@echo "Launch ClaudeQ Monitor from:"
	@echo "  • Spotlight: Search 'ClaudeQ Monitor'"
	@echo "  • Applications: Double-click ClaudeQ Monitor.app"
	@echo "  • Dock: Pin it for quick access"
	@echo ""
	@echo "$(YELLOW)Note: Custom icon in Dock works perfectly!$(NC)"
	@echo ""

.PHONY: clean
clean:
	@echo "$(PROMPT_PREFIX) Cleaning up..."
	@poetry env remove --all
	@rm -rf .pytest_cache .coverage coverage.xml .ruff_cache .mypy_cache
	@rm -rf ~/.claude-queues ~/.claude-sockets
	@rm -rf build dist
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
	@chmod +x $(SRC_DIR)/claudeq-main.sh
	@chmod +x $(SRC_DIR)/claudeq-server.py
	@chmod +x $(SRC_DIR)/claudeq-client.py
	@chmod +x $(SRC_DIR)/claudeq-cleanup.sh
	@chmod +x $(SRC_DIR)/claudeq-monitor.py
	@$(MAKE) .configure-vscode
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
			if ! grep -q "terminal.integrated.tabs.title" "$$VSCODE_SETTINGS"; then \
				echo "  Updating VS Code settings for terminal titles..."; \
				cp "$$VSCODE_SETTINGS" "$$VSCODE_SETTINGS.backup-$$(date +%Y%m%d-%H%M%S)"; \
				python3 -c "import json, sys; \
					data = json.load(open('$$VSCODE_SETTINGS')); \
					data['terminal.integrated.tabs.title'] = '\$${sequence}'; \
					json.dump(data, open('$$VSCODE_SETTINGS', 'w'), indent=4)" 2>/dev/null && \
				echo "$(GREEN)  ✓ VS Code settings updated (backup created)$(NC)" || \
				echo "$(YELLOW)  ⚠ Could not update VS Code settings$(NC)"; \
			else \
				echo "  ✓ VS Code terminal title setting already configured"; \
			fi; \
		elif [ -d "$$HOME/Library/Application Support/Code/User" ]; then \
			echo "  Creating VS Code settings.json..."; \
			echo '{\n    "terminal.integrated.tabs.title": "$${sequence}"\n}' > "$$VSCODE_SETTINGS" && \
			echo "$(GREEN)  ✓ VS Code settings.json created$(NC)" || \
			echo "$(YELLOW)  ⚠ Could not create settings.json$(NC)"; \
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
	echo "#        claudeq-activate (or: cqa) - activate the venv" >> "$$RC_FILE"; \
	echo "#" >> "$$RC_FILE"; \
	echo "# You can modify the content below, but keep the START/END marker lines" >> "$$RC_FILE"; \
	echo "# for proper uninstallation." >> "$$RC_FILE"; \
	echo "export CLAUDEQ_PROJECT_DIR=\"$(REPO_PATH)\"" >> "$$RC_FILE"; \
	echo "export CLAUDEQ_PYTHON=\"$$POETRY_VENV/bin/python3\"" >> "$$RC_FILE"; \
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
	echo "    \"\$$CLAUDEQ_PROJECT_DIR/src/claudeq-main.sh\" \"\$$@\"" >> "$$RC_FILE"; \
	echo "}" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "claudeq-cleanup() {" >> "$$RC_FILE"; \
	echo "    \"\$$CLAUDEQ_PROJECT_DIR/src/claudeq-cleanup.sh\"" >> "$$RC_FILE"; \
	echo "}" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "claudeq-activate() {" >> "$$RC_FILE"; \
	echo "    VENV_PATH=\$$(cd \"\$$CLAUDEQ_PROJECT_DIR\" && poetry env info --path 2>/dev/null)" >> "$$RC_FILE"; \
	echo "    if [ -n \"\$$VENV_PATH\" ] && [ -f \"\$$VENV_PATH/bin/activate\" ]; then" >> "$$RC_FILE"; \
	echo "        source \"\$$VENV_PATH/bin/activate\"" >> "$$RC_FILE"; \
	echo "        echo \"✓ Activated ClaudeQ venv: \$$VENV_PATH\"" >> "$$RC_FILE"; \
	echo "    else" >> "$$RC_FILE"; \
	echo "        echo \"Error: ClaudeQ venv not found. Run 'cd \$$CLAUDEQ_PROJECT_DIR && make install'\"" >> "$$RC_FILE"; \
	echo "        return 1" >> "$$RC_FILE"; \
	echo "    fi" >> "$$RC_FILE"; \
	echo "}" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "alias cq='claudeq'" >> "$$RC_FILE"; \
	echo "alias cqc='claudeq-cleanup'" >> "$$RC_FILE"; \
	echo "alias cqa='claudeq-activate'" >> "$$RC_FILE"; \
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
	@rm -rf build dist
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
	@rm -rf ~/.claude-queues ~/.claude-sockets
	@rm -rf .pytest_cache .coverage coverage.xml .ruff_cache .mypy_cache
	@rm -rf build dist
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
