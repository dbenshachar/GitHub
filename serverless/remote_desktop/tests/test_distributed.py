import asyncio
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from remote_desktop.distributed import DistributedSessionRouter, NodeStats, session_from_dict, session_to_dict
from remote_desktop.resources import parse_profile
from remote_desktop.session import SessionInfo, SessionState


def info(session_id, node_id, name=None):
    return SessionInfo(
        session_id=session_id,
        name=name,
        state=SessionState.RUNNING,
        created_at=datetime.now(timezone.utc),
        disk_path=Path("/tmp/session.img"),
        pid=42,
        node_id=node_id,
        profile="tiny",
        memory_mb=128,
        disk_mb=64,
    )


@dataclass
class FakeNode:
    node_id: str
    capacity: int
    sessions: list[SessionInfo] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    async def stats(self):
        return NodeStats(
            node_id=self.node_id,
            host="127.0.0.1",
            port=9000,
            capacity=self.capacity,
            running=len(self.sessions),
            sessions=list(self.sessions),
        )

    async def list_sessions(self):
        return list(self.sessions)

    async def create_session(self, name=None, profile_name=None):
        profile = parse_profile(profile_name)
        created = info(f"{self.node_id}-{len(self.sessions) + 1}", self.node_id, name)
        created = SessionInfo(
            session_id=created.session_id,
            name=created.name,
            state=created.state,
            created_at=created.created_at,
            disk_path=created.disk_path,
            pid=created.pid,
            node_id=created.node_id,
            profile=profile.name,
            memory_mb=profile.memory_mb,
            disk_mb=profile.disk_mb,
        )
        self.sessions.append(created)
        return created

    async def close(self, session_id):
        self.closed.append(session_id)
        self.sessions = [item for item in self.sessions if item.session_id != session_id]

    async def resize(self, session_id, profile_name):
        profile = parse_profile(profile_name)
        for index, item in enumerate(self.sessions):
            if item.session_id == session_id:
                resized = SessionInfo(
                    session_id=item.session_id,
                    name=item.name,
                    state=item.state,
                    created_at=item.created_at,
                    disk_path=item.disk_path,
                    pid=item.pid,
                    node_id=item.node_id,
                    profile=profile.name,
                    memory_mb=profile.memory_mb,
                    disk_mb=profile.disk_mb,
                )
                self.sessions[index] = resized
                return resized
        raise RuntimeError("missing session")


class DistributedRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedules_on_least_loaded_node(self):
        busy = FakeNode("busy", 4, [info("busy-1", "busy"), info("busy-2", "busy")])
        quiet = FakeNode("quiet", 4, [info("quiet-1", "quiet")])
        router = DistributedSessionRouter([busy, quiet])

        created = await router.new_session("work")

        self.assertEqual(created.node_id, "quiet")
        self.assertEqual(created.name, "work")

    async def test_close_uses_remembered_location(self):
        node = FakeNode("node-a", 2)
        router = DistributedSessionRouter([node])
        created = await router.new_session()

        await router.close(created.session_id)

        self.assertEqual(node.closed, [created.session_id])

    async def test_resize_uses_remembered_location(self):
        node = FakeNode("node-a", 2)
        router = DistributedSessionRouter([node])
        created = await router.new_session()

        resized = await router.resize(created.session_id, "small")

        self.assertEqual(resized.session_id, created.session_id)
        self.assertEqual(resized.profile, "small")
        self.assertEqual(resized.memory_mb, 512)

    def test_session_serialization_round_trips_node_id(self):
        original = info("abc", "node-a", "dev")

        parsed = session_from_dict(session_to_dict(original, node_id="node-a"))

        self.assertEqual(parsed.session_id, "abc")
        self.assertEqual(parsed.node_id, "node-a")
        self.assertEqual(parsed.name, "dev")


if __name__ == "__main__":
    unittest.main()
