"""Measured QEMU latencies with raw samples and explicit measurement boundaries."""
from dataclasses import replace
from datetime import datetime, timezone
import math
import platform
import statistics
import time


def summary(values):
    values = list(values)
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "raw": []}
    return {"samples": len(values), "min": ordered[0], "mean": statistics.mean(ordered),
            "p50": ordered[math.ceil(len(ordered) * .5) - 1],
            "p95": ordered[math.ceil(len(ordered) * .95) - 1], "max": ordered[-1], "raw": values}


async def benchmark(scheduler, spec, *, iterations=10, warm_runs=3, fanouts=(1, 4, 16)):
    if type(iterations) is not int or iterations < 1 or type(warm_runs) is not int or warm_runs < 1:
        raise ValueError("iterations and warm probes must be positive")
    if not fanouts or any(type(n) is not int or not 1 <= n <= scheduler.max_fanout for n in fanouts):
        raise ValueError("invalid benchmark fanout")
    singles = [(await scheduler.invoke(replace(spec, benchmark_warm_runs=warm_runs))).metrics for _ in range(iterations)]
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "host": platform.platform(),
        "backend": type(scheduler).__name__, "spec": {"memory_mb": spec.memory_mb, "cpus": spec.cpus},
        "notes": [
            "startup_ms: worker execution through fresh disk preparation and first Mini OS prompt.",
            "boot_ms: QEMU process creation to the first shell prompt.",
            "cold_latency_ms: disk preparation plus guest boot and first script execution.",
            "warm_latency_ms: serial dispatch and complete script execution in the same benchmark-only VM.",
            "Warm probes retain guest disk state; use a repeatable workload without fanout.",
            "Production jobs never reuse a guest. Kubernetes total_ms includes placement, image pull, polling and cleanup observation.",
            "fanout_ms: submission through every branch completing; includes queueing and guest startup.",
            "Local fanout_ms additionally includes a coordinating guest. Kubernetes uses one Indexed Job directly.",
            "No AWS measurements are collected; compare equivalent scripts and measurement boundaries yourself.",
        ],
        "single": {key: summary(s[key] for s in singles) for key in
                   ("startup_ms", "boot_ms", "disk_setup_ms", "cold_latency_ms", "execution_ms", "total_ms")},
        "warm_latency_ms": summary(v for s in singles for v in s["warm_latency_ms"]),
        "raw": singles, "fanout": {},
    }
    for count in fanouts:
        times, starts, submissions = [], [], []
        for _ in range(iterations):
            start = time.perf_counter()
            if hasattr(scheduler, "fanout"):
                result = await scheduler.fanout(replace(spec, benchmark_warm_runs=0), count)
                children = result.children if count > 1 else [result]
                submissions.append(result.metrics["submit_ms"])
            else:
                # Source code is deployment configuration; no guest I/O is copied.
                parent = replace(spec, script=f'fanout({count}, "CHILD.MOS");',
                                 scripts={**spec.scripts, "CHILD.MOS": spec.script}, benchmark_warm_runs=0)
                result = await scheduler.invoke(parent)
                children = result.children
                submissions.extend(s["submit_ms"] for s in result.metrics["fanout_submissions"])
            times.append((time.perf_counter() - start) * 1000)
            starts.extend(child.metrics["startup_ms"] for child in children)
        report["fanout"][str(count)] = {"fanout_ms": summary(times), "child_startup_ms": summary(starts),
                                          "submit_ms": summary(submissions)}
    return report
