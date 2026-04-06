"""Web session management for the HTTP/sysop interface.

Sessions are kept in-memory with a configurable TTL.  A background task
prunes expired sessions every few minutes.  Tokens are cryptographically
random URL-safe strings (same generator as the BBS auth module).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ..auth import generate_session_token, verify_password
from ..store.models import PRIV_SYSOP

LOG = logging.getLogger(__name__)

_SESSION_TTL  = 86400    # 24 hours
_PRUNE_EVERY  = 300      # prune expired sessions every 5 minutes


@dataclass
class WebSession:
    token:      str
    call:       str
    privilege:  str
    created_at: float
    expires_at: float

    @property
    def is_sysop(self) -> bool:
        return self.privilege == PRIV_SYSOP

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


class SessionStore:
    """Thread-safe in-memory web session store."""

    def __init__(self, ttl: int = _SESSION_TTL) -> None:
        self._ttl      = ttl
        self._sessions: dict[str, WebSession] = {}
        self._lock     = asyncio.Lock()
        self._prune_task: asyncio.Task | None = None

    def start(self) -> None:
        self._prune_task = asyncio.create_task(self._prune_loop())

    def stop(self) -> None:
        if self._prune_task:
            self._prune_task.cancel()

    async def create(self, call: str, privilege: str) -> WebSession:
        token = generate_session_token()
        now   = time.time()
        sess  = WebSession(
            token=token,
            call=call.upper(),
            privilege=privilege,
            created_at=now,
            expires_at=now + self._ttl,
        )
        async with self._lock:
            self._sessions[token] = sess
        return sess

    async def get(self, token: str) -> WebSession | None:
        async with self._lock:
            sess = self._sessions.get(token)
        if sess and not sess.expired:
            return sess
        if sess:
            await self.revoke(token)
        return None

    async def revoke(self, token: str) -> None:
        async with self._lock:
            self._sessions.pop(token, None)

    async def get_from_request(self, req) -> WebSession | None:
        """Extract session from Bearer token or session cookie."""
        token = req.bearer_token() or req.cookie("pb_session")
        if not token:
            return None
        return await self.get(token)

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(_PRUNE_EVERY)
            now = time.time()
            async with self._lock:
                expired = [t for t, s in self._sessions.items() if s.expires_at < now]
                for t in expired:
                    del self._sessions[t]
            if expired:
                LOG.debug("web: pruned %d expired sessions", len(expired))
