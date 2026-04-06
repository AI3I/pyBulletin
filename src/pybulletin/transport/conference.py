"""BBS conference (real-time multi-user chat) hub.

The ConferenceHub is a shared asyncio object created once by ``serve-core``
and injected into each BBSSession.  When a user types ``C`` they enter
conference mode where every line they type is broadcast to all other
participants.

Usage in the command engine::

    hub = ConferenceHub()           # created once at startup

    # Inside a session handling the C command:
    async def _cmd_conference(self, args):
        await hub.run_session(callsign, reader, writer)

Wire format
-----------
Messages sent to the conference look like::

    <AI3I> Hello everyone!\r\n

System announcements (join/leave) look like::

    *** AI3I has joined the conference\r\n

Exit
----
The user types ``/Q``, ``/EXIT``, or sends a bare ``B``/``BYE`` line.
Ctrl-Z or an empty line is ignored so fat-finger disconnects don't boot
someone from the channel.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

LOG = logging.getLogger(__name__)

_QUEUE_DEPTH = 64       # max queued messages per participant
_READ_TIMEOUT = 600.0   # 10 minute idle limit in conference


class ConferenceHub:
    """Broadcast hub for the BBS conference channel."""

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()
        # callsign (upper) → asyncio.Queue of strings to send
        self._members: dict[str, asyncio.Queue[str]] = {}

    # ------------------------------------------------------------------
    # Public: called by the BBS command engine
    # ------------------------------------------------------------------

    async def run_session(
        self,
        callsign: str,
        readline_cb,   # async () -> str
        send_cb,       # async (str) -> None
    ) -> None:
        """Enter the conference and block until the user exits.

        *readline_cb* must be an async callable returning one line of user
        input (stripped CR/LF).  *send_cb* must be an async callable that
        writes text back to the user.

        Exits cleanly when the user types ``/Q``, ``B``, or ``BYE``.
        """
        call = callsign.upper()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_DEPTH)

        async with self._lock:
            self._members[call] = queue

        members_str = ", ".join(sorted(self._members))
        await send_cb(
            f"\r\n*** Entering conference.  Members: {members_str}\r\n"
            f"*** Type /Q or B to exit.\r\n\r\n"
        )
        await self._broadcast(f"*** {call} has joined the conference\r\n", exclude=call)

        try:
            await self._run_loop(call, queue, readline_cb, send_cb)
        finally:
            async with self._lock:
                self._members.pop(call, None)
            await self._broadcast(f"*** {call} has left the conference\r\n", exclude=None)
            await send_cb("\r\n*** You have left the conference.\r\n")
            LOG.info("conference: %s left (%d remaining)", call, len(self._members))

    @property
    def member_count(self) -> int:
        return len(self._members)

    @property
    def members(self) -> list[str]:
        return sorted(self._members)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _broadcast(self, text: str, exclude: str | None) -> None:
        async with self._lock:
            targets = {k: q for k, q in self._members.items() if k != exclude}
        for q in targets.values():
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                pass  # slow client — drop rather than block everyone else

    async def _run_loop(
        self,
        call: str,
        queue: asyncio.Queue[str],
        readline_cb,
        send_cb,
    ) -> None:
        """Race between user input and incoming conference messages."""
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

                if upper in ("/Q", "/EXIT", "B", "BYE", "Q", "QUIT"):
                    return
                if not stripped or upper == "\x1a":
                    continue

                msg = f"<{call}> {stripped}\r\n"
                # Echo to sender
                await send_cb(msg)
                # Broadcast to others
                await self._broadcast(msg, exclude=call)

            if recv_task in done:
                try:
                    text = recv_task.result()
                except Exception:
                    continue
                await send_cb(text)
