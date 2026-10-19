import asyncio
import json
from pathlib import Path

import pytest
from evidence_builder import check, evidence, issue, session

from swarm_persona import agent, run
from swarm_persona.agent import Outcome, read_outcome
from swarm_persona.config import Config
from swarm_persona.judge import judge, outcome
from swarm_persona.run import baseline_ran, replay, walk

FIXTURE = Path(__file__).parent / 'fixtures' / 'run' / 'evidence.json'


def verdicts(ev):
    return [(v['result'], v['basis']) for v in judge(ev, ['blocker'])['personas'] if v['target'] == 'preview']


@pytest.mark.parametrize('session_kw, fired, expected', [
    ({'crashed': True, 'verdict': 'ACHIEVED: fine', 'success': True}, [], ('fail', 'evidence')),
    ({'timed_out': True, 'timeout_minutes': 30, 'verdict': 'TIMED OUT: cut off', 'steps': 0}, [], ('fail', 'evidence')),
    ({'done': True, 'success': True, 'verdict': 'ACHIEVED: fine'}, [], ('pass', 'evidence')),
    ({'done': True, 'success': True, 'verdict': 'ACHIEVED: fine'}, ['page-error'], ('partial', 'evidence')),
    ({'done': True, 'success': True, 'verdict': 'GAVE UP: lost'}, [], ('partial', 'evidence')),
    ({'done': True, 'success': False, 'verdict': 'ACHIEVED: fine'}, [], ('fail', 'evidence')),
    ({'done': False, 'success': None, 'steps': 25, 'verdict': 'ACHIEVED: fine'}, [], ('fail', 'evidence')),
    ({'done': False, 'success': None, 'steps': 3, 'verdict': 'ACHIEVED: fine'}, [], ('pass', 'self-report')),
    ({'verdict': 'ACHIEVED: fine', 'steps': 3}, ['server-error'], ('partial', 'self-report')),
    ({'verdict': 'GAVE UP: stuck', 'steps': 3}, [], ('fail', 'self-report')),
    ({'verdict': '', 'steps': 3}, [], ('fail', 'self-report')),
])
def test_outcome_rules(session_kw, fired, expected):
    assert outcome(session_kw, fired, 25)[:2] == expected


def test_an_unknown_max_steps_leaves_the_decision_to_the_self_report():
    assert outcome({'done': False, 'steps': 99, 'verdict': 'ACHIEVED: x'}, [], None)[:2] == ('pass', 'self-report')


def test_only_a_5xx_during_the_personas_own_walk_makes_success_partial():
    ev = evidence(checks=[check('preview', device='mobile', status=500), check('preview', page_errors=['boom'])],
                  sessions=[session('preview', 1, device='mobile'), session('preview', 2, device='desktop')])
    assert verdicts(ev) == [('pass', 'evidence'), ('pass', 'evidence')]  # entry checks ran in another browser
    mid_walk = evidence(sessions=[session('preview', 1, server_errors=[{'method': 'GET', 'path': '/x', 'status': 502}]),
                                  session('preview', 2)])
    assert verdicts(mid_walk) == [('partial', 'evidence'), ('pass', 'evidence')]


def test_done_without_a_success_flag_leaves_it_to_the_self_report():
    assert outcome({'done': True, 'success': None, 'steps': 3, 'verdict': 'ACHIEVED: ok'}, [], 25)[:2] == (
        'pass', 'self-report')
    assert outcome({'done': True, 'success': None, 'steps': 25, 'verdict': 'GAVE UP: no'}, [], 25)[:2] == (
        'fail', 'self-report')


def test_sessions_without_ids_keep_their_own_verdicts_in_the_report(tmp_path):
    ev = evidence(sessions=[session('preview', None, name='Ana', achieved=True),
                            session('preview', None, name='Ben', achieved=False, success=False)])
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    rows = [line for line in replay(tmp_path)['report'].splitlines() if line.startswith('| preview |')]
    assert '| **pass**: finished' in rows[0] and '| **fail**: finished and marked the goal not met' in rows[1]


