"""Router client usable from a laptop or any Kubernetes host."""
import asyncio
import json
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .functions import FunctionError, InvocationResult


class RouterClient:
    max_fanout = 256

    def __init__(self, url, token, *, poll_s=.5):
        if not url.startswith(("http://", "https://")):
            raise ValueError("router requires an HTTP(S) URL")
        self.url, self.token, self.poll_s = url.rstrip("/"), token, poll_s

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
        job = await asyncio.to_thread(self.request, "POST", "/v1/jobs", {"spec": spec.to_dict(), "count": count})
        path = "/v1/jobs/" + quote(job["job_id"], safe="")
        submitted = time.perf_counter()
        try:
            async with asyncio.timeout(spec.timeout_s + 15):
                while True:
                    result = await asyncio.to_thread(self.request, "GET", path)
                    if result["phase"] == "failed":
                        raise FunctionError("Kubernetes job failed: " + json.dumps(result))
                    if result["phase"] == "complete":
                        break
                    await asyncio.sleep(self.poll_s)
            children = []
            for item in result["results"]:
                if item.get("type") != "result":
                    raise FunctionError("worker metrics missing: " + json.dumps(item))
                children.append(InvocationResult(item["pod"], item["output"], item["metrics"]))
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
            try:
                await asyncio.to_thread(self.request, "DELETE", path)
            except Exception:
                # The cluster deadline still bounds execution if routing is unavailable.
                pass
            raise

    async def fanout(self, spec, count):
        return await self.invoke(spec, count=count)
