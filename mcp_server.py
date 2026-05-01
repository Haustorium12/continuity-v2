"""continuity-v2 MCP server -- search and recall across every Claude Code session.

Exposes three tools backed by the FTS5 index built by index.py:
  - search_sessions: full-text search with snippet output
  - recall_session: full or sliced replay of a session by id (or id prefix)
  - recent_sessions: list recent sessions, optionally filtered by project

Run via stdio (matches Sean's other local MCP servers):
  command: C:\\Python314\\python.exe
  args:    [\"C:\\\\dev\\\\continuity-v2\\\\mcp_server.py\"]
"""

import logging
import os
import sqlite3
import sys
from pathlib import Path

# MCP clients parse stdout as JSON-RPC; keep stderr quiet.
logging.disable(logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

DB_PATH = Path(__file__).parent / "data" / "continuity.db"

mcp = FastMCP("continuity-v2")


def _connect():
    if not DB_PATH.exists():
        raise RuntimeError(f"Index DB not found: {DB_PATH}. Run: python index.py")
    conn = sqlite3.connect(DB_PATH)
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


if __name__ == "__main__":
    mcp.run()
