"""Portable, script-free local reports from saved evidence. No model or network calls."""

import base64
from html import escape
from pathlib import Path


def text(value) -> str:
    return escape(str(value if value is not None else '').replace(chr(0x2014), ', '), quote=True)


MISSING_IMAGE = ('<div class="missing-image"><strong>Screenshot unavailable</strong><p>The original image was '
                 'not included in this saved run, or could not be safely embedded.</p></div>')


def screenshot(out: Path, name: str, budget: list[int] | None = None,
               label: str = 'Captured page evidence', eager: bool = False) -> str:
    root = (out / 'shots').resolve()
    path = root / Path(name).name
    if not root.is_relative_to(out.resolve()) or path.is_symlink():
        return MISSING_IMAGE
    try:
        size = path.stat().st_size
        if path.suffix != '.png' or not path.resolve().is_relative_to(root) or size > 8_000_000:
            return MISSING_IMAGE
        if budget is not None and size > budget[0]:
            return '<p class="muted">Image omitted to keep this report small. Original screenshots are in shots/.</p>'
        data = path.read_bytes()
    except OSError:
        return MISSING_IMAGE
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return MISSING_IMAGE
    if budget is not None:
        budget[0] -= len(data)
    width = int.from_bytes(data[16:20], 'big') if len(data) >= 24 and data[12:16] == b'IHDR' else 0
    height = int.from_bytes(data[20:24], 'big') if width else 0
    dimensions = f'width="{width}" height="{height}" ' if 0 < width <= 100_000 and 0 < height <= 100_000 else ''
    ratio = min(3, max(.25, width / height)) if dimensions else 1
    loading = 'eager' if eager else 'lazy'
    return (f'<figure style="--capture-ratio:{ratio:.3f};--capture-width:{ratio * 400:.0f}px">'
            f'<figcaption>{text(label)}</figcaption><img {dimensions}loading="{loading}" '
            f'alt="{text(label)}" src="data:image/png;base64,' + base64.b64encode(data).decode() + '"></figure>')


def finding(out: Path, item: dict, budget: list[int], show_images: bool = True) -> str:
    source = 'Agent-reported observation' if item['source'] in ('agent', 'journey') else 'Browser signal'
    fix = item.get('possible_fix')
    next_steps = {
        'selector': 'Check whether the named element is visible, enabled and free of overlays at this viewport. Retest that interaction.',
        'http-status': 'Reproduce the entry-page response and inspect server logs for this path before retesting.',
        'load-failed': 'Check that this entry page is reachable by the test account and that its redirects stay inside the allowed hosts.',
        'server-error': 'Inspect the failing request and server logs. Reproduce the triggering action, then retest it.',
        'page-error': 'Reproduce the uncaught error on this entry page and inspect its stack trace before retesting.',
        'journey': 'Check the persona outcomes for the step where progress stopped. Reproduce that step with the same task and viewport.',
    }
    action = (fix.get('change') if fix else None) or next_steps.get(item['kind']) or (
        'Reproduce this step with the stated audience. Check the screenshot and expected behavior, '
        'make one focused change, then retest the same task.')
    images = ''.join(screenshot(out, s, budget) for s in item.get('screenshots', [])[:2]) if show_images else ''
    severity = item['severity'] if item['severity'] in ('blocker', 'major', 'minor', 'polish') else 'minor'
    return (f'<article class="finding {severity}"><p class="eyebrow"><span class="badge">{text(item["severity"])}</span> {source}</p>'
            f'<h3>{text(item["id"])}. {text(item["title"])}</h3><p>{text(item["detail"])}</p>'
            f'<p class="muted">Observed on {text(", ".join(item.get("paths", [])) or "task journey")} · '
            f'Basis: {text(item["rule"])}</p><h4>Suggested next step (unverified)</h4><p>{text(action)}</p>'
            f'{images}</article>')


