from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    FileEntry, ForwardNeighbor, Message, MfaChallenge, User, UserPref, WPEntry,
    MSG_BULLETIN, MSG_NTS, MSG_PRIVATE,
    PRIV_NONE, PRIV_USER, PRIV_SYSOP,
    STATUS_KILLED, STATUS_NEW, STATUS_READ,
)

LOG = logging.getLogger(__name__)

# Increment this when the schema changes.  The store will run
# _migrate() automatically on open.
_SCHEMA_VERSION = 3

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    bid          TEXT    NOT NULL UNIQUE,
    msg_type     TEXT    NOT NULL DEFAULT 'P',
    status       TEXT    NOT NULL DEFAULT 'N',
    from_call    TEXT    NOT NULL DEFAULT '',
    to_call      TEXT    NOT NULL DEFAULT '',
    at_bbs       TEXT    NOT NULL DEFAULT '',
    subject      TEXT    NOT NULL DEFAULT '',
    body         TEXT    NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    forward_path TEXT    NOT NULL DEFAULT '',
    read_by      TEXT    NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    read_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_to_call   ON messages (to_call);
CREATE INDEX IF NOT EXISTS idx_messages_from_call ON messages (from_call);
CREATE INDEX IF NOT EXISTS idx_messages_status    ON messages (status);
CREATE INDEX IF NOT EXISTS idx_messages_msg_type  ON messages (msg_type);
CREATE INDEX IF NOT EXISTS idx_messages_created   ON messages (created_at);
CREATE INDEX IF NOT EXISTS idx_messages_bid       ON messages (bid);

