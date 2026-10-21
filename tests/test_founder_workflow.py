import asyncio
import json
from pathlib import Path

import pytest
from evidence_builder import evidence, issue, session

from swarm_persona.cli import main, result_code
from swarm_persona.config import load


def test_start_saves_a_reusable_read_only_scenario_without_github(tmp_path):
    path = tmp_path / 'founder.yml'
    with pytest.raises(SystemExit) as stop:
        main(['start', '--url', 'https://notes.example.com/pricing', '--name', 'Notes',
              '--goal', 'Choose a plan for a small team', '--audience', 'First-time founders',
              '--success', 'Explain the selected plan and price', '--provider', 'bedrock',
              '--config', str(path), '--setup-only'])
    assert stop.value.code == 0
    cfg = load(path)
    assert cfg.app.url == 'https://notes.example.com'
    assert cfg.journeys[0].start_path == '/pricing'
    assert cfg.journeys[0].success_criteria == 'Explain the selected plan and price'
    assert cfg.personas.audience == 'First-time founders' and cfg.personas.count == 3
    assert cfg.guard.allowed_writes == [] and not cfg.auth.command


def test_start_refuses_overwrite_before_prompting(tmp_path, monkeypatch):
    path = tmp_path / 'keep.yml'
    path.write_text('keep')
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('must refuse before asking for input'))
    with pytest.raises(SystemExit) as stop:
        main(['start', '--config', str(path), '--setup-only'])
    assert stop.value.code == 2 and path.read_text() == 'keep'


@pytest.mark.parametrize('url', ['http://notes.example.com', 'https://a:b@notes.example.com',
                                'https://notes.example.com/?token=secret', 'https://127.0.0.1',
                                'https://notes.example.com/#secret'])
def test_start_validates_url_before_creating_config(url, tmp_path):
    from swarm_persona.workflow import starter_config

    with pytest.raises(ValueError):
        starter_config(url, 'Choose a plan', 'Founders', 'A plan is selected', 'Notes')
    assert list(tmp_path.iterdir()) == []


def test_incomplete_runs_cannot_be_made_successful_with_no_gate():
    assert result_code({'passed': False, 'run_health': {'state': 'incomplete'}}, True) == 2
    assert result_code({'passed': False, 'run_health': {'state': 'complete'}}, True) == 0


def test_retest_uses_the_original_profiles_and_refuses_changed_goals(monkeypatch, tmp_path):
    from swarm_persona import workflow

    cfg = workflow.starter_config('https://notes.example.com/pricing', 'Choose a plan', 'Founders',
                                  'A plan is selected', 'Notes')
    person = {k: v for k, v in session('preview', 1, journey='first-task',
                                     goal='Choose a plan', start_path='/pricing',
                                     success_criteria='A plan is selected').items()
              if k in ('id', 'name', 'who', 'device', 'patience', 'tech_comfort', 'journey', 'goal',
                       'start_path', 'success_criteria')}
    cfg.personas.count = 1
    ev = evidence(sessions=[session('preview', 1)], baseline_ran=False)
    ev['personas'] = [person]
    ev['meta']['scenario_key'] = workflow.scenario_key(cfg, cfg.journeys)
    ev['meta']['mode'] = 'personas'
    previous = tmp_path / 'before'
    previous.mkdir()
    (previous / 'evidence.json').write_text(json.dumps(ev))
    seen = {}

    async def fake_run(config, **kw):
        seen.update(kw)
        return {'passed': True}

    monkeypatch.setattr('swarm_persona.run.run', fake_run)
    result = asyncio.run(workflow.retest(cfg, previous, tmp_path / 'after', 'https://new.example.com'))
    assert result['passed'] and seen['personas_override'] == [person]
    assert seen['previous']['meta']['scenario_key'] == ev['meta']['scenario_key']
    cfg.journeys[0].goal = 'Explore a different task'
    with pytest.raises(ValueError, match='changed'):
        asyncio.run(workflow.retest(cfg, previous, tmp_path / 'another'))


def test_retest_refuses_old_evidence_and_overlapping_output(tmp_path):
    from swarm_persona.workflow import retest, starter_config

    cfg = starter_config('https://notes.example.com', 'Read pricing', 'Founders', 'Explain pricing', 'Notes')
    old = tmp_path / 'old'
    old.mkdir()
    (old / 'evidence.json').write_text(json.dumps(evidence()))
    with pytest.raises(ValueError):
        asyncio.run(retest(cfg, old, old / 'nested'))
    with pytest.raises(ValueError, match='scenario'):
        asyncio.run(retest(cfg, old, tmp_path / 'after'))


