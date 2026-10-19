# Unbuilt ideas

Status: parked. None is promised; each needs a design note before code.

- **Analytics-seeded personas.** Cast personas and journeys from real usage (top paths, devices,
  drop-off points) exported from the app's analytics, instead of only `personas.audience`. Must
  take an export file, never live credentials, and keep journeys config-owned.
- **Screenshot-only mode.** Entry checks and screenshots per journey and device with no agent and no
  model: a cheap visual diff for every PR. Most of it exists in `src/swarm_persona/checks.py`.
- **A/B across two URLs.** Run the same personas against two arbitrary URLs and compare, reusing
  the baseline machinery in `src/swarm_persona/judge.py` with neither side treated as production.
- **Slack notify.** Post the summary line and gate result to a Slack webhook after `publish`.
  Needs the same defanging as `report.safe` and a webhook allowlist.
