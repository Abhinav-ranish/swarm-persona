"""Local first-run setup and repeatable founder tests, without a repository integration."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .config import Config, Journey, LLM, https_origin
from .guard.policy import internal_host
from .judge import judge

STARTERS = {
    '1': ('Understand pricing', 'Find the plan that fits a small team and explain its price and limits.'),
    '2': ('Get started', 'Find how to get started and identify the first useful action, without creating an account.'),
    '3': ('Find a feature', 'Find the main feature and explain how it would help complete your task.'),
}


def starter_config(url: str, goal: str, audience: str, success: str, name: str,
                   provider: str = 'bedrock', model: str | None = None,
                   storage_state: str | None = None, description: str | None = None) -> Config:
    parts = urlsplit(url.strip())
    if parts.username or parts.password or parts.query or parts.fragment or internal_host(parts.hostname or ''):
        raise ValueError('Use a public HTTPS page URL without credentials, query parameters or fragments.')
    origin = https_origin(f'{parts.scheme}://{parts.netloc}')
    path = parts.path or '/'
    for value, label in ((goal, 'task'), (audience, 'audience'), (success, 'success criteria'), (name, 'name')):
        if not value.strip():
            raise ValueError(f'{label} must not be empty')
    if provider != 'bedrock' and not model:
        raise ValueError('Specify a model ID for this provider.')
    if storage_state and not Path(storage_state).expanduser().is_file():
        raise ValueError('The storage state file does not exist.')
    llm = {'provider': provider, 'model': model or LLM().model}
    if provider in ('openai', 'anthropic'):
        llm['api_key_env'] = 'OPENAI_API_KEY' if provider == 'openai' else 'ANTHROPIC_API_KEY'
    return Config.model_validate({
        'app': {'name': name.strip(), 'description': description or f'{name.strip()}: evaluate first-time task completion.',
                'url': origin, 'routes': [path]},
        'journeys': [{'name': 'first-task', 'goal': goal.strip(), 'start_path': path,
                      'success_criteria': success.strip()}],
        'personas': {'count': 3, 'max_steps': 15, 'audience': audience.strip()},
        'llm': llm, 'auth': {'storage_state': storage_state},
        'report': {'title': 'First-time user review'},
    })


def configure(args) -> Config:
    path = Path(args.config)
    if path.exists():
        raise ValueError(f'{path} exists; choose a new config path')
    if not args.setup_only and (Path(args.out) / 'evidence.json').exists():
        raise ValueError('The output directory already contains a run; choose a new directory.')
    url = args.url or input('Public HTTPS page URL: ').strip()
    name = args.name or input('Product name: ').strip()
    goal = args.goal
    if not goal:
        for key, (label, _) in STARTERS.items():
            print(f'{key}. {label}')
        choice = input('Choose a task (1-3), or describe your own: ').strip()
        if choice.isdigit() and choice not in STARTERS:
            raise ValueError('Choose task 1, 2 or 3, or describe a task in words.')
        goal = STARTERS[choice][1] if choice in STARTERS else choice
    audience = args.audience or input('Who is trying this task? ').strip()
    success = args.success or input('What should they understand or accomplish? ').strip()
    provider = args.provider or (input('Model provider (bedrock, anthropic, openai) [bedrock]: ').strip()
                                 if sys.stdin.isatty() else '') or 'bedrock'
    model = args.model
    if provider != 'bedrock' and not model:
        model = input('Model ID from your provider: ').strip()
    cfg = starter_config(url, goal, audience, success, name, provider, model, args.storage_state, args.description)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as file:
        yaml.safe_dump(cfg.model_dump(mode='json'), file, sort_keys=False)
    print(f'Saved {path}. Reuse this file to keep the task, audience and success criteria consistent.')
    print('Writes are blocked. For a flow that saves drafts, configure its exact allowed requests first.')
    if args.checks_only:
        print('Plan: entry-page checks on desktop and mobile-sized Chromium, no personas or model calls.')
    else:
        print('The configured model receives page content and screenshots during testing.')
        print(f'Plan: 3 personas, up to {cfg.personas.max_steps} steps each, desktop and mobile-sized Chromium.')
    print('Results stay in the output folder.')
    return cfg


def scenario_key(cfg: Config, journeys: list[Journey]) -> str:
    """A comparison contract, separate from the public finding/ticket fingerprint."""
    scenario = {'name': cfg.app.name, 'description': cfg.app.description, 'config_url': cfg.app.url,
                'hosts': cfg.app.hosts, 'auth': cfg.auth.model_dump(mode='json'),
                'preflight': [p.model_dump(mode='json') for p in cfg.preflight],
                'journeys': [j.model_dump(mode='json') for j in journeys],
                'personas': cfg.personas.model_dump(mode='json'), 'guard': cfg.guard.model_dump(mode='json'),
                'budget': cfg.budget.model_dump(mode='json'),
                'llm': cfg.llm.model_dump(mode='json', exclude={'api_key_env'})}
    return hashlib.sha256(json.dumps(scenario, sort_keys=True).encode()).hexdigest()


def compare(before: dict, after: dict) -> dict:
    old = judge(before, before['meta'].get('fail_on', ['blocker']))
    new = judge(after, after['meta'].get('fail_on', ['blocker']))
    previous = {f['fingerprint'] for f in old['findings']}
    current = {f['fingerprint'] for f in new['findings']}
    return {'previous_run': before['meta'].get('run_id'),
            'conclusive': (old['run_health']['state'] == new['run_health']['state'] == 'complete'
                           and old['run_health']['targets'].keys() == new['run_health']['targets'].keys()),
            'seen_again': len(previous & current), 'not_observed': len(previous - current),
            'newly_observed': len(current - previous)}


async def retest(cfg: Config, previous: Path, out: Path, url: str | None = None) -> dict:
    from .run import run

    old_path, new_path = previous.resolve(), out.resolve()
    if old_path == new_path or old_path in new_path.parents or new_path in old_path.parents:
        raise ValueError('Retest output must be separate from the original run directory.')
    if (out / 'evidence.json').exists():
        raise ValueError('Retest output already contains a run; choose a new directory.')
    old = json.loads((previous / 'evidence.json').read_text())
    if not old['meta'].get('scenario_key'):
        raise ValueError('This run predates saved scenario metadata; start a fresh run first.')
    personas = old.get('personas')
    checks_only = old['meta'].get('mode') == 'checks-only'
    baseline = 'baseline' in old['meta'].get('targets', [])
    if baseline and cfg.app.baseline_url != old['meta'].get('baseline_url'):
        raise ValueError('The baseline changed; start a fresh run instead.')
    if not isinstance(personas, list) or (not personas and not checks_only):
        raise ValueError('The original run has no reusable persona plan; start a fresh run first.')
    names = {p.get('journey') for p in personas} if personas else {c['journey'] for c in old['checks']}
    selected = [j for j in cfg.journeys if j.name in names]
    if not selected or scenario_key(cfg, selected) != old['meta']['scenario_key']:
        raise ValueError('The scenario, audience, model or request policy changed; start a fresh run instead.')
    for person in personas:
        journey = next((j for j in selected if j.name == person.get('journey')), None)
        if (not journey or person.get('goal') != journey.goal or person.get('start_path') != journey.start_path
                or person.get('success_criteria') != journey.success_criteria
                or person.get('device') not in journey.devices):
            raise ValueError('The saved persona plan changed; start a fresh run instead.')
    cfg = cfg.model_copy(deep=True)
    cfg.journeys = selected
    return await run(cfg, out=out, url=url, baseline=baseline, model_selection=False, checks_only=checks_only,
                     personas_override=personas, previous=old)
