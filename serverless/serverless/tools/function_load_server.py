"""HTTP benchmark ingress for local QEMU or the multi-host Kubernetes router."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import signal
import time

from remote_desktop.functions import FunctionSpec, QemuScheduler
from remote_desktop.function_client import RouterClient


class LoadServer:
    def __init__(self, scheduler, spec, *, slots=16, max_pending=4096, backend="qemu-local"):
        self.scheduler, self.spec = scheduler, spec
        self.slots = asyncio.Semaphore(slots)
        self.max_pending = max_pending
        self.pending = self.active = 0
        self.peak_pending = self.peak_active = 0
        self.completed = self.failed = self.rejected = self.cancelled = 0
        self.handlers = set()
        self.backend = backend
        self.worker_nodes = Counter()

    def stats(self):
        return {**{key: getattr(self, key) for key in (
            "pending", "active", "peak_pending", "peak_active", "completed", "failed", "rejected", "cancelled")},
            "backend": self.backend, "worker_nodes": dict(self.worker_nodes),
            "job_timeout_s": self.spec.timeout_s,
            "queue_scope": ("frontend and local scheduler" if self.backend == "qemu-local" else
                "frontend and worker execution queue" if self.backend == "worker-pool" else
                "frontend slots only; Kubernetes placement delay is included in total latency"),
            "active_scope": "admitted invocations; router mode includes Kubernetes pending jobs"}

    async def reply(self, writer, status, value):
        body = json.dumps(value).encode()
        writer.write(f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            async with asyncio.timeout(10):
                header = await reader.readuntil(b"\r\n\r\n")
            request = header.split(b"\r\n", 1)[0].split()
            if len(request) != 3:
                return await self.reply(writer, 400, {"error": "invalid request"})
            method, path, _ = request
            if method == b"GET" and path in (b"/healthz", b"/metrics"):
                return await self.reply(writer, 200, self.stats())
            if method != b"POST" or path != b"/invoke":
                return await self.reply(writer, 404, {"error": "use POST /invoke"})
            if self.pending >= self.max_pending:
                self.rejected += 1
                return await self.reply(writer, 503, {"error": "admission limit reached"})
            self.pending += 1
            self.peak_pending = max(self.peak_pending, self.pending)
            start = time.perf_counter()
            execution = disconnect = None

            async def execute():
                async with self.slots:
                    queue_ms = (time.perf_counter() - start) * 1000
                    self.active += 1
                    self.peak_active = max(self.peak_active, self.active)
                    try:
                        return await self.scheduler.invoke(self.spec), queue_ms
                    finally:
                        self.active -= 1

            try:
                async with asyncio.timeout(self.spec.timeout_s):
                    execution = asyncio.create_task(execute())
                    # Benchmark POSTs have no body. EOF means the client stopped waiting.
                    disconnect = asyncio.create_task(reader.read(1))
                    done, _ = await asyncio.wait((execution, disconnect), return_when=asyncio.FIRST_COMPLETED)
                    if execution not in done:
                        self.cancelled += 1
                        return
                    result, queue_ms = await execution
                self.completed += 1
                metrics = dict(result.metrics)
                if metrics.get("backend") == "worker-pool":
                    self.backend = "worker-pool"
                if metrics.get("worker_node"):
                    self.worker_nodes[metrics["worker_node"]] += 1
                metrics["queue_ms"] = queue_ms + metrics.get("queue_ms", 0)
                metrics["server_total_ms"] = (time.perf_counter() - start) * 1000
                await self.reply(writer, 200, {"metrics": metrics})
            except Exception as exc:
                self.failed += 1
                await self.reply(writer, 504 if isinstance(exc, TimeoutError) else 500,
                                 {"error": str(exc) or "job deadline exceeded"})
            finally:
                children = [t for t in (execution, disconnect) if t is not None]
                for child in children:
                    if not child.done():
                        child.cancel()
                # RouterClient cancellation requests deletion of its Kubernetes job.
                await asyncio.gather(*children, return_exceptions=True)
                self.pending -= 1
        except (ConnectionError, TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.handlers.discard(task)

    async def close(self):
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve(args):
    spec = FunctionSpec(args.script.read_text(), memory_mb=args.memory, cpus=args.cpus,
                        timeout_s=args.job_timeout)
    if not args.router and not (args.mini_os_root / "job-runtime-v1").is_file():
        raise ValueError("--mini-os-root must point to a built Mini OS runtime")
    if args.router and not os.environ.get(args.token_env):
        raise ValueError(f"set {args.token_env} to the Kubernetes router token")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    async with AsyncExitStack() as stack:
        if args.router:
            scheduler = RouterClient(args.router, os.environ[args.token_env], poll_s=args.poll_interval,
                                     concurrency=args.router_concurrency)
        else:
            scheduler = await stack.enter_async_context(QemuScheduler(args.mini_os_root, concurrency=args.slots))
        app = LoadServer(scheduler, spec, slots=args.slots, max_pending=args.max_pending,
                         backend="kubernetes-router" if args.router else "qemu-local")
        server = await asyncio.start_server(app.handle, args.host, args.port, backlog=4096)
        print(json.dumps({"listening": [str(sock.getsockname()) for sock in server.sockets],
                          "slots": args.slots, "max_pending": args.max_pending,
                          "backend": app.backend, "router": args.router,
                          "script": str(args.script), "guest_memory_mb": args.memory}), flush=True)
        try:
            async with server:
                await stopped.wait()
        finally:
            await app.close()
            print(json.dumps({"final_metrics": app.stats()}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--mini-os-root", type=Path)
    backend.add_argument("--router", help="URL of the deployed Kubernetes function router")
    parser.add_argument("--token-env", default="FUNCTION_TOKEN")
    parser.add_argument("--poll-interval", type=float, default=.5)
    parser.add_argument("--router-concurrency", type=int, default=8,
                        help="maximum simultaneous submission/status/deletion HTTP requests to the router")
    parser.add_argument("--script", type=Path, default=Path("examples/HELLO.MOS"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--slots", type=int, default=16,
                        help="local execution slots or maximum outstanding Kubernetes invocations")
    parser.add_argument("--max-pending", type=int, default=4096)
    parser.add_argument("--memory", type=int, default=128)
    parser.add_argument("--cpus", type=float, default=1, help="CPU request/limit per Kubernetes guest")
    parser.add_argument("--job-timeout", type=int, default=30)
    args = parser.parse_args()
    if args.slots < 1 or args.max_pending < args.slots or args.router_concurrency < 1:
        parser.error("require 1 <= slots <= max-pending")
    if not 0 < args.poll_interval <= 10:
        parser.error("--poll-interval must be between 0 and 10 seconds")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
