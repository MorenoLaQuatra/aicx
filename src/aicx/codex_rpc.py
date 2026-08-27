from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections import deque
from typing import Any

from . import __version__
from .errors import AicxError, RpcError


class CodexAppServer:
    def __init__(self, binary: str, env: dict[str, str], timeout: float = 15.0) -> None:
        self.binary = binary
        self.env = env
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.reader: threading.Thread | None = None
        self.stderr_lines: deque[str] = deque(maxlen=20)
        self.stderr_reader: threading.Thread | None = None
        self.next_id = 1

    def __enter__(self) -> "CodexAppServer":
        self.process = subprocess.Popen(
            [self.binary, "app-server", "--stdio"],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise AicxError("Failed to open Codex app-server pipes")
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()
        try:
            self._send(
                {
                    "method": "initialize",
                    "id": 0,
                    "params": {
                        "clientInfo": {
                            "name": "aicx",
                            "title": "aicx",
                            "version": __version__,
                        }
                    },
                }
            )
            self._wait_for(0)
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
            if self.process.stderr is not None:
                self.process.stderr.close()
        if self.reader is not None:
            self.reader.join(timeout=1)
        if self.stderr_reader is not None:
            self.stderr_reader.join(timeout=1)

    def _read_stdout(self) -> None:
        if self.process is None or self.process.stdout is None:
            self.messages.put(None)
            return
        try:
            for line in self.process.stdout:
                self.messages.put(line)
        finally:
            self.messages.put(None)

    def _read_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        for line in self.process.stderr:
            stripped = line.strip()
            if stripped:
                self.stderr_lines.append(stripped)

    def _exit_detail(self) -> str:
        if self.stderr_reader is not None:
            self.stderr_reader.join(timeout=0.1)
        if not self.stderr_lines:
            return ""
        return f": {self.stderr_lines[-1]}"

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self._send(message)
        return self._wait_for(request_id)

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise AicxError("Codex app-server is not running")
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except BrokenPipeError as exc:
            raise AicxError("Codex app-server closed unexpectedly") from exc

    def _wait_for(self, request_id: int) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise AicxError("Codex app-server is not running")
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AicxError("Timed out waiting for Codex app-server")
            try:
                line = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise AicxError("Timed out waiting for Codex app-server")
            if line is None:
                code = self.process.poll()
                raise AicxError(
                    f"Codex app-server exited unexpectedly (code {code}){self._exit_detail()}"
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                raise RpcError(detail)
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise RpcError(f"Unexpected response for {request_id}")
            return result
