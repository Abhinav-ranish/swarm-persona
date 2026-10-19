import json
import shutil
from pathlib import Path

import pytest

from swarm_persona import github
from swarm_persona.tickets import MARKER, build, file_issues, issue_body, ticket_id

FIXTURE = Path(__file__).parent / 'fixtures' / 'run' / 'evidence.json'


class Fake(github.Client):
    """Answers like the GitHub issues API: open issues are listed back, created ones are kept."""

    def __init__(self, issues=(), login='me'):
        super().__init__('t', login)
        self.issues, self.calls = list(issues), []

    def api(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'GET' and path.startswith('/repos/o/r/issues?state=open&creator=me&per_page=100&page='):
            page = int(path.rsplit('=', 1)[1])
            return self.issues[(page - 1) * 100:page * 100]
        if method == 'POST' and path == '/repos/o/r/issues':
            url = f'https://github.com/o/r/issues/{len(self.issues) + 1}'
            self.issues.append({'body': body['body'], 'user': {'login': 'me'}, 'html_url': url})
            return {'html_url': url}
        raise AssertionError(path)


@pytest.fixture
def run_dir(tmp_path):
    shutil.copy(FIXTURE, tmp_path / 'evidence.json')
    return tmp_path


def created(fake):
    return [b for m, p, b in fake.calls if m == 'POST']


def test_tickets_json_has_one_ticket_per_fingerprint_with_steps_and_screenshots(run_dir):
    tickets = build(run_dir)
    assert len({t['fingerprint'] for t in tickets}) == len(tickets) == 5
    agent = next(t for t in tickets if t['fingerprint'].startswith('agent|'))
    assert agent['screenshots'] == ['preview-1-1.png'] and agent['personas'] == ['P1']
    assert agent['steps'] == ['Open /write on desktop.', 'Try to: g', 'Observe: Save button hidden behind cookie bar']
    assert agent['id'] == ticket_id(agent['fingerprint']) and agent['severity'] == 'blocker'
    fixed = next(t for t in tickets if t['possible_fix'])
    assert fixed['possible_fix']['file'] == 'src/app/write/page.tsx'
    assert [t['severity'] for t in build(run_dir, 'blocker')] == ['blocker', 'blocker']


def test_the_cli_writes_tickets_json_and_files_nothing_unless_asked(run_dir, monkeypatch):
    from swarm_persona import cli

    monkeypatch.setattr(github.Client, 'from_cli', classmethod(lambda cls: pytest.fail('no GitHub without the flag')))
    with pytest.raises(SystemExit) as stop:
        cli.main(['tickets', str(run_dir)])
    assert stop.value.code == 0
    assert len(json.loads((run_dir / 'tickets.json').read_text())) == 5


def test_issues_are_deduped_by_the_fingerprint_marker_on_our_open_issues(run_dir):
    tickets = build(run_dir)
    first = tickets[0]
    fake = Fake(issues=[
        {'body': 'old ' + MARKER.format(first['id']), 'user': {'login': 'me'}, 'html_url': 'https://github.com/o/r/issues/9'},
        {'body': MARKER.format(tickets[1]['id']), 'user': {'login': 'mallory'}, 'html_url': 'x'},  # not ours
        {'body': MARKER.format(tickets[2]['id']), 'user': {'login': 'me'}, 'html_url': 'y', 'pull_request': {}},
    ])
    results = file_issues(fake, 'o/r', tickets, ['ux'])
    assert results[0] == {'id': first['id'], 'url': 'https://github.com/o/r/issues/9', 'created': False}
    assert [r['created'] for r in results] == [False, True, True, True, True]
    assert all(b['labels'] == ['ux'] for b in created(fake))
    again = file_issues(fake, 'o/r', tickets, [])
    assert not any(r['created'] for r in again) and len(created(fake)) == 4


def test_the_same_fingerprint_twice_in_one_batch_files_once(run_dir):
    tickets = build(run_dir)
    fake = Fake()
    file_issues(fake, 'o/r', [tickets[0], dict(tickets[0])], [])
    assert len(created(fake)) == 1 and 'labels' not in created(fake)[0]


def test_listing_follows_pages(run_dir):
    tickets = build(run_dir)
    filler = [{'body': '', 'user': {'login': 'me'}, 'html_url': 'f'}] * 100
    fake = Fake(issues=filler + [{'body': MARKER.format(tickets[0]['id']), 'user': {'login': 'me'}, 'html_url': 'p2'}])
    assert file_issues(fake, 'o/r', tickets[:1], [])[0] == {'id': tickets[0]['id'], 'url': 'p2', 'created': False}


def test_issue_text_is_defanged():
    t = {'id': 'a' * 16, 'fingerprint': 'agent|x', 'title': 'x', 'severity': 'major', 'classification': 'new',
         'journey': 'j', 'devices': ['desktop'], 'detail': 'ping @octocat see https://evil.net and #12 <img src=x>',
         'steps': ['Observe: [click](https://evil.net)'], 'personas': ['@admin'], 'screenshots': [],
         'possible_fix': {'file': 'a.ts', 'change': 'call @team now'}, 'run_id': 'r'}
    body = issue_body(t, '0.2.0')
    assert body.startswith(MARKER.format('a' * 16))
    for bad in ('@octocat', '@admin', '@team', 'https://evil.net', ' #12', '<img', ']('):
        assert bad not in body
    assert 'Possible fix (unverified): in `a.ts`' in body
