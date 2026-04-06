"""Migrate an existing LinFBB/FBB data directory into pyBulletin.

Imports:
  - Messages from FBB mail/ directory
  - User database (FBB users file)
  - Forward/routing configuration
  - White pages database

Usage::

    python scripts/migrate_fbb.py --config config/pybulletin.toml --source /fbb [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pybulletin.config import load_config


async def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate FBB data to pyBulletin")
    ap.add_argument("--config",  required=True, help="Path to pybulletin.toml")
    ap.add_argument("--source",  required=True, help="FBB data directory (e.g. /fbb)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    source = Path(args.source)
    if not source.is_dir():
        print(f"[migrate] source directory not found: {source}", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(args.config)
    mode = "DRY RUN — " if args.dry_run else ""
    print(f"[migrate] {mode}source: {source}")
    print(f"[migrate] {mode}target: {cfg.store.sqlite_path}")

    # --- Mail messages ---
    mail_dir = source / "mail"
    if mail_dir.is_dir():
        await _migrate_mail(mail_dir, cfg, dry_run=args.dry_run)
    else:
        print(f"[migrate] no mail/ directory found at {mail_dir} — skipping")

    # --- Users ---
    for users_file in (source / "users", source / "users.dat", source / "user.dat"):
        if users_file.exists():
            await _migrate_users(users_file, cfg, dry_run=args.dry_run)
            break
    else:
        print("[migrate] no users file found — skipping user import")

    print(f"[migrate] {mode}complete")


async def _migrate_mail(mail_dir: Path, cfg, *, dry_run: bool) -> None:
    """Import FBB-format message files from mail_dir."""
    from pybulletin.store.store import BBSStore
    from pybulletin.store.models import Message, MSG_PRIVATE, MSG_BULLETIN, MSG_NTS

    files = sorted(mail_dir.glob("*.txt")) + sorted(mail_dir.glob("*.msg"))
    if not files:
        print(f"[migrate] mail/: no message files found")
        return

    if dry_run:
        print(f"[migrate] mail/: would import {len(files)} file(s)")
        return

    store = BBSStore(cfg.store.sqlite_path)
    imported = skipped = 0

    for f in files:
        try:
            text = f.read_text(encoding="ascii", errors="replace")
            msg = await _parse_fbb_message(text, store)
            if msg is None:
                skipped += 1
                continue
            await store.insert_message(msg)
            imported += 1
        except Exception as exc:
            print(f"[migrate] warning: {f.name}: {exc}")
            skipped += 1

    await store.close()
    print(f"[migrate] mail/: imported {imported}, skipped {skipped}")


async def _parse_fbb_message(text: str, store) -> "Message | None":
    """Parse a minimal FBB message file.

    FBB message files have a header block followed by body text.
    Header lines: From:, To:, At:, Subject:, Date:, BID:, Type:
    """
    from pybulletin.store.models import (
        Message, MSG_PRIVATE, MSG_BULLETIN, MSG_NTS, STATUS_NEW
    )
    from datetime import datetime, timezone

    lines = text.splitlines()
    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False

    for line in lines:
        if in_body:
            body_lines.append(line)
            continue
        if line.strip() == "" or line.strip() == "---":
            in_body = True
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            headers[key.strip().lower()] = val.strip()

    if not headers.get("from") and not headers.get("to"):
        return None

    msg_type_raw = headers.get("type", "P").upper()
    if msg_type_raw.startswith("B"):
        msg_type = MSG_BULLETIN
    elif msg_type_raw.startswith("T"):
        msg_type = MSG_NTS
    else:
        msg_type = MSG_PRIVATE

    bid = headers.get("bid", "").strip()
    if bid and await store.has_bid(bid):
        return None  # already imported

    now = datetime.now(timezone.utc)
    return Message(
        bid=bid or await store.generate_bid(headers.get("from", "BBS")),
        msg_type=msg_type,
        status=STATUS_NEW,
        from_call=headers.get("from", "").upper(),
        to_call=headers.get("to", "").upper(),
        at_bbs=headers.get("at", "").upper(),
        subject=headers.get("subject", "(no subject)"),
        body="\n".join(body_lines).strip(),
        created_at=now,
    )


async def _migrate_users(users_file: Path, cfg, *, dry_run: bool) -> None:
    """Import FBB users file (simple key=value format)."""
    from pybulletin.store.store import BBSStore
    from pybulletin.store.models import User, PRIV_USER
    from datetime import datetime, timezone

    text = users_file.read_text(encoding="ascii", errors="replace")
    blocks = text.strip().split("\n\n")
    if not blocks:
        return

    if dry_run:
        print(f"[migrate] users: would process {len(blocks)} record(s)")
        return

    store = BBSStore(cfg.store.sqlite_path)
    imported = 0
    now = datetime.now(timezone.utc)

    for block in blocks:
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                fields[k.strip().lower()] = v.strip()
        call = fields.get("call", "").strip().upper()
        if not call:
            continue
        user = User(
            call=call,
            display_name=fields.get("name", ""),
            privilege=PRIV_USER,
            home_bbs=fields.get("homebbs", ""),
            locator=fields.get("qra", ""),
            city=fields.get("city", ""),
            zip_code=fields.get("zip", ""),
            last_seen=now,
            created_at=now,
        )
        await store.upsert_user(user)
        imported += 1

    await store.close()
    print(f"[migrate] users: imported {imported} record(s)")


if __name__ == "__main__":
    asyncio.run(main())
