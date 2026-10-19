# Release runbook

A release is a version bump on a branch, a gated fast-forward of `main`, and an annotated tag.
Nothing is published to PyPI; users install from a tag or, better, a full commit SHA.

## 1. Branch

Work on a branch, never on `main`. Its name must never equal a tag name (`v0.2.1` as a branch makes
`git push origin v0.2.1` ambiguous): use `agent/...` or `release-0.2.1`.

Bump the version in both `pyproject.toml` and `src/swarm_persona/__init__.py`, and add a
**X.Y.Z** paragraph to the README "Changes" section. Anything that changes a finding fingerprint, a
config field or a CLI flag gets an upgrade note there.

## 2. Push gate: a fresh subagent review with mutation checks

Before anything is pushed, a fresh subagent with no part in the work reviews the full diff against
`main`. It must:

- read the diff and the files it touches, and report correctness and safety problems with file:line;
- for every fix or guard the diff claims, mutate the code back (revert the line, flip the check) and
  confirm a test fails, then restore it. A claim with no failing test under mutation is not done;
- check the repo rules: no em dashes, no other product names (`tests/test_repo_hygiene.py`), no
  secrets, no `from __future__ import annotations` in `agent.py` or `mcp_server.py`.

Fix what it finds and re-run the review on the new diff until it comes back clean.

## 3. Tests on CodeBuild

```bash
SP_PROJECT=<project> SP_EXPECT_ACCOUNT=<account id> deploy/aws/test.sh
```

The maintainer's project and account are in their session instructions; the script prints the
account before uploading and stops on a mismatch. It tests the working tree as it is, uncommitted
files included. It must end with `Tests SUCCEEDED`. If `.gitignore` changed, also run
`uv run --python 3.12 --extra dev pytest tests/test_repo_hygiene.py` locally (those tests skip on
CodeBuild). Then run `.brain/bin/brain doctor`.

## 4. Fast-forward main

```bash
git fetch origin
git rebase origin/main          # only if main moved; then repeat steps 2 and 3
git push origin HEAD:refs/heads/main
```

`main` only ever fast-forwards: no merge commits, no force pushes. If the push is refused, main
moved: rebase and gate again.

## 5. Annotated tag, pushed by full ref

```bash
git tag -a vX.Y.Z -m "X.Y.Z: one line on what changed" <the main commit>
git push origin refs/tags/vX.Y.Z
```

Never move or delete a pushed tag. Users and the downstream app pin by SHA because tags can move in
general; ours never do. A bad release is fixed by the next patch version.

Docs-only and `.brain/` changes skip the bump and the tag: gate, test, fast-forward.

## 6. Downstream sync

The downstream app runs swarm-persona from its own copy of `deploy/aws/buildspec.yml`. Sessions in
this repository never touch the downstream app's repository. For each release:

1. Ask the owner whether to sync now. Do nothing downstream without that yes.
2. Pin the full SHA of the tag's commit: `git rev-parse vX.Y.Z^{commit}`. Never a tag name.
3. Copy `deploy/aws/buildspec.yml` from that commit into the downstream app.
4. Adapt the downstream app's swarm-persona config to new or changed fields, and validate it with
   `swarm-persona check-config` at the pinned SHA.
5. Run the downstream app's own gates (its tests and its review), from a session in that repository.
