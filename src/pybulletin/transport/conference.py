"""BBS conference (real-time multi-user chat).

``ConferenceHubManager`` owns a dict of named ``ConferenceRoom`` objects.
It is created once in ``serve-core`` and injected into every BBSSession.

When a user types ``C [room]`` they enter the named room (default: CONF)
and every line they type is broadcast to all other participants.

Wire format
-----------
Messages look like::

    [AI3I] Hello everyone!\r\n

System announcements (join/leave) look like::

    *** AI3I has joined CONF\r\n

Exit
----
The user types ``/EXIT``, ``/X``, ``/Q``, or ``B``/``BYE``.

While in conference
-------------------
``/WHO``  — list participants in the current room.
``/ROOMS`` — list all active rooms.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Callable

LOG = logging.getLogger(__name__)

_QUEUE_DEPTH  = 64      # max queued messages per participant
_READ_TIMEOUT = 600.0   # 10-minute idle limit
_ROOM_RE      = re.compile(r"^[A-Z0-9]{1,16}$")
DEFAULT_ROOM  = "CONF"
_EXIT_CMDS    = {"/Q", "/EXIT", "/X", "B", "BYE", "Q", "QUIT"}


class ConferenceRoom:
    """Single named conference room."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock: asyncio.Lock = asyncio.Lock()
        self._members: dict[str, asyncio.Queue[str]] = {}
        self._joined_at: dict[str, float] = {}

    @property
    def member_count(self) -> int:
        return len(self._members)

    @property
    def members(self) -> list[str]:
        return sorted(self._members)

    async def run_session(
        self,
        callsign: str,
        readline_cb: Callable,
        send_cb: Callable,
        on_state_change: Callable | None = None,
    ) -> None:
        """Enter the room and block until the user exits."""
        call  = callsign.upper()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_DEPTH)

        async with self._lock:
            self._members[call]   = queue
            self._joined_at[call] = time.monotonic()

        if on_state_change:
            on_state_change()

        members_str = ", ".join(self.members)
        await send_cb(
            f"\r\n*** Entering room {self.name}.  Members: {members_str}\r\n"
            f"*** Type /EXIT or /X to leave.  /WHO for participant list.\r\n\r\n"
        )
        await self._broadcast(f"*** {call} has joined {self.name}\r\n", exclude=call)

        try:
            await self._run_loop(call, queue, readline_cb, send_cb)
        finally:
            async with self._lock:
                self._members.pop(call, None)
                self._joined_at.pop(call, None)
            await self._broadcast(f"*** {call} has left {self.name}\r\n", exclude=None)
            await send_cb(f"\r\n*** You have left {self.name}.\r\n")
            if on_state_change:
                on_state_change()
            LOG.info("conference[%s]: %s left (%d remaining)", self.name, call, len(self._members))

    async def join_ws(
        self,
        callsign: str,
        on_state_change: Callable | None = None,
    ) -> tuple[asyncio.Queue, str]:
        """Add a WebSocket participant.  Returns (queue, welcome_text)."""
        call  = callsign.upper()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_DEPTH)

        async with self._lock:
            self._members[call]   = queue
            self._joined_at[call] = time.monotonic()

        if on_state_change:
            on_state_change()

        members_str = ", ".join(self.members)
        welcome = (
            f"*** Entering room {self.name}.  Members: {members_str}\n"
            f"*** Type a message and press Enter.  Click Leave to exit."
        )
        await self._broadcast(f"*** {call} has joined {self.name}\r\n", exclude=call)
        LOG.info("conference[%s]: %s joined via web (%d total)", self.name, call, len(self._members))
        return queue, welcome

    async def send_from_ws(self, callsign: str, text: str) -> None:
        """Broadcast a message from a WebSocket participant."""
        call = callsign.upper()
        msg  = f"[{call}] {text}\r\n"
        try:
            async with self._lock:
                if call in self._members:
                    self._members[call].put_nowait(msg)  # echo to sender
        except asyncio.QueueFull:
            pass
        await self._broadcast(msg, exclude=call)

    async def leave_ws(
        self,
        callsign: str,
        on_state_change: Callable | None = None,
    ) -> None:
        """Remove a WebSocket participant."""
        call = callsign.upper()
        async with self._lock:
            self._members.pop(call, None)
            self._joined_at.pop(call, None)
        await self._broadcast(f"*** {call} has left {self.name}\r\n", exclude=None)
        if on_state_change:
            on_state_change()
        LOG.info("conference[%s]: %s left via web (%d remaining)", self.name, call, len(self._members))

    async def _broadcast(self, text: str, exclude: str | None) -> None:
        async with self._lock:
            targets = {k: q for k, q in self._members.items() if k != exclude}
        for q in targets.values():
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                pass  # slow client — drop rather than block everyone

    async def _run_loop(
        self,
        call: str,
        queue: asyncio.Queue[str],
        readline_cb: Callable,
        send_cb: Callable,
    ) -> None:
        while True:
            input_task = asyncio.create_task(readline_cb())
            recv_task  = asyncio.create_task(queue.get())

            done, pending = await asyncio.wait(
                {input_task, recv_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            if input_task in done:
                try:
                    line = input_task.result()
                except Exception:
                    return

                stripped = line.strip()
                upper    = stripped.upper()

                if upper in _EXIT_CMDS:
                    return
                if upper in ("/WHO", "/W"):
                    await self._send_who(call, send_cb)
                    continue
                if not stripped or upper == "\x1a":
                    continue

                msg = f"[{call}] {stripped}\r\n"
                await send_cb(msg)
                await self._broadcast(msg, exclude=call)

            if recv_task in done:
                try:
                    text = recv_task.result()
                except Exception:
                    continue
                await send_cb(text)

    async def _send_who(self, requester: str, send_cb: Callable) -> None:
        async with self._lock:
            snapshot = dict(self._joined_at)
        now = time.monotonic()
        lines = [f"\r\n*** {self.name} — {len(snapshot)} participant(s):\r\n"]
        for call in sorted(snapshot):
            elapsed = int(now - snapshot[call])
            mins, secs = divmod(elapsed, 60)
            marker = " (you)" if call == requester else ""
            lines.append(f"    {call:<9}  {mins}m{secs:02d}s{marker}\r\n")
        await send_cb("".join(lines) + "\r\n")


class ConferenceHubManager:
    """Manages a collection of named conference rooms."""

    DEFAULT_ROOM = DEFAULT_ROOM

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()
        self._rooms: dict[str, ConferenceRoom] = {}
        self._on_state_change: Callable | None = None

    def set_state_change_callback(self, cb: Callable) -> None:
        """Wire in a callback (e.g. WebSocket broadcast) for room state changes."""
        self._on_state_change = cb

    async def enter_room(
        self,
        room_name: str,
        callsign: str,
        readline_cb: Callable,
        send_cb: Callable,
    ) -> None:
        """Enter (or create) the named room and block until the user exits."""
        name = self._normalize_room(room_name)
        async with self._lock:
            if name not in self._rooms:
                self._rooms[name] = ConferenceRoom(name)
            room = self._rooms[name]

        await room.run_session(callsign, readline_cb, send_cb,
                               on_state_change=self._on_state_change)

        async with self._lock:
            if name in self._rooms and self._rooms[name].member_count == 0:
                del self._rooms[name]

    async def enter_room_ws(
        self, room_name: str, callsign: str,
    ) -> tuple[asyncio.Queue, str]:
        """Join room as a WebSocket participant. Returns (queue, welcome_text)."""
        name = self._normalize_room(room_name)
        async with self._lock:
            if name not in self._rooms:
                self._rooms[name] = ConferenceRoom(name)
            room = self._rooms[name]
        return await room.join_ws(callsign, on_state_change=self._on_state_change)

    async def send_from_ws(self, room_name: str, callsign: str, text: str) -> None:
        """Broadcast a message from a WebSocket participant."""
        name = self._normalize_room(room_name)
        async with self._lock:
            room = self._rooms.get(name)
        if room:
            await room.send_from_ws(callsign, text)

    async def leave_room_ws(self, room_name: str, callsign: str) -> None:
        """Remove a WebSocket participant from the room."""
        name = self._normalize_room(room_name)
        async with self._lock:
            room = self._rooms.get(name)
        if room:
            await room.leave_ws(callsign, on_state_change=self._on_state_change)
            async with self._lock:
                if name in self._rooms and self._rooms[name].member_count == 0:
                    del self._rooms[name]

    def rooms_snapshot(self) -> dict[str, list[str]]:
        """Return {room_name: [sorted callsigns]} for the sysop monitor."""
        return {name: room.members for name, room in self._rooms.items()}

    @staticmethod
    def _normalize_room(name: str) -> str:
        cleaned = re.sub(r"[^A-Z0-9]", "", name.upper())[:16] or DEFAULT_ROOM
        return cleaned


# Backwards-compatible alias so existing imports of ConferenceHub still work
ConferenceHub = ConferenceHubManager
