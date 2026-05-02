"""Wire TEMPORAL edges into continuity.db.

TEMPORAL edges: turn[i] -> turn[i+1] within each session, ordered by turn_idx.
Fully derived from the turns table -- safe to re-run (rebuilds TEMPORAL edges).

Usage:
  python wire_edges.py            # wire all sessions
  python wire_edges.py --check    # report edge counts only, no writes
"""

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "continuity.db"

EDGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_turn_id INTEGER NOT NULL,
    dst_turn_id INTEGER NOT NULL,
    edge_type   TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src_turn_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst_turn_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
"""


def connect():
    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}. Run index.py first.")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def apply_schema(conn):
    for stmt in EDGES_SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()


def wire_temporal(conn, verbose=True):
    conn.execute("DELETE FROM edges WHERE edge_type='TEMPORAL'")
    conn.commit()

    sessions = conn.execute(
        "SELECT DISTINCT session_id FROM turns ORDER BY session_id"
    ).fetchall()

    total_edges = 0
    for (sid,) in sessions:
        turns = conn.execute(
            "SELECT id FROM turns WHERE session_id=? ORDER BY turn_idx",
            (sid,),
        ).fetchall()
        if len(turns) < 2:
            continue
        pairs = [
            (turns[i][0], turns[i + 1][0], "TEMPORAL", 1.0)
            for i in range(len(turns) - 1)
        ]
        conn.executemany(
            "INSERT INTO edges (src_turn_id, dst_turn_id, edge_type, weight) VALUES (?,?,?,?)",
            pairs,
        )
        total_edges += len(pairs)

    conn.commit()
    return total_edges


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report counts only")
    args = parser.parse_args()

    conn = connect()
    apply_schema(conn)

    if args.check:
        n = conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type='TEMPORAL'").fetchone()[0]
        t = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        s = conn.execute("SELECT COUNT(DISTINCT session_id) FROM turns").fetchone()[0]
        print(f"Turns:          {t}")
        print(f"Sessions:       {s}")
        print(f"TEMPORAL edges: {n}")
        return

    print("Wiring TEMPORAL edges...")
    n = wire_temporal(conn)

    t = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    s = conn.execute("SELECT COUNT(DISTINCT session_id) FROM turns").fetchone()[0]
    size_mb = DB_PATH.stat().st_size / (1024 * 1024)

    print(f"Done.")
    print(f"Sessions:       {s}")
    print(f"Turns:          {t}")
    print(f"TEMPORAL edges: {n}")
    print(f"DB:             {DB_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
