# continuity v2

Long-term memory layer for Claude Code, built on the JSONL session record.

## What this is

`continuity` (v1) protected against compaction *within* a single session — PreCompact hook saves a checkpoint, SessionStart hook injects it back, SSE proxy rings bells at 70/85/95% token pressure.

`continuity v2` solves the other half: **across-session recall.**

Every Claude Code session is already written to disk as a JSONL — every turn, every tool call, every response. Plain text. Append-only. Free.

That file is an episodic memory store hiding in plain sight. v2 is the index, search, and retrieval layer on top of it.

## Architecture

```
~/.claude/projects/<project-id>/<session-id>.jsonl   (raw episodic record, already there)
                       ↓
                   v2 indexer
                       ↓
              SQLite + FTS5 + (later) embeddings
                       ↓
                  MCP tool surface
                       ↓
        Claude calls search_sessions(query) mid-conversation
```

## Build stages

### Stage 1 — Index (MVP)

Walk `~/.claude/projects/`, extract every turn into SQLite with FTS5:

```
sessions(id, project, started_at, ended_at, turn_count)
turns(session_id, turn_idx, ts, role, text)
turns_fts (FTS5 mirror of turns.text)
```

Literal full-text search across every conversation ever had with Claude Code. Filterable by project, reverse-chronological.

### Stage 2 — MCP tool

Wire it into the existing `claude-memory` MCP (port 8200) or stand alone.

- `search_sessions(query, limit=10)` — FTS5 search, returns matching turns + session ID + surrounding context
- `recall_session(session_id, range="full")` — return full or sliced session
- `recent_sessions(n=10)` — list recent sessions with first/last user message

### Stage 3 — Semantic recall (later)

Embed each turn, store vectors alongside FTS5. Hybrid search: literal + semantic.

For when the words don't match but the concept does ("when did we talk about the thread-home problem" should match conversations about "Hal", "50 First Dates", "Alice in Wonderland").

### Stage 4 — Auto-tagging (later)

- Project mentions (saranna, dream-module, gold-402, etc.)
- Decision markers (`DECIDED`, `SAVE`, session-close triggers)
- File paths touched

## Why this matters

Compaction stops being context loss and becomes a cache miss. The 3-hour conversation isn't gone — it's a `search_sessions()` call away.

The "I told you this two weeks ago" problem disappears. The episodic record was always there. Nothing was ever lost. Nobody had wired it into recall yet.

## Status

**Stage 1 + Stage 2 complete.** 873 sessions / 72,254 turns indexed against `~/.claude/projects/`. DB: 49.4 MB.

```
python index.py             # build / update DB (incremental -- skips unchanged files)
python stats.py             # health + recent sessions
python search.py "<query>"  # FTS5 search across every session
python recall.py <id>       # full or sliced session by id

# FTS5 quoting note: hyphenated/numeric tokens MUST be double-quoted
#   python search.py '"memory-v4" wave propagation' --project C--dev
#   python search.py '"gold-402" distribution'
```

DB lives at `data/continuity.db` (gitignored). Run `python index.py` periodically or after long sessions to keep it fresh — it only re-indexes files whose mtime changed.

### Stage 2 -- MCP server

`mcp_server.py` exposes the index as a stdio MCP server. Add to `~/.claude/settings.json`:

```json
"continuity-v2": {
  "command": "C:\\Python314\\python.exe",
  "args": ["C:\\dev\\continuity-v2\\mcp_server.py"],
  "env": { "PYTHONIOENCODING": "utf-8" }
}
```

Tools exposed:

- `search_sessions(query, limit=10, project=None)` -- FTS5 search with snippets
- `recall_session(session_id, idx_from=None, idx_to=None)` -- full or sliced replay
- `recent_sessions(n=10, project=None)` -- list recent sessions
- `index_stats()` -- DB health and size

Restart Claude Code to load the server.


## License

MIT — same as continuity v1.
