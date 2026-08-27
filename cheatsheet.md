# aicx cheatsheet

## Install

```bash
pipx install .
aicx doctor
```

## Add accounts

```bash
# New logins
aicx login codex work --device-auth
aicx login codex personal --device-auth
aicx login claude work --sso
aicx login claude personal --sso

# Keep an existing login
aicx adopt codex personal
aicx adopt claude personal
```

## Launch and switch

```bash
aicx codex @work
aicx codex @personal
aicx claude @work
aicx claude @personal

# Use the last selected account
aicx codex
aicx claude

# Pass provider arguments through
aicx codex @work resume --last
aicx claude @personal --resume SESSION_ID

# Select without launching
aicx use codex work
aicx use personal
```

## Accounts and usage

```bash
aicx accounts
aicx accounts codex
aicx balance
aicx balance codex --profile work

# Refresh every 60 seconds
aicx balance --watch

# Custom refresh interval
aicx balance --watch --interval 15

# Machine-readable output
aicx accounts --json
aicx balance --json
```

`--continuous` is an alias for `--watch`. Press `Ctrl+C` to stop watch mode.

## History and sessions

```bash
aicx sync codex
aicx sync claude
aicx sessions codex
aicx sessions claude --all-profiles
aicx sessions codex --profile work --json
```

Conversation history is shared by default. Exit a session before resuming it
with another account.

```bash
# Optional isolation
export AICX_CODEX_HISTORY=isolated
export AICX_CLAUDE_HISTORY=isolated
```

## Stop processes

```bash
aicx close codex PID
aicx close claude all
aicx close codex SESSION_ID
aicx close codex all
```

## VS Code and shell helpers

```bash
aicx vscode work .
aicx vscode personal ~/src/project

# Add to .bashrc
eval "$(aicx shell-init bash)"

# Add to .zshrc
eval "$(aicx shell-init zsh)"
```

## Help and locations

```bash
aicx --help
aicx balance --help
aicx doctor
```

- Data: `~/.local/share/aicx`
- Custom data location: `AICX_HOME=/path/to/data`
- Disable color: `NO_COLOR=1`
