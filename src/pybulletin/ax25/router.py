"""AX.25 frame router — demultiplexes frames to connections and sessions.

The router sits between the KISS TNC link and the application layer:

    KissLink ──► AX25Router ──► AX25Connection(s) ──► BBSSession(s)

Responsibilities:
  - Accept raw AX.25 frames from the KISS link, tagged with their KISS port
  - Match frames to existing connections by remote callsign
  - Accept incoming SABM frames to create new server-mode connections
  - Spawn a BBSSession task for each new connected station
  - Route outbound frames back to the KISS link on the correct port
  - Send UI beacons on demand
  - Maintain a heard-station log for sysop visibility

Multi-port KISS (Dire Wolf multi-channel)
-----------------------------------------
Each received frame carries a KISS *port* (0–5) that identifies which
radio channel it arrived on.  The router records the port for each remote
callsign and wraps ``send_frame_cb`` to inject the same port for all
outgoing frames to that station.  This ensures that, for example, a
station connecting via the HF channel (port 1) receives replies on the
HF channel rather than VHF (port 0).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Awaitable, TYPE_CHECKING

from .frame import AX25Address, AX25Frame, FrameType
from .connection import AX25Connection, ConnState
from .stream import AX25Reader, AX25Writer, AX25Meta
from ..access_policy import CHANNEL_AX25

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..store.store import BBSStore
    from ..strings import StringCatalog
    from ..transport.conference import ConferenceHubManager

LOG = logging.getLogger(__name__)

# Type alias: send_cb(frame, kiss_port)
SendFrameCb = Callable[[AX25Frame, int], Awaitable[None]]

# Maximum entries in the heard-station deque
_HEARD_MAX = 200


class AX25Router:
    """Demultiplexes AX.25 frames and manages connection lifecycle."""

    def __init__(
        self,
        cfg: AppConfig,
        store: BBSStore,
        strings: StringCatalog,
        send_frame_cb: SendFrameCb,
        conference_hub: ConferenceHubManager | None = None,
    ) -> None:
        self._cfg      = cfg
        self._store    = store
        self._strings  = strings
        self._send_cb  = send_frame_cb
        self._conf_hub = conference_hub

        self._local_addr = AX25Address.parse(cfg.node.node_call)
        # Active connections keyed by remote callsign string
        self._connections: dict[str, AX25Connection] = {}
        self._session_tasks: dict[str, asyncio.Task] = {}
        # KISS port on which each remote was last heard
        self._conn_port: dict[str, int] = {}
        # Heard-station log: (utc_timestamp, callsign, port)
        self._heard: deque[tuple[datetime, str, int]] = deque(maxlen=_HEARD_MAX)

    # ------------------------------------------------------------------
    # Called by the KISS link for every received AX.25 frame
    # ------------------------------------------------------------------

    async def handle_frame(self, frame: AX25Frame, port: int = 0) -> None:
        # Record every heard station (even those not addressed to us)
        self._heard.append((datetime.now(timezone.utc), str(frame.src).upper(), port))

        # Only process frames addressed to our node
        if not frame.dest.matches(self._local_addr):
            alias = self._cfg.node.node_alias
            if alias and not AX25Address.parse(alias).matches(frame.dest):
                return

        src_key = str(frame.src).upper()
        ft = frame.frame_type

        if ft == FrameType.UI:
            LOG.debug("ax25: UI from %s port=%d info=%r",
                      src_key, port, frame.info[:40])
            return

        if ft == FrameType.SABM:
            await self._handle_sabm(frame, src_key, port)
            return

        conn = self._connections.get(src_key)
        if conn is None:
            LOG.debug("ax25: frame from unknown station %s — sending DM", src_key)
            await self._send_cb(AX25Frame.dm(
                AX25Address(frame.src.callsign, frame.src.ssid),
                AX25Address(self._local_addr.callsign, self._local_addr.ssid),
            ), port)
            return

        await conn.handle_frame(frame)

    async def _handle_sabm(
        self,
        frame: AX25Frame,
        src_key: str,
        port: int,
    ) -> None:
        existing = self._connections.get(src_key)

        if existing and existing.state == ConnState.CONNECTED:
            await existing.handle_frame(frame)
            return

        # Cancel any lingering task from a previous session with this station
        old_task = self._session_tasks.pop(src_key, None)
        if old_task and not old_task.done():
            old_task.cancel()

        # New connection — record which port this station is on
        self._conn_port[src_key] = port
        local  = AX25Address(self._local_addr.callsign, self._local_addr.ssid)
        remote = AX25Address(frame.src.callsign, frame.src.ssid)

        # Wrap send_cb to pin the KISS port for this connection
        conn_port = port
        async def _port_send(f: AX25Frame) -> None:
            await self._send_cb(f, conn_port)

        conn = AX25Connection(
            local_addr=local,
            remote_addr=remote,
            send_frame_cb=_port_send,
            t1=self._cfg.kiss.paclen / 1200 * 3 + 1.0,
            paclen=self._cfg.kiss.paclen,
        )
        self._connections[src_key] = conn

        await conn.handle_frame(frame)

        task = asyncio.create_task(
            self._run_session(conn, src_key),
            name=f"ax25-{src_key}",
        )
        self._session_tasks[src_key] = task
        task.add_done_callback(lambda _: self._cleanup(src_key))
        LOG.info("ax25: new connection from %s port=%d (%d active)",
                 src_key, port, len(self._connections))

    async def _run_session(self, conn: AX25Connection, src_key: str) -> None:
        from ..session.session import BBSSession

        meta   = AX25Meta(src_key)
        reader = AX25Reader(conn, meta)
        writer = AX25Writer(conn)

        router_ref = self
        session = BBSSession(
            reader, writer, meta, self._cfg, self._store, self._strings,
            heard_provider=lambda: router_ref.heard_stations,
            conference_hub=self._conf_hub,
        )
        try:
            await session.run()
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("ax25: unhandled error in session for %s", src_key)
        finally:
            if conn.state == ConnState.CONNECTED:
                await conn.disconnect()

    def _cleanup(self, src_key: str) -> None:
        self._connections.pop(src_key, None)
        self._session_tasks.pop(src_key, None)
        self._conn_port.pop(src_key, None)
        LOG.info("ax25: session for %s ended", src_key)

    # ------------------------------------------------------------------
    # Outbound UI frame (beacon, etc.)
    # ------------------------------------------------------------------

    async def send_ui(
        self,
        dest: str,
        info: bytes,
        via: list[str] | None = None,
        port: int = 0,
    ) -> None:
        dest_addr = AX25Address.parse(dest)
        src_addr  = AX25Address(self._local_addr.callsign, self._local_addr.ssid)
        reps      = [AX25Address.parse(v) for v in (via or [])]
        frame = AX25Frame.ui(dest_addr, src_addr, info, repeaters=reps)
        await self._send_cb(frame, port)

    # ------------------------------------------------------------------
    # Status / visibility
    # ------------------------------------------------------------------

    @property
    def active_connections(self) -> list[str]:
        return list(self._connections.keys())

    @property
    def heard_stations(self) -> list[tuple[datetime, str, int]]:
        """Recent heard stations as list of (utc_time, callsign, kiss_port).

        Most recent entry last; maximum ``_HEARD_MAX`` entries retained.
        """
        return list(self._heard)
