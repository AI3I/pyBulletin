"""Forward scheduler — iterate configured neighbors, check cron schedule,
dispatch ForwardSession for each neighbor whose window is now open.

Intended to be called from the ``run-forward`` CLI subcommand (one-shot) or
from a long-running periodic task inside ``serve-core`` (future phase).

Usage (one-shot)::

    scheduler = ForwardScheduler(cfg, store)
    sent, received = await scheduler.run_once()

The scheduler respects each neighbor's ``schedule`` cron expression (UTC).
Disabled neighbors (``enabled = false``) are always skipped.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .cron import matches as cron_matches
from .session import ForwardSession

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..store.store import BBSStore

LOG = logging.getLogger(__name__)


class ForwardScheduler:
    """Runs one forwarding pass across all configured neighbors."""

    def __init__(self, cfg: AppConfig, store: BBSStore) -> None:
        self._cfg   = cfg
        self._store = store

    async def run_once(self) -> tuple[int, int]:
        """Check all neighbors and forward to those whose schedule matches now.

        Returns (total_sent, total_received) across all sessions.
        """
        fwd = self._cfg.forward
        if not fwd.enabled:
            LOG.info("scheduler: forwarding disabled in config")
            return 0, 0

        neighbors = [n for n in fwd.neighbors if n.enabled]
        if not neighbors:
            LOG.info("scheduler: no enabled neighbors configured")
            return 0, 0

        total_sent = total_received = 0

        for neighbor in neighbors:
            if not cron_matches(neighbor.schedule):
                LOG.debug("scheduler: %s — schedule %r does not match now, skipping",
                          neighbor.call, neighbor.schedule)
                continue

            LOG.info("scheduler: initiating session with %s", neighbor.call)
            session = ForwardSession(self._cfg, self._store, neighbor)
            try:
                sent, received = await session.run_outgoing()
                total_sent     += sent
                total_received += received
                LOG.info("scheduler: %s — sent %d, received %d",
                         neighbor.call, sent, received)
            except Exception as exc:
                LOG.warning("scheduler: session with %s failed: %s",
                            neighbor.call, exc)

        LOG.info("scheduler: pass complete — sent %d, received %d total",
                 total_sent, total_received)
        return total_sent, total_received
