# Add a New Client Command

When adding a new client command to ClaudeQ, update ALL of these locations to keep them in sync.

## Files to Update

### 1. Command Handler — `src/claudeq/client/client.py`

**`_process_command()` method** — Add the command logic:

```python
# !mycmd / !mycommand
if line_lower in ['!mycmd', '!mycommand']:
    # Handle bare command (no args) — show usage hint
    print("Usage: !mycmd <arg>  (e.g., !mycmd something)\n")
    return True
if line_lower.startswith('!mycmd ') or line_lower.startswith('!mycommand '):
    # Handle command with arguments
    ...
    return True
```

**`_print_commands_help()` method** — Add to the commands list:

```python
commands = [
    ...
    ("\U0001FXXX", "!mycmd <arg> or !mycommand <arg>", "Description here"),
    ...
]
```

**Emoji rules:**
- ALL emojis MUST be 2 display columns wide
- NEVER use single-width emojis (e.g. `⚡` U+26A1, `🗑` U+1F5D1, `ℹ` U+2139)
- Test in both JetBrains terminal AND iTerm2/Terminal.app
- If alignment breaks, the emoji is single-width — pick a different one

### 2. CLAUDE.md — `CLAUDE.md`

Update the Client Commands table:

```markdown
| `!mycmd <arg>` or `!mycommand <arg>` | Description here |
```

### 3. README.md — `README.md`

Update the Client Commands table (includes emoji):

```markdown
| 🔮 `!mycmd <arg>` or `!mycommand <arg>` | Description here |
```

### 4. Shell Help — `src/scripts/claudeq-main.sh`

Update the CLIENT COMMANDS section:

```
    !mycmd <arg>        Description here
```

## Checklist

- [ ] `client.py` `_process_command()` — command handler with bare-command usage hint
- [ ] `client.py` `_print_commands_help()` — help list entry with 2-col-wide emoji
- [ ] `CLAUDE.md` — command table
- [ ] `README.md` — command table with emoji
- [ ] `src/scripts/claudeq-main.sh` — help text
- [ ] Test alignment in JetBrains terminal
- [ ] Test alignment in iTerm2 / Terminal.app
- [ ] Test `!h` command shows the new entry correctly
