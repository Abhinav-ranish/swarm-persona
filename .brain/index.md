# swarm-persona brain: routing index

Repo rules live in `AGENTS.md`. This is the map of what else exists and when to load it. Facts are
dated leads, not ground truth: current code and a real run win.

## Start here

```bash
.brain/bin/brain context              # facts, docs and tests for the files this branch changed
.brain/bin/brain recall "guard self-test"
.brain/bin/brain doctor               # also run by tests/test_brain.py on CodeBuild
```

## What is here, and when to load it

| Path | What it is | Load it when |
|---|---|---|
| `facts/gotchas.json` | Traps the code cannot show (the annotations bug, Python 3.12, fingerprints as a public contract, CodeBuild skips). | Before editing anything: `brain context` surfaces the ones anchored to your files. |
| `facts/decisions.json` | One anchored pointer per ADR, so `brain context` finds the rationale from a file. | Changing guard, judge, selection, posting, tickets, fixes or MCP behaviour. |
| `facts/conventions.json` | Writing and design rules (no em dashes, fail closed, evidence is the replay contract, commit style). | Writing code, docs or commits. |
| `facts/notes.json` | Context worth keeping (the downstream app pins a SHA, live guard tests need mitmdump). | Releasing, or reading test results. |
| `people/<handle>.json` | One file per person: git identities and standing preferences (`brain pref`). Created on first `brain pref add`; each person edits only their own. It stores your git email, which is already public in commits. | Start of a session: `brain pref list`. |
| `release.md` | Release runbook: branch, push-gate review, CodeBuild tests, fast-forward, tag by full ref, downstream sync. | Before any push to `main` or any tag. |
| `adr/0001-network-write-guard.md` | Why the write guard is a per-browser mitmproxy, not page JavaScript. | Touching `src/swarm_persona/guard/` or the agent's browser setup. |
| `adr/0002-deterministic-judge.md` | Why severity and the gate are rules over evidence, with corroboration. | Touching `judge.py`, `merge.py` or severity. |
| `adr/0003-diff-aware-journey-selection.md` | Why ownership globs pick journeys and the model can only add. | Touching `journeys.py` or `planner.py`. |
| `adr/0004-screenshots-in-images-repo.md` | Why screenshots are committed and linked by commit SHA. | Touching `publish` or `upload_images`. |
| `adr/0005-github-app-posting.md` | Posting as a GitHub App, openssl-signed. | Touching `github.py` auth. |
| `adr/0006-stable-fingerprints.md` | Why fingerprints use issue keys only, and what changing them costs. | Touching fingerprints, `normalize`, `tickets.py`. |
| `adr/0007-tests-on-codebuild.md` | Why tests run on CodeBuild and the repo has no workflow. | Touching `deploy/aws/test.sh` or adding CI. |
| `adr/0008-possible-fixes-opt-in.md` | Why possible fixes are off by default, and their limits. | Touching `fixes.py`. |
| `adr/0009-mcp-server-safety.md` | The MCP server's allowlist, auth.log, run cap and kill path. | Touching `mcp_server.py`. |
| `bin/brain` | The CLI (stdlib Python 3.9+): `recall`, `context`, `show`, `remember`, `pref`, `whoami`, `doctor`. | Always. |

## Open decisions

`brain doctor` and `tests/test_brain.py` fail on any file under `plans/` or `adr/` that this index
does not name in backticks, and on any listed doc that no longer exists. Delete a plan, and its
line here, when the work lands or is dropped.

- `plans/v020-review-nits.md`: six small fixes from the final v0.2.0 review (secret filter, ticket
  body cap, auth.log message, corrupt run.json, fsync, merged classification).
- `plans/codebuild-lockfile.md`: constraints file or committed lock for CodeBuild installs.
- `plans/chromium-background-noise.md`: the ~50 Google requests per run the guard refuses.
- `plans/outcome-real-model-check.md`: confirm the structured Outcome on real models.
- `plans/unbuilt-ideas.md`: analytics-seeded personas, screenshot-only mode, A/B across two URLs,
  Slack notify.
- Large PRs: `gh pr view --json files` returns at most 100 files, so journey selection can miss
  journeys on a bigger PR (see `adr/0003-diff-aware-journey-selection.md`). Undecided: paginate the
  REST files API, or select every journey when the list may be truncated.

## Adding to the brain

- A fact is a trap the code cannot show, a decision's pointer, or a rule. Never a restatement of
  code or a docstring. `brain remember gotcha "<text>" --paths src/x.py --source <commit>`.
- A fact whose anchored file is renamed or deleted fails `brain doctor`: update its paths or drop it.
- A decision with alternatives worth remembering gets an ADR in `adr/` and a line in the table above.