def test_summary_counts_preview_personas_and_findings():
    ev = evidence(checks=[check('preview', status=500)],
                  sessions=[session('preview', 1, server_errors=[{'method': 'GET', 'path': '/', 'status': 500}]),
                            session('preview', 2, journey='a'),
                            session('preview', 3, achieved=False), session('preview', 4),
                            session('baseline', 1)])
    assert judge(ev, ['blocker'])['summary'] == '2/4 personas completed (1 partly); 2 issues'
    assert judge(evidence(), ['blocker'])['summary'] == 'No personas walked; 0 issues'


def test_worked_well_comes_from_preview_personas_once_each():
    ev = evidence(sessions=[session('preview', 1, worked_well=['Fast search', 'fast  search']),
                            session('preview', 2, worked_well=['Clear labels']),
                            session('baseline', 1, worked_well=['Baseline only'])])
    assert judge(ev, ['blocker'])['worked_well'] == [{'persona': 'P1', 'note': 'Fast search'},
                                                     {'persona': 'P2', 'note': 'Clear labels'}]


def test_the_structured_outcome_is_read_and_plain_text_still_works():
    final = Outcome(verdict='ACHIEVED: saved it', worked_well=['Autosave', ' ', 'Clear\nbutton']).model_dump_json()
    assert read_outcome(final) == ('ACHIEVED: saved it', ['Autosave', 'Clear button'])
    assert read_outcome('GAVE UP: no button') == ('GAVE UP: no button', [])
    assert read_outcome(None)[0].startswith('GAVE UP')


def test_replay_shows_verdicts_summary_and_worked_well(tmp_path):
    (tmp_path / 'evidence.json').write_text(FIXTURE.read_text())
    result = replay(tmp_path)
    report = (tmp_path / 'report.md').read_text()
    assert result['summary'].startswith('0/2 personas completed;')
    assert '**fail**: hit max_steps (25) without finishing' in report
    assert '**fail** (self-report): says it gave up' in report
    assert 'The editor opened quickly (P2)' in report and 'Saving was instant' not in report
    saved = json.loads((tmp_path / 'report.json').read_text())
    assert [v['result'] for v in saved['personas']] == ['fail', 'fail', 'pass', 'pass']


def test_worked_well_text_is_defanged_in_the_report(tmp_path):
    ev = evidence(sessions=[session('preview', 1, worked_well=['see @octocat and https://evil.net #12'])])
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    report = replay(tmp_path)['report']
    assert '@octocat' not in report and 'https://evil.net' not in report and ' #12' not in report


def cut_off(target, pid, **kw):
    return session(target, pid, achieved=False, verdict='TIMED OUT: cut off at the 30-minute timeout', done=False,
                   success=None, steps=0, tokens=0, timed_out=True, timeout_minutes=30, **kw)


def test_a_cut_off_persona_fails_on_the_evidence_with_the_timeout_named():
    assert outcome(cut_off('preview', 1), [], 25) == ('fail', 'evidence', 'cut off at the 30-minute timeout')
    assert outcome({'timed_out': True}, [], 25)[2] == 'cut off at the timeout'


def test_the_summary_counts_cut_off_personas():
    ev = evidence(sessions=[session('preview', 1), session('preview', 2), cut_off('preview', 3), cut_off('preview', 4),
                            cut_off('baseline', 1)],
                  raw=[issue('preview', 1, category='ux'), issue('preview', 1, category='copy'),
                       issue('preview', 2, category='layout')])
    result = judge(ev, ['blocker'])
    assert result['summary'] == '2/4 personas completed; 2 timed out; 3 issues'
    assert [(v['result'], v['timed_out']) for v in result['personas']] == [
        ('pass', False), ('pass', False), ('fail', True), ('fail', True), ('fail', True)]


def test_a_cut_off_persona_counts_on_neither_side_of_the_journey_rule():
    def journey_titles(*sessions):
        return [f['title'] for f in judge(evidence(sessions=sessions), ['blocker'])['findings'] if f['source'] == 'journey']

    # Cut off on the preview where the baseline finished: the shared clock, not the journey.
    assert journey_titles(cut_off('preview', 1), session('baseline', 1)) == []
    # Gave up on the preview, cut off on the baseline: the baseline did not fail the journey too.
    assert journey_titles(session('preview', 1, achieved=False), cut_off('baseline', 1)) == []
    # The rule still fires on the personas that did walk.
    assert journey_titles(session('preview', 1, achieved=False), cut_off('preview', 2), session('baseline', 1),
                          cut_off('baseline', 2)) == ['Personas could not finish write on desktop']


