# Chromium background noise

Status: open.

Each run, Chromium still sends about 50 requests to Google hosts (component updates, safe
browsing, optimisation hints, sign-in) despite the `QUIET` flags in
`src/swarm_persona/agent.py:42`. The guard refuses them, so nothing leaks, but they fill the guard
logs and the "other sites" count in the report, which hides real cross-site refusals.

Next steps:

1. Capture the refused hosts and paths from one run's `guard/*.guard.jsonl`.
2. Try the flags and policies that cover the remaining sources (for example
   `--disable-features=OptimizationHints,MediaRouter,...`, `--disable-field-trial-config`, and an
   enterprise policy file), checking each against a fresh count.
3. If some remain, report them as a separate known-browser-noise count rather than mixing them
   with refusals caused by the app.

Done when a run shows under 5 browser-originated refusals, or the report separates them.