CSS = """
:root{color-scheme:light;--ink:#20342d;--muted:#58675f;--line:#dce3dc;--accent:#205c45;
--paper:#f6f7f3;--surface:#fff;font-family:system-ui,-apple-system,sans-serif;color:var(--ink);background:var(--paper)}
*{box-sizing:border-box}body{margin:0}main{max-width:1120px;margin:auto;padding:0 32px 64px}
.masthead{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:24px 0;border-bottom:1px solid var(--line)}
.brand{font-size:17px;font-weight:750;letter-spacing:-.04em;white-space:nowrap}.brand::before{content:'+';display:inline-grid;place-items:center;
width:28px;height:28px;margin-right:10px;border-radius:8px;background:var(--accent);color:white;font-size:22px;font-weight:400}
.edition{color:var(--muted);font-size:12px;letter-spacing:.06em;text-transform:uppercase}
header{padding:40px 0 24px}h1{font-family:Georgia,serif;font-weight:400;font-size:clamp(36px,5.5vw,60px);letter-spacing:-.045em;line-height:1.08;margin:12px 0 20px}
h2{font-size:24px;letter-spacing:-.025em;margin:36px 0 16px}h3{font-size:22px;letter-spacing:-.02em;margin:16px 0 12px;line-height:1.3}
h4{font-size:14px;margin:24px 0 4px}p,li{line-height:1.65;overflow-wrap:anywhere}p{margin:12px 0}
.eyebrow{font-size:11px;font-weight:650;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.muted,figcaption{color:var(--muted);font-size:13px}.notice{background:#fff3d8;border:1px solid #e7c98b;border-radius:12px;padding:20px 24px;margin:16px 0}
article,details,.panel{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:28px;margin:16px 0}
.panel{background:#edf3ed}.panel h2,.notice h2{margin-top:0}.panel p{max-width:78ch}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border:1px solid var(--line);border-radius:14px;background:white;margin:8px 0 28px;overflow:hidden}
.stat{padding:20px 24px}.stat+.stat{border-left:1px solid var(--line)}.stat strong{display:block;font-size:30px;font-weight:550;letter-spacing:-.04em;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted)}.badge{display:inline-block;padding:4px 9px;border-radius:6px;background:#f3e9ce;color:#755518;margin-right:8px;letter-spacing:.04em}
.blocker .badge{background:#fbe5df;color:#963822}.major .badge{background:#fff0d5;color:#80551b}.polish .badge{background:#eaf0ee;color:#3e6253}
.finding{border-left:3px solid #ddc79c}.finding.blocker{border-left-color:#c76c57}.finding.major{border-left-color:#c69b4b}
.finding>p{max-width:85ch}summary{cursor:pointer;min-height:44px;align-content:center;font-weight:600;font-size:14px}
summary:hover{color:var(--accent)}summary:focus-visible{outline:3px solid var(--accent);outline-offset:6px;border-radius:4px}
details[open]>summary{margin-bottom:20px}details article{padding:24px 0;border:0;border-top:1px solid var(--line);border-radius:0}
.captures{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}.captures>figure{min-width:0;flex:var(--capture-ratio,1) 1 var(--capture-width,320px)}
figure{margin:24px 0 0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#f1f3ef}
figcaption{padding:12px 16px;border-bottom:1px solid var(--line);background:white;font-size:12px;font-weight:550}
img{display:block;width:100%;height:auto;max-height:560px;object-fit:contain;object-position:top}
.captures figure{margin:0}.captures img{height:auto;max-height:none}.missing-image{padding:24px;border:1px dashed #bdc9c0;border-radius:10px;background:#f8faf7;margin-top:20px;color:var(--muted);font-size:13px}.captures .missing-image{margin-top:0;flex:1 1 320px}
.missing-image p{margin-bottom:0}footer{border-top:1px solid var(--line);margin-top:40px;padding-top:24px;max-width:80ch}ul{padding-left:22px}
@media(max-width:640px){main{padding:0 20px 40px}.masthead{padding:20px 0}.edition{display:none}header{padding:28px 0 16px}
article,details,.panel{padding:20px;border-radius:12px}.captures>figure{flex-basis:100%}.stat{padding:16px 12px}.stat strong{font-size:25px}.stat span{font-size:11px}}
@media print{body{background:white}main{max-width:none;padding:0}.captures img,img{max-height:400px}article,figure{break-inside:avoid}}
"""


