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
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ax25.router import AX25Router
    from ..config import AppConfig

LOG = logging.getLogger(__name__)

# HOST mode frame marker. The frame format is length-delimited, so payload
# bytes are not escaped.
_HOST_MARK = 0xAA

# HOST mode status bytes
_STATUS_CONNECT    = 0x00
_STATUS_DISCONNECT = 0x01
_STATUS_DATA       = 0x02

_RECONNECT_DELAY = 10.0


@dataclass(frozen=True, slots=True)
class PactorHostFrame:
    channel: int
    status: int
    payload: bytes = b""


def encode_host_frame(channel: int, status: int, payload: bytes = b"") -> bytes:
    """Encode one WA8DED/SCS HOST mode frame.

    The HOST frame length is one byte, so payloads are limited to 255 bytes.
    `PactorLink.send_frame()` chunks larger writes according to configured
    PACLEN before calling this helper.
    """
    if not (0 <= int(channel) <= 255):
        raise ValueError("HOST channel must be between 0 and 255")
    if not (0 <= int(status) <= 255):
        raise ValueError("HOST status must be between 0 and 255")
    if len(payload) > 255:
        raise ValueError("HOST payload cannot exceed 255 bytes")
    return bytes((_HOST_MARK, channel & 0xFF, status & 0xFF, len(payload))) + payload


def decode_host_stream(buf: bytearray) -> list[PactorHostFrame]:
    """Decode and consume complete HOST frames from *buf*.

    Noise before the next 0xAA marker is discarded. Incomplete frames remain in
    *buf* for the next read.
    """
    frames: list[PactorHostFrame] = []
    while True:
        try:
            start = buf.index(_HOST_MARK)
        except ValueError:
            buf.clear()
            return frames
        if start:
            del buf[:start]
        if len(buf) < 4:
            return frames
        length = buf[3]
        total = 4 + length
        if len(buf) < total:
            return frames
        channel = buf[1]
        status = buf[2]
        payload = bytes(buf[4:total])
        del buf[:total]
        frames.append(PactorHostFrame(channel=channel, status=status, payload=payload))


class PactorLink:
    """PACTOR TNC link via SCS HOST mode serial interface.

    This implements serial connection management plus WA8DED/SCS HOST-mode
    frame encode/decode. Full BBS session bridging still requires validation
    against physical SCS hardware.
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
        self._writer = None
        self._write_lock = asyncio.Lock()

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
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None

    async def send_frame(self, data: bytes, channel: int = 1) -> None:
        """Send connected data to *channel* using HOST data frames."""
        if self._writer is None:
            LOG.debug("pactor: send_frame called while modem is disconnected — dropping")
            return
        chunk_size = max(1, min(int(self._paclen), 255))
        async with self._write_lock:
            for offset in range(0, len(data), chunk_size):
                chunk = data[offset:offset + chunk_size]
                self._writer.write(encode_host_frame(channel, _STATUS_DATA, chunk))
            try:
                await self._writer.drain()
            except Exception as exc:
                LOG.warning("pactor: write error: %s", exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                break
            except ImportError:
                LOG.error("pactor: pyserial-asyncio missing; install pybulletin[tnc]")
                break
            except Exception as exc:
                LOG.warning("pactor: connection error: %s", exc)
            LOG.info("pactor: reconnecting in %.0fs", _RECONNECT_DELAY)
            try:
                await asyncio.sleep(_RECONNECT_DELAY)
            except asyncio.CancelledError:
                break
        LOG.info("pactor: stopped")

    async def _connect_and_read(self) -> None:
        try:
            import serial_asyncio  # type: ignore[import]
        except ImportError:
            raise

        LOG.info("pactor: opening %s at %d baud", self._device, self._baud)
        reader, writer = await serial_asyncio.open_serial_connection(
            url=self._device,
            baudrate=self._baud,
        )
        self._writer = writer
        LOG.info(
            "pactor: HOST serial path active on %s; session bridging requires SCS hardware validation",
            self._device,
        )
        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(1024)
                if not chunk:
                    LOG.info("pactor: serial device closed")
                    break
                buf.extend(chunk)
                for frame in decode_host_stream(buf):
                    await self._handle_host_frame(frame)
        finally:
            self._writer = None
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_host_frame(self, frame: PactorHostFrame) -> None:
        if frame.status == _STATUS_CONNECT:
            LOG.info("pactor: channel %d connected %r", frame.channel, frame.payload)
        elif frame.status == _STATUS_DISCONNECT:
            LOG.info("pactor: channel %d disconnected %r", frame.channel, frame.payload)
        elif frame.status == _STATUS_DATA:
            LOG.debug("pactor: channel %d RX %d byte(s)", frame.channel, len(frame.payload))
            # Full BBSSession bridging will land here after validation with an
            # SCS modem. For now we preserve the data path and observability.
        else:
            LOG.debug(
                "pactor: channel %d status=0x%02x payload=%r",
                frame.channel,
                frame.status,
                frame.payload[:40],
            )


def pactor_diagnostics(cfg) -> list[str]:
    lines = [
        f"enabled          : {cfg.enabled}",
        f"device           : {cfg.device or '<unset>'}",
        f"baud             : {cfg.baud}",
        f"paclen           : {cfg.paclen}",
    ]
    try:
        import serial_asyncio  # type: ignore[import]  # noqa: F401
    except Exception:
        lines.append("serial_asyncio   : missing")
    else:
        lines.append("serial_asyncio   : available")
    lines.append("status           : HOST framing implemented; BBS session bridging requires SCS hardware validation")
    return lines
