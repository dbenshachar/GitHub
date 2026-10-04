from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    host: str
    port: int
    mini_os_root: Path
    work_dir: Path
    qemu_bin: str
    make_bin: str
    build: bool
    max_sessions: int
    replay_bytes: int

    @classmethod
    def default(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int = 8023,
        mini_os_root: Path | str = "auto",
        work_dir: Path | str = ".remote_desktop",
        qemu_bin: str = "qemu-system-aarch64",
        make_bin: str = "make",
        build: bool = True,
        max_sessions: int = 16,
        replay_bytes: int = 8192,
    ) -> "ServiceConfig":
        return cls(
            host=host,
            port=port,
            mini_os_root=resolve_mini_os_root(mini_os_root),
            work_dir=Path(work_dir).expanduser().resolve(),
            qemu_bin=qemu_bin,
            make_bin=make_bin,
            build=build,
            max_sessions=max_sessions,
            replay_bytes=replay_bytes,
        )


def resolve_mini_os_root(value: Path | str) -> Path:
    raw = str(value)
    if raw != "auto":
        path = Path(value).expanduser().resolve()
        if path.exists():
            return path
        if raw == "../mini_os":
            sibling = Path(__file__).resolve().parents[3] / "mini_os"
            if sibling.exists():
                return sibling
        return path

    candidates = [
        Path.cwd() / "../mini_os",
        Path(__file__).resolve().parents[3] / "mini_os",
        Path.cwd() / "../../mini_os",
    ]
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.exists():
            return resolved
    return (Path.cwd() / "../mini_os").resolve()
