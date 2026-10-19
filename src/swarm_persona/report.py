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


def img(images: dict[str, str] | None, path: str | None, width: int = 360) -> str:
    url = (images or {}).get(Path(path).name) if path else None
    return f'<img src="{url}" width="{width}" alt="{Path(path).name}">' if url else ''


def counts(findings: list[dict]) -> str:
    return ', '.join(f'{n} {s}' for s in SEVERITIES if (n := sum(1 for f in findings if f['severity'] == s))) or 'none'


def render(meta: dict, verdict: dict, sessions: list[dict], checks: list[dict], dropped: list[dict],
           no_em_dashes: bool = False, images: dict[str, str] | None = None) -> str:
    """The PR comment: a summary table on top, every detail folded below it. images maps a
    screenshot's file name to a URL GitHub can show; without it screenshots are named, not shown."""
    findings = verdict['findings']
    where = f" for #{meta['pr']}" if meta.get('pr') else ''
    lines = [MARKER, f"### {safe(meta['title'], 80)}{where}", '']
    footer = f"<sub>swarm-persona {meta.get('version', '')} · `{safe(meta['model'], 60)}` · `{meta['url']}`"
    if meta.get('skipped'):
        lines += [f"Nothing ran: {safe(meta['skipped'])}", '', footer + '</sub>']
        return '\n'.join(lines)
    walked = [s for s in sessions if s['target'] == 'preview']
    verdicts = verdict.get('personas') or []
    results = {v['target'] + str(v['id']): v for v in verdicts}
    preview = [v for v in verdicts if v['target'] == 'preview']
    gate = 'Passed' if verdict['passed'] else f"**Failed** ({len(verdict['gating'])} new at {'/'.join(meta['fail_on'])})"
    tally = ', '.join(f"{sum(1 for v in preview if v['result'] == r)} {r}" for r in ('pass', 'partial', 'fail'))
    if verdict.get('summary'):
        lines += [f"**{safe(verdict['summary'], 120)}**", '']
    lines += ['| Gate | Personas | Findings |', '|---|---|---|',
              f"| {gate} | {len(walked)} walked: {tally} | {counts(findings)} |", '']
    if preview:
        lines += ['| Persona | Journey | Verdict |', '|---|---|---|']
        lines += [f"| {safe(v['name'], 40)} ({v['device']}) | {safe(v['journey'] or '-', 60)} | {result_text(v)} |"
                  for v in preview]
        lines.append('')
    if findings:
        lines += ['| # | Severity | Finding | Seen by | Status |', '|---|---|---|---|---|']
        for f in findings:
            who = f"{len(f['personas'])} persona{'s' if len(f['personas']) != 1 else ''}" if f['personas'] else f['source']
            lines.append(f"| {f['id']} | {f['severity']} | {safe(f['title'], 100)} | {who}, {'/'.join(f['devices'])} | "
                         f"{f['classification']} |")
        lines.append('')
        for f in findings:
            lines += [f"<details><summary><b>{f['id']}. {safe(f['title'], 100)}</b> ({f['severity']})</summary>", '',
                      safe(f['detail'], 1200), '']
            meta_line = f"Severity rule: {safe(f['rule'], 120)}."
            if f['paths']:
                meta_line += f" Pages: {', '.join(safe(p, 80) for p in f['paths'][:5])}."
            if f['personas']:
                meta_line += f" Seen by {', '.join(safe(p, 40) for p in f['personas'])}."
            lines += [meta_line, '']
            shots = [img(images, s) for s in f['screenshots'][:3]]
            if any(shots):
                lines += [' '.join(x for x in shots if x), '']
            elif f['screenshots']:
                lines += [f"Screenshots: {', '.join(Path(s).name for s in f['screenshots'][:6])}", '']
            lines += ['</details>', '']
    if verdict.get('worked_well'):
        lines += [f"<details><summary>Worked well ({len(verdict['worked_well'])})</summary>", '',
                  *[f"- {safe(w['note'], 200)} ({safe(w['persona'], 40)})" for w in verdict['worked_well']],
                  '', '</details>', '']
    if walked or sessions:
        lines += [f'<details><summary>Sessions ({len(sessions)})</summary>', '',
                  '| Target | Persona | Device | Goal | Result | Said | Steps |', '|---|---|---|---|---|---|---|']
        for s in sessions:
            v = results.get(s['target'] + str(s.get('id')))
            lines.append(f"| {s['target']} | {safe(s['name'], 40)}: {safe(s['who'], 160)} | {s['device']} | "
                         f"{safe(s['goal'], 200)} | {result_text(v) if v else '-'} | {safe(s['verdict'], 300)} | "
                         f"{s['steps']} |")
        lines += ['', '</details>', '']
    if checks:
        lines += [f'<details><summary>Entry checks ({len(checks)})</summary>', '',
                  '| Target | Journey | Device | Status | Signals |', '|---|---|---|---|---|']
        for c in checks:
            bad = [x['state'] for x in c['selectors'] if x['state'] != 'ok']
            sig = (len(c['page_errors']), len(c['console_errors']), len(c['failed_requests']), len(c['server_errors']))
            lines.append(f"| {c['target']} | {safe(c['journey'], 60)} | {c['device']} | {c['status'] or safe(c['error'], 80)} | "
                         f"{sig[0]} page errors, {sig[1]} console, {sig[2]} failed, {sig[3]} 5xx"
                         + (f"; selectors {', '.join(bad)}" if bad else '') + ' |')
        shots = [img(images, c.get('screenshot'), 240) for c in checks]
        lines += ['', ' '.join(x for x in shots if x), '', '</details>', '']
    extra = guard_lines(sessions, meta.get('app_hosts') or [urlsplit(meta['url']).hostname or ''])
    notes = [f'- {safe(n, 300)}' for n in meta.get('notes') or []]
    if dropped:
        notes.append(f'- The merge pass left out {len(dropped)} raw report(s): ' + '; '.join(
            f"{safe(d['title'], 80)} ({safe(d['reason'], 120)})" for d in dropped) + '.')
    if extra or notes:
        lines += ['<details><summary>Guard and notes</summary>', '', *(extra[1:] if extra else []), *notes, '', '</details>', '']
    if verdict['improved']:
        lines += ['**Improved** (fails on the baseline, passes on the preview): ' + '; '.join(
            f"{safe(i['journey'], 60)} on {i['device']}" for i in verdict['improved']), '']
    lines.append(footer + (' · baseline `' + meta['baseline_url'] + '`' if verdict['baseline_ran']
                           else ' · no baseline, so findings are unclassified') + '</sub>')
    text = '\n'.join(lines)
    return re.sub(rf'\s*{EM_DASH}\s*', ', ', text) if no_em_dashes else text


def result_text(v: dict) -> str:
    label = '**' + v['result'] + '**' + (' (self-report)' if v['basis'] == 'self-report' else '')
    return f"{label}: {safe(v['reason'], 120)}"


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
