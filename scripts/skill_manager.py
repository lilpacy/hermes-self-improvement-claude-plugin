#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from common import (
    FileLock,
    data_root,
    ensure_config,
    plugin_root,
    SKILL_NAME_RE,
    append_jsonl,
    atomic_write_json,
    copy_tree,
    expand_path,
    load_json,
    now_iso,
    safe_relative_path,
    sha256_bytes,
    tree_hash,
    validate_skill_dir,
)


def load_config() -> dict[str, Any]:
    _, config = ensure_config()
    return config


def roots(config: dict[str, Any]) -> tuple[Path, Path]:
    return expand_path(config.get("skills_root", "~/.claude/skills")), data_root()


def registry_path(state_root: Path) -> Path:
    return state_root / "registry.json"


def load_registry(state_root: Path) -> dict[str, Any]:
    registry = load_json(registry_path(state_root), {"version": 1, "skills": {}, "authorizations": {}})
    if not isinstance(registry, dict):
        raise RuntimeError("registry must be a JSON object")
    registry.setdefault("version", 1)
    registry.setdefault("skills", {})
    registry.setdefault("authorizations", {})
    return registry


def save_registry(state_root: Path, registry: dict[str, Any]) -> None:
    atomic_write_json(registry_path(state_root), registry)


def actor() -> str:
    forced = os.environ.get("HERMES_CLAUDE_ACTOR", "").strip().lower()
    if forced:
        if forced not in {"foreground", "background", "manual"}:
            raise RuntimeError(f"invalid HERMES_CLAUDE_ACTOR: {forced}")
        return forced
    return "foreground"


def validate_name(name: str) -> None:
    if not SKILL_NAME_RE.fullmatch(name):
        raise RuntimeError("skill name must match ^[a-z0-9][a-z0-9-]{0,63}$")


def skill_path(skills_root: Path, name: str) -> Path:
    validate_name(name)
    return skills_root / name


def sync_skill_record(registry: dict[str, Any], skills_root: Path, name: str) -> dict[str, Any] | None:
    records = registry["skills"]
    path = skill_path(skills_root, name)
    record = records.get(name)
    if record is not None:
        return record
    if not path.exists():
        return None
    inferred_owner = "external" if path.is_symlink() else "user"
    record = {
        "owner": inferred_owner,
        "protected": True,
        "created_by": "external" if inferred_owner == "external" else "user",
        "registered_at": now_iso(),
        "path": str(path),
        "patch_count": 0,
    }
    records[name] = record
    return record


def imported_owner(config: dict[str, Any], name: str) -> str | None:
    for value in config.get("import_owner_registries", []):
        source = load_json(expand_path(value), None)
        if not isinstance(source, dict):
            continue
        record = source.get("skills", {}).get(name)
        if isinstance(record, dict) and record.get("owner") in {"agent", "user"}:
            return str(record["owner"])
    return None


def sync_all(config: dict[str, Any], registry: dict[str, Any], skills_root: Path) -> None:
    skills_root.mkdir(parents=True, exist_ok=True)
    for path in sorted(skills_root.iterdir()):
        if path.name.startswith("."):
            continue
        if not ((path.is_dir() or path.is_symlink()) and (path / "SKILL.md").exists()):
            continue
        known = path.name in registry["skills"]
        record = sync_skill_record(registry, skills_root, path.name)
        if known or record is None or record.get("owner") == "external":
            continue
        owner = imported_owner(config, path.name)
        if owner == "agent":
            record.update({"owner": "agent", "protected": False, "created_by": "import:hermes-codex"})


def read_input(path_value: str) -> str:
    if path_value == "-":
        return sys.stdin.read()
    path = expand_path(path_value)
    allowed = [Path.cwd().resolve()]
    for value in (os.environ.get("TMPDIR"), "/tmp"):
        if value:
            allowed.append(expand_path(value))
    extra = os.environ.get("HERMES_CLAUDE_ALLOW_INPUT_ROOTS", "")
    for value in extra.split(os.pathsep):
        if value.strip():
            allowed.append(expand_path(value.strip()))
    if not any(path == root or root in path.parents for root in allowed):
        raise RuntimeError("input files must be inside the current workspace or a temporary directory")
    return path.read_text(encoding="utf-8")


