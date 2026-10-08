"""Direct five-server resource-capacity ramp, without invocation queues."""
import argparse
import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
import resource
import time

from remote_desktop.function_rpc import RPCError, request
from remote_desktop.functions import FunctionSpec


def levels(start, target):
    values = []
    while start < target:
        values.append(start)
        start *= 2
    return values + [target]


def distribution(values):
    values = sorted(values)
    return {'samples': len(values), **{label: values[max(0, math.ceil(len(values) * fraction) - 1)]
        if values else None for label, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]}}


def population(stats, expected):
    workers = [worker for worker in stats.get('workers', []) if worker.get('ready')]
    if len(workers) != expected or len({worker['node'] for worker in workers}) != expected:
        raise RuntimeError(f'Need exactly {expected} ready worker servers on distinct hosting nodes')
    return {worker['name']: worker['node'] for worker in workers}


async def stage(args, token, spec, users):
    before = await request(args.url, token, 'GET', '/metrics')
    identities = population(before, args.servers)
    if before.get('pending', 0):
        raise RuntimeError('Workers have unfinished requests; wait for cleanup before testing')
    rate = users * args.requests_per_user_minute / 60
    planned = math.ceil(rate * args.duration)
    counters = Counter()
    inflight = set()
    timings = {name: [] for name in ('latency_ms', 'rejection_latency_ms', 'startup_ms',
        'execution_ms', 'worker_total_ms', 'transport_and_router_ms', 'dispatch_lag_ms')}
    failures, nodes = Counter(), Counter()
    snapshots = []
    start = time.perf_counter()
    end = start + args.duration

    async def invoke(due):
        began = time.perf_counter()
        counters['sent'] += 1
        timings['dispatch_lag_ms'].append(max(0, (began - due) * 1000))
        try:
            value = await request(args.url, token, 'POST', '/v1/invoke', {'spec': spec.to_dict()},
                                  timeout=args.request_timeout)
            now = time.perf_counter()
            counters['completed'] += 1
            counters['completed_in_window'] += now <= end
            metrics = value['metrics']
            timings['latency_ms'].append((now - due) * 1000)
            for key in ('startup_ms', 'execution_ms'):
                timings[key].append(metrics[key])
            timings['worker_total_ms'].append(metrics['total_ms'])
            timings['transport_and_router_ms'].append(max(0, (now - began) * 1000 - metrics['total_ms']))
            nodes[metrics['worker_node']] += 1
        except RPCError as exc:
            key = 'resource_rejected' if exc.status == 429 else 'execution_or_transport_failed'
            counters[key] += 1
            if exc.status == 429:
                timings['rejection_latency_ms'].append((time.perf_counter() - due) * 1000)
            failures[f'HTTP {exc.status}: {exc}'] += 1
        except Exception as exc:
            counters['execution_or_transport_failed'] += 1
            failures[f'{type(exc).__name__}: {exc}'] += 1

    async def monitor():
        while True:
            try:
                stats = await request(args.url, token, 'GET', '/metrics', timeout=3)
                snapshots.append({'seconds': time.perf_counter() - start, **stats})
            except Exception as exc:
                snapshots.append({'seconds': time.perf_counter() - start, 'error': str(exc)})
            await asyncio.sleep(.5)

    monitoring = asyncio.create_task(monitor())
    try:
        index = 0
        while index < planned:
            now = time.perf_counter()
            if now >= end:
                counters['generator_dropped'] += planned - index
                break
            due = start + index / rate
            if now < due:
                await asyncio.sleep(min(due - now, .01))
                continue
            due_end = min(planned, math.floor((now - start) * rate) + 1)
            while index < due_end:
                due = start + index / rate
                if len(inflight) >= args.max_inflight or now - due > .1:
                    counters['generator_dropped'] += 1
                else:
                    task = asyncio.create_task(invoke(due))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                    counters['peak_client_inflight'] = max(counters['peak_client_inflight'], len(inflight))
                index += 1
            await asyncio.sleep(0)
        await asyncio.sleep(max(0, end - time.perf_counter()))
        await asyncio.gather(*list(inflight))
        deadline = time.perf_counter() + args.request_timeout
        while True:
            after = await request(args.url, token, 'GET', '/metrics', timeout=3)
            if not after.get('pending', 0):
                break
            if time.perf_counter() >= deadline:
                raise RuntimeError('Workers did not drain after request completion')
            await asyncio.sleep(.5)
    finally:
        monitoring.cancel()
        await asyncio.gather(monitoring, return_exceptions=True)
        outstanding = list(inflight)
        for task in outstanding:
            task.cancel()
        await asyncio.gather(*outstanding, return_exceptions=True)
    def same_population(snapshot):
        try:
            return not snapshot.get('error') and population(snapshot, args.servers) == identities
        except RuntimeError:
            return False
    stable = same_population(after) and all(same_population(s) for s in snapshots)
    errors = sum(counters[k] for k in ('resource_rejected', 'execution_or_transport_failed', 'generator_dropped'))
    return {'users': users, 'offered_rps': rate, 'duration_seconds': args.duration,
            'elapsed_with_drain_seconds': time.perf_counter() - start, 'planned_requests': planned,
            **{key: counters[key] for key in ('sent', 'completed', 'resource_rejected',
                'execution_or_transport_failed', 'generator_dropped', 'peak_client_inflight')},
            'error_fraction': errors / max(1, planned),
            'completed_rps_in_window': counters['completed_in_window'] / args.duration,
            'workers_stable': stable, 'worker_node_completions': dict(nodes), 'failures': dict(failures),
            **{key: distribution(values) for key, values in timings.items()}, 'resource_timeline': snapshots}