CREATE TABLE IF NOT EXISTS users (
    call             TEXT    PRIMARY KEY,
    display_name     TEXT    NOT NULL DEFAULT '',
    privilege        TEXT    NOT NULL DEFAULT '',
    email            TEXT    NOT NULL DEFAULT '',
    address          TEXT    NOT NULL DEFAULT '',
    home_bbs         TEXT    NOT NULL DEFAULT '',
    locator          TEXT    NOT NULL DEFAULT '',
    city             TEXT    NOT NULL DEFAULT '',
    zip_code         TEXT    NOT NULL DEFAULT '',
    password_hash    TEXT    NOT NULL DEFAULT '',
    msg_base         INTEGER NOT NULL DEFAULT 0,
    page_length      INTEGER NOT NULL DEFAULT 24,
    expert_mode      INTEGER NOT NULL DEFAULT 0,
    language         TEXT    NOT NULL DEFAULT 'EN',
    last_login_at    INTEGER,
    last_login_peer  TEXT    NOT NULL DEFAULT '',
    last_seen        INTEGER NOT NULL,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_prefs (
    call  TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (call, key)
);

CREATE TABLE IF NOT EXISTS user_startup_commands (
    call     TEXT    NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    command  TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (call, position)
);

CREATE TABLE IF NOT EXISTS mfa_challenges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    call       TEXT    NOT NULL,
    code       TEXT    NOT NULL,
    channel    TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mfa_call ON mfa_challenges (call);

CREATE TABLE IF NOT EXISTS wp_entries (
    call       TEXT PRIMARY KEY,
    home_bbs   TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL,
    source_bbs TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS forward_neighbors (
    call            TEXT PRIMARY KEY,
    last_connect_at INTEGER,
    last_success_at INTEGER,
    msgs_sent       INTEGER NOT NULL DEFAULT 0,
    msgs_received   INTEGER NOT NULL DEFAULT 0,
    session_active  INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS file_entries (
    filename    TEXT NOT NULL,
    area        TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    owner       TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    downloads   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (filename, area)
);
"""

# Migration SQL applied in steps as schema_version increments
_MIGRATIONS: dict[int, list[str]] = {
    3: [
        # Schema v2 → v3: add sysop edit tracking to messages
        "ALTER TABLE messages ADD COLUMN edited_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE messages ADD COLUMN edited_at INTEGER",
    ],
    2: [
        # Schema v1 → v2: expand users table, add user_prefs / mfa tables
        "ALTER TABLE users ADD COLUMN display_name    TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN privilege       TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN email           TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN address         TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN last_login_at   INTEGER",
        "ALTER TABLE users ADD COLUMN last_login_peer TEXT NOT NULL DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS user_prefs (
            call  TEXT NOT NULL,
            key   TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (call, key)
        )""",
        """CREATE TABLE IF NOT EXISTS user_startup_commands (
            call     TEXT    NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            command  TEXT    NOT NULL DEFAULT '',
            PRIMARY KEY (call, position)
        )""",
        """CREATE TABLE IF NOT EXISTS mfa_challenges (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            call       TEXT    NOT NULL,
            code       TEXT    NOT NULL,
            channel    TEXT    NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mfa_call ON mfa_challenges (call)",
    ],
}


def _to_epoch(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.timestamp())


def _from_epoch(ts: int | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _row_to_message(row: sqlite3.Row) -> Message:
    keys = row.keys()
    return Message(
        id=row["id"],
        bid=row["bid"],
        msg_type=row["msg_type"],
        status=row["status"],
        from_call=row["from_call"],
        to_call=row["to_call"],
        at_bbs=row["at_bbs"],
        subject=row["subject"],
        body=row["body"],
        size=row["size"],
        forward_path=row["forward_path"],
        read_by=row["read_by"],
        created_at=_from_epoch(row["created_at"]) or datetime.now(timezone.utc),
        expires_at=_from_epoch(row["expires_at"]),
        read_at=_from_epoch(row["read_at"]),
        edited_by=row["edited_by"] if "edited_by" in keys else "",
        edited_at=_from_epoch(row["edited_at"]) if "edited_at" in keys else None,
    )


def _row_to_user(row: sqlite3.Row) -> User:
    keys = row.keys()
    return User(
        call=row["call"],
        display_name=row["display_name"] if "display_name" in keys else "",
        privilege=row["privilege"] if "privilege" in keys else PRIV_NONE,
        email=row["email"] if "email" in keys else "",
        address=row["address"] if "address" in keys else "",
        home_bbs=row["home_bbs"],
        locator=row["locator"],
        city=row["city"],
        zip_code=row["zip_code"],
        password_hash=row["password_hash"],
        msg_base=row["msg_base"],
        page_length=row["page_length"],
        expert_mode=bool(row["expert_mode"]),
        language=row["language"],
        last_login_at=_from_epoch(row["last_login_at"] if "last_login_at" in keys else None),
        last_login_peer=row["last_login_peer"] if "last_login_peer" in keys else "",
        last_seen=_from_epoch(row["last_seen"]) or datetime.now(timezone.utc),
        created_at=_from_epoch(row["created_at"]) or datetime.now(timezone.utc),
    )


def _row_to_wp(row: sqlite3.Row) -> WPEntry:
    return WPEntry(
        call=row["call"],
        home_bbs=row["home_bbs"],
        name=row["name"],
        updated_at=_from_epoch(row["updated_at"]) or datetime.now(timezone.utc),
        source_bbs=row["source_bbs"],
    )


def _row_to_neighbor(row: sqlite3.Row) -> ForwardNeighbor:
    return ForwardNeighbor(
        call=row["call"],
        last_connect_at=_from_epoch(row["last_connect_at"]),
        last_success_at=_from_epoch(row["last_success_at"]),
        msgs_sent=row["msgs_sent"],
        msgs_received=row["msgs_received"],
        session_active=bool(row["session_active"]),
        enabled=bool(row["enabled"]),
    )


def _row_to_file(row: sqlite3.Row) -> FileEntry:
    return FileEntry(
        filename=row["filename"],
        area=row["area"],
        description=row["description"],
        owner=row["owner"],
        size=row["size"],
        created_at=_from_epoch(row["created_at"]) or datetime.now(timezone.utc),
        downloads=row["downloads"],
    )


class BBSStore:
    """Async-safe SQLite store for pyBulletin.

    All public methods are coroutines and must be awaited inside a running
    event loop.  One-shot scripts should wrap calls with ``asyncio.run()``.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = asyncio.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Schema init is synchronous (called before event loop starts)
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema management (sync — called once at startup before event loop)
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        row = self._conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        current = row["version"] if row else 0
        if current == 0:
            self._conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO sequence (name, value) VALUES ('msg_id', 0)"
            )
            self._conn.commit()
            LOG.info("store: schema created at version %d", _SCHEMA_VERSION)
        elif current < _SCHEMA_VERSION:
            self._migrate(current)
        self._conn.execute("PRAGMA optimize")

    def _migrate(self, from_version: int) -> None:
        """Apply incremental migrations from from_version to _SCHEMA_VERSION."""
        for target in range(from_version + 1, _SCHEMA_VERSION + 1):
            steps = _MIGRATIONS.get(target, [])
            for sql in steps:
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError as exc:
                    # Column/table already exists — safe to ignore
                    if "duplicate column" in str(exc).lower() or "already exists" in str(exc).lower():
                        pass
                    else:
                        raise
        self._conn.execute(
            "UPDATE schema_version SET version = ?", (_SCHEMA_VERSION,)
        )
        self._conn.commit()
        LOG.info("store: migrated schema from %d to %d", from_version, _SCHEMA_VERSION)

    async def close(self) -> None:
        async with self._lock:
            self._conn.execute("PRAGMA optimize")
            self._conn.close()

    def close_sync(self) -> None:
        """Synchronous close for use in scripts/CLI outside an event loop."""
        self._conn.execute("PRAGMA optimize")
        self._conn.close()

    # ------------------------------------------------------------------
    # Message sequence
    # ------------------------------------------------------------------

    async def next_message_id(self) -> int:
        """Atomically increment and return the next sequential message number."""
        async with self._lock:
            self._conn.execute(
                "UPDATE sequence SET value = value + 1 WHERE name = 'msg_id'"
            )
            row = self._conn.execute(
                "SELECT value FROM sequence WHERE name = 'msg_id'"
            ).fetchone()
            self._conn.commit()
            return row["value"]

    async def generate_bid(self, call: str) -> str:
        """Generate a globally-unique BID.  Format: <CALL><yymmddHHMMSS>"""
        ts = time.strftime("%y%m%d%H%M%S", time.gmtime())
        base = f"{call.upper().split('-')[0]}{ts}"
        suffix = 0
        bid = base[:12]
        while await self.has_bid(bid):
            suffix += 1
            bid = f"{base[:10]}{suffix:02d}"[:12]
        return bid

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def has_bid(self, bid: str) -> bool:
        async with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM messages WHERE bid = ?", (bid,)
            ).fetchone()
            return row is not None

    async def insert_message(self, msg: Message) -> int:
        """Insert a message, returning its assigned message number."""
        if not msg.id:
            msg.id = await self.next_message_id()
        if not msg.bid:
            msg.bid = await self.generate_bid(msg.from_call or "BBS")
        msg.size = len(msg.body.encode("utf-8", errors="replace"))
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO messages
                  (id, bid, msg_type, status, from_call, to_call, at_bbs,
                   subject, body, size, forward_path, read_by,
                   created_at, expires_at, read_at)
                VALUES
                  (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id, msg.bid, msg.msg_type, msg.status,
                    msg.from_call.upper(), msg.to_call.upper(), msg.at_bbs.upper(),
                    msg.subject, msg.body, msg.size,
                    msg.forward_path, msg.read_by,
                    _to_epoch(msg.created_at), _to_epoch(msg.expires_at),
                    _to_epoch(msg.read_at),
                ),
            )
            self._conn.commit()
        LOG.debug("store: inserted message %d bid=%s", msg.id, msg.bid)
        return msg.id

    async def get_message(self, msg_id: int) -> Message | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
        return _row_to_message(row) if row else None

    async def list_messages(
        self,
        *,
        to_call: str | None = None,
        from_call: str | None = None,
        msg_type: str | None = None,
        status: str | None = None,
        exclude_killed: bool = True,
        since_id: int = 0,
        before_id: int | None = None,
        after_date: datetime | None = None,
        before_date: datetime | None = None,
        search: str | None = None,
        limit: int | None = None,
        reverse: bool = False,
    ) -> list[Message]:
        parts = ["SELECT * FROM messages WHERE 1=1"]
        params: list[Any] = []

        if to_call is not None:
            parts.append("AND to_call = ?")
            params.append(to_call.upper())
        if from_call is not None:
            parts.append("AND from_call = ?")
            params.append(from_call.upper())
        if msg_type is not None:
            parts.append("AND msg_type = ?")
            params.append(msg_type)
        if status is not None:
            parts.append("AND status = ?")
            params.append(status)
        elif exclude_killed:
            parts.append("AND status != 'K'")
        if since_id:
            parts.append("AND id > ?")
            params.append(since_id)
        if before_id is not None:
            parts.append("AND id < ?")
            params.append(before_id)
        if after_date is not None:
            parts.append("AND created_at > ?")
            params.append(_to_epoch(after_date))
        if before_date is not None:
            parts.append("AND created_at < ?")
            params.append(_to_epoch(before_date))
        if search is not None:
            parts.append("AND subject LIKE ?")
            params.append(f"%{search}%")

        parts.append("ORDER BY id " + ("DESC" if reverse else "ASC"))
        if limit is not None:
            parts.append("LIMIT ?")
            params.append(limit)

        sql = " ".join(parts)
        async with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_message(r) for r in rows]

    async def update_message_status(self, msg_id: int, status: str) -> bool:
        async with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET status = ? WHERE id = ?", (status, msg_id)
            )
            self._conn.commit()
        return cur.rowcount > 0

    async def mark_read(self, msg_id: int, by_call: str) -> bool:
        now = _to_epoch(datetime.now(timezone.utc))
        async with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET status = ?, read_by = ?, read_at = ? "
                "WHERE id = ? AND status != ?",
                (STATUS_READ, by_call.upper(), now, msg_id, STATUS_KILLED),
            )
            self._conn.commit()
        return cur.rowcount > 0

    async def update_message(
        self,
        msg_id: int,
        *,
        subject: str,
        body: str,
        edited_by: str,
        edited_at: datetime,
    ) -> bool:
        size = len(body.encode("utf-8", errors="replace"))
        async with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET subject=?, body=?, size=?, edited_by=?, edited_at=? WHERE id=?",
                (subject, body, size, edited_by, _to_epoch(edited_at), msg_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    async def kill_message(self, msg_id: int) -> bool:
        return await self.update_message_status(msg_id, STATUS_KILLED)

    async def hold_message(self, msg_id: int) -> bool:
        from .models import STATUS_HELD
        return await self.update_message_status(msg_id, STATUS_HELD)

    async def release_message(self, msg_id: int) -> bool:
        return await self.update_message_status(msg_id, STATUS_NEW)

    async def mark_forwarded(self, msg_id: int) -> bool:
        from .models import STATUS_FORWARDED
        return await self.update_message_status(msg_id, STATUS_FORWARDED)

    async def append_forward_path(self, msg_id: int, bbs_call: str) -> None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT forward_path FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
            if row is None:
                return
            path = row["forward_path"]
            new_path = f"{path} {bbs_call}".strip() if path else bbs_call
            self._conn.execute(
                "UPDATE messages SET forward_path = ? WHERE id = ?",
                (new_path, msg_id),
            )
            self._conn.commit()

    async def count_messages(
        self,
        *,
        to_call: str | None = None,
        msg_type: str | None = None,
        status: str | None = None,
    ) -> int:
        parts = ["SELECT COUNT(*) FROM messages WHERE 1=1"]
        params: list[Any] = []
        if to_call:
            parts.append("AND to_call = ?")
            params.append(to_call.upper())
        if msg_type:
            parts.append("AND msg_type = ?")
            params.append(msg_type)
        if status:
            parts.append("AND status = ?")
            params.append(status)
        async with self._lock:
            row = self._conn.execute(" ".join(parts), params).fetchone()
        return row[0] if row else 0

    async def highest_message_id(self) -> int:
        async with self._lock:
            row = self._conn.execute("SELECT MAX(id) FROM messages").fetchone()
        return row[0] or 0

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def get_user(self, call: str) -> User | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE call = ?", (call.upper(),)
            ).fetchone()
        return _row_to_user(row) if row else None

    async def upsert_user(self, user: User) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO users
                  (call, display_name, privilege, email, address,
                   home_bbs, locator, city, zip_code, password_hash,
                   msg_base, page_length, expert_mode, language,
                   last_login_at, last_login_peer, last_seen, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call) DO UPDATE SET
                  display_name     = excluded.display_name,
                  privilege        = excluded.privilege,
                  email            = excluded.email,
                  address          = excluded.address,
                  home_bbs         = excluded.home_bbs,
                  locator          = excluded.locator,
                  city             = excluded.city,
                  zip_code         = excluded.zip_code,
                  password_hash    = excluded.password_hash,
                  msg_base         = excluded.msg_base,
                  page_length      = excluded.page_length,
                  expert_mode      = excluded.expert_mode,
                  language         = excluded.language,
                  last_login_at    = excluded.last_login_at,
                  last_login_peer  = excluded.last_login_peer,
                  last_seen        = excluded.last_seen
                """,
                (
                    user.call.upper(), user.display_name, user.privilege,
                    user.email, user.address,
                    user.home_bbs, user.locator, user.city, user.zip_code,
                    user.password_hash, user.msg_base, user.page_length,
                    int(user.expert_mode), user.language,
                    _to_epoch(user.last_login_at), user.last_login_peer,
                    _to_epoch(user.last_seen), _to_epoch(user.created_at),
                ),
            )
            self._conn.commit()

    async def record_login(self, call: str, peer: str) -> User:
        """Record a successful login; auto-creates a registry entry if needed.

        Mirrors pyCluster's record_login() — first connect bootstraps the user.
        Returns the (possibly new) User record.
        """
        call = call.upper()
        now = datetime.now(timezone.utc)
        existing = await self.get_user(call)
        if existing is None:
            user = User(
                call=call,
                privilege=PRIV_USER,
                last_login_at=now,
                last_login_peer=peer,
                last_seen=now,
                created_at=now,
            )
            await self.upsert_user(user)
            LOG.info("store: auto-created user %s on first login from %s", call, peer)
            return user

        existing.last_login_at = now
        existing.last_login_peer = peer
        existing.last_seen = now
        await self.upsert_user(existing)
        return existing

    async def list_users(
        self,
        *,
        search: str | None = None,
        privilege: str | None = None,
        limit: int | None = None,
    ) -> list[User]:
        parts = ["SELECT * FROM users WHERE 1=1"]
        params: list[Any] = []
        if search:
            parts.append("AND (call LIKE ? OR display_name LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like])
        if privilege is not None:
            parts.append("AND privilege = ?")
            params.append(privilege)
        parts.append("ORDER BY call")
        if limit is not None:
            parts.append("LIMIT ?")
            params.append(limit)
        async with self._lock:
            rows = self._conn.execute(" ".join(parts), params).fetchall()
        return [_row_to_user(r) for r in rows]

    async def delete_user(self, call: str) -> bool:
        async with self._lock:
            cur = self._conn.execute(
                "DELETE FROM users WHERE call = ?", (call.upper(),)
            )
            self._conn.commit()
        return cur.rowcount > 0

    async def set_privilege(self, call: str, privilege: str) -> bool:
        async with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET privilege = ? WHERE call = ?",
                (privilege, call.upper()),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # User preferences
    # ------------------------------------------------------------------

    async def get_user_pref(self, call: str, key: str) -> str | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT value FROM user_prefs WHERE call = ? AND key = ?",
                (call.upper(), key),
            ).fetchone()
        return row["value"] if row else None

    async def set_user_pref(self, call: str, key: str, value: str) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO user_prefs (call, key, value) VALUES (?, ?, ?)
                ON CONFLICT(call, key) DO UPDATE SET value = excluded.value
                """,
                (call.upper(), key, value),
            )
            self._conn.commit()

    async def list_user_prefs(self, call: str) -> dict[str, str]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM user_prefs WHERE call = ? ORDER BY key",
                (call.upper(),),
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    async def delete_user_pref(self, call: str, key: str) -> bool:
        async with self._lock:
            cur = self._conn.execute(
                "DELETE FROM user_prefs WHERE call = ? AND key = ?",
                (call.upper(), key),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # User startup commands
    # ------------------------------------------------------------------

    async def get_startup_commands(self, call: str) -> list[str]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT command FROM user_startup_commands "
                "WHERE call = ? ORDER BY position",
                (call.upper(),),
            ).fetchall()
        return [r["command"] for r in rows]

    async def set_startup_commands(self, call: str, commands: list[str]) -> None:
        call = call.upper()
        async with self._lock:
            self._conn.execute(
                "DELETE FROM user_startup_commands WHERE call = ?", (call,)
            )
            for i, cmd in enumerate(commands):
                self._conn.execute(
                    "INSERT INTO user_startup_commands (call, position, command) "
                    "VALUES (?, ?, ?)",
                    (call, i, cmd),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # MFA challenges
    # ------------------------------------------------------------------

    async def create_mfa_challenge(
        self, call: str, channel: str, ttl_seconds: int = 300
    ) -> str:
        """Create a 6-digit MFA code, store it, and return the code."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = int(time.time())
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO mfa_challenges (call, code, channel, created_at, expires_at, used)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (call.upper(), code, channel, now, now + ttl_seconds),
            )
            self._conn.commit()
        return code

    async def verify_mfa_challenge(self, call: str, code: str) -> bool:
        """Return True and mark used if code is valid and unexpired."""
        now = int(time.time())
        async with self._lock:
            row = self._conn.execute(
                """
                SELECT id FROM mfa_challenges
                WHERE call = ? AND code = ? AND used = 0 AND expires_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (call.upper(), code, now),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE mfa_challenges SET used = 1 WHERE id = ?", (row["id"],)
            )
            self._conn.commit()
        return True

    async def purge_expired_mfa(self) -> int:
        now = int(time.time())
        async with self._lock:
            cur = self._conn.execute(
                "DELETE FROM mfa_challenges WHERE expires_at < ?", (now,)
            )
            self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # White Pages
    # ------------------------------------------------------------------

    async def get_wp_entry(self, call: str) -> WPEntry | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wp_entries WHERE call = ?", (call.upper(),)
            ).fetchone()
        return _row_to_wp(row) if row else None

    async def upsert_wp_entry(self, entry: WPEntry) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO wp_entries (call, home_bbs, name, updated_at, source_bbs)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(call) DO UPDATE SET
                  home_bbs   = excluded.home_bbs,
                  name       = excluded.name,
                  updated_at = excluded.updated_at,
                  source_bbs = excluded.source_bbs
                """,
                (
                    entry.call.upper(), entry.home_bbs, entry.name,
                    _to_epoch(entry.updated_at), entry.source_bbs,
                ),
            )
            self._conn.commit()

    async def list_wp_entries(self, limit: int | None = None) -> list[WPEntry]:
        sql = "SELECT * FROM wp_entries ORDER BY call"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        async with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_wp(r) for r in rows]

    async def count_wp_entries(self) -> int:
        async with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM wp_entries").fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Forward neighbors
    # ------------------------------------------------------------------

    async def get_neighbor(self, call: str) -> ForwardNeighbor | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM forward_neighbors WHERE call = ?", (call.upper(),)
            ).fetchone()
        return _row_to_neighbor(row) if row else None

    async def upsert_neighbor(self, neighbor: ForwardNeighbor) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO forward_neighbors
                  (call, last_connect_at, last_success_at, msgs_sent,
                   msgs_received, session_active, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call) DO UPDATE SET
                  last_connect_at = excluded.last_connect_at,
                  last_success_at = excluded.last_success_at,
                  msgs_sent       = excluded.msgs_sent,
                  msgs_received   = excluded.msgs_received,
                  session_active  = excluded.session_active,
                  enabled         = excluded.enabled
                """,
                (
                    neighbor.call.upper(),
                    _to_epoch(neighbor.last_connect_at),
                    _to_epoch(neighbor.last_success_at),
                    neighbor.msgs_sent, neighbor.msgs_received,
                    int(neighbor.session_active), int(neighbor.enabled),
                ),
            )
            self._conn.commit()

    async def list_neighbors(self) -> list[ForwardNeighbor]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM forward_neighbors ORDER BY call"
            ).fetchall()
        return [_row_to_neighbor(r) for r in rows]

    async def update_neighbor_stats(
        self,
        call: str,
        *,
        sent: int = 0,
        received: int = 0,
        success: bool = False,
    ) -> None:
        now = _to_epoch(datetime.now(timezone.utc))
        async with self._lock:
            if success:
                self._conn.execute(
                    """
                    UPDATE forward_neighbors
                    SET msgs_sent = msgs_sent + ?,
                        msgs_received = msgs_received + ?,
                        last_connect_at = ?,
                        last_success_at = ?
                    WHERE call = ?
                    """,
                    (sent, received, now, now, call.upper()),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE forward_neighbors
                    SET msgs_sent = msgs_sent + ?,
                        msgs_received = msgs_received + ?,
                        last_connect_at = ?
                    WHERE call = ?
                    """,
                    (sent, received, now, call.upper()),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    async def list_files(self, area: str | None = None) -> list[FileEntry]:
        if area is not None:
            async with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM file_entries WHERE area = ? ORDER BY filename",
                    (area,),
                ).fetchall()
        else:
            async with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM file_entries ORDER BY area, filename"
                ).fetchall()
        return [_row_to_file(r) for r in rows]

    async def get_file_entry(self, filename: str, area: str = "") -> FileEntry | None:
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM file_entries WHERE filename = ? AND area = ?",
                (filename, area),
            ).fetchone()
        return _row_to_file(row) if row else None

    async def upsert_file_entry(self, entry: FileEntry) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO file_entries
                  (filename, area, description, owner, size, created_at, downloads)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filename, area) DO UPDATE SET
                  description = excluded.description,
                  owner       = excluded.owner,
                  size        = excluded.size,
                  downloads   = excluded.downloads
                """,
                (
                    entry.filename, entry.area, entry.description,
                    entry.owner, entry.size,
                    _to_epoch(entry.created_at), entry.downloads,
                ),
            )
            self._conn.commit()

    async def delete_file_entry(self, filename: str, area: str = "") -> bool:
        async with self._lock:
            cur = self._conn.execute(
                "DELETE FROM file_entries WHERE filename = ? AND area = ?",
                (filename, area),
            )
            self._conn.commit()
        return cur.rowcount > 0

    async def increment_downloads(self, filename: str, area: str = "") -> None:
        async with self._lock:
            self._conn.execute(
                "UPDATE file_entries SET downloads = downloads + 1 "
                "WHERE filename = ? AND area = ?",
                (filename, area),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Retention cleanup
    # ------------------------------------------------------------------

    async def cleanup_expired(
        self,
        *,
        personal_days: int = 30,
        bulletin_days: int = 14,
        nts_days: int = 7,
        killed_days: int = 1,
    ) -> int:
        """Delete messages older than their retention window. Returns count deleted."""
        now = int(time.time())

        cutoffs = [
            (MSG_PRIVATE,  now - personal_days * 86400),
            (MSG_BULLETIN, now - bulletin_days * 86400),
            (MSG_NTS,      now - nts_days * 86400),
        ]
        killed_cutoff = now - killed_days * 86400

        total = 0
        async with self._lock:
            for msg_type, cutoff in cutoffs:
                cur = self._conn.execute(
                    "DELETE FROM messages WHERE msg_type = ? AND created_at < ? "
                    "AND status != 'H'",
                    (msg_type, cutoff),
                )
                total += cur.rowcount

            cur = self._conn.execute(
                "DELETE FROM messages WHERE status = 'K' AND created_at < ?",
                (killed_cutoff,),
            )
            total += cur.rowcount
            self._conn.commit()

        if total:
            LOG.info("store: retention cleanup removed %d messages", total)
        return total
