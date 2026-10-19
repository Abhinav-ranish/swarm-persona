# Agent guide: swarm-persona

swarm-persona is an MIT-licensed, open-source AI-persona UX tester: persona agents walk a web app
behind a per-browser network write guard, and a deterministic judge posts a ranked report on the PR.
Durable memory for agents is in `.brain/`: start at `.brain/index.md`, and run
`.brain/bin/brain context` before editing.

## Rules

- No em dashes anywhere: code, comments, docs, commits, PR text.
- Never name other products in this tree. `tests/test_repo_hygiene.py` enforces a list; call the
  consumer of this tool "the downstream app". Never edit the downstream app's repository from here.
- No `from __future__ import annotations` in `src/swarm_persona/agent.py` or `mcp_server.py`.
- The package needs Python 3.12+. `.brain/bin` stays standard-library Python 3.9+.
- Guard and preflight changes fail closed; `judge.py` stays pure (no model, browser or network).
- Changing a finding fingerprint re-files every user's tickets: release note and version bump.
- Match the surrounding code and keep changes scoped. No attribution lines in commits or PRs.
- Before pushing, a fresh subagent reviews the diff (`.brain/release.md`, step 2).
- When you learn a trap the code cannot show, `brain remember` it and commit it with the change.

## Verify

```bash
SP_PROJECT=<project> SP_EXPECT_ACCOUNT=<account id> deploy/aws/test.sh
.brain/bin/brain doctor
```

`deploy/aws/test.sh` runs pytest on CodeBuild (Python 3.12, mitmproxy installed) against the
working tree, uncommitted files included; extra arguments go to pytest. The gitignore hygiene tests
skip there (no `.git` in the zip): run `tests/test_repo_hygiene.py` locally when `.gitignore`
changes. Local runs: `uv run --python 3.12 --extra dev --extra bedrock --extra mcp pytest -q`.

## Release

Branch, fresh-subagent push gate with mutation checks, `deploy/aws/test.sh`, fast-forward `main`,
annotated tag pushed by full ref (`git push origin refs/tags/vX.Y.Z`), never move a tag, then the
downstream sync only with the owner's yes. Full runbook: `.brain/release.md`.
