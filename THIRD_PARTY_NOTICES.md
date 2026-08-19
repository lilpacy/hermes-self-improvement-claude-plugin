# Third-party notices

This repository bundles selected prompt constants (`SKILLS_GUIDANCE`, `_SKILL_REVIEW_PROMPT`, `_COMBINED_REVIEW_PROMPT`) extracted by AST parsing from `NousResearch/hermes-agent`, pinned to the commit recorded in:

```text
vendor/hermes/UPSTREAM.json
```

The bundled Hermes material is accompanied by its upstream license:

```text
vendor/hermes/LICENSE.hermes-agent
```

`prompts/*.fallback.txt` are original minimal compatibility fallbacks intended only for offline smoke testing; they contain no Hermes text.

Update the vendored material with:

```bash
bin/hermes-claude-review upstream-update <commit-sha>
```
