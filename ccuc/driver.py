"""ClaudeDriver — drives the Claude Code CLI as a backend.

StreamingDriver keeps ONE long-lived `claude -p --input-format stream-json
--output-format stream-json` process per active session. User turns are written
to stdin as NDJSON; stdout events are parsed by a continuous background reader
and forwarded as WS frames via an on_frame callback. A turn ends at the `result`
event. Ported/generalized from smart-gateway-extractor's pipeline_spawner.py
(_build_claude_command, _spawn_local, _monitor_local).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import signal
import subprocess
from typing import Awaitable, Callable, Optional

from ccuc.stream import translate

logger = logging.getLogger(__name__)

OnFrame = Callable[[dict], Awaitable[None]]


def _make_preexec(settings):
    """Child preexec: new session/process group (so we can group-kill on stop),
    plus optional resource limits. Default limits are off (agentic workloads)."""
    def _pre():
        os.setsid()
        if settings.rlimit_nproc:
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (settings.rlimit_nproc, settings.rlimit_nproc))
            except Exception:  # noqa: BLE001
                pass
        if settings.rlimit_cpu_s:
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (settings.rlimit_cpu_s, settings.rlimit_cpu_s))
            except Exception:  # noqa: BLE001
                pass
    return _pre


def _collect_descendants(pid: int) -> list[int]:
    """All ppid-linked descendants of pid (best-effort, via ps). Collect BEFORE
    killing so the tree is still intact. Fully-daemonized/reparented children
    (nohup/setsid → ppid=1) can't be tracked this way — that needs a container."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return []
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                p, pp = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            kids.setdefault(pp, []).append(p)
    seen: set[int] = set()
    stack, res = [pid], []
    while stack:
        for ch in kids.get(stack.pop(), []):
            if ch not in seen:
                seen.add(ch)
                res.append(ch)
                stack.append(ch)
    return res


def _signal_group(pgid: Optional[int], sig: int) -> None:
    if pgid is None:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _signal_pids(pids: list[int], sig: int) -> None:
    for p in pids:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


