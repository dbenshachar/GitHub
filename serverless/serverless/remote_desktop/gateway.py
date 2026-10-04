from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Awaitable, Callable


class GatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class EdgeAddress:
    host: str
    port: int
    edge_id: str

    @classmethod
    def parse(cls, value: str) -> "EdgeAddress":
        edge_id = value
        target = value
        if "=" in value:
            edge_id, target = value.split("=", 1)
        host, sep, port_s = target.rpartition(":")
        if not sep or not host or not port_s:
            raise ValueError("edge must be HOST:PORT or EDGE_ID=HOST:PORT")
        return cls(host=host, port=int(port_s), edge_id=edge_id)

    @property
    def label(self) -> str:
        return f"{self.edge_id}@{self.host}:{self.port}"


@dataclass
class EdgeTarget:
    address: EdgeAddress
    active: int = 0
    failures: int = 0
    unhealthy_until: float = 0


Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class EdgeGateway:
    """Front door that spreads client TCP sessions across edge routers."""

    def __init__(
        self,
        edges: list[EdgeAddress],
        *,
        connect_timeout: float = 5,
        failure_backoff: float = 1,
        connector: Connector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not edges:
            raise ValueError("at least one edge router is required")
        self.targets = [EdgeTarget(edge) for edge in edges]
        self.connect_timeout = connect_timeout
        self.failure_backoff = failure_backoff
        self.connector = connector or asyncio.open_connection
        self.clock = clock
        self._lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None

    async def serve(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._handle_client, host, port)
        addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        print(f"remote_desktop gateway listening on {addrs}", flush=True)
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        target: EdgeTarget | None = None
        edge_writer: asyncio.StreamWriter | None = None
        try:
            target, edge_reader, edge_writer = await self._connect_to_edge()
            await self._proxy(client_reader, client_writer, edge_reader, edge_writer)
        except GatewayError as exc:
            with suppress(ConnectionError):
                client_writer.write(f"ERR {exc}\n".encode())
                await client_writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            if edge_writer is not None:
                edge_writer.close()
                with suppress(ConnectionError):
                    await edge_writer.wait_closed()
            if target is not None:
                await self._release(target)
            client_writer.close()
            with suppress(ConnectionError):
                await client_writer.wait_closed()

    async def _connect_to_edge(
        self,
    ) -> tuple[EdgeTarget, asyncio.StreamReader, asyncio.StreamWriter]:
        tried: set[str] = set()
        errors: list[str] = []
        while len(tried) < len(self.targets):
            target = await self._choose_target(tried)
            tried.add(target.address.label)
            try:
                reader, writer = await asyncio.wait_for(
                    self.connector(target.address.host, target.address.port),
                    timeout=self.connect_timeout,
                )
            except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
                errors.append(f"{target.address.label}: {exc}")
                await self._mark_failure(target)
                continue
            await self._mark_success(target)
            return target, reader, writer
        detail = "; ".join(errors) if errors else "no edge routers configured"
        raise GatewayError(f"no healthy edge routers ({detail})")

    async def _choose_target(self, tried: set[str]) -> EdgeTarget:
        now = self.clock()
        async with self._lock:
            candidates = [
                target
                for target in self.targets
                if target.address.label not in tried and target.unhealthy_until <= now
            ]
            if not candidates:
                candidates = [target for target in self.targets if target.address.label not in tried]
            if not candidates:
                raise GatewayError("no untried edge routers")
            return min(
                candidates,
                key=lambda target: (
                    target.active,
                    target.unhealthy_until,
                    target.failures,
                    target.address.edge_id,
                ),
            )

    async def _mark_success(self, target: EdgeTarget) -> None:
        async with self._lock:
            target.active += 1
            target.failures = 0
            target.unhealthy_until = 0

    async def _mark_failure(self, target: EdgeTarget) -> None:
        async with self._lock:
            target.failures += 1
            delay = min(30.0, self.failure_backoff * (2 ** min(target.failures - 1, 4)))
            target.unhealthy_until = self.clock() + delay

    async def _release(self, target: EdgeTarget) -> None:
        async with self._lock:
            target.active = max(0, target.active - 1)

    async def _proxy(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        edge_reader: asyncio.StreamReader,
        edge_writer: asyncio.StreamWriter,
    ) -> None:
        to_client = asyncio.create_task(self._pipe(edge_reader, client_writer))
        to_edge = asyncio.create_task(self._pipe(client_reader, edge_writer))
        done, pending = await asyncio.wait(
            {to_client, to_edge},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while not reader.at_eof():
            chunk = await reader.read(4096)
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()

