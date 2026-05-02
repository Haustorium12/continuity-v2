"""
wire_similar.py

Wires SIMILAR_TO edges between semantically similar turns using
embeddings stored in turn_vecs (built by embed.py).

For each embedded turn, queries the K nearest neighbors. Wires a
SIMILAR_TO edge when cosine similarity >= THRESHOLD. Skips self-loops
and pairs already connected by TEMPORAL edges.

Idempotent -- clears all existing SIMILAR_TO edges before re-wiring.

Usage:
    python wire_similar.py
"""

import sqlite3
import sqlite_vec
import numpy as np
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "continuity.db"
K = 5
THRESHOLD = 0.85
COMMIT_EVERY = 500


def _connect():
    if not DB_PATH.exists():
        raise RuntimeError("DB not found. Run index.py first.")
    conn = sqlite3.connect(DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def l2_to_cosine(dist):
    return 1.0 - (dist ** 2) / 2.0


def main():
    conn = _connect()

    # Ensure edges table exists (created by wire_edges.py, but be safe)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edges (
            src_turn_id INTEGER NOT NULL,
            dst_turn_id INTEGER NOT NULL,
            edge_type    TEXT NOT NULL,
            weight       REAL NOT NULL DEFAULT 1.0,
            PRIMARY KEY (src_turn_id, dst_turn_id, edge_type)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_turn_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_turn_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type)")
    conn.commit()

    deleted = conn.execute(
        "DELETE FROM edges WHERE edge_type = 'SIMILAR_TO'"
    ).rowcount
    conn.commit()
    print("Cleared {:,} existing SIMILAR_TO edges.".format(deleted))

    turn_ids = [r[0] for r in conn.execute(
        "SELECT turn_id FROM turn_vecs ORDER BY turn_id"
    )]
    print("Embedded turns: {:,}".format(len(turn_ids)))

    if not turn_ids:
        print("No embeddings found. Run embed.py first.")
        conn.close()
        return

    temporal = set()
    for src, dst in conn.execute(
        "SELECT src_turn_id, dst_turn_id FROM edges WHERE edge_type = 'TEMPORAL'"
    ):
        temporal.add((src, dst))
        temporal.add((dst, src))
    print("TEMPORAL edge pairs loaded: {:,}".format(len(temporal)))

    new_edges = 0
    batch = []

    for i, turn_id in enumerate(turn_ids):
        row = conn.execute(
            "SELECT embedding FROM turn_vecs WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if not row:
            continue

        neighbors = conn.execute(
            "SELECT turn_id, distance FROM turn_vecs "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (row[0], K + 1)
        ).fetchall()

        for nb in neighbors:
            nb_id = nb["turn_id"]
            if nb_id == turn_id:
                continue
            cos_sim = l2_to_cosine(nb["distance"])
            if cos_sim < THRESHOLD:
                continue
            if (turn_id, nb_id) in temporal:
                continue
            batch.append((turn_id, nb_id, "SIMILAR_TO", round(cos_sim, 6)))
            new_edges += 1

        if len(batch) >= COMMIT_EVERY:
            conn.executemany(
                "INSERT OR IGNORE INTO edges(src_turn_id, dst_turn_id, edge_type, weight) "
                "VALUES (?, ?, ?, ?)",
                batch
            )
            conn.commit()
            batch = []

        if (i + 1) % 1000 == 0:
            import sys
            sys.stdout.write("  {:,}/{:,} ({:.1f}%)  edges: {:,}\r".format(
                i + 1, len(turn_ids), (i + 1) / len(turn_ids) * 100, new_edges))
            sys.stdout.flush()

    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO edges(src_turn_id, dst_turn_id, edge_type, weight) "
            "VALUES (?, ?, ?, ?)",
            batch
        )
        conn.commit()

    print("\nDone. {:,} SIMILAR_TO edges wired.".format(new_edges))
    conn.close()


if __name__ == "__main__":
    main()
