"""Router client usable from a laptop or any Kubernetes host."""
import asyncio
import json
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .functions import FunctionError, InvocationResult
from .function_rpc import RPCError, request as rpc_request


class RouterClient:
    max_fanout = 256

    def __init__(self, url, token, *, poll_s=.5, concurrency=8, threaded_transport=False):
        if not url.startswith(("http://", "https://")):
            raise ValueError("router requires an HTTP(S) URL")
        self.url, self.token, self.poll_s = url.rstrip("/"), token, poll_s
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("router concurrency must be positive")
        self._http_slots = asyncio.Semaphore(concurrency)
        self.threaded_transport = threaded_transport

    async def async_request(self, method, path, body=None):
        async with self._http_slots:
            if self.threaded_transport:
                return await asyncio.to_thread(self.request, method, path, body)
            try:
                return await rpc_request(self.url, self.token, method, path, body)
            except RPCError as exc:
                raise FunctionError(f"router HTTP {exc.status}: {exc}") from exc

    async def finish_cleanup(self, coroutine):
        """Finish bounded HTTP cleanup even if a deadline and disconnect both cancel us."""
        task = asyncio.create_task(coroutine)
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                continue
        return task.result()

    async def cancel_request(self, path):
        try:
            await self.async_request("DELETE", path)
        except Exception:
            # The Kubernetes deadline still bounds execution if routing is unavailable.
            pass

    async def cancel_submission(self, submission):
        await asyncio.wait((submission,))
        try:
            job = submission.result()
        except Exception:
            return
        await self.cancel_request("/v1/jobs/" + quote(job["job_id"], safe=""))

    def request(self, method, path, body=None):
        request = Request(self.url + path, None if body is None else json.dumps(body).encode(),
            {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise FunctionError(f"router HTTP {exc.code}: " + exc.read(4096).decode(errors="replace")) from exc

    async def invoke(self, spec, *, count=1):
        started = time.perf_counter()
        submission = asyncio.create_task(self.async_request(
            "POST", "/v1/jobs", {"spec": spec.to_dict(), "count": count}))
        try:
            # wait() doesn't cancel submission or install shield's exception-logging callback.
            await asyncio.wait((submission,))
            job = submission.result()
        except asyncio.CancelledError:
            # A synchronous HTTP POST may still create a job after its caller disconnects.
            # Recover its ID before requesting deletion so submission races don't orphan jobs.
            await self.finish_cleanup(self.cancel_submission(submission))
            raise
        path = "/v1/jobs/" + quote(job["job_id"], safe="")
        submitted = time.perf_counter()
        try:
            if job.get("wait_supported"):
                # One asynchronous completion notification; no executor thread or status polling.
                value = await rpc_request(self.url, self.token, "GET", path + "/wait",
                                          timeout=spec.timeout_s + 30)
                if value["phase"] != "complete":
                    raise FunctionError(value.get("error", "worker invocation failed"))

                def decode(item):
                    return InvocationResult(item["invocation_id"], item["output"], item["metrics"],
                                            [decode(child) for child in (item.get("children") or [])])

                result = decode(value["result"])
                result.metrics["worker_total_ms"] = result.metrics.get("total_ms", 0)
                result.metrics.update(total_ms=(time.perf_counter() - started) * 1000,
                                      submit_ms=(submitted - started) * 1000)
                if count > 1:
                    result.metrics["fanout_ms"] = result.metrics["total_ms"]
                return result
            async with asyncio.timeout(spec.timeout_s + 15):
                while True:
                    result = await self.async_request("GET", path)
                    if result["phase"] == "failed":
                        raise FunctionError("Kubernetes job failed: " + json.dumps(result))
                    if result["phase"] == "complete":
                        break
                    await asyncio.sleep(self.poll_s)
            children = []
            for item in result["results"]:
                if item.get("type") != "result":
                    raise FunctionError("worker metrics missing: " + json.dumps(item))
                metrics = dict(item["metrics"])
                if item.get("node"):
                    metrics["worker_node"] = item["node"]
                children.append(InvocationResult(item["pod"], item["output"], metrics))
            if len(children) != count:
                raise FunctionError("indexed job returned an unexpected result count")
            if count == 1:
                child = children[0]
                child.invocation_id = job["job_id"]
                child.metrics.update(total_ms=(time.perf_counter() - started) * 1000,
                                     submit_ms=(submitted - started) * 1000)
                return child
            return InvocationResult(job["job_id"], "", {
                "fanout_ms": (time.perf_counter() - started) * 1000,
                "submit_ms": (submitted - started) * 1000,
            }, children)
        except BaseException:
            await self.finish_cleanup(self.cancel_request(path))
            raise

    async def fanout(self, spec, count):
        return await self.invoke(spec, count=count)