async def benchmark(args, token):
    spec = FunctionSpec(args.script.read_text(), memory_mb=args.memory, cpus=args.cpus, timeout_s=args.job_timeout)
    report = {'load_model': 'periodic_staggered_arrivals', 'requests_per_user_minute': args.requests_per_user_minute,
              'partial_period_sample': args.duration * args.requests_per_user_minute < 60,
              'admission': 'measured CPU and memory; no execution slots or admission queue',
              'expected_servers': args.servers, 'target_users': args.target_users, 'stages': [],
              'notes': ['Every success boots a fresh Mini OS/QEMU guest.',
                        'Logical users specify traffic, not simultaneous open connections.',
                        'A partial-period sample does not establish sustained full-population capacity.']}
    try:
        for users in levels(args.start_users, args.target_users):
            print(f'Testing {users:,} users: {users * args.requests_per_user_minute / 60:.1f} requests/s for {args.duration:g}s', flush=True)
            value = await stage(args, token, spec, users)
            report['stages'].append(value)
            print(json.dumps({k: v for k, v in value.items() if k != 'resource_timeline'}, indent=2), flush=True)
            if not value['workers_stable']:
                report['stop_reason'] = 'worker population changed; capacity result is invalid'
                break
            if value['generator_dropped']:
                report['stop_reason'] = 'generator could not send the requested rate'
                break
        else:
            report['stop_reason'] = 'all user levels tested'
    except Exception as exc:
        report['stop_reason'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['highest_tested_users'] = max((s['users'] for s in report['stages']), default=0)
        report['highest_passing_users'] = max((s['users'] for s in report['stages']
            if s['workers_stable'] and s['error_fraction'] <= args.max_error_fraction and not s['generator_dropped']), default=0)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print('\nFinal report:', flush=True)
        print(json.dumps({k: v for k, v in report.items() if k != 'stages'}, indent=2), flush=True)
        print(f'Full metrics and resource timelines: {args.output.resolve()}', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8080')
    parser.add_argument('--script', type=Path, default=Path('examples/HELLO.MOS'))
    for name, default in [('start-users', 4096), ('target-users', 1000000), ('servers', 5),
                          ('max-inflight', 8192), ('job-timeout', 30), ('memory', 8)]:
        parser.add_argument('--' + name, type=int, default=default)
    for name, default in [('requests-per-user-minute', 1), ('duration', 15), ('request-timeout', 40), ('cpus', .25), ('max-error-fraction', .01)]:
        parser.add_argument('--' + name, type=float, default=default)
    parser.add_argument('--output', type=Path, default=Path('load-worker-pool.json'))
    args = parser.parse_args()
    if not 1 <= args.start_users <= args.target_users or min(args.servers, args.max_inflight) < 1:
        parser.error('require positive counts and start-users <= target-users')
    if any(not math.isfinite(n) or n <= 0 for n in (args.requests_per_user_minute, args.duration, args.request_timeout)) or not 0 <= args.max_error_fraction <= 1:
        parser.error('rates/durations must be positive and error fraction between zero and one')
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and soft < args.max_inflight + 128:
        parser.error(f'load generator needs {args.max_inflight + 128} open files; run ulimit -n 16384')
    token = os.environ.get('FUNCTION_TOKEN')
    if not token:
        parser.error('set FUNCTION_TOKEN or run make test')
    try:
        asyncio.run(benchmark(args, token))
    except (OSError, ValueError, RuntimeError, RPCError) as exc:
        parser.exit(1, f'Benchmark failed: {exc}\n')


if __name__ == '__main__':
    main()
