"""Behavioral regressions for execution, retests, report safety and command integration."""
import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from evidence_builder import check, evidence, issue, session
from test_mcp import EXAMPLE, finish, home  # noqa: F401  (home is a fixture)

from swarm_persona import mcp_server, workflow
from swarm_persona import run as runner
from swarm_persona.cli import main, result_code
from swarm_persona.config import Config
from swarm_persona.judge import judge, outcome
from swarm_persona.run import replay
from swarm_persona.workflow import starter_config

PNG = b'\x89PNG\r\n\x1a\n' + b'fixture'
UNFINISHED = ('budget-skipped', 'timed-out', 'crashed', 'agent-error', 'interrupted')
NOT_RUN = ('budget-skipped', 'timed-out', 'agent-error', 'interrupted')


# ---- exit codes -------------------------------------------------------------------------------

@pytest.mark.parametrize('result, no_gate, code', [
    ({'passed': False, 'run_health': {'state': 'complete'}}, False, 1),
    ({'passed': True, 'run_health': {'state': 'skipped'}}, False, 0),
    ({'passed': True, 'run_health': {'state': 'complete'}}, False, 0),
    ({'passed': False, 'run_health': {'state': 'not-tested'}}, True, 2),
    ({'passed': True}, False, 2),
])
def test_result_code_table(result, no_gate, code):
    assert result_code(result, no_gate) == code


def fake_result(state, passed):
    return {'report': 'r', 'passed': passed, 'run_health': {'state': state}}


def test_run_cli_uses_result_code_and_forwards_checks_only(monkeypatch, tmp_path):
    seen = []

    async def fake_run(cfg, **kw):
        seen.append(kw)
        return fake_result(*results.pop(0))

    results = [('incomplete', False), ('complete', False), ('complete', False), ('complete', True)]
    monkeypatch.setattr('swarm_persona.run.run', fake_run)
    base = ['run', '--config', str(EXAMPLE)]
    for extra, code in [([], 2), ([], 1), (['--no-gate'], 0), (['--checks-only'], 0)]:
        with pytest.raises(SystemExit) as stop:
            main(base + ['--out', str(tmp_path / f'o{len(seen)}')] + extra)
        assert stop.value.code == code
    assert [kw['checks_only'] for kw in seen] == [False, False, False, True]


def test_replay_cli_exits_2_for_incomplete_evidence(tmp_path):
    (tmp_path / 'evidence.json').write_text(json.dumps(evidence(sessions=[session('preview', 1, crashed=True, steps=0)])))
    with pytest.raises(SystemExit) as stop:
        main(['replay', str(tmp_path)])
    assert stop.value.code == 2


def test_start_cli_runs_without_baseline_forwards_checks_only_and_exits_2_when_incomplete(monkeypatch, tmp_path):
    seen = {}

    async def fake_run(cfg, **kw):
        seen.update(kw)
        return fake_result('incomplete', False)

    monkeypatch.setattr('swarm_persona.run.run', fake_run)
    with pytest.raises(SystemExit) as stop:
        main(['start', '--url', 'https://notes.example.com', '--name', 'Notes', '--goal', 'g', '--audience', 'a',
              '--success', 's', '--provider', 'bedrock', '--config', str(tmp_path / 'c.yml'),
              '--out', str(tmp_path / 'o'), '--checks-only'])
    assert stop.value.code == 2 and seen['checks_only'] is True and seen['baseline'] is False


def test_retest_cli_exits_2_when_the_retest_is_incomplete(monkeypatch, tmp_path):
    async def fake_retest(cfg, previous, out, url=None):
        return fake_result('incomplete', False)

    monkeypatch.setattr('swarm_persona.workflow.retest', fake_retest)
    with pytest.raises(SystemExit) as stop:
        main(['retest', str(tmp_path), '--config', str(EXAMPLE), '--out', str(tmp_path / 'new')])
    assert stop.value.code == 2


# ---- run health / judge -----------------------------------------------------------------------

def test_a_session_that_neither_finished_nor_stepped_is_not_a_completed_attempt():
    ev = evidence(sessions=[session('preview', 1, done=False, steps=0, success=None, achieved=False)])
    assert judge(ev, ['blocker'])['run_health']['state'] == 'incomplete'


