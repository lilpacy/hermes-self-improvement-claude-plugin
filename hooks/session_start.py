#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def guidance_text() -> str:
    vendor = PLUGIN_ROOT / "vendor/hermes/skills_guidance.txt"
    if not vendor.is_file():
        vendor = PLUGIN_ROOT / "prompts/skills_guidance.fallback.txt"
    guidance = vendor.read_text(encoding="utf-8").strip()
    helper = PLUGIN_ROOT / "bin/hermes-claude-skill"
    quoted = guidance.replace("\n", "\n> ")
    return f"""## Hermes-compatible skill self-improvement

The following upstream Hermes guidance applies, with `skill_manage` mapped to the guarded command described below:

> {quoted}

### Guarded Claude Code adapter

- Use the exact executable `{helper}` for every global skill create/update/delete performed under this policy. Never edit skill directories directly for these operations.
- Before changing a skill, run `{helper} list`, then `{helper} view <name>` and read the complete current `SKILL.md`.
- Autonomous creation: write a complete candidate `SKILL.md` in the current workspace or `/tmp`, then run `{helper} create <name> --content-file <path>`. The new skill is agent-owned.
- Autonomous maintenance: immediately patch an outdated, incomplete, or wrong **agent-owned** skill with `{helper} patch`. Prefer a narrow exact replacement over `edit`.
- Existing or unregistered global skills are user-owned and protected. Foreground and background agents must not change them autonomously.
- Advisory boundary: the helper prints `USER APPROVAL REQUIRED`, but cannot mechanically prove approval. Run `authorize`, `create-user`, `adopt`, or `release` only when the user's current explicit request authorizes that exact target and action.
- When the user explicitly requests a protected skill change, run `{helper} authorize <name> --actions <action>`, then use its one-time token only for that requested operation via `--authorization <token>`.
- When the user explicitly asks to create a user-managed global skill, use `{helper} create-user`.
- Never call `authorize`, `adopt`, `release`, or `create-user` merely to bypass protection. `adopt` and `release` are only for an explicit user decision about future maintenance ownership.
- Project-local `.claude/skills` remain team/user-managed and are outside this global self-improvement manager.
- Save only verified procedures. Do not save unresolved guesses, secrets, personal data, temporary IDs, or raw conversation transcripts.
- After a complex successful task, a recovered failure, a user correction, or a non-trivial workflow discovery, evaluate whether a reusable skill should be created or an agent-owned skill patched before ending the turn.
"""


def main() -> int:
    try:
        json.load(sys.stdin)
    except Exception:
        pass
    if os.environ.get("HERMES_CLAUDE_BACKGROUND") == "1":
        return 0
    # Ensure config/registry exist and pick up skills created outside the helper.
    try:
        subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "scripts/skill_manager.py"), "list", "--json"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception:
        pass
    print(guidance_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
