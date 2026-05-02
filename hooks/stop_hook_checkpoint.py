"""
stop_hook_checkpoint.py

Fires on every Stop event (end of turn).
Reads bell signal files written by sse_proxy.py.

If any bells have fired since the last check:
  bell_70 -> write/update checkpoint, no warning
  bell_85 -> write/update checkpoint + inject pressure warning + sticky note request
  bell_95 -> write/update checkpoint + inject emergency warning + sticky note request

Also scans the last user message for session-close trigger words
("save", "goodnight", etc.) and injects a sticky note write request
so Claude overwrites project_current_state.md before the session ends.

Bells are cleared after reading so each threshold fires once
per crossing, not once per turn.
"""

import sys
import json
import os
from datetime import datetime
from pathlib import Path

SIGNALS_DIR = Path.home() / ".claude" / "hooks" / "signals"
CHECKPOINT = Path.home() / ".claude" / "compaction_checkpoint.md"
LOG = Path.home() / ".claude" / "hooks" / "sse_proxy.log"
SESSION_STATE = r"C:\Users\Sean\.claude\projects\C--dev\memory\project_current_state.md"

CLOSE_TRIGGERS = [
    "save", "goodnight", "good night", "wrap up", "wrapping up",
    "closing", "close out", "done for today", "done for the day",
    "end session", "signing off", "signing out", "bye", "goodbye",
    "that's it for today", "thats it for today", "shutting down",
]

STICKY_NOTE_PROMPT = (
    "[SESSION CLOSE] Overwrite {} now. "
    "UDNL, <=60 lines. Sections: CONTEXT_SNAPSHOT, DONE_THIS_SESSION, "
    "WHERE_WE_ARE, STILL_PENDING (only truly unfinished items), NEXT_MOVES, MOOD. "
    "Be precise about what is done vs in-progress. This is the boot note."
).format(SESSION_STATE)


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.now().isoformat()[:19], msg))
    except Exception:
        pass


def read_and_clear_signals():
    """Return (highest_level, signal_data_dict). Clears all signal files."""
    highest = None
    data = {}
    for level in [95, 85, 70]:
        path = SIGNALS_DIR / "bell_{}.signal".format(level)
        if path.exists():
            try:
                data[level] = json.loads(path.read_text(encoding="utf-8"))
                if highest is None:
                    highest = level
            except Exception:
                pass
            try:
                path.unlink()
            except Exception:
                pass
    return highest, data


# ---- transcript parsing (mirrors precompact_save.py) ----

def extract_text(content):
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
    user_msgs = []
    assistant_msgs = []
    files_touched = []
    bash_descs = []
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
                    if isinstance(content, list) and any(
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


def write_checkpoint(transcript_path, session_id, label="periodic"):
    try:
        user_msgs, assistant_msgs, files_touched, bash_descs = parse_transcript(transcript_path)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
        lines = [
            "=== COMPACTION CHECKPOINT ({}) ===".format(label),
            "Session: {}".format(session_id),
            "Saved:   {}".format(now),
            "",
            "== RECENT USER MESSAGES (last 5) ==",
        ]
        for ts, text in user_msgs[-5:]:
            lines.append("  [{}] {}".format(ts, text[:200].replace("\n", " ")))
        lines.append("")
        lines.append("== LAST ASSISTANT RESPONSE ==")
        if assistant_msgs:
            ts, text = assistant_msgs[-1]
            lines.append("[{}]".format(ts))
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
        CHECKPOINT.write_text("\n".join(lines), encoding="utf-8")
        log("Checkpoint written ({}): users={} files={} ops={}".format(
            label, len(user_msgs), len(files_touched), len(bash_descs)))
    except Exception as e:
        log("Checkpoint write error: {}".format(e))


def get_last_user_message(transcript_path):
    """Return the last non-tool-result user message text from the transcript."""
    last_user = None
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
                if obj.get("type") != "user":
                    continue
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list) and any(
                    isinstance(c, dict) and c.get("type") == "tool_result"
                    for c in content
                ):
                    continue
                text = extract_text(content)
                if text:
                    last_user = text
    except Exception as e:
        log("get_last_user_message error: {}".format(e))
    return last_user


def is_close_trigger(text):
    """Return the matched trigger word if text contains a session-close signal."""
    lower = text.lower()
    for trigger in CLOSE_TRIGGERS:
        if trigger in lower:
            return trigger
    return None


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        payload = {}

    highest, signal_data = read_and_clear_signals()
    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path", "")

    # Always write checkpoint when transcript is available.
    # PreCompact no longer needs to find the transcript itself -- it relies on this being current.
    if transcript_path and os.path.exists(transcript_path):
        if highest is not None:
            write_checkpoint(transcript_path, session_id, label="bell_{}".format(highest))
        else:
            write_checkpoint(transcript_path, session_id, label="periodic")
    elif not transcript_path:
        log("No transcript_path in Stop payload -- checkpoint not updated.")

    # Check for session-close trigger in last user message
    trigger_word = None
    if transcript_path and os.path.exists(transcript_path):
        last_msg = get_last_user_message(transcript_path)
        if last_msg:
            trigger_word = is_close_trigger(last_msg)
            if trigger_word:
                log("Close trigger detected: '{}'".format(trigger_word))

    # If no bells and no trigger, nothing to inject
    if highest is None and not trigger_word:
        sys.exit(0)

    # Build additionalContext
    context_parts = []

    if highest is not None and highest >= 85:
        info = signal_data.get(highest, {})
        pct = info.get("percentage", float(highest))
        token_count = info.get("token_count", "?")
        max_tokens = info.get("max_tokens", 200000)
        if highest >= 95:
            pressure = (
                "[CONTEXT CRITICAL -- HARD STOP: {:.1f}% ({}/{} tokens used)] "
                "Compaction fires next turn. You MUST do these three things RIGHT NOW "
                "before responding to anything else: "
                "(1) Finish or explicitly abandon the current task. "
                "(2) Overwrite {} with current state (UDNL, <=60 lines). "
                "(3) Acknowledge the state write in your response. "
                "This is not optional. The context will be destroyed."
            ).format(pct, token_count, max_tokens, SESSION_STATE)
        else:
            pressure = (
                "[CONTEXT PRESSURE -- ACTION REQUIRED: {:.1f}% ({}/{} tokens used)] "
                "Context window is at 85%%. You MUST overwrite {} now "
                "with current session state (UDNL, <=60 lines) before continuing. "
                "Do not defer. Write the sticky note this turn."
            ).format(pct, token_count, max_tokens, SESSION_STATE)
        context_parts.append(pressure)
        log("Injecting hard pressure directive (bell_{})".format(highest))

    if trigger_word:
        context_parts.append(STICKY_NOTE_PROMPT)
        log("Injecting sticky note prompt (trigger: '{}')".format(trigger_word))

    if context_parts:
        print(json.dumps({
            "hookSpecificOutput": {
                "additionalContext": "\n\n".join(context_parts)
            }
        }))

    sys.exit(0)


if __name__ == "__main__":
    main()
