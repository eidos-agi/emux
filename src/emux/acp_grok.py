"""Grok Build ACP client (stdio JSON-RPC) for emux Phase C.

Spawns ``grok agent [--always-approve] stdio`` and speaks a minimal subset of
the Agent Client Protocol:

* ``initialize``
* ``authenticate`` (optional; skipped when agent reports no methods)
* ``session/new`` / ``session/load``
* ``session/prompt``
* ``session/cancel``

Agent → client reverse-RPC (permissions, fs stubs) is answered auto-approve
or method-not-found so a turn never hangs.

No third-party ACP SDK — pure NDJSON over pipes (mirrors grok-build
``RawStdioClient``). See ``docs/grok-control-plane.md``.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import grok_control as gc

PROTOCOL_VERSION = 1
CLIENT_NAME = "emux"
CLIENT_VERSION = "0.68.8"


class AcpError(RuntimeError):
    """Protocol or process failure."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass
class AcpUpdate:
    """One ``session/update`` notification (or similar) from the agent."""

    kind: str
    raw: dict[str, Any]
    text: str = ""
    session_id: str | None = None


@dataclass
class GrokAcpClient:
    """Long-lived (or one-shot) client over a spawned ``grok agent stdio``."""

    process: subprocess.Popen[str]
    argv: list[str]
    always_approve: bool = True
    _next_id: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    updates: list[AcpUpdate] = field(default_factory=list)
    text_chunks: list[str] = field(default_factory=list)
    closed: bool = False
    _stderr_tail: str = ""
    _line_q: queue.Queue[str | None] = field(default_factory=queue.Queue, repr=False)
    _reader: threading.Thread | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def spawn(
        cls,
        *,
        bin_path: str | None = None,
        model: str | None = None,
        always_approve: bool = True,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        leader: bool | None = False,
        extra: list[str] | None = None,
    ) -> GrokAcpClient:
        argv = gc.acp_stdio_argv(
            bin_path=bin_path,
            model=model,
            always_approve=always_approve,
            leader=leader,
            extra=extra,
        )
        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        home = Path.home()
        extra_path = str(home / ".grok" / "bin")
        run_env["PATH"] = extra_path + os.pathsep + (run_env.get("PATH") or "")

        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered text when possible
                cwd=cwd if cwd and Path(cwd).is_dir() else None,
                env=run_env,
            )
        except FileNotFoundError as exc:
            raise AcpError(f"grok_not_found:{exc}") from exc
        except OSError as exc:
            raise AcpError(f"spawn_failed:{exc}") from exc

        client = cls(process=proc, argv=argv, always_approve=always_approve)
        client._start_reader()
        return client

    def _start_reader(self) -> None:
        """Background readline — avoids select()+TextIO buffering deadlocks."""
        stdout = self.process.stdout
        if not stdout:
            return

        def _loop() -> None:
            try:
                while True:
                    line = stdout.readline()
                    if line == "":
                        self._line_q.put(None)
                        return
                    self._line_q.put(line)
            except Exception:  # noqa: BLE001
                self._line_q.put(None)

        self._reader = threading.Thread(target=_loop, name="acp-stdout", daemon=True)
        self._reader.start()

    def close(self, *, kill: bool = True, timeout: float = 3.0) -> None:
        if self.closed:
            return
        self.closed = True
        proc = self.process
        try:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if kill and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001 — best-effort teardown
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        self._drain_stderr()

    def __enter__(self) -> GrokAcpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Wire
    # ------------------------------------------------------------------

    def _alloc_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def _write(self, msg: dict[str, Any]) -> None:
        if self.closed or not self.process.stdin:
            raise AcpError("client_closed")
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AcpError(f"stdin_write_failed:{exc}") from exc

    def _read_line(self, deadline: float) -> str | None:
        """Read one stdout line or None on timeout / EOF."""
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                item = self._line_q.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self.process.poll() is not None and self._line_q.empty():
                    return None
                continue
            if item is None:
                return None
            return item

    def _handle_agent_request(self, msg: dict[str, Any]) -> None:
        """Answer reverse-RPC so the agent never blocks on us."""
        req_id = msg.get("id")
        method = (msg.get("method") or "").strip()
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        if method in (
            "session/request_permission",
            "requestPermission",
            "session/requestPermission",
        ):
            # Prefer AllowOnce option if present; else first option id.
            option_id = None
            options = params.get("options") if isinstance(params, dict) else None
            if isinstance(options, list):
                for opt in options:
                    if not isinstance(opt, dict):
                        continue
                    kind = (opt.get("kind") or "").lower()
                    oid = opt.get("optionId") or opt.get("option_id") or opt.get("id")
                    if kind in ("allowonce", "allow_once", "allow"):
                        option_id = oid
                        break
                if option_id is None and options:
                    first = options[0]
                    if isinstance(first, dict):
                        option_id = (
                            first.get("optionId")
                            or first.get("option_id")
                            or first.get("id")
                        )
            outcome: dict[str, Any]
            if option_id is not None and self.always_approve:
                outcome = {"outcome": "selected", "optionId": option_id}
            elif self.always_approve:
                # Some agents accept bare allow without optionId.
                outcome = {"outcome": "selected", "optionId": "allow-once"}
            else:
                outcome = {"outcome": "cancelled"}
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": outcome,
                }
            )
            return

        # Capability-less stubs: refuse with method-not-found (fail closed for fs).
        self._write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"unsupported by emux acp client: {method}",
                },
            }
        )

    def _ingest_notification(self, msg: dict[str, Any]) -> None:
        method = (msg.get("method") or "").strip()
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method in ("session/update", "session/updateNotification", "_x.ai/session/update"):
            update = params.get("update") if isinstance(params.get("update"), dict) else params
            kind = ""
            text = ""
            if isinstance(update, dict):
                kind = str(
                    update.get("sessionUpdate")
                    or update.get("session_update")
                    or update.get("type")
                    or ""
                )
                content = update.get("content")
                if isinstance(content, dict):
                    text = str(content.get("text") or "")
                elif isinstance(content, str):
                    text = content
                if kind in ("agent_message_chunk", "agent_message", "message") and text:
                    self.text_chunks.append(text)
            sid = None
            if isinstance(params, dict):
                raw_sid = params.get("sessionId") or params.get("session_id")
                if isinstance(raw_sid, str):
                    sid = raw_sid
                elif isinstance(raw_sid, dict):
                    sid = str(raw_sid.get("id") or raw_sid.get("sessionId") or "")
            self.updates.append(
                AcpUpdate(kind=kind or method, raw=msg, text=text, session_id=sid)
            )
        else:
            self.updates.append(
                AcpUpdate(kind=method or "notification", raw=msg, text="")
            )

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
    ) -> Any:
        """Send a JSON-RPC request and wait for the matching response."""
        if self.closed:
            raise AcpError("client_closed")
        if self.process.poll() is not None:
            self._drain_stderr()
            raise AcpError(
                f"agent_exited:{self.process.returncode}",
                data={"stderr": self._stderr_tail},
            )

        req_id = self._alloc_id()
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        self._write(msg)

        deadline = time.time() + max(1.0, float(timeout))
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self._drain_stderr()
                raise AcpError(
                    f"timeout waiting for {method}",
                    data={"stderr": self._stderr_tail, "updates": len(self.updates)},
                )
            line = self._read_line(deadline)
            if line is None:
                if self.process.poll() is not None:
                    self._drain_stderr()
                    raise AcpError(
                        f"agent_exited_during_{method}:{self.process.returncode}",
                        data={"stderr": self._stderr_tail},
                    )
                continue
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue

            # Agent → client request (has method + id)
            if "method" in parsed and "id" in parsed and "result" not in parsed and "error" not in parsed:
                self._handle_agent_request(parsed)
                continue

            # Notification (method, no id)
            if "method" in parsed and "id" not in parsed:
                self._ingest_notification(parsed)
                continue

            # Response for our id (integer or string form)
            rid = parsed.get("id")
            if rid != req_id and str(rid) != str(req_id):
                # Unrelated — keep going (shouldn't happen often)
                continue
            if "error" in parsed and parsed["error"] is not None:
                err = parsed["error"]
                if isinstance(err, dict):
                    raise AcpError(
                        str(err.get("message") or err),
                        code=err.get("code") if isinstance(err.get("code"), int) else None,
                        data=err.get("data"),
                    )
                raise AcpError(str(err))
            return parsed.get("result")

    def _drain_stderr(self) -> None:
        err = self.process.stderr
        if not err:
            return
        try:
            # Non-blocking-ish: set blocking false if available
            try:
                import fcntl

                fd = err.fileno()
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            except Exception:  # noqa: BLE001
                pass
            chunk = err.read() or ""
            if chunk:
                self._stderr_tail = (self._stderr_tail + chunk)[-4000:]
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def initialize(
        self,
        *,
        timeout: float = 30.0,
        client_type: str = "emux",
    ) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {},
                    "terminal": False,
                },
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "version": CLIENT_VERSION,
                },
                "_meta": {
                    "startupHints": {
                        "nonInteractive": True,
                        "skipGitStatus": True,
                        "skipProjectLayout": True,
                    },
                    "clientType": client_type,
                    "clientVersion": CLIENT_VERSION,
                },
            },
            timeout=timeout,
        )
        if not isinstance(result, dict):
            return {}
        return result

    def authenticate_if_needed(
        self,
        init_result: dict[str, Any],
        *,
        timeout: float = 30.0,
    ) -> bool:
        """Authenticate using xai.api_key when the agent lists it. Returns True if called."""
        methods = init_result.get("authMethods") or init_result.get("auth_methods") or []
        if not isinstance(methods, list) or not methods:
            return False
        method_id = None
        for m in methods:
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            if isinstance(mid, dict):
                mid = mid.get("id") or mid.get("0")
            mid_s = str(mid or "")
            if mid_s in ("xai.api_key", "api_key", "xai-api-key"):
                method_id = mid_s
                break
        if method_id is None:
            # Take first method id if any
            first = methods[0]
            if isinstance(first, dict):
                mid = first.get("id")
                if isinstance(mid, dict):
                    mid = mid.get("id")
                method_id = str(mid) if mid else None
        if not method_id:
            return False
        self.request(
            "authenticate",
            {
                "methodId": method_id,
                "_meta": {"headless": True},
            },
            timeout=timeout,
        )
        return True

    def session_new(
        self,
        cwd: str,
        *,
        model: str | None = None,
        yolo: bool = True,
        timeout: float = 30.0,
    ) -> str:
        params: dict[str, Any] = {
            "cwd": cwd,
            "mcpServers": [],
        }
        meta: dict[str, Any] = {}
        if yolo:
            meta["yoloMode"] = True
        if model:
            meta["modelId"] = model
        if meta:
            params["_meta"] = meta
        result = self.request("session/new", params, timeout=timeout)
        return _extract_session_id(result)

    def session_load(
        self,
        session_id: str,
        cwd: str,
        *,
        timeout: float = 60.0,
    ) -> str:
        result = self.request(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": cwd,
                "mcpServers": [],
            },
            timeout=timeout,
        )
        # load may echo session id or empty
        sid = _extract_session_id(result) if result else ""
        return sid or session_id

    def session_prompt(
        self,
        session_id: str,
        text: str,
        *,
        timeout: float = 120.0,
    ) -> Any:
        # Clear per-turn text buffer but keep updates history for callers
        self.text_chunks.clear()
        return self.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
            timeout=timeout,
        )

    def session_cancel(self, session_id: str, *, timeout: float = 10.0) -> Any:
        try:
            return self.request(
                "session/cancel",
                {"sessionId": session_id},
                timeout=timeout,
            )
        except AcpError:
            # cancel may be notification-only on some agents
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                }
            )
            return None

    def captured_text(self) -> str:
        return "".join(self.text_chunks)


