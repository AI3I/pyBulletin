"""BBS session state machine.

One instance per connected client.  Drives the login flow, post-login
welcome, startup-command execution, and main command loop.  I/O is
mediated through the TelnetReader/TelnetWriter pair from the transport
layer so the session is transport-agnostic.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from ..auth import is_valid_call, normalize_call, verify_password
from ..auth_logging import log_auth_failure
from ..access_policy import CHANNEL_TELNET, CHANNEL_AX25

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..store.store import BBSStore
    from ..store.models import User
    from ..strings import StringCatalog
    from ..transport.telnet import TelnetReader, TelnetWriter, ConnectionMeta
    from ..transport.conference import ConferenceHub

LOG = logging.getLogger(__name__)

_MAX_AUTH_ATTEMPTS = 3
_CTRL_Z = "\x1a"


class BBSSession:
    """Async BBS session: login → command loop → goodbye."""

    def __init__(
        self,
        reader: TelnetReader,
        writer: TelnetWriter,
        meta: ConnectionMeta,
        cfg: AppConfig,
        store: BBSStore,
        strings: StringCatalog,
        *,
        heard_provider: Callable[[], list] | None = None,
        conference_hub: ConferenceHub | None = None,
    ) -> None:
        self._reader          = reader
        self._writer          = writer
        self._meta            = meta
        self._cfg             = cfg
        self._store           = store
        self._strings         = strings
        self._user: User | None = None
        self._channel         = CHANNEL_AX25 if meta.channel == "ax25" else CHANNEL_TELNET
        self._heard_provider  = heard_provider    # () -> list[(dt, call, port)]
        self._conference_hub  = conference_hub

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the full session lifecycle."""
        from ..command.engine import CommandEngine

        await self._send_banner()

        if not await self._authenticate():
            return

        await self._post_login()
        engine = CommandEngine(self)

        # Run any startup commands the user has configured
        startup = await self._store.get_startup_commands(self._user.call)
        for cmd in startup:
            await engine.dispatch(cmd)

        await self._command_loop(engine)

        cfg = self._cfg
        await self.send(self._strings.get(
            "connect.goodbye",
            node_call=cfg.node.node_call,
        ))

    # ------------------------------------------------------------------
    # Properties used by CommandEngine
    # ------------------------------------------------------------------

    @property
    def user(self) -> User | None:
        return self._user

    @property
    def cfg(self) -> AppConfig:
        return self._cfg

    @property
    def store(self) -> BBSStore:
        return self._store

    @property
    def strings(self) -> StringCatalog:
        return self._strings

    @property
    def meta(self) -> ConnectionMeta:
        return self._meta

    @property
    def heard_provider(self) -> Callable[[], list] | None:
        return self._heard_provider

    @property
    def conference_hub(self) -> ConferenceHub | None:
        return self._conference_hub

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    async def send(self, text: str) -> None:
        """Write text to the client and flush pending IAC replies."""
        self._writer.send(text)
        if self._reader.has_replies():
            self._writer.send_raw(self._reader.take_replies())
        await self._writer.drain()

    async def send_paged(self, text: str) -> None:
        """Send text with pagination if paging is enabled for this user."""
        page = self._user.page_length if self._user else 24
        if page <= 0:
            await self.send(text)
            return

        lines = text.split("\n")
        buf: list[str] = []
        line_count = 0

        for line in lines:
            buf.append(line)
            # Count only non-empty lines toward page length
            if line.strip():
                line_count += 1
            if line_count >= page:
                await self.send("\n".join(buf))
                buf.clear()
                line_count = 0
                await self.send(self._strings.get("connect.page_break"))
                resp = await self._readline()
                if resp.strip().upper() == "A":
                    await self.send(self._strings.get("error.aborted"))
                    return

        if buf:
            await self.send("\n".join(buf))

    async def _readline(self) -> str:
        """Read one line, flushing any pending IAC replies afterward."""
        line = await self._reader.readline()
        if self._reader.has_replies():
            self._writer.send_raw(self._reader.take_replies())
            await self._writer.drain()
        return line

    async def _readline_hidden(self) -> str:
        """Read a line with client echo suppressed (for password entry).

        Sends ``IAC WILL ECHO`` so the client stops echoing locally (the
        server is taking over echo responsibility, then deliberately not
        echoing anything).  Restores local echo with ``IAC WONT ECHO``
        afterward.  AX.25 sessions ignore these IAC bytes harmlessly.
        """
        from ..transport.telnet import IAC, WONT, WILL, OPT_ECHO
        # Server takes over echo → client stops local echo
        self._writer.send_raw(bytes([IAC, WILL, OPT_ECHO]))
        await self._writer.drain()
        line = await self._readline()
        # Server relinquishes echo → client resumes local echo
        self._writer.send_raw(bytes([IAC, WONT, OPT_ECHO]))
        self._writer.send("\r\n")
        await self._writer.drain()
        return line

    # ------------------------------------------------------------------
    # Banner
    # ------------------------------------------------------------------

    async def _send_banner(self) -> None:
        cfg = self._cfg
        s   = self._strings
        await self.send(s.get(
            "connect.banner",
            branding=cfg.node.branding_name,
            node_call=cfg.node.node_call,
        ))
        if cfg.node.motd:
            await self.send(s.get("connect.motd", motd=cfg.node.motd))
        await self.send(s.get("connect.login_tip", login_tip=cfg.node.login_tip))

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self) -> bool:
        cfg = self._cfg
        s   = self._strings
        peer = self._meta.peer

        for attempt in range(_MAX_AUTH_ATTEMPTS):
            # --- Callsign ---
            await self.send(s.get("auth.enter_call"))
            raw_call = await self._readline()
            call = raw_call.strip().upper()

            if not call:
                continue

            # Allow immediate disconnect
            if call in ("B", "BYE", "Q", "QUIT"):
                return False

            if not is_valid_call(call):
                await self.send(s.get("auth.bad_call"))
                log_auth_failure(call, peer, self._channel, "bad_callsign")
                continue

            # --- Password ---
            if cfg.node.require_password:
                await self.send(s.get("auth.enter_password"))
                password = await self._readline_hidden()

                user = await self._store.get_user(call)
                if user is None or not user.password_hash:
                    await self.send(s.get("auth.not_registered"))
                    log_auth_failure(call, peer, self._channel, "not_registered")
                    continue

                if not verify_password(password, user.password_hash):
                    await self.send(s.get("auth.bad_password"))
                    log_auth_failure(call, peer, self._channel, "bad_password")
                    continue

                self._user = await self._store.record_login(call, peer)
                LOG.info("session: authenticated %s from %s", call, peer)
                return True

            else:
                # Open node — auto-create on first connect
                self._user = await self._store.record_login(call, peer)
                LOG.info("session: open login %s from %s", call, peer)
                return True

        await self.send(s.get("auth.locked_out"))
        return False

    # ------------------------------------------------------------------
    # Post-login welcome
    # ------------------------------------------------------------------

    async def _post_login(self) -> None:
        assert self._user is not None
        cfg = self._cfg
        s   = self._strings

        new_mail = await self._store.count_messages(
            to_call=self._user.call,
            status="N",
        )
        await self.send(s.get(
            "connect.session_info",
            call=self._user.call,
            new_mail=new_mail,
        ))

        if cfg.node.show_status_after_login:
            # Brief node status line
            total = await self._store.count_messages()
            await self.send(
                f"  Node   : {cfg.node.node_call}  ({cfg.node.qth})\r\n"
                f"  Msgs   : {total} total, {new_mail} new for you\r\n"
            )

    # ------------------------------------------------------------------
    # Main command loop
    # ------------------------------------------------------------------

    async def _command_loop(self, engine: CommandEngine) -> None:
        assert self._user is not None
        cfg = self._cfg
        s   = self._strings

        while True:
            # Build prompt
            if self._user.expert_mode:
                prompt = s.get("connect.prompt_expert", node_call=cfg.node.node_call)
            else:
                new = await self._store.count_messages(
                    to_call=self._user.call, status="N"
                )
                suffix = f"({new})" if new else ""
                prompt = s.get(
                    "connect.prompt",
                    node_call=cfg.node.node_call,
                    suffix=suffix,
                )

            await self.send(prompt)
            line = await self._readline()
            line = line.strip()

            if not line:
                continue

            verb = line.split()[0].upper()
            if verb in ("B", "BYE", "Q", "QUIT", "G", "GB", "GE"):
                break

            try:
                await engine.dispatch(line)
            except Exception:
                LOG.exception("session: unhandled error in command dispatch")
                await self.send(s.get("error.internal"))
