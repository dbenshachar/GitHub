"""Load balancing only; workers admit guests against CPU/memory headroom."""
import asyncio
import ipaddress
import json
import os
import re
import signal
import time

from .function_kubernetes import JobRouter, KubernetesAPI
from .function_rpc import RPCError, Server, request
from .functions import FunctionSpec


class PoolRouter:
    def __init__(self, token, *, api=None, workers=None, cron=None):
        self.token, self.api, self.cron = token, api, cron
        self.workers = workers or {}
        self.cursor = 0
        self.ready = False
        self.last_discovery = 0

    async def refresh(self):
        if self.api and time.monotonic() - self.last_discovery >= 5:
            pods = await asyncio.to_thread(self.api.list, "pods", "app=mini-functions-worker")
            discovered = {}
            for pod in pods:
                metadata, status = pod["metadata"], pod.get("status", {})
                if metadata.get("deletionTimestamp") or not any(
                        c["type"] == "Ready" and c["status"] == "True" for c in status.get("conditions", [])):
                    continue
                address = ipaddress.ip_address(status["podIP"])
                host = f"[{address}]" if address.version == 6 else str(address)
                old = self.workers.get(metadata["name"], {})
                discovered[metadata["name"]] = {**old, "url": f"http://{host}:8181"}
            self.workers = discovered
            self.last_discovery = time.monotonic()

        async def update(name, endpoint):
            try:
                value = await request(endpoint["url"], self.token, "GET", "/metrics", timeout=2)
                endpoint.update(value, observed=time.monotonic())
            except Exception:
                endpoint["ready"] = False
        await asyncio.gather(*(update(name, entry) for name, entry in list(self.workers.items())))
        self.ready = any(entry.get("ready") for entry in self.workers.values())

    async def watch(self):
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                print(json.dumps({"event": "worker_discovery_error", "error": str(exc)}), flush=True)
            await asyncio.sleep(1)

    def endpoint(self, job_id):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}~[a-f0-9]{32}", job_id):
            raise RPCError(400, "invalid invocation ID")
        name = job_id.split("~", 1)[0]
        entry = self.workers.get(name)
        if entry is None:
            raise RPCError(410, "invocation worker is unavailable; work is not replayed")
        return entry

    async def forward_submission(self, endpoint, body):
        submission = asyncio.create_task(request(endpoint["url"], self.token, "POST", "/v1/jobs", body))
        try:
            await asyncio.wait((submission,))
            return submission.result()
        except asyncio.CancelledError:
            # If the caller disappears during submission, cancel the accepted work.
            while not submission.done():
                try:
                    await asyncio.wait((submission,))
                except asyncio.CancelledError:
                    continue
            if not submission.cancelled() and submission.exception() is None:
                value = submission.result()
                try:
                    await request(endpoint["url"], self.token, "DELETE", "/v1/jobs/" + value["job_id"])
                except Exception:
                    pass
            raise

    async def dispatch(self, method, path, body):
        if method == "GET" and path in {"/healthz", "/metrics"}:
            ready = [v for v in self.workers.values() if v.get("ready")]
            return (200 if self.ready else 503), {"backend": "worker-pool", "ready_workers": len(ready),
                "active": sum(v.get("active", 0) for v in ready),
                "pending": sum(v.get("pending", 0) for v in ready),
                "workers": [{"name": name, **{k: v.get(k) for k in (
                    "node", "ready", "active", "pending", "completed", "failed", "rejected",
                    "cpu_used_cores", "cpu_available_cores", "memory_available_mb")}}
                    for name, v in self.workers.items()]}
        if method == "POST" and path in {"/v1/jobs", "/v1/invoke"}:
            FunctionSpec(**body["spec"])
            count, depth = body.get("count", 1), body.get("depth", 0)
            if type(count) is not int or not 1 <= count <= 256 or type(depth) is not int or not 0 <= depth <= 8:
                raise ValueError("invalid count/depth")
            candidates = [v for v in self.workers.values() if v.get("ready") and
                          time.monotonic() - v.get("observed", 0) < 15]
            if not candidates:
                raise RPCError(503, "no ready workers; add or warm worker replicas")
            # Least outstanding traffic with rotating ties; workers own resource admission.
            offset = self.cursor % len(candidates)
            self.cursor += 1
            candidates = candidates[offset:] + candidates[:offset]
            candidates.sort(key=lambda v: max(v.get("active", 0), v.get("routing_active", 0)))
            for endpoint in candidates:
                endpoint["routing_active"] = endpoint.get("routing_active", 0) + 1
                try:
                    if path == '/v1/invoke':
                        value = await request(endpoint['url'], self.token, method, path, body,
                                              timeout=body['spec'].get('timeout_s', 600) + 5)
                        return 200, value
                    value = await self.forward_submission(endpoint, body)
                    return 202, value
                except RPCError as exc:
                    # Only an explicit non-admission response is safe to send to another worker.
                    if exc.status != 429 and not (exc.status == 503 and str(exc) in {
                            'worker is warming or draining', 'RPC connection limit reached'}):
                        raise
                finally:
                    endpoint['routing_active'] -= 1
            raise RPCError(429, "all workers lack CPU or memory headroom")
        if path.startswith("/v1/jobs/"):
            key = path.removeprefix("/v1/jobs/").removesuffix("/wait")
            endpoint = self.endpoint(key)
            value = await request(endpoint["url"], self.token, method, path, timeout=630)
            return 200, value
        if path == "/v1/schedules" and method == "POST" and self.cron:
            value = dict(body)
            spec = FunctionSpec(**value.pop("spec"))
            return 202, await asyncio.to_thread(self.cron.schedule, spec, **value)
        if path.startswith("/v1/schedules/") and method == "DELETE" and self.cron:
            name = path.removeprefix("/v1/schedules/")
            await asyncio.to_thread(self.api.request, "DELETE", self.api.path("cronjobs", name),
                                    {"propagationPolicy": "Foreground"})
            return 200, {"deleted": name}
        raise RPCError(404, "unknown router endpoint")


class PoolCronRouter(JobRouter):
    def job_manifest(self, spec, *, count=1, **kwargs):
        manifest = super().job_manifest(spec, count=1, **kwargs)
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        container["command"] = ["python3", "-m", "remote_desktop.function_cron"]
        container["env"].append({"name": "FUNCTION_COUNT", "value": str(count)})
        container["resources"] = {"requests": {"cpu": "25m", "memory": "32Mi"},
                                  "limits": {"cpu": "250m", "memory": "128Mi"}}
        return manifest


async def main_async():
    api = KubernetesAPI(os.environ.get("FUNCTION_NAMESPACE", "mini-functions"))
    token = os.environ["FUNCTION_TOKEN"]
    cron = PoolCronRouter(api, image=os.environ["FUNCTION_IMAGE"], router_url=os.environ["FUNCTION_ROUTER_URL"])
    router = PoolRouter(token, api=api, cron=cron)
    await router.refresh()
    discovery = asyncio.create_task(router.watch())
    rpc = Server(router.dispatch, token)
    server = await asyncio.start_server(rpc.handle, "0.0.0.0", 8080, limit=16384, backlog=4096)
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    try:
        async with server:
            await stopped.wait()
    finally:
        discovery.cancel()
        await asyncio.gather(discovery, return_exceptions=True)
        await rpc.close()


if __name__ == "__main__":
    asyncio.run(main_async())
