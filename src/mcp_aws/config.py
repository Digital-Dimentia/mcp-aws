"""Environment-driven settings. Everything has a working default."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _list_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    tool_prefix: str = field(default_factory=lambda: os.environ.get("MCP_AWS_TOOL_PREFIX", "aws_"))
    extra_catalog_dirs: list[Path] = field(
        default_factory=lambda: [Path(p) for p in _list_env("MCP_AWS_CATALOG_DIR")]
    )
    allowed_profiles: list[str] = field(default_factory=lambda: _list_env("MCP_AWS_PROFILES"))
    max_items: int = field(default_factory=lambda: _int_env("MCP_AWS_MAX_ITEMS", 100))
    max_chars: int = field(default_factory=lambda: _int_env("MCP_AWS_MAX_CHARS", 20_000))
    cache_ttl: int = field(default_factory=lambda: _int_env("MCP_AWS_CACHE_TTL", 60))
    expand_max_concurrency: int = field(
        default_factory=lambda: _int_env("MCP_AWS_EXPAND_CONCURRENCY", 8)
    )

    def profile_allowed(self, name: str) -> bool:
        return not self.allowed_profiles or name in self.allowed_profiles


SETTINGS = Settings()
