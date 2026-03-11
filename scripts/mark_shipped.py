#!/usr/bin/env python3
"""Mark system improvement requests as shipped.

Usage:
    python scripts/mark_shipped.py --requests 1,2,3
    python scripts/mark_shipped.py --requests 5 --note "Deployed in v0.4"
"""

import argparse
import sys
from datetime import datetime, timezone

# Allow running from project root
sys.path.insert(0, ".")

from database.schema import get_db


def mark_shipped(db_path: str, request_ids: list[int], note: str = "") -> None:
    """Mark the given improvement request IDs as shipped.

    Args:
        db_path: Path to the SQLite database.
        request_ids: List of primary key IDs to mark shipped.
        note: Optional status note.
    """
    now = datetime.now(timezone.utc).isoformat()
    status_note = note or "Marked shipped via CLI"

    conn = get_db(db_path)

    for req_id in request_ids:
        row = conn.execute(
            "SELECT id, title, status FROM system_improvement_requests WHERE id = ?",
            (req_id,),
        ).fetchone()

        if row is None:
            print(f"  [SKIP] #{req_id}: not found")
            continue

        if row["status"] != "pending":
            print(f"  [SKIP] #{req_id}: status is '{row['status']}', not 'pending'")
            continue

        conn.execute(
            "UPDATE system_improvement_requests SET status = 'shipped', "
            "shipped_at = ?, status_note = ? WHERE id = ?",
            (now, status_note, req_id),
        )
        print(f"  [OK]   #{req_id}: {row['title']}")

    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Mark system improvement requests as shipped"
    )
    parser.add_argument(
        "--requests",
        required=True,
        help="Comma-separated list of improvement request IDs",
    )
    parser.add_argument(
        "--note",
        default="",
        help="Optional status note (default: 'Marked shipped via CLI')",
    )
    parser.add_argument(
        "--db",
        default="data/system.db",
        help="Path to SQLite database (default: data/system.db)",
    )
    args = parser.parse_args()

    try:
        request_ids = [int(x.strip()) for x in args.requests.split(",")]
    except ValueError:
        print("Error: --requests must be comma-separated integers")
        sys.exit(1)

    print(f"Marking {len(request_ids)} request(s) as shipped...")
    mark_shipped(args.db, request_ids, args.note)
    print("Done.")


if __name__ == "__main__":
    main()
