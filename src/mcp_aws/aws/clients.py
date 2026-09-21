"""Cached boto3 sessions and clients, plus the short-lived result cache."""

from __future__ import annotations

import threading
import time
from typing import Any

import boto3
from botocore.config import Config

from .. import __version__
from ..config import SETTINGS

_BOTO_CONFIG = Config(
    retries={"max_attempts": 4, "mode": "adaptive"},
    connect_timeout=5,
    read_timeout=30,
    user_agent_extra=f"mcp-aws/{__version__}",
)

_lock = threading.Lock()
_sessions: dict[str, boto3.Session] = {}
_clients: dict[tuple[str, str, str | None], Any] = {}


def session_for(profile: str) -> boto3.Session:
    with _lock:
        cached = _sessions.get(profile)
        if cached is None:
            cached = boto3.Session(profile_name=profile)
            _sessions[profile] = cached
        return cached


def client_for(profile: str, service: str, region: str | None = None) -> Any:
    key = (profile, service, region)
    with _lock:
        cached = _clients.get(key)
        if cached is None:
            cached = session_for(profile).client(service, region_name=region, config=_BOTO_CONFIG)
            _clients[key] = cached
        return cached


def default_region(profile: str) -> str | None:
    return session_for(profile).region_name


def reset() -> None:
    """Drop every cached session/client. Used by tests."""
    with _lock:
        _sessions.clear()
        _clients.clear()


class TTLCache:
    """Tiny thread-safe TTL cache. AWS inventory does not change between two turns of a
    conversation, and a model re-asking the same question should not re-bill the API."""

    def __init__(self, ttl: int | None = None) -> None:
        self._ttl = SETTINGS.cache_ttl if ttl is None else ttl
        self._lock = threading.Lock()
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        if self._ttl <= 0:
            return None
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires < time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: Any, value: Any) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