def test_nothing_planned_and_nothing_run_is_not_tested():
    assert judge(evidence(baseline_ran=False), ['blocker'])['run_health']['state'] == 'not-tested'


@pytest.mark.parametrize('status', UNFINISHED)
def test_unfinished_status_alone_blocks_completion_and_journey_findings(status):
    ev = evidence(sessions=[session('preview', 1, achieved=False, crashed=False, status=status),
                            session('baseline', 1)])
    result = judge(ev, ['blocker'])
    assert result['run_health']['targets']['preview']['completed'] == 0
    assert not any(f['source'] == 'journey' for f in result['findings'])


@pytest.mark.parametrize('status', NOT_RUN)
def test_unfinished_status_alone_is_not_run(status):
    assert outcome(session('preview', 1, status=status, crashed=False, verdict='stopped'), [], 25)[0] == 'not-run'


def test_not_run_is_counted_in_the_markdown_tally(tmp_path):
    ev = evidence(sessions=[session('preview', 1, status='budget-skipped', done=False, success=None,
                                    achieved=False, steps=0, verdict='budget')])
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    assert '1 not-run' in replay(tmp_path)['report']


def test_checks_only_report_says_no_persona_attempted_the_task(tmp_path):
    ev = evidence(checks=[check('preview')], baseline_ran=False)
    ev['meta'].update(mode='checks-only', targets=['preview'], checks_per_target=1)
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    assert 'Entry checks only: no personas attempted the task.' in replay(tmp_path)['report']


def test_markdown_states_that_completion_is_agent_reported(tmp_path):
    (tmp_path / 'evidence.json').write_text(json.dumps(evidence(sessions=[session('preview', 1)])))
    assert 'agent-reported, not independently verified' in replay(tmp_path)['report']


# ---- agent classification ---------------------------------------------------------------------

class Proxy:
    server = 'http://127.0.0.1:1'

    def records(self, kind):
        return []


def attempt(monkeypatch, tmp_path, *, history=(), errors=(), done=False, max_steps=25, raises=None):
    import browser_use
    from swarm_persona import agent

    async def fake(self, max_steps):
        if raises:
            raise raises
        return SimpleNamespace(history=list(history), usage=None, final_result=lambda: None,
                               is_done=lambda: done, is_successful=lambda: None, errors=lambda: list(errors))

    monkeypatch.setattr(browser_use.Agent, 'run', fake)
    cfg = Config.model_validate({'app': {'name': 'Notes', 'description': 'Notes', 'url': 'https://app.example.com'}})
    cfg.personas.max_steps = max_steps
    return asyncio.run(agent.run_persona(cfg, {'title': ''}, session('preview', 1), 'preview',
                                         cfg.app.url, Proxy(), None, tmp_path))


def acted():
    return SimpleNamespace(model_output=object())


def test_an_empty_history_is_interrupted_not_a_crash(monkeypatch, tmp_path):
    result = attempt(monkeypatch, tmp_path)
    assert result['status'] == 'interrupted' and result['crashed'] is False
    assert result['verdict'] == 'Testing stopped before the agent returned a task outcome.'


def test_an_early_error_that_recovered_is_not_a_provider_stop(monkeypatch, tmp_path):
    result = attempt(monkeypatch, tmp_path, history=[acted()] * 3, errors=['x', None, None])
    assert result['status'] == 'interrupted'


def test_finishing_wins_over_a_trailing_error(monkeypatch, tmp_path):
    result = attempt(monkeypatch, tmp_path, history=[acted()] * 2, errors=[None, 'x'], done=True)
    assert result['status'] == 'finished'


def test_unfinished_attempts_do_not_carry_the_model_text_as_their_verdict(monkeypatch, tmp_path):
    result = attempt(monkeypatch, tmp_path, history=[SimpleNamespace(model_output=None)] * 3, errors=['e'] * 3)
    assert result['status'] == 'agent-error'
    assert result['verdict'] == 'Testing stopped before the agent returned a task outcome.'


def test_an_exception_in_the_agent_is_a_crash(monkeypatch, tmp_path):
    result = attempt(monkeypatch, tmp_path, raises=RuntimeError('boom'))
    assert result['status'] == 'crashed' and result['crashed'] is True and result['verdict'].startswith('CRASHED')


