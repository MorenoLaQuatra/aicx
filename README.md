# aicx: Multiple accounts for Codex and Claude Code

`aicx` is a small Linux CLI for keeping several Codex and Claude Code accounts
on one machine. Each account has its own credentials and settings, while saved
conversations are shared between accounts by default.

Use it when you want to switch between work and personal subscriptions without
logging in again or losing access to previous conversations.

## Install

Requirements:

- Linux
- Python 3.11 or newer
- Codex, Claude Code, or both already installed

From a cloned checkout:

```bash
pipx install .
aicx doctor
```

Without `pipx`:

```bash
python3 -m pip install --user .
aicx doctor
```

## Set up accounts

Create a named profile and log in once:

```bash
aicx login codex work --device-auth
aicx login codex personal --device-auth

aicx login claude work --sso
aicx login claude personal --sso
```

If you already use Codex or Claude Code, adopt that login instead. This copies
the existing provider home into a profile and leaves the original untouched.

```bash
aicx adopt codex personal
aicx adopt claude personal
```

Use `--from PATH` when the existing provider home is in a custom location:

```bash
aicx adopt codex work --from ~/.codex-work
```

Adoption never overwrites an existing profile.

## Daily use

Put `@profile` immediately after the provider name:

```bash
aicx codex @work
aicx codex @personal
aicx claude @work
```

The selection is remembered, so the shorter form uses the last account:

```bash
aicx codex
aicx claude
```

Provider arguments pass through unchanged:

```bash
aicx codex @work resume --last
aicx claude @personal --resume SESSION_ID
```

`aicx` synchronizes saved conversations before launch and after exit. Start a
conversation with one account, exit normally, then resume it with another:

```bash
aicx codex @personal
aicx codex @work resume --last
```

Do not open the same saved session with two accounts at the same time. Sequential
handoff is supported, concurrent writing to one transcript is not.

## Accounts and balance

```bash
aicx accounts
aicx accounts codex
aicx balance
aicx balance codex --profile work
```

`aicx accounts` shows the account email when the provider exposes it.

`aicx balance` shows usage, remaining percentage, the local reset time, and a
human-readable countdown. Keep it open like `watch` with:

```bash
aicx balance --watch
aicx balance --watch --interval 15
aicx balance codex --watch --interval 30
```

`--continuous` is an alias for `--watch`. The default refresh interval is 60
seconds. Press `Ctrl+C` to stop.

Codex usage is read live from its local app server. Claude usage is captured
from Claude Code's status-line payload and appears after the first response in
each profile. `aicx` installs its collector during Claude login or adoption when
no other status line is configured. It never replaces an existing status line.

For scripts, use JSON output without watch mode:

```bash
aicx accounts --json
aicx balance --json
aicx sessions codex --json
```

## Shared conversation history

History sharing is enabled separately for Codex and Claude Code.

- Codex shares saved and archived JSONL sessions.
- Claude shares project transcripts, spilled session tool results, project
  memory, and file checkpoints needed for rewind.
- Credentials, provider settings, logs, plugins, caches, and runtimes stay inside
  each account profile.
- Active profiles are not copied while an `aicx` managed process is writing.
  They synchronize after that process exits.

Inspect or manually synchronize history with:

```bash
aicx sync codex
aicx sync claude
aicx sessions codex
aicx sessions claude --all-profiles
```

To keep conversations isolated, set either variable before running `aicx`:

```bash
export AICX_CODEX_HISTORY=isolated
export AICX_CLAUDE_HISTORY=isolated
```

## Other useful commands

Select an account without launching it:

```bash
aicx use codex work
aicx use personal
```

Stop a process launched by `aicx`:

```bash
aicx close codex PID
aicx close claude all
```

Open an isolated VS Code window for a profile:

```bash
aicx vscode work .
```

Make bare `codex` and `claude` commands route through `aicx` in Bash:

```bash
eval "$(aicx shell-init bash)"
```

Use `zsh` instead of `bash` for Zsh. See [cheatsheet.md](cheatsheet.md) for the
compact command list.

## Storage and security

Data is stored under `~/.local/share/aicx` by default:

```text
~/.local/share/aicx/
├── profiles/            # private credentials and settings per account
├── shared/              # synchronized conversation data
├── run/                 # tracked process records
├── state.json           # selected profile names
└── vscode/              # isolated VS Code user data
```

Set `AICX_HOME` to use another location. Profile directories contain credentials
and conversation text. Do not commit or share them. `aicx` delegates login to the
official CLIs and does not print or exchange provider tokens.

Environment API keys can override stored subscription logins. `aicx doctor`
warns when `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is set.

## Development and current limits

Run the checks with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

Version 0.3 supports Linux. macOS support is planned. VS Code integration is a
launcher for separate windows, not an account picker inside an existing window.
See [docs/architecture.md](docs/architecture.md) for implementation details.
