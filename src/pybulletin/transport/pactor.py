"""PACTOR TNC transport — SCS HOST mode interface.

PACTOR-I/II/III/IV are proprietary link-layer protocols developed by SCS
(Special Communications Systems).  They are **not** open-source and require
SCS-licensed hardware:

  SCS PTC-IIusb, PTC-IIIusb, PTC-IVex, Dragon

This module implements the WA8DED / SCS HOST mode serial command set that
all SCS modems support via USB or RS-232.  HOST mode provides:

  - Connection management (connect / disconnect / listen)
  - Channel status polling
  - Data transfer (packetised via 0xAA framing)
  - Mode selection (PACTOR-I up to PACTOR-IV as licensed)

Minimum hardware requirements for non-stub operation
------------------------------------------------------
  * SCS PTC-IIusb (PACTOR-I/II/III) or better
  * ``pip install pybulletin[tnc]``  (pulls in pyserial-asyncio)
  * ``[pactor]`` section in config:

    .. code-block:: toml

       [pactor]
       enabled = true
       device  = "/dev/ttyUSB1"
       baud    = 115200

HOST mode framing (WA8DED)
---------------------------
Commands and responses are wrapped in a lightweight frame::

    0xAA  (marker)
    channel (1 byte — 0 for control, 1..n for link channels)
    status  (1 byte)
    length  (1 byte — payload length)
    payload (length bytes)

Data frames (incoming/outgoing connected data) use channel ≥ 1.
Control frames (status, mode change) use channel 0.

Note: full SCS HOST mode documentation is available in the PTC-IIusb
Operating Manual (SCS GmbH, Hanau).  This stub implements enough to
accept incoming connections and hand them to a BBSSession.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Awaitable

if TYPE_CHECKING:
    from ..ax25.router import AX25Router
    from ..config import AppConfig

LOG = logging.getLogger(__name__)

# HOST mode frame marker
_HOST_MARK = 0xAA

# HOST mode status bytes
_STATUS_CONNECT    = 0x00
_STATUS_DISCONNECT = 0x01
_STATUS_DATA       = 0x02

_RECONNECT_DELAY = 10.0


class PactorLink:
    """PACTOR TNC link via SCS HOST mode serial interface.

    Current implementation: **stub** — logs a warning and exits cleanly.
    Full HOST mode implementation requires the SCS PTC hardware manual
    and a physical modem for protocol testing.

    The stub is wired into ``serve-core`` when ``[pactor] enabled = true``
    so that the config section and CLI plumbing are ready for the full
    implementation.  All public methods are present and no-op.
    """

    def __init__(
        self,
        device: str,
        baud: int,
        router: AX25Router,
        *,
        paclen: int = 250,
    ) -> None:
        self._device  = device
        self._baud    = baud
        self._router  = router
        self._paclen  = paclen
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="pactor")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def send_frame(self, data: bytes, channel: int = 1) -> None:
        """Send *data* to the connected station on *channel* (stub: no-op)."""
        pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        LOG.warning(
            "pactor: stub transport started on %s — "
            "full SCS HOST mode implementation pending; "
            "hardware: SCS PTC-IIusb or newer required",
            self._device,
        )
        # Stub: do nothing.  Full implementation would:
        #   1. Open serial port via pyserial-asyncio
        #   2. Send HOST mode entry command (e.g. 0xAA 0x00 0x00 0x01 'H')
        #   3. Poll channel status
        #   4. On incoming connection: create BBSSession via router
        #   5. Bridge session I/O through HOST mode data frames
