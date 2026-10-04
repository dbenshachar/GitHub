from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Protocol

from .router import RouterError
from .session import SessionInfo


HELP = """OK commands: HELP, PING, NEW [name] [profile=PROFILE], LIST, RESIZE <session_id> <profile>, ATTACH <session_id>, CLOSE <session_id>
"""


class DesktopRouter(Protocol):
    async def new_session(self, name: str | None = None, profile_name: str | None = None):
        ...

    async def list_sessions(self) -> list[SessionInfo]:
        ...

    async def close(self, session_id: str) -> None:
        ...

    async def resize(self, session_id: str, profile_name: str) -> SessionInfo:
        ...

    async def attach(
        self,
        session_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        ...


class TcpRouter:
    def __init__(self, router: DesktopRouter):
        self.router = router
        self._server: asyncio.AbstractServer | None = None

    async def serve(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._handle_client, host, port)
        addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        print(f"remote_desktop listening on {addrs}", flush=True)
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(b"remote_desktop ready. Type HELP.\n")
        await writer.drain()
        try:
            while not reader.at_eof():
                writer.write(b"rd> ")
                await writer.drain()
                line = await reader.readline()
                if not line:
                    break
                keep_control = await self._handle_command(line.decode(errors="replace").strip(), reader, writer)
                if not keep_control:
                    break
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def _handle_command(
        self,
        line: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        if not line:
            return True
        command, _, rest = line.partition(" ")
        command = command.upper()
        arg = rest.strip()

        try:
            if command == "HELP":
                writer.write(HELP.encode())
                await writer.drain()
                return True
            if command == "PING":
                writer.write(b"PONG\n")
                await writer.drain()
                return True
            if command == "NEW":
                name, profile = _parse_new_args(arg)
                session = await self.router.new_session(name, profile)
                writer.write(f"SESSION {session.session_id}\n".encode())
                await writer.drain()
                return True
            if command == "LIST":
                sessions = await self.router.list_sessions()
                if not sessions:
                    writer.write(b"NO SESSIONS\n")
                for info in sessions:
                    name = info.name or "-"
                    node = info.node_id or "local"
                    writer.write(
                        (
                            f"{info.session_id} {info.state.value} node={node} "
                            f"profile={info.profile} mem={info.memory_mb}MB disk={info.disk_mb}MB "
                            f"pid={info.pid or '-'} name={name}\n"
                        ).encode()
                    )
                await writer.drain()
                return True
            if command == "RESIZE":
                parts = arg.split()
                if len(parts) != 2:
                    writer.write(b"ERR RESIZE requires a session_id and profile\n")
                    await writer.drain()
                    return True
                info = await self.router.resize(parts[0], parts[1])
                writer.write(
                    (
                        f"RESIZED {info.session_id} profile={info.profile} "
                        f"mem={info.memory_mb}MB disk={info.disk_mb}MB\n"
                    ).encode()
                )
                await writer.drain()
                return True
            if command == "CLOSE":
                if not arg:
                    writer.write(b"ERR CLOSE requires a session_id\n")
                    await writer.drain()
                    return True
                await self.router.close(arg)
                writer.write(b"OK closed\n")
                await writer.drain()
                return True
            if command == "ATTACH":
                if not arg:
                    writer.write(b"ERR ATTACH requires a session_id\n")
                    await writer.drain()
                    return True
                writer.write(f"ATTACHED {arg}\n".encode())
                await writer.drain()
                await self.router.attach(arg, reader, writer)
                return False
            writer.write(b"ERR unknown command\n")
            await writer.drain()
            return True
        except RouterError as exc:
            writer.write(f"ERR {exc}\n".encode())
            await writer.drain()
            return True
        except ValueError as exc:
            writer.write(f"ERR {exc}\n".encode())
            await writer.drain()
            return True


def _parse_new_args(arg: str) -> tuple[str | None, str | None]:
    if not arg:
        return None, None
    name_parts: list[str] = []
    profile: str | None = None
    for part in arg.split():
        if part.startswith("profile="):
            profile = part.split("=", 1)[1]
        else:
            name_parts.append(part)
    return (" ".join(name_parts) or None), profile
