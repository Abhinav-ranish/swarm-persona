# A lockfile or constraints file for CodeBuild installs

Status: open.

`deploy/aws/buildspec.yml` and `deploy/aws/test.sh` pin swarm-persona (by SHA), uv, mitmproxy and
browser-use, but every other dependency resolves fresh on each build: playwright, pydantic, boto3,
mcp and browser-use's own tree. A new upstream release can break or change a run with no change
here, and an unpinned tree is a supply-chain exposure in a job that holds model credentials and a
signed-in test session.

Options:

- A `constraints.txt` generated with `uv pip compile` for Python 3.12 on Linux, passed to
  `uv tool install --constraints` in both scripts. Smallest change; keeps `uv.lock` out of git
  (it is ignored on purpose, aa50085).
- Commit `uv.lock` and install with `uv sync --frozen` for tests. Locks dev too, but changes the
  ignore rule and the hygiene test that expects `uv.lock` ignored.

Recommendation: constraints file, refreshed in the release runbook. Done when both scripts install
with it and a test proves the buildspec passes it.
