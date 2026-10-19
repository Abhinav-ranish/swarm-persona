"""GitHub: one sticky PR comment, screenshots GitHub can show, and the Actions step summary.

Comments are posted with a GitHub App installation token when an app is configured (the comment
then comes from <app>[bot]), otherwise with the gh CLI's token or GH_TOKEN. Screenshots are
committed, in one commit per run, to a repository you choose (images_repo) and linked by commit, so
a later run never changes an earlier comment's pictures. On a private images repository, only people
who can read it see them.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .report import MARKER

API = 'https://api.github.com'


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class Client:
    def __init__(self, token: str, login: str | None = None) -> None:
        self.token, self._login = token, login

    @classmethod
    def from_cli(cls) -> 'Client':
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if not token:
            token = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, check=True).stdout.strip()
        return cls(token)

    @classmethod
    def from_app(cls, app: dict, repo: str, permissions: dict | None = None) -> 'Client':
        """An installation token for one repository, minted from {app_id, installation_id, private_key}.
        Signed with the openssl CLI so the package needs no crypto dependency."""
        now = int(time.time())
        unsigned = (b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode()) + '.'
                    + b64url(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': str(app['app_id'])}).encode()))
        fd, key = tempfile.mkstemp(prefix='swarm-persona-app-')
        try:
            os.write(fd, app['private_key'].encode())
            os.close(fd)
            signature = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', key], input=unsigned.encode(),
                                       capture_output=True, check=True).stdout
        finally:
            os.unlink(key)
        jwt = Client(f'{unsigned}.{b64url(signature)}')
        slug = jwt.api('GET', '/app')['slug']
        minted = jwt.api('POST', f"/app/installations/{app['installation_id']}/access_tokens",
                         {'repositories': [repo.split('/', 1)[1]],
                          'permissions': permissions or {'pull_requests': 'write'}})
        return cls(minted['token'], f'{slug}[bot]')

    def api(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header('Accept', 'application/vnd.github+json')
        req.add_header('X-GitHub-Api-Version', '2022-11-28')
        req.add_header('Authorization', f'Bearer {self.token}')
        if body is not None:
            req.add_header('Content-Type', 'application/json')
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                    return json.loads(data) if data else {}
            except urllib.error.HTTPError as error:
                if error.code < 500 or attempt == 2:
                    raise RuntimeError(f'GitHub {method} {path}: HTTP {error.code} {error.read()[:200]!r}') from None
            time.sleep(2 * (attempt + 1))

    def login(self) -> str:
        """Whose comment to edit: anyone can paste the marker, so it must also be ours."""
        if self._login is None:
            try:
                self._login = self.api('GET', '/user')['login']
            except RuntimeError:  # a GITHUB_TOKEN cannot read /user; its comments are the Actions bot's
                self._login = 'github-actions[bot]'
        return self._login


def repo_name() -> str:
    return os.environ.get('GITHUB_REPOSITORY') or subprocess.run(
        ['gh', 'repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner'],
        capture_output=True, text=True, check=True).stdout.strip()


def upsert_comment(client: Client, repo: str, pr: str, body: str) -> str:
    """Edit our existing swarm-persona comment, or post one. Returns its URL."""
    login = client.login()
    page = 1
    while True:
        comments = client.api('GET', f'/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}')
        for c in comments:
            if MARKER in (c.get('body') or '') and (c.get('user') or {}).get('login') == login:
                return client.api('PATCH', f"/repos/{repo}/issues/comments/{c['id']}", {'body': body})['html_url']
        if len(comments) < 100:
            break
        page += 1
    return client.api('POST', f'/repos/{repo}/issues/{pr}/comments', {'body': body})['html_url']


def upload_images(client: Client, repo: str, files: list[Path], folder: str) -> dict[str, str]:
    """Commit the files under <folder>/ on the repository's default branch in one commit, and return
    {file name: URL} pinned to that commit."""
    if not files:
        return {}
    branch = client.api('GET', f'/repos/{repo}')['default_branch']
    blobs = [{'path': f'{folder}/{f.name}', 'mode': '100644', 'type': 'blob',
              'sha': client.api('POST', f'/repos/{repo}/git/blobs',
                                {'content': base64.b64encode(f.read_bytes()).decode(), 'encoding': 'base64'})['sha']}
             for f in files]
    for attempt in range(3):  # another run may move the branch between our read and our update
        head = client.api('GET', f'/repos/{repo}/git/ref/heads/{branch}')['object']['sha']
        base_tree = client.api('GET', f'/repos/{repo}/git/commits/{head}')['tree']['sha']
        tree = client.api('POST', f'/repos/{repo}/git/trees', {'base_tree': base_tree, 'tree': blobs})['sha']
        commit = client.api('POST', f'/repos/{repo}/git/commits',
                            {'message': f'swarm-persona screenshots: {folder}', 'tree': tree, 'parents': [head]})['sha']
        try:
            client.api('PATCH', f'/repos/{repo}/git/refs/heads/{branch}', {'sha': commit})
            break
        except RuntimeError:
            if attempt == 2:
                raise
    return {f.name: f'https://github.com/{repo}/blob/{commit}/{folder}/{f.name}?raw=true' for f in files}


def step_summary(body: str) -> None:
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a') as fh:
            fh.write(body + '\n')