def test_repeated_action_errors_before_the_step_limit_are_reported_as_agent_error(monkeypatch, tmp_path):
    """Characterises the `steps < max_steps` clause: the model answered every step, an action kept failing."""
    result = attempt(monkeypatch, tmp_path, history=[acted()] * 3, errors=['no element'] * 3)
    assert result['status'] == 'agent-error'


# ---- walk -------------------------------------------------------------------------------------

def test_walk_records_a_persona_that_raised_as_crashed(monkeypatch, tmp_path):
    from swarm_persona import agent

    async def boom(*args, **kw):
        raise RuntimeError('no browser')

    monkeypatch.setattr(agent, 'run_persona', boom)
    cfg = Config.model_validate({'app': {'name': 'Notes', 'description': 'Notes', 'url': 'https://app.example.com'}})
    notes = []
    [result] = asyncio.run(runner.walk(cfg, {}, [session('preview', 1)], 'preview', cfg.app.url, [None], None,
                                       tmp_path, {'tokens': 0}, notes, False))
    assert result['status'] == 'crashed' and result['crashed'] is True and result['verdict'].startswith('CRASHED')
    assert any('Every persona' in n for n in notes)


# ---- checks-only with a PR ----------------------------------------------------------------------

def test_checks_only_with_pr_files_makes_no_model_call(monkeypatch, tmp_path):
    cfg = starter_config('https://notes.example.com', 'Read pricing', 'Founders', 'Explain pricing', 'Notes')
    cfg.journeys[0].owns = ['src/**']
    ctx = tmp_path / 'pr.json'
    ctx.write_text(json.dumps({'title': 't', 'files': ['src/a.py', 'docs/b.md']}))

    async def forbidden(*args, **kw):
        pytest.fail('checks-only must not call the model')

    class Stop(Exception):
        pass

    monkeypatch.setattr(runner, 'add_with_model', forbidden)
    monkeypatch.setattr(runner, 'plan', forbidden)
    monkeypatch.setattr(runner, 'GuardProxy', lambda *a: (_ for _ in ()).throw(Stop()))
    monkeypatch.setattr(runner.preflight, 'run', lambda *a: [])
    with pytest.raises(Stop):
        asyncio.run(runner.run(cfg, out=tmp_path / 'o', pr_context_file=str(ctx), checks_only=True))


# ---- setup ------------------------------------------------------------------------------------

@pytest.mark.parametrize('url', ['https://a@notes.example.com', 'https://:p@notes.example.com'])
def test_setup_refuses_either_half_of_credentials(url):
    with pytest.raises(ValueError, match='credentials'):
        starter_config(url, 'g', 'a', 's', 'Notes')


@pytest.mark.parametrize('field', range(4))
def test_setup_refuses_blank_fields(field):
    args = ['Choose a plan', 'Founders', 'Explain', 'Notes']
    args[field] = '   '
    with pytest.raises(ValueError, match='must not be empty'):
        starter_config('https://notes.example.com', *args)


def test_setup_needs_a_model_for_other_providers_and_names_the_key():
    with pytest.raises(ValueError, match='model'):
        starter_config('https://notes.example.com', 'g', 'a', 's', 'Notes', provider='openai')
    for provider, env in (('openai', 'OPENAI_API_KEY'), ('anthropic', 'ANTHROPIC_API_KEY')):
        cfg = starter_config('https://notes.example.com', 'g', 'a', 's', 'Notes', provider=provider, model='m')
        assert cfg.llm.api_key_env == env and cfg.llm.model == 'm'


