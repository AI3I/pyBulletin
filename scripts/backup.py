"""SQLite database backup using the sqlite3 online backup API.

Creates a timestamped copy of the database in the same directory.
Safe to run while the BBS is live — no locking of the main file.

Usage::

    python scripts/backup.py --config config/pybulletin.toml [--dest /path/to/backups]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pybulletin.config import load_config


def main() -> None:
    ap = argparse.ArgumentParser(description="pyBulletin database backup")
    ap.add_argument("--config", required=True)
    ap.add_argument(
        "--dest",
        default="",
        help="Destination directory (default: same directory as the database)",
    )
    args = ap.parse_args()

    cfg     = load_config(args.config)
    db_path = Path(cfg.store.sqlite_path)

    if not db_path.exists():
        print(f"[backup] database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    dest_dir = Path(args.dest) if args.dest else db_path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    ts      = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = dest_dir / f"{db_path.stem}-{ts}.db"

    # Uses the sqlite3 online backup API — safe to run while BBS is live
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(backup_path))
    with dst:
        src.backup(dst, pages=256)
    dst.close()
    src.close()

    size_kb = backup_path.stat().st_size // 1024
    print(f"[backup] {backup_path} ({size_kb} KB)")


if __name__ == "__main__":
    main()
