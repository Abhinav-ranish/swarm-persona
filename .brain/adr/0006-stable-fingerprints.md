# 0006: Fingerprints are built only from issue keys

Status: accepted (v0.2.0, a50c885, replacing 22d0f0f)

## Context

`tickets --github-issues` deduplicates against issues already filed through a hidden marker that
hashes the finding's fingerprint. An agent finding's title is model prose that changes every run, and
22d0f0f still mixed one title into the fingerprint, so the same problem re-filed itself.

## Decision

`agent_fingerprint` in `src/swarm_persona/judge.py` is the sorted set of the sources'
(journey, page, category) from `issue_key`, nothing else. Two findings of one run may share a
fingerprint: `src/swarm_persona/tickets.py` merges them into one ticket, and possible fixes are keyed
per finding by `finding_key` (also in `judge.py`, read by `src/swarm_persona/fixes.py`).

## Consequences

- The fingerprint is a public contract: markers live in other people's issue trackers. Changing it
  re-files every ticket (see the `fingerprint-is-public-contract` fact). 0.2.0 broke 0.1.x markers
  once, with an upgrade note in the README.
- A merged ticket takes its title and classification from its first (most important) finding; see
  `plans/v020-review-nits.md`.
