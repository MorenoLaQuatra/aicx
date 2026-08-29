from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from .codex_auth import CODEX_CREDENTIAL_ROOT_ENTRIES
from .errors import AicxError

UTC = timezone.utc

TOOLS = ("codex", "claude")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CODEX_THREAD_ID_RE = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)
CODEX_THREAD_HISTORY_TABLES = (
    "thread_items",
    "thread_turns",
    "thread_realtime_items",
    "thread_history_projection_state",
)


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

    def _prune_profile_dir(self, profile: str) -> None:
        """Remove the profile directory once it holds no tools."""
        profile_dir = self.profiles_dir / profile
        try:
            if profile_dir.is_dir() and not any(profile_dir.iterdir()):
                profile_dir.rmdir()
        except OSError:
            pass

    def _rewrite_marker_profile(self, home: Path, profile: str) -> None:
        marker = home / ".aicx-profile.json"
        if not marker.is_file():
            return
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            data["profile"] = profile
            self._write_json(marker, data)

    def remove_tool(self, tool: str, profile: str) -> None:
        """Delete one tool's home from a profile and forget it from state.json."""
        home = self.tool_home(tool, profile)
        if not home.is_dir():
            raise AicxError(f"Profile '{profile}' does not contain {tool}.")
        shutil.rmtree(home)
        self._prune_profile_dir(profile)
        state = self.load_state()
        if state["active"].get(tool) == profile:
            state["active"].pop(tool, None)
            state["schema"] = 1
            self.ensure()
            self._write_json(self.state_path, state)

    def rename_tool(self, tool: str, old: str, new: str) -> Path:
        """Move one tool's home from profile `old` to profile `new`."""
        source = self.tool_home(tool, old)
        if not source.is_dir():
            raise AicxError(f"Profile '{old}' does not contain {tool}.")
        target = self.tool_home(tool, new)
        if target.exists():
            raise AicxError(f"Profile '{new}' already contains {tool}.")
        self.ensure()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._chmod_private(target.parent)
        source.replace(target)
        self._prune_profile_dir(old)
        self._rewrite_marker_profile(target, new)
        state = self.load_state()
        if state["active"].get(tool) == old:
            state["active"][tool] = new
            state["schema"] = 1
            self._write_json(self.state_path, state)
        return target

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

    def get_preference(self, key: str) -> Any:
        """Return a stored CLI preference, or None when it was never set."""
        return self.load_state().get(key)

    def set_preference(self, key: str, value: Any) -> None:
        """Persist (or, with value=None, clear) a CLI preference in state.json."""
        state = self.load_state()
        state["schema"] = 1
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
        self.ensure()
        self._write_json(self.state_path, state)

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
                ignore=(
                    partial(codex_adopt_ignore, source)
                    if tool == "codex"
                    else _ignore_special_files
                ),
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
                reconcile_codex_thread_paths(temp_target, stored_home=target)
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


def reconcile_codex_thread_paths(home: Path, *, stored_home: Path | None = None) -> int:
    """Point indexed Codex threads at rollouts inside their current profile home.

    Codex stores absolute rollout paths in ``state_*.sqlite``. Copying a Codex
    home during adoption, or copying a newer rollout between profile homes,
    otherwise leaves an existing thread row pointing at the old home. If that
    old file still exists, ``codex resume`` can load the stale copy.

    ``home`` is the directory being inspected. ``stored_home`` is only different
    while an adopted home is staged in a temporary directory before being moved
    to its final location.
    """
    home = home.resolve()
    final_home = (stored_home or home).resolve()
    rollouts: dict[tuple[str, bool], tuple[Path, Path]] = {}
    for area, archived in (("sessions", False), ("archived_sessions", True)):
        area_root = home / area
        if not area_root.is_dir():
            continue
        for path in area_root.rglob("*.jsonl"):
            if not path.is_file():
                continue
            match = CODEX_THREAD_ID_RE.search(path.name)
            if match is None:
                continue
            key = (match.group("id").lower(), archived)
            relative = path.relative_to(home)
            current = rollouts.get(key)
            if current is None or _newer_file(path, current[0]):
                rollouts[key] = (path, relative)

    if not rollouts:
        return 0

    reconciled = 0
    for database in sorted(home.glob("state_*.sqlite")):
        try:
            with sqlite3.connect(database, timeout=5) as connection:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(threads)")
                }
                if not {"id", "rollout_path", "archived"}.issubset(columns):
                    continue
                updates: list[tuple[str, str]] = []
                for thread_id, archived, rollout_path in connection.execute(
                    "SELECT id, archived, rollout_path FROM threads"
                ):
                    normalized_id = str(thread_id).lower()
                    key = (normalized_id, bool(archived))
                    candidate = rollouts.get(key)
                    if candidate is None:
                        candidate = rollouts.get((normalized_id, not bool(archived)))
                    if candidate is None:
                        continue
                    desired = str(final_home / candidate[1])
                    if str(rollout_path) != desired:
                        updates.append((desired, str(thread_id)))
                if updates:
                    connection.executemany(
                        "UPDATE threads SET rollout_path = ? WHERE id = ?",
                        updates,
                    )
                    reconciled += len(updates)
        except sqlite3.Error as exc:
            raise AicxError(
                f"Cannot reconcile Codex thread paths in {database}: {exc}"
            ) from exc
    return reconciled