def test_comparison_never_claims_an_unobserved_finding_is_fixed():
    from swarm_persona.workflow import compare

    before = evidence(sessions=[session('preview', 1)], raw=[issue('preview', 1)], baseline_ran=False)
    after = evidence(sessions=[session('preview', 1)], baseline_ran=False)
    result = compare(before, after)
    assert result['not_observed'] == 1 and result['seen_again'] == 0
    assert result['conclusive'] is True
    assert 'fixed' not in json.dumps(result).lower()
    after['sessions'][0]['crashed'] = True
    assert compare(before, after)['conclusive'] is False


def test_guided_start_uses_a_starter_task(tmp_path, monkeypatch):
    answers = iter(['https://notes.example.com/pricing', 'Notes', '1', 'Founders',
                    'Explain the price', 'bedrock'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    path = tmp_path / 'guided.yml'
    with pytest.raises(SystemExit) as stop:
        main(['start', '--config', str(path), '--setup-only'])
    assert stop.value.code == 0
    assert 'plan that fits a small team' in load(path).journeys[0].goal


def test_checks_only_never_invokes_any_model_pass(tmp_path, monkeypatch):
    from swarm_persona import run as runner
    from swarm_persona import checks
    from swarm_persona.workflow import starter_config

    cfg = starter_config('https://notes.example.com', 'Read pricing', 'Founders', 'Explain pricing', 'Notes')
    cfg.report.suggest_fixes = True

    async def forbidden(*args, **kwargs):
        pytest.fail('checks-only must not call any model pass')

    class Proxy:
        server = 'http://127.0.0.1:9999'
        def __init__(self, *args):
            pass
        def start(self):
            pass
        def stop(self):
            pass

    async def entry_checks(cfg, origin, label, targets, *args):
        from evidence_builder import check
        return [check(label, journey=t['journey'], device=t['device']) for t in targets]

    for name in ('plan', 'add_with_model', 'merge', 'add_fixes', 'walk'):
        monkeypatch.setattr(runner, name, forbidden)
    monkeypatch.setattr(runner, 'GuardProxy', Proxy)
    monkeypatch.setattr(runner.preflight, 'run', lambda *args: [])
    monkeypatch.setattr(checks, 'run_checks', entry_checks)
    result = asyncio.run(runner.run(cfg, out=tmp_path / 'run', checks_only=True))
    assert result['run_health']['state'] == 'complete'
    assert (tmp_path / 'run' / 'report.html').exists()
    assert json.loads((tmp_path / 'run' / 'evidence.json').read_text())['meta']['tokens'] == 0


def test_product_context_is_saved_and_reaches_the_walker(tmp_path):
    from swarm_persona.agent import task_prompt
    path = tmp_path / 'context.yml'
    with pytest.raises(SystemExit) as stop:
        main(['start', '--url', 'https://notes.example.com', '--name', 'Notes',
              '--description', 'Shared notes for small design studios.', '--goal', 'Read pricing',
              '--audience', 'Founders', '--success', 'Explain pricing', '--provider', 'bedrock',
              '--config', str(path), '--setup-only'])
    assert stop.value.code == 0
    cfg = load(path)
    assert cfg.app.description == 'Shared notes for small design studios.'
    assert cfg.app.description in task_prompt(cfg, {'title': ''}, session('preview', 1), cfg.app.url)


def test_retest_runs_the_recorded_plan_without_replanning(tmp_path, monkeypatch):
    from swarm_persona import run as runner, checks, agent
    from swarm_persona.workflow import retest, starter_config
    from swarm_persona.config import SavedPersona

    cfg = starter_config('https://notes.example.com', 'Read pricing', 'Founders', 'Explain pricing', 'Notes')
    cfg.personas.saved = [SavedPersona(name=f'Reader {i}', background='Runs a small studio') for i in range(3)]

    class Proxy:
        server = 'http://127.0.0.1:9999'
        def __init__(self, *args):
            pass
        def start(self):
            pass
        def stop(self):
            pass

    async def entry_checks(cfg, origin, label, targets, *args):
        from evidence_builder import check
        return [check(label, journey=t['journey'], device=t['device']) for t in targets]

    seen = []
    async def attempt(cfg, ctx, person, label, *args):
        seen.append(person)
        return {**session(label, person['id']), **person, 'issues': []}

    monkeypatch.setattr(runner, 'GuardProxy', Proxy)
    monkeypatch.setattr(runner.preflight, 'run', lambda *args: [])
    monkeypatch.setattr(checks, 'run_checks', entry_checks)
    monkeypatch.setattr(agent, 'run_persona', attempt)
    first = tmp_path / 'first'
    assert asyncio.run(runner.run(cfg, out=first, baseline=False))['run_health']['state'] == 'complete'
    recorded = json.loads((first / 'evidence.json').read_text())['personas']
    assert len(recorded) == 3

    async def no_replan(*args):
        pytest.fail('retesting must reuse the saved plan')
    monkeypatch.setattr(runner, 'plan', no_replan)
    result = asyncio.run(retest(cfg, first, tmp_path / 'second'))
    assert seen[3:] == recorded
    assert result['comparison']['conclusive'] and result['run_health']['state'] == 'complete'
    assert 'Since the previous test' in (tmp_path / 'second' / 'report.html').read_text()


def test_retest_preserves_the_original_baseline_and_budget(monkeypatch, tmp_path):
    from swarm_persona import workflow
    cfg = workflow.starter_config('https://notes.example.com', 'Read pricing', 'Founders', 'Explain pricing', 'Notes')
    cfg.app.baseline_url = 'https://base.example.com'
    person = session('preview', 1, journey='first-task', goal='Read pricing', start_path='/',
                     success_criteria='Explain pricing')
    ev = evidence(sessions=[person, {**person, 'target': 'baseline'}])
    ev['personas'] = [person]
    ev['meta'].update(scenario_key=workflow.scenario_key(cfg, cfg.journeys), targets=['preview', 'baseline'],
                      baseline_url=cfg.app.baseline_url)
    old = tmp_path / 'old'
    old.mkdir()
    (old / 'evidence.json').write_text(json.dumps(ev))
    seen = {}
    async def fake_run(config, **kw):
        seen.update(kw)
        return {}
    monkeypatch.setattr('swarm_persona.run.run', fake_run)
    asyncio.run(workflow.retest(cfg, old, tmp_path / 'new'))
    assert seen['baseline'] is True
    cfg.budget.max_total_steps += 1
    with pytest.raises(ValueError, match='changed'):
        asyncio.run(workflow.retest(cfg, old, tmp_path / 'new-budget'))
    cfg.budget.max_total_steps -= 1
    cfg.app.baseline_url = 'https://different.example.com'
    with pytest.raises(ValueError, match='baseline'):
        asyncio.run(workflow.retest(cfg, old, tmp_path / 'new-baseline'))


def test_start_refuses_used_output_before_saving_a_config(tmp_path, monkeypatch):
    config = tmp_path / 'new.yml'
    out = tmp_path / 'used'
    out.mkdir()
    (out / 'evidence.json').write_text('{}')
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('refuse before prompting'))
    with pytest.raises(SystemExit) as stop:
        main(['start', '--config', str(config), '--out', str(out)])
    assert stop.value.code == 2 and not config.exists()


def test_setup_rejects_missing_storage_state():
    from swarm_persona.workflow import starter_config
    with pytest.raises(ValueError, match='storage state'):
        starter_config('https://notes.example.com', 'Read pricing', 'Founders', 'Explain pricing',
                       'Notes', storage_state='/does/not/exist.json')


def test_budget_cuts_keep_the_original_plan_for_completeness(tmp_path, monkeypatch):
    from swarm_persona import run as runner, checks
    from swarm_persona.workflow import starter_config
    from swarm_persona.config import SavedPersona
    cfg = starter_config('https://notes.example.com', 'Read pricing', 'Founders', 'Explain pricing', 'Notes')
    cfg.personas.saved = [SavedPersona(name=f'Reader {i}', background='Runs a studio') for i in range(3)]
    cfg.budget.max_total_steps = 15
    class Proxy:
        server = 'http://127.0.0.1:9999'
        def __init__(self, *args):
            pass
        def start(self):
            pass
        def stop(self):
            pass
    async def entry_checks(*args):
        return []
    async def walk(config, ctx, personas, label, *args):
        return [{**session(label, p['id']), **p, 'issues': []} for p in personas]
    monkeypatch.setattr(runner, 'GuardProxy', Proxy)
    monkeypatch.setattr(runner.preflight, 'run', lambda *args: [])
    monkeypatch.setattr(checks, 'run_checks', entry_checks)
    monkeypatch.setattr(runner, 'walk', walk)
    out = tmp_path / 'cut'
    result = asyncio.run(runner.run(cfg, out=out, baseline=False))
    assert result['run_health']['planned'] == 3 and result['run_health']['completed'] == 1
    assert result['run_health']['state'] == 'incomplete'
    assert len(json.loads((out / 'evidence.json').read_text())['personas']) == 3
