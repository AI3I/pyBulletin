"""KISS-over-serial link (hardware TNC on a serial/USB port).

Requires the optional ``[tnc]`` extra::

    pip install pybulletin[tnc]   # pulls in pyserial-asyncio

Falls back gracefully with an ImportError if pyserial-asyncio is absent,
so the rest of the application still loads on machines without a TNC.

TNC initialisation
------------------
Many hardware TNCs (Kantronics KPC-3/9612, MFJ-1270, TAPR TNC-2 clones,
Kenwood TS-2000) power up in a command-line mode rather than KISS mode.
Configure ``init_cmds`` in the ``[kiss]`` section to send the commands
needed to switch the TNC into KISS mode before the binary read loop begins.

Common init sequences::

    # Kantronics KPC-3 / KPC-9612
    init_cmds = ["INTFACE KISS", "RESET"]

    # TAPR TNC-2, MFJ-1270 / 1274, AEA PK-232 (KISS firmware)
    init_cmds = ["KISS ON"]

    # Kenwood TH-D7 / D710 internal TNC (already speaks KISS via USB)
    init_cmds = []  # leave empty

Usage::

    link = KissSerialLink(
        device="/dev/ttyUSB0",
        baud=9600,
        router=router,
        init_cmds=["INTFACE KISS", "RESET"],
        init_delay_ms=500,
    )
    link.start()
    ...
    await link.stop()
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .kiss import decode_stream, encode, CMD_DATA

if TYPE_CHECKING:
    from ..ax25.router import AX25Router
    from ..ax25.frame import AX25Frame

LOG = logging.getLogger(__name__)

_READ_CHUNK = 512


class KissSerialLink:
    """KISS serial TNC link via pyserial-asyncio."""

    def __init__(
        self,
        device: str,
        baud: int,
        router: AX25Router,
        *,
        init_cmds: list[str] | None = None,
        init_delay_ms: int = 500,
    ) -> None:
        self._device       = device
        self._baud         = baud
        self._router       = router
        self._init_cmds    = init_cmds or []
        self._init_delay   = init_delay_ms / 1000.0
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="kiss-serial")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def send_frame(self, frame: AX25Frame, port: int = 0) -> None:
        if self._writer is None:
            LOG.debug("kiss-serial: send_frame called but not open — dropping")
            return
        raw  = frame.encode()
        kiss = encode(raw, port=port)
        async with self._write_lock:
            try:
                self._writer.write(kiss)
                await self._writer.drain()
            except Exception as exc:
                LOG.warning("kiss-serial: write error: %s", exc)

    # ------------------------------------------------------------------
    # TNC initialisation
    # ------------------------------------------------------------------

    async def _init_tnc(self, writer: asyncio.StreamWriter) -> None:
        """Send text-mode init commands before entering KISS binary mode."""
        if not self._init_cmds:
            return
        LOG.info("kiss-serial: sending TNC init sequence (%d cmd(s))",
                 len(self._init_cmds))
        for cmd in self._init_cmds:
            LOG.debug("kiss-serial: init → %r", cmd)
            writer.write((cmd + "\r").encode("ascii", errors="replace"))
            await writer.drain()
            await asyncio.sleep(self._init_delay)
        # Extra settling time after the last command (e.g. RESET)
        await asyncio.sleep(self._init_delay)
        LOG.info("kiss-serial: TNC init complete — entering KISS mode")

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            import serial_asyncio  # type: ignore[import]
        except ImportError:
            LOG.error(
                "kiss-serial: pyserial-asyncio not installed. "
                "Install with: pip install pybulletin[tnc]"
            )
            return

        LOG.info("kiss-serial: opening %s at %d baud", self._device, self._baud)
        try:
            reader, writer = await serial_asyncio.open_serial_connection(
                url=self._device,
                baudrate=self._baud,
            )
        except Exception as exc:
            LOG.error("kiss-serial: failed to open %s: %s", self._device, exc)
            return

        self._writer = writer
        LOG.info("kiss-serial: opened %s", self._device)

        # Put hardware TNC into KISS mode (no-op for soft TNCs)
        await self._init_tnc(writer)

        buf = bytearray()
        try:
            while True:
                try:
                    chunk = await reader.read(_READ_CHUNK)
                except Exception as exc:
                    LOG.warning("kiss-serial: read error: %s", exc)
                    break
                if not chunk:
                    LOG.info("kiss-serial: EOF on %s", self._device)
                    break
                buf.extend(chunk)
                for port, cmd, data in decode_stream(buf):
                    if cmd == CMD_DATA and data:
                        await self._deliver(data, port)
        except asyncio.CancelledError:
            pass
        finally:
            self._writer = None
            try:
                writer.close()
            except Exception:
                pass
            LOG.info("kiss-serial: closed %s", self._device)

    async def _deliver(self, data: bytes, port: int) -> None:
        from ..ax25.frame import AX25Frame
        try:
            frame = AX25Frame.decode(data)
            LOG.debug("kiss-serial: RX port=%d %s", port, frame)
            await self._router.handle_frame(frame, port)
        except Exception as exc:
            LOG.debug("kiss-serial: frame decode error: %s", exc)
