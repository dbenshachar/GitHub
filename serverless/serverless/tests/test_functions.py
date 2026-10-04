import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from remote_desktop.function_benchmark import benchmark, summary
from remote_desktop.function_kubernetes import APIError, JobRouter
from remote_desktop.function_router import RouterServer
from remote_desktop.functions import FunctionError, FunctionSpec, QemuScheduler

ROOT = Path(__file__).resolve().parents[1]


class FakeAPI:
    namespace = "mini-functions"

    def __init__(self):
        self.calls = []
        self.objects = {}

    def path(self, resource, name=""):
        return "/" + resource + ("/" + name if name else "")

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            key = path + "/" + body["metadata"]["name"]
            if key in self.objects:
                raise APIError(409, "already exists")
            self.objects[key] = json.loads(json.dumps(body))
            return self.objects[key]
        if method == "GET":
            return self.objects[path]
        if method == "DELETE":
            return self.objects.pop(path)
        raise AssertionError(method)

    def list(self, resource, selector):
        return []


def make_router(api=None):
    return JobRouter(api or FakeAPI(), image="registry.example/mini:v1",
                     router_url="http://router:8080", parallelism=8)


class ManifestTests(unittest.TestCase):
    def test_indexed_fanout_is_bounded_ephemeral_and_distributed(self):
        router = make_router()
        job = router.job_manifest(FunctionSpec('print("hello");'), count=32)
        spec = job["spec"]
        self.assertEqual(spec["completionMode"], "Indexed")
        self.assertEqual(spec["completions"], 32)
        self.assertEqual(spec["parallelism"], 8)
        self.assertEqual(spec["activeDeadlineSeconds"], 600)
        self.assertEqual(spec["backoffLimit"], 0)
        self.assertEqual(spec["ttlSecondsAfterFinished"], 600)
        pod = spec["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["topologySpreadConstraints"][0]["topologyKey"], "kubernetes.io/hostname")
        self.assertIn("emptyDir", pod["volumes"][0])
        self.assertNotIn("hostPath", json.dumps(pod))
        self.assertNotIn("docker", json.dumps(pod).lower())
        self.assertEqual(pod["containers"][0]["securityContext"]["capabilities"]["drop"], ["ALL"])

    def test_cron_has_timezone_overlap_control_and_ten_minute_deadline(self):
        manifest = make_router().cron_manifest(FunctionSpec("print(1);"), name="every-ten-minutes",
                                              cron="*/10 * * * *", timezone="America/Los_Angeles")
        spec = manifest["spec"]
        self.assertEqual(spec["schedule"], "*/10 * * * *")
        self.assertEqual(spec["timeZone"], "America/Los_Angeles")
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        self.assertEqual(spec["jobTemplate"]["spec"]["activeDeadlineSeconds"], 600)
        self.assertNotIn("mini-root", spec["jobTemplate"]["spec"]["template"]["metadata"]["labels"])

    def test_script_fanout_uses_registered_code_and_identity_across_replicas(self):
        api = FakeAPI()
        parent = FunctionSpec('print("parent");', {"CHILD.MOS": "print(2);"}, memory_mb=64)
        job = make_router(api).job_manifest(parent, name="parent")
        pod = {"metadata": {"name": "pod", "uid": "uid-123", "labels": job["metadata"]["labels"]},
               "spec": job["spec"]["template"]["spec"], "status": {"phase": "Running"}}
        api.objects["/pods/pod"] = pod
        one = make_router(api).fanout("pod", "uid-123", 4, "CHILD.MOS", 0)
        two = make_router(api).fanout("pod", "uid-123", 4, "CHILD.MOS", 0)
        self.assertEqual(one, two)
        child = api.objects["/jobs/" + one["job_id"]]
        env = child["spec"]["template"]["spec"]["containers"][0]["env"]
        child_spec = json.loads(next(v["value"] for v in env if v["name"] == "FUNCTION_SPEC"))
        self.assertEqual(child_spec["script"], "print(2);")
        self.assertEqual(child_spec["memory_mb"], 64)
        self.assertEqual(set(child_spec), {"script", "scripts", "memory_mb", "cpus", "timeout_s", "benchmark_warm_runs"})
        with self.assertRaisesRegex(ValueError, "identity"):
            make_router(api).fanout("pod", "wrong-uid", 4, "CHILD.MOS", 1)
        with self.assertRaisesRegex(ValueError, "unknown child"):
            make_router(api).fanout("pod", "uid-123", 4, "NOPE.MOS", 1)
        pod["metadata"]["labels"]["mini-depth"] = "8"
        with self.assertRaisesRegex(ValueError, "depth"):
            make_router(api).fanout("pod", "uid-123", 4, "CHILD.MOS", 1)

    def test_invalid_specs_and_limits_are_rejected(self):
        for options in ({"timeout_s": 601}, {"timeout_s": 0}, {"timeout_s": 1.5},
                        {"cpus": float("nan")}, {"scripts": {"../X.MOS": "print(1);"}},
                        {"script": "a\0b"}, {"memory_mb": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                FunctionSpec(**{"script": "print(1);", **options})
        with self.assertRaises(ValueError):
            make_router().job_manifest(FunctionSpec(""), count=257)

    def test_percentiles_keep_raw_samples_and_missing_data(self):
        data = summary([4, 1, 3, 2])
        self.assertEqual(data["p50"], 2)
        self.assertEqual(data["p95"], 4)
        self.assertEqual(data["raw"], [4, 1, 3, 2])
        self.assertEqual(summary([]), {"samples": 0, "raw": []})


@unittest.skipUnless(os.environ.get("FUNCTION_NETWORK_TESTS") == "1", "opt-in loopback HTTP tests")
class RouterHTTPTests(unittest.TestCase):
    def test_auth_validation_submission_and_shared_backend(self):
        token = "t" * 32
        server = RouterServer(("127.0.0.1", 0), make_router(), token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/v1/jobs"
        try:
            payload = json.dumps({"spec": FunctionSpec("print(1);").to_dict()}).encode()
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, payload), timeout=5)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()
            with urlopen(Request(url, payload, {"Authorization": "Bearer " + token}), timeout=5) as response:
                self.assertEqual(response.status, 202)
                self.assertIn("job_id", json.load(response))
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, b'{}', {"Authorization": "Bearer " + token}), timeout=5)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@unittest.skipUnless(os.environ.get("FUNCTION_QEMU_TESTS") == "1", "opt-in real Mini OS/QEMU tests")
class QemuTests(unittest.IsolatedAsyncioTestCase):
    def scheduler(self, **kwargs):
        return QemuScheduler(Path(os.environ.get("FUNCTION_MINI_OS_ROOT", ROOT / ".mini-runtime")), **kwargs)

    async def test_actual_script_file_runs_in_fresh_guest(self):
        async with self.scheduler() as scheduler:
            result = await scheduler.invoke(FunctionSpec('print("hello");'))
            self.assertIn("hello\n", result.output)
            self.assertGreater(result.metrics["boot_ms"], 0)
            self.assertGreater(result.metrics["cold_latency_ms"], result.metrics["startup_ms"])

    async def test_failure_stops_guest_before_next_statement(self):
        for command in ("cat /ABSENT.TXT;", "invalidcommand;", "calc 1 / 0;", 'let fd = open("/ABSENT.TXT", O_RDONLY);'):
            async with self.scheduler() as scheduler:
                with self.assertRaises(FunctionError) as error:
                    await scheduler.invoke(FunctionSpec(command + 'print("SHOULD_NEVER_RUN");'))
                self.assertIn("failed", str(error.exception))
                self.assertNotIn("SHOULD_NEVER_RUN", str(error.exception))

    async def test_fanout_has_no_parent_output_or_disk_and_one_slot_works(self):
        spec = FunctionSpec('write /STATE.TXT parent-secret; print("parent-prefix"); fanout(3, "CHILD.MOS");',
                            {"CHILD.MOS": 'print("child-only");'})
        async with self.scheduler(concurrency=1) as scheduler:
            result = await scheduler.invoke(spec)
            self.assertEqual(len(result.children), 3)
            for child in result.children:
                self.assertIn("child-only", child.output)
                self.assertNotIn("parent-prefix", child.output)
            # Looking for a parent-created file in a child must fail.
            with self.assertRaises(FunctionError):
                await scheduler.invoke(replace(spec, scripts={"CHILD.MOS": "cat /STATE.TXT;"}))

    async def test_nested_fanout_and_limit_rejection(self):
        spec = FunctionSpec('fanout(2, "MIDDLE.MOS");', {"MIDDLE.MOS": 'fanout(2, "LEAF.MOS");', "LEAF.MOS": "print(7);"})
        async with self.scheduler(concurrency=1) as scheduler:
            result = await scheduler.invoke(spec)
            self.assertEqual([len(child.children) for child in result.children], [2, 2])
        async with self.scheduler(max_depth=0) as scheduler:
            with self.assertRaises(FunctionError):
                await scheduler.invoke(spec)

    async def test_real_benchmark_measures_startup_cold_warm_and_fanout(self):
        async with self.scheduler(concurrency=2) as scheduler:
            report = await benchmark(scheduler, FunctionSpec("print(42);"), iterations=2, warm_runs=2, fanouts=(1, 2, 16))
        self.assertEqual(report["single"]["startup_ms"]["samples"], 2)
        self.assertEqual(report["warm_latency_ms"]["samples"], 4)
        self.assertGreater(report["fanout"]["2"]["fanout_ms"]["p50"], 0)
        self.assertEqual(report["fanout"]["2"]["child_startup_ms"]["samples"], 4)
        self.assertEqual(report["fanout"]["16"]["child_startup_ms"]["samples"], 32)

    async def test_deadline_and_cancellation_cleanup(self):
        # CPU exhaustion stops at the interpreter step limit or the job deadline.
        async with self.scheduler() as scheduler:
            with self.assertRaises(FunctionError):
                await scheduler.invoke(FunctionSpec("while (1) { print(1); }", timeout_s=1))
            task = asyncio.create_task(scheduler.invoke(FunctionSpec("while (1) { print(1); }")))
            await asyncio.sleep(.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(scheduler._tasks)


@unittest.skipUnless(os.environ.get("FUNCTION_KUBERNETES_TESTS") == "1", "opt-in deployed Kubernetes tests")
class KubernetesIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_distributed_job_benchmark(self):
        from remote_desktop.function_client import RouterClient
        client = RouterClient(os.environ["FUNCTION_ROUTER_URL"], os.environ["FUNCTION_TOKEN"])
        report = await benchmark(client, FunctionSpec("print(42);"), iterations=2, warm_runs=2, fanouts=(1, 4))
        self.assertEqual(report["fanout"]["4"]["child_startup_ms"]["samples"], 8)
        self.assertGreater(report["single"]["startup_ms"]["p50"], 0)
