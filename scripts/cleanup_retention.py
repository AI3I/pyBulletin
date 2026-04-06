"""Message retention cleanup — called daily by the systemd timer."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pybulletin.config import load_config
from pybulletin.store.store import BBSStore


async def main() -> None:
    ap = argparse.ArgumentParser(description="pyBulletin message retention cleanup")
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg   = load_config(args.config)
    store = BBSStore(cfg.store.sqlite_path)
    r     = cfg.retention

    removed = await store.cleanup_expired(
        personal_days=r.personal_mail_days,
        bulletin_days=r.bulletin_days,
        nts_days=r.nts_days,
        killed_days=r.killed_days,
    )
    await store.close()
    print(f"[retention] removed {removed} expired message(s)")


if __name__ == "__main__":
    asyncio.run(main())
