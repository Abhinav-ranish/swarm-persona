import json
import shutil
from pathlib import Path

from swarm_persona import github
from swarm_persona.report import MARKER

FIXTURE = Path(__file__).parent / 'fixtures' / 'run' / 'evidence.json'


class Fake(github.Client):
    """Records calls; answers like the GitHub API for the routes publish touches."""

    def __init__(self, login='me', comments=()):
        super().__init__('t', login)
        self.calls, self.comments = [], list(comments)

    def api(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith('/comments?per_page=100&page=1'):
            return self.comments
        if method in ('PATCH', 'POST') and 'comments' in path:
            return {'html_url': f'https://github.com/x/{method.lower()}'}
        if path == '/repos/o/shots':
            return {'default_branch': 'main'}
        if path.endswith('/git/blobs'):
            return {'sha': f"blob-{len(self.calls)}"}
        if '/git/ref/heads/' in path:
            return {'object': {'sha': 'head'}}
        if '/git/commits/head' in path:
            return {'tree': {'sha': 'tree0'}}
        if path.endswith('/git/trees'):
            return {'sha': 'tree1'}
        if path.endswith('/git/commits'):
            return {'sha': 'c0ffee'}
        return {}


def test_only_our_own_marked_comment_is_edited():
    fake = Fake(comments=[{'id': 1, 'body': MARKER + ' fake', 'user': {'login': 'mallory'}},
                          {'id': 2, 'body': MARKER, 'user': {'login': 'me'}}])
    assert github.upsert_comment(fake, 'o/r', '5', 'body').endswith('/patch')
    assert ('PATCH', '/repos/o/r/issues/comments/2', {'body': 'body'}) in fake.calls
    fresh = Fake(comments=[{'id': 1, 'body': MARKER, 'user': {'login': 'mallory'}}])
    assert github.upsert_comment(fresh, 'o/r', '5', 'body').endswith('/post')


def test_screenshots_go_up_in_one_commit_and_link_to_it(tmp_path):
    files = []
    for name in ('a.png', 'b.png'):
        (tmp_path / name).write_bytes(b'png')
        files.append(tmp_path / name)
    fake = Fake()
    urls = github.upload_images(fake, 'o/shots', files, 'o-r/pr-5/run1')
    assert urls == {n: f'https://github.com/o/shots/blob/c0ffee/o-r/pr-5/run1/{n}?raw=true' for n in ('a.png', 'b.png')}
    assert sum(1 for m, p, _ in fake.calls if p.endswith('/git/commits') and m == 'POST') == 1
    assert ('PATCH', '/repos/o/shots/git/refs/heads/main', {'sha': 'c0ffee'}) in fake.calls


def test_publish_embeds_uploaded_screenshots_in_the_comment(tmp_path, monkeypatch):
    from swarm_persona import run as run_mod

    shutil.copy(FIXTURE, tmp_path / 'evidence.json')
    (tmp_path / 'shots').mkdir()
    for name in ('preview-1-1.png', 'unrelated.png'):
        (tmp_path / 'shots' / name).write_bytes(b'png')
    fake = Fake()
    monkeypatch.setattr(github.Client, 'from_cli', classmethod(lambda cls: fake))
    url = run_mod.publish(tmp_path, '7', repo='o/r', images_repo='o/shots')
    assert url.endswith('/post')
    body = [b for m, p, b in fake.calls if p == '/repos/o/r/issues/7/comments' and m == 'POST'][0]['body']
    assert '<img src="https://github.com/o/shots/blob/c0ffee/o-r/pr-7/' in body and 'preview-1-1.png' in body
    uploaded = [b['tree'] for m, p, b in fake.calls if p.endswith('/git/trees')][0]
    assert [t['path'].rsplit('/', 1)[1] for t in uploaded] == ['preview-1-1.png']  # only referenced shots
    assert json.loads((tmp_path / 'report.json').read_text())['findings']


def test_without_an_images_repo_screenshots_are_named_not_shown(tmp_path, monkeypatch):
    from swarm_persona import run as run_mod

    shutil.copy(FIXTURE, tmp_path / 'evidence.json')
    fake = Fake()
    monkeypatch.setattr(github.Client, 'from_cli', classmethod(lambda cls: fake))
    run_mod.publish(tmp_path, '7', repo='o/r')
    body = [b for m, p, b in fake.calls if m == 'POST'][0]['body']
    assert '<img' not in body and 'Screenshots: preview-1-1.png' in body
    assert not any('/git/' in p for _, p, _ in fake.calls)
