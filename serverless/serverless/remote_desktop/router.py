from __future__ import annotations

import asyncio
import shutil
from contextlib import suppress
from typing import Protocol

from .resources import ResourceProfile, parse_profile
from .session import MiniOSSession, SessionInfo, SessionState


class SessionFactory(Protocol):
    async def create_session(
        self,
        name: str | None = None,
        *,
        profile: ResourceProfile,
        session_id: str | None = None,
        disk_path,
        create_disk: bool,
    ) -> MiniOSSession:
        ...


class RouterError(RuntimeError):
    pass


class SessionRouter:
    def __init__(self, factory: SessionFactory, *, max_sessions: int = 16):
        self.factory = factory
        self.max_sessions = max_sessions
        self._sessions: dict[str, MiniOSSession] = {}
        self._lock = asyncio.Lock()

    async def new_session(self, name: str | None = None, profile_name: str | None = None) -> MiniOSSession:
        profile = parse_profile(profile_name)
        async with self._lock:
            self._forget_finished_locked()
            if len(self._sessions) >= self.max_sessions:
                raise RouterError(f"session limit reached ({self.max_sessions})")
            session = await self.factory.create_session(
                name,
                profile=profile,
                session_id=None,
                disk_path=None,
                create_disk=True,
            )
            self._sessions[session.session_id] = session
            return session

    async def get(self, session_id: str) -> MiniOSSession:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise RouterError(f"unknown session: {session_id}")
            return session

    async def close(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise RouterError(f"unknown session: {session_id}")
        await session.close()

    async def resize(self, session_id: str, profile_name: str) -> SessionInfo:
        profile = parse_profile(profile_name)
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise RouterError(f"unknown session: {session_id}")
            if session.state != SessionState.RUNNING:
                raise RouterError(f"session is not running: {session.state.value}")
            if session.profile.name == profile.name:
                return session.info()
            session.state = SessionState.RESIZING

        replacement_id = f"{session_id}-resize"
        replacement_disk = session.disk_path.with_name(f"{session_id}-{profile.name}.img")
        await asyncio.to_thread(shutil.copyfile, session.disk_path, replacement_disk)
        if replacement_disk.stat().st_size < profile.disk_bytes:
            with replacement_disk.open("r+b") as disk:
                disk.truncate(profile.disk_bytes)

        try:
            replacement = await self.factory.create_session(
                session.name,
                profile=profile,
                session_id=replacement_id,
                disk_path=replacement_disk,
                create_disk=False,
            )
        except Exception:
            async with self._lock:
                if self._sessions.get(session_id) is session:
                    session.state = SessionState.RUNNING
            raise

        await session.close()
        replacement.session_id = session_id
        replacement.created_at = session.created_at

        async with self._lock:
            self._sessions[session_id] = replacement
        return replacement.info()

    async def list_sessions(self) -> list[SessionInfo]:
        async with self._lock:
            self._forget_finished_locked()
            return [session.info() for session in self._sessions.values()]

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)

    async def attach(
        self,
        session_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        session = await self.get(session_id)
        queue = session.subscribe(replay=True)
        to_client = asyncio.create_task(self._session_to_client(queue, writer))
        to_session = asyncio.create_task(self._client_to_session(reader, session))
        done, pending = await asyncio.wait(
            {to_client, to_session},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)
        session.unsubscribe(queue)

    def _forget_finished_locked(self) -> None:
        finished = [
            session_id
            for session_id, session in self._sessions.items()
            if session.state in {SessionState.EXITED, SessionState.FAILED}
        ]
        for session_id in finished:
            del self._sessions[session_id]

    async def _session_to_client(
        self,
        queue: asyncio.Queue[bytes | None],
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            writer.write(chunk)
            await writer.drain()

    async def _client_to_session(
        self,
        reader: asyncio.StreamReader,
        session: MiniOSSession,
    ) -> None:
        while not reader.at_eof():
            chunk = await reader.read(4096)
            if not chunk:
                return
            with suppress(RuntimeError):
                await session.write(chunk)
