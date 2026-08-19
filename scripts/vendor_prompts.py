#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from common import atomic_write_json, atomic_write_text, now_iso

REPO = "NousResearch/hermes-agent"
API = f"https://api.github.com/repos/{REPO}"
RAW = f"https://raw.githubusercontent.com/{REPO}"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-codex-installer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to download Hermes upstream: {url}: {exc.reason}") from exc


def resolve_ref(ref: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return ref.lower()
    data = json.loads(fetch(f"{API}/commits/{ref}").decode("utf-8"))
    sha = str(data.get("sha", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError(f"could not resolve Hermes ref: {ref}")
    return sha


def extract_constant(source: str, name: str) -> str:
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                value = ast.literal_eval(node.value)
                if not isinstance(value, str):
                    raise RuntimeError(f"{name} is not a string")
                return value
    raise RuntimeError(f"constant not found: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--ref", default="main")
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    sha = resolve_ref(args.ref)
    files = {
        "prompt_builder.py": fetch(f"{RAW}/{sha}/agent/prompt_builder.py"),
        "background_review.py": fetch(f"{RAW}/{sha}/agent/background_review.py"),
        "LICENSE.hermes-agent": fetch(f"{RAW}/{sha}/LICENSE"),
    }
    prompt_builder = files["prompt_builder.py"].decode("utf-8")
    background_review = files["background_review.py"].decode("utf-8")
    extracted = {
        "skills_guidance.txt": extract_constant(prompt_builder, "SKILLS_GUIDANCE"),
        "skill_review_prompt.txt": extract_constant(background_review, "_SKILL_REVIEW_PROMPT"),
    }
    try:
        extracted["combined_review_prompt.txt"] = extract_constant(background_review, "_COMBINED_REVIEW_PROMPT")
    except RuntimeError:
        pass

    for name, body in extracted.items():
        atomic_write_text(output / name, body.rstrip() + "\n", 0o644)
    atomic_write_text(output / "LICENSE.hermes-agent", files["LICENSE.hermes-agent"].decode("utf-8"), 0o644)
    metadata = {
        "repository": REPO,
        "requested_ref": args.ref,
        "commit": sha,
        "fetched_at": now_iso(),
        "files": {
            name: hashlib.sha256((body if isinstance(body, bytes) else body.encode("utf-8"))).hexdigest()
            for name, body in {**files, **extracted}.items()
        },
    }
    atomic_write_json(output / "UPSTREAM.json", metadata, 0o644)
    print(sha)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=__import__("sys").stderr)
        raise SystemExit(1)
