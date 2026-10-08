from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from .function_benchmark import benchmark
from .function_client import RouterClient
from .functions import FunctionError, FunctionSpec, QemuScheduler


def build_parser():
    parser = argparse.ArgumentParser(prog="ephemeral-functions")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "schedule", "benchmark"):
        command = sub.add_parser(name)
        command.add_argument("script", type=Path, help="Mini OS .MOS script")
        command.add_argument("--network", action="store_true", help="enable guest networking for explicit uploads/downloads")
        command.add_argument("--child", action="append", default=[], type=Path, help="registered child .MOS code file")
        command.add_argument("--router", default=os.environ.get("FUNCTION_ROUTER_URL"))
        command.add_argument("--token-env", default="FUNCTION_TOKEN")
        command.add_argument("--local", action="store_true", help="local QEMU development transport")
        command.add_argument("--mini-os-root", type=Path, default=Path(".mini-runtime"))
        command.add_argument("--memory", type=int, default=128)
        command.add_argument("--cpus", type=float, default=1)
        command.add_argument("--timeout", type=int, default=600)
        command.add_argument("--concurrency", type=int, default=16)
        if name == "schedule":
            command.add_argument("--cron", required=True)
            command.add_argument("--name", required=True)
            command.add_argument("--timezone", default="UTC")
        if name == "benchmark":
            command.add_argument("--iterations", type=int, default=10)
            command.add_argument("--warm-runs", type=int, default=3)
            command.add_argument("--fanout", type=int, nargs="+", default=[1, 4, 16])
            command.add_argument("--output", type=Path)
    return parser


async def execute(args):
    names = [path.name.upper() for path in args.child]
    if len(set(names)) != len(names):
        raise ValueError("duplicate child script names")
    spec = FunctionSpec(args.script.read_text(), dict(zip(names, (p.read_text() for p in args.child))),
                        args.memory, args.cpus, args.timeout, network=args.network)
    if args.local:
        if args.command == "schedule":
            raise ValueError("cron schedules require the Kubernetes router")
        async with QemuScheduler(args.mini_os_root, concurrency=args.concurrency) as scheduler:
            if args.command == "benchmark":
                return await benchmark(scheduler, spec, iterations=args.iterations, warm_runs=args.warm_runs, fanouts=args.fanout)
            return (await scheduler.invoke(spec)).to_dict()
    if not args.router:
        raise ValueError("provide --router or FUNCTION_ROUTER_URL (or --local for development)")
    client = RouterClient(args.router, os.environ[args.token_env])
    if args.command == "schedule":
        return await asyncio.to_thread(client.request, "POST", "/v1/schedules",
            {"spec": spec.to_dict(), "name": args.name, "cron": args.cron, "timezone": args.timezone})
    if args.command == "benchmark":
        return await benchmark(client, spec, iterations=args.iterations, warm_runs=args.warm_runs, fanouts=args.fanout)
    return (await client.invoke(spec)).to_dict()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        value = json.dumps(asyncio.run(execute(args)), indent=2)
        if args.command == "benchmark" and args.output:
            args.output.write_text(value + "\n")
        else:
            print(value)
        return 0
    except (FunctionError, OSError, ValueError, KeyError) as exc:
        print(f"ephemeral-functions: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
