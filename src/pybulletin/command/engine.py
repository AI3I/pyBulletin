"""BBS command engine.

All commands operate via the BBSSession interface so they are
transport-agnostic.  Each handler is an async method that sends
output through session.send() / session.send_paged().

Command set:
  H / HELP / ?      — help
  V / VERSION        — version info
  I [call]           — node info / white pages lookup
  L [opts]           — list messages (since msg_base)
  LA                 — list all messages (alias for L)
  LL [n]             — list last n messages
  LM / LP            — list my mail (personal)
  LB [cat]           — list bulletins (optionally filtered)
  LT                 — list NTS traffic
  LH                 — list held messages
  LK                 — list killed messages
  LF                 — list forwarded messages
  LY                 — list read messages
  LW                 — list worldwide (WW) bulletins
  LS <text>          — search messages by subject/to/from
  LD <MMDD|YYYYMMDD> — list messages since a date
  L> n               — list from message n
  N                  — new message summary, advance msg_base
  RA                 — read all new personal mail sequentially
  NH [name]          — set display name
  NL [locator]       — set locator / QTH
  NQ [city]          — set city / QTH text
  NZ [zip]           — set ZIP / postal code
  NB [call]          — set home BBS
  R n [n ...]        — read message(s)
  RP n               — reply to message n (pre-fills To: and Re: subject)
  S / SP to          — send personal mail (interactive compose)
  SB to              — send bulletin
  ST / SN to         — send NTS traffic
  SC n call[@bbs]    — copy message to another recipient
  K / D / KILL n     — kill message(s)
  KM                 — kill all my mail
  O                  — show options
  O param [value]    — set option (PAGER, LINES, EXPERT, LANG, BASE, COLS, PW)
  X                  — toggle expert mode (shorthand for O EXPERT ON/OFF)
  BBS                — list known neighbor nodes
  W / WHO            — who / session info
  WHOAMI             — show current callsign and profile
  DATE / TIME        — show current UTC date and time
  STATS              — node statistics
  J                  — heard stations (RF channel only)
  P call             — white pages lookup (alias for I <call>)
  WPS name           — search White Pages by name or partial call
  F [neighbor]       — trigger forward to neighbor (sysop only)
  G / GB / GE        — goodbye (same as B)
  SH n [n ...]       — sysop: hold message(s)          [sysop]
  SR n [n ...]       — sysop: release held message(s)  [sysop]
  ED n               — sysop: edit message via terminal [sysop]
  MOVE n call[@bbs]  — sysop: reassign message          [sysop]
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
            "H":  self._cmd_help,
            "?":  self._cmd_help,
            "V":  self._cmd_version,
            # Info / WP
            "I":  self._cmd_info,
            "P":  self._cmd_wp,
            # Message browsing / reading
            "N":  self._cmd_new,
            "RA": self._cmd_read_all,
            "L":  self._cmd_list,
            "LN": self._cmd_list_new_login,
            "LR": self._cmd_list_reverse,
            "LL": self._cmd_list_last,
            "LM": self._cmd_list_mine,
            "LP": self._cmd_list_mine,
            "LB": self._cmd_list_bulletins,
            "LT": self._cmd_list_nts,
            "LH": self._cmd_list_held,
            "LK": self._cmd_list_killed,
            "LF": self._cmd_list_forwarded,
            "LY": self._cmd_list_read,
            "LW": self._cmd_list_ww,
            "LS": self._cmd_list_search,
            "LD": self._cmd_list_date,
            "R":  self._cmd_read,
            "RP": self._cmd_reply,
            # Message sending / copying
            "S":  self._cmd_send_private,
            "SB": self._cmd_send_bulletin,
            "ST": self._cmd_send_nts,
            "SN": self._cmd_send_nts,
            "SC": self._cmd_copy_message,
            # Kill
            "K":  self._cmd_kill,
            "D":  self._cmd_kill,
            "RM": self._cmd_kill,
            "KM": self._cmd_kill_mine,
            "KK": self._cmd_kill_bulk,
            # Options / profile
            "O":  self._cmd_options,
            "NH": self._cmd_nh,
            "NL": self._cmd_nl,
            "NQ": self._cmd_nq,
            "NZ": self._cmd_nz,
            "NB": self._cmd_nb,
            # Forwarding (sysop)
            "F":  self._cmd_forward,
            "FL": self._cmd_forward_list,
            "FN": self._cmd_forward_path,
            "FD": self._cmd_forward_drop,
            # Sysop message management
            "$":  self._cmd_msg_status,
            "EM": self._cmd_edit_body,
            "ED": self._cmd_edit_terminal,
            "MV": self._cmd_move,
            "SH": self._cmd_sysop_hold,
            "MH": self._cmd_sysop_hold,
            "SR": self._cmd_sysop_release,
            "MR": self._cmd_sysop_release,
            # Status / info
            "DT": self._cmd_datetime,
            "NS": self._cmd_stats,
            "ME": self._cmd_whoami,
            "X":  self._cmd_expert_toggle,
            "BB": self._cmd_bbs_list,
            "W":  self._cmd_who,
            "J":  self._cmd_heard,
            "WS": self._cmd_wp_search,
            # Sysop user/WP management
            "U":  self._cmd_users,
            "DU": self._cmd_user_detail,
            "DS": self._cmd_sysop_list,
            "EU": self._cmd_user_edit,
            "IL": self._cmd_wp_detail,
            "IE": self._cmd_wp_edit,
            # File transfer
            "Y":  self._cmd_yapp_list,
            "YL": self._cmd_yapp_list,
            "YG": self._cmd_yapp_get,
            "YU": self._cmd_yapp_upload,
            # Conference / page
            "C":  self._cmd_conference,
            "T":  self._cmd_page_sysop,
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

        # Special: K> call  K< call  K@ bbs — bulk kill by from/to/bbs
        if verb in ("K>", "K<", "K@"):
            await self._cmd_kill_bulk(verb[1:] + " " + args)
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
        "RP": "R", "RA": "R",
        "LL": "L", "LM": "L", "LB": "L", "LT": "L",
        "LH": "L", "LK": "L", "LF": "L", "LY": "L",
        "LW": "L", "LS": "L", "LD": "L", "LN": "L", "LR": "L",
        "SC": "S",
        "KM": "K", "KK": "K",
        "EM": "SH", "ED": "SH", "MV": "SH", "$": "SH",
        "YG": "Y", "YU": "Y", "YL": "Y",
        "NH": "N", "NL": "N", "NQ": "N", "NZ": "N", "NB": "N",
        "P": "I", "WS": "I", "BB": "I",
        "IL": "IL", "IE": "IL",
        "ME": "W",
        "DT": "V", "NS": "V",
        "X": "O",
        "DU": "U", "DS": "U", "EU": "U",
        "FL": "F", "FN": "F", "FD": "F", "FS": "F",
    }

    async def _cmd_help(self, args: str) -> None:
        cmd = args.strip().upper()
        if not cmd:
            await self._s.send(self._st.get("help.compact"))
            return
        if cmd == "?":
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
        user  = await self._store.get_user(call)
        if not entry and not user:
            await self._s.send(self._st.get("info.wp_not_found", call=call))
            return
        parts = [call]
        name     = (user and user.display_name) or (entry and entry.name) or ""
        home_bbs = (user and user.home_bbs) or (entry and entry.home_bbs) or ""
        locator  = (user and user.locator) or ""
        city     = (user and user.city)    or ""
        if name:     parts.append(name)
        if home_bbs: parts.append(f"@{home_bbs}")
        if locator:  parts.append(locator)
        if city:     parts.append(city)
        await self._s.send(self._st.get("info.wp_card", details="  ".join(parts)))

    async def _cmd_wp_search(self, args: str) -> None:
        """WPS name — search White Pages by name or partial callsign."""
        term = args.strip()
        if not term:
            await self._s.send(self._st.get("error.usage", cmd="WS <name>"))
            return
        entries = await self._store.search_wp(term)
        if not entries:
            await self._s.send(self._st.get("info.wp_search_none", term=term))
            return
        lines = ["\r\n"]
        for e in entries:
            lines.append(self._st.get(
                "info.wp_search_row",
                call=e.call,
                name=(e.name or "")[:20],
                home_bbs=e.home_bbs,
            ))
        lines.append(self._st.get("list.end", count=len(entries)))
        await self._s.send_paged("".join(lines))

    # ------------------------------------------------------------------
    # DATE / TIME — current UTC clock
    # ------------------------------------------------------------------

    async def _cmd_datetime(self, args: str) -> None:
        now = datetime.now(timezone.utc)
        await self._s.send(
            f"\r\n  {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\r\n"
        )

    # ------------------------------------------------------------------
    # STATS — node statistics
    # ------------------------------------------------------------------

    async def _cmd_stats(self, args: str) -> None:
        total   = await self._store.count_messages()
        new_p   = await self._store.count_messages(status=STATUS_NEW, msg_type=MSG_PRIVATE)
        new_b   = await self._store.count_messages(status=STATUS_NEW, msg_type=MSG_BULLETIN)
        users   = len(await self._store.list_users())
        wp      = await self._store.count_wp_entries()
        cfg     = self._cfg
        lines = [
            "\r\n",
            f"  Node     : {cfg.node.node_call}  ({cfg.node.qth})\r\n",
            f"  Messages : {total} total  {new_p} new private  {new_b} new bulletins\r\n",
            f"  Users    : {users}\r\n",
            f"  WP entries: {wp}\r\n",
        ]
        neighbors = cfg.forward.neighbors
        if neighbors:
            lines.append(f"  Neighbors: {', '.join(n.call for n in neighbors)}\r\n")
        await self._s.send("".join(lines))

    # ------------------------------------------------------------------
    # WHOAMI — show current session info
    # ------------------------------------------------------------------

    async def _cmd_whoami(self, args: str) -> None:
        user = self._user
        await self._s.send(
            f"\r\n  Callsign : {user.call}\r\n"
            f"  Name     : {user.display_name or '(not set)'}\r\n"
            f"  Privilege: {user.privilege or 'user'}\r\n"
            f"  Home BBS : {user.home_bbs or self._cfg.node.node_call}\r\n"
            f"  Msg base : {user.msg_base}\r\n"
        )

    # ------------------------------------------------------------------
    # X — toggle expert mode (shorthand for O EXPERT ON/OFF)
    # ------------------------------------------------------------------

    async def _cmd_expert_toggle(self, args: str) -> None:
        user = self._user
        user.expert_mode = not user.expert_mode
        await self._store.upsert_user(user)
        state = "ON" if user.expert_mode else "OFF"
        await self._s.send(f"\r\n  Expert mode {state}.\r\n")

    # ------------------------------------------------------------------
    # BBS — list known neighbor nodes
    # ------------------------------------------------------------------

    async def _cmd_bbs_list(self, args: str) -> None:
        neighbors = self._cfg.forward.neighbors
        if not neighbors:
            await self._s.send("\r\n  No neighbor nodes configured.\r\n")
            return
        lines = [
            "\r\n",
            f"  {'Call':<12} {'Address':<24} {'Schedule':<14}  Status\r\n",
            "  " + "-" * 64 + "\r\n",
        ]
        for n in neighbors:
            status = "enabled" if n.enabled else "disabled"
            lines.append(
                f"  {n.call:<12} {(n.address or '(RF)')[:24]:<24}"
                f" {n.schedule:<14}  {status}\r\n"
            )
        await self._s.send("".join(lines))

    # ------------------------------------------------------------------
    # ED n — sysop: terminal edit of a message
    # ------------------------------------------------------------------

    async def _cmd_edit_terminal(self, args: str) -> None:
        """ED n — interactively edit subject and body of message n."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        ids = self._parse_id_list(args)
        if not ids:
            await self._s.send("\r\n  Usage: ED <msg#>\r\n")
            return
        msg = await self._store.get_message(ids[0])
        if msg is None:
            await self._s.send(self._st.get("read.not_found", id=ids[0]))
            return
        if msg.status == STATUS_KILLED:
            await self._s.send(self._st.get("read.killed", id=ids[0]))
            return

        # Show current subject, allow change
        await self._s.send(
            f"\r\n  Editing message {ids[0]}\r\n"
            f"  Current subject: {msg.subject}\r\n"
            f"  New subject (blank to keep): "
        )
        new_subject = (await self._s._readline()).strip()
        if not new_subject:
            new_subject = msg.subject

        # Show current body, then collect new body
        await self._s.send(
            f"\r\n  Current body ({msg.size} bytes) follows.\r\n"
            f"  Enter new body, /EX to finish, /AB to abort:\r\n\r\n"
        )
        await self._s.send(msg.body.replace("\n", "\r\n") + "\r\n")
        await self._s.send(
            "\r\n  --- Enter new body (blank line = keep current) ---\r\n"
        )

        body_lines: list[str] = []
        while True:
            line = await self._s._readline()
            if line.strip() in ("/AB", "/ab"):
                await self._s.send(self._st.get("error.aborted"))
                return
            if line.strip() in ("/EX", "/ex", "\x1a", "***"):
                break
            body_lines.append(line)

        new_body = "\n".join(body_lines).strip() if body_lines else msg.body

        await self._store.update_message(
            ids[0],
            subject=new_subject,
            body=new_body,
            edited_by=self._user.call,
            edited_at=datetime.now(timezone.utc),
        )
        await self._s.send(f"\r\n  Message {ids[0]} updated.\r\n")
        LOG.info("session: sysop %s edited message %d via terminal",
                 self._user.call, ids[0])

    # ------------------------------------------------------------------
    # MOVE n call — sysop: reassign message to different recipient
    # ------------------------------------------------------------------

    async def _cmd_move(self, args: str) -> None:
        """MOVE n call[@bbs] — reassign message n to a different recipient."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        parts = args.split(None, 1)
        if len(parts) < 2:
            await self._s.send("\r\n  Usage: MOVE <msg#> <callsign[@bbs]>\r\n")
            return
        try:
            msg_id = int(parts[0])
        except ValueError:
            await self._s.send("\r\n  Usage: MOVE <msg#> <callsign[@bbs]>\r\n")
            return

        dest = parts[1].strip().upper()
        at_bbs = ""
        if "@" in dest:
            dest, at_bbs = dest.split("@", 1)

        msg = await self._store.get_message(msg_id)
        if msg is None:
            await self._s.send(self._st.get("read.not_found", id=msg_id))
            return

        async with self._store._lock:
            self._store._conn.execute(
                "UPDATE messages SET to_call=?, at_bbs=? WHERE id=?",
                (dest, at_bbs.upper(), msg_id),
            )
            self._store._conn.commit()

        dest_str = f"{dest}@{at_bbs}" if at_bbs else dest
        await self._s.send(f"\r\n  Message {msg_id} moved to {dest_str}.\r\n")
        LOG.info("session: sysop %s moved message %d to %s",
                 self._user.call, msg_id, dest_str)

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
    # RA — read all new personal mail sequentially
    # ------------------------------------------------------------------

    async def _cmd_read_all(self, args: str) -> None:
        """RA — read all new personal mail addressed to me, then kill each."""
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return

        msgs = await self._store.list_messages(
            to_call=self._user.call,
            status=STATUS_NEW,
        )
        if not msgs:
            await self._s.send(self._st.get("list.no_msgs"))
            return

        for msg in msgs:
            await self._read_one(msg.id)

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
            await self._s.send(self._st.get("profile.prompt", field="Name", current=user.display_name or ""))
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

        await self._s.send(self._st.get("profile.set", field="Name", value=name))
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
            await self._s.send(self._st.get("profile.prompt", field="Locator", current=user.locator or ""))
            locator = (await self._s._readline()).strip().upper()[:6]
            if not locator:
                return

        user.locator = locator
        await self._store.upsert_user(user)
        await self._s.send(self._st.get("profile.set", field="Locator", value=locator))
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
            await self._s.send(self._st.get("profile.prompt", field="QTH", current=user.city or ""))
            city = (await self._s._readline()).strip()[:40]
            if not city:
                return
        user.city = city
        await self._store.upsert_user(user)
        await self._s.send(self._st.get("profile.set", field="QTH", value=city))
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
            await self._s.send(self._st.get("profile.prompt", field="ZIP", current=user.zip_code or ""))
            zip_code = (await self._s._readline()).strip()[:10]
            if not zip_code:
                return
        user.zip_code = zip_code
        await self._store.upsert_user(user)
        await self._s.send(self._st.get("profile.set", field="ZIP", value=zip_code))
        LOG.info("session: %s set zip_code=%r", user.call, zip_code)

    # ------------------------------------------------------------------
    # NB [call] — set / show home BBS
    # ------------------------------------------------------------------

    async def _cmd_nb(self, args: str) -> None:
        """NB [call] — set your home BBS callsign."""
        user = self._user
        if args.strip():
            home = args.strip().upper()[:12]
        else:
            await self._s.send(self._st.get("profile.prompt", field="Home BBS", current=user.home_bbs or ""))
            home = (await self._s._readline()).strip().upper()[:12]
            if not home:
                return
        user.home_bbs = home
        await self._store.upsert_user(user)
        # Update White Pages
        from ..store.models import WPEntry
        wp = await self._store.get_wp_entry(user.call) or WPEntry(call=user.call)
        wp.home_bbs   = home
        wp.source_bbs = self._cfg.node.node_call
        await self._store.upsert_wp_entry(wp)
        await self._s.send(self._st.get("profile.set", field="Home BBS", value=home))
        LOG.info("session: %s set home_bbs=%r", user.call, home)

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

    async def _cmd_list_new_login(self, args: str) -> None:
        """LN — list messages new since last login (not since msg_base pointer)."""
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        user = self._user
        since = user.last_login_at
        if since is None:
            msgs = await self._store.list_messages()
        else:
            msgs = await self._store.list_messages(after_date=since)
        await self._send_list(msgs)

    async def _cmd_list_reverse(self, args: str) -> None:
        """LR [n] — list last n messages, newest first (default 20)."""
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        try:
            n = int(args.strip()) if args.strip() else 20
        except ValueError:
            n = 20
        msgs = await self._store.list_messages(limit=n, reverse=True)
        await self._send_list(msgs)

    async def _cmd_list_ww(self, args: str) -> None:
        """LW — list worldwide bulletins (to_call = WW)."""
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        msgs = await self._store.list_messages(msg_type=MSG_BULLETIN, to_call="WW")
        await self._send_list(msgs)

    async def _cmd_list_search(self, args: str) -> None:
        """LS [text] — list messages whose subject/from/to contains text."""
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        term = args.strip()
        if not term:
            await self._s.send("\r\n  Usage: LS <search text>\r\n")
            return
        # Search subject; also check from_call / to_call prefix
        msgs = await self._store.list_messages(search=term)
        # Also include rows where from_call or to_call starts with term
        from_msgs = await self._store.list_messages(from_call=term.upper())
        to_msgs   = await self._store.list_messages(to_call=term.upper())
        seen: set[int] = {m.id for m in msgs}
        for m in (*from_msgs, *to_msgs):
            if m.id not in seen:
                msgs.append(m)
                seen.add(m.id)
        msgs.sort(key=lambda m: m.id)
        await self._send_list(msgs)

    async def _cmd_list_date(self, args: str) -> None:
        """LD [MMDD | YYYYMMDD] — list messages since a date (UTC)."""
        if not self._can(CAP_READ):
            await self._s.send(self._st.get("error.no_permission"))
            return
        term = args.strip()
        if not term:
            await self._s.send("\r\n  Usage: LD <MMDD | YYYYMMDD>\r\n")
            return
        from datetime import timezone as _tz
        now = datetime.now(timezone.utc)
        try:
            if len(term) == 4:   # MMDD
                after = datetime(now.year, int(term[:2]), int(term[2:]),
                                 tzinfo=_tz.utc)
            elif len(term) == 6:  # YYMMDD
                after = datetime(2000 + int(term[:2]), int(term[2:4]),
                                 int(term[4:]), tzinfo=_tz.utc)
            elif len(term) == 8:  # YYYYMMDD
                after = datetime(int(term[:4]), int(term[4:6]),
                                 int(term[6:]), tzinfo=_tz.utc)
            else:
                raise ValueError
        except (ValueError, OverflowError):
            await self._s.send("\r\n  Usage: LD <MMDD | YYYYMMDD>\r\n")
            return
        msgs = await self._store.list_messages(after_date=after)
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
    # RP n — reply to message n
    # ------------------------------------------------------------------

    async def _cmd_reply(self, args: str) -> None:
        """RP n — reply to message n, pre-filling To: and Re: subject."""
        if not self._can(CAP_SEND):
            await self._s.send(self._st.get("error.no_permission"))
            return
        ids = self._parse_id_list(args)
        if not ids:
            await self._s.send("\r\n  Usage: RP <msg#>\r\n")
            return
        msg = await self._store.get_message(ids[0])
        if msg is None:
            await self._s.send(self._st.get("read.not_found", id=ids[0]))
            return
        # Reply goes back to sender; pre-fill subject with Re: prefix
        to_call = msg.from_call
        subject = msg.subject if msg.subject.upper().startswith("RE:") \
                  else f"Re: {msg.subject}"
        await self._compose(to_call, MSG_PRIVATE, subject=subject)

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

    async def _compose(
        self,
        to_arg: str,
        msg_type: str,
        *,
        subject: str = "",
    ) -> None:
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
        if not subject:
            await self._s.send(s.get("send.enter_subject"))
            subject = (await self._s._readline()).strip()
        else:
            await self._s.send(f"\r\n  Subject: {subject}\r\n")
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

    async def _cmd_kill_bulk(self, args: str) -> None:
        """KK / K> / K< / K@ / KF — bulk kill by type or criteria.

        KK          kill all killed (purge — same as sweeping K)
        K> call     kill all messages FROM call
        K< call     kill all messages TO call
        K@ bbs      kill all messages addressed @bbs
        KF          kill all forwarded messages
        KK T        kill all NTS traffic
        KK B        kill all bulletins
        KK P        kill all personal mail (sysop only)
        """
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return

        arg = args.strip().upper()
        msgs: list = []

        if arg.startswith(">"):
            call = arg[1:].strip()
            if not call:
                await self._s.send("\r\n  Usage: K> <callsign>\r\n")
                return
            msgs = await self._store.list_messages(from_call=call)
        elif arg.startswith("<"):
            call = arg[1:].strip()
            if not call:
                await self._s.send("\r\n  Usage: K< <callsign>\r\n")
                return
            msgs = await self._store.list_messages(to_call=call)
        elif arg.startswith("@"):
            bbs = arg[1:].strip()
            if not bbs:
                await self._s.send("\r\n  Usage: K@ <bbs>\r\n")
                return
            all_msgs = await self._store.list_messages()
            msgs = [m for m in all_msgs if m.at_bbs.upper() == bbs]
        elif arg == "F":
            msgs = await self._store.list_messages(status=STATUS_FORWARDED)
        elif arg in ("T", "B", "P"):
            type_map = {"T": MSG_NTS, "B": MSG_BULLETIN, "P": MSG_PRIVATE}
            msgs = await self._store.list_messages(msg_type=type_map[arg])
        elif not arg:
            await self._s.send(
                "\r\n  Usage: KK <T|B|P|F>  or  K> <call>  K< <call>  K@ <bbs>\r\n"
            )
            return
        else:
            await self._s.send(
                "\r\n  Unknown KK option. Use: T B P F or K>/K</K@\r\n"
            )
            return

        if not msgs:
            await self._s.send("\r\n  No messages match.\r\n")
            return

        count = 0
        for m in msgs:
            if m.status != STATUS_KILLED:
                await self._store.kill_message(m.id)
                count += 1
        await self._s.send(f"\r\n  {count} message(s) killed.\r\n")
        LOG.info("session: sysop %s bulk-killed %d messages (arg=%r)",
                 self._user.call, count, arg)

    # ------------------------------------------------------------------
    # $ msg# — show forwarding status / path for a message  [sysop]
    # ------------------------------------------------------------------

    async def _cmd_msg_status(self, args: str) -> None:
        """$ msg# — display forwarding path and status for a message."""
        ids = self._parse_id_list(args)
        if not ids:
            await self._s.send("\r\n  Usage: $ <msg#>\r\n")
            return
        msg = await self._store.get_message(ids[0])
        if msg is None:
            await self._s.send(self._st.get("read.not_found", id=ids[0]))
            return
        date_str = msg.created_at.strftime("%Y-%m-%d %H:%M UTC") if msg.created_at else "?"
        path = msg.forward_path or "(none)"
        at   = f"@{msg.at_bbs}" if msg.at_bbs else ""
        await self._s.send(
            f"\r\n  Msg    : {msg.id}  ({msg.msg_type})  [{msg.status}]\r\n"
            f"  BID    : {msg.bid}\r\n"
            f"  From   : {msg.from_call}  →  {msg.to_call}{at}\r\n"
            f"  Date   : {date_str}\r\n"
            f"  Subject: {msg.subject}\r\n"
            f"  Path   : {path}\r\n"
        )

    # ------------------------------------------------------------------
    # EM msg# — sysop: edit message body only
    # ------------------------------------------------------------------

    async def _cmd_edit_body(self, args: str) -> None:
        """EM msg# — edit only the body of a message."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        ids = self._parse_id_list(args)
        if not ids:
            await self._s.send("\r\n  Usage: EM <msg#>\r\n")
            return
        msg = await self._store.get_message(ids[0])
        if msg is None:
            await self._s.send(self._st.get("read.not_found", id=ids[0]))
            return
        if msg.status == STATUS_KILLED:
            await self._s.send(self._st.get("read.killed", id=ids[0]))
            return

        await self._s.send(
            f"\r\n  Editing body of message {ids[0]}\r\n"
            f"  Current body ({msg.size} bytes) follows:\r\n\r\n"
        )
        await self._s.send(msg.body.replace("\n", "\r\n") + "\r\n")
        await self._s.send(
            "\r\n  --- Enter new body, /EX to finish, /AB to abort ---\r\n"
        )

        body_lines: list[str] = []
        while True:
            line = await self._s._readline()
            if line.strip() in ("/AB", "/ab"):
                await self._s.send(self._st.get("error.aborted"))
                return
            if line.strip() in ("/EX", "/ex", "\x1a", "***"):
                break
            body_lines.append(line)

        new_body = "\n".join(body_lines).strip() if body_lines else msg.body
        await self._store.update_message(
            ids[0],
            subject=msg.subject,
            body=new_body,
            edited_by=self._user.call,
            edited_at=datetime.now(timezone.utc),
        )
        await self._s.send(f"\r\n  Message {ids[0]} body updated.\r\n")
        LOG.info("session: sysop %s edited body of message %d", self._user.call, ids[0])

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
        elif param in ("LINES", "PAGER") and value.isdigit():
            user.page_length = max(0, int(value))
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
        elif param in ("COLS", "WIDTH") and value.isdigit():
            # Store column width as a user preference
            await self._store.set_user_pref(user.call, "cols", value)
            await self._s.send(f"\r\n  Terminal width set to {value}.\r\n")
            return

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
    # F [neighbor] — trigger forwarding session  [sysop]
    # ------------------------------------------------------------------

    async def _cmd_forward(self, args: str) -> None:
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return

        fwd_cfg = self._cfg.forward
        if not fwd_cfg.enabled or not fwd_cfg.neighbors:
            await self._s.send("\r\n  Forwarding is not configured on this node.\r\n")
            return

        target = args.strip().upper() if args.strip() else None

        neighbors = [
            n for n in fwd_cfg.neighbors
            if n.enabled and (target is None or n.call.upper() == target)
        ]
        if not neighbors:
            if target:
                await self._s.send(f"\r\n  No enabled neighbor: {target}\r\n")
            else:
                await self._s.send("\r\n  No enabled neighbors configured.\r\n")
            return

        from ..forward.session import ForwardSession
        total_sent = total_received = 0
        for neighbor in neighbors:
            await self._s.send(f"\r\n  Forwarding to {neighbor.call} ...\r\n")
            sess = ForwardSession(self._cfg, self._store, neighbor)
            try:
                sent, received = await sess.run_outgoing()
                total_sent     += sent
                total_received += received
                await self._s.send(
                    f"  {neighbor.call}: sent {sent}, received {received}\r\n"
                )
            except Exception as exc:
                await self._s.send(f"  {neighbor.call}: failed — {exc}\r\n")
                LOG.warning("forward: manual F to %s failed: %s", neighbor.call, exc)

        await self._s.send(
            f"\r\n  Forward complete: {total_sent} sent, {total_received} received.\r\n"
        )

    # ------------------------------------------------------------------
    # W — who / connections
    # ------------------------------------------------------------------

    async def _cmd_page_sysop(self, args: str) -> None:
        """T — send a page request to all system operators."""
        user = self._user
        LOG.info("session: %s paged sysops from %s", user.call, self._s.meta.peer)
        # Send a private message to every sysop-privileged user
        try:
            sysops = await self._store.list_users(privilege=PRIV_SYSOP)
            body = (f"{user.call} is requesting System Operator assistance "
                    f"(connected from {self._s.meta.peer}).\r\n")
            for sysop in sysops:
                note = Message(
                    msg_type=MSG_PRIVATE,
                    from_call=user.call,
                    to_call=sysop.call,
                    subject=f"PAGE from {user.call}",
                    body=body,
                )
                await self._store.insert_message(note)
            if not sysops:
                LOG.warning("session: T command — no sysop users found in store")
        except Exception:
            LOG.exception("session: T command failed to store page messages")
        await self._s.send(self._st.get("sysop.paged"))

    # ------------------------------------------------------------------
    # W — who / connections
    # ------------------------------------------------------------------

    async def _cmd_who(self, args: str) -> None:
        user = self._user
        now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await self._s.send(
            f"\r\n  {user.call}  {self._s.meta.peer}"
            f"  [{self._s.meta.channel}]  {now}\r\n"
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
    # FL / FN / FD / FW / FS — forward queue management  [sysop]
    # ------------------------------------------------------------------

    async def _cmd_forward_list(self, args: str) -> None:
        """FL [bbs] — list messages pending forwarding (optionally to a specific BBS)."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        bbs_filter = args.strip().upper() or None
        msgs = await self._store.list_messages(status=STATUS_NEW, msg_type=MSG_BULLETIN)
        # Include private mail addressed to a remote BBS
        priv = await self._store.list_messages(status=STATUS_NEW, msg_type=MSG_PRIVATE)
        node_call = self._cfg.node.node_call.upper().split("-")[0]
        # Private mail is pending forward if at_bbs is set and differs from us
        remote_priv = [m for m in priv if m.at_bbs and m.at_bbs.upper() != node_call]
        pending = msgs + remote_priv
        if bbs_filter:
            pending = [m for m in pending if m.at_bbs.upper() == bbs_filter]
        if not pending:
            bbs_str = f" to {bbs_filter}" if bbs_filter else ""
            await self._s.send(f"\r\n  No messages pending forward{bbs_str}.\r\n")
            return
        lines = [
            "\r\n",
            f"  {'#':>5}  TS  {'To':<12} {'At BBS':<10} {'From':<10}  Subject\r\n",
            "  " + "-" * 65 + "\r\n",
        ]
        for m in pending:
            lines.append(
                f"  {m.id:>5}  {m.msg_type}{m.status}"
                f"  {m.to_call[:12]:<12} {(m.at_bbs or '')[:10]:<10}"
                f" {m.from_call[:10]:<10}  {m.subject[:30]}\r\n"
            )
        lines.append(f"\r\n  {len(pending)} message(s) pending.\r\n")
        await self._s.send_paged("".join(lines))

    async def _cmd_forward_path(self, args: str) -> None:
        """FN msg# — list BBSes a message has been forwarded to."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        ids = self._parse_id_list(args)
        if not ids:
            await self._s.send("\r\n  Usage: FN <msg#>\r\n")
            return
        msg = await self._store.get_message(ids[0])
        if msg is None:
            await self._s.send(self._st.get("read.not_found", id=ids[0]))
            return
        path = msg.forward_path.split() if msg.forward_path else []
        at   = f"@{msg.at_bbs}" if msg.at_bbs else ""
        await self._s.send(
            f"\r\n  Msg {msg.id}: {msg.to_call}{at}  [{msg.status}]\r\n"
            f"  Forward path: {' → '.join(path) if path else '(none)'}\r\n"
        )

    async def _cmd_forward_drop(self, args: str) -> None:
        """FD msg# BBS — remove a message from the forward queue to a specific BBS."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        parts = args.split(None, 1)
        if len(parts) < 2:
            await self._s.send("\r\n  Usage: FD <msg#> <bbs>\r\n")
            return
        try:
            msg_id = int(parts[0])
        except ValueError:
            await self._s.send("\r\n  Usage: FD <msg#> <bbs>\r\n")
            return
        bbs = parts[1].strip().upper()
        msg = await self._store.get_message(msg_id)
        if msg is None:
            await self._s.send(self._st.get("read.not_found", id=msg_id))
            return
        # Append BBS to forward_path so it's treated as already forwarded there
        await self._store.append_forward_path(msg_id, bbs)
        await self._s.send(f"\r\n  Msg {msg_id} marked as already forwarded to {bbs}.\r\n")
        LOG.info("session: sysop %s dropped fwd of msg %d to %s", self._user.call, msg_id, bbs)

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
    # DU / DS / EU — user detail / sysop list / user edit  [sysop]
    # ------------------------------------------------------------------

    async def _cmd_user_detail(self, args: str) -> None:
        """DU <callsign> — display full user record."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        call = args.strip().upper()
        if not call:
            await self._s.send("\r\n  Usage: DU <callsign>\r\n")
            return
        u = await self._store.get_user(call)
        if u is None:
            await self._s.send(f"\r\n  User {call} not found.\r\n")
            return
        last = u.last_login_at.strftime("%Y-%m-%d %H:%M UTC") if u.last_login_at else "never"
        msgs = await self._store.count_messages(to_call=call)
        new  = await self._store.count_messages(to_call=call, status=STATUS_NEW)
        await self._s.send(
            f"\r\n  Callsign  : {u.call}\r\n"
            f"  Name      : {u.display_name or '(not set)'}\r\n"
            f"  Privilege : {u.privilege or 'user'}\r\n"
            f"  Home BBS  : {u.home_bbs or '(not set)'}\r\n"
            f"  Locator   : {u.locator or '(not set)'}\r\n"
            f"  City/QTH  : {u.city or '(not set)'}\r\n"
            f"  ZIP       : {u.zip_code or '(not set)'}\r\n"
            f"  Language  : {u.language or 'en'}\r\n"
            f"  Expert    : {'on' if u.expert_mode else 'off'}\r\n"
            f"  Msg base  : {u.msg_base}\r\n"
            f"  Last login: {last}\r\n"
            f"  Messages  : {msgs} total, {new} new\r\n"
        )

    async def _cmd_sysop_list(self, args: str) -> None:
        """DS — list all sysop-privileged users."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        users = await self._store.list_users(privilege=PRIV_SYSOP)
        if not users:
            await self._s.send("\r\n  No sysop users found.\r\n")
            return
        lines = ["\r\n", f"  {'Call':<12} {'Name':<24}  Last login\r\n",
                 "  " + "-" * 55 + "\r\n"]
        for u in users:
            last = u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "never"
            lines.append(f"  {u.call:<12} {(u.display_name or '')[:24]:<24}  {last}\r\n")
        lines.append(f"\r\n  {len(users)} sysop(s).\r\n")
        await self._s.send("".join(lines))

    async def _cmd_user_edit(self, args: str) -> None:
        """EU <callsign> — interactively edit a user record."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        call = args.strip().upper()
        if not call:
            await self._s.send("\r\n  Usage: EU <callsign>\r\n")
            return
        u = await self._store.get_user(call)
        if u is None:
            await self._s.send(f"\r\n  User {call} not found.\r\n")
            return

        async def _prompt(label: str, current: str) -> str:
            await self._s.send(f"  {label} [{current or '(none)'}]: ")
            val = (await self._s._readline()).strip()
            return val if val else current

        await self._s.send(f"\r\n  Editing user {u.call}  (blank = keep current)\r\n\r\n")
        u.display_name = await _prompt("Name     ", u.display_name or "")
        u.home_bbs     = (await _prompt("Home BBS ", u.home_bbs or "")).upper()
        u.locator      = (await _prompt("Locator  ", u.locator or "")).upper()
        u.city         = await _prompt("City/QTH ", u.city or "")
        u.zip_code     = await _prompt("ZIP      ", u.zip_code or "")
        u.language     = (await _prompt("Language ", u.language or "en")).lower()

        await self._s.send(f"  Privilege [{u.privilege or 'user'}] (user/sysop/blank): ")
        priv_in = (await self._s._readline()).strip().lower()
        if priv_in in ("user", "sysop", ""):
            if priv_in:
                u.privilege = priv_in

        await self._store.upsert_user(u)
        await self._s.send(f"\r\n  User {u.call} updated.\r\n")
        LOG.info("session: sysop %s edited user %s", self._user.call, u.call)

    # ------------------------------------------------------------------
    # IL / IE — WP record view / edit  [sysop]
    # ------------------------------------------------------------------

    async def _cmd_wp_detail(self, args: str) -> None:
        """IL <callsign> — display full White Pages record."""
        call = args.strip().upper()
        if not call:
            await self._s.send("\r\n  Usage: IL <callsign>\r\n")
            return
        entry = await self._store.get_wp_entry(call)
        user  = await self._store.get_user(call)
        if entry is None and user is None:
            await self._s.send(self._st.get("info.wp_not_found", call=call))
            return
        parts = [call]
        name     = (user and user.display_name) or (entry and entry.name) or ""
        home_bbs = (user and user.home_bbs) or (entry and entry.home_bbs) or ""
        locator  = (user and user.locator)  or ""
        city     = (user and user.city)     or ""
        zip_code = (user and user.zip_code) or ""
        if name:     parts.append(name)
        if home_bbs: parts.append(f"@{home_bbs}")
        if locator:  parts.append(locator)
        if city:     parts.append(city)
        if zip_code: parts.append(zip_code)
        source  = (entry and entry.source_bbs) or self._cfg.node.node_call
        updated = entry.updated_at.strftime("%Y-%m-%d %H:%M") if (entry and entry.updated_at) else "?"
        await self._s.send(self._st.get(
            "info.wp_detail",
            details="  ".join(parts),
            source=source,
            updated=updated,
        ))

    async def _cmd_wp_edit(self, args: str) -> None:
        """IE <callsign> — interactively edit a White Pages record."""
        if self._user.privilege != PRIV_SYSOP:
            await self._s.send(self._st.get("error.no_permission"))
            return
        call = args.strip().upper()
        if not call:
            await self._s.send("\r\n  Usage: IE <callsign>\r\n")
            return
        from ..store.models import WPEntry
        entry = await self._store.get_wp_entry(call) or WPEntry(call=call)

        async def _prompt(label: str, current: str) -> str:
            await self._s.send(f"  {label} [{current or '(none)'}]: ")
            val = (await self._s._readline()).strip()
            return val if val else current

        await self._s.send(f"\r\n  Editing WP record for {call}  (blank = keep current)\r\n\r\n")
        entry.name      = await _prompt("Name     ", entry.name or "")
        entry.home_bbs  = (await _prompt("Home BBS ", entry.home_bbs or "")).upper()
        entry.source_bbs = self._cfg.node.node_call

        await self._store.upsert_wp_entry(entry)
        await self._s.send(f"\r\n  WP record for {call} updated.\r\n")
        LOG.info("session: sysop %s edited WP record for %s", self._user.call, call)

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
