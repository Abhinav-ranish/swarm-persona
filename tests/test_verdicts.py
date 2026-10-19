import json
from pathlib import Path

import pytest
from evidence_builder import check, evidence, session

from swarm_persona.agent import Outcome, read_outcome
from swarm_persona.judge import judge, outcome
from swarm_persona.run import replay

FIXTURE = Path(__file__).parent / 'fixtures' / 'run' / 'evidence.json'


def verdicts(ev):
    return [(v['result'], v['basis']) for v in judge(ev, ['blocker'])['personas'] if v['target'] == 'preview']


@pytest.mark.parametrize('session_kw, fired, expected', [
    ({'crashed': True, 'verdict': 'ACHIEVED: fine', 'success': True}, [], ('fail', 'evidence')),
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


def test_hard_signals_on_the_personas_own_journey_and_device_make_success_partial():
    ev = evidence(checks=[check('preview', device='mobile', status=500), check('preview', device='desktop')],
                  sessions=[session('preview', 1, device='mobile'), session('preview', 2, device='desktop'),
                            session('preview', 3, journey='other')])
    assert verdicts(ev) == [('partial', 'evidence'), ('pass', 'evidence'), ('pass', 'evidence')]
    mid_walk = evidence(sessions=[session('preview', 1, server_errors=[{'method': 'GET', 'path': '/x', 'status': 502}])])
    assert verdicts(mid_walk) == [('partial', 'evidence')]
    console = evidence(checks=[check('preview', console_errors=['noise'])], sessions=[session('preview', 1)])
    assert verdicts(console) == [('pass', 'evidence')]  # console noise is not a hard signal


def test_summary_counts_preview_personas_and_findings():
    ev = evidence(checks=[check('preview', status=500)],
                  sessions=[session('preview', 1, journey='a'), session('preview', 2, journey='a'),
                            session('preview', 3, achieved=False), session('preview', 4),
                            session('baseline', 1)])
    assert judge(ev, ['blocker'])['summary'] == '2/4 personas completed (1 partly); 1 issue'
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
    assert [v['result'] for v in saved['personas']] == ['fail', 'fail', 'partial', 'pass']  # baseline desktop had a page error


def test_worked_well_text_is_defanged_in_the_report(tmp_path):
    ev = evidence(sessions=[session('preview', 1, worked_well=['see @octocat and https://evil.net #12'])])
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    report = replay(tmp_path)['report']
    assert '@octocat' not in report and 'https://evil.net' not in report and ' #12' not in report
