"""Measure an existing Lambda and annotate comparisons with a local QEMU report.

Run: python3 -m tools.benchmark_lambda --function NAME --region REGION
Requires boto3 and normal AWS credentials. Never deploys or changes resources.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from remote_desktop.function_benchmark import summary


def invoke(client, function, qualifier=None):
    options = {"FunctionName": function, "InvocationType": "RequestResponse", "LogType": "Tail", "Payload": b'{"n":100}'}
    if qualifier:
        options["Qualifier"] = qualifier
    start = time.perf_counter()
    response = client.invoke(**options)
    stream = response["Payload"]
    try:
        data = json.loads(stream.read())
    finally:
        stream.close()
    elapsed = (time.perf_counter() - start) * 1000
    if response.get("FunctionError") or response.get("StatusCode") != 200:
        raise RuntimeError("Lambda failed: " + json.dumps(data))
    if (data.get("result") != 100 or type(data.get("first_invocation")) is not bool
            or not isinstance(data.get("environment_id"), str)):
        raise ValueError("deploy examples/lambda_handler.py with handler lambda_handler.handler")
    log = base64.b64decode(response.get("LogResult", "")).decode(errors="replace")
    # Parse only REPORT; the handler's own prints must not be interpreted as metrics.
    line = next((s for s in reversed(log.splitlines()) if s.startswith("REPORT ")), "")
    metrics = {}
    for label, key in (("Duration", "duration_ms"), ("Init Duration", "init_ms"),
                       ("Billed Duration", "billed_ms"), ("Max Memory Used", "memory_mb")):
        match = re.search(r"(?:^|\s)" + re.escape(label) + r":\s*([0-9.]+)", line)
        if match:
            metrics[key] = float(match[1])
    return {"client_ms": elapsed, **data, **metrics, "executed_version": response.get("ExecutedVersion")}


def collect(client, function, *, iterations=5, warm_runs=3, fanouts=(1, 4, 16), concurrency=16, qualifier=None):
    if min(iterations, warm_runs, concurrency) < 1 or not fanouts or any(n < 1 for n in fanouts):
        raise ValueError("iterations, warm probes, concurrency and fanout sizes must be positive")
    samples = []
    for _ in range(iterations):
        # The service decides environment reuse; report the actual environment identities.
        samples.extend(invoke(client, function, qualifier) for _ in range(1 + warm_runs))
    report = {
        "function": function, "qualifier": qualifier, "iterations": iterations,
        "warm_runs": warm_runs, "concurrency": concurrency,
        "first_invocation_count": sum(s["first_invocation"] for s in samples),
        "cold_client_ms": summary(s["client_ms"] for s in samples if s["first_invocation"]),
        "warm_client_ms": summary(s["client_ms"] for s in samples if not s["first_invocation"]),
        "init_ms": summary(s["init_ms"] for s in samples if "init_ms" in s),
        "duration_ms": summary(s["duration_ms"] for s in samples if "duration_ms" in s),
        "work_ms": summary(s["work_ms"] for s in samples), "raw": samples, "fanout": {},
    }
    # Reuse a client and thread pool, rather than timing SDK startup for every request.
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for count in fanouts:
            batches = []
            for _ in range(iterations):
                start = time.perf_counter()
                tasks = [pool.submit(invoke, client, function, qualifier) for _ in range(count)]
                children = [task.result() for task in tasks]
                batches.append({"wall_ms": (time.perf_counter() - start) * 1000,
                                "cold_children": sum(s["first_invocation"] for s in children), "raw": children})
            report["fanout"][str(count)] = {
                "wall_ms": summary(b["wall_ms"] for b in batches),
                "all_cold_ms": summary(b["wall_ms"] for b in batches if b["cold_children"] == count),
                "all_warm_ms": summary(b["wall_ms"] for b in batches if b["cold_children"] == 0),
                "mixed_ms": summary(b["wall_ms"] for b in batches if 0 < b["cold_children"] < count),
                "raw": batches,
            }
    return report


def comparison(qemu, aws):
    rows = [
        {"metric": "first execution completed", "qemu": qemu["single"]["cold_latency_ms"],
         "lambda": aws["cold_client_ms"], "boundary": "QEMU worker-local; Lambda SDK includes network and service routing"},
        {"metric": "warm execution completed", "qemu": qemu["warm_latency_ms"],
         "lambda": aws["warm_client_ms"], "boundary": "QEMU serial dispatch; Lambda SDK includes network and service routing"},
    ]
    for count, local in qemu["fanout"].items():
        if count in aws["fanout"]:
            cloud = aws["fanout"][count]
            rows.append({"metric": "fanout " + count, "qemu": local["fanout_ms"],
                         "lambda": cloud["wall_ms"], "lambda_all_cold": cloud["all_cold_ms"],
                         "lambda_all_warm": cloud["all_warm_ms"],
                         "boundary": "QEMU fresh coordinating guest plus fresh branches; Lambda client batch with observed environment reuse"})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="Invokes an existing Lambda; normal AWS invocation charges apply.")
    parser.add_argument("--function", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--qualifier")
    parser.add_argument("--qemu", type=Path, default=Path("benchmark-qemu.json"))
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warm-runs", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--output", type=Path, default=Path("benchmark-comparison.json"))
    args = parser.parse_args(argv)
    qemu = json.loads(args.qemu.read_text())
    import boto3
    from botocore.config import Config
    client = boto3.client("lambda", region_name=args.region,
        config=Config(max_pool_connections=args.concurrency, retries={"total_max_attempts": 1},
                      connect_timeout=10, read_timeout=30))
    try:
        options = {"FunctionName": args.function}
        if args.qualifier:
            options["Qualifier"] = args.qualifier
        configuration = client.get_function_configuration(**options)
        report = collect(client, args.function, iterations=args.iterations, warm_runs=args.warm_runs,
                         fanouts=tuple(int(n) for n in qemu["fanout"]), concurrency=args.concurrency, qualifier=args.qualifier)
    finally:
        client.close()
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "region": args.region,
        "lambda_configuration": {k: configuration.get(k) for k in ("Runtime", "Architectures", "MemorySize", "Timeout", "SnapStart", "Version")},
        "qemu_source": str(args.qemu), "comparison": comparison(qemu, report), "lambda": report,
        "notes": [
            "Workload is the 100-iteration loop and prints in examples/HELLO.MOS; only use a matching QEMU workload report.",
            "first_invocation marks the first handler call in an environment; this does not establish when provisioning occurred.",
            "No cold start is forced; empty cold groups remain missing, never zero.",
            "QEMU total_ms is excluded because that invocation also includes warm probes and cleanup.",
            "Lambda Init Duration is runtime/function initialization, not the complete guest boot boundary.",
            "SDK timing includes network; QEMU is local. Treat this as a client/platform comparison, not isolated VM overhead.",
            "QEMU fanout always boots fresh children. Lambda batch samples record warm/cold environment counts.",
            "Lambda CPU allocation depends on memory. A 128MB Lambda does not match a full local vCPU.",
            "QEMU raw report lacks concurrency metadata; set --concurrency to the value used for that run.",
            "Five samples are a quick check; nearest-rank p95 of five samples equals the maximum.",
        ]}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print("Metric | QEMU p50 ms | Lambda p50 ms | Lambda samples")
    for row in result["comparison"]:
        cloud = row["lambda"]
        print(f'{row["metric"]} | {row["qemu"].get("p50", "missing")} | {cloud.get("p50", "missing")} | {cloud["samples"]}')
    print("Saved", args.output, "— read boundary notes before making speed claims.")


if __name__ == "__main__":
    main()
