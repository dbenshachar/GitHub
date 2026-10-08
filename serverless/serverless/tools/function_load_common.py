"""HTTP load generation with continuous users or explicit per-user arrival rates."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import math
from pathlib import Path
import resource
import time
from urllib.parse import urlsplit


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {"samples": len(ordered), "p50": ordered[math.ceil(len(ordered) * .5) - 1],
            "p95": ordered[math.ceil(len(ordered) * .95) - 1],
            "p99": ordered[math.ceil(len(ordered) * .99) - 1], "max": ordered[-1]}


async def request(host, port, method, path, timeout):
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
                         "Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            status = int(lines[0].split()[1])
            fields = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            length = int(fields["Content-Length"])
            if not 0 <= length <= 100000:
                raise ValueError("invalid response size")
            return status, json.loads(await reader.readexactly(length))
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


def generator_limit(users, cap, *, connections=None):
    if users > cap:
        return f"load-generator cap is {cap} users; use --generator-max-users to change it"
    connections = users if connections is None else connections
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and connections + 128 > soft:
        return f"load generator needs at least {connections + 128} open files; current limit is {soft}"
    return None


async def run_stage(host, port, users, duration, timeout, *, requests_per_user_minute=None,
                    max_inflight=1024, max_dispatch_lag_ms=100):
    before_status, before = await request(host, port, "GET", "/metrics", timeout)
    if before_status != 200 or before.get("pending", 0):
        raise RuntimeError("server must be healthy and idle before each stage")
    start = time.perf_counter()
    end = start + duration
    completed = failed = finished_in_window = 0
    started = active = peak_inflight = 0
    latencies, queues, startups, dispatch_lags = [], [], [], []
    failures = Counter()
    rate = None if requests_per_user_minute is None else users * requests_per_user_minute / 60
    planned = None if rate is None else math.ceil(rate * duration)
    dropped = 0

    async def invoke(scheduled=None):
        nonlocal completed, failed, finished_in_window, started, active, peak_inflight
        began = time.perf_counter()
        scheduled = began if scheduled is None else scheduled
        dispatch_lags.append(max(0, (began - scheduled) * 1000))
        started += 1
        active += 1
        peak_inflight = max(peak_inflight, active)
        try:
            status, value = await request(host, port, "POST", "/invoke", timeout)
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {value.get('error', 'request failed')}")
            metrics = value["metrics"]
            completed += 1
            if time.perf_counter() <= end:
                finished_in_window += 1
            # Include generator scheduling delay: late sends must not improve latency.
            latencies.append((time.perf_counter() - scheduled) * 1000)
            queues.append(metrics["queue_ms"])
            startups.append(metrics["startup_ms"])
        except Exception as exc:
            failed += 1
            failures[f"{type(exc).__name__}: {str(exc)}"] += 1
            if rate is None:
                # Avoid a tight reconnect loop when ingress rejects requests.
                await asyncio.sleep(.1)
        finally:
            active -= 1

    async def user():
        while time.perf_counter() < end:
            await invoke()

    async def arrivals():
        """Stagger periodic users evenly, with O(in-flight requests) tasks/memory."""
        nonlocal dropped
        index = 0
        inflight = set()
        try:
            while index < planned:
                now = time.perf_counter()
                due = start + index / rate
                if now < due:
                    await asyncio.sleep(min(due - now, .05))
                    continue
                if now >= end:
                    dropped += planned - index
                    break
                # Count missed deadlines instead of hiding them in a delayed client queue.
                expired = min(planned, max(0, math.ceil(
                    (now - start - max_dispatch_lag_ms / 1000) * rate)))
                if expired > index:
                    dropped += expired - index
                    index = expired
                due_end = min(planned, math.floor((now - start) * rate) + 1)
                budget = max_inflight - len(inflight)
                sent_end = min(due_end, index + budget)
                while index < sent_end:
                    task = asyncio.create_task(invoke(start + index / rate))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                    index += 1
                dropped += due_end - index
                index = due_end
                await asyncio.sleep(0)
            # Keep the full observation window, even after its last scheduled arrival.
            await asyncio.sleep(max(0, end - time.perf_counter()))
            await asyncio.gather(*list(inflight))
        finally:
            tasks = list(inflight)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    lag, server_peaks = [], {"active": 0, "pending": 0}

    async def monitor():
        while time.perf_counter() < end:
            tick = time.perf_counter()
            await asyncio.sleep(.1)
            lag.append(max(0, (time.perf_counter() - tick - .1) * 1000))
            try:
                status, stats = await request(host, port, "GET", "/metrics", timeout)
                if status == 200:
                    for key in server_peaks:
                        server_peaks[key] = max(server_peaks[key], stats[key])
            except Exception:
                pass

    monitor_task = asyncio.create_task(monitor())
    try:
        if rate is None:
            await asyncio.gather(*(user() for _ in range(users)))
        else:
            await arrivals()
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
    # A client timeout can precede server cancellation/cleanup. Drain before another stage.
    drain_deadline = time.perf_counter() + timeout + 5
    after = before
    drain_error = None
    while True:
        remaining = drain_deadline - time.perf_counter()
        if remaining <= 0:
            drain_error = "server did not drain; restart it before another test"
            break
        try:
            status, snapshot = await request(host, port, "GET", "/metrics", min(timeout, remaining))
            if status != 200:
                raise RuntimeError(f"metrics HTTP {status}")
            after = snapshot
        except Exception as exc:
            drain_error = f"unable to check server drain: {type(exc).__name__}: {exc}"
            break
        if not after["pending"]:
            break
        await asyncio.sleep(.1)
    elapsed = time.perf_counter() - start
    return {"users": users, "offered_seconds": duration, "elapsed_with_drain_seconds": elapsed,
            "requests_started": started, "completed": completed, "failed": failed,
            "server_drained": drain_error is None, "drain_error": drain_error,
            "server_remaining_pending": after.get("pending"),
            "server_job_timeout_s": before.get("job_timeout_s"), "client_request_timeout_s": timeout,
            "requests_per_user_minute": requests_per_user_minute,
            "offered_rps": rate, "planned_requests": planned,
            "generator_dropped_requests": dropped,
            "error_fraction": (failed + dropped) / max(1, completed + failed + dropped),
            "observed_requests_per_user_minute": started / duration / users * 60,
            "successful_requests_per_user_minute_in_window": finished_in_window / duration / users * 60,
            "request_periods_per_user": None if requests_per_user_minute is None else
                duration * requests_per_user_minute / 60,
            "successful_rps_in_window": finished_in_window / duration,
            "successful_rps_including_drain": completed / elapsed,
            "peak_client_inflight": peak_inflight, "sampled_server_peaks": server_peaks,
            "latency_ms": distribution(latencies), "queue_ms": distribution(queues),
            "startup_ms": distribution(startups), "generator_loop_lag_ms": distribution(lag),
            "dispatch_lag_ms": distribution(dispatch_lags),
            "failures": dict(failures),
            "backend": after.get("backend", "unknown"), "queue_scope": after.get("queue_scope"),
            "worker_node_completions": {node: count - before.get("worker_nodes", {}).get(node, 0)
                                        for node, count in after.get("worker_nodes", {}).items()},
            "server_counter_delta": {k: after.get(k, 0) - before.get(k, 0)
                                     for k in ("completed", "failed", "rejected", "cancelled")}}


def levels(target, start=1):
    result = [start]
    n = 4 if start == 1 else start * 2
    while n < target:
        result.append(n)
        n *= 2
    if target not in result:
        result.append(target)
    return result


async def run(args):
    url = urlsplit(args.url)
    if url.scheme != "http" or not url.hostname or url.path not in ("", "/"):
        raise ValueError("--url must be an HTTP server origin, e.g. http://127.0.0.1:8090")
    host, port = url.hostname, url.port or 80
    stages = []
    reason = "maximum requested user count tested"
    reached = False
    healthy_users = 0
    failures_only = getattr(args, "failures_only", False)
    user_rate = args.requests_per_user_minute
    partial_period = user_rate is not None and args.duration * user_rate / 60 < 1
    if partial_period:
        print(f"Quick sample: {args.duration:g}s covers {args.duration * user_rate / 60:.1%} "
              "of one staggered user request period; this is not a full-population sustained test.", flush=True)
    counts = args.users if args.users else levels(args.target_users, args.start_users)
    for count in counts:
        problem = generator_limit(count, args.generator_max_users,
                                  connections=args.max_inflight if user_rate is not None else None)
        if problem:
            reason = problem
            print(f"Stopping: {reason}", flush=True)
            break
        workload = "continuously requesting" if user_rate is None else f"at {user_rate:g} requests/minute each"
        print(f"Testing {count} users {workload} for {args.duration:g}s...", flush=True)
        stage = await run_stage(host, port, count, args.duration, args.request_timeout,
                                requests_per_user_minute=user_rate, max_inflight=args.max_inflight,
                                max_dispatch_lag_ms=args.max_generator_lag_ms)
        stages.append(stage)
        print(json.dumps(stage, indent=2), flush=True)
        if not stage.get("server_drained", True):
            reason = stage["drain_error"]
            break
        delay = stage[args.delay_metric]["p95"]
        overloaded = (stage["error_fraction"] > args.max_error_fraction or
                      (not failures_only and (delay is None or delay >= args.threshold_seconds * 1000)))
        generator_lag = stage["generator_loop_lag_ms"]["p95"]
        if not failures_only and generator_lag is not None and generator_lag > args.max_generator_lag_ms:
            reason = "load-generator event loop exceeded its lag limit; capacity is inconclusive"
            break
        if stage.get("generator_dropped_requests", 0):
            reason = "load generator could not send the offered request rate; capacity is inconclusive"
            break
        if overloaded:
            reason = "error budget exceeded" if failures_only else "delay threshold reached or error budget exceeded"
            break
        healthy_users = max(healthy_users, count)
        if count >= args.target_users:
            reached = True
    report = {"target_users": args.target_users, "target_sustained": reached,
              "failures_only": failures_only,
              "partial_period_sample": partial_period,
              "requests_per_user_minute": user_rate,
              "load_model": "continuous_closed_loop" if user_rate is None else "periodic_staggered_arrivals",
              "highest_tested_users": max((s["users"] for s in stages), default=0),
              "highest_healthy_tested_users": healthy_users,
              "threshold_seconds": None if failures_only else args.threshold_seconds,
              "delay_metric": args.delay_metric,
              "max_error_fraction": args.max_error_fraction, "stop_reason": reason,
              "url": args.url, "stages": stages,
              "notes": [("One virtual user has one outstanding invocation and immediately requests another after completion."
                         if user_rate is None else
                         "Each logical user invokes periodically at the specified rate; initial phases are uniformly staggered. No idle sockets or tasks are allocated per user."),
                        "Rate mode tests aggregate invocation traffic, not one million simultaneous open connections or synchronized bursts.",
                        "Rate-mode latency includes client dispatch delay. Missed arrivals are counted as generator drops and prevent a capacity claim.",
                        "Every successful invocation executes a fresh HELLO.MOS guest; no warm VM reuse.",
                        "Latency and queue distributions include successful requests only; failures are counted separately.",
                        ("Latency and generator lag are reported without stopping the ramp; request errors, unsent arrivals and resource/drain limits still stop it."
                         if failures_only else
                         "Threshold applies to p95, and capacity is bounded by the tested stages and durations."),
                        "Queue is server admission to execution-slot acquisition; startup includes fresh disk and boot.",
                        "One million users is a target, not a claim; overload and generator limits stop the ramp.",
                        "A generator on the same host competes for resources; use separate load hosts for production capacity."]}
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print("\nFinal report:", flush=True)
    print(json.dumps(report, indent=2), flush=True)
    if user_rate is not None and healthy_users:
        best = next(s for s in stages if s["users"] == healthy_users)
        label = "Highest healthy quick-sample load" if partial_period else "Highest healthy measured load"
        print(f"\n{label}: {healthy_users:,} users at {user_rate:g} requests/minute each; "
              f"p95 request latency {best['latency_ms']['p95']:.1f} ms; "
              f"{best['error_fraction'] * 100:.2f}% failures over {args.duration:g}s.", flush=True)
    return report


def main(*, threshold_seconds, target_users=4096, start_users=1, requests_per_user_minute=None):
    parser = argparse.ArgumentParser(description="Request fresh HELLO.MOS jobs until p95 delay or errors exceed the budget.")
    parser.add_argument("--url", default="http://127.0.0.1:8090")
    parser.add_argument("--duration", type=float, default=60 if requests_per_user_minute is not None else 15,
                        help="seconds of offered load per user level")
    parser.add_argument("--requests-per-user-minute", type=float, default=requests_per_user_minute,
                        help="periodic invocation rate per logical user; omit for continuous closed-loop users")
    parser.add_argument("--max-inflight", type=int, default=1024,
                        help="maximum generator connections in rate mode; unsent requests count as drops")
    parser.add_argument("--users", type=int, nargs="+", help="override the default increasing user levels")
    parser.add_argument("--target-users", type=int, default=target_users)
    parser.add_argument("--start-users", type=int, default=start_users,
                        help="first user count in the default ramp; doubles each stage")
    parser.add_argument("--threshold-seconds", type=float, default=threshold_seconds)
    parser.add_argument("--failures-only", action="store_true",
                        help="ignore latency and loop-lag stopping thresholds; retain errors and generator/resource guards")
    parser.add_argument("--delay-metric", choices=("queue_ms", "latency_ms"),
                        default="latency_ms" if requests_per_user_minute is not None else "queue_ms")
    parser.add_argument("--request-timeout", type=float, default=40)
    parser.add_argument("--max-error-fraction", type=float, default=.01)
    parser.add_argument("--generator-max-users", type=int,
                        default=target_users if requests_per_user_minute is not None else 10000)
    parser.add_argument("--max-generator-lag-ms", type=float, default=100)
    parser.add_argument("--output", type=Path, help="optional JSON report; metrics always print")
    args = parser.parse_args()
    if (any(not math.isfinite(n) for n in (args.duration, args.request_timeout, args.threshold_seconds,
                                         args.max_generator_lag_ms, args.max_error_fraction)) or
            args.duration <= 0 or args.request_timeout <= 0 or args.threshold_seconds <= 0 or
            args.target_users < 1 or args.start_users < 1 or
            args.generator_max_users < 1 or args.max_generator_lag_ms <= 0 or args.max_inflight < 1 or
            not 0 <= args.max_error_fraction <= 1):
        parser.error("counts, durations and lag limits must be positive; error fraction must be between 0 and 1")
    if args.requests_per_user_minute is not None:
        if not math.isfinite(args.requests_per_user_minute) or args.requests_per_user_minute <= 0:
            parser.error("--requests-per-user-minute must be finite and positive")
    if not args.users and args.start_users > args.target_users:
        parser.error("--start-users must not exceed --target-users")
    if args.users and (any(n < 1 for n in args.users) or args.users != sorted(set(args.users))):
        parser.error("--users must be positive, unique and increasing")
    try:
        asyncio.run(run(args))
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        parser.exit(1, f"load test failed: {exc}\n")
