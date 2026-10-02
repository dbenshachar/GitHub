import asyncio
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from remote_desktop.router import RouterError, SessionRouter
from remote_desktop.resources import DEFAULT_PROFILE
from remote_desktop.session import SessionInfo, SessionState


@dataclass
class FakeSession:
    session_id: str
    name: str | None
    state: SessionState = SessionState.RUNNING
    closed: bool = False

    def info(self):
        return SessionInfo(
            session_id=self.session_id,
            name=self.name,
            state=self.state,
            created_at=datetime.now(timezone.utc),
            disk_path=Path("/tmp/fake.img"),
            pid=123,
            profile=DEFAULT_PROFILE.name,
            memory_mb=DEFAULT_PROFILE.memory_mb,
            disk_mb=DEFAULT_PROFILE.disk_mb,
        )

    async def close(self):
        self.closed = True
        self.state = SessionState.EXITED


class FakeFactory:
    def __init__(self):
        self.count = 0

    async def create_session(
        self,
        name=None,
        *,
        profile=DEFAULT_PROFILE,
        session_id=None,
        disk_path=None,
        create_disk=True,
    ):
        self.count += 1
        return FakeSession(session_id or f"s{self.count}", name)


class SessionRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_and_lists_sessions(self):
        router = SessionRouter(FakeFactory(), max_sessions=2)

        session = await router.new_session("dev")
        infos = await router.list_sessions()

        self.assertEqual(session.session_id, "s1")
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].name, "dev")

    async def test_enforces_session_limit(self):
        router = SessionRouter(FakeFactory(), max_sessions=1)

        await router.new_session()

        with self.assertRaises(RouterError):
            await router.new_session()

    async def test_close_removes_session(self):
        router = SessionRouter(FakeFactory(), max_sessions=1)
        session = await router.new_session()

        await router.close(session.session_id)
        infos = await router.list_sessions()

        self.assertEqual(infos, [])
        self.assertTrue(session.closed)

    async def test_finished_sessions_do_not_count_against_limit(self):
        router = SessionRouter(FakeFactory(), max_sessions=1)
        session = await router.new_session()
        session.state = SessionState.EXITED

        replacement = await router.new_session()

        self.assertEqual(replacement.session_id, "s2")


if __name__ == "__main__":
    unittest.main()
