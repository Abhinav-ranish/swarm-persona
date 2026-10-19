# 0008: Possible fixes are opt-in

Status: accepted (v0.2.0, 8413135, aa50085, 54299c5, 1161a7e)

## Context

Suggesting the change that likely caused a finding needs the PR diff in a model prompt. Diffs can
hold secrets and proprietary code, the answer cannot be checked, and an extra model call must not be
able to break or bloat a run.

## Decision

`src/swarm_persona/fixes.py` runs only when `report.suggest_fixes` is true
(`src/swarm_persona/config.py`). Per run: at most five calls, only for new or unclassified findings
of major or worse, each given only the diff of files its journey owns, truncated to
`fix_diff_chars`. Diff sections whose old or new path, or rename source, has a secret-like segment
are never sent. A call starts only if its prompt and reply cap fit `budget.max_tokens`. Any error
becomes a note; the run never fails because of fixes. The report labels them unverified.

## Consequences

- Off by default means most users never send a diff anywhere.
- The secret-name filter is a heuristic and currently too broad (`plans/v020-review-nits.md`).
