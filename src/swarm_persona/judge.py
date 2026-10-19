"""From recorded evidence to findings. Pure and deterministic: no browser, no model, so a saved run
replays to the same verdict.

Classification, against the baseline (production) when one ran:
- new           seen on the preview, not on the baseline: the change caused it
- pre-existing  seen on both: not this change's fault
- unclassified  no baseline ran, so nobody can say
Signals seen only on the baseline are listed as improved.

Severity is set by rules, never raised by a model:
- hard signals carry their own severity (an entry page that fails on every device is a blocker,
  on one device major; an uncaught page error or a 5xx from the app is major; console noise and
  failed requests are minor and never confirm anything on their own);
- an agent's report keeps the severity the persona gave it (the most severe, when several were
  merged), capped at major unless it is corroborated: two or more personas hit it, or a hard
  signal fired on the same journey. The merge model's own severity is ignored.

Each persona also gets pass, fail or partial against its journey's success criteria, decided from
the evidence (crashed, cut off at the timeout, done and marked successful, hit max_steps, a 5xx
during its own walk). Only
when the evidence is silent does the persona's own ACHIEVED / GAVE UP verdict decide, labelled
self-report.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from urllib.parse import urlsplit

from .checks import normalize, signals
from .config import SEVERITIES

HARD = {'load-failed', 'http-status', 'selector', 'page-error', 'server-error'}
SIGNAL_TITLES = {
    'load-failed': 'Entry page did not load', 'http-status': 'Entry page answered an error status',
    'selector': 'A critical element is not usable', 'page-error': 'Uncaught error on the page',
    'server-error': 'The app answered 5xx', 'console-error': 'Console error', 'failed-request': 'A request failed',
}


def rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


def milder(a: str, b: str) -> str:
    return a if rank(a) >= rank(b) else b


def session_signals(session: dict) -> list[dict]:
    """5xx answers the guard proxy saw while an agent walked a journey."""
    journey = session.get('journey') or session.get('start_path') or '/'
    return [{'kind': 'server-error', 'journey': journey, 'device': session['device'], 'path': e['path'],
             'detail': f"{e['method']} {e['path']} answered {e['status']}",
             'fingerprint': '|'.join([journey, session['device'], 'server-error',
                                      normalize(f"{e['method']} {e['path']} {e['status']}")])}
            for e in session.get('server_errors', [])]


def all_signals(evidence: dict, target: str) -> list[dict]:
    out = [s for c in evidence['checks'] if c['target'] == target for s in signals(c)]
    out += [s for x in evidence['sessions'] if x['target'] == target for s in session_signals(x)]
    seen, unique = set(), []
    for s in out:
        if s['fingerprint'] not in seen:
            seen.add(s['fingerprint'])
            unique.append(s)
    return unique


def signal_severity(kind: str, detail: str, devices: set[str], all_devices: set[str]) -> tuple[str, str]:
    everywhere = devices >= all_devices
    if kind in ('load-failed', 'http-status', 'selector'):
        if kind == 'http-status' and (' 401' in detail or ' 403' in detail):
            return 'major', 'entry page refused access'
        return ('blocker', 'fails on every device') if everywhere else ('major', 'fails on some devices')
    if kind in ('page-error', 'server-error'):
        return 'major', f'{kind} is a hard signal'
    return 'minor', f'{kind} alone never confirms a finding'


def classify(present_on_baseline: bool, baseline_ran: bool) -> str:
    if not baseline_ran:
        return 'unclassified'
    return 'pre-existing' if present_on_baseline else 'new'


def signal_findings(evidence: dict) -> tuple[list[dict], list[dict]]:
    baseline_ran = evidence.get('baseline_ran', False)
    preview, baseline = all_signals(evidence, 'preview'), all_signals(evidence, 'baseline')
    base_prints = {s['fingerprint'] for s in baseline}
    devices_by_journey = defaultdict(set)
    for c in evidence['checks']:
        if c['target'] == 'preview':
            devices_by_journey[c['journey']].add(c['device'])
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for s in preview:
        groups[(s['journey'], s['kind'], normalize(s['detail']))].append(s)
    findings = []
    for (journey, kind, _), items in groups.items():
        devices = {s['device'] for s in items}
        severity, rule = signal_severity(kind, items[0]['detail'], devices,
                                         devices_by_journey.get(journey) or devices)
        findings.append({
            'source': 'signal', 'kind': kind, 'journey': journey, 'devices': sorted(devices),
            'title': f"{SIGNAL_TITLES[kind]} ({journey})", 'detail': items[0]['detail'],
            'severity': severity, 'rule': rule, 'paths': sorted({s['path'] for s in items}),
            'classification': classify(any(s['fingerprint'] in base_prints for s in items), baseline_ran),
            'personas': [], 'screenshots': [], 'fingerprint': '|'.join(['signal', journey, kind, normalize(items[0]['detail'])]),
        })
    prev_prints = {s['fingerprint'] for s in preview}
    improved = [{'kind': s['kind'], 'journey': s['journey'], 'device': s['device'], 'detail': s['detail']}
                for s in baseline if s['fingerprint'] not in prev_prints and s['kind'] in HARD]
    return findings, improved


def issue_key(issue: dict) -> tuple:
    """Coarse on purpose: agent reports are prose, so a preview report counts as pre-existing when the
    baseline's personas reported the same category on the same journey and page (ids removed)."""
    path = normalize(urlsplit(issue.get('url') or '').path.rstrip('/') or '/')
    return (issue.get('journey') or '', path, issue.get('category'))