class StreamingDriver:
    def __init__(self, settings, session, seat):
        self.settings = settings
        self.session = session
        self.seat = seat
        self.process: Optional[asyncio.subprocess.Process] = None
        self.native_session_id: Optional[str] = None
        self.tools: list = []
        self.mcp_servers: list = []
        self.skills: list = []
        self.model: str = session.model
        self._on_frame: Optional[OnFrame] = None
        self._ready = asyncio.Event()
        self._turn_done = asyncio.Event()
        self._last_result: Optional[dict] = None
        self.last_rate_limit: Optional[dict] = None
        self._req_counter = 0
        self._alive = False
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

    # ---- command construction ----

    def build_cmd(self, *, resume: bool) -> list[str]:
        s = self.settings
        sess = self.session
        cmd = [
            s.claude_binary,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--model", sess.model,
            "--add-dir", str(sess.work_dir),
        ]
        if s.include_partial_messages:
            cmd.append("--include-partial-messages")
        # chat mode = pure completion: disable ALL built-in tools ("" is meaningful here)
        # and load no MCP servers, so the model can answer but cannot touch the filesystem.
        # agent mode = full-agentic: MCP + tool allow/deny policy as configured.
        if sess.mode == "chat":
            cmd += ["--tools", ""]
        else:
            if sess.mcp_config_path:
                cmd += ["--mcp-config", str(sess.mcp_config_path), "--strict-mcp-config"]
            if sess.allowed_tools:
                cmd += ["--allowedTools", ",".join(sess.allowed_tools)]
            if sess.disallowed_tools:
                cmd += ["--disallowedTools", ",".join(sess.disallowed_tools)]
        if sess.append_system_prompt:
            cmd += ["--append-system-prompt", sess.append_system_prompt]
        cmd += (["--resume", sess.session_id] if resume else ["--session-id", sess.session_id])
        return cmd

    # ---- lifecycle ----

    async def start(self, on_frame: OnFrame, *, resume: bool = False) -> None:
        self._on_frame = on_frame
        cmd = self.build_cmd(resume=resume)
        env = self.seat.child_env(self.settings)
        logger.info(
            "spawn claude | session=%s resume=%s seat=%s model=%s",
            self.session.session_id, resume, self.seat.seat_id, self.session.model,
        )
        # Default: start_new_session=True (own process group for group-kill) — the safe
        # path, no Python callback in the forked child. Only use a preexec_fn when
        # rlimits are configured (opt-in; a fork callback in asyncio is deadlock-prone).
        spawn_kwargs: dict = {}
        if self.settings.rlimit_nproc or self.settings.rlimit_cpu_s:
            spawn_kwargs["preexec_fn"] = _make_preexec(self.settings)
        else:
            spawn_kwargs["start_new_session"] = True
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.session.work_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=1 << 20,
            **spawn_kwargs,
        )
        self._alive = True
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    @property
    def is_alive(self) -> bool:
        return self._alive and self.process is not None and self.process.returncode is None

    async def send_turn(self, text: str, timeout: Optional[float] = None) -> Optional[dict]:
        """Write one user turn to stdin and await its `result` event.

        Returns the result object, or None on timeout / dead process.
        """
        if not self.is_alive:
            raise RuntimeError("claude process is not alive")
        self._turn_done.clear()
        self._last_result = None
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.process.stdin.write((json.dumps(msg) + "\n").encode())
        await self.process.stdin.drain()
        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout=timeout or self.settings.turn_timeout_s)
        except asyncio.TimeoutError:
            logger.warning("turn timeout | session=%s", self.session.session_id)
            return None
        return self._last_result

    async def interrupt(self) -> None:
        """Cancel the in-flight turn without killing the process.

        Sends a stream-json control_request; if the CLI doesn't end the turn
        promptly, synthesize an interrupted result so the turn always unblocks.
        """
        if not self.is_alive or not self.process or not self.process.stdin:
            return
        self._req_counter += 1
        msg = {
            "type": "control_request",
            "request_id": f"int-{self._req_counter}",
            "request": {"subtype": "interrupt"},
        }
        try:
            self.process.stdin.write((json.dumps(msg) + "\n").encode())
            await self.process.stdin.drain()
            logger.info("sent interrupt | session=%s", self.session.session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("interrupt send failed | session=%s: %s", self.session.session_id, e)
            return
        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout=8)
        except asyncio.TimeoutError:
            logger.warning("interrupt: no result in time, synthesizing | session=%s", self.session.session_id)
            self._last_result = {
                "type": "result", "subtype": "interrupted",
                "stop_reason": "interrupted", "is_error": False,
            }
            self._turn_done.set()

    async def stop(self, graceful: bool = True) -> None:
        """Kill `claude` AND everything it spawned: the process group plus every
        ppid-linked descendant (bash, node, tool subprocesses). Fully-daemonized
        escapees (nohup/setsid) need container isolation and are out of scope."""
        self._alive = False
        proc = self.process
        if proc is None:
            return
        pid = proc.pid
        descendants = _collect_descendants(pid)  # while the tree is still intact
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = None
        try:
            if graceful and proc.stdin and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            _signal_group(pgid, signal.SIGTERM)
            _signal_pids(descendants, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            _signal_group(pgid, signal.SIGKILL)
            _signal_pids([pid] + descendants, signal.SIGKILL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
        except ProcessLookupError:
            pass
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()

    # ---- stream reading ----

    async def _read_stdout(self) -> None:
        proc = self.process
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    raw = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as e:
                    logger.warning("oversized stdout line dropped | session=%s: %s", self.session.session_id, e)
                    try:
                        await proc.stdout.read(1 << 20)
                    except Exception:
                        pass
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line:
                    await self._handle_line(line)
        except Exception as e:  # noqa: BLE001
            logger.warning("stdout reader error | session=%s: %s", self.session.session_id, e)
        finally:
            self._alive = False
            self._ready.set()
            self._turn_done.set()  # unblock any awaiting turn

    async def _handle_line(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        t = obj.get("type")

        if t == "system":
            if obj.get("subtype") == "init":
                self.native_session_id = obj.get("session_id")
                self.tools = obj.get("tools", [])
                self.mcp_servers = [
                    (s.get("name") if isinstance(s, dict) else s)
                    for s in obj.get("mcp_servers", [])
                ]
                self.skills = obj.get("skills", [])
                if obj.get("model"):
                    self.model = obj["model"]
                logger.info(
                    "init | session=%s tools=%d mcp=%s skills=%s",
                    self.session.session_id, len(self.tools), self.mcp_servers, self.skills,
                )
                self._ready.set()
            return

        if t == "result":
            self._last_result = obj
            self._turn_done.set()
            return

        if t == "rate_limit_event":
            self.last_rate_limit = obj.get("rate_limit_info")
            return

        for frame in translate(self.session.session_id, obj, self.settings.include_partial_messages):
            if self._on_frame:
                await self._on_frame(frame)

    async def _read_stderr(self) -> None:
        proc = self.process
        assert proc is not None and proc.stderr is not None
        try:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line:
                    logger.info("[claude stderr session=%s] %s", self.session.session_id, line[:500])
        except Exception:
            pass
