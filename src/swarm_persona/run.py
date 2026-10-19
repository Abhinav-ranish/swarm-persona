"""One run: select journeys, plan personas, prove the guard, check entry pages, walk the journeys on
the preview (and the baseline), merge, judge, report. Every step's evidence lands in evidence.json
so `swarm-persona replay` can rebuild the verdict without a browser or a model."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from . import __version__, preflight
from .config import Config, https_origin
from .fixes import pr_diff, suggest
from .guard.proxy import GuardProxy
from .journeys import add_with_model, select
from .judge import judge
from .merge import merge
from .planner import plan, pr_context
from .report import render, write


class Session:
    """Sign-in for one target: a storage state from the auth command or a file, and sign-out."""

    def __init__(self, cfg: Config, origin: str, private: Path, label: str) -> None:
        self.cfg, self.origin = cfg, origin
        self.vars = {'url': origin, 'state': str(private / f'{label}.state.json'),
                     'tokens': str(private / f'{label}.tokens.json')}
        self.path: str | None = None

    def command(self, template: str) -> None:
        # Only the three placeholders are replaced: braces anywhere else (JSON) are left alone.
        argv = [re.sub(r'\{(url|state|tokens)\}', lambda m: self.vars[m.group(1)], part)
                for part in shlex.split(template)]
        subprocess.run(argv, check=True)

    def __enter__(self) -> 'Session':
        if self.cfg.auth.command:
            try:
                self.command(self.cfg.auth.command)
            except BaseException:
                # The command may have opened a session before failing; a with-block whose
                # __enter__ raises never reaches __exit__, so sign out here.
                self.__exit__()
                raise
            self.path = self.vars['state']
        elif self.cfg.auth.storage_state:
            self.path = str(Path(self.cfg.auth.storage_state).expanduser())
        return self

    def __exit__(self, *exc) -> None:
        if self.cfg.auth.sign_out_command:
            try:
                self.command(self.cfg.auth.sign_out_command)
            except subprocess.CalledProcessError as error:
                print(f'WARNING: sign-out for {self.origin} failed ({error.returncode})', flush=True)
        for key in ('state', 'tokens'):
            Path(self.vars[key]).unlink(missing_ok=True)

    def state(self) -> dict | None:
        return json.loads(Path(self.path).read_text()) if self.path else None


def check_targets(cfg: Config, journeys: list) -> list[dict]:
    if journeys:
        return [{'journey': j.name, 'path': j.start_path, 'device': d, 'selectors': j.critical_selectors}
                for j in journeys for d in j.devices]
    return [{'journey': r, 'path': r, 'device': d, 'selectors': []} for r in cfg.app.routes for d in ('desktop', 'mobile')]


def fit_budget(cfg: Config, personas: list[dict], targets: int, notes: list[str]) -> list[dict]:
    most = max(1, cfg.budget.max_total_steps // (cfg.personas.max_steps * targets))
    if len(personas) > most:
        notes.append(f'Budget: ran {most} of {len(personas)} planned personas '
                     f'(max_total_steps {cfg.budget.max_total_steps}).')
        return personas[:most]
    return personas


async def walk(cfg: Config, ctx: dict, personas: list[dict], label: str, origin: str, proxies: list[GuardProxy],
               state: dict | None, shots: Path, spent: dict, notes: list[str], headed: bool) -> list[dict]:
    from .agent import run_persona

    gate = asyncio.Semaphore(cfg.personas.concurrency)

    async def one(i: int, persona: dict):
        async with gate:
            if spent['tokens'] >= cfg.budget.max_tokens:
                notes.append(f"Budget: skipped {persona['name']} on {label}, {spent['tokens']} tokens spent.")
                return None
            print(f"  [{label}] start {persona['name']} ({persona['device']}): {persona['goal'][:100]}", flush=True)
            result = await run_persona(cfg, ctx, persona, label, origin, proxies[i], state, shots, headed)
            spent['tokens'] += result['tokens']
            print(f"  [{label}] done  {persona['name']}: {result['steps']} steps, {result['seconds']}s", flush=True)
            return result

    tasks = [asyncio.create_task(one(i, p)) for i, p in enumerate(personas)]
    done, pending = await asyncio.wait(tasks, timeout=cfg.budget.timeout_minutes * 60)
    for task in pending:
        task.cancel()
    if pending:
        notes.append(f'Timeout: {len(pending)} persona run(s) on {label} were cut off at '
                     f'{cfg.budget.timeout_minutes} minutes.')
        await asyncio.gather(*pending, return_exceptions=True)
    results = []
    for persona, task in zip(personas, tasks):
        if task not in done:
            continue
        if task.exception():  # a persona that broke before its agent ran is reported, never dropped
            error = task.exception()
            print(f"  [{label}] {persona['name']} failed: {type(error).__name__}: {error}", flush=True)
            results.append({**persona, 'target': label, 'verdict': f'CRASHED: {type(error).__name__}: {str(error)[:200]}',
                            'achieved': False, 'crashed': True, 'done': False, 'success': None,
                            'worked_well': [], 'steps': 0, 'tokens': 0, 'seconds': 0,
                            'issues': [], 'guard_blocked': [], 'server_errors': []})
        elif task.result():
            results.append(task.result())
    if personas and results and all(r['crashed'] for r in results):
        notes.append(f'Every persona on {label} crashed; nothing was walked. See the Sessions table.')
    return results


async def run(cfg: Config, *, out: Path, pr: str | None = None, pr_context_file: str | None = None,
              url: str | None = None, baseline: bool = True, model_selection: bool = True,
              headed: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    shots, logs = out / 'shots', out / 'guard'
    shots.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    preview_url = https_origin(url) if url else cfg.app.url
    targets = [('preview', preview_url)]
    if baseline and cfg.app.baseline_url and cfg.app.baseline_url != preview_url:
        targets.append(('baseline', cfg.app.baseline_url))
    meta = {'title': cfg.report.title, 'pr': pr, 'url': preview_url, 'baseline_url': cfg.app.baseline_url,
            'app_hosts': sorted(cfg.app_hosts(preview_url)),
            'model': f'{cfg.llm.provider}:{cfg.llm.model}', 'version': __version__,
            'run_id': time.strftime('%Y%m%d-%H%M%S') + '-' + os.urandom(3).hex(),
            'images_repo': cfg.report.images_repo, 'max_steps': cfg.personas.max_steps,
            'fail_on': cfg.report.fail_on, 'fail_on_unclassified': cfg.report.fail_on_unclassified,
            'no_em_dashes': cfg.report.no_em_dashes, 'notes': []}
    notes, spent = meta['notes'], {'tokens': 0}

    ctx = pr_context(pr, pr_context_file)
    selection = select(cfg, ctx['files'])
    if cfg.journeys and model_selection and ctx['files']:
        spent['tokens'] += await add_with_model(cfg, selection, ctx)
    meta['selection'] = selection.reasons
    if cfg.journeys and not selection.journeys:
        meta['skipped'] = 'this change touches no configured journey.'
        evidence = {'meta': meta, 'baseline_ran': False, 'checks': [], 'sessions': [], 'raw_issues': [], 'merge': None}
        return finish(cfg, out, evidence)

    personas, tokens = await plan(cfg, ctx, selection.journeys)
    spent['tokens'] += tokens
    personas = fit_budget(cfg, personas, len(targets), notes)
    print('Personas: ' + '; '.join(f"{p['name']} ({p['device']}, {p['journey'] or p['start_path']})" for p in personas),
          flush=True)

    private = Path(tempfile.mkdtemp(prefix='swarm-persona-'))  # CA key, sessions: never under --out
    os.chmod(private, 0o700)
    checks, sessions = [], []
    sandbox = os.environ.get('SWARM_PERSONA_NO_SANDBOX') != '1'
    try:
        for label, origin in targets:
            with ExitStack() as stack:
                session = stack.enter_context(Session(cfg, origin, private, label))
                passed = preflight.run(cfg, origin, session.path)
                if passed:
                    print(f'Preflight passed on {label}: {", ".join(passed)}', flush=True)
                state = session.state()
                proxies = [GuardProxy(cfg, origin, private, logs, f'{label}-{p["id"]}') for p in personas]
                checker = GuardProxy(cfg, origin, private, logs, f'{label}-checks')
                for proxy in [checker, *proxies]:
                    stack.callback(proxy.stop)
                    proxy.start()
                print(f'Guard proxies up on {label} and refusing writes ({len(proxies) + 1}).', flush=True)
                from .checks import run_checks

                checks += await run_checks(cfg, origin, label, check_targets(cfg, selection.journeys),
                                           checker.server, state, shots, sandbox)
                sessions += await walk(cfg, ctx, personas, label, origin, proxies, state, shots, spent, notes, headed)
    finally:
        shutil.rmtree(private, ignore_errors=True)

    raw = [i for s in sessions for i in s['issues']]
    preview_raw = [i for i in raw if i['target'] == 'preview']
    merged, tokens = await merge(cfg, ctx, preview_raw, [s for s in sessions if s['target'] == 'preview'])
    spent['tokens'] += tokens
    if merged is None:
        notes.append('The merge pass reply was unusable, so every raw report is listed unmerged.')
    evidence = {'meta': meta, 'baseline_ran': any(s['target'] == 'baseline' for s in sessions),
                'ctx': {'title': ctx['title'], 'files': ctx['files']}, 'personas': personas,
                'checks': checks, 'sessions': [{k: v for k, v in s.items() if k != 'issues'} for s in sessions],
                'raw_issues': raw, 'merge': merged}
    evidence['fixes'] = await add_fixes(cfg, evidence, pr, pr_context_file, spent, notes)
    meta['tokens'] = spent['tokens']
    return finish(cfg, out, evidence)


async def add_fixes(cfg: Config, evidence: dict, pr: str | None, pr_context_file: str | None, spent: dict,
                    notes: list[str]) -> dict:
    """Possible fixes for the judged findings. Never fails the run: on any error, a note and no fixes."""
    if not cfg.report.suggest_fixes:
        return {}
    try:
        diff = pr_diff(pr, pr_context_file)
        if not diff.strip():
            return {}
        findings = judge(evidence, cfg.report.fail_on, cfg.report.fail_on_unclassified)['findings']
        return await suggest(cfg, findings, diff, spent, notes)
    except (Exception, SystemExit) as error:  # a missing API key raises SystemExit
        notes.append(f'Possible fixes were skipped: {type(error).__name__}.')
        return {}


def finish(cfg: Config, out: Path, evidence: dict) -> dict:
    (out / 'evidence.json').write_text(json.dumps(evidence, indent=2, default=str))
    return report_from(cfg.report.fail_on, cfg.report.fail_on_unclassified, cfg.report.no_em_dashes, out, evidence)


def report_from(fail_on: list[str], fail_unclassified: bool, no_em_dashes: bool, out: Path, evidence: dict,
                images: dict[str, str] | None = None) -> dict:
    verdict = judge(evidence, fail_on, fail_unclassified)
    preview_raw = [i for i in evidence['raw_issues'] if i['target'] == 'preview']
    dropped = [{**d, 'title': preview_raw[d['id']]['title']} for d in (evidence.get('merge') or {}).get('dropped', [])
               if d['id'] < len(preview_raw)]
    meta = evidence['meta']
    text = render(meta, verdict, evidence['sessions'], evidence['checks'], dropped, no_em_dashes, images)
    write(out, text, {'version': meta.get('version'), 'meta': meta, **verdict})
    return {'report': text, **verdict}


def replay(out: Path, fail_on: list[str] | None = None, fail_unclassified: bool | None = None) -> dict:
    """Rebuild report.md and report.json from evidence.json alone: no browser, no model."""
    evidence = json.loads((out / 'evidence.json').read_text())
    meta = evidence['meta']
    if fail_unclassified is None:
        fail_unclassified = bool(meta.get('fail_on_unclassified'))
    return report_from(fail_on or meta['fail_on'], fail_unclassified, bool(meta.get('no_em_dashes')), out, evidence)


def publish(out: Path, pr: str, repo: str | None = None, images_repo: str | None = None,
            app_file: str | None = None) -> str:
    """Upload the run's screenshots (when an images repository is set), rebuild the report with
    them embedded, and create or update the sticky PR comment. No browser, no model."""
    import hashlib

    from .github import Client, repo_name, upload_images, upsert_comment

    evidence = json.loads((out / 'evidence.json').read_text())
    meta = evidence['meta']
    repo = repo or repo_name()
    images_repo = images_repo or meta.get('images_repo')
    images = {}
    if images_repo:
        run_id = meta.get('run_id') or hashlib.sha256((out / 'evidence.json').read_bytes()).hexdigest()[:16]
        wanted = {Path(s).name for f in evidence['raw_issues'] for s in [f.get('screenshot')] if s}
        wanted |= {Path(c['screenshot']).name for c in evidence['checks'] if c.get('screenshot')}
        files = sorted(p for p in (out / 'shots').glob('*.png') if p.name in wanted)[:40]
        images = upload_images(Client.from_cli(), images_repo, files, f"{repo.replace('/', '-')}/pr-{pr}/{run_id}")
    result = report_from(meta['fail_on'], bool(meta.get('fail_on_unclassified')), bool(meta.get('no_em_dashes')),
                         out, evidence, images)
    client = Client.from_app(json.loads(Path(app_file).read_text()), repo) if app_file else Client.from_cli()
    return upsert_comment(client, repo, pr, result['report'])
