from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .router import RouterError, SessionRouter
from .runner import NodeRunner
from .session import SessionInfo, SessionState


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeAddress:
    host: str
    port: int
    node_id: str

    @classmethod
    def parse(cls, value: str) -> "NodeAddress":
        node_id = value
        target = value
        if "=" in value:
            node_id, target = value.split("=", 1)
        host, sep, port_s = target.rpartition(":")
        if not sep or not host or not port_s:
            raise ValueError("node must be HOST:PORT or NODE_ID=HOST:PORT")
        return cls(host=host, port=int(port_s), node_id=node_id)

    @property
    def label(self) -> str:
        return f"{self.node_id}@{self.host}:{self.port}"


@dataclass(frozen=True)
class NodeStats:
    node_id: str
    host: str
    port: int
    capacity: int
    running: int
    sessions: list[SessionInfo]

    @property
    def available(self) -> int:
        return max(0, self.capacity - self.running)


def session_to_dict(info: SessionInfo, *, node_id: str | None = None) -> dict[str, Any]:
    data = {
        "session_id": info.session_id,
        "name": info.name,
        "state": info.state.value,
        "created_at": info.created_at.isoformat(),
        "disk_path": str(info.disk_path),
        "pid": info.pid,
        "profile": info.profile,
        "memory_mb": info.memory_mb,
        "disk_mb": info.disk_mb,
    }
    if node_id is not None:
        data["node_id"] = node_id
    return data


def session_from_dict(data: dict[str, Any]) -> SessionInfo:
    return SessionInfo(
        session_id=str(data["session_id"]),
        name=data.get("name"),
        state=SessionState(str(data["state"])),
        created_at=datetime.fromisoformat(str(data["created_at"])),
        disk_path=Path(str(data["disk_path"])),
        pid=data.get("pid"),
        node_id=data.get("node_id"),
        profile=str(data.get("profile", "tiny")),
        memory_mb=int(data.get("memory_mb", 128)),
        disk_mb=int(data.get("disk_mb", 64)),
    )


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await reader.readline()
    if not line:
        raise ProtocolError("connection closed")
    try:
        msg = json.loads(line.decode())
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid json: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("message must be a json object")
    return msg


async def write_message(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    writer.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
    await writer.drain()


class NodeAgentServer:
    """Worker-side service that owns local Mini OS sessions."""

    def __init__(self, *, node_id: str, host: str, port: int, router: SessionRouter):
        self.node_id = node_id
        self.host = host
        self.port = port
        self.router = router
        self._server: asyncio.AbstractServer | None = None

    @classmethod
    def from_runner(
        cls,
        *,
        node_id: str,
        host: str,
        port: int,
        runner: NodeRunner,
        max_sessions: int,
    ) -> "NodeAgentServer":
        return cls(
            node_id=node_id,
            host=host,
            port=port,
            router=SessionRouter(runner, max_sessions=max_sessions),
        )

    async def serve(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        print(f"remote_desktop node {self.node_id} listening on {addrs}", flush=True)
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        await self.router.close_all()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            msg = await read_message(reader)
            op = str(msg.get("op", ""))
            if op == "stats":
                await self._stats(writer)
                return
            if op == "list":
                await self._list(writer)
                return
            if op == "create":
                session = await self.router.new_session(msg.get("name"), msg.get("profile"))
                await write_message(
                    writer,
                    {"ok": True, "session": session_to_dict(session.info(), node_id=self.node_id)},
                )
                return
            if op == "resize":
                info = await self.router.resize(str(msg["session_id"]), str(msg["profile"]))
                await write_message(
                    writer,
                    {"ok": True, "session": session_to_dict(info, node_id=self.node_id)},
                )
                return
            if op == "close":
                await self.router.close(str(msg["session_id"]))
                await write_message(writer, {"ok": True})
                return
            if op == "attach":
                session_id = str(msg["session_id"])
                await self.router.get(session_id)
                await write_message(writer, {"ok": True, "session_id": session_id})
                await self.router.attach(session_id, reader, writer)
                return
            await write_message(writer, {"ok": False, "error": f"unknown op: {op}"})
        except (KeyError, RouterError, ProtocolError, ValueError) as exc:
            with suppress(ConnectionError):
                await write_message(writer, {"ok": False, "error": str(exc)})
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def _stats(self, writer: asyncio.StreamWriter) -> None:
        sessions = await self.router.list_sessions()
        await write_message(
            writer,
            {
                "ok": True,
                "node_id": self.node_id,
                "host": self.host,
                "port": self.port,
                "capacity": self.router.max_sessions,
                "running": len(sessions),
                "sessions": [session_to_dict(info, node_id=self.node_id) for info in sessions],
            },
        )

    async def _list(self, writer: asyncio.StreamWriter) -> None:
        sessions = await self.router.list_sessions()
        await write_message(
            writer,
            {
                "ok": True,
                "sessions": [session_to_dict(info, node_id=self.node_id) for info in sessions],
            },
        )


class NodeClient:
    def __init__(self, address: NodeAddress, *, timeout: float = 5):
        self.address = address
        self.timeout = timeout

    async def stats(self) -> NodeStats:
        msg = await self._request({"op": "stats"})
        sessions = [session_from_dict(item) for item in msg.get("sessions", [])]
        return NodeStats(
            node_id=str(msg.get("node_id") or self.address.node_id),
            host=str(msg.get("host") or self.address.host),
            port=int(msg.get("port") or self.address.port),
            capacity=int(msg["capacity"]),
            running=int(msg["running"]),
            sessions=sessions,
        )

    async def list_sessions(self) -> list[SessionInfo]:
        msg = await self._request({"op": "list"})
        return [session_from_dict(item) for item in msg.get("sessions", [])]

    async def create_session(self, name: str | None = None, profile_name: str | None = None) -> SessionInfo:
        msg = await self._request({"op": "create", "name": name, "profile": profile_name})
        return session_from_dict(msg["session"])

    async def close(self, session_id: str) -> None:
        await self._request({"op": "close", "session_id": session_id})

    async def resize(self, session_id: str, profile_name: str) -> SessionInfo:
        msg = await self._request({"op": "resize", "session_id": session_id, "profile": profile_name})
        return session_from_dict(msg["session"])

    async def attach(
        self,
        session_id: str,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.address.host, self.address.port),
            timeout=self.timeout,
        )
        await write_message(writer, {"op": "attach", "session_id": session_id})
        msg = await asyncio.wait_for(read_message(reader), timeout=self.timeout)
        if not msg.get("ok"):
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            raise RouterError(str(msg.get("error", "attach failed")))
        return reader, writer

    async def _request(self, msg: dict[str, Any]) -> dict[str, Any]:
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.address.host, self.address.port),
                timeout=self.timeout,
            )
            await write_message(writer, msg)
            response = await asyncio.wait_for(read_message(reader), timeout=self.timeout)
            if not response.get("ok"):
                raise RouterError(str(response.get("error", "request failed")))
            return response
        except (OSError, asyncio.TimeoutError, ProtocolError) as exc:
            raise RouterError(f"{self.address.label}: {exc}") from exc
        finally:
            if writer is not None:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()


