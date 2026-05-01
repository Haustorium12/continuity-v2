"""CLI search over indexed sessions. Usage: python search.py <query> [--limit N] [--project NAME]"""

import argparse
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = Path(__file__).parent / "data" / "continuity.db"


def search(query, limit=10, project=None):
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}\nRun: python index.py", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT
            t.id, t.session_id, t.turn_idx, t.ts, t.role, t.text,
            s.project, s.ai_title,
            snippet(turns_fts, 0, '>>>', '<<<', '...', 24) AS snip
        FROM turns_fts
        JOIN turns t ON t.id = turns_fts.rowid
        JOIN sessions s ON s.id = t.session_id
        WHERE turns_fts MATCH ?
    """
    params = [query]
    if project:
        sql += " AND s.project LIKE ?"
        params.append(f"%{project}%")
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    rows = list(conn.execute(sql, params))

    if not rows:
        print("No matches.")
        return 0

    for r in rows:
        title = r["ai_title"] or "(no title)"
        ts = (r["ts"] or "")[:19].replace("T", " ")
        print(f"\n[{ts}] {r['role']:9} | {r['project']} | {title}")
        print(f"  session: {r['session_id']}  turn: {r['turn_idx']}")
        print(f"  {r['snip']}")

    print(f"\n{len(rows)} match(es).")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="FTS5 query (supports AND/OR/NOT, quotes, prefix*)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--project", help="filter by project name substring")
    args = ap.parse_args()
    return search(args.query, args.limit, args.project)


if __name__ == "__main__":
    sys.exit(main())
