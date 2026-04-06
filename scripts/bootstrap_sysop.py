"""Bootstrap the initial SYSOP account.

Called by install.sh and repair.sh after first deploy.
Writes a plaintext credentials note to --output (readable only by root).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pybulletin.auth import generate_sysop_password, hash_password
from pybulletin.config import load_config
from pybulletin.store.models import User, PRIV_SYSOP
from pybulletin.store.store import BBSStore


async def main() -> None:
    ap = argparse.ArgumentParser(description="Bootstrap pyBulletin SYSOP account")
    ap.add_argument("--config", required=True, help="Path to pybulletin.toml")
    ap.add_argument("--output", required=True, help="Path to write credentials note")
    args = ap.parse_args()

    cfg   = load_config(args.config)
    store = BBSStore(cfg.store.sqlite_path)

    existing = await store.get_user("SYSOP")
    if existing and existing.password_hash:
        print("[bootstrap] SYSOP account already exists — skipping password reset")
        _write_note(args.output, "SYSOP", "(already set — check your records)", cfg)
        await store.close()
        return

    password = generate_sysop_password()
    now = datetime.now(timezone.utc)
    sysop = User(
        call="SYSOP",
        display_name="System Operator",
        privilege=PRIV_SYSOP,
        home_bbs=cfg.node.node_call,
        password_hash=hash_password(password),
        last_seen=now,
        created_at=now,
    )
    await store.upsert_user(sysop)
    await store.close()

    _write_note(args.output, "SYSOP", password, cfg)
    print(f"[bootstrap] SYSOP account created — credentials at {args.output}")


def _write_note(path: str, call: str, password: str, cfg) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = (
        f"pyBulletin Initial System Operator Credentials\n"
        f"Generated: {now}\n"
        f"\n"
        f"  Node      : {cfg.node.node_call}\n"
        f"  Callsign  : {call}\n"
        f"  Password  : {password}\n"
        f"\n"
        f"Use these credentials to log into the sysop web console.\n"
        f"Change the password after first login.\n"
        f"\n"
        f"Sysop console: http://127.0.0.1:{cfg.web.port}/\n"
    )
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
