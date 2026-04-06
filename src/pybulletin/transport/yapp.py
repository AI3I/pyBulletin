"""YAPP (Yet Another Packet Protocol) binary file transfer.

YAPP is the standard binary file-transfer protocol used between FBB, BPQ,
and Kantronics PBBS nodes, and by packet terminal programs (Hyperterminal,
TelePac, etc.) when downloading files from a BBS over AX.25 or telnet.

Frame format (YAPP basic)
--------------------------
Each YAPP frame on the wire::

    SOH  (0x01)              — frame start
    len_lo  (byte)           — data length, low byte  (LE)
    len_hi  (byte)           — data length, high byte (LE)
    data    (len bytes)      — payload
    checksum (byte)          — XOR of len_lo, len_hi, and all data bytes

An EOF (end-of-file) frame is a frame with zero-length data::

    0x01 0x00 0x00 0x00

An acknowledgment from the receiver is a bare SOH::

    0x01

An abort from either side is a CAN byte::

    0x18

BBS file-transfer handshake
----------------------------
Download (``YG <filename>``)::

    BBS  →  "(YAPP <filename> <size>)\r"
    User →  "(ok)\r"
    BBS  →  <YAPP frames>  (data)
    User →  0x01            (ACK each frame)
    BBS  →  <YAPP EOF>
    User →  0x01            (ACK EOF)

Upload (``YU <filename>``)::

    User →  "YU <filename> <size>\r"
    BBS  →  "(ok)\r"
    BBS  →  wait for YAPP frames from user
    User →  <YAPP frames>  (data)
    BBS  →  0x01            (ACK each frame)
    User →  <YAPP EOF>
    BBS  →  0x01            (ACK EOF)
    BBS  →  "File received: <filename>\r\n"
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol


class _YappReader(Protocol):
    async def readline(self) -> str: ...
    async def readbytes(self, n: int) -> bytes: ...


class _YappWriter(Protocol):
    def writebytes(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...

LOG = logging.getLogger(__name__)

SOH = 0x01
CAN = 0x18   # abort

# Default block size for outgoing frames (max 255 bytes per YAPP spec)
_BLOCK_SIZE = 128
_ACK_TIMEOUT = 30.0     # seconds to wait for an ACK
_FRAME_TIMEOUT = 30.0   # seconds to wait for next frame from sender


def _checksum(data: bytes) -> int:
    """XOR checksum of all bytes."""
    ck = 0
    for b in data:
        ck ^= b
    return ck & 0xFF


def encode_frame(data: bytes) -> bytes:
    """Encode *data* (up to 255 bytes) as one YAPP frame."""
    assert len(data) <= 255
    length = len(data)
    len_lo = length & 0xFF
    len_hi = (length >> 8) & 0xFF
    payload = bytes([len_lo, len_hi]) + data
    ck = _checksum(payload)
    return bytes([SOH]) + payload + bytes([ck])


def encode_eof() -> bytes:
    """Return a YAPP EOF frame (zero-length data, checksum 0)."""
    return bytes([SOH, 0x00, 0x00, 0x00])


def encode_ack() -> bytes:
    """Return a bare SOH ACK byte."""
    return bytes([SOH])


def encode_abort() -> bytes:
    """Return a CAN abort byte."""
    return bytes([CAN])


def encode_file(data: bytes, block_size: int = _BLOCK_SIZE) -> list[bytes]:
    """Encode *data* into a sequence of YAPP frames + EOF frame."""
    frames: list[bytes] = []
    for offset in range(0, len(data), block_size):
        chunk = data[offset:offset + block_size]
        frames.append(encode_frame(chunk))
    frames.append(encode_eof())
    return frames


async def send_file(
    data: bytes,
    writer: _YappWriter,
    reader: _YappReader,
    filename: str,
) -> bool:
    """Send *data* as a YAPP file transfer.

    *writer* and *reader* must implement the ``_YappWriter`` / ``_YappReader``
    protocols (``writebytes``, ``drain``, ``readline``, ``readbytes``).
    Both ``TelnetReader``/``TelnetWriter`` and ``AX25Reader``/``AX25Writer``
    satisfy this interface.

    Returns True on success, False on abort or timeout.
    """
    # Announce
    announcement = f"(YAPP {filename} {len(data)})\r"
    writer.writebytes(announcement.encode("ascii"))
    await writer.drain()

    # Wait for (ok)
    try:
        resp = await asyncio.wait_for(reader.readline(), timeout=_ACK_TIMEOUT)
        resp = resp.strip()
    except asyncio.TimeoutError:
        LOG.warning("yapp: timeout waiting for (ok)")
        return False

    if resp.lower() != "(ok)":
        LOG.warning("yapp: receiver rejected transfer: %r", resp)
        return False

    # Send frames
    frames = encode_file(data)
    for i, frame in enumerate(frames):
        writer.writebytes(frame)
        await writer.drain()
        # Wait for ACK (single SOH byte)
        try:
            ack = await asyncio.wait_for(reader.readbytes(1), timeout=_ACK_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            LOG.warning("yapp: timeout waiting for ACK on frame %d", i)
            return False
        if ack[0] == CAN:
            LOG.warning("yapp: receiver sent ABORT")
            return False
        # Any SOH-like byte is treated as ACK

    return True


async def receive_file(
    writer: _YappWriter,
    reader: _YappReader,
) -> bytes | None:
    """Receive a YAPP file transfer.

    Sends ACK after each frame.  Returns the assembled data on success,
    or None on abort/error.
    """
    buf = bytearray()

    while True:
        # Read SOH
        try:
            soh_byte = await asyncio.wait_for(reader.readbytes(1), _FRAME_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            LOG.warning("yapp: timeout waiting for frame SOH")
            return None

        if soh_byte[0] == CAN:
            LOG.warning("yapp: sender sent ABORT")
            return None
        if soh_byte[0] != SOH:
            LOG.warning("yapp: expected SOH, got 0x%02x", soh_byte[0])
            return None

        # Read length (2 bytes LE)
        try:
            len_bytes = await asyncio.wait_for(reader.readbytes(2), _FRAME_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            LOG.warning("yapp: timeout reading frame length")
            return None

        length = len_bytes[0] | (len_bytes[1] << 8)

        if length == 0:
            # EOF frame — read and discard checksum byte
            try:
                await asyncio.wait_for(reader.readbytes(1), _FRAME_TIMEOUT)
            except Exception:
                pass
            writer.writebytes(encode_ack())
            await writer.drain()
            break

        # Read data + checksum
        try:
            frame_data = await asyncio.wait_for(
                reader.readbytes(length + 1), _FRAME_TIMEOUT
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            LOG.warning("yapp: timeout reading frame data")
            return None

        payload     = bytes([len_bytes[0], len_bytes[1]]) + frame_data[:-1]
        expected_ck = _checksum(payload)
        received_ck = frame_data[-1]

        if expected_ck != received_ck:
            LOG.warning("yapp: checksum error — expected 0x%02x got 0x%02x",
                        expected_ck, received_ck)
            writer.writebytes(encode_abort())
            await writer.drain()
            return None

        buf.extend(frame_data[:-1])  # append data (exclude checksum byte)
        writer.writebytes(encode_ack())
        await writer.drain()

    return bytes(buf)
