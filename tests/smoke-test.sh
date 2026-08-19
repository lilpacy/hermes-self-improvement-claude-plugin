#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SKILLS="$TMP/home/dotfiles-skills"
mkdir -p "$TMP/fakebin" "$SKILLS/user-skill" "$SKILLS/codex-learned" "$TMP/work" \
  "$TMP/home/.config/hermes-codex" "$TMP/home/.local/state/hermes-codex"

# 正常系: 背景レビューは claude ヘッドレスを固定フラグで起動し、helper 経由でのみ書き込む
cat > "$TMP/fakebin/claude" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != "-p" || "${2:-}" != "--bare" || "${3:-}" != "--permission-mode" || "${4:-}" != "dontAsk" || "${5:-}" != "--allowedTools" ]]; then
  printf 'unexpected claude argument order: %q\n' "$*" >&2
  exit 8
fi
ALLOWED="${6:-}"
case "$ALLOWED" in
  "Read,Write,Bash("*" *)") ;;
  *) printf 'unexpected allowedTools: %s\n' "$ALLOWED" >&2; exit 8 ;;
esac
HELPER="${ALLOWED#Read,Write,Bash(}"
HELPER="${HELPER% \*)}"
cat >/dev/null
cat > bg.md <<'SKILL'
---
name: background-learned
description: Procedure learned by the isolated background reviewer.
---
# Background learned
Use the verified background procedure.
SKILL
"$HELPER" create background-learned --content-file "$PWD/bg.md"
printf 'Original.\n' > old.txt
printf 'Background must not write this.\n' > new.txt
if "$HELPER" patch user-skill --old-file "$PWD/old.txt" --new-file "$PWD/new.txt"; then
  echo 'background protection failed' >&2
  exit 9
fi
if "$HELPER" authorize user-skill --actions patch; then
  echo 'background issued an authorization' >&2
  exit 9
fi
exit 0
EOF
chmod +x "$TMP/fakebin/claude"

cat > "$SKILLS/user-skill/SKILL.md" <<'EOF'
---
name: user-skill
description: User maintained test skill.
---
# User skill
Original.
EOF
cat > "$SKILLS/codex-learned/SKILL.md" <<'EOF'
---
name: codex-learned
description: Skill originally created by the hermes-codex background reviewer.
---
# Codex learned
Verified procedure inherited from the codex adapter.
EOF

# 事前条件: hermes-codex adapter が同居し、canonical skills root と ownership を持つ
cat > "$TMP/home/.config/hermes-codex/config.json" <<EOF
{"version": 1, "skills_root": "$SKILLS"}
EOF
cat > "$TMP/home/.local/state/hermes-codex/registry.json" <<'EOF'
{"version": 1, "authorizations": {}, "skills": {
  "codex-learned": {"owner": "agent", "protected": false, "created_by": "background"},
  "user-skill": {"owner": "user", "protected": true, "created_by": "user"}
}}
EOF

# asdf等のshimはHOME差し替えで壊れるため、実pythonのbinを先に固定する
PYBIN="$(dirname "$(python3 -c 'import sys; print(sys.executable)')")"
export HOME="$TMP/home"
export PATH="$TMP/fakebin:$PYBIN:$PATH"
HELPER="$ROOT/bin/hermes-claude-skill"
REVIEW="$ROOT/bin/hermes-claude-review"

# 正常系: SessionStart hookが config/registry を初期化しガイダンスを注入する
GUIDANCE="$(printf '{}\n' | python3 "$ROOT/hooks/session_start.py")"
grep -q 'Guarded Claude Code adapter' <<<"$GUIDANCE"
grep -q 'skill_manage' <<<"$GUIDANCE"
CONFIG="$TMP/home/.claude/hermes-self-improvement/config.json"
[[ -f "$CONFIG" ]]

# 正常系: 初回configはhermes-codexのcanonical skills rootと共有registryを引き継ぐ
[[ "$(python3 -c "import json; print(json.load(open('$CONFIG'))['skills_root'])")" == "$SKILLS" ]]
SHARED_REGISTRY="$(python3 -c "import os; print(os.path.realpath('$TMP/home/.local/state/hermes-codex/registry.json'))")"
[[ "$(python3 -c "import json; print(json.load(open('$CONFIG'))['registry_path'])")" == "$SHARED_REGISTRY" ]]

# 正常系: codex版registryでagent-ownedのskillはclaude版でもagent-owned(共有registryを直接参照)
[[ "$("$HELPER" owner codex-learned | python3 -c 'import json,sys; print(json.load(sys.stdin)["owner"])')" == agent ]]
[[ "$("$HELPER" owner user-skill | python3 -c 'import json,sys; print(json.load(sys.stdin)["owner"])')" == user ]]

"$HELPER" doctor >/dev/null

# 正常系: 引き継いだagent-owned skillは自律patchできる
printf 'Verified procedure inherited from the codex adapter.\n' > "$TMP/cold.txt"
printf 'Verified procedure inherited from the codex adapter, kept current.\n' > "$TMP/cnew.txt"
"$HELPER" patch codex-learned --old-file "$TMP/cold.txt" --new-file "$TMP/cnew.txt" >/dev/null

