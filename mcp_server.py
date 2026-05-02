"""continuity-v2 MCP server -- search and recall across every Claude Code session.

Exposes tools backed by the FTS5 index, TEMPORAL edge graph, and semantic embeddings:
  - search_sessions: full-text search with snippet output
  - find_similar: semantic similarity search via sentence embeddings + ANN (vec0)
  - thread_recall: BFS over TEMPORAL edges -- returns narrative thread, not just rows
  - recall_session: full or sliced replay of a session by id (or id prefix)
  - recent_sessions: list recent sessions, optionally filtered by project
  - index_stats: health check
  - fts_integrity_check / fts_rebuild: FTS5 maintenance

Run via stdio (matches Sean's other local MCP servers):
  command: C:\\Python314\\python.exe
  args:    [\"C:\\\\dev\\\\continuity-v2\\\\mcp_server.py\"]
"""

import logging
import os
import sqlite3
import sys
import numpy as np
import sqlite_vec
from pathlib import Path

# MCP clients parse stdout as JSON-RPC; keep stderr quiet.
logging.disable(logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

DB_PATH = Path(__file__).parent / "data" / "continuity.db"

mcp = FastMCP("continuity-v2")


_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _connect():
    if not DB_PATH.exists():
        raise RuntimeError(f"Index DB not found: {DB_PATH}. Run: python index.py")
    conn = sqlite3.connect(DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


@mcp.tool()
def search_sessions(query: str, limit: int = 10, project: str | None = None, source: str | None = None):
    """Full-text search across Claude Code sessions AND claude.ai chat conversations.

    Uses SQLite FTS5. Supports AND/OR/NOT, quoted phrases, prefix* matching.
    Hyphenated or numeric tokens MUST be double-quoted (e.g. '"gold-402"').

    Args:
        query: FTS5 query string.
        limit: Max results (default 10).
        project: Optional substring filter on project name (e.g. "C--dev",
                 "chat.claude.ai").
        source: Optional filter -- "code" for Claude Code sessions only,
                "chat" for claude.ai conversations only. Omit for both.

    Returns:
        Plain-text list of matches: timestamp, role, project, ai_title,
        session id, turn index, and a >>>highlighted<<< snippet.
    """
    conn = _connect()
    sql = """
        SELECT
            t.session_id, t.turn_idx, t.ts, t.role,
            s.project, s.ai_title,
            snippet(turns_fts, 0, '>>>', '<<<', '...', 24) AS snip
        FROM turns_fts
        JOIN turns t ON t.id = turns_fts.rowid
        JOIN sessions s ON s.id = t.session_id
        WHERE turns_fts MATCH ?
    """
    params: list = [query]
    if project:
        sql += " AND s.project LIKE ?"
        params.append(f"%{project}%")
    if source:
        sql += " AND s.source = ?"
        params.append(source)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        rows = list(conn.execute(sql, params))
    except sqlite3.OperationalError as e:
        return (
            f"FTS5 syntax error: {e}\n"
            "Hyphens and numbers need double quotes, e.g. '\"gold-402\" distribution'."
        )

    if not rows:
        return "No matches."

    out = []
    for r in rows:
        ts = (r["ts"] or "")[:19].replace("T", " ")
        title = r["ai_title"] or "(no title)"
        out.append(
            f"[{ts}] {r['role']:9} | {r['project']} | {title}\n"
            f"  session: {r['session_id']}  turn: {r['turn_idx']}\n"
            f"  {r['snip']}"
        )
    out.append(f"\n{len(rows)} match(es).")
    return "\n\n".join(out)


@mcp.tool()
def recall_session(
    session_id: str,
    idx_from: int | None = None,
    idx_to: int | None = None,
):
    """Replay a session's turns. Accepts full id or unique prefix.

    Args:
        session_id: Full session id (UUID) or unique prefix.
        idx_from: Start turn index (inclusive). Omit for start.
        idx_to: End turn index (inclusive). Omit for end.

    Returns:
        Header (title, project, timing, turn count) followed by turns
        formatted as: --- [NNN] timestamp role --- text
    """
    conn = _connect()
    s = conn.execute(
        "SELECT * FROM sessions WHERE id = ? OR id LIKE ?",
        (session_id, f"{session_id}%"),
    ).fetchone()
    if not s:
        return f"No session matching: {session_id}"

    sid = s["id"]
    header = (
        f"=== {s['ai_title'] or '(no title)'} ===\n"
        f"session: {sid}\n"
        f"project: {s['project']}  cwd: {s['cwd']}\n"
        f"started: {s['started_at']}  ended: {s['ended_at']}\n"
        f"turns:   {s['turn_count']}\n"
    )

    sql = "SELECT turn_idx, ts, role, text FROM turns WHERE session_id = ?"
    params: list = [sid]
    if idx_from is not None:
        sql += " AND turn_idx >= ?"
        params.append(idx_from)
    if idx_to is not None:
        sql += " AND turn_idx <= ?"
        params.append(idx_to)
    sql += " ORDER BY turn_idx"

    parts = [header]
    for r in conn.execute(sql, params):
        ts = (r["ts"] or "")[:19].replace("T", " ")
        parts.append(f"--- [{r['turn_idx']:03d}] {ts} {r['role']} ---\n{r['text']}")
    return "\n".join(parts)


@mcp.tool()
def recent_sessions(n: int = 10, project: str | None = None, source: str | None = None):
    """List the N most recent sessions across Claude Code and claude.ai chat.

    Args:
        n: Number of sessions to return (default 10).
        project: Optional substring filter on project name (e.g. "C--dev",
                 "chat.claude.ai").
        source: Optional filter -- "code" for Claude Code only,
                "chat" for claude.ai only. Omit for both.

    Returns:
        Plain-text list: timestamp, turn count, id prefix, source, project, title.
    """
    conn = _connect()
    sql = (
        "SELECT id, ai_title, project, started_at, turn_count, source "
        "FROM sessions"
    )
    params: list = []
    clauses: list = []
    if project:
        clauses.append("project LIKE ?")
        params.append(f"%{project}%")
    if source:
        clauses.append("source = ?")
        params.append(source)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(n)

    rows = list(conn.execute(sql, params))
    if not rows:
        return "No sessions."

    out = []
    for r in rows:
        ts = (r["started_at"] or "")[:19].replace("T", " ")
        title = (r["ai_title"] or "(no title)")[:60]
        src = r["source"] or "code"
        out.append(
            f"{ts}  {r['turn_count']:4d}t  {r['id'][:8]}  [{src}]  [{r['project']}]  {title}"
        )
    return "\n".join(out)


@mcp.tool()
def index_stats():
    """Quick health check of the index. Use this to verify the DB is fresh."""
    conn = _connect()
    s = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    t = conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
    code_s = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE source = 'code' OR source IS NULL"
    ).fetchone()["n"]
    chat_s = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE source = 'chat'"
    ).fetchone()["n"]
    earliest = conn.execute("SELECT MIN(started_at) AS m FROM sessions").fetchone()["m"]
    latest = conn.execute("SELECT MAX(ended_at) AS m FROM sessions").fetchone()["m"]
    size_mb = DB_PATH.stat().st_size / (1024 * 1024)
    return (
        f"DB:            {DB_PATH} ({size_mb:.1f} MB)\n"
        f"Sessions:      {s} (code: {code_s}, chat: {chat_s})\n"
        f"Turns:         {t}\n"
        f"Earliest:      {earliest}\n"
        f"Latest:        {latest}"
    )


