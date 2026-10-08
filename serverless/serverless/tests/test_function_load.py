import asyncio
import contextlib
import io
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from remote_desktop.functions import FunctionError, FunctionSpec, InvocationResult
from remote_desktop.function_client import RouterClient
from tools.function_load_common import request, run, run_stage
from tools.function_load_server import LoadServer


class Scheduler:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, spec):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return InvocationResult(str(self.calls), "hello", {"startup_ms": 1, "queue_ms": 0})


class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = Scheduler()
        self.app = LoadServer(self.scheduler, FunctionSpec('print("hello");'), slots=1, max_pending=2)
        self.server = await asyncio.start_server(self.app.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await self.app.close()

    async def test_execution_slots_queue_and_rejection_are_distinct(self):
        first = asyncio.create_task(request("127.0.0.1", self.port, "POST", "/invoke", 2))
        await self.scheduler.entered.wait()
        second = asyncio.create_task(request("127.0.0.1", self.port, "POST", "/invoke", 2))
        async with asyncio.timeout(2):
            while self.app.pending != 2:
                await asyncio.sleep(.001)
        status, _ = await request("127.0.0.1", self.port, "POST", "/invoke", 2)
        self.assertEqual(status, 503)
        self.assertEqual(self.scheduler.calls, 1)
        await asyncio.sleep(.02)
        self.scheduler.release.set()
        responses = await asyncio.gather(first, second)
        self.assertTrue(all(status == 200 for status, _ in responses))
        self.assertGreater(responses[1][1]["metrics"]["queue_ms"], 10)
        self.assertEqual(self.scheduler.calls, 2)
        self.assertEqual(self.app.peak_active, 1)
        self.assertEqual(self.app.rejected, 1)
        self.assertEqual(self.app.pending, 0)

    async def test_virtual_users_repeat_http_requests_and_drain(self):
        self.scheduler.release.set()
        stage = await run_stage("127.0.0.1", self.port, 2, .05, 2)
        self.assertGreater(stage["completed"], 2)
        self.assertEqual(stage["failed"], 0)
        self.assertEqual(stage["completed"], self.scheduler.calls)
        self.assertEqual(stage["peak_client_inflight"], 2)
        self.assertEqual(self.app.pending, 0)

    async def test_rate_mode_accounts_for_large_population_with_bounded_connections(self):
        self.scheduler.release.set()
        stage = await run_stage("127.0.0.1", self.port, 1_000_000, .05, 2,
                                requests_per_user_minute=.006, max_inflight=2)
        self.assertEqual(stage["planned_requests"], 5)
        self.assertEqual(stage["requests_started"] + stage["generator_dropped_requests"], 5)
        self.assertGreater(stage["completed"], 0)
        self.assertLessEqual(stage["peak_client_inflight"], 2)
        self.assertEqual(stage["completed"] + stage["failed"], stage["requests_started"])
        self.assertEqual(self.app.pending, 0)

    async def test_rate_mode_counts_unsent_arrivals_instead_of_slowing_load(self):
        async def release():
            await self.scheduler.entered.wait()
            await asyncio.sleep(.1)
            self.scheduler.release.set()
        release_task = asyncio.create_task(release())
        stage = await run_stage("127.0.0.1", self.port, 40, .05, 2,
                                requests_per_user_minute=60, max_inflight=1)
        await release_task
        self.assertEqual(stage["planned_requests"], 2)
        self.assertEqual(stage["completed"], 1)
        self.assertEqual(stage["generator_dropped_requests"], 1)
        self.assertEqual(stage["error_fraction"], .5)

    async def test_drain_check_failure_preserves_measured_requests(self):
        self.scheduler.release.set()
        metrics_calls = 0

        async def metrics_failure(host, port, method, path, timeout):
            nonlocal metrics_calls
            if path == "/metrics":
                metrics_calls += 1
                if metrics_calls > 1:
                    return 503, {"error": "metrics unavailable"}
            return await request(host, port, method, path, timeout)

        with patch("tools.function_load_common.request", side_effect=metrics_failure):
            stage = await run_stage("127.0.0.1", self.port, 1, .02, 2)
        self.assertFalse(stage["server_drained"])
        self.assertGreater(stage["completed"], 0)
        self.assertIn("metrics HTTP 503", stage["drain_error"])

    async def test_client_disconnect_cancels_active_invocation_and_drains(self):
        task = asyncio.create_task(request("127.0.0.1", self.port, "POST", "/invoke", .05))
        await self.scheduler.entered.wait()
        with self.assertRaises(TimeoutError):
            await task
        async with asyncio.timeout(2):
            while self.app.pending:
                await asyncio.sleep(.001)
        self.assertEqual(self.app.active, 0)
        self.assertEqual(self.app.cancelled, 1)
        self.assertEqual(self.app.completed, 0)

    async def test_disconnect_removes_queued_request_without_starting_it(self):
        first = asyncio.create_task(request("127.0.0.1", self.port, "POST", "/invoke", 2))
        await self.scheduler.entered.wait()
        with self.assertRaises(TimeoutError):
            await request("127.0.0.1", self.port, "POST", "/invoke", .05)
        async with asyncio.timeout(2):
            while self.app.pending != 1:
                await asyncio.sleep(.001)
        self.assertEqual(self.scheduler.calls, 1)
        self.scheduler.release.set()
        self.assertEqual((await first)[0], 200)
        self.assertEqual(self.app.cancelled, 1)

    async def test_cancel_during_router_submission_deletes_eventually_created_job(self):
        entered, release = threading.Event(), threading.Event()
        deleted = []

        def router_request(method, path, body=None):
            if method == "POST":
                entered.set()
                release.wait(2)
                return {"job_id": "submission-race"}
            deleted.append(path)
            return {"cancelled": "submission-race"}

        client = RouterClient("http://router.invalid", "test-token", threaded_transport=True)
        with patch.object(client, "request", side_effect=router_request):
            task = asyncio.create_task(client.invoke(FunctionSpec('print("hello");')))
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(deleted, ["/v1/jobs/submission-race"])

    async def test_failed_submission_after_repeated_cancel_has_no_unhandled_exception(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: errors.append(context))

        def router_request(*args):
            entered.set()
            release.wait(2)
            raise FunctionError("router HTTP 503")

        try:
            client = RouterClient("http://router.invalid", "test-token", threaded_transport=True)
            with patch.object(client, "request", side_effect=router_request):
                task = asyncio.create_task(client.invoke(FunctionSpec('print("hello");')))
                await asyncio.to_thread(entered.wait, 2)
                task.cancel()
                await asyncio.sleep(.01)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0)
            self.assertEqual(errors, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_router_transport_limits_http_concurrency(self):
        active = peak = sequence = 0
        lock = threading.Lock()

        def router_request(method, path, body=None):
            nonlocal active, peak, sequence
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.005)
                if method == "POST":
                    with lock:
                        sequence += 1
                        return {"job_id": str(sequence)}
                return {"phase": "complete", "results": [{"type": "result", "pod": "pod",
                         "output": "hello", "metrics": {"startup_ms": 1}}]}
            finally:
                with lock:
                    active -= 1

        client = RouterClient("http://router.invalid", "test-token", concurrency=2, threaded_transport=True)
        with patch.object(client, "request", side_effect=router_request):
            results = await asyncio.gather(*(client.invoke(FunctionSpec('print("hello");')) for _ in range(8)))
        self.assertEqual(len(results), 8)
        self.assertLessEqual(peak, 2)

    async def test_router_mode_keeps_results_from_different_execution_nodes(self):
        client = RouterClient("http://router.invalid", "test-token", threaded_transport=True)
        sequence = 0

        def router_request(method, path, body=None):
            nonlocal sequence
            if method == "POST":
                self.assertEqual(body["spec"]["memory_mb"], 8)
                sequence += 1
                return {"job_id": str(sequence)}
            node = "node-a" if int(path.rsplit("/", 1)[1]) % 2 else "node-b"
            return {"phase": "complete", "results": [
                {"type": "result", "pod": "pod-" + node, "node": node,
                 "output": "hello", "metrics": {"startup_ms": 1}}]}

        self.app.scheduler = client
        self.app.spec = FunctionSpec('print("hello");', memory_mb=8)
        self.app.backend = "kubernetes-router"
        with patch.object(client, "request", side_effect=router_request):
            responses = await asyncio.gather(*(
                request("127.0.0.1", self.port, "POST", "/invoke", 2) for _ in range(2)))
        self.assertTrue(all(status == 200 for status, _ in responses))
        self.assertEqual(self.app.worker_nodes, {"node-a": 1, "node-b": 1})
        self.assertIn("Kubernetes placement", self.app.stats()["queue_scope"])

    async def test_million_target_is_not_claimed_when_threshold_reached(self):
        args = SimpleNamespace(url="http://127.0.0.1:8090", users=[1, 4], target_users=1_000_000,
                               generator_max_users=10000, duration=1, request_timeout=2,
                               delay_metric="queue_ms", threshold_seconds=5,
                               max_error_fraction=.01, max_generator_lag_ms=100, output=None,
                               requests_per_user_minute=None, max_inflight=1024)
        stages = [{"users": 1, "error_fraction": 0, "queue_ms": {"p95": 10},
                   "generator_loop_lag_ms": {"p95": 0}},
                  {"users": 4, "error_fraction": 0, "queue_ms": {"p95": 6000},
                   "generator_loop_lag_ms": {"p95": 0}}]
        with patch("tools.function_load_common.run_stage", AsyncMock(side_effect=stages)), contextlib.redirect_stdout(io.StringIO()):
            report = await run(args)
        self.assertFalse(report["target_sustained"])
        self.assertEqual(report["highest_tested_users"], 4)
        self.assertEqual(report["highest_healthy_tested_users"], 1)

    async def test_failures_only_continues_above_latency_and_loop_lag_limits(self):
        args = SimpleNamespace(url="http://127.0.0.1:8090", users=[4096, 8192], target_users=8192,
                               generator_max_users=1000000, duration=15, request_timeout=40,
                               delay_metric="latency_ms", threshold_seconds=10,
                               max_error_fraction=0, max_generator_lag_ms=100, output=None,
                               requests_per_user_minute=1, max_inflight=1024, failures_only=True)
        stages = [{"users": n, "error_fraction": 0, "latency_ms": {"p95": 20000},
                   "generator_loop_lag_ms": {"p95": 1000}} for n in args.users]
        with patch("tools.function_load_common.run_stage", AsyncMock(side_effect=stages)), \
             patch("tools.function_load_common.generator_limit", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            report = await run(args)
        self.assertTrue(report["target_sustained"])
        self.assertEqual(report["highest_tested_users"], 8192)
        self.assertIsNone(report["threshold_seconds"])

        stages[1]["error_fraction"] = .1
        with patch("tools.function_load_common.run_stage", AsyncMock(side_effect=stages)), \
             patch("tools.function_load_common.generator_limit", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            report = await run(args)
        self.assertFalse(report["target_sustained"])
        self.assertEqual(report["highest_healthy_tested_users"], 4096)
        self.assertEqual(report["stop_reason"], "error budget exceeded")


if __name__ == "__main__":
    unittest.main()
