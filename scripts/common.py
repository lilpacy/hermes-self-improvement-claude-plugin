#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/.+=]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.fspath(value))).expanduser().resolve()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            digest.update(f"L\0{item.relative_to(path).as_posix()}\0{os.readlink(item)}\n".encode())
        elif item.is_file():
            digest.update(f"F\0{item.relative_to(path).as_posix()}\0".encode())
            with item.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


class FileLock:
    def __init__(self, path: Path, stale_seconds: int = 3600) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.acquired = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - self.path.stat().st_mtime > self.stale_seconds:
                    self.path.unlink()
                    return self.__enter__()
            except OSError:
                pass
            raise RuntimeError(f"lock already held: {self.path}")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "created_at": now_iso()}, fh)
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("SKILL.md frontmatter is not closed")
    result: dict[str, str] = {}
    for raw in text[4:end].splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def validate_skill_dir(skill_dir: Path, *, expected_name: str | None = None, max_file_bytes: int = 262144, max_skill_lines: int = 500) -> list[str]:
    errors: list[str] = []
    if skill_dir.is_symlink():
        return ["skill directory must not be a symlink"]
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return ["missing SKILL.md"]
    try:
        text = skill_md.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["SKILL.md must be UTF-8"]
    try:
        frontmatter = parse_frontmatter(text)
    except ValueError as exc:
        errors.append(str(exc))
        frontmatter = {}
    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")
    if not SKILL_NAME_RE.fullmatch(name):
        errors.append(f"invalid skill name in frontmatter: {name!r}")
    if name and name != (expected_name or skill_dir.name):
        errors.append("frontmatter name must equal directory name")
    if not description.strip():
        errors.append("description is required")
    if len(text.splitlines()) > max_skill_lines:
        errors.append(f"SKILL.md exceeds {max_skill_lines} lines")
    for path in sorted(skill_dir.rglob("*")):
        if path.is_symlink():
            errors.append(f"symlink not allowed: {path.relative_to(skill_dir)}")
            continue
        if not path.is_file():
            continue
        if path.stat().st_size > max_file_bytes:
            errors.append(f"file exceeds {max_file_bytes} bytes: {path.relative_to(skill_dir)}")
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(body):
                errors.append(f"possible secret in {path.relative_to(skill_dir)}")
                break
    return errors


def safe_relative_path(value: str) -> Path:
    rel = Path(value)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("file path must be a safe relative path")
    return rel


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)


def iter_jsonl(path: Path, start: int = 0) -> Iterator[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(start)
        while True:
            line = fh.readline()
            if not line:
                break
            offset = fh.tell()
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                yield offset, item


PLUGIN_NAME = "hermes-self-improvement"
DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "skills_root": "~/.claude/skills",
    "import_owner_registries": ["~/.local/state/hermes-codex/registry.json"],
    "background_review": {
        "enabled": True,
        "interval_turns": 10,
        "timeout_seconds": 900,
        "model": "",
        "max_transcript_bytes": 26214400,
        "delete_transcript_copy": True,
    },
    "validation": {"max_file_bytes": 262144, "max_skill_lines": 500},
}


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    return expand_path(os.environ.get("HERMES_CLAUDE_DATA_DIR", "~/.claude/hermes-self-improvement"))


def _merge_defaults(base: Any, value: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(value, dict):
        return value
    result = dict(base)
    for key, item in value.items():
        result[key] = _merge_defaults(result.get(key), item)
    return result


def ensure_config() -> tuple[Path, dict[str, Any]]:
    path = data_root() / "config.json"
    defaults = DEFAULT_CONFIG
    if not path.exists():
        # First run: inherit the canonical skills root from a hermes-codex
        # adapter install so both adapters manage the same skill directories.
        codex_config = load_json(expand_path("~/.config/hermes-codex/config.json"), {})
        if isinstance(codex_config, dict) and codex_config.get("skills_root"):
            defaults = dict(DEFAULT_CONFIG)
            defaults["skills_root"] = str(codex_config["skills_root"])
    current = load_json(path, {})
    if not isinstance(current, dict):
        raise RuntimeError(f"invalid config: {path}")
    config = _merge_defaults(defaults, current)
    if config != current:
        atomic_write_json(path, config)
    return path, config