# 正常系: agent-ownedスキルの自律作成とpatch
cat > "$TMP/agent.md" <<'EOF'
---
name: learned-test
description: Reusable verified test procedure.
---
# Learned test
Run the verified command.
EOF
"$HELPER" create learned-test --content-file "$TMP/agent.md" >/dev/null
printf 'Run the verified command.\n' > "$TMP/old.txt"
printf 'Run the verified command and check its exit code.\n' > "$TMP/new.txt"
"$HELPER" patch learned-test --old-file "$TMP/old.txt" --new-file "$TMP/new.txt" >/dev/null

# 異常系: user-ownedスキルは自律変更できない
if "$HELPER" patch user-skill --old-file "$TMP/old.txt" --new-file "$TMP/new.txt" >/dev/null 2>&1; then
  echo "user-owned skill changed without authorization" >&2
  exit 1
fi

# 正常系: 明示依頼による保護スキル更新はCLI警告と一回限りtokenを経由する
printf 'Original.\n' > "$TMP/uold.txt"
printf 'Authorized update.\n' > "$TMP/unew.txt"
AUTH_WARNING="$TMP/authorize-warning.txt"
TOKEN="$("$HELPER" authorize user-skill --actions patch 2>"$AUTH_WARNING")"
grep -q 'USER APPROVAL REQUIRED' "$AUTH_WARNING"
"$HELPER" patch user-skill --old-file "$TMP/uold.txt" --new-file "$TMP/unew.txt" --authorization "$TOKEN" >/dev/null
grep -q 'Authorized update' "$SKILLS/user-skill/SKILL.md"

# 異常系: background actorは保護スキルのauthorizationを発行できない
if HERMES_CLAUDE_ACTOR=background "$HELPER" authorize user-skill --actions patch >/dev/null 2>&1; then
  echo "background issued an authorization (direct)" >&2
  exit 1
fi

# 正常系: PreToolUse hookはガード済みhelperコマンドのみ自動許可する
DECISION="$(printf '{"tool_name":"Bash","tool_input":{"command":"%s list"}}\n' "$HELPER" | python3 "$ROOT/hooks/pre_tool_use.py")"
grep -q '"permissionDecision": "allow"' <<<"$DECISION"
NO_DECISION="$(printf '{"tool_name":"Bash","tool_input":{"command":"%s authorize user-skill --actions patch"}}\n' "$HELPER" | python3 "$ROOT/hooks/pre_tool_use.py")"
[[ -z "$NO_DECISION" ]]
NO_DECISION="$(printf '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}\n' | python3 "$ROOT/hooks/pre_tool_use.py")"
[[ -z "$NO_DECISION" ]]

# 正常系: Stop hookがNターンごとに背景レビューを起動し、通知が次のUserPromptSubmitで届く
"$REVIEW" config --interval 1 >/dev/null
cat > "$TMP/transcript.jsonl" <<'EOF'
{"role":"user","content":"The agent completed and verified a reusable workflow. Save it only if justified."}
EOF
printf '%s\n' "{\"session_id\":\"smoke-session\",\"transcript_path\":\"$TMP/transcript.jsonl\",\"cwd\":\"$TMP/work\",\"hook_event_name\":\"Stop\",\"stop_hook_active\":false}" \
  | python3 "$ROOT/hooks/stop.py" >/dev/null
NOTIFY="$TMP/home/.claude/hermes-self-improvement/notifications.jsonl"
for _ in $(seq 1 100); do
  [[ -s "$NOTIFY" ]] && break
  sleep 0.1
done
[[ -d "$SKILLS/background-learned" ]]
[[ "$("$HELPER" owner background-learned | python3 -c 'import json,sys; print(json.load(sys.stdin)["owner"])')" == agent ]]
NOTICE="$(printf '%s\n' '{"session_id":"smoke-session","prompt":"next","cwd":"/tmp","hook_event_name":"UserPromptSubmit"}' \
  | python3 "$ROOT/hooks/user_prompt_submit.py")"
grep -q 'Background skill review applied' <<<"$NOTICE"

# 正常系: 背景実行ガード下ではStop/SessionStartは即時no-op
printf '{}\n' | HERMES_CLAUDE_BACKGROUND=1 python3 "$ROOT/hooks/stop.py" | grep -q '"continue": true'
[[ -z "$(printf '{}\n' | HERMES_CLAUDE_BACKGROUND=1 python3 "$ROOT/hooks/session_start.py")" ]]

python3 - <<PY
import json
from pathlib import Path
home = Path('$TMP/home')
# 正常系: ownershipは共有registryに書かれ、claude側で作ったskillもcodex側から同じownerで見える
r = json.loads(Path('$SHARED_REGISTRY').read_text())
assert r['skills']['user-skill']['owner'] == 'user'
assert r['skills']['learned-test']['owner'] == 'agent'
assert r['skills']['background-learned']['owner'] == 'agent'
assert r['skills']['codex-learned']['owner'] == 'agent'
assert not (home / '.claude/hermes-self-improvement/registry.json').exists()
audit = (home / '.claude/hermes-self-improvement/audit.jsonl').read_text()
assert '"denied"' in audit and '"applied"' in audit
hooks = json.loads(Path('$ROOT/hooks/hooks.json').read_text())['hooks']
assert set(hooks) == {'SessionStart', 'Stop', 'UserPromptSubmit', 'PreToolUse'}
print('smoke test passed')
PY
