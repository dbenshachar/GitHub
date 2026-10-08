"""Exercise resource admission, cancellation, balancing and the replacement ramp."""
import asyncio
from pathlib import Path
import os
import tempfile
import contextlib
import io
from unittest.mock import AsyncMock, patch
import threading
import time
from types import SimpleNamespace
import unittest

from remote_desktop.function_pool_router import PoolRouter
from remote_desktop.function_rpc import RPCError, Server, request
from remote_desktop.function_service import Worker
from remote_desktop.functions import FunctionSpec
from tools.load_test_user_rate import benchmark, levels, population, stage

TOKEN = 'capacity-test-token-' + 'x' * 32


class Headroom:
    def __init__(self, cpu=2, memory=2048):
        self.cpu, self.memory = cpu, memory

    def snapshot(self):
        return {'cpu_available_cores': self.cpu, 'cpu_used_cores': 0,
                'cpu_limit_cores': 2, 'memory_available_mb': self.memory}


def fast_guest(spec, root, submit, stop_event=None):
    time.sleep(.002)
    return {'output': 'hello', 'metrics': {'startup_ms': 1, 'execution_ms': 1}}


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_eight_guests_and_no_fixed_cpu_reservations(self):
        stop = threading.Event()

        def blocked_guest(spec, root, submit, stop_event=None):
            while not stop.wait(.005):
                if stop_event.is_set():
                    raise RuntimeError('cancelled')
            return fast_guest(spec, root, submit)

        worker = Worker(Path('.'), TOKEN, '', cpu_budget=2, memory_budget_mb=2048,
                        resources=Headroom(), runner=blocked_guest)
        worker.ready = True
        body = {'spec': FunctionSpec('print(1);', memory_mb=8, cpus=.25).to_dict()}
        try:
            for _ in range(8):
                await worker.dispatch('POST', '/v1/jobs', body)
            await asyncio.sleep(.21)  # Newly launching guests become measured workloads.
            for _ in range(8):
                await worker.dispatch('POST', '/v1/jobs', body)
            self.assertEqual(worker.active, 16)
            self.assertGreater(worker.cpu_used, worker.cpu_budget)
            self.assertEqual(worker.memory_used, 16 * 40)
        finally:
            stop.set()
            await worker.close(grace=2)
        self.assertEqual(worker.active, 0)
        self.assertEqual(worker.memory_used, 0)

    async def test_cpu_or_memory_exhaustion_rejects_without_queue(self):
        for headroom in (Headroom(cpu=0), Headroom(memory=0)):
            worker = Worker(Path('.'), TOKEN, '', resources=headroom, runner=fast_guest)
            worker.ready = True
            with self.assertRaises(RPCError) as raised:
                await worker.dispatch('POST', '/v1/jobs', {'spec': FunctionSpec('print(1);', memory_mb=8).to_dict()})
            self.assertEqual(raised.exception.status, 429)
            self.assertEqual(worker.active, 0)
            self.assertFalse(worker.records)

    async def test_cancel_before_execution_releases_memory(self):
        worker = Worker(Path('.'), TOKEN, '', resources=Headroom(), runner=fast_guest)
        worker.ready = True
        _, reply = await worker.dispatch('POST', '/v1/jobs', {'spec': FunctionSpec('print(1);', memory_mb=8).to_dict()})
        await worker.dispatch('DELETE', '/v1/jobs/' + reply['job_id'], None)
        self.assertEqual(worker.active, 0)
        self.assertEqual(worker.memory_used, 0)

    def test_levels_start_at_4096_and_end_exactly_at_one_million(self):
        self.assertEqual(levels(4096, 1000000), [4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1000000])

    def test_worker_population_requires_distinct_hosts(self):
        with self.assertRaises(RuntimeError):
            population({'workers': [{'name': 'a', 'node': 'same', 'ready': True},
                                    {'name': 'b', 'node': 'same', 'ready': True}]}, 2)

    async def test_resource_rejections_do_not_stop_the_replacement_ramp(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / 'HELLO.MOS'
            script.write_text('print(1);')
            args = SimpleNamespace(script=script, memory=8, cpus=.25, job_timeout=30,
                requests_per_user_minute=1, duration=15, servers=5, start_users=4096,
                target_users=8192, max_error_fraction=.01, output=Path(folder) / 'report.json')
            results = [{'users': n, 'workers_stable': True, 'generator_dropped': 0,
                        'error_fraction': 1} for n in (4096, 8192)]
            with patch('tools.load_test_user_rate.stage', AsyncMock(side_effect=results)) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                report = await benchmark(args, TOKEN)
            self.assertEqual(run.await_count, 2)
            self.assertEqual(report['highest_tested_users'], 8192)
            self.assertEqual(report['highest_passing_users'], 0)
            self.assertTrue(args.output.exists())


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers, self.rpcs, self.workers = [], [], []
        endpoints = {}
        for index in range(2):
            worker = Worker(Path('.'), TOKEN, '', name=f'worker-{index}', node=f'host-{index}',
                            resources=Headroom(), runner=fast_guest)
            worker.ready = True
            rpc = Server(worker.dispatch, TOKEN)
            server = await asyncio.start_server(rpc.handle, '127.0.0.1', 0)
            self.servers.append(server)
            self.rpcs.append(rpc)
            self.workers.append(worker)
            endpoints[worker.name] = {'url': f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'}
        self.router = PoolRouter(TOKEN, workers=endpoints)
        await self.router.refresh()
        rpc = Server(self.router.dispatch, TOKEN)
        server = await asyncio.start_server(rpc.handle, '127.0.0.1', 0)
        self.rpcs.append(rpc)
        self.servers.append(server)
        self.url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        for worker in self.workers:
            worker.router_url = self.url
        self.discovery = asyncio.create_task(self.router.watch())

    async def asyncTearDown(self):
        self.discovery.cancel()
        await asyncio.gather(self.discovery, return_exceptions=True)
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for rpc in reversed(self.rpcs):
            await rpc.close()
        for worker in self.workers:
            await worker.close(grace=.2)

    async def test_direct_invocations_balance_across_servers(self):
        values = await asyncio.gather(*(request(self.url, TOKEN, 'POST', '/v1/invoke',
                    {'spec': FunctionSpec('print(1);', memory_mb=8, cpus=.01).to_dict()}) for _ in range(12)))
        self.assertEqual({v['metrics']['worker_node'] for v in values}, {'host-0', 'host-1'})
        self.assertTrue(all(v['metrics']['queue_ms'] == 0 for v in values))
        self.assertTrue(all(worker.active == 0 for worker in self.workers))

    async def test_balancer_retries_only_explicit_resource_rejections(self):
        self.workers[0].resources.cpu = 0
        value = await request(self.url, TOKEN, 'POST', '/v1/invoke',
                              {'spec': FunctionSpec('print(1);', memory_mb=8).to_dict()})
        self.assertEqual(value['metrics']['worker_node'], 'host-1')
        self.assertEqual(self.workers[0].completed, 0)

    async def test_new_ramp_measures_real_direct_requests(self):
        args = SimpleNamespace(url=self.url, servers=2, requests_per_user_minute=60,
                               duration=.15, max_inflight=64, request_timeout=2)
        result = await stage(args, TOKEN, FunctionSpec('print(1);', memory_mb=8, cpus=.01), 64)
        self.assertEqual(result['planned_requests'], 10)
        self.assertEqual(result['sent'], 10)
        self.assertEqual(result['completed'], 10)
        self.assertEqual(result['generator_dropped'], 0)
        self.assertTrue(result['workers_stable'])
        self.assertEqual(len(result['worker_node_completions']), 2)

    @unittest.skipUnless(os.environ.get('FUNCTION_QEMU_TESTS') == '1', 'opt-in real QEMU smoke test')
    async def test_fresh_qemu_guests_and_fanout(self):
        from remote_desktop.function_worker import execute
        root = Path(os.environ.get('MINI_RUNTIME', '.mini-runtime')).resolve()
        self.assertTrue((root / 'job-runtime-v1').is_file())
        for worker in self.workers:
            worker.root, worker.runner = root, execute
        spec = FunctionSpec('print("parent"); fanout(2, "CHILD.MOS");',
                            scripts={'CHILD.MOS': 'print("child");'}, memory_mb=8, cpus=.01, timeout_s=10)
        result = await request(self.url, TOKEN, 'POST', '/v1/invoke', {'spec': spec.to_dict()}, timeout=15)
        self.assertIn('parent', result['output'])
        self.assertEqual(len(result['children']), 1)
        children = result['children'][0]['children']
        self.assertEqual(len(children), 2)
        self.assertTrue(all('child' in child['output'] for child in children))
        self.assertNotEqual(children[0]['invocation_id'], children[1]['invocation_id'])
