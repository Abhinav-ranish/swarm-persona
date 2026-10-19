# 0003: Diff-aware journey selection

Status: accepted (v0.1.0, bf3b558)

## Context

Walking every journey on every PR costs model tokens and minutes and buries the signal for the
change under review. Letting a model choose journeys from the diff makes the cost and the coverage
unpredictable, and lets PR text steer what gets tested.

## Decision

`src/swarm_persona/journeys.py` selects from the changed files: a file a journey `owns` selects it,
a `shared` file selects every journey. The model may add journeys for files no glob claimed, never
remove one. A PR that touches no journey runs nothing. Each persona's goal is the journey's goal from
the config, which PR text cannot change (`src/swarm_persona/planner.py`).

## Consequences

- Coverage is only as good as the `owns` globs; `swarm-persona select` shows a selection for free.
- With no journeys configured, the model writes goals, filtered by the forbidden-goal words.
- The file list comes from `gh pr view --json files` (see the open question in index.md about very
  large PRs).
