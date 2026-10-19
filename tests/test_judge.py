from evidence_builder import check, evidence, issue, session

from swarm_persona.judge import judge


def by_kind(result, kind):
    return [f for f in result['findings'] if f['kind'] == kind]


def test_a_signal_only_on_the_preview_is_new_and_blocks_when_it_fails_everywhere():
    ev = evidence(checks=[
        check('preview', device='desktop', selectors=[{'selector': '#save', 'state': 'missing'}]),
        check('preview', device='mobile', selectors=[{'selector': '#save', 'state': 'missing'}]),
        check('baseline', device='desktop', selectors=[{'selector': '#save', 'state': 'ok'}]),
        check('baseline', device='mobile', selectors=[{'selector': '#save', 'state': 'ok'}]),
    ])
    result = judge(ev, ['blocker'])
    [f] = by_kind(result, 'selector')
    assert (f['classification'], f['severity'], f['devices']) == ('new', 'blocker', ['desktop', 'mobile'])
    assert not result['passed'] and result['gating'] == [f['id']]


def test_one_device_only_is_major_and_does_not_gate_on_blocker():
    ev = evidence(checks=[check('preview', device='mobile', status=500), check('preview', device='desktop'),
                          check('baseline', device='mobile'), check('baseline', device='desktop')])
    [f] = by_kind(judge(ev, ['blocker']), 'http-status')
    assert (f['severity'], f['classification']) == ('major', 'new')
    assert judge(ev, ['blocker'])['passed']


def test_a_signal_on_both_sides_is_pre_existing_and_never_gates():
    ev = evidence(checks=[check('preview', status=500), check('baseline', status=500)])
    result = judge(ev, ['blocker', 'major'])
    [f] = by_kind(result, 'http-status')
    assert f['classification'] == 'pre-existing' and result['passed']


def test_fingerprints_ignore_ids_numbers_and_query_strings():
    ev = evidence(checks=[check('preview', page_errors=['TypeError at chunk-8f3a9c1d2e4b.js:12?v=3']),
                          check('baseline', page_errors=['TypeError at chunk-1a2b3c4d5e6f.js:99?v=4'])])
    [f] = by_kind(judge(ev, ['blocker']), 'page-error')
    assert f['classification'] == 'pre-existing'


def test_baseline_only_hard_signals_are_listed_as_improved():
    ev = evidence(checks=[check('preview'), check('baseline', status=502)])
    result = judge(ev, ['blocker'])
    assert result['findings'] == [] and result['improved'][0]['kind'] == 'http-status'


def test_console_noise_is_minor_and_never_confirms_an_agent_report():
    ev = evidence(checks=[check('preview', console_errors=['x failed'])],
                  raw=[issue('preview', 1, severity='blocker')], baseline_ran=False)
    result = judge(ev, ['blocker'], fail_on_unclassified=True)
    assert by_kind(result, 'console-error')[0]['severity'] == 'minor'
    [agent] = [f for f in result['findings'] if f['source'] == 'agent']
    assert agent['severity'] == 'major' and 'capped' in agent['rule']
    assert result['passed']


def test_an_agent_blocker_stands_when_two_personas_hit_it_or_a_hard_signal_fired():
    raw = [issue('preview', 1, severity='blocker'), issue('preview', 2, severity='blocker')]
    merged = {'issues': [{'severity': 'blocker', 'category': 'ux', 'title': 't', 'detail': 'd', 'source_ids': [0, 1]}],
              'dropped': []}
    result = judge(evidence(raw=raw, merge=merged, baseline_ran=False), ['blocker'])
    assert result['findings'][0]['severity'] == 'blocker'
    hard = evidence(checks=[check('preview', page_errors=['boom'])], raw=raw[:1], baseline_ran=False)
    assert [f for f in judge(hard, ['blocker'])['findings'] if f['source'] == 'agent'][0]['severity'] == 'blocker'


