"""
precompact_save.py

Fires before Claude Code compacts the context window.
Reads the transcript JSONL, extracts current session state,
writes a structured checkpoint to disk so session_start_inject.py
can restore it after compaction.

Never blocks compaction -- always exits 0.
"""

import sys
import json
import os
from datetime import datetime
from pathlib import Path

# Adapt these to your setup
CHECKPOINT = Path.home() / ".claude" / "compaction_checkpoint.md"
LOG = Path.home() / ".claude" / "hooks" / "precompact.log"


def log(msg):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.now().isoformat()[:19], msg))
    except Exception:
        pass


def extract_text(content):
    """Pull plain text out of a message content field (str or block list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts).strip()
    return ""


def parse_transcript(transcript_path):
    user_msgs = []       # [(timestamp, text)]
    assistant_msgs = []  # [(timestamp, text)]
    files_touched = []   # unique file paths from Edit/Write tool calls
    bash_descs = []      # bash operation descriptions

    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue

                mtype = obj.get("type")
                ts = obj.get("timestamp", "")[:16]

                if mtype == "user":
                    content = obj.get("message", {}).get("content", [])
                    # Skip messages that are purely tool results
                    if isinstance(content, list):
                        if any(
                            isinstance(c, dict) and c.get("type") == "tool_result"
                            for c in content
                        ):
                            continue
                    text = extract_text(content)
                    if text:
                        user_msgs.append((ts, text))

                elif mtype == "assistant":
                    content = obj.get("message", {}).get("content", [])
                    if not isinstance(content, list):
                        continue
                    text_parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            if name in ("Edit", "Write"):
                                fp = inp.get("file_path", "")
                                if fp and fp not in files_touched:
                                    files_touched.append(fp)
                            elif name == "Bash":
                                desc = inp.get("description", "")
                                cmd = inp.get("command", "")
                                entry = desc if desc else cmd[:80]
                                if entry:
                                    bash_descs.append(entry)
                    text = "\n".join(text_parts).strip()
                    if text:
                        assistant_msgs.append((ts, text))

    except Exception as e:
        log("parse_transcript error: {}".format(e))

    return user_msgs, assistant_msgs, files_touched, bash_descs


def build_checkpoint(payload, user_msgs, assistant_msgs, files_touched, bash_descs):
    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd", "")
    trigger = payload.get("trigger", "unknown")
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")

    lines = [
        "=== COMPACTION CHECKPOINT ===",
        "Session: {}".format(session_id),
        "Saved:   {}".format(now),
        "Trigger: {}".format(trigger),
        "CWD:     {}".format(cwd),
        "",
    ]

    lines.append("== RECENT USER MESSAGES (last 5) ==")
    for ts, text in user_msgs[-5:]:
        short = text[:200].replace("\n", " ")
        lines.append("  [{}] {}".format(ts, short))
    lines.append("")

    lines.append("== LAST ASSISTANT RESPONSE ==")
    if assistant_msgs:
        ts, text = assistant_msgs[-1]
        lines.append("[{}]".format(ts))
        # Cap at 1000 chars -- enough to know where we were
        lines.append(text[:1000])
    else:
        lines.append("(none recorded)")
    lines.append("")

    if files_touched:
        lines.append("== FILES TOUCHED THIS SESSION ==")
        for fp in files_touched[-20:]:
            lines.append("  {}".format(fp))
        lines.append("")

    if bash_descs:
        lines.append("== RECENT OPERATIONS ==")
        for desc in bash_descs[-10:]:
            lines.append("  - {}".format(desc[:100]))
        lines.append("")

    lines.append("=== END CHECKPOINT ===")
    return "\n".join(lines)


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path", "")
    log("PreCompact fired. session={} trigger={} transcript={}".format(
        session_id, payload.get("trigger", "?"), transcript_path
    ))

    if not transcript_path or not os.path.exists(transcript_path):
        # Fallback 1: discover JSONL via session_id
        if session_id and session_id != "unknown":
            projects_dir = Path.home() / ".claude" / "projects"
            matches = list(projects_dir.rglob("{}.jsonl".format(session_id)))
            if matches:
                transcript_path = str(matches[0])
                log("Discovered transcript via session_id: {}".format(transcript_path))

    if not transcript_path or not os.path.exists(transcript_path):
        # Fallback 2: if Stop hook wrote a fresh checkpoint recently, we're covered
        if CHECKPOINT.exists():
            age_min = (datetime.now().timestamp() - CHECKPOINT.stat().st_mtime) / 60
            if age_min < 30:
                log("No transcript found. Checkpoint is fresh ({:.1f} min old) -- Stop hook covered it.".format(age_min))
                sys.exit(0)
        log("No transcript found and checkpoint is stale or missing. Skipping.")
        sys.exit(0)

    user_msgs, assistant_msgs, files_touched, bash_descs = parse_transcript(transcript_path)

    checkpoint = build_checkpoint(payload, user_msgs, assistant_msgs, files_touched, bash_descs)

    try:
        CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
        CHECKPOINT.write_text(checkpoint, encoding="utf-8")
        log("Checkpoint written. users={} assistants={} files={} ops={}".format(
            len(user_msgs), len(assistant_msgs), len(files_touched), len(bash_descs)
        ))
    except Exception as e:
        log("Failed to write checkpoint: {}".format(e))

    # Never return a block decision -- let compaction proceed
    sys.exit(0)


if __name__ == "__main__":
    main()
