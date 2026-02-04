PACKAGE_NAME     := claudeq
PYTHON_VERSION   := "3.10"
REPO_PATH        := $(shell git rev-parse --show-toplevel)
PROMPT_PREFIX    := "\n>"
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
	@echo "  2. Run: cq <tag-name>"
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
	@echo "$(GREEN)✓ Monitor installed successfully!$(NC)"
	@echo ""
	@echo "You can now run: cq-mo"
	@echo ""

.PHONY: clean
clean:
	@echo "$(PROMPT_PREFIX) Cleaning up..."
	@poetry env remove --all
	@rm -rf .pytest_cache .coverage coverage.xml .ruff_cache .mypy_cache
	@rm -rf ~/.claude-queues ~/.claude-sockets

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
	@chmod +x $(SRC_DIR)/cq-mo-wrapper.sh
	@chmod +x $(SRC_DIR)/claudeq-monitor.py
	@$(MAKE) .detect-shell

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
	if grep -q "# ClaudeQ PTY" "$$RC_FILE" 2>/dev/null; then \
		echo "$(YELLOW)⚠ ClaudeQ configuration already exists in $$RC_FILE$(NC)"; \
		read -p "  Overwrite? (y/N) " -n 1 -r REPLY; \
		echo; \
		if [ "$$REPLY" = "y" ] || [ "$$REPLY" = "Y" ]; then \
			sed -i.bak '/# ClaudeQ PTY/,/^alias cq=/d' "$$RC_FILE"; \
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
	echo "# ClaudeQ - Scrollable in JetBrains IDEs! 🎯" >> "$$RC_FILE"; \
	echo "# Uses PTY (no tmux) with native scrolling" >> "$$RC_FILE"; \
	echo "# Server in JetBrains, client in any terminal" >> "$$RC_FILE"; \
	echo "# Usage: claudeq <tag> [message] or cq <tag> [message]" >> "$$RC_FILE"; \
	echo "# Note: Always uses Poetry venv from project directory" >> "$$RC_FILE"; \
	echo "export CLAUDEQ_PROJECT_DIR=\"$(REPO_PATH)\"" >> "$$RC_FILE"; \
	echo "export CLAUDEQ_PYTHON=\"$$POETRY_VENV/bin/python3\"" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "# Add JetBrains IDE CLI tools to PATH for monitor support" >> "$$RC_FILE"; \
	JETBRAINS_PATHS=""; \
	for app in "IntelliJ IDEA" "PyCharm" "WebStorm" "PhpStorm" "GoLand" "RubyMine" "CLion" "DataGrip"; do \
		if [ -d "/Applications/$$app.app/Contents/MacOS" ]; then \
			JETBRAINS_PATHS="$$JETBRAINS_PATHS:/Applications/$$app.app/Contents/MacOS"; \
		fi; \
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
	echo "    \"\$$CLAUDEQ_PROJECT_DIR/src/claudeq-main.sh\" \"\$$@\"" >> "$$RC_FILE"; \
	echo "}" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "cq-mo() {" >> "$$RC_FILE"; \
	echo "    \"\$$CLAUDEQ_PROJECT_DIR/src/cq-mo-wrapper.sh\"" >> "$$RC_FILE"; \
	echo "}" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "cq-cleanup() {" >> "$$RC_FILE"; \
	echo "    \"\$$CLAUDEQ_PROJECT_DIR/src/claudeq-cleanup.sh\"" >> "$$RC_FILE"; \
	echo "}" >> "$$RC_FILE"; \
	echo "" >> "$$RC_FILE"; \
	echo "alias cq='claudeq'" >> "$$RC_FILE"; \
	echo "$(GREEN)✓ Added ClaudeQ configuration to $$RC_FILE$(NC)"; \
	echo "  Using Poetry venv: $$POETRY_VENV"

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
		sed -i.bak '/# ClaudeQ PTY/,/^alias cq=/d' "$$RC_FILE"; \
		echo "$(GREEN)✓ Removed ClaudeQ configuration from $$RC_FILE$(NC)"; \
	fi
	@rm -rf ~/.claude-queues ~/.claude-sockets
	@echo "$(GREEN)✓ Cleaned up data directories$(NC)"
	@echo ""
	@echo "To remove the Poetry venv, run: make clean"