def authorization_hash(token: str) -> str:
    return sha256_bytes(token.encode("utf-8"))


def purge_authorizations(registry: dict[str, Any]) -> None:
    now = time.time()
    auths = registry.get("authorizations", {})
    expired = [key for key, value in auths.items() if float(value.get("expires_at", 0)) < now or value.get("used")]
    for key in expired:
        auths.pop(key, None)


def check_authorization(
    registry: dict[str, Any], name: str, action: str, token: str | None, current_actor: str
) -> str | None:
    if current_actor == "background":
        return None
    if not token:
        return None
    key = authorization_hash(token)
    item = registry.get("authorizations", {}).get(key)
    if not item:
        return None
    if item.get("used") or float(item.get("expires_at", 0)) < time.time():
        return None
    if item.get("skill") != name or action not in set(item.get("actions", [])):
        return None
    return key


def require_write_permission(
    registry: dict[str, Any], skills_root: Path, name: str, action: str, token: str | None
) -> tuple[dict[str, Any], str | None]:
    record = sync_skill_record(registry, skills_root, name)
    if record is None:
        raise RuntimeError(f"skill does not exist: {name}")
    current_actor = actor()
    owner = record.get("owner", "user")
    if owner == "agent":
        return record, None
    auth_key = check_authorization(registry, name, action, token, current_actor)
    if auth_key:
        return record, auth_key
    if owner == "external":
        raise RuntimeError(f"DENIED: {name} is external/read-only")
    raise RuntimeError(
        f"DENIED: {name} is user-owned. Autonomous {current_actor} {action} is prohibited. "
        f"Run `hermes-claude-skill authorize {name} --actions {action}` only after the user explicitly approves this exact change."
    )


def audit(state_root: Path, event: dict[str, Any]) -> None:
    event = {"at": now_iso(), **event}
    append_jsonl(state_root / "audit.jsonl", event)


def history_dir(state_root: Path, name: str, action: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    return state_root / "history" / name / f"{stamp}-{action}"


def mutate_skill(
    config: dict[str, Any],
    state_root: Path,
    skills_root: Path,
    registry: dict[str, Any],
    name: str,
    action: str,
    mutator: Callable[[Path], None],
    token: str | None,
) -> None:
    record, auth_key = require_write_permission(registry, skills_root, name, action, token)
    target = skill_path(skills_root, name)
    if target.is_symlink():
        raise RuntimeError("cannot mutate symlinked skill")
    before_hash = tree_hash(target)
    temp_new = skills_root / f".hermes-claude-new-{name}-{uuid.uuid4().hex}"
    temp_old = skills_root / f".hermes-claude-old-{name}-{uuid.uuid4().hex}"
    copy_tree(target, temp_new)
    try:
        mutator(temp_new)
        validation = config.get("validation", {})
        errors = validate_skill_dir(
            temp_new,
            expected_name=name,
            max_file_bytes=int(validation.get("max_file_bytes", 262144)),
            max_skill_lines=int(validation.get("max_skill_lines", 500)),
        )
        if errors:
            raise RuntimeError("skill validation failed:\n- " + "\n- ".join(errors))
        os.rename(target, temp_old)
        try:
            os.rename(temp_new, target)
        except Exception:
            os.rename(temp_old, target)
            raise
        destination = history_dir(state_root, name, action)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(temp_old), str(destination))
    finally:
        if temp_new.exists():
            shutil.rmtree(temp_new, ignore_errors=True)
        if temp_old.exists() and not target.exists():
            os.rename(temp_old, target)
    after_hash = tree_hash(target)
    record["updated_at"] = now_iso()
    record["last_actor"] = actor()
    record["last_action"] = action
    record["patch_count"] = int(record.get("patch_count", 0)) + 1
    if auth_key:
        registry["authorizations"][auth_key]["used"] = True
    save_registry(state_root, registry)
    audit(
        state_root,
        {
            "status": "applied",
            "actor": actor(),
            "action": action,
            "skill": name,
            "owner": record.get("owner"),
            "before_hash": before_hash,
            "after_hash": after_hash,
            "cwd": os.getcwd(),
        },
    )


