"""Telnet / plain-TCP transport for pyBulletin.

Listens on the configured port(s), performs minimal IAC negotiation, and
hands each connected client off to a session callback as a line-oriented
asyncio stream pair.

IAC negotiation performed:
  - WILL SGA     — suppress go-ahead (full-duplex character mode)
  - DO NAWS      — request terminal window size (optional, stored in metadata)
  - DO TTYPE     — request terminal type

WILL ECHO is NOT sent at connect — the client handles its own local echo.
It is sent only by _readline_hidden() to suppress echo during password entry,
then reverted with WONT ECHO afterward.

The session callback signature::

    async def handle_session(reader: TelnetReader, writer: TelnetWriter,
                              peer: str, meta: ConnectionMeta) -> None: ...

``TelnetReader`` strips all IAC sequences before returning lines.
``TelnetWriter`` wraps ``asyncio.StreamWriter`` and provides ``send_line()``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telnet option codes
# ---------------------------------------------------------------------------
IAC  = 255
DONT = 254
DO   = 253
WONT = 252
WILL = 251
SB   = 250  # subnegotiation begin
SE   = 240  # subnegotiation end

OPT_ECHO  = 1
OPT_SGA   = 3   # suppress go-ahead
OPT_TTYPE = 24  # terminal type
OPT_NAWS  = 31  # negotiate about window size
OPT_LMODE = 34  # linemode

CRLF = b"\r\n"
CR   = b"\r"
LF   = b"\n"


# ---------------------------------------------------------------------------
# Connection metadata
# ---------------------------------------------------------------------------

@dataclass
class ConnectionMeta:
    peer: str = ""
    terminal_type: str = "unknown"
    cols: int = 80
    rows: int = 24
    channel: str = "telnet"


# ---------------------------------------------------------------------------
# TelnetReader — strips IAC sequences, returns clean lines
# ---------------------------------------------------------------------------

class TelnetReader:
    """Wraps asyncio.StreamReader; strips IAC escapes and returns clean text."""

    def __init__(self, raw: asyncio.StreamReader, meta: ConnectionMeta) -> None:
        self._raw = raw
        self._meta = meta
        self._buf: bytearray = bytearray()
        self._iac_replies: bytearray = bytearray()  # queued IAC responses

    def has_replies(self) -> bool:
        return bool(self._iac_replies)

    def take_replies(self) -> bytes:
        data = bytes(self._iac_replies)
        self._iac_replies.clear()
        return data

    async def readline(self) -> str:
        """Read one line, stripping telnet IAC sequences. Returns decoded text."""
        while True:
            # Check if we already have a line in the buffer
            if b"\n" in self._buf or b"\r" in self._buf:
                line = self._extract_line()
                return line.decode("ascii", errors="replace").rstrip("\r\n")

            chunk = await self._raw.read(512)
            if not chunk:
                # EOF
                line = bytes(self._buf)
                self._buf.clear()
                return line.decode("ascii", errors="replace").rstrip("\r\n")

            self._process(chunk)

    async def readbytes(self, n: int) -> bytes:
        """Read exactly *n* raw bytes, draining the text buffer first.

        Used by binary protocols (YAPP) that follow a text handshake.
        Bytes already processed through the IAC stripper come from
        ``_buf``; any remaining bytes are read directly from the raw
        stream (bypassing IAC processing, which is safe because the
        peer has switched to binary mode).
        """
        result = bytearray()
        # Consume already-buffered text data first
        take = min(n, len(self._buf))
        result.extend(self._buf[:take])
        del self._buf[:take]
        # Read any remaining bytes straight from the raw TCP stream
        remaining = n - len(result)
        if remaining > 0:
            chunk = await self._raw.readexactly(remaining)
            result.extend(chunk)
        return bytes(result)

    def _extract_line(self) -> bytes:
        for i, b in enumerate(self._buf):
            if b in (ord("\r"), ord("\n")):
                line = bytes(self._buf[:i])
                # Skip \r\n or just \r or \n
                skip = i + 1
                if i + 1 < len(self._buf) and self._buf[i] == ord("\r") and self._buf[i+1] == ord("\n"):
                    skip = i + 2
                del self._buf[:skip]
                return line
        line = bytes(self._buf)
        self._buf.clear()
        return line

    def _process(self, data: bytes) -> None:
        """Parse data, handling IAC sequences and buffering clean bytes."""
        i = 0
        while i < len(data):
            b = data[i]
            if b == IAC:
                i = self._handle_iac(data, i)
            else:
                self._buf.append(b)
                i += 1

    def _handle_iac(self, data: bytes, i: int) -> int:
        """Handle an IAC sequence starting at index i. Return next index."""
        if i + 1 >= len(data):
            return i + 1  # truncated — skip

        cmd = data[i + 1]

        if cmd == IAC:
            # Escaped literal 255
            self._buf.append(IAC)
            return i + 2

        if cmd in (WILL, WONT, DO, DONT):
            if i + 2 >= len(data):
                return i + 2
            opt = data[i + 2]
            self._handle_option(cmd, opt)
            return i + 3

        if cmd == SB:
            # Subnegotiation — scan to IAC SE
            end = i + 2
            while end < len(data) - 1:
                if data[end] == IAC and data[end + 1] == SE:
                    sub = data[i + 2:end]
                    self._handle_subneg(sub)
                    return end + 2
                end += 1
            return end  # incomplete; skip

        # Unknown IAC command — skip byte
        return i + 2

    def _handle_option(self, cmd: int, opt: int) -> None:
        """Respond to WILL/WONT/DO/DONT from the client."""
        if cmd == WILL and opt == OPT_TTYPE:
            # Client is willing to send terminal type; request it
            self._iac_replies += bytes([IAC, SB, OPT_TTYPE, 1, IAC, SE])
        elif cmd == WILL and opt == OPT_NAWS:
            pass  # window size will arrive as SB
        elif cmd == DO and opt == OPT_ECHO:
            pass  # client accepting server echo — expected
        elif cmd == DO and opt == OPT_SGA:
            pass  # client accepting SGA — expected
        elif cmd == WONT:
            pass  # client refusing something — no action needed
        elif cmd == DONT:
            pass

    def _handle_subneg(self, sub: bytes) -> None:
        """Handle subnegotiation payload."""
        if not sub:
            return
        opt = sub[0]
        if opt == OPT_TTYPE and len(sub) > 2 and sub[1] == 0:
            # IAC SB TTYPE IS <name> IAC SE
            self._meta.terminal_type = sub[2:].decode("ascii", errors="replace").strip("\x00")
        elif opt == OPT_NAWS and len(sub) >= 5:
            # IAC SB NAWS <cols-hi> <cols-lo> <rows-hi> <rows-lo> IAC SE
            self._meta.cols = (sub[1] << 8) | sub[2]
            self._meta.rows = (sub[3] << 8) | sub[4]


# ---------------------------------------------------------------------------
# TelnetWriter — wraps asyncio.StreamWriter, adds send_line()
# ---------------------------------------------------------------------------

class TelnetWriter:
    def __init__(self, raw: asyncio.StreamWriter) -> None:
        self._raw = raw

    def writebytes(self, data: bytes) -> None:
        """Write raw binary data (bypass ASCII encoding — used by YAPP)."""
        self._raw.write(data)

    def send_raw(self, data: bytes) -> None:
        self._raw.write(data)

    def send_line(self, text: str) -> None:
        self._raw.write(text.encode("ascii", errors="replace") + CRLF)

    def send(self, text: str) -> None:
        self._raw.write(text.encode("ascii", errors="replace"))

    async def drain(self) -> None:
        await self._raw.drain()

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass

    async def wait_closed(self) -> None:
        try:
            await self._raw.wait_closed()
        except Exception:
            pass

    @property
    def peer(self) -> str:
        try:
            addr = self._raw.get_extra_info("peername")
            if addr:
                return addr[0]
        except Exception:
            pass
        return "unknown"


# ---------------------------------------------------------------------------
# Initial IAC negotiation burst
# ---------------------------------------------------------------------------

def _build_negotiation() -> bytes:
    """Return IAC bytes to send immediately on connect.

    We intentionally omit WILL SGA so that clients remain in NVT line mode
    (buffered input with local echo).  Character-at-a-time mode (triggered by
    SGA) requires the server to echo input back, which we don't do.
    """
    return bytes([
        IAC, DO,   OPT_NAWS,   # please tell us your window size
        IAC, DO,   OPT_TTYPE,  # please tell us your terminal type
    ])


# ---------------------------------------------------------------------------
# TelnetServer
# ---------------------------------------------------------------------------

SessionHandler = Callable[
    ["TelnetReader", "TelnetWriter", "ConnectionMeta"],
    Awaitable[None],
]


class TelnetServer:
    """Asyncio TCP server that speaks telnet and dispatches to a session handler."""

    def __init__(
        self,
        host: str,
        port: int,
        handler: SessionHandler,
        *,
        max_clients: int = 50,
        idle_timeout: float = 1800.0,
    ) -> None:
        self._host = host
        self._port = port
        self._handler = handler
        self._max_clients = max_clients
        self._idle_timeout = idle_timeout
        self._active: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None

    @property
    def client_count(self) -> int:
        return len(self._active)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._accept,
            self._host,
            self._port,
            reuse_address=True,
        )
        addrs = [s.getsockname() for s in self._server.sockets or []]
        LOG.info("telnet: listening on %s", addrs)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for task in list(self._active):
            task.cancel()
        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)
        LOG.info("telnet: stopped")

    async def _accept(
        self,
        raw_reader: asyncio.StreamReader,
        raw_writer: asyncio.StreamWriter,
    ) -> None:
        peer = "unknown"
        try:
            addr = raw_writer.get_extra_info("peername")
            peer = addr[0] if addr else "unknown"
        except Exception:
            pass

        if len(self._active) >= self._max_clients:
            LOG.warning("telnet: max clients (%d) reached, rejecting %s",
                        self._max_clients, peer)
            try:
                raw_writer.write(b"Too many connections.\r\n")
                await raw_writer.drain()
                raw_writer.close()
            except Exception:
                pass
            return

        meta = ConnectionMeta(peer=peer)
        reader = TelnetReader(raw_reader, meta)
        writer = TelnetWriter(raw_writer)

        # Send initial IAC negotiation burst
        try:
            writer.send_raw(_build_negotiation())
            await writer.drain()
        except Exception:
            writer.close()
            return

        LOG.info("telnet: connect from %s (%d/%d active)",
                 peer, len(self._active) + 1, self._max_clients)

        task = asyncio.create_task(
            self._run_session(reader, writer, meta),
            name=f"telnet-{peer}",
        )
        self._active.add(task)
        task.add_done_callback(self._active.discard)

    async def _run_session(
        self,
        reader: TelnetReader,
        writer: TelnetWriter,
        meta: ConnectionMeta,
    ) -> None:
        peer = meta.peer
        try:
            await asyncio.wait_for(
                self._handler(reader, writer, meta),
                timeout=self._idle_timeout,
            )
        except asyncio.TimeoutError:
            LOG.info("telnet: idle timeout for %s", peer)
            try:
                writer.send_line("\r\nIdle timeout — disconnecting.")
                await writer.drain()
            except Exception:
                pass
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("telnet: unhandled error from %s", peer)
        finally:
            writer.close()
            LOG.info("telnet: disconnect from %s", peer)