def test_replay_rebuilds_cut_off_personas_and_old_evidence_still_replays(tmp_path):
    ev = evidence(sessions=[session('preview', 1, name='Marcus'), cut_off('preview', 2, name='Priya')])
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    result = replay(tmp_path)
    assert result['summary'] == '1/2 personas completed; 1 timed out; 0 issues'
    assert '| Priya (desktop) | write | **fail**: cut off at the 30-minute timeout |' in result['report']
    (tmp_path / 'evidence.json').write_text(FIXTURE.read_text())
    old = replay(tmp_path)
    assert 'timed out' not in old['summary'] and not any(v['timed_out'] for v in old['personas'])


def test_walk_records_personas_cut_off_at_the_timeout(monkeypatch, tmp_path):
    c = Config.model_validate({'app': {'name': 'A', 'description': 'd', 'url': 'https://preview.example.com'},
                               'personas': {'concurrency': 2}, 'budget': {'timeout_minutes': 30}})
    personas = [{'id': n, 'name': f'P{n}', 'device': 'desktop', 'goal': 'g', 'journey': 'write'} for n in (1, 2, 3, 4)]

    async def fake_run(cfg, ctx, persona, label, origin, proxy, state, shots, headed):
        result = {**persona, 'target': label, 'verdict': 'ACHIEVED: ok', 'achieved': True, 'crashed': False,
                  'done': True, 'success': True, 'worked_well': [], 'steps': 4, 'tokens': 10, 'seconds': 1,
                  'issues': [], 'guard_blocked': [], 'server_errors': []}
        if persona['id'] == 1:
            return result
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            if persona['id'] == 3:  # gave back what it had when it was cancelled
                return {**result, 'steps': 7, 'tokens': 50, 'issues': [{'title': 'half-seen'}],
                        'server_errors': [{'method': 'GET', 'path': '/x', 'status': 502}]}
            raise
        return result

    real_wait = asyncio.wait

    async def short_wait(tasks, timeout=None):
        return await real_wait(tasks, timeout=0.2)

    monkeypatch.setattr(agent, 'run_persona', fake_run)
    monkeypatch.setattr(run.asyncio, 'wait', short_wait)
    notes = []
    sessions = asyncio.run(walk(c, {}, personas, 'preview', 'https://preview.example.com', [None] * 4, None,
                                tmp_path, {'tokens': 0}, notes, False))
    assert [s['id'] for s in sessions] == [1, 2, 3, 4]  # P4 never started: queued behind the semaphore
    assert sessions[0].get('timed_out') is None and sessions[0]['steps'] == 4
    for s in sessions[1:]:
        assert s['timed_out'] and s['timeout_minutes'] == 30 and not s['achieved'] and not s['crashed']
        assert s['verdict'] == 'TIMED OUT: cut off at the 30-minute timeout' and s['issues'] == []
    assert [(s['steps'], s['tokens']) for s in sessions[1:]] == [(0, 0), (7, 50), (0, 0)]
    assert [len(s['server_errors']) for s in sessions] == [0, 0, 1, 0]  # a 5xx it saw is still evidence
    assert notes == ['Timeout: 3 persona run(s) on preview were cut off at 30 minutes.']
    ev = evidence(sessions=[{k: v for k, v in s.items() if k != 'issues'} for s in sessions])
    assert judge(ev, ['blocker'])['summary'] == '1/4 personas completed; 3 timed out; 1 issue'  # the 5xx


def test_a_baseline_that_only_crashed_or_timed_out_did_not_run():
    assert not baseline_ran([session('preview', 1), cut_off('baseline', 1), session('baseline', 2, crashed=True)])
    assert baseline_ran([cut_off('baseline', 1), session('baseline', 2, achieved=False)])
    assert not baseline_ran([session('preview', 1)])
