from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex_rpc import CodexAppServer
from .errors import AicxError, RpcError
from .providers import find_binary, profile_env
from .store import Store

UTC = timezone.utc


def codex_account(store: Store, profile: str) -> dict[str, Any] | None:
    binary = find_binary("codex")
    env = profile_env(store, "codex", profile)
    with CodexAppServer(binary, env) as server:
        result = server.request("account/read", {"refreshToken": False})
    account = result.get("account")
    return account if isinstance(account, dict) else None


def codex_balance(store: Store, profile: str) -> dict[str, Any]:
    binary = find_binary("codex")
    env = profile_env(store, "codex", profile)
    with CodexAppServer(binary, env) as server:
        account_result = server.request("account/read", {"refreshToken": False})
        account = account_result.get("account")
        if not account:
            return {"status": "not logged in", "account": None, "limits": []}
        try:
            limit_result = server.request("account/rateLimits/read")
        except RpcError as exc:
            return {
                "status": f"limits unavailable: {exc}",
                "account": account,
                "limits": [],
            }
    buckets = limit_result.get("rateLimitsByLimitId")
    if isinstance(buckets, dict) and buckets:
        limits = list(buckets.values())
    else:
        single = limit_result.get("rateLimits")
        limits = [single] if isinstance(single, dict) else []
    return {
        "status": "live",
        "account": account,
        "limits": limits,
        "reset_credits": limit_result.get("rateLimitResetCredits"),
    }


def claude_usage_cache_path(home: Path) -> Path:
    return home / "aicx-usage.json"


def read_claude_balance(store: Store, profile: str) -> dict[str, Any]:
    home = store.tool_home("claude", profile)
    path = claude_usage_cache_path(home)
    if not path.exists():
        return {
            "status": "waiting for first Claude response",
            "limits": {},
            "updated_at": None,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AicxError(f"Cannot read Claude usage cache {path}: {exc}") from exc
    return data


def capture_claude_statusline(payload: dict[str, Any]) -> str:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        raise AicxError("CLAUDE_CONFIG_DIR is not set; refusing to write usage outside a profile")
    home = Path(config_dir).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    rate_limits = payload.get("rate_limits")
    cache = {
        "status": "cached",
        "limits": rate_limits if isinstance(rate_limits, dict) else {},
        "updated_at": datetime.now(UTC).isoformat(),
        "session_id": payload.get("session_id"),
    }
    path = claude_usage_cache_path(home)
    fd, temp_name = tempfile.mkstemp(prefix=".aicx-usage.", dir=home)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temp_path.chmod(0o600)
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    context = (
        payload.get("context_window")
        if isinstance(payload.get("context_window"), dict)
        else {}
    )
    segments = [str(model.get("display_name") or "Claude")]
    context_pct = context.get("used_percentage")
    if context_pct is not None:
        segments.append(f"ctx {float(context_pct):.0f}%")
    if isinstance(rate_limits, dict):
        five = rate_limits.get("five_hour")
        seven = rate_limits.get("seven_day")
        if isinstance(five, dict) and five.get("used_percentage") is not None:
            segments.append(f"5h {float(five['used_percentage']):.0f}%")
        if isinstance(seven, dict) and seven.get("used_percentage") is not None:
            segments.append(f"7d {float(seven['used_percentage']):.0f}%")
    return " · ".join(segments)


def install_claude_usage_hook(home: Path) -> str:
    settings_path = home / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AicxError(f"Invalid Claude settings JSON: {settings_path}: {exc}") from exc
        if not isinstance(settings, dict):
            raise AicxError(f"Claude settings must be a JSON object: {settings_path}")
    else:
        settings = {}
    existing = settings.get("statusLine")
    desired = {
        "type": "command",
        "command": "aicx _claude-statusline",
        "refreshInterval": 60,
    }
    if existing == desired:
        return "already installed"
    if existing is not None:
        return "skipped: an existing Claude status line is configured"
    settings["statusLine"] = desired
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".settings.", dir=home)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temp_path.chmod(0o600)
        temp_path.replace(settings_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return "installed"
