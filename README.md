# ClaudeQ

**A queueing system and dashboard for managing multiple Claude CLI sessions.**

Run Claude Code in any terminal (JetBrains, VS Code, iTerm2, and more). Queue messages while Claude is busy, track all sessions from a single monitor, and jump straight to the right terminal with one click.

## Key Features

- **Smart message queueing** — Auto-sends when Claude is ready
- **Real-time GUI monitoring** — See all sessions, jump across IDEs and projects
- **PR tracking** — GitLab & GitHub thread detection with `/cq` command support
- **Slack integration** — Bidirectional messaging between Slack and CQ sessions

## Installation

**Platform:** macOS (full support). Linux works for core queueing and Slack, but the Monitor GUI is macOS only.

**Prerequisites:** Python 3.11+, [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)

```bash
git clone https://github.com/nevo24/claudeq.git
cd claudeq
make install
source ~/.zshrc  # or ~/.bashrc
```

Already installed? Run `claudeq --update` to pull the latest version and rebuild.

## Usage

Just run `cq <tag>` instead of `claude` — that's it! ClaudeQ wraps Claude Code with queueing and session tracking.

```bash
cq my-feature      # First run starts the server (Claude runs here)
cq my-feature      # Second run connects a client (queue messages here)
```

The **Monitor** is a native macOS app installed alongside ClaudeQ. Just open it from your Applications folder or Spotlight to see all your sessions at a glance:

![ClaudeQ Monitor](assets/claudeq-monitor.png)

## License

MIT License - see [LICENSE](LICENSE)

---

**Links:** [GitHub](https://github.com/nevo24/claudeq) • [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
