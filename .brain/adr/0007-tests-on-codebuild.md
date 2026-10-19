# 0007: Tests run on AWS CodeBuild, not GitHub Actions

Status: accepted (v0.2.0, e0f4f97, 3f63814, 357beaa)

## Context

The owner keeps model access and runners in AWS, the live guard tests need mitmdump and the
network, and a GitHub Actions workflow in a public repository adds a supply-chain surface the
project does not need.

## Decision

The repository has no workflow of its own. `deploy/aws/test.sh` zips the working tree (tracked and
untracked, ignored files excluded, symlinks stored as links), uploads it, runs pytest on Python 3.12
with mitmproxy installed on the CodeBuild project from `deploy/aws/setup.sh`, and exits with pytest's
result. It prints the AWS account before uploading and refuses a mismatch with `SP_EXPECT_ACCOUNT`.

## Consequences

- Uncommitted work is tested exactly as it is, which is what the push gate needs.
- The zip has no `.git`, so the gitignore hygiene tests skip there.
- `action.yml` and `examples/workflow.yml` are for users of swarm-persona, not CI for this repo.
- Installs are not locked yet (`plans/codebuild-lockfile.md`).
