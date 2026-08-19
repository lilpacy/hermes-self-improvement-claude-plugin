#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from common import atomic_write_json, data_root, expand_path, load_json, plugin_root


def load_config(path: Path) -> dict:
    config = load_json(path, {})
    if not isinstance(config, dict):
        raise RuntimeError("invalid config")
    return config


def main() -> int:
    parser = argparse.ArgumentParser(prog="hermes-claude-review")
    parser.add_argument("--config", default=str(data_root() / "config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("run")
    p.add_argument("--transcript", required=True)
    p.add_argument("--cwd", default=os.getcwd())
    p = sub.add_parser("logs")
    p.add_argument("--tail", type=int, default=10)
    p = sub.add_parser("config")
    toggle = p.add_mutually_exclusive_group()
    toggle.add_argument("--enable", action="store_true")
    toggle.add_argument("--disable", action="store_true")
    p.add_argument("--interval", type=int)
    p.add_argument("--timeout", type=int)
    p.add_argument("--model")
    p = sub.add_parser("upstream-update")
    p.add_argument("ref", nargs="?", default="main")
    args = parser.parse_args()

    config_path = expand_path(args.config)
    config = load_config(config_path)
    state_root = data_root()
    share_root = plugin_root()

    if args.command == "status":
        upstream = load_json(share_root / "vendor/hermes/UPSTREAM.json", {})
        review = config.get("background_review", {})
        print(
            json.dumps(
                {
                    "config": str(config_path),
                    "plugin_root": str(share_root),
                    "skills_root": str(expand_path(config.get("skills_root", "~/.claude/skills"))),
                    "state_root": str(state_root),
                    "background_review": review,
                    "hermes_upstream": upstream,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "run":
        transcript = expand_path(args.transcript)
        if not transcript.is_file():
            raise RuntimeError(f"transcript not found: {transcript}")
        events = state_root / "runtime/events"
        events.mkdir(parents=True, exist_ok=True)
        event_id = time.strftime("%Y%m%d-%H%M%S") + "-manual-" + uuid.uuid4().hex[:8]
        event_path = events / f"{event_id}.json"
        event_path.write_text(
            json.dumps(
                {
                    "session_id": "manual",
                    "turn_id": event_id,
                    "transcript_path": str(transcript),
                    "managed_transcript_copy": False,
                    "cwd": str(expand_path(args.cwd)),
                    "hook_event_name": "ManualReview",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["HERMES_CLAUDE_BACKGROUND"] = "1"
        return subprocess.call(
            [
                sys.executable,
                str(share_root / "scripts/review_worker.py"),
                "--config",
                str(config_path),
                "--event",
                str(event_path),
            ],
            env=env,
        )

    if args.command == "logs":
        logs = state_root / "runtime/logs"
        files = sorted(logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[: args.tail] if logs.exists() else []
        if not files:
            print("No review logs.")
        for path in files:
            print(path)
        return 0


    if args.command == "config":
        review = config.setdefault("background_review", {})
        if args.enable:
            review["enabled"] = True
        if args.disable:
            review["enabled"] = False
        if args.interval is not None:
            if args.interval < 0:
                raise RuntimeError("interval must be 0 or greater")
            review["interval_turns"] = args.interval
        if args.timeout is not None:
            if args.timeout < 30:
                raise RuntimeError("timeout must be at least 30 seconds")
            review["timeout_seconds"] = args.timeout
        if args.model is not None:
            review["model"] = args.model.strip()
        atomic_write_json(config_path, config)
        print(json.dumps(review, ensure_ascii=False, indent=2))
        return 0

    if args.command == "upstream-update":
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        backup = state_root / "upstream-backups" / stamp
        vendor = share_root / "vendor/hermes"
        if vendor.exists():
            shutil.copytree(vendor, backup / "vendor", dirs_exist_ok=True)
        return subprocess.call(
            [
                sys.executable,
                str(share_root / "scripts/vendor_prompts.py"),
                "--output",
                str(share_root / "vendor/hermes"),
                "--ref",
                args.ref,
            ]
        )
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
