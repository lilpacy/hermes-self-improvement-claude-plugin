#!/usr/bin/env python3
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HELPERS = {
    str(PLUGIN_ROOT / "bin/hermes-claude-skill"),
    str(PLUGIN_ROOT / "bin/hermes-claude-review"),
}
# Ownership-changing subcommands stay on the normal permission flow.
AUTO_ALLOWED_ACTIONS = {
    "list", "view", "create", "patch", "edit", "write-file",
    "remove-file", "delete", "owner", "audit", "doctor",
    "status", "logs", "config",
}


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    command = str((event.get("tool_input") or {}).get("command") or "")
    try:
        words = shlex.split(command)
    except ValueError:
        return 0
    if not words or words[0] not in HELPERS:
        return 0
    action = next((w for w in words[1:] if not w.startswith("-")), "")
    if any(ch in command for ch in (";", "|", "&", "`", "$(", "\n")) or action not in AUTO_ALLOWED_ACTIONS:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "hermes-self-improvement guarded helper; ownership is enforced inside the helper",
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