def _extract_session_id(result: Any) -> str:
    if isinstance(result, str) and result.strip():
        return result.strip()
    if not isinstance(result, dict):
        return ""
    for key in ("sessionId", "session_id", "id"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            inner = val.get("id") or val.get("sessionId")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def run_acp_prompt(
    prompt: str,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    bin_path: str | None = None,
    model: str | None = None,
    timeout: float = 120.0,
    always_approve: bool = True,
    env: dict[str, str] | None = None,
    spawn_fn: Callable[..., GrokAcpClient] | None = None,
) -> dict[str, Any]:
    """One-shot ACP turn: spawn → initialize → load|new → prompt → close.

    Returns a steer-shaped dict (``ok``, ``mode=acp``, ``text``, …).
    """
    text = (prompt or "").strip()
    if not text:
        return {"ok": False, "error": "prompt required", "mode": "acp"}

    work_cwd = (cwd or "").strip() or os.getcwd()
    t0 = time.time()
    spawner = spawn_fn or GrokAcpClient.spawn
    client: GrokAcpClient | None = None
    try:
        client = spawner(
            bin_path=bin_path,
            model=model,
            always_approve=always_approve,
            cwd=work_cwd if Path(work_cwd).is_dir() else None,
            env=env,
            leader=False,
        )
    except AcpError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "mode": "acp",
            "hint": "set EMUX_GROK_BIN to absolute path of grok",
        }

    assert client is not None
    sid = (session_id or "").strip() or None
    try:
        init = client.initialize(timeout=min(30.0, timeout))
        try:
            client.authenticate_if_needed(init, timeout=min(30.0, timeout))
        except AcpError:
            # Headless hosts may already be logged in; continue if session ops work.
            pass

        if sid:
            try:
                sid = client.session_load(sid, work_cwd, timeout=min(60.0, timeout))
            except AcpError as exc:
                # Fall back to new session if load fails
                if "not found" in str(exc).lower() or "unknown" in str(exc).lower():
                    sid = client.session_new(
                        work_cwd, model=model, yolo=always_approve, timeout=min(30.0, timeout)
                    )
                else:
                    raise
        else:
            sid = client.session_new(
                work_cwd, model=model, yolo=always_approve, timeout=min(30.0, timeout)
            )

        remaining = max(10.0, timeout - (time.time() - t0))
        prompt_result = client.session_prompt(sid, text, timeout=remaining)
        out_text = client.captured_text()
        if not out_text and isinstance(prompt_result, dict):
            for k in ("text", "result", "message", "stopReason", "stop_reason"):
                v = prompt_result.get(k)
                if isinstance(v, str) and v.strip():
                    out_text = v.strip()
                    break

        return {
            "ok": True,
            "mode": "acp",
            "text": (out_text or "")[:8000],
            "session_id": sid,
            "cwd": work_cwd,
            "prompt_result": prompt_result if isinstance(prompt_result, (dict, list, str)) else None,
            "update_count": len(client.updates),
            "argv": client.argv,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "error": None,
        }
    except AcpError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "mode": "acp",
            "session_id": sid,
            "cwd": work_cwd,
            "argv": client.argv,
            "stderr": client._stderr_tail[:2000],
            "update_count": len(client.updates),
            "partial_text": client.captured_text()[:4000],
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"acp_unexpected:{exc}",
            "mode": "acp",
            "session_id": sid,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    finally:
        if client is not None:
            client.close()
