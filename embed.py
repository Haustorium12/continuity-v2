"""
embed.py

Generates sentence embeddings for all meaningful turns in continuity.db
and stores them in the turn_vecs virtual table (sqlite-vec vec0).

Model: all-MiniLM-L6-v2 (384-dim). Vectors are L2-normalized so that
L2 distance serves as a cosine distance proxy for KNN queries.

Idempotent -- skips turns already embedded. Run after index.py.

Usage:
    python embed.py
"""

import sys
import sqlite3
import numpy as np
import sqlite_vec
from pathlib import Path
from sentence_transformers import SentenceTransformer

DB_PATH = Path(__file__).parent / "data" / "continuity.db"
MODEL_NAME = "all-MiniLM-L6-v2"
BATCH_SIZE = 128
MIN_TEXT_LEN = 30
DIMS = 384

SCHEMA_VECS = """
CREATE VIRTUAL TABLE IF NOT EXISTS turn_vecs USING vec0(
    turn_id INTEGER PRIMARY KEY,
    embedding float[384]
);
"""


def _connect():
    if not DB_PATH.exists():
        raise RuntimeError("DB not found. Run index.py first.")
    conn = sqlite3.connect(DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def is_embeddable(text):
    if not text or len(text) < MIN_TEXT_LEN:
        return False
    t = text.strip()
    if t.startswith("[tool:") or t.startswith("[result]"):
        return False
    return True


def main():
    print("Loading model:", MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)

    conn = _connect()
    conn.execute(SCHEMA_VECS)
    conn.commit()

    already = {r[0] for r in conn.execute("SELECT turn_id FROM turn_vecs")}
    rows = conn.execute(
        "SELECT id, text FROM turns WHERE role IN ('user', 'assistant') ORDER BY id"
    ).fetchall()
    pending = [(r[0], r[1]) for r in rows
               if r[0] not in already and is_embeddable(r[1])]

    print("Turns to embed: {:,}  (already done: {:,})".format(len(pending), len(already)))

    if not pending:
        print("Nothing to do.")
        conn.close()
        return

    total = 0
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        ids = [r[0] for r in batch]
        texts = [r[1][:512] for r in batch]

        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

        conn.executemany(
            "INSERT OR IGNORE INTO turn_vecs(turn_id, embedding) VALUES (?, ?)",
            [(tid, v.astype(np.float32).tobytes()) for tid, v in zip(ids, vecs)]
        )
        conn.commit()

        total += len(batch)
        sys.stdout.write("  {:,}/{:,} ({:.1f}%)\r".format(total, len(pending), total / len(pending) * 100))
        sys.stdout.flush()

    print("\nDone. {:,} turns embedded.".format(total))
    conn.close()


if __name__ == "__main__":
    main()
