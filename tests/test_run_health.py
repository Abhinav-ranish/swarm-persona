import asyncio
import json

import pytest

from evidence_builder import check, evidence, session

from swarm_persona.config import Config
from swarm_persona.judge import judge
from swarm_persona.run import replay, walk


def test_crashes_are_test_failures_not_a_clean_product_result(tmp_path):
    ev = evidence(sessions=[session('preview', 1, crashed=True, success=None, steps=0)])
    result = judge(ev, ['blocker'])
    assert result['gate_passed'] is True
    assert result['passed'] is False
    assert result['run_health']['state'] == 'incomplete'
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    assert 'Testing incomplete' in replay(tmp_path)['report']


def test_missing_planned_baseline_seats_make_the_run_incomplete():
    ev = evidence(sessions=[session('preview', 1)])
    ev['personas'] = [{'id': 1}]
    ev['meta']['targets'] = ['preview', 'baseline']
    health = judge(ev, ['blocker'])['run_health']
    assert health['state'] == 'incomplete'
    assert health['planned'] == 2 and health['completed'] == 1
    assert health['targets']['baseline']['completed'] == 0


def test_empty_evidence_is_not_a_success_but_intentional_skip_is():
    ev = evidence(baseline_ran=False)
    assert judge(ev, ['blocker'])['passed'] is False
    ev['meta']['skipped'] = 'no affected journey'
    assert judge(ev, ['blocker'])['run_health']['state'] == 'skipped'
    assert judge(ev, ['blocker'])['passed'] is True


def test_a_completed_attempt_that_did_not_meet_the_goal_is_still_a_valid_test():
    ev = evidence(sessions=[session('preview', 1, achieved=False)])
    result = judge(ev, ['blocker'])
    assert result['run_health']['state'] == 'complete'
    assert result['personas'][0]['result'] == 'fail'


def test_explicit_checks_only_mode_does_not_require_personas():
    ev = evidence(checks=[check('preview')], baseline_ran=False)
    ev['meta'].update(mode='checks-only', targets=['preview'], checks_per_target=1)
    result = judge(ev, ['blocker'])
    assert result['run_health']['state'] == 'complete'
    assert result['passed'] is True


def test_walk_records_budget_skips_and_timeouts(monkeypatch, tmp_path):
    from swarm_persona import agent

    cfg = Config.model_validate({'app': {'name': 'Notes', 'description': 'Notes', 'url': 'https://app.example.com'}})
    persona = session('preview', 1)
    skipped = asyncio.run(walk(cfg, {}, [persona], 'preview', cfg.app.url, [None], None,
                               tmp_path, {'tokens': cfg.budget.max_tokens}, [], False))
    assert len(skipped) == 1 and skipped[0]['status'] == 'budget-skipped'

    async def stuck(*args, **kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(agent, 'run_persona', stuck)
    cfg.budget.timeout_minutes = 0
    timed = asyncio.run(walk(cfg, {}, [persona], 'preview', cfg.app.url, [None], None,
                             tmp_path, {'tokens': 0}, [], False))
    assert len(timed) == 1 and timed[0]['status'] == 'timed-out'


def test_unattempted_personas_do_not_become_product_journey_failures():
    ev = evidence(sessions=[session('preview', 1, status='budget-skipped', done=False,
                                   success=None, achieved=False, steps=0), session('baseline', 1)])
    result = judge(ev, ['blocker'])
    assert result['run_health']['state'] == 'incomplete'
    assert not any(f['source'] == 'journey' for f in result['findings'])


@pytest.mark.parametrize('max_steps', [3, 25])
def test_provider_step_errors_are_incomplete_execution(monkeypatch, tmp_path, max_steps):
    from types import SimpleNamespace
    import browser_use
    from swarm_persona import agent

    class Proxy:
        server = 'http://127.0.0.1:1'
        def records(self, kind):
            return []

    async def broken_model(self, max_steps):
        return SimpleNamespace(history=[None] * 3, usage=None, final_result=lambda: None,
                               is_done=lambda: False, is_successful=lambda: None,
                               errors=lambda: ['model call failed'] * 3)

    monkeypatch.setattr(browser_use.Agent, 'run', broken_model)
    cfg = Config.model_validate({'app': {'name': 'Notes', 'description': 'Notes', 'url': 'https://app.example.com'}})
    cfg.personas.max_steps = max_steps
    result = asyncio.run(agent.run_persona(cfg, {'title': ''}, session('preview', 1), 'preview',
                                           cfg.app.url, Proxy(), None, tmp_path))
    assert result['status'] == 'agent-error' and result['step_error_count'] == 3
    judged = judge(evidence(sessions=[result]), ['blocker'])
    assert judged['run_health']['state'] == 'incomplete'
    assert judged['personas'][0]['result'] == 'not-run'
    assert not any(f['source'] == 'journey' for f in judged['findings'])


def test_incomplete_markdown_keeps_the_defect_gate_visible(tmp_path):
    ev = evidence(checks=[check('preview', status=500), check('baseline')],
                  sessions=[session('preview', 1, crashed=True, steps=0)])
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    result = replay(tmp_path, fail_on=['blocker'])
    assert result['gate_passed'] is False
    assert 'Testing incomplete' in result['report'] and '**Failed**' in result['report']


@pytest.mark.parametrize('model_failed', [False, True])
def test_step_limit_distinguishes_product_actions_from_provider_failures(monkeypatch, tmp_path, model_failed):
    from types import SimpleNamespace
    import browser_use
    from swarm_persona import agent
    class Proxy:
        server = 'http://127.0.0.1:1'
        def records(self, kind):
            return []
    async def step_limited(self, max_steps):
        history = [SimpleNamespace(model_output=object()) for _ in range(max_steps)]
        if model_failed:
            history[-1].model_output = None
        return SimpleNamespace(history=history, usage=None,
                               final_result=lambda: None, is_done=lambda: False, is_successful=lambda: None,
                               errors=lambda: [None] * (max_steps - 1) + ['Element no longer exists'])
    monkeypatch.setattr(browser_use.Agent, 'run', step_limited)
    cfg = Config.model_validate({'app': {'name': 'Notes', 'description': 'Notes', 'url': 'https://app.example.com'}})
    result = asyncio.run(agent.run_persona(cfg, {'title': ''}, session('preview', 1), 'preview',
                                           cfg.app.url, Proxy(), None, tmp_path))
    assert result['status'] == ('agent-error' if model_failed else 'step-limit')
    judged = judge(evidence(sessions=[result]), ['blocker'])
    assert judged['run_health']['state'] == ('incomplete' if model_failed else 'complete')
    assert judged['personas'][0]['result'] == ('not-run' if model_failed else 'fail')
