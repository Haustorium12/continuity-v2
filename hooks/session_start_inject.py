"""
session_start_inject.py

Fires on every SessionStart event. Injects context based on the source field:

  "compact"  -> context window was just compacted.
                Read the checkpoint saved by precompact_save.py and inject it.
                Falls back to PROJECT_STATE if no fresh checkpoint exists.
  "resume"   -> session resumed after restart.
                Inject PROJECT_STATE as orientation.
  "startup"  -> fresh session start. No injection.
  "clear"    -> user ran /clear intentionally. No injection.

Output: JSON with hookSpecificOutput.additionalContext, or nothing.
"""

import sys
import json
import os
from datetime import datetime
from pathlib import Path

# Adapt these to your setup
CHECKPOINT = Path.home() / ".claude" / "compaction_checkpoint.md"
LOG = Path.home() / ".claude" / "hooks" / "session_start.log"

# Path to a project state file to inject on resume and as compact fallback.
# Set to None to disable. Conventionally: a sticky-note file your boot
# protocol writes at session end summarizing current state.
PROJECT_STATE = Path.home() / ".claude" / "memory" / "project_current_state.md"

# Max age in minutes before a checkpoint is considered stale
CHECKPOINT_MAX_AGE_MINUTES = 180

# Character caps to avoid eating the whole context budget
CHECKPOINT_MAX_CHARS = 4000
PROJECT_STATE_MAX_CHARS = 3000


def log(msg):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.now().isoformat()[:19], msg))
    except Exception:
        pass


def read_file(path, max_chars):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except Exception:
        return None


def checkpoint_is_fresh():
    try:
        age_minutes = (datetime.now().timestamp() - CHECKPOINT.stat().st_mtime) / 60
        return age_minutes < CHECKPOINT_MAX_AGE_MINUTES
    except Exception:
        return False


def inject(context_text):
    """Print the additionalContext JSON and exit."""
    output = {
        "hookSpecificOutput": {
            "additionalContext": context_text
        }
    }
    print(json.dumps(output))
    sys.exit(0)


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    source = payload.get("source", "startup")
    session_id = payload.get("session_id", "unknown")
    log("SessionStart fired. source={} session={}".format(source, session_id))

    if source == "compact":
        # Context window was just compacted -- warm restart from checkpoint
        if CHECKPOINT.exists() and checkpoint_is_fresh():
            content = read_file(str(CHECKPOINT), CHECKPOINT_MAX_CHARS)
            if content:
                context = (
                    "=== WARM RESTART AFTER COMPACTION ===\n"
                    "The context window was just compacted. The following state was "
                    "captured immediately before compaction. Resume from here.\n\n"
                    + content
                    + "\n\nRead project_current_state.md if you need deeper context."
                )
                log("Injecting compaction checkpoint ({} chars)".format(len(content)))
                inject(context)
        else:
            # Checkpoint missing or stale -- fall back to project state
            log("Checkpoint missing or stale, falling back to project state")
            if PROJECT_STATE and PROJECT_STATE.exists():
                content = read_file(str(PROJECT_STATE), PROJECT_STATE_MAX_CHARS)
                if content:
                    context = (
                        "=== COMPACTION OCCURRED (no fresh checkpoint) ===\n"
                        "Context was compacted but no checkpoint was found. "
                        "Project state follows -- re-orient from here.\n\n"
                        + content
                    )
                    inject(context)

    elif source == "resume":
        # Session resumed after restart -- inject current project state
        if PROJECT_STATE and PROJECT_STATE.exists():
            content = read_file(str(PROJECT_STATE), PROJECT_STATE_MAX_CHARS)
            if content:
                context = (
                    "=== SESSION RESUMED ===\n"
                    "This session was resumed after a restart. "
                    "Current project state:\n\n"
                    + content
                )
                log("Injecting project state for resume ({} chars)".format(len(content)))
                inject(context)

    # source == "startup" or "clear": no injection
    log("No injection for source={}".format(source))
    sys.exit(0)


if __name__ == "__main__":
    main()
