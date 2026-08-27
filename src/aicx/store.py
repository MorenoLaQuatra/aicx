from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import AicxError

TOOLS = ("codex", "claude")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def default_root() -> Path:
    override = os.environ.get("AICX_HOME")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home).expanduser() / "aicx"
    return Path.home() / ".local" / "share" / "aicx"


def validate_tool(tool: str) -> str:
    if tool not in TOOLS:
        raise AicxError(f"Unsupported tool: {tool}. Expected one of: {', '.join(TOOLS)}")
    return tool


def validate_profile(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise AicxError(
            "Profile names must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens (maximum 64 characters)."
        )
    return profile


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_root()).expanduser().resolve()

    @property
    def profiles_dir(self) -> Path:
        return self.root / "profiles"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def run_dir(self) -> Path:
        return self.root / "run"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profiles_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._chmod_private(self.root)
        self._chmod_private(self.profiles_dir)
        self._chmod_private(self.run_dir)

    @staticmethod
    def _chmod_private(path: Path) -> None:
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise AicxError(f"Cannot secure directory {path}: {exc}") from exc

    def profile_root(self, profile: str) -> Path:
        return self.profiles_dir / validate_profile(profile)

    def tool_home(self, tool: str, profile: str) -> Path:
        return self.profile_root(profile) / validate_tool(tool)

    def profile_exists(self, tool: str, profile: str) -> bool:
        return self.tool_home(tool, profile).is_dir()

    def create_profile(self, tool: str, profile: str) -> Path:
        self.ensure()
        home = self.tool_home(tool, profile)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._chmod_private(home.parent)
        self._chmod_private(home)
        marker = home / ".aicx-profile.json"
        if not marker.exists():
            self._write_json(
                marker,
                {
                    "schema": 1,
                    "tool": tool,
                    "profile": profile,
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
        if tool == "codex":
            ensure_codex_file_auth(home / "config.toml")
        return home

    def list_profiles(self, tool: str | None = None) -> list[tuple[str, str]]:
        if tool is not None:
            validate_tool(tool)
        if not self.profiles_dir.exists():
            return []
        result: list[tuple[str, str]] = []
        for profile_dir in sorted(self.profiles_dir.iterdir(), key=lambda path: path.name):
            if not profile_dir.is_dir() or not PROFILE_RE.fullmatch(profile_dir.name):
                continue
            for candidate in TOOLS:
                if tool is not None and candidate != tool:
                    continue
                if (profile_dir / candidate).is_dir():
                    result.append((candidate, profile_dir.name))
        return result

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema": 1, "active": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AicxError(f"Cannot read state file {self.state_path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("active"), dict):
            raise AicxError(f"Invalid aicx state file: {self.state_path}")
        return data

    def set_active(self, tool: str, profile: str) -> None:
        validate_tool(tool)
        validate_profile(profile)
        if not self.profile_exists(tool, profile):
            raise AicxError(
                f"Profile '{profile}' does not contain {tool}. "
                f"Create it with: aicx login {tool} {profile}"
            )
        state = self.load_state()
        state["schema"] = 1
        state["active"][tool] = profile
        self.ensure()
        self._write_json(self.state_path, state)

    def get_active(self, tool: str) -> str:
        validate_tool(tool)
        active = self.load_state()["active"].get(tool)
        if not active:
            raise AicxError(
                f"No active {tool} profile. Launch one with: aicx {tool} @PROFILE"
            )
        if not self.profile_exists(tool, active):
            raise AicxError(
                f"Active {tool} profile '{active}' is missing. Select another profile."
            )
        return str(active)

    def adopt(self, tool: str, profile: str, source: Path) -> Path:
        validate_tool(tool)
        validate_profile(profile)
        source = source.expanduser().resolve()
        if not source.is_dir():
            raise AicxError(f"Source directory does not exist: {source}")
        self.ensure()
        target = self.tool_home(tool, profile)
        if target.exists():
            raise AicxError(
                f"Target already exists: {target}. Adoption never overwrites a profile."
            )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp_parent = Path(tempfile.mkdtemp(prefix=f".{tool}-adopt-", dir=target.parent))
        temp_target = temp_parent / tool
        try:
            shutil.copytree(
                source,
                temp_target,
                symlinks=True,
                ignore=_ignore_special_files,
            )
            if tool == "claude" and not (temp_target / ".claude.json").exists():
                # Claude stores this state file beside ~/.claude by default, but
                # moves it inside CLAUDE_CONFIG_DIR when that override is used.
                companion_config = source.with_name(f"{source.name}.json")
                if companion_config.is_file():
                    shutil.copy2(companion_config, temp_target / ".claude.json")
            self._chmod_private(temp_target)
            marker = temp_target / ".aicx-profile.json"
            self._write_json(
                marker,
                {
                    "schema": 1,
                    "tool": tool,
                    "profile": profile,
                    "adopted_from": str(source),
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
            if tool == "codex":
                ensure_codex_file_auth(temp_target / "config.toml")
            temp_target.replace(target)
        except Exception:
            shutil.rmtree(temp_parent, ignore_errors=True)
            raise
        shutil.rmtree(temp_parent, ignore_errors=True)
        return target

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.chmod(0o600)
            temp_path.replace(path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise


def ensure_codex_file_auth(config_path: Path) -> None:
    """Force profile-local auth so independent CODEX_HOME values stay independent."""
    desired = 'cli_auth_credentials_store = "file"\n'
    if not config_path.exists():
        config_path.write_text(desired, encoding="utf-8")
        config_path.chmod(0o600)
        return
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    first_table = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("[")),
        len(lines),
    )
    key_re = re.compile(r"^\s*cli_auth_credentials_store\s*=")
    replaced = False
    for index in range(first_table):
        if key_re.match(lines[index]):
            lines[index] = desired
            replaced = True
            break
    if not replaced:
        lines.insert(first_table, desired)
    config_path.write_text("".join(lines), encoding="utf-8")
    config_path.chmod(0o600)


def _ignore_special_files(directory: str, names: list[str]) -> list[str]:
    """Ignore transient IPC endpoints that copytree cannot safely reproduce."""
    ignored: list[str] = []
    parent = Path(directory)
    for name in names:
        path = parent / name
        try:
            mode = path.lstat().st_mode
        except OSError:
            ignored.append(name)
            continue
        if stat.S_ISLNK(mode) or stat.S_ISREG(mode) or stat.S_ISDIR(mode):
            continue
        ignored.append(name)
    return ignored
