from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from .artifacts import MiniOSArtifacts
from .resources import DEFAULT_PROFILE, ResourceProfile
from .session import MiniOSSession


class NodeRunner:
    """Starts Mini OS sessions on the local machine."""

    def __init__(
        self,
        *,
        artifacts: MiniOSArtifacts,
        work_dir: Path,
        qemu_bin: str,
        replay_bytes: int,
    ):
        self.artifacts = artifacts
        self.work_dir = work_dir
        self.qemu_bin = qemu_bin
        self.replay_bytes = replay_bytes

    async def create_session(
        self,
        name: str | None = None,
        *,
        profile: ResourceProfile = DEFAULT_PROFILE,
        session_id: str | None = None,
        disk_path: Path | None = None,
        create_disk: bool = True,
    ) -> MiniOSSession:
        session_id = session_id or uuid4().hex[:12]
        disk_path = disk_path or self.work_dir / "sessions" / f"{session_id}.img"
        session = MiniOSSession(
            session_id=session_id,
            name=name,
            artifacts=self.artifacts,
            qemu_bin=self.qemu_bin,
            disk_path=disk_path,
            profile=profile,
            create_disk=create_disk,
            replay_bytes=self.replay_bytes,
        )
        await session.start()
        return session
