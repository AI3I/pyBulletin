"""Line-oriented stream adapter over an AX25Connection.

Provides the same reader/writer interface as transport/telnet.py so that
BBSSession is transport-agnostic.  Data arriving as I-frame payloads is
buffered and split on CR or LF to produce lines.

Usage::

    reader = AX25Reader(conn, meta)
    writer = AX25Writer(conn)
    session = BBSSession(reader, writer, meta, cfg, store, strings)
    await session.run()
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connection import AX25Connection

LOG = logging.getLogger(__name__)


class AX25Meta:
    """Mirrors transport.telnet.ConnectionMeta for the AX.25 channel."""
    def __init__(self, remote_call: str, peer: str = "") -> None:
        self.peer          = peer or remote_call
        self.terminal_type = "ax25"
        self.cols          = 80
        self.rows          = 24
        self.channel       = "ax25"


class AX25Reader:
    """Adapts AX25Connection.read() into a line-at-a-time interface."""

    def __init__(self, conn: AX25Connection, meta: AX25Meta) -> None:
        self._conn = conn
        self._meta = meta
        self._buf  = bytearray()

    def has_replies(self) -> bool:
        # AX.25 has no IAC negotiation
        return False

    def take_replies(self) -> bytes:
        return b""

    async def readline(self) -> str:
        """Return the next complete line (strips CR/LF)."""
        while True:
            # Check buffer for a complete line
            for sep in (b"\r\n", b"\r", b"\n"):
                idx = self._buf.find(sep)
                if idx >= 0:
                    line = self._buf[:idx]
                    del self._buf[:idx + len(sep)]
                    return line.decode("ascii", errors="replace")

            # Need more data
            chunk = await self._conn.read()
            if not chunk:
                # Connection closed — return whatever is buffered
                line = bytes(self._buf)
                self._buf.clear()
                return line.decode("ascii", errors="replace")
            self._buf.extend(chunk)

    async def readbytes(self, n: int) -> bytes:
        """Read exactly *n* bytes from the AX.25 I-frame stream.

        Used by binary protocols (YAPP) after the text handshake.
        """
        while len(self._buf) < n:
            chunk = await self._conn.read()
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(self._buf), n)
            self._buf.extend(chunk)
        result = bytes(self._buf[:n])
        del self._buf[:n]
        return result


class AX25Writer:
    """Adapts AX25Connection.write() into a send_line / send / drain interface."""

    def __init__(self, conn: AX25Connection) -> None:
        self._conn = conn
        self._pending = bytearray()

    def writebytes(self, data: bytes) -> None:
        """Queue raw binary data (used by YAPP and other binary protocols)."""
        self._pending.extend(data)

    def send_raw(self, data: bytes) -> None:
        # AX.25 has no IAC — ignore raw IAC bytes from the session layer
        pass

    def send_line(self, text: str) -> None:
        self._pending.extend(text.encode("ascii", errors="replace") + b"\r\n")

    def send(self, text: str) -> None:
        self._pending.extend(text.encode("ascii", errors="replace"))

    async def drain(self) -> None:
        if self._pending:
            await self._conn.write(bytes(self._pending))
            self._pending.clear()

    def close(self) -> None:
        asyncio.create_task(self._conn.disconnect())

    async def wait_closed(self) -> None:
        pass  # disconnect is fire-and-forget here

    @property
    def peer(self) -> str:
        return str(self._conn.remote_addr)