def agent_fingerprint(src: list[dict]) -> str:
    """The sorted set of the sources' (journey, page, category): only deterministic inputs, so the same
    problem keeps its fingerprint from run to run and whatever order the merge lists its sources in.
    Titles are model prose and never part of it, so two findings of one run can share a fingerprint;
    tickets merge them and fixes are keyed per finding (finding_key)."""
    return 'agent|' + ';'.join(sorted({'|'.join(str(k) for k in issue_key(s)) for s in src}))


def finding_key(finding: dict) -> str:
    """Tells apart findings that share a fingerprint, and is the same on every replay of one evidence."""
    text = '\n'.join([finding['fingerprint'], finding['title'] or '', finding['detail'] or ''])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def agent_findings(evidence: dict, hard_journeys: set[str]) -> list[dict]:
    raw = [i for i in evidence['raw_issues'] if i['target'] == 'preview']
    base_keys = {issue_key(i) for i in evidence['raw_issues'] if i['target'] == 'baseline'}
    # The baseline's entry checks may have run while every one of its personas crashed or was cut off:
    # then no persona walked it, and an agent report cannot be called new or pre-existing.
    walkers = [x for x in evidence['sessions'] if x['target'] == 'baseline']
    baseline_ran = evidence.get('baseline_ran', False) and not (
        walkers and all(x.get('crashed') or x.get('timed_out') for x in walkers))
    merge = evidence.get('merge')
    groups = ([{**m, 'source_ids': [i for i in m.get('source_ids', []) if 0 <= i < len(raw)]} for m in merge['issues']]
              if merge else [{**i, 'source_ids': [n]} for n, i in enumerate(raw)])
    findings = []
    for g in groups:
        src = [raw[i] for i in g['source_ids']]
        if not src:
            continue
        personas = {s['persona_id'] for s in src}
        journeys = {s.get('journey') or '' for s in src}
        corroborated = len(personas) >= 2 or bool(journeys & hard_journeys)
        # The most severe of the personas' own reports: the merge reply only groups and words them.
        claimed = min((s['severity'] for s in src), key=rank)
        severity = claimed if corroborated else milder(claimed, 'major')
        rule = ('corroborated by ' + ('several personas' if len(personas) >= 2 else 'a hard signal')
                if corroborated else 'one persona, no hard signal: capped at major')
        findings.append({
            'source': 'agent', 'kind': g.get('category') or src[0]['category'],
            'journey': ', '.join(sorted(j for j in journeys if j)) or None,
            'devices': sorted({s['device'] for s in src}), 'title': g.get('title') or src[0]['title'],
            'detail': g.get('detail') or src[0]['detail'], 'severity': severity, 'rule': rule,
            'paths': sorted({urlsplit(s['url']).path or '/' for s in src if s.get('url')}),
            'classification': classify(any(issue_key(s) in base_keys for s in src), baseline_ran),
            'personas': sorted({s['persona'] for s in src}),
            'screenshots': [s['screenshot'] for s in src if s.get('screenshot')],
            'fingerprint': agent_fingerprint(src),
        })
    return findings


