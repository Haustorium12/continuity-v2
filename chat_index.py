"""Index claude.ai conversations from an Anthropic data export into continuity.db.

Usage:
    python chat_index.py <path/to/conversations.json>

Loads into the same DB as index.py. Chat sessions are tagged source='chat',
project='chat.claude.ai'. Incremental: skips conversations whose updated_at
has not changed since last index run.

Filter to chat sessions in MCP tools using project="chat.claude.ai".
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "continuity.db"


def _ensure_source_column(conn: sqlite3.Connection) -> None:
    cols = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
    if "source" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT DEFAULT 'code'")
        conn.commit()


def _updated_epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _index_conversation(conn: sqlite3.Connection, convo: dict) -> bool:
    uuid = convo["uuid"]
    updated_at = convo.get("updated_at", "")
    updated_epoch = _updated_epoch(updated_at)

    row = conn.execute(
        "SELECT file_mtime FROM sessions WHERE id = ?", (uuid,)
    ).fetchone()
    if row and row[0] == updated_epoch:
        return False

    conn.execute("DELETE FROM turns WHERE session_id = ?", (uuid,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (uuid,))

    messages = convo.get("chat_messages", [])
    if not messages:
        return False

    turn_idx = 0
    for msg in messages:
        sender = msg.get("sender", "human")
        role = "user" if sender == "human" else "assistant"
        text = msg.get("text") or ""

        # Include attachment and file names in indexed text
        for att in msg.get("attachments") or []:
            name = att.get("file_name") or att.get("name") or ""
            if name:
                text += f"\n[attachment: {name}]"
        for f in msg.get("files") or []:
            name = f.get("file_name") or f.get("name") or ""
            if name:
                text += f"\n[file: {name}]"

        if not text.strip():
            continue

        conn.execute(
            "INSERT INTO turns (session_id, turn_idx, ts, role, text) VALUES (?, ?, ?, ?, ?)",
            (uuid, turn_idx, msg.get("created_at", ""), role, text),
        )
        turn_idx += 1

    if turn_idx == 0:
        return False

    # Index summary as a synthetic turn (turn_idx -1, role 'summary')
    summary = convo.get("summary") or ""
    if summary:
        conn.execute(
            "INSERT INTO turns (session_id, turn_idx, ts, role, text) VALUES (?, ?, ?, ?, ?)",
            (uuid, -1, convo.get("created_at", ""), "summary", summary),
        )

    conn.execute(
        """INSERT INTO sessions
               (id, project, ai_title, cwd, started_at, ended_at,
                turn_count, file_path, file_mtime, indexed_at, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid,
            "chat.claude.ai",
            convo.get("name") or "(no name)",
            None,
            convo.get("created_at"),
            updated_at,
            turn_idx,
            None,
            updated_epoch,
            datetime.now(timezone.utc).isoformat(),
            "chat",
        ),
    )
    conn.commit()
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python chat_index.py <path/to/conversations.json>")
        return 1

    export_path = Path(sys.argv[1])
    if not export_path.exists():
        print(f"File not found: {export_path}", file=sys.stderr)
        return 1

    if not DB_PATH.exists():
        print(
            f"DB not found at {DB_PATH}. Run python index.py first to create it.",
            file=sys.stderr,
        )
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    _ensure_source_column(conn)

    data = json.loads(export_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        print("Unexpected format: conversations.json should be a JSON array.", file=sys.stderr)
        return 1

    new_count = 0
    skip_count = 0
    err_count = 0

    for convo in data:
        try:
            if _index_conversation(conn, convo):
                new_count += 1
                print(f"  indexed: {(convo.get('name') or convo['uuid'])[:70]}")
            else:
                skip_count += 1
        except Exception as exc:
            err_count += 1
            print(f"  ERROR {convo.get('uuid', '?')}: {exc}", file=sys.stderr)

    chat_count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE source = 'chat'"
    ).fetchone()[0]
    all_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    all_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    size_mb = DB_PATH.stat().st_size / (1024 * 1024)

    print()
    print(f"New/updated:   {new_count}")
    print(f"Unchanged:     {skip_count}")
    print(f"Errors:        {err_count}")
    print(f"Chat sessions: {chat_count}")
    print(f"All sessions:  {all_sessions}")
    print(f"All turns:     {all_turns}")
    print(f"DB:            {DB_PATH} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
