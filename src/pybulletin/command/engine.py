"""BBS command engine — Phase 2 + Phase 6 command set.

All commands operate via the BBSSession interface so they are
transport-agnostic.  Each handler is an async method that sends
output through session.send() / session.send_paged().

Command set:
  H / HELP / ?      — help
  V / VERSION        — version info
  I [call]           — node info / white pages lookup
  L [opts]           — list messages (since msg_base)
  LL [n]             — list last n messages
  LM / LP            — list my mail (personal)
  LB [cat]           — list bulletins (optionally filtered)
  LT                 — list NTS traffic
  LH                 — list held messages
  LK                 — list killed messages
  LF                 — list forwarded messages
  LY                 — list read messages
  L> n               — list from message n
  N                  — new message summary, advance msg_base
  NH [name]          — set display name
  NL [locator]       — set locator / QTH
  NQ [city]          — set city / QTH text
  NZ [zip]           — set ZIP / postal code
  R n [n ...]        — read message(s)
  S / SP to          — send personal mail (interactive compose)
  SB to              — send bulletin
  ST to              — send NTS traffic
  SC n call[@bbs]    — copy message to another recipient
  K n [n ...]        — kill message(s)
  KM                 — kill all my mail
  O                  — show options
  O param [value]    — set option (PAGER, EXPERT, LANG, BASE, PW)
  W                  — who / connections
  J                  — heard stations (RF channel only)
  P call             — white pages lookup (alias for I <call>)
  SH n [n ...]       — sysop: hold message(s)          [sysop]
  SR n [n ...]       — sysop: release held message(s)  [sysop]
  U [search]         — sysop: list users               [sysop]
  YL [area]          — list downloadable files
  YG <filename>      — download file via YAPP
  YU <filename>      — upload file via YAPP
  C                  — enter conference (multi-user chat)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .. import __version__
from ..auth import hash_password, verify_password
from ..store.models import (
    Message, FileEntry,
    MSG_BULLETIN, MSG_NTS, MSG_PRIVATE,
    STATUS_KILLED, STATUS_HELD, STATUS_NEW, STATUS_READ, STATUS_FORWARDED,
    PRIV_SYSOP,
)
from ..access_policy import (
    CAP_READ, CAP_SEND, CAP_SYSOP,
    access_allowed,
)

if TYPE_CHECKING:
    from ..session.session import BBSSession

LOG = logging.getLogger(__name__)

# Maximum lines collected during message compose
_MAX_BODY_LINES = 500


class CommandEngine:
    """Parse command lines and dispatch to handlers."""

    def __init__(self, session: BBSSession) -> None:
        self._s = session

        # Command routing table: first token (upper) → handler method
        self._table: dict[str, any] = {
            # Core
            "H":       self._cmd_help,
            "HELP":    self._cmd_help,
            "?":       self._cmd_help,
            "V":       self._cmd_version,
            "VERSION": self._cmd_version,
            # Info / WP
            "I":       self._cmd_info,
            "INFO":    self._cmd_info,
            "P":       self._cmd_wp,
            "WP":      self._cmd_wp,
            # Message browsing
            "N":       self._cmd_new,
            "L":       self._cmd_list,
            "LL":      self._cmd_list_last,
            "LM":      self._cmd_list_mine,
            "LP":      self._cmd_list_mine,       # FBB alias
            "LB":      self._cmd_list_bulletins,
            "LT":      self._cmd_list_nts,
            "LH":      self._cmd_list_held,
            "LK":      self._cmd_list_killed,
            "LF":      self._cmd_list_forwarded,
            "LY":      self._cmd_list_read,
            "R":       self._cmd_read,
            "RE":      self._cmd_read,
            # Message sending / copying
            "S":       self._cmd_send_private,
            "SP":      self._cmd_send_private,
            "SB":      self._cmd_send_bulletin,
            "ST":      self._cmd_send_nts,
            "SC":      self._cmd_copy_message,
            # Kill
            "K":       self._cmd_kill,
            "KM":      self._cmd_kill_mine,
            # Options / profile
            "O":       self._cmd_options,
            "NH":      self._cmd_nh,
            "NL":      self._cmd_nl,
            "NQ":      self._cmd_nq,
            "NZ":      self._cmd_nz,
            # Status
            "W":       self._cmd_who,
            "J":       self._cmd_heard,
            # Sysop
            "SH":      self._cmd_sysop_hold,
            "MH":      self._cmd_sysop_hold,     # FBB alias
            "SR":      self._cmd_sysop_release,
            "MR":      self._cmd_sysop_release,  # FBB alias
            "U":       self._cmd_users,
            # File transfer
            "YL":      self._cmd_yapp_list,
            "YG":      self._cmd_yapp_get,
            "YU":      self._cmd_yapp_upload,
            # Conference
            "C":       self._cmd_conference,
            # Sysop page
            "T":       self._cmd_page_sysop,
        }

    async def dispatch(self, line: str) -> None:
        """Parse *line* and call the appropriate handler."""
        if not line:
            return

        parts = line.split(None, 1)
        verb  = parts[0].upper()
        args  = parts[1].strip() if len(parts) > 1 else ""

        # Special: L> n
        if verb.startswith("L>"):
            n_str = verb[2:] or args
            await self._cmd_list_from(n_str)
            return

        # ?X  — detailed help for command X  (e.g. ?O, ?L, ?S)
        if verb.startswith("?") and len(verb) > 1:
            await self._cmd_help(verb[1:])
            return

        handler = self._table.get(verb)
        if handler is None:
            await self._s.send(self._s.strings.get("error.unknown_cmd"))
            return

        await handler(args)

    # ------------------------------------------------------------------
    # Convenience shorthands
    # ------------------------------------------------------------------

    @property
    def _st(self):
        return self._s.strings

    @property
    def _cfg(self):
        return self._s.cfg

    @property
    def _store(self):
        return self._s.store

    @property
    def _user(self):
        return self._s.user

    def _can(self, cap: str) -> bool:
        return access_allowed(self._user.call, cap, self._store)

    # ------------------------------------------------------------------
    # H — help
    # ------------------------------------------------------------------

    # Aliases → canonical command name for help lookup
    _HELP_ALIASES: dict[str, str] = {
        "SP": "S", "RE": "R", "WP": "I", "INFO": "I",
        "HELP": "H", "VERSION": "V",
        "LL": "L", "LM": "L", "LP": "L", "LB": "L", "LT": "L",
        "LH": "L", "LK": "L", "LF": "L", "LY": "L",
        "SC": "S",
        "KM": "K",
        "MH": "SH", "MR": "SR",
        "YG": "Y", "YU": "Y", "YL": "Y",
        "NH": "N", "NL": "N", "NQ": "N", "NZ": "N",
        "P": "I",
    }

    async def _cmd_help(self, args: str) -> None:
        cmd = args.strip().upper()
        if not cmd:
            await self._s.send(self._st.get("help.short"))
            return
        # Resolve alias to canonical name
        canonical = self._HELP_ALIASES.get(cmd, cmd)
        key  = f"help.cmd.{canonical}"
        text = self._st.get(key)
        if text == key:
            # Key not found — strings.get returns the key itself when missing
            await self._s.send(f"\r\nNo detailed help for {cmd}.\r\n")
        else:
            await self._s.send(text)

    # ------------------------------------------------------------------
    # V — version
    # ------------------------------------------------------------------

    async def _cmd_version(self, args: str) -> None:
        cfg = self._cfg
        await self._s.send(self._st.get(
            "info.version",
            branding=cfg.node.branding_name,
            version=__version__,
            node_call=cfg.node.node_call,
            qth=cfg.node.qth,
        ))

    # ------------------------------------------------------------------
    # I [call] — node info or white pages lookup
    # ------------------------------------------------------------------

    async def _cmd_info(self, args: str) -> None:
        call = args.strip().upper()
        if call:
            await self._wp_lookup(call)
            return

        cfg = self._cfg
        await self._s.send(self._st.get(
            "info.bbs_info",
            branding=cfg.node.branding_name,
            node_call=cfg.node.node_call,
            qth=cfg.node.qth,
            owner=cfg.node.owner_name,
        ))
        await self._s.send(self._st.get(
            "info.version",
            branding=cfg.node.branding_name,
            version=__version__,
            node_call=cfg.node.node_call,
            qth=cfg.node.qth,
        ))

    # ------------------------------------------------------------------
    # P <call> — white pages lookup
    # ------------------------------------------------------------------

    async def _cmd_wp(self, args: str) -> None:
        call = args.strip().upper()
        if not call:
            count = await self._store.count_wp_entries()
            await self._s.send(self._st.get("info.wp_count", count=count))
            return
        await self._wp_lookup(call)

    async def _wp_lookup(self, call: str) -> None:
        entry = await self._store.get_wp_entry(call)
        if entry:
            await self._s.send(self._st.get(
                "info.wp_header"
            ) + self._st.get(
                "info.wp_found",
                call=entry.call,
                home_bbs=entry.home_bbs,
                name=entry.name,
            ))
        else:
            await self._s.send(self._st.get("info.wp_not_found", call=call))

    # ------------------------------------------------------------------
    # N — new messages summary, advance msg_base
    # ------------------------------------------------------------------

    async def _cmd_new(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return

        msgs = await self._store.list_messages(
            to_call=self._user.call,
            status=STATUS_NEW,
            since_id=self._user.msg_base,
        )
        if not msgs:
            await self._s.send(self._st.get("list.no_msgs"))
            return

        output = self._st.get("list.header") + self._st.get("list.divider")
        for m in msgs:
            output += self._format_list_row(m)
        output += self._st.get("list.end", count=len(msgs))
        await self._s.send_paged(output)

        # Advance msg_base to highest message seen
        if msgs:
            self._user.msg_base = max(m.id for m in msgs)
            await self._store.upsert_user(self._user)

    # ------------------------------------------------------------------
    # NH [name] — set display name / home BBS
    # ------------------------------------------------------------------

    async def _cmd_nh(self, args: str) -> None:
        """NH <name> — set your display name (registered with White Pages)."""
        s = self._st
        user = self._user

        if args.strip():
            name = args.strip()[:40]
        else:
            await self._s.send(
                f"\r\n  Current name : {user.display_name or '(not set)'}\r\n"
                f"  Enter new name (blank to keep): "
            )
            name = (await self._s._readline()).strip()[:40]
            if not name:
                return

        user.display_name = name
        await self._store.upsert_user(user)

        # Update White Pages
        from ..store.models import WPEntry
        wp = await self._store.get_wp_entry(user.call) or WPEntry(call=user.call)
        wp.name     = name
        wp.home_bbs = user.home_bbs or self._cfg.node.node_call
        wp.source_bbs = self._cfg.node.node_call
        await self._store.upsert_wp_entry(wp)

        await self._s.send(f"\r\n  Name set to: {name}\r\n")
        LOG.info("session: %s set display_name=%r", user.call, name)

    # ------------------------------------------------------------------
    # NL [locator] — set locator / QTH
    # ------------------------------------------------------------------

    async def _cmd_nl(self, args: str) -> None:
        """NL <locator> — set your Maidenhead grid locator."""
        user = self._user

        if args.strip():
            locator = args.strip().upper()[:6]
        else:
            await self._s.send(
                f"\r\n  Current locator : {user.locator or '(not set)'}\r\n"
                f"  Enter Maidenhead locator (e.g. FN20, blank to keep): "
            )
            locator = (await self._s._readline()).strip().upper()[:6]
            if not locator:
                return

        user.locator = locator
        await self._store.upsert_user(user)
        await self._s.send(f"\r\n  Locator set to: {locator}\r\n")
        LOG.info("session: %s set locator=%r", user.call, locator)

    # ------------------------------------------------------------------
    # NQ [city] — set city / QTH text
    # ------------------------------------------------------------------

    async def _cmd_nq(self, args: str) -> None:
        """NQ <city> — set your city / QTH description."""
        user = self._user
        if args.strip():
            city = args.strip()[:40]
        else:
            await self._s.send(
                f"\r\n  Current QTH : {user.city or '(not set)'}\r\n"
                f"  Enter city/QTH (blank to keep): "
            )
            city = (await self._s._readline()).strip()[:40]
            if not city:
                return
        user.city = city
        await self._store.upsert_user(user)
        await self._s.send(f"\r\n  QTH set to: {city}\r\n")
        LOG.info("session: %s set city=%r", user.call, city)

    # ------------------------------------------------------------------
    # NZ [zip] — set ZIP / postal code
    # ------------------------------------------------------------------

    async def _cmd_nz(self, args: str) -> None:
        """NZ <zip> — set your ZIP / postal code."""
        user = self._user
        if args.strip():
            zip_code = args.strip()[:10]
        else:
            await self._s.send(
                f"\r\n  Current ZIP : {user.zip_code or '(not set)'}\r\n"
                f"  Enter ZIP/postal code (blank to keep): "
            )
            zip_code = (await self._s._readline()).strip()[:10]
            if not zip_code:
                return
        user.zip_code = zip_code
        await self._store.upsert_user(user)
        await self._s.send(f"\r\n  ZIP set to: {zip_code}\r\n")
        LOG.info("session: %s set zip_code=%r", user.call, zip_code)

    # ------------------------------------------------------------------
    # L — list messages (since msg_base)
    # ------------------------------------------------------------------

    async def _cmd_list(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return

        msgs = await self._store.list_messages(since_id=self._user.msg_base)
        await self._send_list(msgs)

    async def _cmd_list_last(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        try:
            n = int(args.strip()) if args.strip() else 20
        except ValueError:
            n = 20
        msgs = await self._store.list_messages(limit=n, reverse=True)
        msgs = list(reversed(msgs))
        await self._send_list(msgs)

    async def _cmd_list_mine(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        msgs = await self._store.list_messages(to_call=self._user.call)
        await self._send_list(msgs)

    async def _cmd_list_bulletins(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        to_call = args.strip().upper() if args.strip() else None
        msgs = await self._store.list_messages(
            msg_type=MSG_BULLETIN,
            to_call=to_call,
        )
        await self._send_list(msgs)

    async def _cmd_list_nts(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        msgs = await self._store.list_messages(msg_type=MSG_NTS)
        await self._send_list(msgs)

    async def _cmd_list_from(self, n_str: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        try:
            n = int(n_str.strip())
        except ValueError:
            await self._s.send("\r\n  Usage: L <msg#>\r\n")
            return
        msgs = await self._store.list_messages(since_id=n - 1)
        await self._send_list(msgs)

    async def _cmd_list_held(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        msgs = await self._store.list_messages(status=STATUS_HELD)
        await self._send_list(msgs)

    async def _cmd_list_killed(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        msgs = await self._store.list_messages(status=STATUS_KILLED)
        await self._send_list(msgs)

    async def _cmd_list_forwarded(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        msgs = await self._store.list_messages(status=STATUS_FORWARDED)
        await self._send_list(msgs)

    async def _cmd_list_read(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        msgs = await self._store.list_messages(status=STATUS_READ)
        await self._send_list(msgs)

    async def _send_list(self, msgs: list[Message]) -> None:
        if not msgs:
            await self._s.send(self._st.get("list.no_msgs"))
            return
        output = self._st.get("list.header") + self._st.get("list.divider")
        for m in msgs:
            output += self._format_list_row(m)
        output += self._st.get("list.end", count=len(msgs))
        await self._s.send_paged(output)

    def _format_list_row(self, m: Message) -> str:
        date_str = m.created_at.strftime("%d-%b-%y") if m.created_at else "?"
        return self._st.get(
            "list.row",
            id=m.id,
            type=m.msg_type,
            status=m.status,
            size=m.size,
            to=m.to_call[:10],
            **{"from": m.from_call[:10]},
            date=date_str,
            subject=m.subject[:28],
        )

    # ------------------------------------------------------------------
    # R n — read message(s)
    # ------------------------------------------------------------------

    async def _cmd_read(self, args: str) -> None:
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return

        ids = self._parse_id_list(args)
        if not ids:
            await self._s.send("\r\n  Usage: R <msg#> [msg# ...]\r\n")
            return

        for msg_id in ids:
            await self._read_one(msg_id)

    async def _read_one(self, msg_id: int) -> None:
        msg = await self._store.get_message(msg_id)
        if msg is None:
            await self._s.send(self._st.get("read.not_found", id=msg_id))
            return

        if msg.status == STATUS_KILLED:
            await self._s.send(self._st.get("read.killed", id=msg_id))
            return

        # Personal mail: only addressee or sysop may read
        if msg.msg_type == MSG_PRIVATE:
            call = self._user.call
            is_mine = (call == msg.to_call or call == msg.from_call
                       or self._user.privilege == PRIV_SYSOP)
            if not is_mine:
                await self._s.send(self._st.get("read.not_yours", id=msg_id))
                return

        s = self._st
        date_str = msg.created_at.strftime("%Y-%m-%d %H:%M UTC") if msg.created_at else "?"
        type_names = {MSG_PRIVATE: "Private", MSG_BULLETIN: "Bulletin", MSG_NTS: "NTS"}

        header = (
            s.get("read.header_from",    from_call=msg.from_call)
            + s.get("read.header_to",    to_call=msg.to_call)
        )
        if msg.at_bbs:
            header += s.get("read.header_at", at_bbs=msg.at_bbs)
        header += (
            s.get("read.header_date",    date=date_str)
            + s.get("read.header_subject", subject=msg.subject)
            + s.get("read.header_bid",   bid=msg.bid)
            + s.get("read.header_size",  size=msg.size)
            + s.get("read.separator")
            + msg.body.replace("\n", "\r\n")
        )
        if msg.edited_by:
            edit_date = (msg.edited_at.strftime("%Y-%m-%d %H:%M UTC")
                         if msg.edited_at else "?")
            header += f"\r\n[Edited by {msg.edited_by} on {edit_date}]\r\n"
        header += s.get("read.footer", id=msg_id, type=type_names.get(msg.msg_type, "?"))
        await self._s.send_paged(header)

        # Mark personal mail as read
        if msg.msg_type == MSG_PRIVATE and msg.status == STATUS_NEW:
            await self._store.mark_read(msg_id, self._user.call)

    # ------------------------------------------------------------------
    # S / SP / SB / ST — send message (interactive compose)
    # ------------------------------------------------------------------

    async def _cmd_send_private(self, args: str) -> None:
        await self._compose(args, MSG_PRIVATE)

    async def _cmd_send_bulletin(self, args: str) -> None:
        await self._compose(args, MSG_BULLETIN)

    async def _cmd_send_nts(self, args: str) -> None:
        await self._compose(args, MSG_NTS)

    async def _cmd_copy_message(self, args: str) -> None:
        """SC n call[@bbs] — copy message n to another recipient."""
        if not self._can(CAP_SEND):
            await self._s.send(self._st.get("error.no_permission"))
            return
        parts = args.split(None, 1)
        if len(parts) < 2:
            await self._s.send("\r\n  Usage: SC <msg#> <callsign[@bbs]>\r\n")
            return
        try:
            src_id = int(parts[0])
        except ValueError:
            await self._s.send("\r\n  Usage: SC <msg#> <callsign[@bbs]>\r\n")
            return
        dest = parts[1].strip().upper()
        at_bbs = ""
        if "@" in dest:
            dest, at_bbs = dest.split("@", 1)

        src = await self._store.get_message(src_id)
        if src is None:
            await self._s.send(self._st.get("read.not_found", id=src_id))
            return

        copy = Message(
            msg_type=MSG_PRIVATE,
            from_call=self._user.call,
            to_call=dest,
            at_bbs=at_bbs,
            subject=src.subject,
            body=src.body,
        )
        new_id = await self._store.insert_message(copy)
        await self._s.send(f"\r\n  Message {src_id} copied to {dest} as #{new_id}.\r\n")
        LOG.info("session: %s copied message %d to %s as %d",
                 self._user.call, src_id, dest, new_id)

    async def _compose(self, to_arg: str, msg_type: str) -> None:
        if not self._can(CAP_SEND):
            await self._s.send(self._st.get("error.no_permission"))
            return

        s   = self._st
        cfg = self._cfg

        # --- To ---
        if to_arg.strip():
            to_call = to_arg.strip().upper()
        else:
            await self._s.send(s.get("send.enter_to"))
            to_call = (await self._s._readline()).strip().upper()
        if not to_call:
            await self._s.send(s.get("error.aborted"))
            return

        # --- At BBS (only for private mail) ---
        at_bbs = ""
        if msg_type == MSG_PRIVATE:
            await self._s.send(s.get("send.enter_at"))
            at_bbs = (await self._s._readline()).strip().upper()

        # --- Subject ---
        await self._s.send(s.get("send.enter_subject"))
        subject = (await self._s._readline()).strip()
        if not subject:
            await self._s.send(s.get("error.aborted"))
            return

        # --- Body ---
        await self._s.send(s.get("send.enter_body"))
        body_lines: list[str] = []
        max_bytes = cfg.rate_limit.max_message_body_bytes

        while len(body_lines) < _MAX_BODY_LINES:
            line = await self._s._readline()
            # /EX or lone ctrl-Z ends composition
            if line.strip() in ("/EX", "/ex", "\x1a", "***"):
                break
            body_lines.append(line)
            if sum(len(l.encode()) for l in body_lines) > max_bytes:
                await self._s.send(s.get(
                    "send.too_large", max=max_bytes
                ))
                return

        body = "\n".join(body_lines)
        msg = Message(
            msg_type=msg_type,
            from_call=self._user.call,
            to_call=to_call,
            at_bbs=at_bbs,
            subject=subject,
            body=body,
        )
        msg_id = await self._store.insert_message(msg)
        await self._s.send(s.get("send.saved", id=msg_id))
        LOG.info("session: %s sent message %d to %s", self._user.call, msg_id, to_call)

    # ------------------------------------------------------------------
    # K n — kill message(s)
    # ------------------------------------------------------------------

    async def _cmd_kill(self, args: str) -> None:
        ids = self._parse_id_list(args)
        if not ids:
            await self._s.send("\r\n  Usage: K <msg#> [msg# ...]\r\n")
            return
        for msg_id in ids:
            await self._kill_one(msg_id)

    async def _cmd_kill_mine(self, args: str) -> None:
        """KM — kill all personal mail addressed to me."""
        msgs = await self._store.list_messages(
            to_call=self._user.call,
            msg_type=MSG_PRIVATE,
        )
        if not msgs:
            await self._s.send(self._st.get("list.no_msgs"))
            return
        for m in msgs:
            await self._kill_one(m.id)

    async def _kill_one(self, msg_id: int) -> None:
        s = self._st
        msg = await self._store.get_message(msg_id)
        if msg is None:
            await self._s.send(s.get("kill.not_found", id=msg_id))
            return
        if msg.status == STATUS_HELD:
            await self._s.send(s.get("kill.held", id=msg_id))
            return

        is_sysop = self._user.privilege == PRIV_SYSOP
        is_mine  = (self._user.call in (msg.from_call, msg.to_call))
        if not (is_mine or is_sysop):
            await self._s.send(s.get("kill.not_yours"))
            return

        await self._store.kill_message(msg_id)
        await self._s.send(s.get("kill.ok", id=msg_id))
        LOG.info("session: %s killed message %d", self._user.call, msg_id)

    # ------------------------------------------------------------------
    # O — options (includes O PW for password change)
    # ------------------------------------------------------------------

    async def _cmd_options(self, args: str) -> None:
        s    = self._st
        user = self._user

        if not args.strip():
            # Display current options
            await self._s.send(s.get("options.header", call=user.call))
            await self._s.send(s.get(
                "options.paging",
                state="ON" if user.page_length > 0 else "OFF",
                lines=user.page_length,
            ))
            await self._s.send(s.get(
                "options.expert_mode",
                state="ON" if user.expert_mode else "OFF",
            ))
            await self._s.send(s.get("options.language", lang=user.language))
            await self._s.send(s.get("options.msg_base", base=user.msg_base))
            return

        parts = args.split(None, 1)
        param = parts[0].upper()
        value = parts[1].strip() if len(parts) > 1 else ""

        # Password change handled separately (interactive, hidden input)
        if param in ("PW", "PASSWORD"):
            await self._change_password()
            return

        changed = False
        if param == "PAGER" and value.upper() in ("ON", "OFF"):
            user.page_length = 24 if value.upper() == "ON" else 0
            changed = True
        elif param == "EXPERT" and value.upper() in ("ON", "OFF"):
            user.expert_mode = value.upper() == "ON"
            changed = True
        elif param == "LANG" and value:
            user.language = value[:5].upper()
            changed = True
        elif param in ("BASE", "MSGBASE"):
            try:
                user.msg_base = int(value)
                changed = True
            except ValueError:
                pass

        if changed:
            await self._store.upsert_user(user)
            await self._s.send(s.get("options.changed"))
        else:
            await self._s.send(s.get("options.invalid"))

    async def _change_password(self) -> None:
        """Interactive password change (O PW)."""
        s    = self._st
        user = self._user

        # Verify current password — labels right-justified to colon at column 18
        await self._s.send(f"  {'Current password':>16}: ")
        old_pw = await self._s._readline_hidden()
        if not verify_password(old_pw, user.password_hash):
            await self._s.send("  Incorrect password.\r\n")
            return

        await self._s.send(f"  {'New password':>16}: ")
        new_pw = await self._s._readline_hidden()
        if len(new_pw) < 6:
            await self._s.send("  Password must be at least 6 characters.\r\n")
            return

        await self._s.send(f"  {'Confirm password':>16}: ")
        confirm = await self._s._readline_hidden()
        if new_pw != confirm:
            await self._s.send("  Passwords do not match.\r\n")
            return

        user.password_hash = hash_password(new_pw)
        await self._store.upsert_user(user)
        await self._s.send("  Password changed successfully.\r\n")
        LOG.info("session: %s changed password", user.call)

    # ------------------------------------------------------------------
    # W — who / connections
    # ------------------------------------------------------------------

    async def _cmd_page_sysop(self, args: str) -> None:
        """T — send a page request to the sysop."""
        user = self._user
        LOG.info("session: %s paged sysop from %s", user.call, self._s.meta.peer)
        # Best-effort: store a paged-notification message to SYSOP
        try:
            from ..store.models import Message, MSG_PRIVATE
            note = Message(
                msg_type=MSG_PRIVATE,
                from_call=user.call,
                to_call="SYSOP",
                subject=f"PAGE from {user.call}",
                body=f"{user.call} is requesting sysop assistance "
                     f"(telnet from {self._s.meta.peer}).\r\n",
            )
            await self._store.insert_message(note)
        except Exception:
            pass
        await self._s.send(self._st.get("sysop.paged"))

    # ------------------------------------------------------------------
    # W — who / connections
    # ------------------------------------------------------------------

    async def _cmd_who(self, args: str) -> None:
        await self._s.send(
            f"\r\n  {self._user.call} (you) — {self._s.meta.peer}\r\n"
        )

    # ------------------------------------------------------------------
    # J — heard stations (RF channel)
    # ------------------------------------------------------------------

    async def _cmd_heard(self, args: str) -> None:
        hp = self._s.heard_provider
        if hp is None:
            await self._s.send(
                "\r\n  Heard list only available on RF (AX.25) sessions.\r\n"
            )
            return

        entries = hp()   # list of (datetime, callsign, port)
        if not entries:
            await self._s.send("\r\n  No stations heard recently.\r\n")
            return

        # Show most-recent 20, newest first
        recent = list(reversed(entries[-20:]))
        lines = ["\r\n  Recently heard stations:\r\n",
                 "  " + "-" * 42 + "\r\n"]
        for dt, call, port in recent:
            age = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
            lines.append(f"  {call:<10}  port {port}  {age:>4} min ago\r\n")
        await self._s.send("".join(lines))

    # ------------------------------------------------------------------
    # SH / MH — sysop hold message(s)
    # ------------------------------------------------------------------

    async def _cmd_sysop_hold(self, args: str) -> None:
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        ids = self._parse_id_list(args)
        if not ids:
            await self._s.send("\r\n  Usage: SH <msg#> [msg# ...]\r\n")
            return
        for msg_id in ids:
            ok = await self._store.hold_message(msg_id)
            if ok:
                await self._s.send(f"\r\n  Message {msg_id} held.\r\n")
                LOG.info("session: sysop %s held message %d", self._user.call, msg_id)
            else:
                await self._s.send(f"\r\n  Message {msg_id} not found.\r\n")

    # ------------------------------------------------------------------
    # SR / MR — sysop release held message(s)
    # ------------------------------------------------------------------

    async def _cmd_sysop_release(self, args: str) -> None:
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        ids = self._parse_id_list(args)
        if not ids:
            await self._s.send("\r\n  Usage: SR <msg#> [msg# ...]\r\n")
            return
        for msg_id in ids:
            ok = await self._store.release_message(msg_id)
            if ok:
                await self._s.send(f"\r\n  Message {msg_id} released.\r\n")
                LOG.info("session: sysop %s released message %d", self._user.call, msg_id)
            else:
                await self._s.send(f"\r\n  Message {msg_id} not found or not held.\r\n")

    # ------------------------------------------------------------------
    # U [search] — user list (sysop)
    # ------------------------------------------------------------------

    async def _cmd_users(self, args: str) -> None:
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return

        search = args.strip() or None
        users  = await self._store.list_users(search=search, limit=100)
        if not users:
            await self._s.send("\r\n  No users found.\r\n")
            return

        lines = [
            "\r\n",
            f"  {'Call':<10} {'Priv':<6} {'Name':<20} {'Last login':<20}\r\n",
            "  " + "-" * 60 + "\r\n",
        ]
        for u in users:
            last = u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "never"
            lines.append(
                f"  {u.call:<10} {u.privilege:<6} {(u.display_name or '')[:20]:<20} {last}\r\n"
            )
        lines.append(f"\r\n  {len(users)} user(s).\r\n")
        await self._s.send_paged("".join(lines))

    # ------------------------------------------------------------------
    # YL [area] — list files
    # ------------------------------------------------------------------

    async def _cmd_yapp_list(self, args: str) -> None:
        area = args.strip() or None
        files = await self._store.list_files(area=area)
        if not files:
            area_str = f" in area '{area}'" if area else ""
            await self._s.send(f"\r\n  No files available{area_str}.\r\n")
            return

        lines = [
            "\r\n",
            f"  {'Filename':<24} {'Size':>8}  {'Area':<12}  Description\r\n",
            "  " + "-" * 70 + "\r\n",
        ]
        for f in files:
            lines.append(
                f"  {f.filename[:24]:<24} {f.size:>8}  {(f.area or 'general')[:12]:<12}"
                f"  {f.description[:30]}\r\n"
            )
        await self._s.send_paged("".join(lines))

    # ------------------------------------------------------------------
    # YG <filename> — download file via YAPP
    # ------------------------------------------------------------------

    async def _cmd_yapp_get(self, args: str) -> None:
        from ..transport.yapp import send_file

        filename = args.strip()
        if not filename:
            await self._s.send("\r\n  Usage: YG <filename>\r\n")
            return

        entry = await self._store.get_file_entry(filename)
        if entry is None:
            await self._s.send(f"\r\n  File not found: {filename}\r\n")
            return

        # Locate file on disk
        files_root = Path(self._cfg.store.files_path)
        file_path  = files_root / (entry.area or "") / entry.filename
        if not file_path.exists():
            await self._s.send(f"\r\n  File not available: {filename}\r\n")
            return

        data = file_path.read_bytes()
        await self._s.send(f"\r\n  Starting YAPP transfer: {filename} ({len(data)} bytes)\r\n")
        LOG.info("session: %s downloading %s via YAPP", self._user.call, filename)

        ok = await send_file(data, self._s._writer, self._s._reader, filename)
        if ok:
            await self._store.increment_downloads(filename, entry.area or "")
            await self._s.send(f"\r\n  Transfer complete.\r\n")
            LOG.info("session: %s YAPP download of %s complete", self._user.call, filename)
        else:
            await self._s.send(f"\r\n  Transfer aborted or failed.\r\n")

    # ------------------------------------------------------------------
    # YU <filename> — upload file via YAPP
    # ------------------------------------------------------------------

    async def _cmd_yapp_upload(self, args: str) -> None:
        from ..transport.yapp import receive_file
        from ..store.models import FileEntry

        parts    = args.split(None, 1)
        filename = parts[0].strip() if parts else ""
        if not filename:
            await self._s.send("\r\n  Usage: YU <filename>\r\n")
            return

        # Sanitize: no directory traversal
        safe_name = Path(filename).name
        if not safe_name or safe_name != filename:
            await self._s.send("\r\n  Invalid filename.\r\n")
            return

        await self._s.send(f"\r\n  Ready to receive '{safe_name}' via YAPP.\r\n  (ok)\r\n")

        data = await receive_file(self._s._writer, self._s._reader)
        if data is None:
            await self._s.send("\r\n  Upload aborted or failed.\r\n")
            return

        # Write to disk
        files_root = Path(self._cfg.store.files_path)
        files_root.mkdir(parents=True, exist_ok=True)
        dest = files_root / safe_name
        dest.write_bytes(data)

        entry = FileEntry(
            filename=safe_name,
            area="",
            owner=self._user.call,
            size=len(data),
        )
        await self._store.upsert_file_entry(entry)
        await self._s.send(f"\r\n  File received: {safe_name} ({len(data)} bytes)\r\n")
        LOG.info("session: %s uploaded %s (%d bytes) via YAPP",
                 self._user.call, safe_name, len(data))

    # ------------------------------------------------------------------
    # C — conference (multi-user real-time chat)
    # ------------------------------------------------------------------

    async def _cmd_conference(self, args: str) -> None:
        hub = self._s.conference_hub
        if hub is None:
            await self._s.send("\r\n  Conference not available on this node.\r\n")
            return

        async def _send(text: str) -> None:
            await self._s.send(text)

        async def _read() -> str:
            return await self._s._readline()

        await hub.run_session(self._user.call, _read, _send)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_id_list(args: str) -> list[int]:
        """Parse 'n [n n ...]' or 'n-m' into a list of message IDs."""
        ids: list[int] = []
        for token in args.split():
            if "-" in token:
                try:
                    lo, hi = token.split("-", 1)
                    ids.extend(range(int(lo), int(hi) + 1))
                except ValueError:
                    pass
            else:
                try:
                    ids.append(int(token))
                except ValueError:
                    pass
        return ids
