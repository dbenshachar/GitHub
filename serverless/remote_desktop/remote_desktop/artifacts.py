from __future__ import annotations

import asyncio
from pathlib import Path


class ArtifactError(RuntimeError):
    pass


class MiniOSArtifacts:
    def __init__(
        self,
        root: Path,
        make_bin: str = "make",
        build: bool = True,
        *,
        tool_dir: Path | None = None,
        host_cc: str = "cc",
    ):
        self.root = root
        self.make_bin = make_bin
        self.build = build
        self.tool_dir = tool_dir
        self.host_cc = host_cc
        self._mkfat32: Path | None = None

    @property
    def kernel(self) -> Path:
        return self.root / "kernel.elf"

    @property
    def mkfat32(self) -> Path:
        if self._mkfat32 is not None:
            return self._mkfat32
        return self.root / "out" / "mkfat32"

    async def prepare(self) -> None:
        if not self.root.exists():
            raise ArtifactError(f"Mini OS root does not exist: {self.root}")
        if not (self.root / "Makefile").exists():
            raise ArtifactError(f"Mini OS Makefile not found in: {self.root}")

        if self.build:
            proc = await asyncio.create_subprocess_exec(
                self.make_bin,
                "-s",
                "kernel.elf",
                cwd=self.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                detail = out.decode(errors="replace").strip()
                raise ArtifactError(f"Mini OS build failed: {detail}")

        await self._prepare_mkfat32()

        missing = [str(path) for path in (self.kernel, self.mkfat32) if not path.exists()]
        if missing:
            raise ArtifactError("Missing Mini OS artifact(s): " + ", ".join(missing))

    async def create_disk(self, disk_path: Path, *, disk_bytes: int | None = None) -> None:
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            str(self.mkfat32),
            str(disk_path),
            cwd=self.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            detail = out.decode(errors="replace").strip()
            raise ArtifactError(f"Mini OS disk creation failed: {detail}")
        if disk_bytes is not None and disk_bytes > disk_path.stat().st_size:
            with disk_path.open("r+b") as disk:
                disk.truncate(disk_bytes)

    async def _prepare_mkfat32(self) -> None:
        repo_tool = self.root / "out" / "mkfat32"
        if repo_tool.exists():
            self._mkfat32 = repo_tool
            return

        source = self.root / "tools" / "mkfat32.c"
        if not source.exists():
            return
        if self.tool_dir is None:
            return

        self.tool_dir.mkdir(parents=True, exist_ok=True)
        local_tool = self.tool_dir / "mkfat32"
        proc = await asyncio.create_subprocess_exec(
            self.host_cc,
            "-std=c99",
            "-Wall",
            "-Wextra",
            "-o",
            str(local_tool),
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            detail = out.decode(errors="replace").strip()
            raise ArtifactError(f"Mini OS disk formatter build failed: {detail}")
        self._mkfat32 = local_tool
