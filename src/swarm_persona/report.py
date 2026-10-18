"""report.md (the PR comment), report.json and the GitHub step summary.

Text that came from a model or a web page is defanged before it reaches markdown: no @mentions,
no links or images, no HTML, and no table-breaking pipes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .config import SEVERITIES

MARKER = '<!-- swarm-persona -->'
ZWSP = chr(0x200B)
EM_DASH = chr(0x2014)


def safe(text, limit: int = 600) -> str:
    text = ' '.join(str(text or '').split())[:limit]
    text = re.sub(r'<[^>]*>', '', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'(?i)\b(https?)://', lambda m: f'{m.group(1)}:/{ZWSP}/', text)
    text = re.sub(r'(?i)\bwww\.', 'www' + ZWSP + '.', text)
    text = re.sub(r'#(?=\d)', '#' + ZWSP, text)  # #123 and owner/repo#123 cross-reference other issues
    text = re.sub(r'@(?=[A-Za-z0-9_-])', '@' + ZWSP, text)
    return text.replace('|', '/').replace('`', "'")


def path_of(url: str) -> str:
    return urlsplit(url or '').path or '/'


def render(meta: dict, verdict: dict, sessions: list[dict], checks: list[dict], dropped: list[dict],
           no_em_dashes: bool = False) -> str:
    findings = verdict['findings']
    baseline = meta.get('baseline_url')
    head = f"## {safe(meta['title'], 80)}{f' for #{meta['pr']}' if meta.get('pr') else ''}"
    gate = ('Passed: nothing new at ' + '/'.join(meta['fail_on']) + '.') if verdict['passed'] else (
        f"**Failed**: {len(verdict['gating'])} new finding(s) at {'/'.join(meta['fail_on'])} "
        f"(#{', #'.join(map(str, verdict['gating']))}).")
    lines = [MARKER, head, '']
    if meta.get('skipped'):
        lines += [f"Nothing ran: {safe(meta['skipped'])}", '', f"_swarm-persona {meta.get('version', '')}_"]
        return '\n'.join(lines)
    lines += [
        f"{len({s['id'] for s in sessions})} personas on `{safe(meta['model'], 80)}` against `{meta['url']}`"
        + (f", with `{baseline}` as the baseline." if verdict['baseline_ran'] else
           '. **No baseline ran**, so findings are unclassified: nobody can say whether this change caused them.'),
        '', gate, '',
    ]
    if meta.get('selection'):
        lines += ['Journeys: ' + '; '.join(f'{safe(k, 60)} ({safe(v, 120)})' for k, v in meta['selection'].items()), '']
    for title, cls in (('New', 'new'), ('Unclassified', 'unclassified'), ('Pre-existing (also on the baseline)', 'pre-existing')):
        group = [f for f in findings if f['classification'] == cls]
        if not group:
            continue
        counts = ', '.join(f'{n} {s}' for s in SEVERITIES if (n := sum(1 for f in group if f['severity'] == s)))
        lines += [f'### {title}: {counts}', '']
        for f in group:
            who = f"; seen by {', '.join(safe(p, 40) for p in f['personas'])}" if f['personas'] else ''
            lines.append(f"{f['id']}. **[{f['severity']}] {safe(f['title'], 120)}** ({f['source']}, "
                         f"{safe(f['kind'], 30)}; {', '.join(f['devices'])}{who})")
            lines.append(f"   {safe(f['detail'], 900)}")
            lines.append(f"   Severity rule: {safe(f['rule'], 120)}."
                         + (f" Pages: {', '.join(safe(p, 80) for p in f['paths'][:5])}." if f['paths'] else ''))
            if f['screenshots']:
                lines.append(f"   Screenshots: {', '.join(Path(s).name for s in f['screenshots'][:6])}")
        lines.append('')
    if not findings:
        lines += ['No findings.', '']
    if verdict['improved']:
        lines += ['### Improved (fails on the baseline, passes on the preview)', '']
        lines += [f"- {safe(i['journey'], 60)} on {i['device']}: {safe(i['detail'], 200)}" for i in verdict['improved']]
        lines.append('')
    if checks:
        lines += ['### Entry checks', '', '| Target | Journey | Device | Status | Signals |', '|---|---|---|---|---|']
        for c in checks:
            bad = [s['state'] for s in c['selectors'] if s['state'] != 'ok']
            sig = (len(c['page_errors']), len(c['console_errors']), len(c['failed_requests']), len(c['server_errors']))
            lines.append(f"| {c['target']} | {safe(c['journey'], 60)} | {c['device']} | {c['status'] or safe(c['error'], 80)} | "
                         f"{sig[0]} page errors, {sig[1]} console, {sig[2]} failed, {sig[3]} 5xx"
                         + (f"; selectors {', '.join(bad)}" if bad else '') + ' |')
        lines.append('')
    lines += ['### Sessions', '', '| Target | Persona | Device | Goal | Verdict | Steps |', '|---|---|---|---|---|---|']
    for s in sessions:
        lines.append(f"| {s['target']} | {safe(s['name'], 40)}: {safe(s['who'], 160)} | {s['device']} | "
                     f"{safe(s['goal'], 200)} | {safe(s['verdict'], 300)} | {s['steps']} |")
    if dropped:
        lines += ['', f'The merge pass left out {len(dropped)} raw report(s): ' + '; '.join(
            f"{safe(d['title'], 80)} ({safe(d['reason'], 120)})" for d in dropped) + '.']
    lines += guard_lines(sessions, meta.get('app_hosts') or [urlsplit(meta['url']).hostname or ''])
    if meta.get('notes'):
        lines += [''] + [f'- {safe(n, 300)}' for n in meta['notes']]
    lines += ['', f"_Simulated users: this shows where an agent got stuck, not real user behaviour. "
                  f"swarm-persona {meta.get('version', '')}_"]
    text = '\n'.join(lines)
    return text.replace(EM_DASH, ', ') if no_em_dashes else text


def guard_lines(sessions: list[dict], app_hosts: list[str]) -> list[str]:
    """Refusals grouped by request and reason, the app's own first; other sites in one count."""
    own: dict[tuple, set] = {}
    other = 0
    for s in sessions:
        for b in s.get('guard_blocked', []):
            host = (urlsplit(b.get('url', '')).hostname or '').lower()
            if host not in app_hosts:
                other += 1
                continue
            key = (b.get('method'), path_of(b.get('url', '')), b.get('reason'))
            own.setdefault(key, set()).add(s['name'])
    if not own and not other:
        return []
    parts = [f"{m} {safe(p, 80)} ({safe(r, 60)}, {len(who)} persona{'s' if len(who) > 1 else ''})"
             for (m, p, r), who in sorted(own.items(), key=lambda kv: -len(kv[1]))[:20]]
    text = 'The network guard refused, on the app: ' + ('; '.join(parts) if parts else 'nothing') + '.'
    if other:
        text += f' It also refused {other} request(s) to other sites.'
    return ['', text]


def write(out: Path, report: str, data: dict) -> None:
    (out / 'report.md').write_text(report)
    (out / 'report.json').write_text(json.dumps(data, indent=2, default=str))
