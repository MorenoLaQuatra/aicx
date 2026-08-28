from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .errors import AicxError
from .store import Store


def _proc_stat_start_ticks(pid: int) -> str | None:
    """Linux fast path: kernel start time in clock ticks from /proc/<pid>/stat."""
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_name = content.rsplit(")", 1)[1].split()
        return str(int(fields_after_name[19]))
    except (OSError, ValueError, IndexError):
        return None


def _ps_start_time(pid: int) -> str | None:
    """Portable fallback: absolute process start time reported by ps (BSD/macOS)."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    return line or None


def process_start_signature(pid: int) -> str | None:
    """Return a stable token for a running PID that changes if the PID is reused.

    Used to detect stale registry records after a PID has been recycled. Returns
    None when the process is gone or cannot be inspected.
    """
    if sys.platform.startswith("linux"):
        ticks = _proc_stat_start_ticks(pid)
        if ticks is not None:
            return ticks
    return _ps_start_time(pid)


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
            "start_signature": process_start_signature(pid),
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
            current_signature = process_start_signature(pid)
            expected_signature = record.get("start_signature", record.get("start_ticks"))
            if expected_signature is not None:
                expected_signature = str(expected_signature)
            if current_signature is None or (
                expected_signature is not None and current_signature != expected_signature
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