class DistributedSessionRouter:
    """Edge router that schedules sessions onto remote node agents."""

    def __init__(self, nodes: list[NodeClient]):
        if not nodes:
            raise ValueError("at least one node is required")
        self.nodes = nodes
        self._locations: dict[str, NodeClient] = {}

    async def new_session(self, name: str | None = None, profile_name: str | None = None) -> SessionInfo:
        node = await self._choose_node()
        info = await node.create_session(name, profile_name)
        self._locations[info.session_id] = node
        return info

    async def list_sessions(self) -> list[SessionInfo]:
        results = await asyncio.gather(
            *(node.list_sessions() for node in self.nodes),
            return_exceptions=True,
        )
        sessions: list[SessionInfo] = []
        for node, result in zip(self.nodes, results, strict=True):
            if isinstance(result, Exception):
                continue
            for info in result:
                self._locations[info.session_id] = node
                sessions.append(info)
        return sessions

    async def close(self, session_id: str) -> None:
        node = await self._locate(session_id)
        await node.close(session_id)
        self._locations.pop(session_id, None)

    async def resize(self, session_id: str, profile_name: str) -> SessionInfo:
        node = await self._locate(session_id)
        info = await node.resize(session_id, profile_name)
        self._locations[info.session_id] = node
        return info

    async def attach(
        self,
        session_id: str,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        node = await self._locate(session_id)
        node_reader, node_writer = await node.attach(session_id)
        to_client = asyncio.create_task(self._pipe(node_reader, client_writer))
        to_node = asyncio.create_task(self._pipe(client_reader, node_writer))
        done, pending = await asyncio.wait(
            {to_client, to_node},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)
        node_writer.close()
        with suppress(ConnectionError):
            await node_writer.wait_closed()

    async def close_all(self) -> None:
        sessions = await self.list_sessions()
        await asyncio.gather(
            *(self.close(info.session_id) for info in sessions),
            return_exceptions=True,
        )

    async def _choose_node(self) -> NodeClient:
        stats_results = await asyncio.gather(
            *(node.stats() for node in self.nodes),
            return_exceptions=True,
        )
        healthy: list[tuple[NodeClient, NodeStats]] = []
        for node, result in zip(self.nodes, stats_results, strict=True):
            if isinstance(result, Exception):
                continue
            if result.available > 0:
                healthy.append((node, result))
        if not healthy:
            raise RouterError("no healthy node has capacity")
        healthy.sort(key=lambda item: (item[1].running, -item[1].available, item[1].node_id))
        return healthy[0][0]

    async def _locate(self, session_id: str) -> NodeClient:
        node = self._locations.get(session_id)
        if node is not None:
            return node
        await self.list_sessions()
        node = self._locations.get(session_id)
        if node is None:
            raise RouterError(f"unknown session: {session_id}")
        return node

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