def create_skill(
    config: dict[str, Any], state_root: Path, skills_root: Path, registry: dict[str, Any], name: str, content: str, owner: str
) -> None:
    target = skill_path(skills_root, name)
    if target.exists() or name in registry["skills"]:
        raise RuntimeError(f"skill already exists or is registered: {name}")
    temp_new = skills_root / f".hermes-claude-new-{name}-{uuid.uuid4().hex}"
    temp_new.mkdir(parents=True)
    (temp_new / "SKILL.md").write_text(content, encoding="utf-8")
    validation = config.get("validation", {})
    errors = validate_skill_dir(
        temp_new,
        expected_name=name,
        max_file_bytes=int(validation.get("max_file_bytes", 262144)),
        max_skill_lines=int(validation.get("max_skill_lines", 500)),
    )
    if errors:
        shutil.rmtree(temp_new, ignore_errors=True)
        raise RuntimeError("skill validation failed:\n- " + "\n- ".join(errors))
    os.rename(temp_new, target)
    current_actor = actor()
    registry["skills"][name] = {
        "owner": owner,
        "protected": owner != "agent",
        "created_by": current_actor if owner == "agent" else "user",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "last_actor": current_actor,
        "last_action": "create",
        "patch_count": 0,
        "path": str(target),
    }
    save_registry(state_root, registry)
    audit(
        state_root,
        {
            "status": "applied",
            "actor": current_actor,
            "action": "create",
            "skill": name,
            "owner": owner,
            "before_hash": None,
            "after_hash": tree_hash(target),
            "cwd": os.getcwd(),
        },
    )


def command_list(config: dict[str, Any], state_root: Path, skills_root: Path, registry: dict[str, Any], as_json: bool) -> int:
    sync_all(config, registry, skills_root)
    save_registry(state_root, registry)
    rows = []
    for name, record in sorted(registry["skills"].items()):
        path = skill_path(skills_root, name)
        rows.append(
            {
                "name": name,
                "owner": record.get("owner", "user"),
                "protected": bool(record.get("protected", True)),
                "exists": path.exists(),
                "created_by": record.get("created_by"),
                "updated_at": record.get("updated_at"),
                "patch_count": int(record.get("patch_count", 0)),
            }
        )
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif not rows:
        print("No skills found.")
    else:
        print("NAME\tOWNER\tPROTECTED\tEXISTS\tPATCHES")
        for row in rows:
            print(f"{row['name']}\t{row['owner']}\t{str(row['protected']).lower()}\t{str(row['exists']).lower()}\t{row['patch_count']}")
    return 0


def command_view(skills_root: Path, registry: dict[str, Any], state_root: Path, name: str, file_path: str) -> int:
    record = sync_skill_record(registry, skills_root, name)
    if record is None:
        raise RuntimeError(f"skill does not exist: {name}")
    save_registry(state_root, registry)
    rel = safe_relative_path(file_path)
    target = skill_path(skills_root, name) / rel
    if not target.is_file() or target.is_symlink():
        raise RuntimeError(f"file not found: {name}/{rel.as_posix()}")
    sys.stdout.write(target.read_text(encoding="utf-8"))
    audit(state_root, {"status": "read", "actor": actor(), "action": "view", "skill": name, "file": rel.as_posix()})
    return 0


