"""Walk ~/.claude/projects/ and index every JSONL session into SQLite + FTS5."""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
DB_PATH = Path(__file__).parent / "data" / "continuity.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project TEXT,
    ai_title TEXT,
    cwd TEXT,
    started_at TEXT,
    ended_at TEXT,
    turn_count INTEGER,
    file_path TEXT,
    file_mtime REAL,
    indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_idx INTEGER NOT NULL,
    ts TEXT,
    role TEXT,
    text TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    text,
    role UNINDEXED,
    session_id UNINDEXED,
    turn_idx UNINDEXED,
    ts UNINDEXED,
    content='turns',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, text, role, session_id, turn_idx, ts)
    VALUES (new.id, new.text, new.role, new.session_id, new.turn_idx, new.ts);
END;

CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, text, role, session_id, turn_idx, ts)
    VALUES ('delete', old.id, old.text, old.role, old.session_id, old.turn_idx, old.ts);
END;
"""


def extract_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            name = block.get("name", "")
            inp = block.get("input") or {}
            desc = inp.get("description") if isinstance(inp, dict) else ""
            parts.append(f"[tool:{name}] {desc or ''}".strip())
        elif kind == "tool_result":
            c = block.get("content", "")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            if not isinstance(c, str):
                c = str(c)
            parts.append(f"[result] {c[:500]}")
    return "\n".join(p for p in parts if p)


def index_file(conn, path):
    sid = path.stem
    project = path.parent.name
    mtime = path.stat().st_mtime

    row = conn.execute("SELECT file_mtime FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row and row[0] == mtime:
        return False

    conn.execute("DELETE FROM turns WHERE session_id = ?", (sid,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    ai_title = None
    cwd = None
    timestamps = []
    turn_idx = 0

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            kind = obj.get("type")
            if kind == "ai-title":
                ai_title = obj.get("aiTitle")
                continue
            if kind not in ("user", "assistant"):
                continue

            ts = obj.get("timestamp")
            if ts:
                timestamps.append(ts)
            if not cwd:
                cwd = obj.get("cwd")

            msg = obj.get("message") or {}
            text = extract_text(msg.get("content", ""))
            if not text:
                continue

            conn.execute(
                "INSERT INTO turns (session_id, turn_idx, ts, role, text) VALUES (?, ?, ?, ?, ?)",
                (sid, turn_idx, ts, kind, text),
            )
            turn_idx += 1

    if turn_idx == 0:
        return False

    started = min(timestamps) if timestamps else None
    ended = max(timestamps) if timestamps else None

    conn.execute(
        """
        INSERT INTO sessions
            (id, project, ai_title, cwd, started_at, ended_at,
             turn_count, file_path, file_mtime, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sid, project, ai_title, cwd, started, ended, turn_idx,
         str(path), mtime, datetime.now().isoformat()),
    )
    conn.commit()
    return True


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    if not PROJECTS_DIR.exists():
        print(f"Projects dir not found: {PROJECTS_DIR}", file=sys.stderr)
        return 1

    new_count = 0
    skip_count = 0
    err_count = 0

    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            if index_file(conn, jsonl):
                new_count += 1
                print(f"  indexed: {jsonl.parent.name}/{jsonl.name}")
            else:
                skip_count += 1
        except Exception as exc:
            err_count += 1
            print(f"  ERROR {jsonl}: {exc}", file=sys.stderr)

    sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    size_mb = DB_PATH.stat().st_size / (1024 * 1024) if DB_PATH.exists() else 0

    print()
    print(f"New/updated: {new_count}")
    print(f"Unchanged:   {skip_count}")
    print(f"Errors:      {err_count}")
    print(f"Sessions:    {sessions}")
    print(f"Turns:       {turns}")
    print(f"DB:          {DB_PATH} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
