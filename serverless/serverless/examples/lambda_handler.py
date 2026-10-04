"""Lambda counterpart to HELLO.MOS; configure lambda_handler.handler."""
import time
import uuid

_first_invocation = True
_environment_id = uuid.uuid4().hex


def handler(event, context):
    global _first_invocation
    first = _first_invocation
    _first_invocation = False
    target = event.get("n", 100)
    print("Hello from a fresh Mini OS guest")
    n = 0
    start = time.perf_counter()
    while n < target:
        n += 1
    work_ms = (time.perf_counter() - start) * 1000
    print(n)
    return {"result": n, "first_invocation": first, "environment_id": _environment_id,
            "work_ms": work_ms}
