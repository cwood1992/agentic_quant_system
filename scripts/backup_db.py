"""Backup the SQLite database with a timestamp.

Removes backups older than 7 days.

Usage:
    python scripts/backup_db.py [db_path] [backup_dir]

Defaults:
    db_path:    data/system.db
    backup_dir: data/backups
"""

import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone


def backup(db_path: str = "data/system.db", backup_dir: str = "data/backups") -> None:
    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"system_{timestamp}.db")

    # Use SQLite's backup API for a consistent copy (handles WAL mode)
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup_file)
        src.backup(dst)
        dst.close()
        src.close()
    except Exception:
        # Fallback to file copy
        shutil.copy2(db_path, backup_file)
        wal = db_path + "-wal"
        shm = db_path + "-shm"
        if os.path.isfile(wal):
            shutil.copy2(wal, backup_file + "-wal")
        if os.path.isfile(shm):
            shutil.copy2(shm, backup_file + "-shm")

    print(f"Backup created: {backup_file}")

    # Remove backups older than 7 days
    cutoff = time.time() - 7 * 86400
    remaining = 0
    for name in os.listdir(backup_dir):
        path = os.path.join(backup_dir, name)
        if name.startswith("system_") and name.endswith(".db"):
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                # Also remove associated WAL/SHM
                for suffix in ("-wal", "-shm"):
                    if os.path.isfile(path + suffix):
                        os.remove(path + suffix)
            else:
                remaining += 1

    print(f"Backups retained: {remaining}")


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "data/system.db"
    bdir = sys.argv[2] if len(sys.argv) > 2 else "data/backups"
    backup(db, bdir)