def test_setup_refuses_an_unknown_starter_number(tmp_path, monkeypatch):
    answers = iter(['https://notes.example.com', 'Notes', '7'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    config = tmp_path / 'c.yml'
    with pytest.raises(SystemExit) as stop:
        main(['start', '--config', str(config), '--setup-only'])
    assert stop.value.code == 2 and not config.exists()


def test_run_itself_refuses_a_used_output_directory(tmp_path):
    cfg = starter_config('https://notes.example.com', 'g', 'a', 's', 'Notes')
    (tmp_path / 'evidence.json').write_text('keep')
    with pytest.raises(ValueError, match='already contains a run'):
        asyncio.run(runner.run(cfg, out=tmp_path))
    assert (tmp_path / 'evidence.json').read_text() == 'keep'


# ---- retest -----------------------------------------------------------------------------------

def saved_run(tmp_path, *, baseline=False, checks_only=False):
    cfg = starter_config('https://notes.example.com/pricing', 'Choose a plan', 'Founders', 'A plan is selected', 'Notes')
    person = {'id': 1, 'name': 'P1', 'who': 'a tester', 'device': 'desktop', 'patience': 'medium',
              'tech_comfort': 'medium', 'journey': 'first-task', 'goal': 'Choose a plan', 'start_path': '/pricing',
              'success_criteria': 'A plan is selected'}
    ev = evidence(sessions=[session('preview', 1)], checks=[check('preview', journey='first-task')], baseline_ran=False)
    ev['personas'] = [] if checks_only else [person]
    targets = ['preview', 'baseline'] if baseline else ['preview']
    if baseline:
        cfg.app.baseline_url = 'https://base.example.com'
    ev['meta'].update(scenario_key=workflow.scenario_key(cfg, cfg.journeys), targets=targets,
                      mode='checks-only' if checks_only else 'personas', baseline_url=cfg.app.baseline_url)
    old = tmp_path / 'before'
    old.mkdir()
    (old / 'evidence.json').write_text(json.dumps(ev))
    return cfg, old, ev


def do_retest(monkeypatch, cfg, old, out, url=None):
    seen = {}

    async def fake_run(config, **kw):
        seen.update(kw)
        return {'passed': True}

    monkeypatch.setattr('swarm_persona.run.run', fake_run)
    asyncio.run(workflow.retest(cfg, old, out, url))
    return seen


@pytest.mark.parametrize('where', ['same', 'nested', 'parent'])
def test_retest_refuses_overlapping_output(monkeypatch, tmp_path, where):
    cfg, old, _ = saved_run(tmp_path)
    out = {'same': old, 'nested': old / 'nested', 'parent': old.parent}[where]
    with pytest.raises(ValueError, match='separate'):
        do_retest(monkeypatch, cfg, old, out)


def test_retest_refuses_a_used_output_directory(monkeypatch, tmp_path):
    cfg, old, _ = saved_run(tmp_path)
    used = tmp_path / 'after'
    used.mkdir()
    (used / 'evidence.json').write_text('keep')
    with pytest.raises(ValueError, match='already contains'):
        do_retest(monkeypatch, cfg, old, used)


def test_retest_refuses_an_empty_persona_plan(monkeypatch, tmp_path):
    cfg, old, ev = saved_run(tmp_path)
    ev['personas'] = []
    (old / 'evidence.json').write_text(json.dumps(ev))
    with pytest.raises(ValueError, match='persona plan'):
        do_retest(monkeypatch, cfg, old, tmp_path / 'after')


@pytest.mark.parametrize('key, value', [('goal', 'Another goal'), ('start_path', '/other'),
                                        ('success_criteria', 'Another'), ('device', 'tablet'),
                                        ('journey', 'ghost')])
def test_retest_refuses_a_tampered_persona_plan_even_when_the_scenario_key_matches(monkeypatch, tmp_path, key, value):
    cfg, old, ev = saved_run(tmp_path)
    ev['personas'][0][key] = value
    (old / 'evidence.json').write_text(json.dumps(ev))
    if key == 'journey':  # a second journey name that the config does not have, next to a valid persona
        ev['personas'] = [{**ev['personas'][0], 'journey': 'first-task'}, ev['personas'][0]]
        (old / 'evidence.json').write_text(json.dumps(ev))
    with pytest.raises(ValueError, match='changed'):
        do_retest(monkeypatch, cfg, old, tmp_path / 'after')


def test_retest_forwards_the_original_mode_baseline_and_new_url(monkeypatch, tmp_path):
    cfg, old, _ = saved_run(tmp_path)
    seen = do_retest(monkeypatch, cfg, old, tmp_path / 'a', 'https://new.example.com')
    assert seen['model_selection'] is False and seen['baseline'] is False and seen['checks_only'] is False
    assert seen['url'] == 'https://new.example.com'
    cfg, old, _ = saved_run(tmp_path / 'b' if (tmp_path / 'b').mkdir() is None else tmp_path, checks_only=True)
    seen = do_retest(monkeypatch, cfg, old, tmp_path / 'c')
    assert seen['checks_only'] is True and seen['personas_override'] == []


@pytest.mark.parametrize('edit', [
    lambda c: setattr(c.app, 'name', 'Other'),
    lambda c: setattr(c.app, 'description', 'Other'),
    lambda c: setattr(c.app, 'url', 'https://other.example.com'),
    lambda c: setattr(c.app, 'hosts', ['cdn.example.com']),
    lambda c: setattr(c.auth, 'storage_state', 'state.json'),
    lambda c: setattr(c, 'preflight', [__import__('swarm_persona.config', fromlist=['Preflight']).Preflight(path='/x', message='m')]),
    lambda c: setattr(c.journeys[0], 'critical_selectors', ['#buy']),
    lambda c: setattr(c.personas, 'audience', 'Someone else'),
    lambda c: setattr(c.guard, 'allow_delete', True),
    lambda c: setattr(c.budget, 'max_tokens', 1),
    lambda c: setattr(c.llm, 'model', 'another-model'),
], ids=['name', 'description', 'config_url', 'hosts', 'auth', 'preflight', 'journey', 'audience', 'guard', 'budget', 'model'])
def test_retest_refuses_every_scenario_input_that_changed(monkeypatch, tmp_path, edit):
    cfg, old, _ = saved_run(tmp_path)
    edit(cfg)
    with pytest.raises(ValueError, match='changed'):
        do_retest(monkeypatch, cfg, old, tmp_path / 'after')


def test_the_api_key_variable_name_is_not_part_of_the_scenario(monkeypatch, tmp_path):
    cfg, old, _ = saved_run(tmp_path)
    cfg.llm.api_key_env = 'SOME_OTHER_VARIABLE'
    do_retest(monkeypatch, cfg, old, tmp_path / 'after')


def test_comparison_needs_both_runs_complete_and_the_same_targets():
    from swarm_persona.workflow import compare

    done = evidence(sessions=[session('preview', 1)], baseline_ran=False)
    broken = evidence(sessions=[session('preview', 1, crashed=True)], baseline_ran=False)
    assert compare(broken, done)['conclusive'] is False
    assert compare(done, broken)['conclusive'] is False
    with_baseline = evidence(sessions=[session('preview', 1), session('baseline', 1)], baseline_ran=True)
    with_baseline['personas'] = [{'id': 1}]
    done['personas'] = [{'id': 1}]
    with_baseline['meta']['targets'] = ['preview', 'baseline']
    assert compare(done, with_baseline)['conclusive'] is False


def test_comparison_counts_seen_again_and_newly_observed():
    from swarm_persona.workflow import compare

    before = evidence(sessions=[session('preview', 1)], raw=[issue('preview', 1)], baseline_ran=False)
    after = evidence(sessions=[session('preview', 1)], baseline_ran=False,
                     raw=[issue('preview', 1), issue('preview', 2, category='bug', url='https://app.example.com/other')])
    result = compare(before, after)
    assert (result['seen_again'], result['not_observed'], result['newly_observed']) == (1, 0, 1)


# ---- visual report ----------------------------------------------------------------------------

X = '<i>X</i>'


def page_of(out, ev):
    (out / 'evidence.json').write_text(json.dumps(ev))
    replay(out)
    return (out / 'report.html').read_text()


def test_every_model_or_page_derived_field_is_escaped(tmp_path):
    (tmp_path / 'shots').mkdir()
    (tmp_path / 'shots' / 'a.png').write_bytes(PNG)
    ev = evidence(
        checks=[check('preview', journey=X, status=None, error=X, screenshot='shots/a.png')],
        sessions=[session('preview', 1, name=X, goal=X, worked_well=[X]),
                  session('preview', 2, name=X, status='interrupted', crashed=False, done=False, success=None,
                          verdict=X, steps=0, achieved=False)],
        raw=[issue('preview', 1, title=X, detail=X, url='https://app.example.com/' + X)], baseline_ran=False)
    ev['personas'] = [{'id': 1, 'goal': X}]
    ev['comparison'] = {'previous_run': X, 'conclusive': True, 'seen_again': X, 'not_observed': X, 'newly_observed': X}
    ev['meta'].update(title=X, url=X, model=X, run_id=X, notes=[X])
    page = page_of(tmp_path, ev)
    assert X not in page and '&lt;i&gt;X&lt;/i&gt;' in page


def test_skipped_reason_is_escaped(tmp_path):
    ev = evidence(baseline_ran=False)
    ev['meta']['skipped'] = X
    page = page_of(tmp_path, ev)
    assert X not in page and 'Nothing ran' in page


def test_captions_and_alt_text_cannot_break_out_of_the_attribute(tmp_path):
    (tmp_path / 'shots').mkdir()
    (tmp_path / 'shots' / 'a.png').write_bytes(PNG)
    ev = evidence(checks=[check('preview', journey='x" onerror="y', screenshot='shots/a.png')], baseline_ran=False)
    ev['meta'].update(mode='checks-only', targets=['preview'], checks_per_target=1)
    page = page_of(tmp_path, ev)
    assert ' onerror="' not in page and '&quot;' in page


def test_em_dashes_are_removed_from_html(tmp_path):
    ev = evidence(sessions=[session('preview', 1)], baseline_ran=False)
    ev['meta']['title'] = 'Before' + chr(0x2014) + 'after'
    assert chr(0x2014) not in page_of(tmp_path, ev)


def test_csp_denies_everything_but_data_images(tmp_path):
    page = page_of(tmp_path, evidence(sessions=[session('preview', 1)], baseline_ran=False))
    assert "default-src 'none'" in page and 'img-src data:' in page and "base-uri 'none'" in page


def test_inconclusive_comparison_and_agent_reported_notice_are_shown(tmp_path):
    ev = evidence(sessions=[session('preview', 1)], baseline_ran=False)
    ev['comparison'] = {'previous_run': 'a', 'conclusive': False, 'seen_again': 0, 'not_observed': 0, 'newly_observed': 0}
    page = page_of(tmp_path, ev)
    assert 'Comparison inconclusive' in page and 'agent-reported' in page


def test_comparison_zero_counts_are_printed_as_zero(tmp_path):
    ev = evidence(sessions=[session('preview', 1)], baseline_ran=False)
    ev['comparison'] = {'previous_run': 'a', 'conclusive': True, 'seen_again': 0, 'not_observed': 0, 'newly_observed': 0}
    assert '0 seen again · 0 not observed this time · 0 newly observed' in page_of(tmp_path, ev)


def test_screenshot_limits(tmp_path):
    from swarm_persona.visual_report import MISSING_IMAGE, screenshot

    shots = tmp_path / 'out' / 'shots'
    shots.mkdir(parents=True)
    out = tmp_path / 'out'
    (shots / 'huge.png').write_bytes(PNG + b'0' * 8_000_000)
    (shots / 'gif.png').write_bytes(b'GIF89a-not-a-png')
    (shots / 'real.txt').write_bytes(PNG)
    for name in ('huge.png', 'gif.png', 'real.txt', 'missing.png'):
        assert screenshot(out, 'shots/' + name) == MISSING_IMAGE
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'ok.png').write_bytes(PNG)
    linked = tmp_path / 'linked'
    linked.mkdir()
    (linked / 'shots').symlink_to(elsewhere)
    assert screenshot(linked, 'shots/ok.png') == MISSING_IMAGE


def test_one_budget_covers_finding_images_and_page_captures(tmp_path):
    (tmp_path / 'shots').mkdir()
    (tmp_path / 'shots' / 'big1.png').write_bytes(PNG + b'0' * 7_900_000)
    (tmp_path / 'shots' / 'big2.png').write_bytes(PNG + b'1' * 7_900_000)
    ev = evidence(sessions=[session('preview', 1)], checks=[check('preview', screenshot='shots/big2.png')],
                  raw=[issue('preview', 1, screenshot='shots/big1.png')], baseline_ran=False)
    page = page_of(tmp_path, ev)
    assert page.count('data:image/png') == 1 and 'Image omitted' in page


def test_top_findings_are_not_embedded_twice(tmp_path):
    (tmp_path / 'shots').mkdir()
    (tmp_path / 'shots' / 'one.png').write_bytes(PNG)
    raw = [issue('preview', i, title=f'Issue {i}', screenshot='shots/one.png') for i in range(5)]
    page = page_of(tmp_path, evidence(sessions=[session('preview', 1)], raw=raw, baseline_ran=False))
    assert '2 more findings' in page and page.count('data:image/png') == 5  # once per finding


# ---- action -----------------------------------------------------------------------------------

def run_action(tmp_path, *, cli_exit, report, fail):
    script = next(s['run'] for s in yaml.safe_load(Path('action.yml').read_text())['runs']['steps'] if s.get('id') == 'run')
    bins = tmp_path / 'bin'
    bins.mkdir()
    for name, content in {'uv': '#!/bin/sh\nprintf /tmp\n', 'gh': '#!/bin/sh\nprintf "{}"\n',
                          'swarm-persona': f'#!/bin/sh\nexit {cli_exit}\n'}.items():
        (bins / name).write_text(content)
        (bins / name).chmod(0o700)
    if report is not None:
        (tmp_path / 'swarm-persona').mkdir()
        (tmp_path / 'swarm-persona' / 'report.json').write_text(json.dumps(report))
    outputs = tmp_path / 'outputs'
    env = {**os.environ, 'PATH': str(bins) + os.pathsep + os.environ['PATH'], 'RUNNER_TEMP': str(tmp_path),
           'SP_PR': '1', 'SP_ARGS': '', 'SP_CONFIG': 't.yml', 'SP_URL': '', 'SP_COMMENT': 'false',
           'SP_FAIL': fail, 'GITHUB_OUTPUT': str(outputs)}
    done = subprocess.run(['bash', '-eo', 'pipefail', '-c', script], env=env, capture_output=True, text=True)
    return done.returncode, outputs.read_text() if outputs.exists() else ''


COMPLETE = {'passed': True, 'run_health': {'state': 'complete'}}
GATED = {'passed': False, 'run_health': {'state': 'complete'}}


@pytest.mark.parametrize('cli_exit, report, fail, code, outputs', [
    (0, None, 'false', 2, ['passed=false', 'run-health=error']),
    (0, None, 'true', 2, ['run-health=error']),
    (1, COMPLETE, 'false', 1, ['passed=true', 'run-health=complete']),
    (0, GATED, 'true', 1, ['passed=false', 'run-health=complete']),
    (0, GATED, 'false', 0, ['passed=false']),
    (0, COMPLETE, 'true', 0, ['passed=true', 'run-health=complete']),
    (2, {'passed': False, 'run_health': {'state': 'not-tested'}}, 'false', 0, ['run-health=not-tested']),
])
def test_action_exit_policy(tmp_path, cli_exit, report, fail, code, outputs):
    got, text = run_action(tmp_path, cli_exit=cli_exit, report=report, fail=fail)
    assert got == code
    for line in outputs:
        assert line in text


def test_action_declares_the_run_health_output():
    spec = yaml.safe_load(Path('action.yml').read_text())
    assert spec['outputs']['run-health']['value'] == '${{ steps.run.outputs.run-health }}'


# ---- mcp --------------------------------------------------------------------------------------

def write_report(home, run_id, **report):
    out = home / run_id / 'out'
    out.mkdir(exist_ok=True)
    (out / 'report.json').write_text(json.dumps(report))


def test_mcp_incomplete_covers_not_tested_and_only_exit_2(home):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    write_report(home, run_id, run_health={'state': 'not-tested'}, passed=False, gating=[], findings=[])
    finish(2)
    assert mcp_server.status(run_id)['state'] == 'incomplete'


def test_mcp_exit_1_stays_gate_failed_even_with_an_incomplete_report(home):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    write_report(home, run_id, run_health={'state': 'incomplete'}, passed=False, gating=[], findings=[])
    finish(1)
    assert mcp_server.status(run_id)['state'] == 'gate-failed'


@pytest.mark.parametrize('body', [None, '{not json', '[]'])
def test_mcp_exit_2_without_a_readable_incomplete_report_is_an_error(home, body):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    if body is not None:
        (home / run_id / 'out').mkdir()
        (home / run_id / 'out' / 'report.json').write_text(body)
    finish(2)
    assert mcp_server.status(run_id)['state'] == 'error'


def test_mcp_findings_reports_health_and_the_report_verdict(home):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    write_report(home, run_id, run_health={'state': 'incomplete', 'planned': 3, 'completed': 1}, passed=False,
                 gating=[], findings=[], personas=[], worked_well=[], summary='s')
    finish(2)
    got = mcp_server.findings(run_id)
    assert got['state'] == 'incomplete' and got['passed'] is False
    assert got['run_health']['completed'] == 1 and got['report_html'].endswith('out/report.html')
