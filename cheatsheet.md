# aicx cheatsheet

## Install

```bash
pip install aicx
aicx doctor
```

## Add accounts

```bash
# Browser OAuth is the default; each profile gets an independent session
aicx login codex personal
aicx login codex work

# Optional device-code login for headless or remote machines
aicx login codex personal --device-auth

# Import safe settings/history from ~/.codex, then start a fresh browser login
aicx adopt codex personal

# Claude adoption retains its existing behavior
claude auth login
aicx adopt claude personal
aicx login claude work --sso
```

Codex `adopt` never copies OAuth credentials. Do not run `codex logout` as part
of migration from an older adopted profile.

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

# Rename or delete one tool's profile
aicx rename claude test1 personal
aicx forget claude personal        # add --yes to skip the prompt
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

# Choose columns (remembered next time); reset to show all
aicx balance --columns tool,profile,window,usage,reset-in
aicx balance --reset-columns

# Machine-readable output
aicx accounts --json
aicx balance --json
```

`--continuous` is an alias for `--watch`. Press `Ctrl+C` to stop watch mode.
`--json` always prints every field regardless of `--columns`.

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

## Shell helpers

```bash
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

If Codex reports `HTTP 401`, `token_revoked`, or an invalidated OAuth token:

```bash
aicx doctor
aicx accounts codex
aicx login codex PROFILE --force
```

Repeat the safe forced login for every affected Codex profile. It removes only
the selected profile's local CLI auth and never invokes `codex logout`.

- Data: `~/.local/share/aicx`
- Custom data location: `AICX_HOME=/path/to/data`
- Disable color: `NO_COLOR=1`
