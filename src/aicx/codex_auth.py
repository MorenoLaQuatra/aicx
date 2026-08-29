from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import AicxError


# Codex currently persists CLI auth and file-backed MCP OAuth credentials in
# these two CODEX_HOME files. Neither is safe to clone into another profile.
CODEX_CLI_AUTH_FILENAME = "auth.json"
CODEX_MCP_OAUTH_FILENAME = ".credentials.json"
CODEX_SECRETS_DIRNAME = "secrets"
CODEX_CREDENTIAL_ROOT_ENTRIES = frozenset(
    (
        CODEX_CLI_AUTH_FILENAME,
        CODEX_MCP_OAUTH_FILENAME,
        CODEX_SECRETS_DIRNAME,
    )
)


@dataclass(frozen=True)
class CodexAuthInspection:
    state: str
    oauth_fingerprint: str | None = None


@dataclass(frozen=True)
class CodexAuthDiagnostics:
    duplicate_groups: tuple[tuple[str, ...], ...]
    uninspectable: tuple[str, ...]


def codex_auth_files(home: Path) -> tuple[Path, ...]:
    """Return every known file-backed Codex credential store."""
    return (
        home / CODEX_CLI_AUTH_FILENAME,
        home / CODEX_MCP_OAUTH_FILENAME,
        home / CODEX_SECRETS_DIRNAME / "codex_auth.age",
        home / CODEX_SECRETS_DIRNAME / "mcp_oauth.age",
        home / CODEX_SECRETS_DIRNAME / "local.age",
    )


def prepare_codex_reauthentication(home: Path) -> bool:
    """Remove only profile-local CLI auth without asking Codex to revoke it.

    Older aicx releases may have cloned this file into multiple CODEX_HOME
    values. Running ``codex logout`` here could revoke the shared refresh token
    and break every clone, so forced reauthentication must only unlink the
    selected profile's local file.
    """
    auth_path = home / CODEX_CLI_AUTH_FILENAME
    try:
        auth_path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AicxError(
            f"Cannot remove stale Codex credentials at {auth_path}: {exc}"
        ) from exc
    return True


def inspect_codex_auth_file(auth_path: Path) -> CodexAuthInspection:
    """Classify Codex auth without returning or logging credential values."""
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CodexAuthInspection("missing")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return CodexAuthInspection("uninspectable")

    if not isinstance(data, dict):
        return CodexAuthInspection("uninspectable")

    auth_mode = data.get("auth_mode")
    api_key = data.get("OPENAI_API_KEY")
    if auth_mode == "apikey" or (
        auth_mode is None and isinstance(api_key, str) and api_key.strip()
    ):
        return CodexAuthInspection("api-key")

    known_non_oauth_modes = {
        "headers",
        "agentIdentity",
        "personalAccessToken",
        "bedrockApiKey",
        "bedrockAccessKeys",
    }
    if auth_mode in known_non_oauth_modes:
        return CodexAuthInspection("other")
    if auth_mode not in (None, "chatgpt", "chatgptAuthTokens"):
        return CodexAuthInspection("uninspectable")

    tokens = data.get("tokens")
    refresh_token: object = None
    if isinstance(tokens, dict):
        # ``refresh_token`` is the current schema. The camelCase and top-level
        # variants cover older serialized forms without recursively searching
        # arbitrary JSON or exposing any other auth material.
        refresh_token = tokens.get("refresh_token") or tokens.get("refreshToken")
    elif tokens is not None:
        return CodexAuthInspection("uninspectable")
    if refresh_token is None:
        refresh_token = data.get("refresh_token") or data.get("refreshToken")

    if isinstance(refresh_token, str) and refresh_token:
        fingerprint = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
        return CodexAuthInspection("chatgpt-oauth", fingerprint)
    if auth_mode in ("chatgpt", "chatgptAuthTokens") or isinstance(tokens, dict):
        return CodexAuthInspection("oauth-without-refresh")
    return CodexAuthInspection("other")


def codex_oauth_fingerprint(auth_path: Path) -> str | None:
    """Return only a one-way refresh-token fingerprint, when present."""
    return inspect_codex_auth_file(auth_path).oauth_fingerprint


def codex_auth_diagnostics(
    homes: Sequence[tuple[str, Path]],
) -> CodexAuthDiagnostics:
    """Find CODEX_HOME values that contain the same ChatGPT OAuth session."""
    labels_by_fingerprint: dict[str, list[str]] = {}
    uninspectable: list[str] = []
    seen_paths: set[Path] = set()
    for label, home in homes:
        normalized_home = home.expanduser().resolve()
        if normalized_home in seen_paths:
            continue
        seen_paths.add(normalized_home)
        inspection = inspect_codex_auth_file(
            normalized_home / CODEX_CLI_AUTH_FILENAME
        )
        if inspection.oauth_fingerprint is not None:
            labels_by_fingerprint.setdefault(
                inspection.oauth_fingerprint, []
            ).append(label)
        elif inspection.state == "uninspectable":
            uninspectable.append(label)

    duplicate_groups = tuple(
        tuple(labels)
        for labels in labels_by_fingerprint.values()
        if len(labels) > 1
    )
    return CodexAuthDiagnostics(duplicate_groups, tuple(uninspectable))
