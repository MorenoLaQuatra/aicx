from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex_rpc import CodexAppServer
from .providers import find_binary, profile_env
from .store import Store

UTC = timezone.utc


def codex_sessions(store: Store, profile: str, limit: int = 100) -> list[dict[str, Any]]:
    binary = find_binary("codex")
    env = profile_env(store, "codex", profile)
    sessions: list[dict[str, Any]] = []
    cursor: str | None = None
    with CodexAppServer(binary, env) as server:
        while len(sessions) < limit:
            params: dict[str, Any] = {
                "limit": min(100, limit - len(sessions)),
                "sortKey": "updated_at",
                "sortDirection": "desc",
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = server.request("thread/list", params)
            data = result.get("data", [])
            if not isinstance(data, list):
                break
            for item in data:
                if not isinstance(item, dict):
                    continue
                status = item.get("status")
                sessions.append(
                    {
                        "id": item.get("id", ""),
                        "name": item.get("name") or item.get("preview") or "",
                        "state": (
                            status.get("type", "")
                            if isinstance(status, dict)
                            else str(status or "")
                        ),
                        "updated_at": item.get("updatedAt"),
                        "cwd": item.get("cwd") or "",
                    }
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
    return sessions[:limit]


def interrupt_codex_session(store: Store, profile: str, session_id: str) -> None:
    binary = find_binary("codex")
    env = profile_env(store, "codex", profile)
    with CodexAppServer(binary, env) as server:
        result = server.request(
            "thread/read", {"threadId": session_id, "includeTurns": True}
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise ValueError(f"Codex session not found: {session_id}")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            turns = []
        active_turn: dict[str, Any] | None = None
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            status = str(turn.get("status", "")).lower().replace("_", "")
            if status in {"inprogress", "running"}:
                active_turn = turn
                break
        if active_turn is None or not active_turn.get("id"):
            raise ValueError(f"Codex session is not actively running: {session_id}")
        server.request(
            "turn/interrupt",
            {"threadId": session_id, "turnId": str(active_turn["id"])},
        )


def interrupt_all_codex_sessions(store: Store, profile: str) -> list[str]:
    active = [
        session
        for session in codex_sessions(store, profile, limit=500)
        if session.get("state") == "active"
    ]
    interrupted: list[str] = []
    for session in active:
        session_id = str(session["id"])
        try:
            interrupt_codex_session(store, profile, session_id)
        except ValueError:
            continue
        interrupted.append(session_id)
    return interrupted


def claude_sessions(store: Store, profile: str, limit: int = 100) -> list[dict[str, Any]]:
    home = store.tool_home("claude", profile)
    projects = home / "projects"
    if not projects.exists():
        return []
    result: list[dict[str, Any]] = []
    paths_with_times: list[tuple[float, Path]] = []
    for path in projects.glob("**/*.jsonl"):
        try:
            paths_with_times.append((path.stat().st_mtime, path))
        except OSError:
            continue
    paths = [path for _, path in sorted(paths_with_times, reverse=True)]
    for path in paths[:limit]:
        try:
            stat = path.stat()
        except OSError:
            continue
        result.append(
            {
                "id": path.stem,
                "name": "",
                "state": "saved",
                "updated_at": int(stat.st_mtime),
                "cwd": str(path.parent.relative_to(projects)),
            }
        )
    return result


def format_timestamp(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).astimezone().strftime(
            "%Y-%m-%d %H:%M"
        )
    except (TypeError, ValueError, OSError):
        return str(value)