def command_patch(args: argparse.Namespace, config: dict[str, Any], state_root: Path, skills_root: Path, registry: dict[str, Any]) -> int:
    old = read_input(args.old_file)
    new = read_input(args.new_file)

    def apply(temp: Path) -> None:
        path = temp / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise RuntimeError("old_string was not found")
        if not args.all and count != 1:
            raise RuntimeError(f"old_string matched {count} times; make it unique or pass --all")
        path.write_text(text.replace(old, new, -1 if args.all else 1), encoding="utf-8")

    mutate_skill(config, state_root, skills_root, registry, args.name, "patch", apply, args.authorization)
    print(f"Patched {args.name}.")
    return 0


def command_edit(args: argparse.Namespace, config: dict[str, Any], state_root: Path, skills_root: Path, registry: dict[str, Any]) -> int:
    content = read_input(args.content_file)

    def apply(temp: Path) -> None:
        (temp / "SKILL.md").write_text(content, encoding="utf-8")

    mutate_skill(config, state_root, skills_root, registry, args.name, "edit", apply, args.authorization)
    print(f"Edited {args.name}.")
    return 0


def command_write_file(args: argparse.Namespace, config: dict[str, Any], state_root: Path, skills_root: Path, registry: dict[str, Any]) -> int:
    rel = safe_relative_path(args.file_path)
    if rel.as_posix() == "SKILL.md":
        raise RuntimeError("use edit or patch for SKILL.md")
    body = read_input(args.file)

    def apply(temp: Path) -> None:
        path = temp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    mutate_skill(config, state_root, skills_root, registry, args.name, "write-file", apply, args.authorization)
    print(f"Wrote {args.name}/{rel.as_posix()}.")
    return 0


def command_remove_file(args: argparse.Namespace, config: dict[str, Any], state_root: Path, skills_root: Path, registry: dict[str, Any]) -> int:
    rel = safe_relative_path(args.file_path)
    if rel.as_posix() == "SKILL.md":
        raise RuntimeError("SKILL.md cannot be removed")

    def apply(temp: Path) -> None:
        path = temp / rel
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"file not found: {rel.as_posix()}")
        path.unlink()
        parent = path.parent
        while parent != temp and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    mutate_skill(config, state_root, skills_root, registry, args.name, "remove-file", apply, args.authorization)
    print(f"Removed {args.name}/{rel.as_posix()}.")
    return 0


def command_delete(args: argparse.Namespace, state_root: Path, skills_root: Path, registry: dict[str, Any]) -> int:
    record, auth_key = require_write_permission(registry, skills_root, args.name, "delete", args.authorization)
    target = skill_path(skills_root, args.name)
    destination = history_dir(state_root, args.name, "delete")
    destination.parent.mkdir(parents=True, exist_ok=True)
    before_hash = tree_hash(target)
    shutil.move(str(target), str(destination))
    registry["skills"].pop(args.name, None)
    if auth_key:
        registry["authorizations"][auth_key]["used"] = True
    save_registry(state_root, registry)
    audit(
        state_root,
        {
            "status": "applied",
            "actor": actor(),
            "action": "delete",
            "skill": args.name,
            "owner": record.get("owner"),
            "before_hash": before_hash,
            "after_hash": None,
        },
    )
    print(f"Deleted {args.name}; backup: {destination}")
    return 0


def command_authorize(args: argparse.Namespace, state_root: Path, skills_root: Path, registry: dict[str, Any]) -> int:
    if actor() == "background":
        raise RuntimeError("background review cannot authorize protected-skill writes")
    if args.ttl < 30 or args.ttl > 3600:
        raise RuntimeError("ttl must be between 30 and 3600 seconds")
    record = sync_skill_record(registry, skills_root, args.name)
    if record is None:
        raise RuntimeError(f"skill does not exist: {args.name}")
    if record.get("owner") != "user":
        raise RuntimeError("authorization is only needed for user-owned skills")
    allowed = {"patch", "edit", "write-file", "remove-file", "delete"}
    actions = [item.strip() for item in args.actions.split(",") if item.strip()]
    if not actions or any(item not in allowed for item in actions):
        raise RuntimeError("actions must be a comma-separated subset of patch,edit,write-file,remove-file,delete")
    print(
        f"USER APPROVAL REQUIRED: issue an authorization for {args.name} only after the user explicitly requested these actions: {','.join(actions)}",
        file=sys.stderr,
    )
    token = secrets.token_urlsafe(32)
    key = authorization_hash(token)
    registry["authorizations"][key] = {
        "skill": args.name,
        "actions": actions,
        "created_at": now_iso(),
        "expires_at": time.time() + args.ttl,
        "used": False,
    }
    save_registry(state_root, registry)
    audit(state_root, {"status": "issued", "actor": "explicit-user-request", "action": "authorize", "skill": args.name, "actions": actions})
    print(token)
    return 0


