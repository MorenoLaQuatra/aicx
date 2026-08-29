# Changelog

## 0.4.0 - 2026-08-29

This is a minor release because it intentionally changes the externally visible
Codex adoption workflow and adds OAuth migration diagnostics. Existing profiles
created with `aicx login` remain compatible.

### Fixed

- Fixed Codex profiles becoming invalid with `token_revoked` after OAuth
  credentials were copied between independent `CODEX_HOME` values.
- Codex CLI and MCP OAuth credential files are no longer cloned by
  `aicx adopt`.
- Forced Codex reauthentication now clears stale profile-local CLI credentials
  without calling `codex logout` or revoking credentials that may exist in
  older cloned profiles.

### Added

- Added `aicx doctor` diagnostics for duplicated ChatGPT OAuth refresh
  credentials using non-reversible fingerprints.
- Added migration and `HTTP 401`/`token_revoked` recovery instructions.

### Changed

- `aicx adopt codex PROFILE` now imports safe local Codex state and then starts
  an independent browser login for the new profile. It selects the profile only
  when authentication succeeds.
- Standard browser OAuth remains the default. `--device-auth` remains optional
  for headless and remote environments.

### Migration

Users who adopted Codex profiles with an older aicx release should run:

```bash
aicx doctor
aicx accounts codex
aicx login codex PROFILE --force
```

Repeat the forced login for each affected Codex profile. Do not run
`codex logout` as part of this migration. Settings, session databases,
conversation history, and profile names are preserved.
