from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Deque

from .artifacts import MiniOSArtifacts
from .resources import DEFAULT_PROFILE, ResourceProfile


class SessionState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    RESIZING = "resizing"
    EXITED = "exited"
    FAILED = "failed"
    CLOSING = "closing"


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    name: str | None
    state: SessionState
    created_at: datetime
    disk_path: Path
    pid: int | None
    node_id: str | None = None
    profile: str = DEFAULT_PROFILE.name
    memory_mb: int = DEFAULT_PROFILE.memory_mb
    disk_mb: int = DEFAULT_PROFILE.disk_mb


@dataclass
class MiniOSSession:
    session_id: str
    name: str | None
    artifacts: MiniOSArtifacts
    qemu_bin: str
    disk_path: Path
    profile: ResourceProfile = DEFAULT_PROFILE
    create_disk: bool = True
    replay_bytes: int = 8192
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    state: SessionState = SessionState.STARTING
    process: asyncio.subprocess.Process | None = None
    _subscribers: set[asyncio.Queue[bytes | None]] = field(default_factory=set)
    _replay: Deque[bytes] = field(default_factory=deque)
    _replay_size: int = 0
    _pump_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.create_disk:
            await self.artifacts.create_disk(self.disk_path, disk_bytes=self.profile.disk_bytes)
        self.process = await asyncio.create_subprocess_exec(
            self.qemu_bin,
            "-M",
            "virt",
            "-cpu",
            "cortex-a57",
            "-m",
            self.profile.memory_arg,
            "-smp",
            "1",
            "-nodefaults",
            "-nographic",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-kernel",
            str(self.artifacts.kernel),
            "-drive",
            f"if=none,file={self.disk_path},format=raw,id=hd0",
            "-device",
            "virtio-blk-device,drive=hd0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.state = SessionState.RUNNING
        self._pump_task = asyncio.create_task(self._pump_output())

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            name=self.name,
            state=self.state,
            created_at=self.created_at,
            disk_path=self.disk_path,
            pid=self.process.pid if self.process else None,
            profile=self.profile.name,
            memory_mb=self.profile.memory_mb,
            disk_mb=self.profile.disk_mb,
        )

    def subscribe(self, *, replay: bool = True) -> asyncio.Queue[bytes | None]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        if replay:
            for chunk in self._replay:
                queue.put_nowait(chunk)
        if self.state in {SessionState.EXITED, SessionState.FAILED}:
            queue.put_nowait(None)
            return queue
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        self._subscribers.discard(queue)

    async def write(self, data: bytes) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("session process is not writable")
        if self.state != SessionState.RUNNING:
            raise RuntimeError(f"session is not running: {self.state.value}")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def close(self) -> None:
        if self.process is None:
            self.state = SessionState.EXITED
            return
        if self.state == SessionState.RUNNING:
            try:
                await self.write(b"exit\n")
            except RuntimeError:
                pass
            self.state = SessionState.CLOSING
        try:
            await asyncio.wait_for(self.process.wait(), timeout=2)
        except asyncio.TimeoutError:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._pump_task:
            await asyncio.gather(self._pump_task, return_exceptions=True)
        self.state = SessionState.EXITED

    async def _pump_output(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        try:
            while True:
                chunk = await self.process.stdout.read(4096)
                if not chunk:
                    break
                self._append_replay(chunk)
                dead: list[asyncio.Queue[bytes | None]] = []
                for queue in self._subscribers:
                    try:
                        queue.put_nowait(chunk)
                    except asyncio.QueueFull:
                        dead.append(queue)
                for queue in dead:
                    self._subscribers.discard(queue)
            rc = await self.process.wait()
            self.state = SessionState.EXITED if rc == 0 else SessionState.FAILED
        finally:
            for queue in list(self._subscribers):
                queue.put_nowait(None)
            self._subscribers.clear()

    def _append_replay(self, chunk: bytes) -> None:
        if self.replay_bytes <= 0:
            return
        self._replay.append(chunk)
        self._replay_size += len(chunk)
        while self._replay_size > self.replay_bytes and self._replay:
            old = self._replay.popleft()
            self._replay_size -= len(old)
