"""Shared fixtures for pyBulletin tests."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pybulletin.store.store import BBSStore
from pybulletin.store.models import User, PRIV_SYSOP, PRIV_USER
from pybulletin.strings import StringCatalog
from pybulletin.config import load_config


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

@pytest.fixture
async def store() -> BBSStore:
    s = BBSStore(":memory:")
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Minimal config / strings
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg():
    import tempfile, os, pathlib
    src = pathlib.Path("config/pybulletin.toml")
    if src.exists():
        return load_config(str(src))
    # Build a minimal in-memory config
    from pybulletin.config import AppConfig
    return AppConfig()


@pytest.fixture
def strings():
    p = "config/strings.toml"
    import pathlib
    if pathlib.Path(p).exists():
        return StringCatalog(p)
    # Return a stub that echoes the key
    class _Stub:
        def get(self, key, **kw):
            return f"\r\n[{key}]\r\n"
    return _Stub()


# ---------------------------------------------------------------------------
# Fake BBSSession for engine tests
# ---------------------------------------------------------------------------

class FakeSession:
    """Minimal BBSSession stand-in for testing CommandEngine handlers."""

    def __init__(self, store: BBSStore, cfg, strings, user: User):
        self.store  = store
        self.cfg    = cfg
        self.strings = strings
        self.user   = user
        self._output: list[str] = []
        self._input:  deque[str] = deque()
        self.meta = MagicMock()
        self.meta.peer    = "127.0.0.1"
        self.meta.channel = "telnet"
        self.heard_provider    = None
        self.conference_hub    = None

    # -- Output --
    async def send(self, text: str) -> None:
        self._output.append(text)

    async def send_paged(self, text: str) -> None:
        self._output.append(text)

    def output(self) -> str:
        return "".join(self._output)

    def clear_output(self) -> None:
        self._output.clear()

    # -- Input --
    def push_input(self, *lines: str) -> None:
        self._input.extend(lines)

    async def _readline(self) -> str:
        if self._input:
            return self._input.popleft()
        return ""

    async def _readline_hidden(self) -> str:
        return await self._readline()

    def send_raw(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


@pytest.fixture
async def user_record(store: BBSStore) -> User:
    """A regular user registered in the store."""
    user = await store.record_login("W1TEST", "127.0.0.1")
    return user


@pytest.fixture
async def sysop_record(store: BBSStore) -> User:
    """A sysop user registered in the store."""
    user = await store.record_login("AI3I", "127.0.0.1")
    user.privilege = PRIV_SYSOP
    await store.upsert_user(user)
    return user


@pytest.fixture
async def fake_session(store, cfg, strings, user_record) -> FakeSession:
    return FakeSession(store, cfg, strings, user_record)


@pytest.fixture
async def sysop_session(store, cfg, strings, sysop_record) -> FakeSession:
    return FakeSession(store, cfg, strings, sysop_record)