def codex_thread_projection_status(
    home: Path,
) -> dict[str, tuple[Path, int, int]]:
    """Return thread ID -> (database, byte offset, ordinal) for Codex history."""
    status: dict[str, tuple[Path, int, int]] = {}
    for database in sorted(home.glob("thread_history_*.sqlite")):
        try:
            with sqlite3.connect(database, timeout=5) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "thread_history_projection_state" not in tables:
                    continue
                for thread_id, byte_offset, ordinal in connection.execute(
                    "SELECT thread_id, next_rollout_byte_offset, next_rollout_ordinal "
                    "FROM thread_history_projection_state"
                ):
                    status[str(thread_id).lower()] = (
                        database,
                        int(byte_offset),
                        int(ordinal),
                    )
        except sqlite3.Error as exc:
            raise AicxError(
                f"Cannot inspect Codex thread history in {database}: {exc}"
            ) from exc
    return status


def copy_codex_thread_projection(
    source_database: Path, destination_database: Path, thread_id: str
) -> int:
    """Copy one thread's derived paginated-history rows between profile DBs."""
    destination_database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with sqlite3.connect(source_database, timeout=5) as source:
            if not destination_database.exists():
                with sqlite3.connect(destination_database, timeout=5) as destination:
                    source.backup(destination)
                return 1

            with sqlite3.connect(destination_database, timeout=5) as destination:
                table_columns: dict[str, list[str]] = {}
                for table in CODEX_THREAD_HISTORY_TABLES:
                    source_columns = [
                        str(row[1])
                        for row in source.execute(f"PRAGMA table_info({table})")
                    ]
                    destination_columns = [
                        str(row[1])
                        for row in destination.execute(f"PRAGMA table_info({table})")
                    ]
                    if not source_columns or source_columns != destination_columns:
                        raise AicxError(
                            "Codex thread-history schemas differ between profiles "
                            f"for table {table}."
                        )
                    table_columns[table] = source_columns

                copied = 0
                normalized_id = thread_id.lower()
                rows_by_table: dict[str, list[tuple[Any, ...]]] = {}
                for table, columns in table_columns.items():
                    column_list = ", ".join(columns)
                    rows_by_table[table] = list(
                        source.execute(
                            f"SELECT {column_list} FROM {table} "
                            "WHERE lower(thread_id) = ?",
                            (normalized_id,),
                        )
                    )

                for table in CODEX_THREAD_HISTORY_TABLES:
                    destination.execute(
                        f"DELETE FROM {table} WHERE lower(thread_id) = ?",
                        (normalized_id,),
                    )
                for table, rows in rows_by_table.items():
                    if not rows:
                        continue
                    columns = table_columns[table]
                    placeholders = ", ".join("?" for _ in columns)
                    column_list = ", ".join(columns)
                    destination.executemany(
                        f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})",
                        rows,
                    )
                    copied += len(rows)
                return copied
    except sqlite3.Error as exc:
        raise AicxError(
            "Cannot synchronize Codex thread history from "
            f"{source_database} to {destination_database}: {exc}"
        ) from exc


def _newer_file(candidate: Path, current: Path) -> bool:
    """Return whether candidate is the better local copy for a duplicate ID."""
    try:
        candidate_stat = candidate.stat()
        current_stat = current.stat()
    except OSError as exc:
        raise AicxError(
            f"Cannot inspect Codex rollout while reconciling paths: {exc}"
        ) from exc
    return (candidate_stat.st_mtime_ns, candidate_stat.st_size) > (
        current_stat.st_mtime_ns,
        current_stat.st_size,
    )


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


def codex_adopt_ignore(
    source: Path, directory: str, names: list[str]
) -> list[str]:
    """Exclude transient files and every known Codex credential store."""
    ignored = _ignore_special_files(directory, names)
    if Path(directory).resolve() == source.resolve():
        ignored.extend(
            name
            for name in names
            if name in CODEX_CREDENTIAL_ROOT_ENTRIES and name not in ignored
        )
    return ignored