def _bfs_expand(conn, seed_ids: list[int], max_hops: int, max_turns: int, edge_types: tuple = ("TEMPORAL",)) -> set[int]:
    """Walk the edge graph outward from seed turn IDs. Returns visited turn ID set."""
    visited = set(seed_ids)
    frontier = set(seed_ids)
    type_ph = ",".join("?" * len(edge_types))

    for _ in range(max_hops):
        if len(visited) >= max_turns or not frontier:
            break
        fron_ph = ",".join("?" * len(frontier))
        fron_list = list(frontier)
        next_frontier: set[int] = set()

        for tid in conn.execute(
            f"SELECT dst_turn_id FROM edges WHERE src_turn_id IN ({fron_ph}) AND edge_type IN ({type_ph})",
            fron_list + list(edge_types),
        ):
            if tid[0] not in visited:
                visited.add(tid[0])
                next_frontier.add(tid[0])

        for tid in conn.execute(
            f"SELECT src_turn_id FROM edges WHERE dst_turn_id IN ({fron_ph}) AND edge_type IN ({type_ph})",
            fron_list + list(edge_types),
        ):
            if tid[0] not in visited:
                visited.add(tid[0])
                next_frontier.add(tid[0])

        frontier = next_frontier

    return visited


@mcp.tool()
def thread_recall(
    query: str,
    max_hops: int = 8,
    max_turns: int = 60,
    seed_limit: int = 3,
    snippet_len: int = 300,
):
    """BFS wave retrieval -- returns a narrative thread, not just matching rows.

    Seeds from FTS5 matches, then walks TEMPORAL edges forward and backward to
    build the surrounding context. Shows what led to the topic and what followed.

    Args:
        query:       FTS5 query string (same syntax as search_sessions).
        max_hops:    BFS depth from each seed (default 8 = ~8 turns each direction).
        max_turns:   Hard cap on total turns returned (default 60).
        seed_limit:  Number of FTS5 seed matches to start from (default 3).
        snippet_len: Max chars per turn body in output (default 300).

    Returns:
        Narrative thread grouped by session, ordered chronologically.
        Seed turns are marked with [MATCH].
    """
    conn = _connect()

    # Find seed turn IDs via FTS5
    seed_sql = """
        SELECT t.id, t.session_id, t.turn_idx
        FROM turns_fts
        JOIN turns t ON t.id = turns_fts.rowid
        WHERE turns_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """
    try:
        seeds = conn.execute(seed_sql, [query, seed_limit]).fetchall()
    except sqlite3.OperationalError as e:
        return f"FTS5 error: {e}"

    if not seeds:
        return "No matches found."

    # Check edges table exists
    has_edges = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edges'"
    ).fetchone()[0]
    if not has_edges:
        return "Edges table not found. Run: python wire_edges.py"

    seed_ids = [r[0] for r in seeds]
    seed_id_set = set(seed_ids)

    # BFS expand
    visited = _bfs_expand(conn, seed_ids, max_hops=max_hops, max_turns=max_turns)

    if not visited:
        return "No thread found."

    # Fetch full turn data for visited set
    ph = ",".join("?" * len(visited))
    rows = conn.execute(
        f"""
        SELECT t.id, t.session_id, t.turn_idx, t.ts, t.role, t.text,
               s.ai_title, s.project, s.started_at
        FROM turns t
        JOIN sessions s ON s.id = t.session_id
        WHERE t.id IN ({ph})
        ORDER BY s.started_at, t.turn_idx
        """,
        list(visited),
    ).fetchall()

    if not rows:
        return "No thread data found."

    # Group by session, render
    out = [f"Thread for: {query!r}  |  {len(rows)} turns from {len(seed_ids)} seed(s)\n"]
    current_sid = None
    for r in rows:
        tid, sid, tidx, ts, role, text, title, project, started = r
        if sid != current_sid:
            ts_fmt = (started or "")[:10]
            out.append(f"\n=== {title or '(no title)'} [{project}] {ts_fmt} ===")
            current_sid = sid

        marker = " [MATCH]" if tid in seed_id_set else ""
        ts_short = (ts or "")[:16].replace("T", " ")
        body = (text or "")[:snippet_len]
        if len(text or "") > snippet_len:
            body += "..."
        out.append(f"  [{tidx:03d}] {ts_short} {role}{marker}\n    {body}")

    return "\n".join(out)


