"""Tickets from a finished run: tickets.json, and on request GitHub Issues, one per fingerprint.

Opt-in only: `run` never files anything. An issue carries a hidden marker with a hash of the
finding's fingerprint, and an open issue of ours with that marker means the ticket already exists,
so filing again after every run adds only what is new. Text that reaches GitHub is defanged by
report.safe, like the PR comment.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

from .judge import rank
from .report import safe

MARKER = '<!-- swarm-persona-ticket:{} -->'
MARKER_RE = re.compile(r'<!-- swarm-persona-ticket:([0-9a-f]{16}) -->')


def ticket_id(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:16]


def steps(finding: dict, goals: dict[str, str]) -> list[str]:
    devices = '/'.join(finding['devices']) or 'any device'
    journey = finding.get('journey') or ''
    out = [f"Open {', '.join(finding['paths'][:3])} on {devices}." if finding['paths']
           else f'Start the {journey} journey on {devices}.']
    goal = goals.get(journey.split(', ')[0])
    if finding['source'] != 'signal' and goal:
        out.append(f'Try to: {goal}')
    out.append(f"Observe: {finding['detail'] if finding['source'] == 'signal' else finding['title']}")
    return out


def build(out: Path, min_severity: str = 'polish') -> list[dict]:
    """One ticket per fingerprint, from report.json (rebuilt from evidence.json when missing)."""
    if not (out / 'report.json').exists():
        from .run import replay

        replay(out)
    report = json.loads((out / 'report.json').read_text())
    evidence = json.loads((out / 'evidence.json').read_text())
    goals = {s['journey']: s['goal'] for s in evidence.get('sessions', []) if s.get('journey')}
    tickets: dict[str, dict] = {}
    for f in report['findings']:
        if rank(f['severity']) > rank(min_severity):
            continue
        if f['fingerprint'] in tickets:
            t = tickets[f['fingerprint']]
            t['personas'] = sorted(set(t['personas']) | set(f['personas']))
            t['screenshots'] += [s for s in f['screenshots'] if s not in t['screenshots']]
            continue
        tickets[f['fingerprint']] = {
            'id': ticket_id(f['fingerprint']), 'fingerprint': f['fingerprint'], 'title': f['title'],
            'severity': f['severity'], 'classification': f['classification'], 'journey': f.get('journey'),
            'devices': f['devices'], 'detail': f['detail'], 'steps': steps(f, goals),
            'personas': f['personas'], 'screenshots': [Path(s).name for s in f['screenshots']],
            'possible_fix': f.get('possible_fix'), 'run_id': report.get('meta', {}).get('run_id'),
        }
    return list(tickets.values())


def write_json(out: Path, tickets: list[dict]) -> Path:
    path = out / 'tickets.json'
    path.write_text(json.dumps(tickets, indent=2))
    return path


def issue_body(t: dict, version: str | None = None) -> str:
    fix = t.get('possible_fix')
    lines = [MARKER.format(t['id']),
             f"**{t['severity']}**, {t['classification']}, journey {safe(t['journey'] or '-', 60)}, "
             f"{'/'.join(t['devices']) or 'any device'}", '', safe(t['detail'], 1500), '',
             '**Steps to reproduce**', *[f'{n}. {safe(s, 400)}' for n, s in enumerate(t['steps'], 1)], '']
    if t['personas']:
        lines += [f"Seen by: {', '.join(safe(p, 40) for p in t['personas'])}.", '']
    if t['screenshots']:
        lines += [f"Screenshots (the run's shots/ folder): {', '.join(safe(s, 80) for s in t['screenshots'][:6])}", '']
    if fix:
        place = f"in `{safe(fix['file'], 120)}`: " if fix.get('file') else ''
        lines += [f"Possible fix (unverified): {place}{safe(fix['change'], 800)}", '']
    lines.append(f"<sub>Filed by swarm-persona {safe(version or '', 20)} from run {safe(t.get('run_id') or '-', 40)}. "
                 f"Fingerprint: `{safe(t['fingerprint'], 200)}`</sub>")
    return '\n'.join(lines)


def open_ticket_ids(client, repo: str) -> dict[str, str]:
    """{ticket id: issue URL} for open issues we opened that carry a ticket marker."""
    login, found, page = client.login(), {}, 1
    while True:
        issues = client.api('GET', f'/repos/{repo}/issues?state=open&creator={quote(login)}&per_page=100&page={page}')
        for issue in issues:
            if 'pull_request' in issue or (issue.get('user') or {}).get('login') != login:
                continue
            for tid in MARKER_RE.findall(issue.get('body') or ''):
                found.setdefault(tid, issue['html_url'])
        if len(issues) < 100:
            return found
        page += 1


def file_issues(client, repo: str, tickets: list[dict], labels: list[str], version: str | None = None) -> list[dict]:
    """Create an issue per ticket that has no open issue yet. Returns what happened to each."""
    existing = open_ticket_ids(client, repo)
    results = []
    for t in tickets:
        if t['id'] in existing:
            results.append({'id': t['id'], 'url': existing[t['id']], 'created': False})
            continue
        body = {'title': safe(t['title'], 120) or 'swarm-persona finding', 'body': issue_body(t, version)}
        if labels:
            body['labels'] = labels
        url = client.api('POST', f'/repos/{repo}/issues', body)['html_url']
        existing[t['id']] = url
        results.append({'id': t['id'], 'url': url, 'created': True})
    return results
