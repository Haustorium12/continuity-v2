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

**Stage 1 + Stage 2 complete.** 1,026 sessions / 76,010 turns. DB: 57.8 MB.

Two sources in one DB:
- **Claude Code sessions** -- JSONL files under `~/.claude/projects/`
- **claude.ai chat conversations** -- Anthropic data export (`conversations.json`)

```
python index.py                              # index Claude Code sessions (incremental)
python chat_index.py <path/conversations.json>  # index claude.ai chat export (incremental)
python stats.py                              # health + recent sessions
python search.py "<query>"                   # FTS5 search across all sessions
python recall.py <id>                        # full or sliced session by id

# FTS5 quoting note: hyphenated/numeric tokens MUST be double-quoted
#   python search.py '"memory-v4" wave propagation' --project C--dev
#   python search.py '"gold-402" distribution'
```

DB lives at `data/continuity.db` (gitignored).

To get your claude.ai chat export: claude.ai -> Settings -> Export data.

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

- `search_sessions(query, limit=10, project=None, source=None)` -- FTS5 search with snippets
- `recall_session(session_id, idx_from=None, idx_to=None)` -- full or sliced replay
- `recent_sessions(n=10, project=None, source=None)` -- list recent sessions
- `index_stats()` -- DB health broken out by source

The `source` param accepts `"code"` (Claude Code only) or `"chat"` (claude.ai only). Omit for both.

Restart Claude Code to load the server.


## Hooks: source vs. installed

The `hooks/` directory in this repo is the **template**. After installing into `~/.claude/hooks/`, expect to **customize a handful of paths in each script** to match your machine, then leave the installed copies alone.

### What gets customized post-install

| Script | Constant | Source (template) | Installed (typical) |
|---|---|---|---|
| `precompact_save.py` | `LOG` | `~/.claude/hooks/continuity.log` | site-specific log path (e.g. `precompact.log`) |
| `session_start_inject.py` | `PROJECT_STATE` | `~/.claude/memory/project_current_state.md` | wherever your real sticky-note file lives |
| `session_start_inject.py` | `LOG` | `~/.claude/hooks/continuity.log` | site-specific (`session_start.log`) |
| `stop_hook_checkpoint.py` | `LOG` | shared `sse_proxy.log` | usually unchanged |
| `sse_proxy.py` | port, log path | `9099`, shared log | usually unchanged |

### Do not blindly `cp source/* installed/`

This will overwrite your customized `PROJECT_STATE`, `LOG`, and any other site-specific paths, silently breaking session-start injection and log routing. Symptoms include:

- `compaction_checkpoint.md` reads stale data despite hooks firing
- `session_start_inject.py` logs "checkpoint missing" even though one exists
- Logs vanish into a path that doesn't exist or isn't being tailed

### Updating from source after a bug fix

Surgical only. When this repo lands a fix to a hook:

1. `git diff old new -- hooks/<script>.py` to see what changed
2. Apply the substantive logic change to the installed copy by hand (or with a targeted patch)
3. Leave site-customized constants alone

For the 2026-05-05 `stop_hook_checkpoint.py` `session_id` rglob fallback: the change is in `main()` between `transcript_path = payload.get(...)` and the `if transcript_path and os.path.exists(transcript_path):` block. Mirror it into the installed copy without touching the constants at the top of the file.


## License

MIT — same as continuity v1.