def command_adopt(args: argparse.Namespace, state_root: Path, skills_root: Path, registry: dict[str, Any]) -> int:
    if actor() == "background":
        raise RuntimeError("background review cannot adopt user-owned skills")
    print(
        f"USER APPROVAL REQUIRED: adopt {args.name} only after the user explicitly requested autonomous future maintenance.",
        file=sys.stderr,
    )
    record = sync_skill_record(registry, skills_root, args.name)
    if record is None:
        raise RuntimeError(f"skill does not exist: {args.name}")
    if record.get("owner") == "external":
        raise RuntimeError("external skills cannot be adopted")
    record.update({"owner": "agent", "protected": False, "adopted_at": now_iso(), "last_actor": "explicit-user-request"})
    save_registry(state_root, registry)
    audit(state_root, {"status": "applied", "actor": "explicit-user-request", "action": "adopt", "skill": args.name, "owner": "agent"})
    print(f"Adopted {args.name}; autonomous updates are now allowed.")
    return 0


def command_release(args: argparse.Namespace, state_root: Path, skills_root: Path, registry: dict[str, Any]) -> int:
    if actor() == "background":
        raise RuntimeError("background review cannot change skill ownership")
    print(
        f"USER APPROVAL REQUIRED: release {args.name} only after the user explicitly requested user-managed ownership.",
        file=sys.stderr,
    )
    record = sync_skill_record(registry, skills_root, args.name)
    if record is None:
        raise RuntimeError(f"skill does not exist: {args.name}")
    if record.get("owner") == "external":
        raise RuntimeError("external skill ownership cannot be changed")
    record.update({"owner": "user", "protected": True, "released_at": now_iso(), "last_actor": actor()})
    save_registry(state_root, registry)
    audit(state_root, {"status": "applied", "actor": actor(), "action": "release", "skill": args.name, "owner": "user"})
    print(f"Released {args.name}; autonomous updates are now denied.")
    return 0


def command_owner(args: argparse.Namespace, state_root: Path, skills_root: Path, registry: dict[str, Any]) -> int:
    record = sync_skill_record(registry, skills_root, args.name)
    if record is None:
        raise RuntimeError(f"skill does not exist: {args.name}")
    save_registry(state_root, registry)
    print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_audit(args: argparse.Namespace, state_root: Path) -> int:
    path = state_root / "audit.jsonl"
    if not path.exists():
        print("No audit events.")
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()[-args.tail :]
    for line in lines:
        print(line)
    return 0


