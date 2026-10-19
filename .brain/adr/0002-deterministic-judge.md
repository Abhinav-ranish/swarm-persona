# 0002: A deterministic judge with corroboration, not an LLM judge

Status: accepted (v0.1.0, bf3b558; per-persona verdicts a87e59e)

## Context

Agents over-report and a model asked "how bad is this" drifts from run to run. A gate that fails
PRs must give the same answer for the same evidence and must not be talked up or down by page text.

## Decision

`src/swarm_persona/judge.py` turns recorded evidence into findings by rules only. Hard signals
(entry page status, uncaught errors, 5xx, unusable critical selectors) carry rule severities. An
agent report keeps the persona's own severity, capped at major unless two personas hit it or a hard
signal fired on the same journey. The one model pass, `src/swarm_persona/merge.py`, only groups and
words reports; its severity is ignored. Classification against the baseline uses fingerprints, not a
model. browser-use's own judge is off (`use_judge=False`).

## Consequences

- `swarm-persona replay` rebuilds the verdict from `evidence.json` with no browser or model.
- The merge model still decides which reports are duplicates, and that is what makes "seen by two
  personas"; the report names every persona so a reader can check it.
- Agent reports match the baseline coarsely (journey, page, category), a documented limit.