def test_a_merge_reply_cannot_raise_severity():
    raw = [issue('preview', 1, severity='minor')]
    merged = {'issues': [{'severity': 'blocker', 'category': 'ux', 'title': 't', 'detail': 'd', 'source_ids': [0]}],
              'dropped': []}
    [f] = judge(evidence(raw=raw, merge=merged), ['blocker'])['findings']
    assert f['severity'] == 'minor'


def test_agent_reports_match_the_baseline_by_journey_page_and_category():
    raw = [issue('preview', 1), issue('baseline', 1), issue('preview', 2, category='copy')]
    found = {f['kind']: f['classification'] for f in judge(evidence(raw=raw), ['blocker'])['findings']}
    assert found == {'ux': 'pre-existing', 'copy': 'new'}


def test_without_a_baseline_everything_is_unclassified_and_gates_only_when_asked():
    ev = evidence(checks=[check('preview', status=500), check('preview', device='mobile', status=500)], baseline_ran=False)
    assert judge(ev, ['blocker'])['findings'][0]['classification'] == 'unclassified'
    assert judge(ev, ['blocker'])['passed']
    assert not judge(ev, ['blocker'], fail_on_unclassified=True)['passed']


def test_a_journey_that_only_the_preview_cannot_finish_is_a_new_major():
    ev = evidence(sessions=[session('preview', 1, achieved=False), session('baseline', 1, achieved=True)])
    [f] = by_kind(judge(ev, ['major']), 'journey')
    assert (f['classification'], f['severity']) == ('new', 'major')
    crashed = evidence(sessions=[session('preview', 1, achieved=False, crashed=True), session('baseline', 1)])
    assert by_kind(judge(crashed, ['major']), 'journey') == []


def test_guard_observed_5xx_during_a_walk_is_a_hard_signal():
    ev = evidence(sessions=[session('preview', 1, server_errors=[{'method': 'POST', 'path': '/api/generate', 'status': 502}]),
                            session('baseline', 1)])
    [f] = by_kind(judge(ev, ['blocker']), 'server-error')
    assert (f['severity'], f['classification']) == ('major', 'new')


def test_findings_are_ordered_new_first_then_by_severity():
    ev = evidence(checks=[check('preview', console_errors=['a']), check('preview', journey='b', status=500),
                          check('baseline', journey='b', status=500)])
    order = [(f['classification'], f['severity']) for f in judge(ev, ['blocker'])['findings']]
    assert order == [('new', 'minor'), ('pre-existing', 'blocker')]


def test_one_preview_persona_finishing_the_journey_means_no_journey_finding():
    ev = evidence(sessions=[session('preview', 1, achieved=False), session('preview', 2, achieved=True),
                            session('baseline', 1), session('baseline', 2)])
    assert by_kind(judge(ev, ['major']), 'journey') == []


def agent_prints(result):
    return [f['fingerprint'] for f in result['findings'] if f['source'] == 'agent']


def test_an_agent_fingerprint_survives_new_title_wording_across_runs():
    run1 = [issue('preview', 1, title='Save button does nothing'), issue('preview', 2, title='Save is dead',
                                                                         url='https://app.example.com/w2')]
    run2 = [issue('preview', 3, title='Clicking save has no effect at all'),
            issue('preview', 4, title='Nothing happens on save', url='https://app.example.com/w2')]

    def prints(raw, title):
        m = {'issues': [{'severity': 'major', 'category': 'ux', 'title': title, 'detail': 'd', 'source_ids': [0, 1]}],
             'dropped': []}
        return agent_prints(judge(evidence(raw=raw, merge=m, baseline_ran=False), ['blocker']))

    assert prints(run1, 'Save broken') == prints(run2, 'Saving fails') == prints(run1[::-1], 'x')
    assert prints(run1, 'x') == ['agent|write|/w#|ux;write|/write|ux']


def test_findings_sharing_a_fingerprint_keep_distinct_keys():
    raw = [issue('preview', 1, title='Save button does nothing'), issue('preview', 2, title='Sidebar overlaps the editor')]
    found = [f for f in judge(evidence(raw=raw, baseline_ran=False), ['blocker'])['findings'] if f['source'] == 'agent']
    assert len({f['fingerprint'] for f in found}) == 1 and len({f['key'] for f in found}) == 2
