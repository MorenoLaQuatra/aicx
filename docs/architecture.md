# Architecture

## Principle

`aicx` owns selection, history synchronization, and process launching; the
official tools own identity. There is no token broker and no custom OAuth client.

```text
name selected in state.json
          │
          ├── Codex:  CODEX_HOME=<profile>/codex  ──> official codex binary
          │                       │                     ├── private auth/runtime
          │                       └── synchronized ─────┴── shared rollouts
          │
          └── Claude: CLAUDE_CONFIG_DIR=<profile>/claude ─> official claude binary
                                  │                      ├── private auth/settings
                                  └── synchronized ──────┴── shared project history
```

The preferred `aicx TOOL @PROFILE` form selects and launches in one operation.
Selection writes only `state.json`; launching synchronizes persisted conversation
files, constructs a provider-specific environment, starts the official
executable, and records the PID plus a process start-time signature used to
detect PID reuse (`/proc/<pid>/stat` start ticks on Linux, `ps -o lstart` on
macOS). Login is a separate one-time command.

## Shared history

Provider authentication does not need to own conversation history. However,
Codex does not expose a separate supported `CODEX_SESSIONS_HOME`: `CODEX_HOME`
contains auth and rollout files, while `CODEX_SQLITE_HOME` covers only
SQLite-backed state. Sharing the entire home would therefore race on credentials
and runtimes.

aicx instead maintains canonical history stores under `shared/` and atomically
merges the newest persisted files into every profile before launch and after
exit. Codex shares JSONL rollouts. Claude shares project transcripts and their
supporting tool-result, project-memory, and checkpoint files. Both providers
discover copied conversations through their normal home directories. SQLite
authentication and runtime databases are never copied between profiles. After
copying Codex rollouts, aicx reconciles the absolute `rollout_path` values in
each profile's own thread index and merges only the matching rows from Codex's
derived paginated-history cache. Credential files, configuration, logs, plugins,
unrelated caches, and IPC endpoints are never synchronized.

This is sequential handoff, not collaborative multi-writer editing. The same
session must not be active under two account profiles simultaneously. Set
`AICX_CODEX_HISTORY=isolated` or `AICX_CLAUDE_HISTORY=isolated` to disable it.

## Provider adapters

### Codex

- Isolation: `CODEX_HOME`.
- Authentication: independent `codex login` sessions and `codex login status`.
- Credential storage: profile-local file storage selected in `config.toml`.
- Usage and sessions: JSON-RPC over `codex app-server --stdio`.
- Active-session stop: `thread/read`, then `turn/interrupt`.

Every profile config pins `cli_auth_credentials_store = "file"`, which makes
CLI auth deterministic at `<profile>/codex/auth.json`. Current Codex can also
store file-backed MCP OAuth credentials at `<profile>/codex/.credentials.json`.
Keyring-backed modes can pair the OS keyring with encrypted files under
`<profile>/codex/secrets/`. None of these credential stores is copied by Codex
adoption. `aicx adopt codex PROFILE` imports safe settings, rollouts, and local
state, reconciles absolute thread paths, synchronizes history, and then starts
the official browser login inside the new profile. The profile becomes active
only after login succeeds.

Forced Codex reauthentication unlinks only the selected profile's local
`auth.json` before it launches `codex login`. It deliberately does not invoke
`codex logout`: older aicx releases could clone a rotating refresh token across
homes, and a server-side revocation from one clone would invalidate the others.
Unrelated MCP OAuth credentials, settings, session databases, and conversation
history remain intact.

`aicx doctor` inspects the normal Codex home and every aicx Codex profile. It
recognizes current ChatGPT OAuth serialization, older refresh-token layouts,
API-key-only auth, partial files, and malformed files. Duplicate comparison uses
only SHA-256 fingerprints in memory; tokens and fingerprints are not displayed.
Malformed or unknown files produce a warning without making `doctor` fail.

The RPC client uses a background line reader because a text-buffered
`select`/`readline` loop can stall when a notification and response arrive in a
single operating-system read.

### Claude Code

- Isolation: `CLAUDE_CONFIG_DIR`.
- Authentication: `claude auth login` and `claude auth status`.
- Usage: the documented status-line JSON stream, cached per profile.
- Sessions: metadata inferred from files under `projects/`; conversation content
  is not parsed.

The status-line installer will not compose with or overwrite an existing hook.
That conservative policy avoids executing or rewriting arbitrary shell commands.

## macOS support

`aicx` runs on Linux and macOS. The platform guard now only rejects other
systems (for example Windows).

- PID identity uses `/proc/<pid>/stat` on Linux and falls back to `ps -o lstart`
  on macOS and other BSD-flavored systems.
- Codex credentials stay profile-local because `config.toml` pins
  `cli_auth_credentials_store = "file"`, so the macOS Keychain is not consulted.
- Claude Code writes `.credentials.json` inside `CLAUDE_CONFIG_DIR`. On a macOS
  machine whose login Keychain also holds a legacy `Claude Code-credentials`
  item, confirm the provider reads the profile file before relying on multiple
  Claude accounts; `security delete-generic-password -s "Claude Code-credentials"`
  removes the stale item.

The on-disk profile layout and CLI syntax are identical on both platforms.
