# hermes-self-improvement-claude-plugin

[Hermes Agent](https://github.com/NousResearch/hermes-agent) の Skill 自己改善ループを Claude Code に移植した **Claude Code plugin**。[hermes-self-improvement-codex-adapter](https://github.com/lilpacy/hermes-self-improvement-codex-adapter) の Claude Code 版です。

Hermes の memory / user modeling / session search は対象外。Skill の自己改善ループのみを忠実に移植します。

## 2つの学習ループ

| ループ | 契機 | 仕組み |
|---|---|---|
| フォアグラウンド学習 | 会話中の学び(非自明なタスク完了、エラー回復、ユーザー訂正、再利用可能な手順の発見) | `SessionStart` hook が毎セッション Hermes の `SKILLS_GUIDANCE` を context に注入し、Claude 自身がガード付き helper 経由で agent-owned skill を即時作成・patch |
| バックグラウンド学習 | Nターン完了ごと(デフォルト10) | `Stop` hook が transcript をコピーし、隔離された `claude -p --bare` プロセスに Hermes の `_SKILL_REVIEW_PROMPT` を実行させて取りこぼしを回収。結果は次の `UserPromptSubmit` で通知 |

## Ownership モデル(安全設計の中核)

| 由来 | owner | 挙動 |
|---|---|---|
| 既存 / 未登録の skill | `user` | 自律更新は helper が拒否 |
| helper `create` で作成 | `agent` | 自律更新を許可 |
| symlink された skill | `external` | 常に読み取り専用 |

- user-owned skill の変更は、ユーザーがそのターンで明示依頼した場合のみ `authorize` の一回限り token(skill・action 限定、TTL デフォルト600秒)経由で可能。
- `adopt` / `release` で ownership を移動(明示依頼時のみ)。background reviewer は `authorize` / `adopt` / `release` / `create-user` を一切実行できない(helper 内で強制)。

## Codex 版との対応

| Codex adapter | この plugin |
|---|---|
| `~/.codex/hooks.json` への merge | `hooks/hooks.json` を同梱(plugin 機構が自動登録) |
| `AGENTS.md` へのガイダンス追記 | `SessionStart` hook の stdout 注入(ファイル改変なし) |
| `~/.codex/rules/*.rules` | `PreToolUse` hook が helper のガード済み subcommand のみ自動許可 |
| `codex exec --ephemeral --sandbox workspace-write` | `claude -p --bare --permission-mode dontAsk --allowedTools "Read,Write,Bash(<helper> *)"` |
| install 時の prompt vendor | ビルド時に vendor 済み(`vendor/hermes/`、commit は `UPSTREAM.json` に pin) |
| `~/.config` + `~/.local/state` | `~/.claude/hermes-self-improvement/` に統合 |
| skills root `~/.agents/skills` | `~/.claude/skills`(Claude Code が native に読む) |

`--bare` により背景レビューは hooks/plugins を読み込まないため、Stop hook の再帰は構造的に発生しません(`HERMES_CLAUDE_BACKGROUND=1` ガードも保険で残置)。

## インストール

```text
/plugin marketplace add lilpacy/hermes-self-improvement-claude-plugin
/plugin install hermes-self-improvement@lilpacy
```

開発中のローカル試験:

```bash
claude --plugin-dir /path/to/hermes-self-improvement-claude-plugin
```

初回セッション開始時に `SessionStart` hook が config と ownership registry を初期化し、既存の `~/.claude/skills` 配下を user-owned(symlink は external)として登録します。

## CLI

```bash
bin/hermes-claude-skill list|view|create|patch|edit|write-file|remove-file|delete|authorize|adopt|release|owner|audit|doctor
bin/hermes-claude-review status|run|logs|config|upstream-update
```

背景レビューの設定:

```bash
bin/hermes-claude-review config --interval 5 --model haiku --timeout 600
bin/hermes-claude-review config --disable
```

## ファイル配置

| パス | 内容 |
|---|---|
| `~/.claude/skills/` | skill 本体(既存 = user-owned、自動作成 = agent-owned) |
| `~/.claude/hermes-self-improvement/` | config.json / registry.json / audit.jsonl / history / runtime logs / notifications |
| `vendor/hermes/` | 抽出済み Hermes prompts + `UPSTREAM.json`(pinned commit + SHA-256)+ upstream LICENSE |

## 保護レイヤ

- **強制(helper 内)**: ownership registry 検査、一回限り token 検証、background actor 制限、skill バリデーション(frontmatter / サイズ / secret パターン)、変更前バージョンの history 保存、JSONL audit log。
- **助言のみ**: SessionStart 注入ガイダンス、`USER APPROVAL REQUIRED` の stderr 警告。
- **非保護**: helper を迂回した直接ファイル書き込み。Codex 版と同じく、脅威モデルはエージェントがガイダンスに協力することを前提とします。ただし skills root が git 管理下(dotfiles symlink 等)なら全自律変更を `git diff` で監査できます。

## テスト

```bash
tests/smoke-test.sh   # fake claude で foreground/background/ownership/hook 配線を一括検証
```

## アンインストール

```text
/plugin uninstall hermes-self-improvement@lilpacy
```

skill 本体(`~/.claude/skills`)には触れません。状態も残したくない場合は `rm -rf ~/.claude/hermes-self-improvement` を追加実行してください。

## License / Notices

`vendor/hermes/` は upstream ライセンスに従います。詳細は `THIRD_PARTY_NOTICES.md`。
