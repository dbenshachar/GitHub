"""Stateless Mini OS jobs. Kubernetes owns placement, deadlines and cleanup."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
import json
import math
from pathlib import Path
import re
import sys
import time
from uuid import uuid4


class FunctionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FunctionSpec:
    script: str
    scripts: dict[str, str] = field(default_factory=dict)
    memory_mb: int = 128
    cpus: float = 1
    timeout_s: int = 600
    benchmark_warm_runs: int = 0

    def __post_init__(self):
        if type(self.timeout_s) is not int or not 1 <= self.timeout_s <= 600:
            raise ValueError("jobs require an integer deadline between 1 and 600 seconds")
        if type(self.memory_mb) is not int or not 32 <= self.memory_mb <= 4096:
            raise ValueError("guest memory must be between 32 and 4096 MiB")
        if not math.isfinite(self.cpus) or not 0 < self.cpus <= 64:
            raise ValueError("invalid CPU limit")
        if type(self.benchmark_warm_runs) is not int or not 0 <= self.benchmark_warm_runs <= 100:
            raise ValueError("warm probes must be between 0 and 100")
        if not isinstance(self.scripts, dict) or len(self.scripts) > 12:
            raise ValueError("at most 12 child script files are supported")
        for name in self.scripts:
            if not re.fullmatch(r"[A-Z0-9_-]{1,8}\.MOS", name) or name == "RUN.MOS":
                raise ValueError("child scripts need uppercase FAT 8.3 names other than RUN.MOS")
        for source in [self.script, *self.scripts.values()]:
            if not isinstance(source, str) or "\0" in source or len(source.encode()) > 60000:
                raise ValueError("script must be NUL-free text of at most 60000 bytes")
        if len(json.dumps(asdict(self)).encode()) > 90000:
            raise ValueError("script bundle exceeds 90000 bytes")

    def to_dict(self):
        return asdict(self)

    def child(self, name):
        if name not in self.scripts:
            raise ValueError("unknown child script")
        return replace(self, script=self.scripts[name], benchmark_warm_runs=0)


@dataclass
class InvocationResult:
    invocation_id: str
    output: str
    metrics: dict
    children: list["InvocationResult"] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class QemuScheduler:
    """Local development transport; production execution uses Kubernetes Jobs."""
    def __init__(self, mini_os_root: Path, *, concurrency=16, max_fanout=256, max_depth=8):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("concurrency must be positive")
        self.mini_os_root = Path(mini_os_root).resolve()
        self.max_fanout, self.max_depth = max_fanout, max_depth
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks = set()
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def invoke(self, spec, *, depth=0):
        if self._closed:
            raise FunctionError("scheduler is closed")
        task = asyncio.create_task(self._invoke(spec, depth))
        self._tasks.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self._tasks.discard(task)

    async def _invoke(self, spec, depth):
        proc = None
        children = []
        started = time.perf_counter()
        invocation_id = "local-" + uuid4().hex
        try:
            async with asyncio.timeout(spec.timeout_s):
                async with self._slots:
                    queued = (time.perf_counter() - started) * 1000
                    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                        sys.executable, "-u", "-m", "remote_desktop.function_worker",
                        "--mini-os-root", str(self.mini_os_root), "--protocol",
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, limit=2 * 1024 * 1024,
                        start_new_session=True,
                    ))
                    try:
                        proc = await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        # Cancellation during process creation must still reap the process.
                        proc = await spawning
                        raise
                    proc.stdin.write((json.dumps(spec.to_dict()) + "\n").encode())
                    await proc.stdin.drain()
                    # Drain stderr while the worker runs so QEMU diagnostics cannot block it.
                    errors = asyncio.create_task(proc.stderr.read())
                    result = None
                    try:
                        while line := await proc.stdout.readline():
                            message = json.loads(line)
                            if message["type"] == "fanout":
                                count, name = message["count"], message["script"]
                                try:
                                    if type(count) is not int or not 1 <= count <= self.max_fanout or depth >= self.max_depth:
                                        raise ValueError("fanout limit exceeded")
                                    child_spec = spec.child(name)
                                    children.extend(asyncio.create_task(self.invoke(child_spec, depth=depth + 1)) for _ in range(count))
                                    reply = {"ok": True, "job_id": invocation_id + "-fanout-" + str(len(children)), "count": count}
                                except ValueError as exc:
                                    reply = {"ok": False, "error": str(exc)}
                                proc.stdin.write((json.dumps(reply) + "\n").encode())
                                await proc.stdin.drain()
                            elif message["type"] == "error":
                                raise FunctionError(message["error"])
                            elif message["type"] == "result":
                                result = InvocationResult(invocation_id, message["output"], message["metrics"])
                        await proc.wait()
                        stderr = await errors
                        if proc.returncode or result is None:
                            raise FunctionError("worker failed: " + stderr.decode(errors="replace")[-4096:])
                    finally:
                        if not errors.done():
                            errors.cancel()
                        await asyncio.gather(errors, return_exceptions=True)
                # The parent VM has exited and released its slot before waiting for children.
                result.children = await asyncio.gather(*children)
                result.metrics.update(queue_ms=queued, total_ms=(time.perf_counter() - started) * 1000)
                return result
        except TimeoutError as exc:
            raise FunctionError("job exceeded its deadline") from exc
        finally:
            if proc is not None and proc.returncode is None:
                import os
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
