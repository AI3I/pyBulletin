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

Conference commands (prefix /)
-------------------------------
``/WHO``  or ``/W``   — list participants in the current room
``/ROOMS`` or ``/L``  — list all active rooms
``/J room``           — switch to a different room
``/EXIT``, ``/X``, ``/Q``, ``B``, ``BYE`` — leave conference

Multiple sessions with the same callsign are supported (web + terminal).
Each session has a unique internal key; the callsign is used only for display.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from typing import Callable

LOG = logging.getLogger(__name__)

_QUEUE_DEPTH = 64       # max queued messages per participant
DEFAULT_ROOM = "CONF"
_EXIT_CMDS   = {"/Q", "/EXIT", "/X", "B", "BYE", "Q", "QUIT"}
_WHO_CMDS    = {"/WHO", "/W"}
_LIST_CMDS   = {"/ROOMS", "/L"}

_key_counter = itertools.count(1)


def _new_key(callsign: str) -> str:
    return f"{callsign}:{next(_key_counter)}"


class _Participant:
    __slots__ = ("callsign", "queue", "joined_at")

    def __init__(self, callsign: str) -> None:
        self.callsign  = callsign
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_DEPTH)
        self.joined_at = time.monotonic()


class ConferenceRoom:
    """Single named conference room."""

    def __init__(self, name: str, manager: ConferenceHubManager) -> None:
        self.name     = name
        self._manager = manager
        self._lock: asyncio.Lock = asyncio.Lock()
        self._parts: dict[str, _Participant] = {}   # session_key → Participant

    @property
    def member_count(self) -> int:
        return len(self._parts)

    @property
    def members(self) -> list[str]:
        """Sorted list of callsigns (may have duplicates)."""
        return sorted(p.callsign for p in self._parts.values())

    # ------------------------------------------------------------------
    # Terminal session (blocks until user exits)
    # ------------------------------------------------------------------

    async def run_session(
        self,
        callsign: str,
        readline_cb: Callable,
        send_cb: Callable,
        on_state_change: Callable | None = None,
    ) -> None:
        call = callsign.upper()
        key  = _new_key(call)
        part = _Participant(call)

        async with self._lock:
            self._parts[key] = part

        if on_state_change:
            on_state_change()

        members_str = ", ".join(self.members) or "you"
        await send_cb(
            f"\r\n*** Entering room {self.name}.  Members: {members_str}\r\n"
            f"*** /W=who  /L=list  /J=join  /X=exit\r\n\r\n"
        )
        await self._broadcast(f"*** {call} has joined {self.name}\r\n", exclude_key=key)

        try:
            await self._run_loop(call, key, part.queue, readline_cb, send_cb)
        finally:
            async with self._lock:
                self._parts.pop(key, None)
            await self._broadcast(f"*** {call} has left {self.name}\r\n", exclude_key=None)
            await send_cb(f"\r\n*** You have left {self.name}.\r\n")
            if on_state_change:
                on_state_change()
            LOG.info("conference[%s]: %s left (%d remaining)", self.name, call, len(self._parts))

    # ------------------------------------------------------------------
    # WebSocket session (caller drives; returns queue to drain)
    # ------------------------------------------------------------------

    async def join_ws(
        self,
        callsign: str,
        on_state_change: Callable | None = None,
    ) -> tuple[str, asyncio.Queue, str]:
        """Add a WebSocket participant. Returns (session_key, queue, welcome_text)."""
        call = callsign.upper()
        key  = _new_key(call)
        part = _Participant(call)

        async with self._lock:
            self._parts[key] = part

        if on_state_change:
            on_state_change()

        members_str = ", ".join(self.members) or "you"
        welcome = (
            f"*** Entering room {self.name}.  Members: {members_str}\n"
            f"*** /W=who  /L=list  /J=join  /X=exit"
        )
        await self._broadcast(f"*** {call} has joined {self.name}\r\n", exclude_key=key)
        LOG.info("conference[%s]: %s joined via web (%d total)", self.name, call, len(self._parts))
        return key, part.queue, welcome

    async def send_ws(self, session_key: str, text: str) -> None:
        """Broadcast a message from a WebSocket participant."""
        async with self._lock:
            part = self._parts.get(session_key)
        if not part:
            return
        call = part.callsign
        msg  = f"[{call}] {text}\r\n"
        # Echo to sender's own queue
        try:
            part.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass
        await self._broadcast(msg, exclude_key=session_key)

    async def handle_ws_input(
        self,
        session_key: str,
        text: str,
        reply_cb: Callable,
    ) -> str | None:
        """Parse a web user's input. Returns new room name for /J, else None."""
        upper = text.strip().upper()
        if upper in _WHO_CMDS:
            async with self._lock:
                part = self._parts.get(session_key)
            call = part.callsign if part else ""
            await self._send_who(call, reply_cb)
            return None
        if upper in _LIST_CMDS:
            await self._send_rooms(reply_cb)
            return None
        if upper.startswith("/J ") or upper.startswith("/JOIN "):
            parts = text.strip().split(None, 1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()
            return None
        await self.send_ws(session_key, text)
        return None

    async def leave_ws(
        self,
        session_key: str,
        on_state_change: Callable | None = None,
    ) -> None:
        """Remove a WebSocket participant."""
        async with self._lock:
            part = self._parts.pop(session_key, None)
        if part is None:
            return
        await self._broadcast(f"*** {part.callsign} has left {self.name}\r\n", exclude_key=None)
        if on_state_change:
            on_state_change()
        LOG.info("conference[%s]: %s left via web (%d remaining)", self.name, part.callsign, len(self._parts))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _broadcast(self, text: str, exclude_key: str | None) -> None:
        async with self._lock:
            targets = [p for k, p in self._parts.items() if k != exclude_key]
        for p in targets:
            try:
                p.queue.put_nowait(text)
            except asyncio.QueueFull:
                pass  # slow client — drop rather than block everyone

    async def _run_loop(
        self,
        call: str,
        key: str,
        queue: asyncio.Queue,
        readline_cb: Callable,
        send_cb: Callable,
    ) -> str | None:
        """Race between user input and incoming messages. Returns room to switch to, or None."""
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

            if recv_task in done:
                try:
                    text = recv_task.result()
                except Exception:
                    pass
                else:
                    await send_cb(text)

            if input_task in done:
                try:
                    line = input_task.result()
                except Exception:
                    return None

                stripped = line.strip()
                upper    = stripped.upper()

                if upper in _EXIT_CMDS:
                    return None
                if upper in _WHO_CMDS:
                    await self._send_who(call, send_cb)
                    continue
                if upper in _LIST_CMDS:
                    await self._send_rooms(send_cb)
                    continue
                if upper.startswith("/J ") or upper.startswith("/JOIN "):
                    new_room = stripped.split(None, 1)[1].strip() if " " in stripped else ""
                    if new_room:
                        return new_room   # signal caller to switch rooms
                    continue
                if not stripped or upper == "\x1a":
                    continue

                msg = f"[{call}] {stripped}\r\n"
                await send_cb(msg)
                await self._broadcast(msg, exclude_key=key)

        return None

    async def _send_who(self, requester: str, send_cb: Callable) -> None:
        async with self._lock:
            snapshot = [(p.callsign, p.joined_at) for p in self._parts.values()]
        now = time.monotonic()
        lines = [f"\r\n*** {self.name} — {len(snapshot)} participant(s):\r\n"]
        for callsign, joined_at in sorted(snapshot):
            elapsed = int(now - joined_at)
            mins, secs = divmod(elapsed, 60)
            marker = " (you)" if callsign == requester else ""
            lines.append(f"    {callsign:<9}  {mins}m{secs:02d}s{marker}\r\n")
        await send_cb("".join(lines) + "\r\n")

    async def _send_rooms(self, send_cb: Callable) -> None:
        snapshot = self._manager.rooms_snapshot()
        if not snapshot:
            await send_cb("\r\n*** No active rooms.\r\n\r\n")
            return
        lines = ["\r\n*** Active rooms:\r\n"]
        for rname, members in sorted(snapshot.items()):
            marker = " *" if rname == self.name else ""
            lines.append(f"    {rname:<12} {len(members)} user(s): {', '.join(members)}{marker}\r\n")
        await send_cb("".join(lines) + "\r\n")


class ConferenceHubManager:
    """Manages a collection of named conference rooms."""

    DEFAULT_ROOM = DEFAULT_ROOM

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()
        self._rooms: dict[str, ConferenceRoom] = {}
        self._on_state_change: Callable | None = None

    def set_state_change_callback(self, cb: Callable) -> None:
        self._on_state_change = cb

    async def enter_room(
        self,
        room_name: str,
        callsign: str,
        readline_cb: Callable,
        send_cb: Callable,
    ) -> None:
        """Enter (or create) the named room and block until the user exits.
        Handles /J room switches internally."""
        name = _norm(room_name)
        while True:
            room = await self._get_or_create(name)
            switch_to = await room.run_session(
                callsign, readline_cb, send_cb,
                on_state_change=self._on_state_change,
            )
            await self._maybe_remove(name)
            if not switch_to:
                break
            name = _norm(switch_to)

    async def enter_room_ws(
        self, room_name: str, callsign: str,
    ) -> tuple[str, asyncio.Queue, str]:
        """Join room as WebSocket participant. Returns (session_key, queue, welcome)."""
        name = _norm(room_name)
        room = await self._get_or_create(name)
        return await room.join_ws(callsign, on_state_change=self._on_state_change)

    async def send_from_ws(self, room_name: str, session_key: str, text: str) -> None:
        name = _norm(room_name)
        async with self._lock:
            room = self._rooms.get(name)
        if room:
            await room.send_ws(session_key, text)

    async def handle_ws_input(
        self, room_name: str, session_key: str, text: str, reply_cb: Callable,
    ) -> str | None:
        """Parse web input. Returns new room name for /J switch, else None."""
        name = _norm(room_name)
        async with self._lock:
            room = self._rooms.get(name)
        if room:
            return await room.handle_ws_input(session_key, text, reply_cb)
        return None

    async def leave_room_ws(self, room_name: str, session_key: str) -> None:
        name = _norm(room_name)
        async with self._lock:
            room = self._rooms.get(name)
        if room:
            await room.leave_ws(session_key, on_state_change=self._on_state_change)
            await self._maybe_remove(name)

    def rooms_snapshot(self) -> dict[str, list[str]]:
        return {name: room.members for name, room in self._rooms.items()}

    async def _get_or_create(self, name: str) -> ConferenceRoom:
        async with self._lock:
            if name not in self._rooms:
                self._rooms[name] = ConferenceRoom(name, self)
            return self._rooms[name]

    async def _maybe_remove(self, name: str) -> None:
        async with self._lock:
            if name in self._rooms and self._rooms[name].member_count == 0:
                del self._rooms[name]


def _norm(name: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9]", "", name.upper())[:16]
    return cleaned or DEFAULT_ROOM


# Backwards-compatible alias
ConferenceHub = ConferenceHubManager
