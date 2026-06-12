# Leap

**A queueing system and dashboard for managing multiple AI CLI sessions + Cursor Editor Agent Sessions**

Run AI coding agents (Claude Code, Codex CLI, GitHub Copilot, Cursor Agent, Gemini CLI) in any terminal (JetBrains, VS Code, Cursor, iTerm2, cmux, WezTerm, Arduino IDE, and more). Queue messages while the agent is busy, track all sessions from a single monitor, and jump straight to the right terminal with one click.

**NEW!** Manage your Cursor Editor Agent sessions right in the monitor too - live status, PR tracking, and one-click jump to the exact tab.

## Key Features

- **Smart message queueing** - Auto-sends when the CLI is ready
- **Real-time GUI monitoring** - See all sessions, jump across IDEs and projects
- **Context usage tracking** - See how full each session's context window is (Claude, Codex, Copilot, Gemini), so you know how close it is to auto-compaction. Hover the Context cell on a Claude, Codex, or Gemini session to also see last-message and whole-session token counts plus an estimated API cost
- **PR tracking** - GitLab, GitHub & Bitbucket (Cloud and Server/Data Center) comment detection with `/leap` tag support
- **Slack integration** - Bidirectional messaging between Slack and Leap sessions
- **Prevent sleep while busy** - Mac stays awake until every session is idle (optional lid-close override)

## Installation

**Platform:** macOS (full support). Linux works for core queueing and Slack, but the Monitor GUI is macOS only.

**Prerequisites:** Python 3.11+, and one or more AI CLIs: [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), [GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/copilot-cli), [Cursor Agent](https://cursor.com/docs/cli/overview), [Gemini CLI](https://github.com/google-gemini/gemini-cli)

```bash
git clone https://github.com/nevo24/leap.git
cd leap
make install
source ~/.zshrc  # ~/.bashrc on Linux
```

Already installed? Run `leap --update` to pull the latest version and rebuild. If the update command fails, `cd` into the project directory and run `make update`.

Installed a new CLI / IDE / terminal **after** Leap? Run `leap --reconfigure` so Leap wires its hooks and IDE/terminal settings into the newly-installed tool. (`make install` skips anything that wasn't on disk at the time, so newly-installed tools start without integration.)

### Upgrading from ClaudeQ

The project was renamed from **ClaudeQ** (`claudeq`) to **Leap** (`leap`). If you have an existing ClaudeQ installation:

```bash
cd <path-to-your-claudeq-repo>
git pull
cd ..
mv claudeq leap
cd leap
make install    # runs migration + installs new 'leap' command
source ~/.zshrc  # ~/.bashrc on Linux
```

This migrates your storage, hooks, shell config, and monitor app automatically. The old `cq` / `claudeq` commands are replaced by `leap`.

## Usage

Just run `leap <tag>` - that's it! Leap wraps your AI CLI with queueing and session tracking.

```bash
leap my-feature         # First run starts a server
leap my-feature         # Second run connects a client (queue messages here)
^^hello world           # Type ^^ (quickly) in the server tab to queue directly
^^                      # Inside ^^: save msg to history (↑↓ to browse)
^^!!                    # Inside ^^: force-send next queued msg (Enter to confirm)
leap --resume           # Pick a past Leap tag; for Claude, resumes in your current cwd
                        # (transcript is relocated automatically - no `cd` needed)
leap --headroom         # (optional) route chosen CLIs through Headroom to compress context + cut tokens
```

The **Monitor** is a native macOS app installed alongside Leap. Just open it from your Applications folder or Spotlight to see all your sessions at a glance:

![Leap Monitor](assets/leap-monitor.png)

## License

MIT License - see [LICENSE](LICENSE)

---

**Links:** [GitHub](https://github.com/nevo24/leap) • [Claude Code](https://docs.anthropic.com/en/docs/claude-code) • [Codex CLI](https://github.com/openai/codex) • [GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/copilot-cli) • [Cursor Agent](https://cursor.com/docs/cli/overview) • [Gemini CLI](https://github.com/google-gemini/gemini-cli)
