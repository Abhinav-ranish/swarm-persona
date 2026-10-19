"""Tickets from a finished run: tickets.json, and on request GitHub Issues, one per fingerprint.

Opt-in only: `run` never files anything. An issue carries a hidden marker with a hash of the
finding's fingerprint. An open issue of ours with that marker means the ticket already exists, and
a closed one means somebody closed it, so it is not filed again unless asked; filing after every
run adds only what is new. Text that reaches GitHub is defanged by
report.safe, like the PR comment.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

from .judge import judge, rank
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
    """One ticket per fingerprint, from findings rebuilt out of evidence.json the way replay does
    (report.json may come from an older version without fingerprints)."""
    evidence = json.loads((out / 'evidence.json').read_text())
    meta = evidence.get('meta', {})
    findings = judge(evidence, meta.get('fail_on') or ['blocker'])['findings']
    goals = {s['journey']: s['goal'] for s in evidence.get('sessions', []) if s.get('journey')}
    tickets: dict[str, dict] = {}
    for f in findings:
        if rank(f['severity']) > rank(min_severity):
            continue
        if f['fingerprint'] in tickets:
            t = tickets[f['fingerprint']]
            t['personas'] = sorted(set(t['personas']) | set(f['personas']))
            t['screenshots'] += [Path(s).name for s in f['screenshots'] if Path(s).name not in t['screenshots']]
            continue
        tickets[f['fingerprint']] = {
            'id': ticket_id(f['fingerprint']), 'fingerprint': f['fingerprint'], 'title': f['title'],
            'severity': f['severity'], 'classification': f['classification'], 'journey': f.get('journey'),
            'devices': f['devices'], 'detail': f['detail'], 'steps': steps(f, goals),
            'personas': f['personas'], 'screenshots': [Path(s).name for s in f['screenshots']],
            'possible_fix': f.get('possible_fix'), 'run_id': meta.get('run_id'),
        }
    return list(tickets.values())


def write_json(out: Path, tickets: list[dict]) -> Path:
    path = out / 'tickets.json'
    path.write_text(json.dumps(tickets, indent=2))
    return path


def issue_body(t: dict, version: str | None = None) -> str:
    fix = t.get('possible_fix')
    lines = [MARKER.format(t['id']),
             (f"**{t['severity']}**, {t['classification']}, journey {safe(t['journey'] or '-', 60)}, "
              f"{'/'.join(t['devices']) or 'any device'}"), '', safe(t['detail'], 1500), '',
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


def known_tickets(client, repo: str) -> dict[str, dict]:
    """{ticket id: {url, number, state}} for every issue we opened, open or closed, carrying a marker.
    An open issue wins over a closed one with the same marker."""
    login, found, page = client.login(), {}, 1
    while True:
        issues = client.api('GET', f'/repos/{repo}/issues?state=all&creator={quote(login)}&per_page=100&page={page}')
        for issue in issues:
            if 'pull_request' in issue or (issue.get('user') or {}).get('login') != login:
                continue
            for tid in MARKER_RE.findall(issue.get('body') or ''):
                if tid not in found or issue.get('state') == 'open':
                    found[tid] = {'url': issue['html_url'], 'number': issue.get('number'), 'state': issue.get('state')}
        if len(issues) < 100:
            return found
        page += 1


def file_issues(client, repo: str, tickets: list[dict], labels: list[str], version: str | None = None,
                reopen_closed: bool = False, max_issues: int = 10) -> list[dict]:
    """Open an issue per ticket that has none yet; a ticket whose issue was closed stays closed unless
    reopen_closed. At most max_issues are created or reopened. Returns what happened to each:
    created, reopened, exists, closed, or skipped (over max_issues)."""
    known = known_tickets(client, repo)
    results, budget = [], max_issues
    for t in tickets:
        have = known.get(t['id'])
        if have and have['state'] == 'open':
            results.append({'id': t['id'], 'url': have['url'], 'action': 'exists'})
        elif have and not reopen_closed:
            results.append({'id': t['id'], 'url': have['url'], 'action': 'closed'})
        elif budget <= 0:
            results.append({'id': t['id'], 'url': None, 'action': 'skipped'})
        elif have:
            budget -= 1
            client.api('PATCH', f"/repos/{repo}/issues/{have['number']}", {'state': 'open'})
            known[t['id']] = {**have, 'state': 'open'}
            results.append({'id': t['id'], 'url': have['url'], 'action': 'reopened'})
        else:
            budget -= 1
            body = {'title': safe(t['title'], 120) or 'swarm-persona finding', 'body': issue_body(t, version)}
            if labels:
                body['labels'] = labels
            url = client.api('POST', f'/repos/{repo}/issues', body)['html_url']
            known[t['id']] = {'url': url, 'number': None, 'state': 'open'}
            results.append({'id': t['id'], 'url': url, 'action': 'created'})
    return results
