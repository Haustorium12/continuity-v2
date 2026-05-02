"""
session_start_inject.py

Fires on every SessionStart event. Behavior depends on the source field:

  "compact"  -> context was just compacted.
                Read the checkpoint written by precompact_save.py and
                inject it as additionalContext (warm restart).

  "resume"   -> session resumed after a restart.
                Inject your project state file if one exists.

  "startup"  -> fresh session start. No injection.
  "clear"    -> user ran /clear intentionally. No injection.
"""

import sys
import json
import os
from datetime import datetime
from pathlib import Path

# Adapt these to your setup
CHECKPOINT = Path.home() / ".claude" / "compaction_checkpoint.md"
LOG = Path.home() / ".claude" / "hooks" / "continuity.log"

# Optional: path to a project state file to inject on resume
# Set to None to disable resume injection
PROJECT_STATE = Path.home() / ".claude" / "memory" / "project_current_state.md"

CHECKPOINT_MAX_AGE_MINUTES = 180
CHECKPOINT_MAX_CHARS = 4000
PROJECT_STATE_MAX_CHARS = 3000


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(datetime.now().isoformat()[:19], msg))
    except Exception:
        pass


def inject(context):
    print(json.dumps({"hookSpecificOutput": {"additionalContext": context}}))
    sys.exit(0)


def checkpoint_is_fresh():
    try:
        age_minutes = (datetime.now().timestamp() - CHECKPOINT.stat().st_mtime) / 60
        return age_minutes < CHECKPOINT_MAX_AGE_MINUTES
    except Exception:
        return False


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        payload = {}

    source = payload.get("source", "startup")
    log("SessionStart. source={}  session={}".format(source, payload.get("session_id", "")))

    if source == "compact":
        if CHECKPOINT.exists() and checkpoint_is_fresh():
            content = CHECKPOINT.read_text(encoding="utf-8", errors="replace")[:CHECKPOINT_MAX_CHARS]
            log("Injecting checkpoint ({} chars).".format(len(content)))
            inject(
                "=== WARM RESTART AFTER COMPACTION ===\n"
                "The context window was just compacted. The following state was "
                "captured immediately before compaction. Resume from here.\n\n"
                + content
            )
        log("No fresh checkpoint for compact source -- no injection.")

    elif source == "resume":
        if PROJECT_STATE and PROJECT_STATE.exists():
            content = PROJECT_STATE.read_text(encoding="utf-8", errors="replace")[:PROJECT_STATE_MAX_CHARS]
            log("Injecting project state for resume ({} chars).".format(len(content)))
            inject("=== SESSION RESUMED ===\n\n" + content)

    # startup / clear: no injection
    log("No injection for source={}.".format(source))
    sys.exit(0)


if __name__ == "__main__":
    main()