def command_doctor(config: dict[str, Any], state_root: Path, skills_root: Path) -> int:
    import shutil as _shutil

    root = plugin_root()
    checks = {
        "claude_executable": _shutil.which("claude") is not None,
        "config": (data_root() / "config.json").is_file(),
        "skills_root": skills_root.is_dir(),
        "state_root": state_root.is_dir(),
        "registry": registry_path(state_root).is_file(),
        "helper": (root / "bin/hermes-claude-skill").is_file(),
        "hooks_manifest": (root / "hooks/hooks.json").is_file(),
        "hermes_guidance": (root / "vendor/hermes/skills_guidance.txt").is_file()
        or (root / "prompts/skills_guidance.fallback.txt").is_file(),
        "hermes_review_prompt": (root / "vendor/hermes/skill_review_prompt.txt").is_file()
        or (root / "prompts/skill_review_prompt.fallback.txt").is_file(),
    }
    for name, ok in checks.items():
        print(f"{'OK' if ok else 'FAIL'}\t{name}")
    return 0 if all(checks.values()) else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hermes-claude-skill")
    sub = root.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("view")
    p.add_argument("name")
    p.add_argument("--file", default="SKILL.md")

    for command in ("create", "create-user"):
        p = sub.add_parser(command)
        p.add_argument("name")
        p.add_argument("--content-file", required=True)

    p = sub.add_parser("patch")
    p.add_argument("name")
    p.add_argument("--old-file", required=True)
    p.add_argument("--new-file", required=True)
    p.add_argument("--all", action="store_true")
    p.add_argument("--authorization")

    p = sub.add_parser("edit")
    p.add_argument("name")
    p.add_argument("--content-file", required=True)
    p.add_argument("--authorization")

    p = sub.add_parser("write-file")
    p.add_argument("name")
    p.add_argument("file_path")
    p.add_argument("--file", required=True)
    p.add_argument("--authorization")

    p = sub.add_parser("remove-file")
    p.add_argument("name")
    p.add_argument("file_path")
    p.add_argument("--authorization")

    p = sub.add_parser("delete")
    p.add_argument("name")
    p.add_argument("--authorization")

    p = sub.add_parser("authorize")
    p.add_argument("name")
    p.add_argument("--actions", required=True)
    p.add_argument("--ttl", type=int, default=600)

    p = sub.add_parser("adopt")
    p.add_argument("name")

    p = sub.add_parser("release")
    p.add_argument("name")

    p = sub.add_parser("owner")
    p.add_argument("name")

    p = sub.add_parser("audit")
    p.add_argument("--tail", type=int, default=20)

    sub.add_parser("doctor")
    return root


def main() -> int:
    args = parser().parse_args()
    config = load_config()
    skills_root, state_root = roots(config)
    skills_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(state_root / "registry.lock", stale_seconds=1800)
    try:
        with lock:
            registry = load_registry(state_root)
            purge_authorizations(registry)
            sync_all(config, registry, skills_root)
            save_registry(state_root, registry)
            if args.command == "list":
                return command_list(config, state_root, skills_root, registry, args.json)
            if args.command == "view":
                return command_view(skills_root, registry, state_root, args.name, args.file)
            if args.command == "create":
                create_skill(config, state_root, skills_root, registry, args.name, read_input(args.content_file), "agent")
                print(f"Created agent-owned skill {args.name}.")
                return 0
            if args.command == "create-user":
                if actor() == "background":
                    raise RuntimeError("background review cannot create user-owned skills")
                print(
                    f"USER APPROVAL REQUIRED: create user-owned skill {args.name} only after the user explicitly requested it.",
                    file=sys.stderr,
                )
                create_skill(config, state_root, skills_root, registry, args.name, read_input(args.content_file), "user")
                print(f"Created user-owned skill {args.name}.")
                return 0
            if args.command == "patch":
                return command_patch(args, config, state_root, skills_root, registry)
            if args.command == "edit":
                return command_edit(args, config, state_root, skills_root, registry)
            if args.command == "write-file":
                return command_write_file(args, config, state_root, skills_root, registry)
            if args.command == "remove-file":
                return command_remove_file(args, config, state_root, skills_root, registry)
            if args.command == "delete":
                return command_delete(args, state_root, skills_root, registry)
            if args.command == "authorize":
                return command_authorize(args, state_root, skills_root, registry)
            if args.command == "adopt":
                return command_adopt(args, state_root, skills_root, registry)
            if args.command == "release":
                return command_release(args, state_root, skills_root, registry)
            if args.command == "owner":
                return command_owner(args, state_root, skills_root, registry)
            if args.command == "audit":
                return command_audit(args, state_root)
            if args.command == "doctor":
                return command_doctor(config, state_root, skills_root)
    except RuntimeError as exc:
        audit(state_root, {"status": "denied", "actor": actor(), "action": args.command, "skill": getattr(args, "name", None), "error": str(exc)})
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
