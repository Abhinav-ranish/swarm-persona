# 0001: The write guard sits at the network layer, not in the page

Status: accepted (v0.1.0, bf3b558)

## Context

Persona agents click around a signed-in app and must never publish, send, pay or delete. An
in-page guard (patched `fetch`, injected JavaScript) only sees what the page's own scripts do in
that frame: service workers, beacons, cross-site frames, plain form posts and new tabs go around it,
and the page can undo the patch.

## Decision

Every browser (each persona's and the checker's) talks only to its own `mitmdump` running
`src/swarm_persona/guard/addon.py`. The policy is a pure function in
`src/swarm_persona/guard/policy.py`, decided per request and failing closed: https only, no internal
addresses, other sites read-only and never as a page or frame, app writes only on the allowlist with
the body judged as the app will parse it, upgrades refused. `src/swarm_persona/guard/proxy.py`
refuses to hand out a proxy until it has refused its self-test probes.

## Consequences

- The prompt guardrails in `src/swarm_persona/agent.py` are a courtesy; the guard is the safety.
- The proxy re-signs TLS, so browsers run with `--ignore-certificate-errors` against the proxy only;
  mitmdump still verifies upstream certificates.
- mitmdump runs from its own tool environment, so `addon.py` and `policy.py` import only the
  standard library (and mitmproxy).
- Every new rule needs tests for encoded and malformed variants (`tests/test_policy.py`).
