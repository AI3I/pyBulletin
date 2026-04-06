"""AX.25 Layer 2 state machine — modulo-8 windowed connections.

One AX25Connection instance per established (or establishing) link.
The state machine handles:
  - Incoming SABM → UA (server/listen mode)
  - Outgoing SABM → wait for UA (client/connect mode)
  - I-frame exchange with windowed acknowledgment
  - RR / RNR / REJ supervisory frames
  - T1 (acknowledgment / retransmit) timer
  - T3 (link activity keep-alive) timer
  - DISC / DM handling
  - Graceful disconnect

Usage pattern (server listen mode)::

    conn = AX25Connection(
        local_addr=AX25Address.parse("W3BBS"),
        remote_addr=AX25Address.parse("AI3I"),
        send_frame_cb=tnc_link.send,  # async callable(AX25Frame)
    )
    await conn.handle_frame(sabm_frame)   # triggers UA + CONNECTED
    # ... session reads/writes via conn.read() / conn.write()
    await conn.disconnect()

The read/write interface is byte-stream oriented.  The caller is
responsible for line framing on top (see ax25/stream.py).
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum, auto
from typing import Callable, Awaitable

from .frame import AX25Address, AX25Frame, FrameType

LOG = logging.getLogger(__name__)

# Modulo-8 window parameters
_MOD   = 8
_WINDOW = 7   # maximum outstanding unacknowledged I frames

# Timer defaults (seconds)
T1_DEFAULT = 3.0    # retransmit timer
T3_DEFAULT = 120.0  # link activity timer

# Maximum retransmit attempts before declaring link failure
_MAX_RETRIES = 10


class ConnState(Enum):
    DISCONNECTED      = auto()
    AWAITING_CONNECT  = auto()   # outgoing SABM sent, waiting for UA
    CONNECTED         = auto()
    AWAITING_RELEASE  = auto()   # DISC sent, waiting for UA/DM


SendCb = Callable[[AX25Frame], Awaitable[None]]


class AX25Connection:
    """AX.25 modulo-8 connection state machine."""

    def __init__(
        self,
        local_addr: AX25Address,
        remote_addr: AX25Address,
        send_frame_cb: SendCb,
        *,
        t1: float = T1_DEFAULT,
        t3: float = T3_DEFAULT,
        paclen: int = 236,
    ) -> None:
        self._local  = local_addr
        self._remote = remote_addr
        self._send_cb = send_frame_cb
        self._t1_val = t1
        self._t3_val = t3
        self._paclen = paclen

        self._state = ConnState.DISCONNECTED
        self._vs = 0   # V(S): next send sequence number
        self._vr = 0   # V(R): next expected receive sequence number
        self._va = 0   # V(A): last acknowledged sequence number

        self._retry_count = 0
        self._t1_task: asyncio.Task | None = None
        self._t3_task: asyncio.Task | None = None

        # Unacknowledged I frames: {ns: frame}
        self._unacked: dict[int, AX25Frame] = {}
        # Data waiting to be sent as I frames
        self._tx_queue: asyncio.Queue[bytes] = asyncio.Queue()
        # Received data delivered to the session
        self._rx_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # Set when connection is established (for awaiting callers)
        self._connected_event = asyncio.Event()
        # Set when connection is torn down
        self._disconnected_event = asyncio.Event()

        self._peer_busy = False   # remote sent RNR
        self._local_busy = False  # we sent RNR (not implemented: always False)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConnState:
        return self._state

    @property
    def local_addr(self) -> AX25Address:
        return self._local

    @property
    def remote_addr(self) -> AX25Address:
        return self._remote

    async def wait_connected(self) -> bool:
        """Wait until connected or disconnected. Returns True if connected."""
        done, _ = await asyncio.wait(
            [
                asyncio.ensure_future(self._connected_event.wait()),
                asyncio.ensure_future(self._disconnected_event.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        return self._state == ConnState.CONNECTED

    async def connect(self) -> bool:
        """Initiate a connection (client mode). Returns True on success."""
        if self._state != ConnState.DISCONNECTED:
            return False
        self._state = ConnState.AWAITING_CONNECT
        self._retry_count = 0
        await self._send(AX25Frame.sabm(
            self._make_dest(), self._make_src()
        ))
        self._start_t1()
        return await self.wait_connected()

    async def disconnect(self) -> None:
        """Initiate a graceful disconnect."""
        if self._state not in (ConnState.CONNECTED, ConnState.AWAITING_CONNECT):
            return
        self._stop_t1()
        self._stop_t3()
        self._state = ConnState.AWAITING_RELEASE
        await self._send(AX25Frame.disc(
            self._make_dest(), self._make_src()
        ))
        self._start_t1()

    async def write(self, data: bytes) -> None:
        """Queue *data* for transmission as I frame(s)."""
        if self._state != ConnState.CONNECTED:
            return
        # Split into PACLEN chunks
        for i in range(0, max(1, len(data)), self._paclen):
            chunk = data[i:i + self._paclen]
            await self._tx_queue.put(chunk)
        # Pump the send window
        await self._pump_tx()

    async def read(self) -> bytes:
        """Wait for and return the next received data chunk."""
        return await self._rx_queue.get()

    async def read_nowait(self) -> bytes | None:
        try:
            return self._rx_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ------------------------------------------------------------------
    # Frame handler — called by the router for every arriving frame
    # ------------------------------------------------------------------

    async def handle_frame(self, frame: AX25Frame) -> None:
        ft = frame.frame_type
        LOG.debug("ax25: %s ← %s [%s]", self._local, self._remote, ft.name)

        if ft == FrameType.SABM:
            await self._handle_sabm(frame)
        elif ft == FrameType.UA:
            await self._handle_ua(frame)
        elif ft == FrameType.DISC:
            await self._handle_disc(frame)
        elif ft == FrameType.DM:
            await self._handle_dm(frame)
        elif ft == FrameType.I:
            await self._handle_i(frame)
        elif ft == FrameType.RR:
            await self._handle_rr(frame)
        elif ft == FrameType.RNR:
            await self._handle_rnr(frame)
        elif ft == FrameType.REJ:
            await self._handle_rej(frame)
        elif ft == FrameType.UI:
            # Connectionless UI — deliver info to rx queue
            if frame.info:
                await self._rx_queue.put(frame.info)
        else:
            LOG.debug("ax25: unhandled frame type %s from %s", ft.name, self._remote)

    # ------------------------------------------------------------------
    # SABM — incoming connect request (server role)
    # ------------------------------------------------------------------

    async def _handle_sabm(self, frame: AX25Frame) -> None:
        if self._state == ConnState.CONNECTED:
            # Re-SABM on existing connection: reset and re-ack
            LOG.info("ax25: re-connect from %s — resetting", self._remote)
            self._reset_counters()
        await self._send(AX25Frame.ua(self._make_dest(), self._make_src()))
        self._state = ConnState.CONNECTED
        self._reset_counters()
        self._start_t3()
        self._connected_event.set()
        LOG.info("ax25: connected from %s", self._remote)

    # ------------------------------------------------------------------
    # UA — acknowledgment of our SABM or DISC
    # ------------------------------------------------------------------

    async def _handle_ua(self, frame: AX25Frame) -> None:
        if self._state == ConnState.AWAITING_CONNECT:
            self._stop_t1()
            self._state = ConnState.CONNECTED
            self._reset_counters()
            self._start_t3()
            self._connected_event.set()
            LOG.info("ax25: connected to %s", self._remote)
            await self._pump_tx()
        elif self._state == ConnState.AWAITING_RELEASE:
            self._stop_t1()
            self._state = ConnState.DISCONNECTED
            self._disconnected_event.set()
            LOG.info("ax25: disconnected from %s (clean)", self._remote)
        else:
            LOG.debug("ax25: unexpected UA from %s in state %s", self._remote, self._state)

    # ------------------------------------------------------------------
    # DISC — remote wants to disconnect
    # ------------------------------------------------------------------

    async def _handle_disc(self, frame: AX25Frame) -> None:
        self._stop_t1()
        self._stop_t3()
        await self._send(AX25Frame.ua(self._make_dest(), self._make_src()))
        old_state = self._state
        self._state = ConnState.DISCONNECTED
        self._disconnected_event.set()
        if old_state == ConnState.CONNECTED:
            LOG.info("ax25: remote %s disconnected", self._remote)

    # ------------------------------------------------------------------
    # DM — remote is disconnected / refused our SABM
    # ------------------------------------------------------------------

    async def _handle_dm(self, frame: AX25Frame) -> None:
        self._stop_t1()
        self._stop_t3()
        self._state = ConnState.DISCONNECTED
        self._disconnected_event.set()
        LOG.info("ax25: DM from %s — link refused/closed", self._remote)

    # ------------------------------------------------------------------
    # I frame — received data
    # ------------------------------------------------------------------

    async def _handle_i(self, frame: AX25Frame) -> None:
        if self._state != ConnState.CONNECTED:
            await self._send(AX25Frame.dm(self._make_dest(), self._make_src()))
            return

        self._stop_t3()
        self._start_t3()

        # Acknowledge the N(R) in the I frame (piggybacked ACK)
        self._update_va(frame.nr)

        ns = frame.ns
        if ns == self._vr:
            # In-sequence frame
            self._vr = (self._vr + 1) % _MOD
            if frame.info:
                await self._rx_queue.put(frame.info)
            # Send RR acknowledgment
            if not self._local_busy:
                await self._send(AX25Frame.rr(
                    self._make_dest(), self._make_src(), self._vr
                ))
            else:
                await self._send(AX25Frame.rnr(
                    self._make_dest(), self._make_src(), self._vr
                ))
        else:
            # Out-of-sequence — send REJ to request retransmission
            LOG.debug("ax25: out-of-seq I from %s ns=%d expected=%d",
                      self._remote, ns, self._vr)
            await self._send(AX25Frame.rej(
                self._make_dest(), self._make_src(), self._vr
            ))

        # If T1 is running and all frames acked, stop it
        if self._va == self._vs:
            self._stop_t1()

        # Pump any queued transmit data
        await self._pump_tx()

    # ------------------------------------------------------------------
    # S frames — supervisory
    # ------------------------------------------------------------------

    async def _handle_rr(self, frame: AX25Frame) -> None:
        if self._state != ConnState.CONNECTED:
            return
        self._peer_busy = False
        self._update_va(frame.nr)
        if self._va == self._vs:
            self._stop_t1()
        self._stop_t3()
        self._start_t3()
        await self._pump_tx()

    async def _handle_rnr(self, frame: AX25Frame) -> None:
        if self._state != ConnState.CONNECTED:
            return
        self._peer_busy = True
        self._update_va(frame.nr)
        self._stop_t1()
        self._start_t1()  # restart T1 to poll peer later

    async def _handle_rej(self, frame: AX25Frame) -> None:
        if self._state != ConnState.CONNECTED:
            return
        self._update_va(frame.nr)
        self._stop_t1()
        # Retransmit from N(R) onward
        await self._retransmit_from(frame.nr)
        if self._vs != self._va:
            self._start_t1()

    # ------------------------------------------------------------------
    # TX window management
    # ------------------------------------------------------------------

    async def _pump_tx(self) -> None:
        """Send as many queued I frames as the window allows."""
        if self._state != ConnState.CONNECTED or self._peer_busy:
            return
        while not self._tx_queue.empty():
            outstanding = (self._vs - self._va) % _MOD
            if outstanding >= _WINDOW:
                break
            try:
                data = self._tx_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            frame = AX25Frame.iframe(
                self._make_dest(), self._make_src(),
                ns=self._vs, nr=self._vr,
                info=data,
            )
            self._unacked[self._vs] = frame
            self._vs = (self._vs + 1) % _MOD
            await self._send(frame)
            if self._t1_task is None or self._t1_task.done():
                self._start_t1()

    async def _retransmit_from(self, nr: int) -> None:
        """Retransmit all unacked frames starting from sequence *nr*."""
        seq = nr
        while seq != self._vs:
            if seq in self._unacked:
                frame = self._unacked[seq]
                # Update N(R) to current V(R) before retransmitting
                ctrl = (frame.control & 0x1F) | ((self._vr & 0x07) << 5)
                frame.control = ctrl
                await self._send(frame)
                LOG.debug("ax25: retransmit ns=%d to %s", seq, self._remote)
            seq = (seq + 1) % _MOD

    def _update_va(self, nr: int) -> None:
        """Advance V(A) to *nr*, discarding acked frames from unacked queue."""
        seq = self._va
        while seq != nr:
            self._unacked.pop(seq, None)
            seq = (seq + 1) % _MOD
        self._va = nr

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _start_t1(self) -> None:
        self._stop_t1()
        self._t1_task = asyncio.create_task(self._t1_expire())

    def _stop_t1(self) -> None:
        if self._t1_task and not self._t1_task.done():
            self._t1_task.cancel()
        self._t1_task = None

    async def _t1_expire(self) -> None:
        await asyncio.sleep(self._t1_val)
        if self._state == ConnState.AWAITING_CONNECT:
            self._retry_count += 1
            if self._retry_count > _MAX_RETRIES:
                LOG.warning("ax25: T1 expired %d times connecting to %s — giving up",
                            _MAX_RETRIES, self._remote)
                self._state = ConnState.DISCONNECTED
                self._disconnected_event.set()
                return
            await self._send(AX25Frame.sabm(self._make_dest(), self._make_src()))
            self._start_t1()
        elif self._state == ConnState.AWAITING_RELEASE:
            self._retry_count += 1
            if self._retry_count > _MAX_RETRIES:
                self._state = ConnState.DISCONNECTED
                self._disconnected_event.set()
                return
            await self._send(AX25Frame.disc(self._make_dest(), self._make_src()))
            self._start_t1()
        elif self._state == ConnState.CONNECTED:
            if self._vs != self._va:
                # Retransmit unacked frames
                self._retry_count += 1
                if self._retry_count > _MAX_RETRIES:
                    LOG.warning("ax25: link to %s failed — no ACK", self._remote)
                    await self._link_failure()
                    return
                await self._retransmit_from(self._va)
                self._start_t1()
            else:
                # Poll with RR
                await self._send(AX25Frame.rr(
                    self._make_dest(), self._make_src(), self._vr, pf=True, command=True
                ))
                self._start_t1()

    def _start_t3(self) -> None:
        self._stop_t3()
        self._t3_task = asyncio.create_task(self._t3_expire())

    def _stop_t3(self) -> None:
        if self._t3_task and not self._t3_task.done():
            self._t3_task.cancel()
        self._t3_task = None

    async def _t3_expire(self) -> None:
        await asyncio.sleep(self._t3_val)
        if self._state == ConnState.CONNECTED:
            # Send RR poll to check if remote is still alive
            await self._send(AX25Frame.rr(
                self._make_dest(), self._make_src(), self._vr, pf=True, command=True
            ))
            self._start_t1()

    async def _link_failure(self) -> None:
        self._stop_t1()
        self._stop_t3()
        self._state = ConnState.DISCONNECTED
        self._disconnected_event.set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reset_counters(self) -> None:
        self._vs = self._vr = self._va = 0
        self._retry_count = 0
        self._unacked.clear()
        self._peer_busy = False

    def _make_dest(self) -> AX25Address:
        return AX25Address(self._remote.callsign, self._remote.ssid)

    def _make_src(self) -> AX25Address:
        return AX25Address(self._local.callsign, self._local.ssid)

    async def _send(self, frame: AX25Frame) -> None:
        try:
            await self._send_cb(frame)
        except Exception:
            LOG.exception("ax25: error sending frame to %s", self._remote)
