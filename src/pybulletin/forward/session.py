"""FBB B2F forwarding session state machine.

Handles one complete forwarding session with a single neighbor node over
a TCP connection (AX.25 forwarding reuses the same state machine via the
AX25Reader/AX25Writer adapter from ax25/stream.py).

Session flow
------------
1.  Connect (TCP open_connection or receive AX.25 connection)
2.  SID exchange — each side writes its SID immediately on connect
3.  Await ">" prompt from remote (or send ">" if we are the called side)
4.  Outgoing proposals  — we send FB lines for messages to forward
5.  Remote responds     — FS line with +/-/= per proposal
6.  Incoming proposals  — remote sends its FB lines; we respond with FS
7.  Transfer            — send accepted outgoing, receive accepted incoming
8.  FQ / disconnect

A session is initiated (caller role) by ``ForwardSession.run_outgoing()``.
A session is received (called role) by ``ForwardSession.run_incoming()``.
Both paths share the same proposal/transfer inner logic.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .sid import SID, generate as gen_sid, parse as parse_sid, detect_software
from .protocol import (
    Proposal,
    decode_b1_block, decode_b2f_block, decompress_b2f,
    encode_message_b1, encode_message_b2f,
    format_proposal, parse_fs_response, parse_proposal,
)
from ..store.models import (
    Message, MSG_PRIVATE, MSG_BULLETIN, MSG_NTS,
    STATUS_NEW, STATUS_FORWARDED,
)

if TYPE_CHECKING:
    from ..config import AppConfig, ForwardNeighborConfig
    from ..store.store import BBSStore

LOG = logging.getLogger(__name__)

_CONNECT_TIMEOUT  = 30.0    # seconds
_READLINE_TIMEOUT = 60.0    # seconds
_MAX_MSG_BYTES    = 256_000  # 256 KB max per message


class ForwardSession:
    """One B2F forwarding session with a single neighbor."""

    def __init__(
        self,
        cfg: AppConfig,
        store: BBSStore,
        neighbor: ForwardNeighborConfig,
    ) -> None:
        self._cfg      = cfg
        self._store    = store
        self._neighbor = neighbor
        self._reader: asyncio.StreamReader | None  = None
        self._writer: asyncio.StreamWriter | None  = None
        self._remote_sid: SID | None = None
        self._use_b2f = True   # negotiate down to B1 if needed
        self._sent    = 0
        self._received = 0

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def run_outgoing(self) -> tuple[int, int]:
        """Initiate a connection to the neighbor and run the session.

        Returns (msgs_sent, msgs_received).
        """
        addr = self._neighbor.address
        if not addr:
            LOG.warning("forward: no address for neighbor %s", self._neighbor.call)
            return 0, 0

        host, _, port_str = addr.rpartition(":")
        if not host:
            host, port_str = addr, "6300"
        try:
            port = int(port_str)
        except ValueError:
            port = 6300

        LOG.info("forward: connecting to %s at %s:%d", self._neighbor.call, host, port)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=_CONNECT_TIMEOUT,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            LOG.warning("forward: connect to %s failed: %s", self._neighbor.call, exc)
            return 0, 0

        try:
            await self._run(caller=True)
        finally:
            await self._close()

        return self._sent, self._received

    async def run_incoming(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> tuple[int, int]:
        """Handle an inbound forwarding connection (called side).

        The caller is responsible for accepting the TCP connection and
        passing the reader/writer here.  Returns (msgs_sent, msgs_received).
        """
        self._reader = reader
        self._writer = writer
        try:
            await self._run(caller=False)
        finally:
            await self._close()
        return self._sent, self._received

    # ------------------------------------------------------------------
    # Core session logic
    # ------------------------------------------------------------------

    async def _run(self, caller: bool) -> None:
        assert self._reader and self._writer

        # 1. SID exchange
        our_sid = gen_sid(self._cfg.node.node_call)
        await self._writeline(our_sid)
        LOG.debug("forward: sent SID %s", our_sid)

        # Read until we get a SID from the remote
        remote_sid_str = await self._readline()
        self._remote_sid = parse_sid(remote_sid_str)
        if self._remote_sid:
            LOG.info("forward: remote SID %s (%s) flags=%s",
                     self._remote_sid, self._remote_sid.software_family,
                     self._remote_sid.flags)
            self._use_b2f = self._remote_sid.supports_b2f and self._neighbor.bin_mode
        else:
            sw = detect_software(remote_sid_str)
            LOG.warning("forward: non-standard SID from %s (%s) — falling back to B1: %r",
                        self._neighbor.call, sw, remote_sid_str[:80])
            self._use_b2f = False

        if caller:
            # Caller waits for ">" prompt from the called side
            while True:
                line = await self._readline()
                if not line:
                    LOG.warning("forward: no prompt from %s, aborting",
                                self._neighbor.call)
                    return
                if line.strip() == ">":
                    break
                LOG.debug("forward: (pre-prompt) %r", line[:60])
        else:
            # Called side sends ">" to invite the caller to begin proposals
            await self._writeline(">")

        # 2. Collect outgoing proposals (messages to send to this neighbor)
        outgoing = await self._select_outgoing()
        LOG.info("forward: %d message(s) to send to %s",
                 len(outgoing), self._neighbor.call)

        # 3. Send outgoing proposals
        for msg in outgoing:
            line = format_proposal(msg, binary=self._use_b2f)
            await self._writeline(line)
            LOG.debug("forward: proposal → %s", line)
        await self._writeline("FF")

        # 4. Receive FS response for our proposals
        fs_line = await self._readline()
        responses = parse_fs_response(fs_line)
        accepted_out = []
        for i, msg in enumerate(outgoing):
            resp = responses[i] if i < len(responses) else "-"
            if resp == "+":
                accepted_out.append(msg)
            elif resp == "=":
                LOG.debug("forward: %s already has msg %d (BID %s)",
                          self._neighbor.call, msg.id, msg.bid)
            else:
                LOG.debug("forward: %s rejected msg %d", self._neighbor.call, msg.id)

        # 5. Receive incoming proposals from remote
        incoming_proposals: list[Proposal] = []
        while True:
            line = await self._readline()
            if not line or line.strip() in ("FF", "FQ", ""):
                break
            prop = parse_proposal(line)
            if prop:
                incoming_proposals.append(prop)

        # Respond to incoming proposals
        await self._respond_to_proposals(incoming_proposals)

        # 6. Transfer: send our accepted outgoing messages
        for msg in accepted_out:
            await self._send_message(msg)

        # 7. Transfer: receive incoming accepted messages
        for prop in incoming_proposals:
            if prop.accepted:
                await self._receive_message(prop)

        # 8. End session
        await self._writeline("FQ")
        LOG.info("forward: session with %s complete — sent %d, received %d",
                 self._neighbor.call, self._sent, self._received)

    # ------------------------------------------------------------------
    # Proposal selection
    # ------------------------------------------------------------------

    async def _select_outgoing(self) -> list[Message]:
        """Return messages that should be forwarded to this neighbor."""
        neighbor = self._neighbor
        msgs = await self._store.list_messages(status=STATUS_NEW)
        selected: list[Message] = []
        node_call = self._cfg.node.node_call.upper()

        for msg in msgs:
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

        return selected

    # ------------------------------------------------------------------
    # Responding to remote proposals
    # ------------------------------------------------------------------

    async def _respond_to_proposals(self, proposals: list[Proposal]) -> None:
        """Decide which incoming proposals to accept, respond with FS line."""
        if not proposals:
            return

        responses: list[str] = []
        for prop in proposals:
            if await self._store.has_bid(prop.bid):
                responses.append("=")   # duplicate
                prop.accepted = False
            elif prop.size > _MAX_MSG_BYTES:
                responses.append("-")   # too large
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
            if self._use_b2f:
                block = encode_message_b2f(msg)
            else:
                block = encode_message_b1(msg)
            self._writer.write(block)
            await self._writer.drain()
            LOG.info("forward: sent message %d (BID %s) to %s",
                     msg.id, msg.bid, self._neighbor.call)
            await self._store.mark_forwarded(msg.id)
            await self._store.append_forward_path(msg.id, self._cfg.node.node_call)
            self._sent += 1
        except Exception as exc:
            LOG.warning("forward: error sending message %d: %s", msg.id, exc)

    # ------------------------------------------------------------------
    # Receive one message
    # ------------------------------------------------------------------

    async def _receive_message(self, prop: Proposal) -> None:
        try:
            # Read the F> or F+ announcement
            header_line = await self._readline()
            if header_line.startswith("F> "):
                compressed_size = int(header_line[3:].strip())
                data = await asyncio.wait_for(
                    self._reader.readexactly(compressed_size),
                    timeout=_READLINE_TIMEOUT,
                )
                raw = decompress_b2f(data)
                if raw is None:
                    LOG.warning("forward: decompression failed for BID %s", prop.bid)
                    return
                msg = decode_b2f_block(raw)
            elif header_line.startswith("F+ "):
                text_size = int(header_line[3:].strip())
                data = await asyncio.wait_for(
                    self._reader.readexactly(text_size),
                    timeout=_READLINE_TIMEOUT,
                )
                msg = decode_b1_block(data)
            else:
                LOG.warning("forward: unexpected block header: %r", header_line[:40])
                return

            if msg is None:
                LOG.warning("forward: could not decode message BID %s", prop.bid)
                return

            # Fill in BID from proposal if envelope didn't include it
            if not msg.bid:
                msg.bid = prop.bid

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
            return ""
        return line.decode("ascii", errors="replace").rstrip("\r\n")

    async def _writeline(self, text: str) -> None:
        assert self._writer
        self._writer.write((text + "\r\n").encode("ascii", errors="replace"))
        await self._writer.drain()

    async def _close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None
