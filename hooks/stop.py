#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False))


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        emit({"continue": True, "suppressOutput": True})
        return 0
    if os.environ.get("HERMES_CLAUDE_BACKGROUND") == "1" or event.get("stop_hook_active"):
        emit({"continue": True, "suppressOutput": True})
        return 0

    data_root = Path(
        os.environ.get("HERMES_CLAUDE_DATA_DIR", "~/.claude/hermes-self-improvement")
    ).expanduser().resolve()
    config_path = data_root / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            config = {}
    except Exception:
        config = {}
    review = config.get("background_review", {})
    interval = int(review.get("interval_turns", 10))
    transcript_source = Path(str(event.get("transcript_path") or "")).expanduser()
    if (
        not review.get("enabled", True)
        or interval <= 0
        or not transcript_source.is_file()
        or transcript_source.stat().st_size > int(review.get("max_transcript_bytes", 26214400))
    ):
        emit({"continue": True, "suppressOutput": True})
        return 0

    state_root = data_root
    runtime = state_root / "runtime"
    events = runtime / "events"
    events.mkdir(parents=True, exist_ok=True)
    counters_path = runtime / "session-counters.json"
    lock_path = runtime / "session-counters.lock"
    if not acquire_short_lock(lock_path):
        emit({"continue": True, "suppressOutput": True})
        return 0
    try:
        counters = json.loads(counters_path.read_text(encoding="utf-8")) if counters_path.exists() else {}
        session_id = str(event.get("session_id") or "unknown")
        count = int(counters.get(session_id, 0)) + 1
        counters[session_id] = count
        atomic_json(counters_path, counters)
    finally:
        lock_path.unlink(missing_ok=True)
    if count % interval != 0:
        emit({"continue": True, "suppressOutput": True})
        return 0

    event_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    transcript_copy = events / f"{event_id}.transcript"
    try:
        shutil.copy2(transcript_source, transcript_copy)
    except OSError:
        emit({"continue": True, "suppressOutput": True})
        return 0
    queued_event = dict(event)
    queued_event["source_transcript_path"] = str(transcript_source)
    queued_event["transcript_path"] = str(transcript_copy)
    queued_event["managed_transcript_copy"] = True
    event_path = events / f"{event_id}.json"
    atomic_json(event_path, queued_event)

    worker = Path(__file__).resolve().parent.parent / "scripts/review_worker.py"
    env = os.environ.copy()
    env["HERMES_CLAUDE_BACKGROUND"] = "1"
    env["HERMES_CLAUDE_DATA_DIR"] = str(data_root)
    try:
        subprocess.Popen(
            [sys.executable, str(worker), "--config", str(config_path), "--event", str(event_path)],
            cwd=str(worker.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        transcript_copy.unlink(missing_ok=True)
        event_path.unlink(missing_ok=True)
    emit({"continue": True, "suppressOutput": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