def render(out: Path, evidence: dict, verdict: dict) -> str:
    meta, health = evidence['meta'], verdict['run_health']
    findings = verdict['findings']
    image_budget = [15_000_000]  # At most 20 MB of base64 images across the entire document.
    checks_only = meta.get('mode') == 'checks-only'
    incomplete = health['state'] not in ('complete', 'skipped')
    title = ('Testing incomplete' if incomplete else 'Test skipped' if health['state'] == 'skipped' else
             'Entry checks only' if checks_only else 'Your task review')
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
             'img-src data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             f'<title>{title} | {text(meta.get("title"))}</title><style>{CSS}</style></head><body><main>',
             '<div class="masthead"><span class="brand">swarm-persona</span><span class="edition">Research report · Local evidence</span></div>',
             f'<header><p class="eyebrow">Observe. Understand. Retest.</p><h1>{title}</h1>',
             f'<p>{text(meta.get("title"))}</p><p class="muted">{text(meta.get("url"))}</p>',
             *[f'<p class="muted">{text(n)}</p>' for n in meta.get('notes', [])], '</header>']
    parts.append('<section class="stats" aria-label="Run overview">'
                 f'<div class="stat"><strong>{len(findings)}</strong><span>Findings reported</span></div>'
                 f'<div class="stat"><strong>{health["completed"]}/{health["planned"]}</strong><span>{"Checks" if checks_only else "Attempts"} completed</span></div>'
                 f'<div class="stat"><strong>{len(evidence.get("checks", []))}</strong><span>Entry checks</span></div></section>')
    if incomplete:
        parts.append(f'<section class="notice"><h2>Could not finish testing</h2><p>'
                     f'{health["completed"]} of {health["planned"]} planned attempts returned usable evidence. '
                     'Review the run details and rerun before drawing conclusions. Findings below may be incomplete.</p></section>')
    elif health['state'] == 'skipped':
        parts.append(f'<p class="notice">Nothing ran: {text(meta.get("skipped"))}</p>')
    if checks_only:
        parts.append('<p class="notice">No personas attempted the task. These checks inspect entry pages only; '
                     'they do not test whether a person can complete the task.</p>')
    elif evidence.get('personas') or evidence.get('sessions'):
        goals = list(dict.fromkeys(p.get('goal', '') for p in evidence.get('personas') or evidence['sessions']))
        preview = [p for p in verdict.get('personas', []) if p['target'] == 'preview']
        met = sum(p['result'] == 'pass' for p in preview)
        parts.append('<section class="panel"><h2>The task</h2>' + ''.join(f'<p>{text(g)}</p>' for g in goals)
                     + f'<p><strong>{met} of {len(preview)} agents reported the goal met.</strong></p>'
                     + '<p class="muted">Completion and usability observations are agent-reported. '
                     'They are hypotheses to verify with people, not measurements of human conversion.</p></section>')
    comparison = verdict.get('comparison')
    if comparison:
        parts.append('<section class="panel"><h2>Since the previous test</h2>')
        if not comparison['conclusive']:
            parts.append('<p class="notice">Comparison inconclusive: one or both tests were incomplete.</p>')
        parts.append(f'<p>{text(comparison["seen_again"])} seen again · {text(comparison["not_observed"])} not observed this time · '
                     f'{text(comparison["newly_observed"])} newly observed</p><p class="muted">Counts compare finding groups. '
                     'An observation missing from this run does not prove a fix. Agents can take different paths.</p></section>')
    if findings:
        parts.append(f'<h2>Start with these {min(3, len(findings))}</h2>')
        parts.append('<p class="muted">Suggestions are unverified. Confirm the observation before changing the product.</p>')
        parts.extend(finding(out, f, image_budget) for f in findings[:3])
        if len(findings) > 3:
            parts.append(f'<details><summary>{len(findings) - 3} more findings</summary>')
            parts.extend(finding(out, f, image_budget) for f in findings[3:])
            parts.append('</details>')
    elif not incomplete and health['state'] != 'skipped' and not checks_only:
        parts.append('<p class="panel">No issues reported in this test. This does not establish that the product is usable.</p>')
    if verdict.get('worked_well'):
        parts.append('<section class="panel"><h2>What agents liked</h2><ul>' + ''.join(
            f'<li>{text(w["note"])} <span class="muted">({text(w["persona"])})</span></li>'
            for w in verdict['worked_well']) + '</ul></section>')
    captures = [c for c in evidence.get('checks', []) if c.get('screenshot')]
    missing = len(evidence.get('checks', [])) - len(captures)
    if missing:
        parts.append(f'<p class="muted">{missing} of {len(evidence["checks"])} entry checks have no saved screenshot.</p>')
    if captures:
        parts.append('<section aria-label="Page captures"><h2>Page captures</h2>'
                     '<p class="muted">Entry pages captured by the browser. These show the page at the start of the task.</p><div class="captures">')
        for n, c in enumerate(captures):
            label = f'{c["device"].title()} · {c["target"]} · {c["journey"]} · HTTP {c.get("status") or "unavailable"}'
            parts.append(screenshot(out, c['screenshot'], image_budget, label, eager=n < 2))
        parts.append('</div></section>')
    parts.append('<details><summary>Run details and persona outcomes</summary>')
    parts.append(f'<p>Execution: {text(health["state"])}. {health["completed"]}/{health["planned"]} attempts.</p><ul>')
    for p in verdict.get('personas', []):
        parts.append(f'<li>{text(p["name"])} ({text(p["device"])}, {text(p["target"])}): '
                     f'{text(p["result"])}. {text(p["reason"])}</li>')
    parts.append('</ul>')
    for c in evidence.get('checks', []):
        parts.append(f'<p>Entry check: {text(c["journey"])} ({text(c["device"])}, {text(c["target"])}). '
                     f'Status: {text(c.get("status") or c.get("error"))}</p>')
    run_id = f' Run: {text(meta["run_id"])}.' if meta.get('run_id') else ''
    parts.append(f'<p class="muted">Model: {text(meta.get("model"))}.{run_id}</p></details>')
    embedded = ' Available screenshots are embedded locally.' if image_budget[0] < 15_000_000 else ''
    parts.append(f'<footer class="muted">Generated from saved evidence.{embedded} '
                 'The report loads no external resources. Markdown, JSON and original evidence are beside this file.</footer>')
    return ''.join(parts) + '</main></body></html>'
