# 0005: Comments and issues can be posted as a GitHub App

Status: accepted (v0.1.4, 5661e3d; issues 4cabcd6)

## Context

Posting with a person's token makes every report look like that person wrote it, and a
`GITHUB_TOKEN` cannot be used from CodeBuild.

## Decision

`Client.from_app` in `src/swarm_persona/github.py` mints an installation token for one repository
from `{app_id, installation_id, private_key}` (CLI `--github-app` or `SWARM_PERSONA_GITHUB_APP`),
with only the permission the call needs (`pull_requests` or `issues`). The JWT is signed by the
`openssl` CLI so the package needs no crypto dependency. The sticky comment is edited only when it
carries the marker and was written by the same login, so a pasted marker cannot hijack it.

## Consequences

- `openssl` must be on PATH wherever an app is used.
- The private key is written to a temporary file for the signing call and deleted right after.
