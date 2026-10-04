import base64
from io import BytesIO
import unittest

from tools.benchmark_lambda import collect, comparison, invoke


class FakeLambda:
    def __init__(self):
        self.first = True
        self.calls = []

    def invoke(self, **options):
        import json
        first = self.first
        self.first = False
        self.calls.append(options)
        log = "REPORT RequestId: abc Duration: 0.3 ms Billed Duration: 10 ms Max Memory Used: 40 MB"
        if first:
            log += " Init Duration: 12 ms"
        return {"StatusCode": 200, "ExecutedVersion": "1",
                "Payload": BytesIO(json.dumps({"result": 100, "first_invocation": first,
                                               "environment_id": "a", "work_ms": .01}).encode()),
                "LogResult": base64.b64encode(log.encode()).decode()}


class LambdaBenchmarkTests(unittest.TestCase):
    def test_completed_synchronous_invocation_and_report_boundaries(self):
        client = FakeLambda()
        result = invoke(client, "existing", "1")
        self.assertEqual(client.calls[0]["InvocationType"], "RequestResponse")
        self.assertEqual(client.calls[0]["Qualifier"], "1")
        self.assertEqual(result["init_ms"], 12)
        self.assertEqual(result["duration_ms"], .3)
        self.assertEqual(result["billed_ms"], 10)
        self.assertTrue(result["first_invocation"])

    def test_missing_cold_batches_are_not_zero_and_comparison_omits_total(self):
        result = collect(FakeLambda(), "existing", iterations=2, warm_runs=2, fanouts=(1, 4), concurrency=1)
        self.assertEqual(result["cold_client_ms"]["samples"], 1)
        self.assertEqual(result["warm_client_ms"]["samples"], 5)
        self.assertEqual(result["fanout"]["4"]["all_cold_ms"], {"samples": 0, "raw": []})
        self.assertEqual(result["fanout"]["4"]["all_warm_ms"]["samples"], 2)
        local = {"single": {"cold_latency_ms": {"p50": 39}, "total_ms": {"p50": 110}},
                 "warm_latency_ms": {"p50": .8}, "fanout": {"4": {"fanout_ms": {"p50": 229}}}}
        rows = comparison(local, result)
        self.assertEqual(rows[0]["qemu"]["p50"], 39)
        self.assertEqual(rows[-1]["qemu"]["p50"], 229)

    def test_function_failure_is_not_counted_as_fast_success(self):
        class Failing(FakeLambda):
            def invoke(self, **options):
                result = super().invoke(**options)
                result["FunctionError"] = "Unhandled"
                return result
        with self.assertRaisesRegex(RuntimeError, "failed"):
            invoke(Failing(), "existing")
