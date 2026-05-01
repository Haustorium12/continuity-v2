"""Quick health summary of the index."""

import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = Path(__file__).parent / "data" / "continuity.db"


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}\nRun: python index.py", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    s = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    t = conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
    earliest = conn.execute("SELECT MIN(started_at) AS m FROM sessions").fetchone()["m"]
    latest = conn.execute("SELECT MAX(ended_at) AS m FROM sessions").fetchone()["m"]
    size_mb = DB_PATH.stat().st_size / (1024 * 1024)

    print(f"DB:        {DB_PATH} ({size_mb:.1f} MB)")
    print(f"Sessions:  {s}")
    print(f"Turns:     {t}")
    print(f"Earliest:  {earliest}")
    print(f"Latest:    {latest}")
    print()
    print("Top projects:")
    for row in conn.execute(
        "SELECT project, COUNT(*) AS n, SUM(turn_count) AS turns "
        "FROM sessions GROUP BY project ORDER BY n DESC LIMIT 10"
    ):
        print(f"  {row['n']:4d} sessions, {row['turns'] or 0:6d} turns  {row['project']}")

    print()
    print("Recent sessions:")
    for row in conn.execute(
        "SELECT id, ai_title, project, started_at, turn_count "
        "FROM sessions ORDER BY started_at DESC LIMIT 10"
    ):
        title = (row["ai_title"] or "(no title)")[:60]
        ts = (row["started_at"] or "")[:19].replace("T", " ")
        print(f"  {ts}  {row['turn_count']:3d}t  {row['id'][:8]}  [{row['project']}]  {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
