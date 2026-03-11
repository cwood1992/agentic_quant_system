#!/usr/bin/env python3
"""Generate a formatted report of pending system improvement requests.

Groups by priority (critical > high > normal > low) then by category.
Prints to stdout for piping or review.

Usage:
    python scripts/generate_review_report.py [--db data/system.db]
"""

import argparse
import json
import sys

# Allow running from project root
sys.path.insert(0, ".")

from database.schema import get_db


PRIORITY_ORDER = ["critical", "high", "normal", "low"]


def generate_report(db_path: str) -> str:
    """Query pending improvements and format a review report.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Formatted report string.
    """
    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT id, request_id, agent_id, cycle, title, problem, impact, "
        "category, priority, examples, created_at "
        "FROM system_improvement_requests WHERE status = 'pending' "
        "ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        return "No pending improvement requests.\n"

    # Group by priority then category
    grouped: dict[str, dict[str, list]] = {}
    for r in rows:
        pri = r["priority"] or "normal"
        cat = r["category"] or "uncategorized"
        grouped.setdefault(pri, {}).setdefault(cat, []).append(dict(r))

    lines = [
        "=" * 70,
        "SYSTEM IMPROVEMENT REVIEW REPORT",
        f"Pending requests: {len(rows)}",
        "=" * 70,
        "",
    ]

    for priority in PRIORITY_ORDER:
        if priority not in grouped:
            continue

        lines.append(f"[{priority.upper()}]")
        lines.append("-" * 40)

        for category in sorted(grouped[priority].keys()):
            items = grouped[priority][category]
            lines.append(f"  Category: {category} ({len(items)} item(s))")
            lines.append("")

            for item in items:
                lines.append(f"    #{item['id']} | {item['request_id']}")
                lines.append(f"    Title:   {item['title']}")
                lines.append(f"    Agent:   {item['agent_id']} (cycle {item['cycle']})")
                lines.append(f"    Created: {item['created_at']}")
                lines.append(f"    Problem: {item['problem']}")
                lines.append(f"    Impact:  {item['impact']}")

                if item.get("examples"):
                    try:
                        examples = json.loads(item["examples"])
                        if isinstance(examples, list):
                            lines.append(f"    Examples:")
                            for ex in examples[:3]:
                                lines.append(f"      - {ex}")
                        else:
                            lines.append(f"    Examples: {item['examples']}")
                    except (json.JSONDecodeError, TypeError):
                        lines.append(f"    Examples: {item['examples']}")

                lines.append("")

        lines.append("")

    lines.append("=" * 70)
    lines.append("Actions:")
    lines.append("  Ship:    python scripts/mark_shipped.py --requests <id1,id2,...>")
    lines.append("  Decline: Use /decline <id> <note> via Telegram")
    lines.append("=" * 70)

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Generate system improvement review report"
    )
    parser.add_argument(
        "--db",
        default="data/system.db",
        help="Path to SQLite database (default: data/system.db)",
    )
    args = parser.parse_args()

    report = generate_report(args.db)
    print(report)


if __name__ == "__main__":
    main()
