<h1 align="center">aicx</h1>
<p align="center"><i>Switch between Codex and Claude Code accounts without losing local conversation history.</i></p>

<div align="center">
  <a href="https://pypi.org/project/aicx/"><img src="https://img.shields.io/pypi/v/aicx" alt="PyPI version"></a>
  <a href="https://pypi.org/project/aicx/"><img src="https://img.shields.io/pypi/pyversions/aicx" alt="Python versions"></a>
  <a href="https://github.com/MorenoLaQuatra/aicx/stargazers"><img src="https://img.shields.io/github/stars/MorenoLaQuatra/aicx" alt="GitHub stars"></a>
  <a href="https://github.com/MorenoLaQuatra/aicx/blob/main/LICENSE"><img src="https://img.shields.io/github/license/MorenoLaQuatra/aicx" alt="License"></a>
</div>

<br>

`aicx` keeps credentials and settings separate for every account. Saved
conversations are shared by default, so you can start with one account and
continue with another.

## Install

You need Linux or macOS, Python 3.9 or newer, and the official Codex or Claude
Code CLI.

```bash
pip install aicx
aicx doctor
```

## Set up two accounts

### Codex example

```bash
aicx login codex personal
aicx login codex work
```

Each command launches the official Codex login inside that profile's private
`CODEX_HOME`, so every profile receives an independently issued OAuth session.
Standard browser OAuth is the default. Device-code login is optional and is
mainly useful on headless or remote machines:

```bash
aicx login codex personal --device-auth
```

If you already have Codex settings and conversations under `~/.codex`, import
the safe local state with:

```bash
aicx adopt codex personal
```

For Codex, `adopt` intentionally excludes CLI and MCP OAuth credential files,
fixes profile-local configuration and session paths, then starts a fresh browser
login for the new profile. It does not change the source directory. Codex OAuth
credentials are never copied between profiles.

### Claude Code example

```bash
# First account
claude auth login
aicx adopt claude personal
claude auth logout

# Second account
claude auth login
aicx adopt claude work
```

Profile names are yours to choose. `personal`, `work`, and `client` are only
examples.

## Use an account

Launch the provider with `@account-name`:

```bash
aicx codex @personal
aicx codex @work
aicx claude @personal
```

The last selection is remembered:

```bash
aicx codex
aicx claude
```

Provider arguments pass through normally:

```bash
aicx codex @work resume --last
aicx claude @personal --resume SESSION_ID
```

## Rename or delete a profile

```bash
aicx rename claude test1 personal   # move one tool's profile to a new name
aicx forget claude personal         # delete one tool's profile (asks first; --yes skips)
```

Both act on a single tool. `forget` keeps the shared conversation history and
leaves the other tool in that profile untouched. Neither works while a tracked
process for that profile is still running.

## Accounts and usage

```bash
aicx accounts
aicx balance
aicx balance codex --profile work
```

Keep the balance view open and refresh it every 60 seconds:

```bash
aicx balance --watch
```

Choose another interval when needed:

```bash
aicx balance --watch --interval 15
```

The balance table shows usage, remaining percentage, reset time, and the time
left until reset.

Pick the columns you care about; the choice is saved and reused on the next run:

```bash
aicx balance --columns tool,profile,window,usage,reset-in
aicx balance                       # same columns as last time
aicx balance --reset-columns       # forget the choice, show every column
```

Column names: `tool`, `profile`, `active`, `account`, `window`, `usage`,
`left`, `reset-in`, `reset`, `source`.

## Conversation history

`aicx` synchronizes saved history before a provider starts and after it exits.
This lets you exit a conversation and resume it with another account.

Do not open the same conversation from two accounts at the same time. History
sharing supports sequential handoff, not concurrent editing.

```bash
# Manual synchronization
aicx sync codex
aicx sync claude

# Optional: disable sharing
export AICX_CODEX_HISTORY=isolated
export AICX_CLAUDE_HISTORY=isolated
```

Credentials remain private to each account profile. They are never shared with
the conversation store.

## Codex OAuth troubleshooting

If Codex or `codex_apps` fails with `HTTP 401`, `token_revoked`, or
`Encountered invalidated oauth token`, older aicx versions may have copied one
rotating OAuth refresh token into more than one profile.

```bash
aicx doctor
aicx accounts codex
aicx login codex personal --force
aicx login codex work --force
```

Run the forced login once for each affected profile. The safe forced-login flow
removes only that profile's local Codex CLI credential before starting a fresh
login. It preserves settings, sessions, history, MCP credentials, and the
profile name, and it does not run `codex logout` or revoke a token that an older
cloned profile may still hold.

Do not use `codex logout` to migrate profiles that may contain cloned OAuth
credentials. `aicx doctor` compares one-way refresh-token fingerprints and
never prints credential values.

## TODO

- Test the complete workflow with multiple real Claude Code accounts. Multiple
  Codex accounts are tested.
- Add and test VS Code account integration.
- On macOS, confirm profile-local Claude credentials when the login Keychain
  holds a `Claude Code-credentials` item (Codex is pinned to file storage).

## More documentation

- [Command cheatsheet](cheatsheet.md)
- [Architecture and storage](docs/architecture.md)
- [Changelog and migration notes](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)

## License

[MIT](LICENSE)