def journey_findings(evidence: dict) -> list[dict]:
    """A journey the preview's personas could not finish while the baseline's did, per device."""
    if not evidence.get('baseline_ran'):
        return []
    outcome: dict[tuple, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for x in evidence['sessions']:
        # A persona cut off at the run's timeout says nothing about the journey: the clock is shared by
        # every persona on the target, and some never start. Like a crash, it counts on neither side.
        if x.get('journey') and not x.get('crashed') and not x.get('timed_out'):
            outcome[(x['journey'], x['device'])][x['target']].append(bool(x.get('achieved')))
    findings = []
    for (journey, device), by in sorted(outcome.items()):
        prev, base = by.get('preview', []), by.get('baseline', [])
        if not prev or not base or any(prev):
            continue
        findings.append({
            'source': 'journey', 'kind': 'journey', 'journey': journey, 'devices': [device],
            'title': f'Personas could not finish {journey} on {device}',
            'detail': f"{len(prev)} preview persona(s) gave up; on the baseline {sum(base)} of {len(base)} finished.",
            'severity': 'major' if any(base) else 'minor',
            'rule': 'agent outcome only: capped at major' if any(base) else 'fails on the baseline too',
            'classification': 'new' if any(base) else 'pre-existing', 'paths': [], 'personas': [], 'screenshots': [],
            'fingerprint': f'journey|{journey}|{device}',
        })
    return findings


def outcome(session: dict, fired: list[str], max_steps: int | None) -> tuple[str, str, str]:
    """(pass / fail / partial, evidence / self-report, why) for one persona's walk."""
    if session.get('crashed'):
        return 'fail', 'evidence', 'crashed before finishing'
    if session.get('timed_out'):
        minutes = session.get('timeout_minutes')
        return 'fail', 'evidence', f'cut off at the {minutes}-minute timeout' if minutes else 'cut off at the timeout'
    said = str(session.get('verdict') or '').strip().upper()
    signalled = f"hard signals fired: {', '.join(fired)}"
    if session.get('success') is True:
        if fired:
            return 'partial', 'evidence', f'marked the goal met, but {signalled}'
        if said.startswith('GAVE UP'):
            return 'partial', 'evidence', 'marked the goal met, but its verdict says it gave up'
        return 'pass', 'evidence', 'finished and marked the goal met'
    if session.get('done') and session.get('success') is False:
        return 'fail', 'evidence', 'finished and marked the goal not met'
    if max_steps and not session.get('done') and session.get('steps', 0) >= max_steps:
        return 'fail', 'evidence', f'hit max_steps ({max_steps}) without finishing'
    if said.startswith('ACHIEVED'):
        return ('partial', 'self-report', f'says it got there, but {signalled}') if fired else (
            'pass', 'self-report', 'says it got there')
    return 'fail', 'self-report', 'says it gave up' if said.startswith('GAVE UP') else 'no verdict'


def persona_verdicts(evidence: dict) -> list[dict]:
    """One per session, in evidence order. Only hard signals from the persona's own walk count: the
    5xx its guard proxy saw. Entry checks run in a separate browser the persona never used, and the
    walk records no page or console errors, so those cannot be pinned on a persona."""
    max_steps = evidence['meta'].get('max_steps')
    out = []
    for x in evidence['sessions']:
        kinds = ['server-error'] if x.get('server_errors') else []
        result, basis, reason = outcome(x, kinds, max_steps)
        out.append({'target': x['target'], 'id': x.get('id'), 'name': x['name'], 'journey': x.get('journey'),
                    'device': x['device'], 'success_criteria': x.get('success_criteria') or '',
                    'result': result, 'basis': basis, 'reason': reason, 'timed_out': bool(x.get('timed_out'))})
    return out


def worked_well(evidence: dict, limit: int = 10) -> list[dict]:
    """The preview personas' positive notes, once each."""
    seen, out = set(), []
    for x in evidence['sessions']:
        if x['target'] != 'preview':
            continue
        for note in x.get('worked_well') or []:
            key = ' '.join(str(note).lower().split())
            if key and key not in seen and len(out) < limit:
                seen.add(key)
                out.append({'persona': x['name'], 'note': str(note)})
    return out


def summary(verdicts: list[dict], findings: list[dict]) -> str:
    walked = [v for v in verdicts if v['target'] == 'preview']
    done = sum(1 for v in walked if v['result'] == 'pass')
    partial = sum(1 for v in walked if v['result'] == 'partial')
    cut = sum(1 for v in walked if v.get('timed_out'))
    issues = f"{len(findings)} issue{'s' if len(findings) != 1 else ''}"
    if not walked:
        return f'No personas walked; {issues}'
    return f"{done}/{len(walked)} personas completed" + (f' ({partial} partly)' if partial else '') + (
        f'; {cut} timed out' if cut else '') + f'; {issues}'


def judge(evidence: dict, fail_on: list[str], fail_on_unclassified: bool = False) -> dict:
    sig, improved = signal_findings(evidence)
    hard_journeys = {f['journey'] for f in sig if f['kind'] in HARD and f['classification'] != 'pre-existing'}
    findings = sig + agent_findings(evidence, hard_journeys) + journey_findings(evidence)
    order = {'new': 0, 'unclassified': 1, 'pre-existing': 2}
    findings.sort(key=lambda f: (order[f['classification']], rank(f['severity']), -len(f['personas'])))
    fixes = evidence.get('fixes') or {}
    for n, f in enumerate(findings, 1):
        f['id'] = n
        f['key'] = finding_key(f)
        f['possible_fix'] = fixes.get(f['key'])
    gating = [f for f in findings if f['severity'] in fail_on and (
        f['classification'] == 'new' or (f['classification'] == 'unclassified' and fail_on_unclassified))]
    verdicts = persona_verdicts(evidence)
    return {'findings': findings, 'improved': improved, 'gating': [f['id'] for f in gating],
            'passed': not gating, 'baseline_ran': evidence.get('baseline_ran', False),
            'personas': verdicts, 'summary': summary(verdicts, findings), 'worked_well': worked_well(evidence)}
