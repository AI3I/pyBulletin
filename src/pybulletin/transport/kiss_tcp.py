"""KISS-over-TCP link (Dire Wolf, soundmodem, etc.).

Connects to a TCP port that speaks KISS framing over a plain TCP stream.
Reads AX.25 frames and routes them via AX25Router; accepts frames from
the router and writes them to the TCP stream.

Reconnects automatically if the TCP connection drops.

Dire Wolf multi-channel (multi-port) support
--------------------------------------------
Dire Wolf supports up to six simultaneous soundcard channels, each
presented as a distinct KISS port (0–5) on the same TCP connection.
Received frames carry the originating port in the KISS type byte.
Outgoing frames are tagged with the target port so Dire Wolf sends them
on the correct radio channel.

The router passes the KISS port back through ``send_frame(frame, port)``
so that responses and I-frames always leave on the same channel they
arrived on.

Typical Dire Wolf config::

    ADEVICE  plughw:0,0     # VHF (port 0)
    ADEVICE  plughw:1,0     # HF / 300-baud (port 1)
    KISSPORT 8001           # single TCP port for both channels
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

_RECONNECT_DELAY = 5.0   # seconds between reconnect attempts
_READ_CHUNK      = 4096


class KissTcpLink:
    """KISS TCP client — connects to host:port and exchanges AX.25 frames."""

    def __init__(
        self,
        host: str,
        port: int,
        router: AX25Router,
        *,
        reconnect: bool = True,
    ) -> None:
        self._host      = host
        self._port      = port
        self._router    = router
        self._reconnect = reconnect
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="kiss-tcp")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def send_frame(self, frame: AX25Frame, port: int = 0) -> None:
        """Encode *frame* as KISS for *port* and write to the TCP stream.

        *port* should match the port the remote station was heard on so
        Dire Wolf (or any multi-port KISS host) transmits on the correct
        radio channel.
        """
        if self._writer is None or self._writer.is_closing():
            LOG.debug("kiss-tcp: send_frame called but not connected — dropping")
            return
        raw = frame.encode()
        kiss_bytes = encode(raw, port=port)
        async with self._write_lock:
            try:
                self._writer.write(kiss_bytes)
                await self._writer.drain()
            except Exception as exc:
                LOG.warning("kiss-tcp: write error: %s", exc)

    # ------------------------------------------------------------------
    # Internal run loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOG.warning("kiss-tcp: connection error: %s", exc)

            if not self._reconnect:
                break
            LOG.info("kiss-tcp: reconnecting in %.0fs", _RECONNECT_DELAY)
            try:
                await asyncio.sleep(_RECONNECT_DELAY)
            except asyncio.CancelledError:
                break

        LOG.info("kiss-tcp: stopped")

    async def _connect_and_read(self) -> None:
        LOG.info("kiss-tcp: connecting to %s:%d", self._host, self._port)
        reader, writer = await asyncio.open_connection(self._host, self._port)
        self._writer = writer
        LOG.info("kiss-tcp: connected to %s:%d", self._host, self._port)

        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(_READ_CHUNK)
                if not chunk:
                    LOG.info("kiss-tcp: server closed connection")
                    break
                buf.extend(chunk)
                for kiss_port, cmd, data in decode_stream(buf):
                    if cmd == CMD_DATA and data:
                        await self._deliver(data, kiss_port)
        finally:
            self._writer = None
            try:
                writer.close()
            except Exception:
                pass

    async def _deliver(self, data: bytes, port: int) -> None:
        """Decode one AX.25 frame and hand it to the router with its KISS port."""
        from ..ax25.frame import AX25Frame
        try:
            frame = AX25Frame.decode(data)
            LOG.debug("kiss-tcp: RX port=%d %s", port, frame)
            await self._router.handle_frame(frame, port)
        except Exception as exc:
            LOG.debug("kiss-tcp: frame decode error: %s", exc)
