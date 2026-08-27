from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .errors import AicxError
from .store import Store, validate_tool


@dataclass(frozen=True)
class ToolSpec:
    name: str
    binary: str
    env_var: str
    login_args: tuple[str, ...]
    status_args: tuple[str, ...]
    default_home_name: str


SPECS = {
    "codex": ToolSpec(
        name="codex",
        binary="codex",
        env_var="CODEX_HOME",
        login_args=("login",),
        status_args=("login", "status"),
        default_home_name=".codex",
    ),
    "claude": ToolSpec(
        name="claude",
        binary="claude",
        env_var="CLAUDE_CONFIG_DIR",
        login_args=("auth", "login"),
        status_args=("auth", "status"),
        default_home_name=".claude",
    ),
}


def spec_for(tool: str) -> ToolSpec:
    return SPECS[validate_tool(tool)]


def find_binary(tool: str) -> str:
    spec = spec_for(tool)
    binary = shutil.which(spec.binary)
    if not binary:
        raise AicxError(
            f"Cannot find '{spec.binary}' in PATH. Install {tool} before using this command."
        )
    return binary


def profile_env(store: Store, tool: str, profile: str) -> dict[str, str]:
    spec = spec_for(tool)
    home = store.tool_home(tool, profile)
    if not home.is_dir():
        raise AicxError(
            f"Profile '{profile}' does not contain {tool}. "
            f"Create it with: aicx login {tool} {profile}"
        )
    env = os.environ.copy()
    env[spec.env_var] = str(home)
    env["AICX_PROFILE"] = profile
    return env


def default_home(tool: str) -> Path:
    return Path.home() / spec_for(tool).default_home_name


def native_status(store: Store, tool: str, profile: str) -> dict[str, Any]:
    binary = find_binary(tool)
    spec = spec_for(tool)
    env = profile_env(store, tool, profile)
    try:
        result = subprocess.run(
            [binary, *spec.status_args],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"logged_in": False, "detail": "status timed out"}
    output = (result.stdout or result.stderr).strip()
    parsed: dict[str, Any] | None = None
    if tool == "claude" and output:
        try:
            candidate = json.loads(output)
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            parsed = None
    detail = output.splitlines()[-1] if output else ""
    status: dict[str, Any] = {
        "logged_in": result.returncode == 0,
        "detail": detail,
    }
    if parsed:
        status["raw"] = parsed
        if isinstance(parsed.get("loggedIn"), bool):
            status["logged_in"] = parsed["loggedIn"]
        status["email"] = parsed.get("email") or parsed.get("accountEmail")
        status["plan"] = parsed.get("subscriptionType") or parsed.get("plan")
        status["detail"] = " · ".join(
            str(value)
            for value in (parsed.get("authMethod"), parsed.get("apiProvider"))
            if value
        )
    return status


def login(
    store: Store,
    tool: str,
    profile: str,
    *,
    force: bool = False,
    extra_args: Sequence[str] = (),
) -> int:
    store.create_profile(tool, profile)
    binary = find_binary(tool)
    spec = spec_for(tool)
    if not force:
        status = native_status(store, tool, profile)
        if status["logged_in"]:
            return 0
    env = profile_env(store, tool, profile)
    result = subprocess.run([binary, *spec.login_args, *extra_args], env=env, check=False)
    return result.returncode


def build_tool_command(
    store: Store,
    tool: str,
    args: Sequence[str],
    *,
    profile: str | None = None,
) -> tuple[list[str], dict[str, str], str]:
    profile = profile or store.get_active(tool)
    binary = find_binary(tool)
    return [binary, *args], profile_env(store, tool, profile), profile
