from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    memory_mb: int
    disk_mb: int

    @property
    def memory_arg(self) -> str:
        return f"{self.memory_mb}M"

    @property
    def disk_bytes(self) -> int:
        return self.disk_mb * 1024 * 1024


PROFILES: dict[str, ResourceProfile] = {
    "tiny": ResourceProfile("tiny", memory_mb=128, disk_mb=64),
    "small": ResourceProfile("small", memory_mb=512, disk_mb=512),
    "medium": ResourceProfile("medium", memory_mb=1024, disk_mb=2048),
    "large": ResourceProfile("large", memory_mb=4096, disk_mb=8192),
}

DEFAULT_PROFILE = PROFILES["tiny"]


def parse_profile(name: str | None) -> ResourceProfile:
    if name is None or name == "":
        return DEFAULT_PROFILE
    try:
        return PROFILES[name]
    except KeyError as exc:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown resource profile: {name}; valid profiles: {valid}") from exc

