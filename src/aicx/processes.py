from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from .errors import AicxError
from .store import Store


def linux_start_ticks(pid: int) -> int | None:
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_name = content.rsplit(")", 1)[1].split()
        return int(fields_after_name[19])
    except (OSError, ValueError, IndexError):
        return None


class ProcessRegistry:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.store.ensure()

    def add(self, pid: int, tool: str, profile: str, command: Sequence[str]) -> None:
        record = {
            "pid": pid,
            "tool": tool,
            "profile": profile,
            "command": list(command),
            "started_at": time.time(),
            "start_ticks": linux_start_ticks(pid),
        }
        self.store._write_json(self.store.run_dir / f"{pid}.json", record)

    def remove(self, pid: int) -> None:
        (self.store.run_dir / f"{pid}.json").unlink(missing_ok=True)

    def list(self, tool: str | None = None, profile: str | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not self.store.run_dir.exists():
            return result
        for path in sorted(self.store.run_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                pid = int(record["pid"])
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                continue
            current_ticks = linux_start_ticks(pid)
            expected_ticks = record.get("start_ticks")
            if current_ticks is None or (
                expected_ticks is not None and current_ticks != expected_ticks
            ):
                path.unlink(missing_ok=True)
                continue
            if tool is not None and record.get("tool") != tool:
                continue
            if profile is not None and record.get("profile") != profile:
                continue
            result.append(record)
        return result

    def terminate(self, tool: str, target: str, profile: str | None = None) -> list[int]:
        records = self.list(tool=tool, profile=profile)
        if target != "all":
            try:
                pid = int(target)
            except ValueError as exc:
                raise AicxError(
                    f"{tool} process targets must be a tracked PID or 'all'."
                ) from exc
            records = [record for record in records if record["pid"] == pid]
        if not records:
            raise AicxError("No matching aicx-managed processes are running.")
        terminated: list[int] = []
        for record in records:
            pid = int(record["pid"])
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                self.remove(pid)
                continue
            except PermissionError as exc:
                raise AicxError(f"Permission denied while stopping PID {pid}") from exc
            terminated.append(pid)
        return terminated


def run_tracked(
    store: Store,
    tool: str,
    profile: str,
    command: Sequence[str],
    env: dict[str, str],
) -> int:
    process = subprocess.Popen(list(command), env=env)
    registry = ProcessRegistry(store)
    registry.add(process.pid, tool, profile, command)
    try:
        return process.wait()
    finally:
        registry.remove(process.pid)

