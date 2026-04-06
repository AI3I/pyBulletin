"""AX.25 UI beacon — periodic station identification on RF.

Sends a UI frame to BEACON (or configurable destination) at the
configured interval.  Normally APRS-style beacons go to APRS but
a BBS beacon to ID (or CQ) is common practice on packet networks.

The beacon task is started by serve-core when KISS is configured
and beacon.enabled is True.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .. import __version__

if TYPE_CHECKING:
    from .router import AX25Router
    from ..config import AppConfig

LOG = logging.getLogger(__name__)

# Default beacon destination (listened to by most packet nodes)
_BEACON_DEST = "ID"


class BeaconTask:
    """Sends periodic UI beacons via the AX.25 router."""

    def __init__(self, router: AX25Router, cfg: AppConfig) -> None:
        self._router = router
        self._cfg    = cfg
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="ax25-beacon")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        cfg = self._cfg.beacon
        interval = max(60, cfg.interval_seconds)
        LOG.info("beacon: started — interval %ds", interval)
        # Send an initial beacon at startup
        await self._send_beacon()
        while True:
            try:
                await asyncio.sleep(interval)
                await self._send_beacon()
            except asyncio.CancelledError:
                break
            except Exception:
                LOG.exception("beacon: error sending beacon")
        LOG.info("beacon: stopped")

    async def _send_beacon(self) -> None:
        cfg_node   = self._cfg.node
        cfg_beacon = self._cfg.beacon
        text = cfg_beacon.text.format(
            node_call=cfg_node.node_call,
            version=__version__,
        )
        via = [v.strip() for v in cfg_beacon.path.split(",") if v.strip()] \
            if cfg_beacon.path else []
        await self._router.send_ui(
            dest=_BEACON_DEST,
            info=text.encode("ascii", errors="replace"),
            via=via,
        )
        LOG.debug("beacon: sent — %s", text)
