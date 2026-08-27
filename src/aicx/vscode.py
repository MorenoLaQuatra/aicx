from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from .errors import AicxError
from .store import Store, TOOLS, validate_profile


def vscode_launch_context() -> tuple[str, bool]:
    """Describe whether this shell can ask VS Code to open a window."""
    if os.environ.get("VSCODE_IPC_HOOK_CLI"):
        return "VS Code integrated/remote terminal", True
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return "graphical desktop session", True
    if os.environ.get("SSH_CONNECTION"):
        return "plain SSH shell (no display or VS Code IPC)", False
    return "headless shell (no display or VS Code IPC)", False


def build_vscode_command(
    store: Store,
    profile: str,
    target: str,
    extra_args: Sequence[str] = (),
) -> tuple[list[str], dict[str, str]]:
    validate_profile(profile)
    code = shutil.which("code")
    if not code:
        raise AicxError("Cannot find the VS Code 'code' command in PATH.")
    context, launchable = vscode_launch_context()
    if not launchable:
        raise AicxError(
            f"VS Code is installed at {code}, but this is a {context}. "
            "Run this command from a graphical terminal on that machine."
        )
    available = [tool for tool in TOOLS if store.profile_exists(tool, profile)]
    if not available:
        raise AicxError(f"Profile '{profile}' does not contain Codex or Claude.")
    env = os.environ.copy()
    if "codex" in available:
        env["CODEX_HOME"] = str(store.tool_home("codex", profile))
    if "claude" in available:
        env["CLAUDE_CONFIG_DIR"] = str(store.tool_home("claude", profile))
    user_data = store.root / "vscode" / profile / "user-data"
    user_data.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [
        code,
        "--new-window",
        "--user-data-dir",
        str(user_data),
        *extra_args,
        str(Path(target).expanduser()),
    ]
    return command, env


def launch_vscode(store: Store, profile: str, target: str, extra_args: Sequence[str] = ()) -> int:
    command, env = build_vscode_command(store, profile, target, extra_args)
    process = subprocess.Popen(command, env=env, start_new_session=True)
    return process.pid
