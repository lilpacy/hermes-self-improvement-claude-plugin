#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from common import FileLock, append_jsonl, data_root, expand_path, load_json, now_iso, plugin_root


def load_config(path: Path) -> dict[str, Any]:
    value = load_json(path, {})
    if not isinstance(value, dict):
        raise RuntimeError("config must be an object")
    return value


def audit_events(path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    if not path.exists():
        return 0, []
    result: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        for line in fh:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("actor") == "background" and item.get("status") == "applied":
                result.append(item)
        return fh.tell(), result


def build_prompt(config: dict[str, Any], transcript_name: str, source_cwd: str) -> str:
    root = plugin_root()
    helper = root / "bin/hermes-claude-skill"
    upstream = root / "vendor/hermes/skill_review_prompt.txt"
    if not upstream.exists():
        upstream = root / "prompts/skill_review_prompt.fallback.txt"
    upstream_text = upstream.read_text(encoding="utf-8")
    return f"""{upstream_text.rstrip()}

# Claude Code adapter and ownership policy

The conversation is stored verbatim in `{transcript_name}`. Read it before deciding anything.
The original working directory was `{source_cwd}`. It is context only: do not access or modify that repository.
Hermes `skill_manage` is represented by this exact executable:

    {helper}

Use only these mappings:

| Hermes action | Command |
|---|---|
| list | `{helper} list` |
| view | `{helper} view <name>` |
| create | write a complete SKILL.md to a scratch file, then `{helper} create <name> --content-file <file>` |
| patch | write exact old/new strings to scratch files, then `{helper} patch <name> --old-file <old> --new-file <new>` |
| edit | write the complete replacement SKILL.md to a scratch file, then `{helper} edit <name> --content-file <file>` |
| write_file | `{helper} write-file <name> <relative-path> --file <file>` |
| remove_file | `{helper} remove-file <name> <relative-path>` |
| delete | `{helper} delete <name>` |

Mandatory constraints:

1. Existing unknown skills are user-owned. The helper rejects autonomous changes to them.
2. You may create new agent-owned skills and directly improve existing agent-owned skills.
3. Never call `authorize`, `adopt`, `create-user`, or `release`. Background review has no user authorization or ownership-changing authority.
4. Prefer exact `patch` over full `edit`.
5. Save only procedures supported by evidence in the transcript. Do not save guesses or unresolved failures.
6. Do not save credentials, personal data, temporary identifiers, or raw conversation text.
7. Do not modify the source repository, Git state, Claude Code configuration, hooks, or the registry directly.
8. If nothing is worth saving, make no changes.
9. Before changing a skill, run `list` and `view` and read the relevant complete SKILL.md.
10. A created SKILL.md must use valid frontmatter with matching lowercase hyphenated `name` and a precise `description`.

Finish with a concise summary of skill changes, or state that no durable skill change was justified.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--event", required=True)
    args = parser.parse_args()

    config_path = expand_path(args.config)
    event_path = expand_path(args.event)
    config = load_config(config_path)
    state_root = data_root()
    review_cfg = config.get("background_review", {})
    event = load_json(event_path, {})
    transcript_value = event.get("transcript_path")
    transcript = Path(str(transcript_value)).expanduser().resolve() if transcript_value else None
    if transcript is None or not transcript.is_file():
        return 0

    runtime = state_root / "runtime"
    logs = runtime / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    event_id = event_path.stem
    log_path = logs / f"{event_id}.log"
    audit_path = state_root / "audit.jsonl"
    before_offset = audit_path.stat().st_size if audit_path.exists() else 0
    returncode = 1
    stdout = ""
    stderr = ""
    started = time.time()

    try:
        with FileLock(runtime / "background-review.lock", stale_seconds=int(review_cfg.get("timeout_seconds", 900)) + 120):
            with tempfile.TemporaryDirectory(prefix="hermes-claude-review-") as tmp:
                temp = Path(tmp)
                transcript_copy = temp / "conversation-transcript.txt"
                shutil.copy2(transcript, transcript_copy)
                prompt = build_prompt(config, transcript_copy.name, str(event.get("cwd") or "unknown"))
                helper = plugin_root() / "bin/hermes-claude-skill"
                cmd = [
                    "claude",
                    "-p",
                    "--bare",
                    "--permission-mode",
                    "dontAsk",
                    "--allowedTools",
                    f"Read,Write,Bash({helper} *)",
                ]
                model = str(review_cfg.get("model") or "").strip()
                if model:
                    cmd.extend(["--model", model])
                env = os.environ.copy()
                env.update(
                    {
                        "HERMES_CLAUDE_BACKGROUND": "1",
                        "HERMES_CLAUDE_ACTOR": "background",
                        "HERMES_CLAUDE_DATA_DIR": str(data_root()),
                    }
                )
                try:
                    proc = subprocess.run(
                        cmd,
                        cwd=temp,
                        input=prompt,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=int(review_cfg.get("timeout_seconds", 900)),
                        env=env,
                        check=False,
                    )
                    returncode = proc.returncode
                    stdout = proc.stdout
                    stderr = proc.stderr
                except subprocess.TimeoutExpired as exc:
                    returncode = 124
                    stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                    stderr = (exc.stderr if isinstance(exc.stderr, str) else "") + "\nBackground review timed out."
    except RuntimeError as exc:
        returncode = 75
        stderr = str(exc)
    finally:
        log_path.write_text(
            json.dumps(
                {
                    "started_at": now_iso(),
                    "duration_seconds": round(time.time() - started, 3),
                    "returncode": returncode,
                    "source_cwd": event.get("cwd"),
                    "session_id": event.get("session_id"),
                    "hermes_upstream": load_json(plugin_root() / "vendor/hermes/UPSTREAM.json", {}),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n\n--- stdout ---\n"
            + stdout
            + "\n--- stderr ---\n"
            + stderr,
            encoding="utf-8",
        )
        if event.get("managed_transcript_copy") and review_cfg.get("delete_transcript_copy", True):
            transcript.unlink(missing_ok=True)
            event_path.unlink(missing_ok=True)

    _, changes = audit_events(audit_path, before_offset)
    notification = {
        "id": event_id,
        "created_at": now_iso(),
        "session_id": event.get("session_id"),
        "source_cwd": event.get("cwd"),
        "returncode": returncode,
        "changes": [
            {"action": item.get("action"), "skill": item.get("skill"), "owner": item.get("owner")}
            for item in changes
        ],
        "delivered": False,
        "log": str(log_path),
    }
    try:
        with FileLock(state_root / "notifications.lock", stale_seconds=300):
            append_jsonl(state_root / "notifications.jsonl", notification)
    except RuntimeError:
        append_jsonl(state_root / "notifications-fallback.jsonl", notification)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
