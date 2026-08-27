from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import AicxError
from .store import Store, validate_tool


HISTORY_AREAS: dict[str, dict[str, str]] = {
    "codex": {
        "sessions": "*.jsonl",
        "archived_sessions": "*.jsonl",
    },
    "claude": {
        # Transcript JSONL, spilled tool results, and project memory all live here.
        "projects": "*",
        # Claude needs these snapshots to rewind a resumed conversation.
        "file-history": "*",
    },
}


@dataclass(frozen=True)
class SyncResult:
    imported: int
    distributed: int
    profiles: int
    skipped_active_profiles: int = 0


def shared_history_count(store: Store, tool: str) -> int | None:
    """Return the canonical conversation count, or None when sharing is disabled."""
    validate_tool(tool)
    if not history_is_shared(tool):
        return None
    shared_root = store.root / "shared" / f"{tool}-history"
    return sum(
        1
        for area in HISTORY_AREAS[tool]
        for path in (shared_root / area).rglob("*.jsonl")
        if path.is_file()
    )


def history_is_shared(tool: str) -> bool:
    validate_tool(tool)
    variable = f"AICX_{tool.upper()}_HISTORY"
    value = os.environ.get(variable, "shared").strip().lower()
    if value not in {"shared", "isolated"}:
        raise AicxError(
            f"{variable} must be either 'shared' or 'isolated'."
        )
    return value == "shared"


def sync_history(store: Store, tool: str) -> SyncResult:
    """Merge persisted conversations and distribute them to every profile."""
    validate_tool(tool)
    profile_homes = [
        (profile, store.tool_home(tool, profile))
        for candidate, profile in store.list_profiles(tool)
        if candidate == tool
    ]
    if not history_is_shared(tool) or not profile_homes:
        return SyncResult(imported=0, distributed=0, profiles=len(profile_homes))

    # Never read or replace a rollout owned by an aicx-managed live process.
    from .processes import ProcessRegistry

    active_profiles = {
        str(record.get("profile"))
        for record in ProcessRegistry(store).list(tool=tool)
    }

    shared_root = store.root / "shared" / f"{tool}-history"
    shared_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        shared_root.chmod(0o700)
    except OSError as exc:
        raise AicxError(f"Cannot secure shared {tool} history {shared_root}: {exc}") from exc

    imported = 0
    for area, pattern in HISTORY_AREAS[tool].items():
        canonical_area = shared_root / area
        canonical_area.mkdir(parents=True, exist_ok=True, mode=0o700)
        for profile, home in profile_homes:
            if profile in active_profiles:
                continue
            source_area = home / area
            if not source_area.is_dir():
                continue
            for source in source_area.rglob(pattern):
                if not source.is_file():
                    continue
                relative = source.relative_to(source_area)
                canonical = canonical_area / relative
                if _source_wins(source, canonical):
                    _atomic_copy(source, canonical)
                    imported += 1

    distributed = 0
    for area, pattern in HISTORY_AREAS[tool].items():
        canonical_area = shared_root / area
        if not canonical_area.is_dir():
            continue
        for canonical in canonical_area.rglob(pattern):
            if not canonical.is_file():
                continue
            relative = canonical.relative_to(canonical_area)
            for profile, home in profile_homes:
                if profile in active_profiles:
                    continue
                destination = home / area / relative
                if _source_wins(canonical, destination):
                    _atomic_copy(canonical, destination)
                    distributed += 1

    return SyncResult(
        imported=imported,
        distributed=distributed,
        profiles=len(profile_homes),
        skipped_active_profiles=len(active_profiles),
    )


def codex_history_is_shared() -> bool:
    return history_is_shared("codex")


def sync_codex_history(store: Store) -> SyncResult:
    return sync_history(store, "codex")


def _source_wins(source: Path, destination: Path) -> bool:
    try:
        source_stat = source.stat()
    except OSError as exc:
        raise AicxError(f"Cannot inspect history file {source}: {exc}") from exc
    try:
        destination_stat = destination.stat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise AicxError(f"Cannot inspect history file {destination}: {exc}") from exc
    if source_stat.st_mtime_ns != destination_stat.st_mtime_ns:
        return source_stat.st_mtime_ns > destination_stat.st_mtime_ns
    return source_stat.st_size != destination_stat.st_size


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        temp_path.replace(destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
