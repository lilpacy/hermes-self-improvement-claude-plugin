#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False))


def acquire_short_lock(path: Path, timeout_seconds: float = 2.0, stale_seconds: float = 30.0) -> bool:
    deadline = time.time() + timeout_seconds
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_seconds:
                    path.unlink()
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(0.05)


def process_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records: list[dict] = []
    changed = False
    messages: list[str] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not item.get("delivered"):
            item["delivered"] = True
            changed = True
            updates = item.get("changes") or []
            if updates:
                rendered = ", ".join(f"{u.get('action')}:{u.get('skill')}" for u in updates)
                messages.append(f"Background skill review applied: {rendered}. Log: {item.get('log')}")
            elif item.get("returncode") not in (0, None):
                messages.append(f"Background skill review failed; inspect {item.get('log')}")
        records.append(item)
    if changed:
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    return messages


def main() -> int:
    try:
        json.load(sys.stdin)
    except Exception:
        emit({"continue": True, "suppressOutput": True})
        return 0
    if os.environ.get("HERMES_CLAUDE_BACKGROUND") == "1":
        emit({"continue": True, "suppressOutput": True})
        return 0
    state_root = Path(
        os.environ.get("HERMES_CLAUDE_DATA_DIR", "~/.claude/hermes-self-improvement")
    ).expanduser().resolve()
    paths = [state_root / "notifications.jsonl", state_root / "notifications-fallback.jsonl"]
    if not any(path.exists() for path in paths):
        emit({"continue": True, "suppressOutput": True})
        return 0
    lock_path = state_root / "notifications.lock"
    if not acquire_short_lock(lock_path):
        emit({"continue": True, "suppressOutput": True})
        return 0
    try:
        messages: list[str] = []
        for path in paths:
            messages.extend(process_file(path))
    finally:
        lock_path.unlink(missing_ok=True)
    if messages:
        message = "\n".join(messages)
        emit({
            "systemMessage": message,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": message},
        })
    else:
        emit({"continue": True, "suppressOutput": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
