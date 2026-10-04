"""Authenticated, bounded HTTP ingress for stateless Kubernetes job routing."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import threading
from urllib.parse import urlsplit

from .function_kubernetes import APIError, JobRouter, KubernetesAPI
from .functions import FunctionSpec


class RouterServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, router, token, *, max_requests=32):
        if len(token) < 32:
            raise ValueError("router token must contain at least 32 characters")
        self.router, self.token = router, token
        self.slots = threading.BoundedSemaphore(max_requests)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Busy\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(json.dumps({"event": "http", "method": self.command, "path": urlsplit(self.path).path,
                          "message": format % args}), flush=True)

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self):
        path = urlsplit(self.path).path
        if self.command == "GET" and path == "/healthz":
            return self.reply(200, {"ok": True})
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return self.reply(401, {"error": "unauthorized"})
        try:
            router = self.server.router
            if self.command == "POST":
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("chunked requests are unsupported")
                size = int(self.headers.get("Content-Length", "0"))
                if not 1 <= size <= 100000:
                    return self.reply(413, {"error": "request exceeds limit"})
                data = json.loads(self.rfile.read(size))
                if path == "/v1/jobs":
                    value = router.submit(FunctionSpec(**data["spec"]), count=data.get("count", 1))
                elif path == "/v1/fanout":
                    value = router.fanout(**data)
                elif path == "/v1/schedules":
                    value = router.schedule(FunctionSpec(**data.pop("spec")), **data)
                else:
                    return self.reply(404, {"error": "unknown endpoint"})
                return self.reply(202, value)
            if path.startswith("/v1/jobs/"):
                name = path.removeprefix("/v1/jobs/")
                if self.command == "GET":
                    return self.reply(200, router.status(name))
                if self.command == "DELETE":
                    return self.reply(200, router.cancel(name))
            if self.command == "DELETE" and path.startswith("/v1/schedules/"):
                name = path.removeprefix("/v1/schedules/")
                router.api.request("DELETE", router.api.path("cronjobs", name), {"propagationPolicy": "Foreground"})
                return self.reply(200, {"deleted": name})
            self.reply(404, {"error": "unknown endpoint"})
        except APIError as exc:
            self.reply(exc.status if exc.status in {404, 409, 429} else 502, {"error": str(exc)})
        except (ValueError, TypeError, KeyError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception as exc:
            print(json.dumps({"event": "router_error", "error": str(exc)}), flush=True)
            self.reply(503, {"error": "routing unavailable"})

    do_GET = dispatch
    do_POST = dispatch
    do_DELETE = dispatch


def main():
    api = KubernetesAPI(os.environ.get("FUNCTION_NAMESPACE", "mini-functions"))
    router = JobRouter(api, image=os.environ["FUNCTION_IMAGE"], router_url=os.environ["FUNCTION_ROUTER_URL"],
                       max_fanout=int(os.environ.get("FUNCTION_MAX_FANOUT", "256")),
                       parallelism=int(os.environ.get("FUNCTION_PARALLELISM", "64")))
    with RouterServer(("0.0.0.0", 8080), router, os.environ["FUNCTION_TOKEN"]) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
