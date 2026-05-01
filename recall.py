"""Print full or sliced session by ID. Usage: python recall.py <session_id> [--from N --to M]"""

import argparse
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = Path(__file__).parent / "data" / "continuity.db"


def recall(session_id, idx_from=None, idx_to=None):
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}\nRun: python index.py", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    s = conn.execute(
        "SELECT * FROM sessions WHERE id = ? OR id LIKE ?",
        (session_id, f"{session_id}%"),
    ).fetchone()
    if not s:
        print(f"No session matching: {session_id}", file=sys.stderr)
        return 1

    sid = s["id"]
    print(f"=== {s['ai_title'] or '(no title)'} ===")
    print(f"session: {sid}")
    print(f"project: {s['project']}  cwd: {s['cwd']}")
    print(f"started: {s['started_at']}  ended: {s['ended_at']}")
    print(f"turns:   {s['turn_count']}")
    print()

    sql = "SELECT turn_idx, ts, role, text FROM turns WHERE session_id = ?"
    params = [sid]
    if idx_from is not None:
        sql += " AND turn_idx >= ?"
        params.append(idx_from)
    if idx_to is not None:
        sql += " AND turn_idx <= ?"
        params.append(idx_to)
    sql += " ORDER BY turn_idx"

    for r in conn.execute(sql, params):
        ts = (r["ts"] or "")[:19].replace("T", " ")
        print(f"--- [{r['turn_idx']:03d}] {ts} {r['role']} ---")
        print(r["text"])
        print()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id", help="full or prefix session id")
    ap.add_argument("--from", dest="idx_from", type=int)
    ap.add_argument("--to", dest="idx_to", type=int)
    args = ap.parse_args()
    return recall(args.session_id, args.idx_from, args.idx_to)


if __name__ == "__main__":
    sys.exit(main())
