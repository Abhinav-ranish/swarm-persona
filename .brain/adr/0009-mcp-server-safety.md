# 0009: MCP server safety

Status: accepted (v0.2.0, d44babb, d88ddf4, 93f8ced)

## Context

`swarm-persona mcp` lets an agent client start real runs: browsers signed in to an app, model
spend, an auth command. An MCP client is driven by a model that reads untrusted text, so its tool
calls must be treated as untrusted input.

## Decision

`src/swarm_persona/mcp_server.py` wraps the CLI rather than calling the run in-process, so the guard
self-test, preflight, budgets and sign-out behave exactly as on the command line. On top:

- `SWARM_PERSONA_CONFIGS`: when set, only those config files may be used.
- Config errors are reported generically; parse errors quote the file and are never echoed.
- An ad-hoc goal passes the forbidden-goal filter.
- The auth command's output goes to a 0600 `auth.log` (`SWARM_PERSONA_AUTH_LOG`) that no tool returns.
- At most two runs at a time, checked under an `fcntl` lock on `~/.swarm-persona/runs/.lock`.
- Each run is its own session; `close` sends SIGTERM to the process group, and the next call after
  150 seconds sends SIGKILL. `run.json` is written atomically; corrupt run directories are skipped.

## Consequences

- `fcntl` makes the server POSIX-only.
- Open nits: corrupt `run.json` on status and close, no fsync, the auth.log error message
  (`plans/v020-review-nits.md`).
