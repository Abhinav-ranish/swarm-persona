import json
import shutil
from pathlib import Path

import pytest

from swarm_persona import github
from swarm_persona.tickets import MARKER, build, file_issues, issue_body, ticket_id

FIXTURE = Path(__file__).parent / 'fixtures' / 'run' / 'evidence.json'


class Fake(github.Client):
    """Answers like the GitHub issues API: issues are listed back, created ones kept, PATCH reopens."""

    def __init__(self, issues=(), login='me'):
        super().__init__('t', login)
        self.issues, self.calls = [{'state': 'open', 'number': n + 1, **i} for n, i in enumerate(issues)], []

    def api(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'GET' and path.startswith('/repos/o/r/issues?state=all&creator=me&per_page=100&page='):
            page = int(path.rsplit('=', 1)[1])
            return self.issues[(page - 1) * 100:page * 100]
        if method == 'POST' and path == '/repos/o/r/issues':
            url = f'https://github.com/o/r/issues/{len(self.issues) + 1}'
            self.issues.append({'body': body['body'], 'user': {'login': 'me'}, 'html_url': url, 'state': 'open',
                                'number': len(self.issues) + 1})
            return {'html_url': url}
        if method == 'PATCH' and path.startswith('/repos/o/r/issues/'):
            self.issues[int(path.rsplit('/', 1)[1]) - 1]['state'] = body['state']
            return {}
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


def actions(results):
    return [r['action'] for r in results]


def test_issues_are_deduped_by_the_fingerprint_marker_on_our_issues(run_dir):
    tickets = build(run_dir)
    first = tickets[0]
    fake = Fake(issues=[
        {'body': 'old ' + MARKER.format(first['id']), 'user': {'login': 'me'}, 'html_url': 'https://github.com/o/r/issues/1'},
        {'body': MARKER.format(tickets[1]['id']), 'user': {'login': 'mallory'}, 'html_url': 'x'},  # not ours
        {'body': MARKER.format(tickets[2]['id']), 'user': {'login': 'me'}, 'html_url': 'y', 'pull_request': {}},
    ])
    results = file_issues(fake, 'o/r', tickets, ['ux'])
    assert results[0] == {'id': first['id'], 'url': 'https://github.com/o/r/issues/1', 'action': 'exists'}
    assert actions(results) == ['exists', 'created', 'created', 'created', 'created']
    assert all(b['labels'] == ['ux'] for b in created(fake))
    assert actions(file_issues(fake, 'o/r', tickets, [])) == ['exists'] * 5 and len(created(fake)) == 4


def test_a_closed_issue_is_not_filed_again_unless_reopen_closed(run_dir):
    tickets = build(run_dir)[:2]
    fake = Fake(issues=[{'body': MARKER.format(tickets[0]['id']), 'user': {'login': 'me'}, 'html_url': 'c',
                         'state': 'closed'}])
    assert actions(file_issues(fake, 'o/r', tickets, [])) == ['closed', 'created']
    assert actions(file_issues(fake, 'o/r', tickets, [], reopen_closed=True)) == ['reopened', 'exists']
    assert ('PATCH', '/repos/o/r/issues/1', {'state': 'open'}) in fake.calls and fake.issues[0]['state'] == 'open'


def test_an_open_duplicate_wins_over_a_closed_one(run_dir):
    [t] = build(run_dir)[:1]
    mark = {'body': MARKER.format(t['id']), 'user': {'login': 'me'}}
    fake = Fake(issues=[{**mark, 'html_url': 'open', 'state': 'open'}, {**mark, 'html_url': 'shut', 'state': 'closed'}])
    assert file_issues(fake, 'o/r', [t], [])[0] == {'id': t['id'], 'url': 'open', 'action': 'exists'}


def test_max_issues_caps_creations_and_reopens(run_dir):
    tickets = build(run_dir)
    fake = Fake()
    assert actions(file_issues(fake, 'o/r', tickets, [], max_issues=2)) == ['created', 'created'] + ['skipped'] * 3
    assert len(created(fake)) == 2
    assert actions(file_issues(fake, 'o/r', tickets, [])) == ['exists', 'exists', 'created', 'created', 'created']


def test_the_same_fingerprint_twice_in_one_batch_files_once(run_dir):
    tickets = build(run_dir)
    fake = Fake()
    file_issues(fake, 'o/r', [tickets[0], dict(tickets[0])], [])
    assert len(created(fake)) == 1 and 'labels' not in created(fake)[0]


def test_listing_follows_pages(run_dir):
    tickets = build(run_dir)
    filler = [{'body': '', 'user': {'login': 'me'}, 'html_url': 'f'}] * 100
    fake = Fake(issues=filler + [{'body': MARKER.format(tickets[0]['id']), 'user': {'login': 'me'}, 'html_url': 'p2'}])
    assert file_issues(fake, 'o/r', tickets[:1], [])[0] == {'id': tickets[0]['id'], 'url': 'p2', 'action': 'exists'}


def test_tickets_ignore_an_old_report_json_and_rebuild_from_evidence(run_dir):
    (run_dir / 'report.json').write_text(json.dumps({'findings': [{'title': 'v0.1.x finding, no fingerprint',
                                                                  'severity': 'blocker'}], 'gating': []}))
    tickets = build(run_dir)
    assert len(tickets) == 5 and all(t['fingerprint'] for t in tickets)
    assert 'v0.1.x' not in json.dumps(tickets)


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
