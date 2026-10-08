"""Cron trigger: execution is routed to the ready worker pool."""
import asyncio
import json
import os

from .function_client import RouterClient
from .functions import FunctionSpec


async def main():
    client = RouterClient(os.environ["FUNCTION_ROUTER_URL"], os.environ["FUNCTION_TOKEN"])
    result = await client.invoke(FunctionSpec(**json.loads(os.environ["FUNCTION_SPEC"])),
                                 count=int(os.environ.get("FUNCTION_COUNT", "1")))
    print(json.dumps(result.to_dict()), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
