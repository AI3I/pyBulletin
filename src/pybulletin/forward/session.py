"""FBB ASCII forwarding session state machine.

Handles one complete forwarding session with a single neighbor node over
a TCP connection (AX.25 forwarding reuses the same state machine via the
AX25Reader/AX25Writer adapter from ax25/stream.py).

Session flow
------------
1.  Connect (TCP open_connection or receive AX.25 connection)
2.  Called side sends an FBB ASCII SID and prompt; caller replies with its SID
3.  Caller proposes FB lines ending in F>, or FF when empty
4.  Called side replies with FS and receives accepted bodies
5.  Direction reverses; the next proposal or FF acknowledges the prior block
6.  Caller sends FQ and disconnects

A session is initiated (caller role) by ``ForwardSession.run_outgoing()``.
A session is received (called role) by ``ForwardSession.run_incoming()``.
Both paths share the same proposal/transfer inner logic.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from .sid import SID, generate as gen_sid, parse as parse_sid, detect_software
from .protocol import Proposal, parse_fs_response
from .binary import encode_fa, decode_fa
from ..store.models import (
    Message, MSG_PRIVATE, MSG_BULLETIN, MSG_NTS,
)

_RF_CALL = re.compile(r"[A-Z0-9]{1,6}(?:-(?:[0-9]|1[0-5]))?\Z")


def _format_ascii_proposal(msg: Message, *, compressed: bool = False) -> str:
    wire_type = {MSG_BULLETIN: "B", MSG_NTS: "T"}.get(msg.msg_type, "P")
    command = "FA" if compressed else "FB"
    return (f"{command} {wire_type} {msg.from_call} {msg.at_bbs or msg.to_call} "
            f"{msg.to_call} {msg.bid} {msg.size}")


def _parse_ascii_proposal(line: str) -> Proposal | None:
    fields = line.split()
    if len(fields) != 7 or fields[0] not in ("FB", "FA") or fields[1] not in ("P", "B", "T"):
        return None
    try:
        size = int(fields[6])
    except ValueError:
        return None
    return Proposal(
        to_call=fields[4].upper(), from_call=fields[2].upper(),
        bid=fields[5], size=size, date="", subject="",
        at_bbs=fields[3].upper(), msg_type=fields[1],
    )


def inbound_neighbor(forward_config, remote_call: str, peer_host: str = ""):
    """Return an enabled, configured peer or an explicitly allowed unknown peer."""
    from ..config import ForwardNeighborConfig

    call = remote_call.upper()
    if _RF_CALL.fullmatch(call):
        for neighbor in forward_config.neighbors:
            if neighbor.call.upper() == call:
                return neighbor if neighbor.enabled else None
    if peer_host:
        for neighbor in forward_config.neighbors:
            if neighbor.enabled and neighbor.inbound_host == peer_host:
                return neighbor
    if forward_config.allow_unknown_peers and _RF_CALL.fullmatch(call):
        return ForwardNeighborConfig(call=call)
    return None


class _RFReader:
    def __init__(self, reader):
        self._reader = reader

    async def readline(self) -> bytes:
        line = await self._reader.readline()
        if not line and self._reader.at_eof():
            return b""
        return line.encode("ascii", errors="replace") + b"\r\n"

    async def readexactly(self, count: int) -> bytes:
        return await self._reader.readbytes(count)


class _RFWriter:
    def __init__(self, writer):
        self._writer = writer

    def write(self, data: bytes) -> None:
        self._writer.writebytes(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()


class _WireReader:
    """Read CR, LF, or CRLF terminated lines from a TCP FBB peer."""

    def __init__(self, reader):
        self._reader = reader
        self._skip_lf = False

    async def readline(self) -> bytes:
        data = bytearray()
        while True:
            byte = await self._reader.read(1)
            if not byte:
                if data:
                    raise asyncio.IncompleteReadError(bytes(data), len(data) + 1)
                return b""
            if self._skip_lf:
                self._skip_lf = False
                if byte == b"\n":
                    continue
            if byte == b"\r":
                self._skip_lf = True
                return bytes(data) + b"\r"
            if byte == b"\n":
                return bytes(data) + b"\n"
            data.extend(byte)
            if len(data) > _MAX_MSG_BYTES:
                raise ValueError("FBB line exceeds forwarding limit")

    async def readexactly(self, count: int) -> bytes:
        return await self._reader.readexactly(count)

if TYPE_CHECKING:
    from ..config import AppConfig, ForwardNeighborConfig
    from ..store.store import BBSStore

LOG = logging.getLogger(__name__)

_CONNECT_TIMEOUT  = 30.0    # seconds
_READLINE_TIMEOUT = 60.0    # seconds
_MAX_MSG_BYTES    = 256_000  # 256 KB max per message


class ForwardSession:
    """One FBB ASCII forwarding session with a single neighbor."""

    def __init__(
        self,
        cfg: AppConfig,
        store: BBSStore,
        neighbor: ForwardNeighborConfig,
        router=None,
    ) -> None:
        if neighbor.protocol.lower() not in ("ascii", "b0"):
            raise ValueError(f"unsupported FBB forwarding format: {neighbor.protocol}")
        self._cfg      = cfg
        self._store    = store
        self._neighbor = neighbor
        self._router = router
        self._rf_conn = None
        self._reader: asyncio.StreamReader | None  = None
        self._writer: asyncio.StreamWriter | None  = None
        self._remote_sid: SID | None = None
        self._sent    = 0
        self._received = 0
        self._binary = neighbor.protocol.lower() == "b0"

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def run_outgoing(self) -> tuple[int, int]:
        """Initiate a connection to the neighbor and run the session.

        Returns (msgs_sent, msgs_received).
        """
        addr = self._neighbor.address
        if not addr:
            raise ValueError(f"no address for neighbor {self._neighbor.call}")

        if _RF_CALL.fullmatch(addr.upper()):
            if self._router is None:
                raise RuntimeError("RF forwarding requires an active AX.25 router")
            from ..ax25.stream import AX25Reader, AX25Writer, AX25Meta
            self._rf_conn = await self._router.connect_outgoing(addr)
            self._reader = _RFReader(AX25Reader(self._rf_conn, AX25Meta(addr)))
            self._writer = _RFWriter(AX25Writer(self._rf_conn))
            try:
                await self._run(caller=True)
            finally:
                await self._router.close_outgoing(self._rf_conn)
                self._rf_conn = None
                self._reader = self._writer = None
            return self._sent, self._received

        host, _, port_str = addr.rpartition(":")
        if not host:
            host, port_str = addr, "6300"
        try:
            port = int(port_str)
        except ValueError:
            port = 6300

        LOG.info("forward: connecting to %s at %s:%d", self._neighbor.call, host, port)
        try:
            reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=_CONNECT_TIMEOUT,
            )
            self._reader = _WireReader(reader)
        except (asyncio.TimeoutError, OSError) as exc:
            raise ConnectionError(f"connect to {self._neighbor.call} failed: {exc}") from exc

        try:
            if self._neighbor.login_user:
                if not self._neighbor.login_password:
                    raise ValueError("TCP forwarding login password is missing")
                if any(c in self._neighbor.login_user + self._neighbor.login_password for c in "\r\n"):
                    raise ValueError("TCP forwarding credentials cannot contain line breaks")
                # LinBPQ FBBPORT sends no initial prompt. It expects these
                # lines before the BBS banner and SID.
                await self._writeline(self._neighbor.login_user)
                await self._writeline(self._neighbor.login_password)
            await self._run(caller=True)
        finally:
            await self._close()

        return self._sent, self._received

    async def run_incoming(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        greeted: bool = False,
    ) -> tuple[int, int]:
        """Handle an inbound forwarding connection (called side).

        The caller is responsible for accepting the TCP connection and
        passing the reader/writer here.  Returns (msgs_sent, msgs_received).
        """
        self._reader = _WireReader(reader) if isinstance(reader, asyncio.StreamReader) else reader
        self._writer = writer
        try:
            await self._run(caller=False, greeted=greeted)
        finally:
            await self._close()
        return self._sent, self._received

    # ------------------------------------------------------------------
    # Core session logic
    # ------------------------------------------------------------------

    async def _run(self, caller: bool, greeted: bool = False) -> None:
        assert self._reader and self._writer

        our_sid = gen_sid(self._cfg.node.node_call, compressed=self._binary)
        if not caller and not greeted:
            await self._writeline(our_sid)
            await self._writeline(">")

        # Read until we get a SID from the remote
        remote_sid_str = ""
        for _ in range(20):
            remote_sid_str = await self._readline()
            if not remote_sid_str:
                continue
            self._remote_sid = parse_sid(remote_sid_str)
            if self._remote_sid:
                break
            LOG.debug("forward: (pre-SID) %r", remote_sid_str[:80])
        if self._remote_sid:
            actual_call = (f"{self._remote_sid.call}-{self._remote_sid.ssid}"
                           if self._remote_sid.ssid else self._remote_sid.call).upper()
            if self._remote_sid.software_family == "pyBulletin" and actual_call != self._neighbor.call.upper():
                raise ConnectionError(
                    f"neighbor SID callsign {actual_call} does not match {self._neighbor.call}"
                )
            LOG.info("forward: remote SID %s (%s) flags=%s",
                     self._remote_sid, self._remote_sid.software_family,
                     self._remote_sid.flags)
            if "F" not in self._remote_sid.flags:
                raise ConnectionError("remote SID does not offer FBB forwarding")
            if self._binary and "B" not in self._remote_sid.flags:
                raise ConnectionError("remote SID does not offer compressed FBB forwarding")
        else:
            sw = detect_software(remote_sid_str)
            LOG.warning("forward: non-standard SID from %s (%s): %r",
                        self._neighbor.call, sw, remote_sid_str[:80])
            raise ConnectionError("unrecognized FBB SID")

        if caller:
            # Caller waits for ">" prompt from the called side
            while True:
                line = await self._readline()
                if not line:
                    continue
                if line.strip().endswith(">"):
                    break
                LOG.debug("forward: (pre-prompt) %r", line[:60])
            await self._writeline(our_sid)

        if caller:
            attempted: set[int] = set()
            for _ in range(1000):
                sent_messages, offered = await self._offer_batch(attempted)
                response = await self._receive_batch()
                # Reverse proposal, FF, or FQ acknowledges the prior block.
                for msg in sent_messages:
                    await self._store.mark_delivered(msg.id, self._neighbor.call)
                if response == "FQ":
                    break
                if response == "FF" and not offered:
                    await self._writeline("FQ")
                    break
            else:
                raise ConnectionError("FBB session exceeded 1000 proposal rounds")
        else:
            attempted = set()
            pending_ack: list[Message] = []
            for _ in range(1000):
                received = await self._receive_batch()
                for msg in pending_ack:
                    await self._store.mark_delivered(msg.id, self._neighbor.call)
                pending_ack = []
                if received == "FQ":
                    break
                if received == "FF" and not await self._select_outgoing(attempted):
                    await self._writeline("FQ")
                    break
                pending_ack, _ = await self._offer_batch(attempted)
            else:
                raise ConnectionError("FBB session exceeded 1000 proposal rounds")
        LOG.info("forward: session with %s complete — sent %d, received %d",
                 self._neighbor.call, self._sent, self._received)

    async def _offer_batch(self, attempted: set[int]) -> tuple[list[Message], bool]:
        outgoing = await self._select_outgoing(attempted)
        attempted.update(msg.id for msg in outgoing)
        LOG.info("forward: %d message(s) to send to %s",
                 len(outgoing), self._neighbor.call)

        proposal_sum = 0
        for msg in outgoing:
            line = _format_ascii_proposal(msg, compressed=self._binary)
            await self._writeline(line)
            proposal_sum += sum((line + "\r").encode("ascii", "replace"))
            LOG.debug("forward: proposal → %s", line)
        if not outgoing:
            await self._writeline("FF")
            return [], False
        await self._writeline(f"F> {-proposal_sum & 0xff:02X}" if self._binary else "F>")

        fs_line = await self._readline()
        if not fs_line.startswith("FS"):
            raise ConnectionError(f"expected FS from {self._neighbor.call}, got {fs_line!r}")
        responses = parse_fs_response(fs_line)
        if len(responses) != len(outgoing):
            raise ConnectionError("FS response length does not match proposal count")
        accepted_out = []
        for i, msg in enumerate(outgoing):
            resp = responses[i] if i < len(responses) else "-"
            if resp in ("+", "Y"):
                accepted_out.append(msg)
            elif resp == "N":
                LOG.debug("forward: %s confirms duplicate BID %s", self._neighbor.call, msg.bid)
                await self._store.mark_delivered(msg.id, self._neighbor.call)
            elif resp == "-":
                LOG.debug("forward: %s declined msg %d; leaving pending", self._neighbor.call, msg.id)
            else:
                LOG.debug("forward: %s deferred/rejected msg %d", self._neighbor.call, msg.id)

        for msg in accepted_out:
            await self._send_message(msg)
        return accepted_out, True

    async def _receive_batch(self) -> str:
        incoming_proposals: list[Proposal] = []
        proposal_sum = 0
        while True:
            line = await self._readline()
            if line == "FF":
                return "FF"
            if line == "FQ":
                return "FQ"
            if line == "F>" or line.startswith("F> "):
                if line.startswith("F> "):
                    try:
                        checksum = int(line[3:].strip(), 16)
                    except ValueError as exc:
                        raise ValueError("invalid FBB proposal checksum") from exc
                    if (proposal_sum + checksum) & 0xff:
                        raise ValueError("FBB proposal checksum mismatch")
                break
            if not line or line == "FQ":
                raise ConnectionError(f"proposal batch ended unexpectedly from {self._neighbor.call}")
            prop = _parse_ascii_proposal(line)
            if not prop:
                raise ValueError(f"invalid FBB proposal: {line[:80]!r}")
            if self._binary != line.startswith("FA "):
                raise ValueError(f"unexpected FBB proposal mode: {line[:80]!r}")
            incoming_proposals.append(prop)
            proposal_sum += sum((line + "\r").encode("ascii", "replace"))

        # Respond to incoming proposals
        await self._respond_to_proposals(incoming_proposals)

        for prop in incoming_proposals:
            if prop.accepted:
                await self._receive_message(prop)
        return "batch"

    # ------------------------------------------------------------------
    # Proposal selection
    # ------------------------------------------------------------------

    async def _select_outgoing(self, attempted: set[int] | None = None) -> list[Message]:
        """Return messages that should be forwarded to this neighbor."""
        neighbor = self._neighbor
        msgs = await self._store.list_messages()
        delivered = await self._store.delivered_message_ids(neighbor.call)
        selected: list[Message] = []
        node_call = self._cfg.node.node_call.upper()

        for msg in msgs:
            if msg.id in delivered or (attempted and msg.id in attempted) or msg.status in ("K", "H"):
                continue
            # Skip messages we originated from this neighbor (loop prevention)
            if node_call in msg.forward_path.upper().split():
                continue
            if neighbor.call.upper() in msg.forward_path.upper().split():
                continue

            if msg.msg_type == MSG_PRIVATE:
                # Private: forward if at_bbs matches this neighbor
                if msg.at_bbs and msg.at_bbs.upper() == neighbor.call.upper():
                    selected.append(msg)

            elif msg.msg_type in (MSG_BULLETIN, MSG_NTS):
                # Bulletins: forward if to_call matches one of the neighbor's categories
                to_hier = msg.to_call.upper()
                for cat in neighbor.categories:
                    if to_hier == cat.upper() or to_hier.startswith(cat.upper()):
                        selected.append(msg)
                        break

        # The basic FBB protocol allows at most five proposals in a block.
        return selected[:5]

    # ------------------------------------------------------------------
    # Responding to remote proposals
    # ------------------------------------------------------------------

    async def _respond_to_proposals(self, proposals: list[Proposal]) -> None:
        """Decide which incoming proposals to accept, respond with FS line."""
        responses: list[str] = []
        for prop in proposals:
            if await self._store.has_bid(prop.bid):
                responses.append("-")   # already received
                prop.accepted = False
            elif not 0 <= prop.size <= _MAX_MSG_BYTES:
                responses.append("=")   # defer; do not falsely confirm delivery
                prop.accepted = False
            else:
                responses.append("+")
                prop.accepted = True

        fs = "FS " + "".join(responses)
        await self._writeline(fs)
        LOG.debug("forward: sent FS %s", fs)

    # ------------------------------------------------------------------
    # Send one message
    # ------------------------------------------------------------------

    async def _send_message(self, msg: Message) -> None:
        try:
            if self._binary:
                self._writer.write(encode_fa(msg.subject, msg.body))
                await self._writer.drain()
                self._sent += 1
                return
            body = msg.body.replace("\r\n", "\n").replace("\r", "\n")
            if "\x1a" in body:
                raise ValueError("message body contains FBB end marker")
            payload = (msg.subject.replace("\r", " ").replace("\n", " ") + "\r"
                       + body.replace("\n", "\r") + "\r\x1a\r")
            self._writer.write(payload.encode("ascii", errors="replace"))
            await self._writer.drain()
            LOG.info("forward: sent message %d (BID %s) to %s",
                     msg.id, msg.bid, self._neighbor.call)
            # Delivery is recorded after the peer's next proposal or FF.
            self._sent += 1
        except Exception as exc:
            LOG.warning("forward: error sending message %d: %s", msg.id, exc)
            raise

    # ------------------------------------------------------------------
    # Receive one message
    # ------------------------------------------------------------------

    async def _receive_message(self, prop: Proposal) -> None:
        try:
            if self._binary:
                subject, body = await decode_fa(self._reader, max_body=_MAX_MSG_BYTES)
            else:
                subject = await self._readline()
                body_lines = []
                body_bytes = 0
                while True:
                    line = await self._readline()
                    if line == "\x1a":
                        break
                    body_bytes += len(line.encode("ascii", errors="replace")) + 2
                    if body_bytes > _MAX_MSG_BYTES:
                        raise ValueError("incoming FBB message exceeds forwarding limit")
                    body_lines.append(line)
                body = "\n".join(body_lines)
            msg = Message(
                bid=prop.bid, msg_type=prop.msg_type, from_call=prop.from_call,
                to_call=prop.to_call, at_bbs=prop.at_bbs,
                subject=subject, body=body,
            )

            # Avoid duplicates
            if await self._store.has_bid(msg.bid):
                LOG.debug("forward: duplicate BID %s — discarded", msg.bid)
                return

            msg_id = await self._store.insert_message(msg)
            await self._store.append_forward_path(msg_id, self._neighbor.call)
            LOG.info("forward: received message %d (BID %s) from %s",
                     msg_id, msg.bid, self._neighbor.call)
            self._received += 1

        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
            LOG.warning("forward: error receiving BID %s: %s", prop.bid, exc)
            raise

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    async def _readline(self) -> str:
        assert self._reader
        try:
            line = await asyncio.wait_for(
                self._reader.readline(), timeout=_READLINE_TIMEOUT
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            raise ConnectionError(f"forwarding read timed out or ended for {self._neighbor.call}")
        if not line:
            raise ConnectionError(f"forwarding connection closed by {self._neighbor.call}")
        return line.decode("ascii", errors="replace").rstrip("\r\n")

    async def _writeline(self, text: str) -> None:
        assert self._writer
        self._writer.write((text + "\r").encode("ascii", errors="replace"))
        await self._writer.drain()

    async def _close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None
