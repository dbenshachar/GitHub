"""Ready worker servers; each function owns a fresh disk and QEMU process."""
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import signal
import threading
import time
from uuid import uuid4

from .function_rpc import MAX_BODY, RPCError, Server, request
from .function_worker import execute
from .function_resources import Resources
from .functions import FunctionSpec, InvocationResult


@dataclass
class Record:
    job_id: str
    spec: FunctionSpec
    depth: int
    count: int
    stop: threading.Event = field(default_factory=threading.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    children: list = field(default_factory=list)
    task: object = None
    value: dict | None = None
    finished: float = 0
    size: int = 0
    memory_reserved: int = 0


class Worker:
    def __init__(self, root, token, router_url, *, name="worker", node="local",
                 memory_budget_mb=1024, cpu_budget=1.5,
                 result_ttl=60, max_records=4096, result_budget=16 * 1024 * 1024,
                 runner=execute, resources=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", name):
            raise ValueError("worker name must be a DNS label")
        if memory_budget_mb < 40 or cpu_budget <= 0:
            raise ValueError("invalid worker capacity")
        self.root, self.token, self.router_url = Path(root), token, router_url
        self.name, self.node = name, node
        self.memory_budget, self.cpu_budget = memory_budget_mb, cpu_budget
        self.result_ttl, self.max_records, self.result_budget = result_ttl, max_records, result_budget
        self.records = {}
        self.active = self.memory_used = self.cpu_used = self.result_bytes = 0
        self.completed = self.failed = 0
        self.draining = False
        self.ready = False
        self.runner = runner
        self.resources = resources or Resources(cpu_budget, memory_budget_mb)
        self.launches = []
        self.rejected = 0

    def execute_thread(self, call):
        """One thread per admitted guest; no fixed-size executor queue."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def finish(value, error):
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(value)

        def run():
            try:
                value, error = call(), None
            except BaseException as exc:
                value, error = None, exc
            loop.call_soon_threadsafe(finish, value, error)

        threading.Thread(target=run, name='mini-qemu', daemon=True).start()
        return future

    def release(self, record):
        if record.memory_reserved:
            self.active -= 1
            self.memory_used -= record.memory_reserved
            self.cpu_used -= record.spec.cpus
            record.memory_reserved = 0

    def headroom(self):
        value = dict(self.resources.snapshot())
        now = time.monotonic()
        # Account for booting guests before the next CPU sample sees them.
        self.launches = [(until, cpu, memory) for until, cpu, memory in self.launches if until > now]
        value['cpu_available_cores'] = max(0, value['cpu_available_cores'] - sum(cpu for _, cpu, _ in self.launches))
        value['memory_available_mb'] = max(0, min(value['memory_available_mb'] - sum(m for _, _, m in self.launches),
                                                   self.memory_budget - self.memory_used))
        return value

    def prune(self):
        now = time.monotonic()
        for key, record in list(self.records.items()):
            if record.done.is_set() and now - record.finished >= self.result_ttl:
                self.result_bytes -= record.size
                del self.records[key]

    def stats(self):
        return {"ready": self.ready and not self.draining, "worker": self.name, "node": self.node,
                "admission": "measured-resources", "active": self.active,
                "pending": sum(not r.done.is_set() for r in self.records.values()),
                "memory_used_mb": self.memory_used, "memory_budget_mb": self.memory_budget,
                "cpu_reserved": self.cpu_used, "cpu_budget": self.cpu_budget,
                "completed": self.completed, "failed": self.failed, "rejected": self.rejected,
                "result_bytes": self.result_bytes, **self.headroom()}

    async def prewarm(self, runs=1):
        # Warm host code/page caches and validate the runtime; discard every preflight guest.
        for _ in range(runs):
            await self.execute_thread(lambda: self.runner(
                FunctionSpec('print("ready");', memory_mb=8, timeout_s=30), self.root,
                lambda *args: (_ for _ in ()).throw(ValueError("preflight has no fanout"))))
        self.ready = True

    async def children(self, record, spec, count):
        if not 1 <= count <= 256 or record.depth >= 8:
            raise ValueError("fanout limit exceeded")
        # Submit one batch coordinator. It distributes independent child invocations.
        reply = await request(self.router_url, self.token, "POST", "/v1/jobs",
                              {"spec": spec.to_dict(), "count": count, "depth": record.depth + 1})
        record.children.append(reply["job_id"])
        return reply

    async def wait_child(self, job_id, timeout):
        value = await request(self.router_url, self.token, "GET", f"/v1/jobs/{job_id}/wait", timeout=timeout)
        if value["phase"] != "complete":
            raise RuntimeError(value.get("error", "child invocation failed"))
        return value["result"]

    async def cancel_children(self, record):
        await asyncio.gather(*(request(self.router_url, self.token, "DELETE", f"/v1/jobs/{key}")
                               for key in record.children), return_exceptions=True)

    async def run(self, record):
        start = time.perf_counter()
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(record.spec.timeout_s):
                if record.count > 1:
                    # Coordinators hold no execution slot, so one-slot workers can fan out.
                    for _ in range(record.count):
                        reply = await request(self.router_url, self.token, "POST", "/v1/jobs",
                                              {"spec": record.spec.to_dict(), "depth": record.depth})
                        record.children.append(reply["job_id"])
                    result = {"invocation_id": record.job_id, "output": "", "metrics": {
                        "submit_ms": (time.perf_counter() - start) * 1000}, "children": []}
                else:
                    def submit(count, name):
                        future = asyncio.run_coroutine_threadsafe(
                            self.children(record, record.spec.child(name), count), loop)
                        return future.result(timeout=record.spec.timeout_s)

                    execution = self.execute_thread(lambda: self.runner(
                        record.spec, self.root, submit, stop_event=record.stop))
                    try:
                        await asyncio.wait((execution,))
                        value = execution.result()
                    finally:
                        record.stop.set()
                        # Don't release capacity until the QEMU process has actually exited.
                        await asyncio.wait((execution,))
                        if not execution.cancelled():
                            execution.exception()
                        self.release(record)
                    metrics = value["metrics"]
                    metrics.update(queue_ms=0, worker_node=self.node,
                                   worker_server=self.name, backend="worker-pool")
                    result = InvocationResult(record.job_id, value["output"], metrics).to_dict()
                values = await asyncio.gather(*(self.wait_child(key, record.spec.timeout_s)
                                                for key in record.children))
                result["children"] = values
                result["metrics"]["total_ms"] = (time.perf_counter() - start) * 1000
                if record.count > 1:
                    result["metrics"]["fanout_ms"] = result["metrics"]["total_ms"]
                outcome = {"job_id": record.job_id, "phase": "complete", "result": result}
                self.completed += 1
        except BaseException as exc:
            record.stop.set()
            await self.cancel_children(record)
            outcome = {"job_id": record.job_id, "phase": "failed",
                       "status": 504 if isinstance(exc, TimeoutError) else 500,
                       "error": "cancelled" if isinstance(exc, asyncio.CancelledError) else str(exc) or "deadline exceeded"}
            self.failed += 1
        size = len(json.dumps(outcome).encode())
        if size > MAX_BODY:
            outcome = {"job_id": record.job_id, "phase": "failed", "error": "fanout result exceeds size limit"}
            size = len(json.dumps(outcome).encode())
        # Evict completed records before allocating additional retained output.
        for key, old in list(self.records.items()):
            if self.result_bytes + size <= self.result_budget:
                break
            if old.done.is_set():
                self.result_bytes -= old.size
                del self.records[key]
        record.value, record.size, record.finished = outcome, size, time.monotonic()
        self.result_bytes += size
        record.done.set()

    async def dispatch(self, method, path, body):
        self.prune()
        if method == 'POST' and path == '/v1/invoke':
            _, reply = await self.dispatch('POST', '/v1/jobs', body)
            record = self.records[reply['job_id']]
            try:
                await record.done.wait()
                if record.value['phase'] != 'complete':
                    raise RPCError(record.value.get('status', 500), record.value.get('error', 'guest execution failed'))
                return 200, record.value['result']
            except asyncio.CancelledError:
                await self.dispatch('DELETE', '/v1/jobs/' + record.job_id, None)
                raise
        if method == "GET" and path in {"/healthz", "/metrics"}:
            return (200 if self.stats()["ready"] else 503), self.stats()
        if method == "POST" and path == "/v1/jobs":
            spec = FunctionSpec(**body["spec"])
            count, depth = body.get("count", 1), body.get("depth", 0)
            if type(count) is not int or not 1 <= count <= 256 or type(depth) is not int or not 0 <= depth <= 8:
                raise ValueError("invalid fanout count/depth")
            if spec.memory_mb + 32 > self.memory_budget or spec.cpus > self.cpu_budget:
                raise RPCError(413, "function exceeds this worker's resource budget")
            if self.draining or not self.ready:
                raise RPCError(503, "worker is warming or draining")
            # Completed metadata must not consume admission capacity indefinitely.
            for key, old in list(self.records.items()):
                if len(self.records) < self.max_records:
                    break
                if old.done.is_set():
                    self.result_bytes -= old.size
                    del self.records[key]
            if len(self.records) >= self.max_records:
                self.rejected += 1
                raise RPCError(429, "worker invocation metadata memory exhausted")
            record = Record(self.name + "~" + uuid4().hex, spec, depth, count)
            if count == 1:
                available = self.headroom()
                memory = spec.memory_mb + 32
                if memory > available['memory_available_mb'] or spec.cpus > available['cpu_available_cores']:
                    self.rejected += 1
                    raise RPCError(429, "worker has insufficient CPU or memory headroom")
                record.memory_reserved = memory
                self.memory_used += memory
                self.cpu_used += spec.cpus
                self.active += 1
                self.launches.append((time.monotonic() + .2, spec.cpus, memory))
            self.records[record.job_id] = record
            record.task = asyncio.create_task(self.run(record))

            def ensure_terminal(task):
                # A queued task can be cancelled before its coroutine executes its first line.
                if not record.done.is_set():
                    self.release(record)
                    record.value = {"job_id": record.job_id, "phase": "failed", "error": "cancelled"}
                    record.finished = time.monotonic()
                    record.done.set()
            record.task.add_done_callback(ensure_terminal)
            return 202, {"job_id": record.job_id, "count": count, "wait_supported": True, "backend": "worker-pool"}
        if path.startswith("/v1/jobs/"):
            key = path.removeprefix("/v1/jobs/").removesuffix("/wait")
            record = self.records.get(key)
            if record is None:
                raise RPCError(404, "invocation expired or its worker restarted")
            if method == "DELETE":
                record.stop.set()
                if not record.task.done() and not record.task.cancelling():
                    record.task.cancel()
                await record.done.wait()
                await self.cancel_children(record)
                return 200, {"cancelled": key}
            if method == "GET":
                if path.endswith("/wait"):
                    await record.done.wait()
                return 200, record.value or {"job_id": key, "phase": "running"}
        raise RPCError(404, "unknown worker endpoint")

    async def close(self, grace=600):
        self.draining = True
        tasks = [r.task for r in self.records.values() if not r.done.is_set()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace)
            for task in pending:
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def main_async():
    token = os.environ["FUNCTION_TOKEN"]
    worker = Worker(os.environ.get("FUNCTION_MINI_OS_ROOT", "/opt/mini_os"), token,
                    os.environ["FUNCTION_ROUTER_URL"], name=os.environ["FUNCTION_POD_NAME"],
                    node=os.environ["FUNCTION_NODE_NAME"],
                    memory_budget_mb=int(os.environ.get("FUNCTION_MEMORY_BUDGET_MB", "1024")),
                    cpu_budget=float(os.environ.get("FUNCTION_CPU_BUDGET", "1.5")))
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    await worker.prewarm(int(os.environ.get("FUNCTION_PREWARM_RUNS", "1")))
    rpc = Server(worker.dispatch, token)
    server = await asyncio.start_server(rpc.handle, "0.0.0.0", 8181, limit=16384, backlog=4096)
    try:
        async with server:
            await stopped.wait()
            await worker.close()
    finally:
        await rpc.close()


if __name__ == "__main__":
    asyncio.run(main_async())
