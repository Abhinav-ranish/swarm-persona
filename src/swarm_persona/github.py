"""One sticky PR comment, found by its hidden marker and edited in place on every run, plus the
Actions step summary. Uses the gh CLI, so it works wherever gh is signed in or GH_TOKEN is set."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

from .report import MARKER


def gh(*args: str, body: str | None = None) -> str:
    return subprocess.run(['gh', *args], input=body, capture_output=True, text=True, check=True).stdout


def repo_name() -> str:
    return os.environ.get('GITHUB_REPOSITORY') or json.loads(gh('repo', 'view', '--json', 'nameWithOwner'))['nameWithOwner']


def own_login() -> str:
    """Whose comment to edit: anyone can paste the marker, so it must also be ours. A GITHUB_TOKEN
    cannot read /user, and its comments are github-actions[bot]'s."""
    if os.environ.get('SWARM_PERSONA_COMMENT_AUTHOR'):
        return os.environ['SWARM_PERSONA_COMMENT_AUTHOR']
    try:
        return json.loads(gh('api', 'user'))['login']
    except (subprocess.CalledProcessError, KeyError, ValueError):
        return 'github-actions[bot]'


def find_comment(repo: str, pr: str, login: str) -> int | None:
    pages = gh('api', '--paginate', '--slurp', f'repos/{repo}/issues/{pr}/comments')
    for page in json.loads(pages):
        for c in page:
            if MARKER in (c.get('body') or '') and (c.get('user') or {}).get('login') == login:
                return c['id']
    return None


def upsert_comment(pr: str, body: str, repo: str | None = None) -> str:
    """Edit the existing swarm-persona comment, or post one. Returns its URL."""
    repo = repo or repo_name()
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
        json.dump({'body': body}, fh)
    try:
        existing = find_comment(repo, pr, own_login())
        if existing:
            out = gh('api', '-X', 'PATCH', f'repos/{repo}/issues/comments/{existing}', '--input', fh.name)
        else:
            out = gh('api', '-X', 'POST', f'repos/{repo}/issues/{pr}/comments', '--input', fh.name)
    finally:
        os.unlink(fh.name)
    return json.loads(out).get('html_url', '')


def step_summary(body: str) -> None:
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a') as fh:
            fh.write(body + '\n')
