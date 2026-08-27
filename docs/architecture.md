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
Selection writes only `state.json`; launching synchronizes persisted JSONL
conversations, constructs a provider-specific environment, starts the official
executable, and records the PID plus its Linux `/proc` start ticks. Login is a
separate one-time command.

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
databases, credential files, configuration, logs, plugins, caches, and IPC
endpoints are never synchronized.

This is sequential handoff, not collaborative multi-writer editing. The same
session must not be active under two account profiles simultaneously. Set
`AICX_CODEX_HISTORY=isolated` or `AICX_CLAUDE_HISTORY=isolated` to disable it.

## Provider adapters

### Codex

- Isolation: `CODEX_HOME`.
- Authentication: `codex login` and `codex login status`.
- Credential storage: profile-local file storage selected in `config.toml`.
- Usage and sessions: JSON-RPC over `codex app-server --stdio`.
- Active-session stop: `thread/read`, then `turn/interrupt`.

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

## VS Code

`aicx vscode NAME PATH` starts `code` with provider environment variables and a
per-name `--user-data-dir`. The user-data split is required because VS Code
normally routes new windows to an existing process, whose inherited environment
cannot be changed. Existing windows are deliberately unaffected.

## macOS plan

Most code is already portable. The Linux guard makes the unsupported boundary
explicit while these areas are tested on macOS:

1. Replace `/proc` PID identity with a portable process-start-time adapter.
2. Verify profile-local provider credential behavior when Keychain is available.
3. Test the `code` launcher and both official extensions with isolated user data.
4. Add a macOS CI job and remove the platform guard only after end-to-end tests.

The on-disk profile layout and CLI syntax should not need to change.
