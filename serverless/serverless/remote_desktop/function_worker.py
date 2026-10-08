"""One fresh FAT disk and QEMU guest per job; no snapshots or state transfer."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

from .functions import FunctionSpec

OUTPUT_LIMIT = 1024 * 1024


def fanout_library(port):
    # Plain HTTP only reaches the loopback bridge through QEMU's 10.0.2.2.
    # Tokens and Kubernetes credentials are never put on the guest disk.
    return r'''
fn fanout(count, script) {
    let body = str(count) + "\n" + script;
    let request = "POST /fanout HTTP/1.0\r\nContent-Length: " + str(len(body)) + "\r\n\r\n" + body;
    let socket = tcp_connect("10.0.2.2", PORT);
    let sent = 0;
    while (sent < len(request)) {
        let chunk = slice(request, sent, len(request) - sent);
        let n = tcp_send(socket, chunk, len(chunk));
        free(chunk);
        if (n == 0) { return 1 / 0; }
        sent = sent + n;
    }
    let buffer = malloc(512);
    let response = "";
    let n = tcp_recv(socket, buffer, 512);
    while (n > 0) {
        let chunk = slice(buffer, 0, n);
        let next = response + chunk;
        free(response);
        free(chunk);
        response = next;
        n = tcp_recv(socket, buffer, 512);
    }
    tcp_close(socket);
    if (len(response) < 12) { return 1 / 0; }
    let status = slice(response, 0, 12);
    if (status != "HTTP/1.0 202") { return 1 / 0; }
    free(status);
    free(buffer);
    free(response);
    free(request);
    free(body);
    return 0;
}
'''.replace("PORT", str(port))


@contextmanager
def bridge(submit):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.connection.settimeout(5)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.path != "/fanout" or not 1 <= size <= 64:
                    raise ValueError("invalid fanout request")
                count, name = self.rfile.read(size).decode("ascii").split("\n", 1)
                reply = submit(int(count), name)
                payload = json.dumps(reply).encode()
                self.send_response(202)
            except Exception as exc:
                payload = json.dumps({"error": str(exc)}).encode()
                self.send_response(400)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def execute(spec: FunctionSpec, root: Path, submit, *, qemu_bin="qemu-system-aarch64", stop_event=None):
    root = Path(root)
    if not (root / "job-runtime-v1").is_file():
        raise RuntimeError("build the fail-fast Mini OS job runtime with tools/build_mini_os.py")
    started = time.perf_counter()
    deadline = time.monotonic() + spec.timeout_s
    submissions = []
    def fanout(count, name):
        spec.child(name)  # Only application code registered at submission is executable.
        t = time.perf_counter()
        reply = submit(count, name)
        submissions.append({"job_id": reply["job_id"], "count": count, "submit_ms": (time.perf_counter() - t) * 1000})
        return reply

    with tempfile.TemporaryDirectory(prefix="mini-job-") as work, bridge(fanout) as port:
        work = Path(work)
        scripts = {"RUN.MOS": spec.script, **spec.scripts}
        paths = []
        library = fanout_library(port)
        for name, source in scripts.items():
            path = work / name
            path.write_text(library + "\n" + source)
            if path.stat().st_size > 65536:
                raise ValueError("script plus runtime library exceeds Mini OS source limit")
            paths.append(str(path))
        disk = work / "disk.img"
        subprocess.run([str(root / "out/mkfat32"), str(disk), *paths], check=True,
                       stdout=subprocess.DEVNULL, timeout=max(.1, deadline - time.monotonic()))
        disk_ready = time.perf_counter()
        qemu = subprocess.Popen([
            qemu_bin, "-M", "virt", "-accel", "tcg", "-cpu", "cortex-a57",
            "-m", str(spec.memory_mb), "-smp", "1", "-nodefaults", "-nographic",
            "-monitor", "none", "-serial", "stdio", "-kernel", str(root / "kernel.elf"),
            "-drive", f"if=none,file={disk},format=raw,id=hd0",
            "-device", "virtio-blk-device,drive=hd0",
            "-netdev", "user,id=net0", "-device", "virtio-net-device,netdev=net0",
            "-action", "shutdown=poweroff",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr, bufsize=0)
        output = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(qemu.stdout, selectors.EVENT_READ)

        def prompt():
            current = bytearray()
            while not current.endswith(b"\n> "):
                if stop_event is not None and stop_event.is_set():
                    raise RuntimeError("Mini OS job cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Mini OS job exceeded its deadline")
                if not selector.select(min(remaining, .25)):
                    continue
                chunk = os.read(qemu.stdout.fileno(), 4096)
                current.extend(chunk)
                output.extend(chunk)
                if len(output) > OUTPUT_LIMIT:
                    raise RuntimeError("Mini OS output exceeds 1 MiB")
                if b"MINI_JOB_FAILED" in current or b"fs init failed" in current:
                    raise RuntimeError("Mini OS command failed: " + current.decode(errors="replace")[-2048:])
                if not chunk:
                    raise RuntimeError("Mini OS exited before script completion: " + current.decode(errors="replace")[-2048:])
            return time.perf_counter()

        def command(line):
            qemu.stdin.write(line.encode() + b"\n")
            qemu.stdin.flush()

        try:
            ready = prompt()
            warm = []
            command("exec /RUN.MOS")
            first_done = prompt()
            for _ in range(spec.benchmark_warm_runs):
                t = time.perf_counter()
                command("exec /RUN.MOS")
                warm.append((prompt() - t) * 1000)
            command("exit")
            qemu.wait(timeout=max(.1, deadline - time.monotonic()))
            return {
                "output": output.decode(errors="replace"),
                "metrics": {
                    "disk_setup_ms": (disk_ready - started) * 1000,
                    "startup_ms": (ready - started) * 1000,
                    "boot_ms": (ready - disk_ready) * 1000,
                    "cold_latency_ms": (first_done - started) * 1000,
                    "execution_ms": (first_done - ready) * 1000,
                    "warm_latency_ms": warm,
                    "fanout_submissions": submissions,
                },
            }
        finally:
            selector.close()
            if qemu.poll() is None:
                # Request the shell exit on every failure, then enforce termination.
                try:
                    command("exit")
                except (OSError, ValueError):
                    pass
                qemu.terminate()
                try:
                    qemu.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    qemu.kill()
                    qemu.wait()
            qemu.stdin.close()
            qemu.stdout.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mini-os-root", type=Path, default=Path("/opt/mini_os"))
    parser.add_argument("--protocol", action="store_true")
    args = parser.parse_args(argv)
    lock = threading.Lock()
    def emit(value):
        print(json.dumps(value), flush=True)
    if args.protocol:
        spec = FunctionSpec(**json.loads(sys.stdin.readline()))
        def submit(count, name):
            with lock:
                emit({"type": "fanout", "count": count, "script": name})
                reply = json.loads(sys.stdin.readline())
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "fanout rejected"))
            return reply
    else:
        spec = FunctionSpec(**json.loads(os.environ["FUNCTION_SPEC"]))
        sequence = 0
        def submit(count, name):
            nonlocal sequence
            request_id = sequence
            sequence += 1
            request = Request(os.environ["FUNCTION_ROUTER_URL"] + "/v1/fanout",
                json.dumps({"parent_pod": os.environ["FUNCTION_POD_NAME"], "parent_uid": os.environ["FUNCTION_POD_UID"],
                            "count": count, "script": name, "request_id": request_id}).encode(),
                {"Authorization": "Bearer " + os.environ["FUNCTION_TOKEN"], "Content-Type": "application/json"})
            with urlopen(request, timeout=2.5) as response:
                return json.load(response)
    def terminated(*args):
        raise RuntimeError("job terminated")
    signal.signal(signal.SIGTERM, terminated)
    try:
        result = execute(spec, args.mini_os_root, submit)
        emit({"type": "result", **result})
        return 0
    except Exception as exc:
        emit({"type": "error", "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