@mcp.tool()
def fts_integrity_check():
    """Run FTS5 integrity-check. Detects index drift between turns and turns_fts.
    Safe to call at any time -- read-only verification."""
    conn = _connect()
    try:
        conn.execute("INSERT INTO turns_fts(turns_fts) VALUES('integrity-check')")
        return "integrity-check PASSED -- index is consistent."
    except sqlite3.OperationalError as e:
        return (
            f"integrity-check FAILED: {e}\n"
            "Run fts_rebuild() to resync the index."
        )


@mcp.tool()
def fts_rebuild():
    """Rebuild the FTS5 index from scratch by re-reading all rows in turns.
    Use after integrity-check failure. Takes a few seconds at 76k turns."""
    conn = _connect()
    conn.execute("INSERT INTO turns_fts(turns_fts) VALUES('rebuild')")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0]
    return f"FTS5 index rebuilt. {count} entries."


@mcp.tool()
def find_similar(query: str, limit: int = 10):
    """Find turns semantically similar to a natural language query.

    Uses sentence embeddings (all-MiniLM-L6-v2) and ANN search over turn_vecs.
    Complements search_sessions (keyword FTS5) -- finds turns that are *about*
    the same topic even when exact words differ.

    Requires embed.py to have been run to build the turn_vecs index.

    Args:
        query: Natural language query string.
        limit: Max results (default 10).

    Returns:
        Plain-text list of semantically similar turns with cosine similarity scores.
    """
    conn = _connect()

    has_vecs = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='turn_vecs'"
    ).fetchone()[0]
    if not has_vecs:
        return "turn_vecs table not found. Run: python embed.py"

    vec_count = conn.execute("SELECT COUNT(*) FROM turn_vecs").fetchone()[0]
    if vec_count == 0:
        return "No embeddings found. Run: python embed.py"

    model = _get_model()
    query_vec = model.encode([query], normalize_embeddings=True)[0]
    query_bytes = query_vec.astype(np.float32).tobytes()

    rows = conn.execute(
        """
        SELECT tv.turn_id, tv.distance,
               t.session_id, t.turn_idx, t.ts, t.role, t.text,
               s.ai_title, s.project
        FROM (
            SELECT turn_id, distance
            FROM turn_vecs
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
        ) tv
        JOIN turns t ON t.id = tv.turn_id
        JOIN sessions s ON s.id = t.session_id
        ORDER BY tv.distance
        """,
        (query_bytes, limit),
    ).fetchall()

    if not rows:
        return "No similar turns found."

    out = [f"Semantic matches for: {query!r}\n"]
    for r in rows:
        cos_sim = 1.0 - (r["distance"] ** 2) / 2.0
        ts = (r["ts"] or "")[:16].replace("T", " ")
        title = (r["ai_title"] or "(no title)")[:50]
        body = (r["text"] or "")[:300]
        if len(r["text"] or "") > 300:
            body += "..."
        out.append(
            f"[{cos_sim:.3f}] {ts} {r['role']} | {r['project']} | {title}\n"
            f"  session: {r['session_id']}  turn: {r['turn_idx']}\n"
            f"  {body}"
        )
    return "\n\n".join(out)


if __name__ == "__main__":
    mcp.run()
