import json
from pathlib import Path

import pytest

from swarm_persona.config import Config, load
from swarm_persona.journeys import matches, select
from swarm_persona.merge import validate
from swarm_persona.planner import FALLBACK_GOAL, safe_goal, slots
from swarm_persona.preflight import PreflightError, check_storage_state, cookie_header, judge
from swarm_persona.report import MARKER, safe

EXAMPLE = Path(__file__).parents[1] / 'src' / 'swarm_persona' / 'example.yml'


def cfg(**over) -> Config:
    data = {'app': {'name': 'A', 'description': 'd', 'url': 'https://preview.example.com',
                    'baseline_url': 'https://example.com', 'hosts': ['www.example.com']}}
    data.update(over)
    return Config.model_validate(data)


def test_the_shipped_example_config_is_valid():
    assert load(EXAMPLE).journeys[0].name == 'write-a-note'


@pytest.mark.parametrize('url', ['http://x.com', 'https://x.com/path', 'x.com'])
def test_origins_must_be_bare_https(url):
    with pytest.raises(ValueError):
        cfg(app={'name': 'A', 'description': 'd', 'url': url})


def test_globs():
    assert matches('src/app/**', 'src/app/a/b.tsx')
    assert matches('**/globals.css', 'globals.css') and matches('**/globals.css', 'src/x/globals.css')
    assert matches('.github/**', '.github/workflows/ci.yml')
    assert not matches('src/app/*', 'src/app/a/b.tsx')
    assert not matches('src/app/**', 'src/application.ts')


def test_journey_selection_is_glob_first_and_shared_selects_all():
    c = cfg(journeys=[{'name': 'write', 'goal': 'g', 'owns': ['src/write/**']},
                      {'name': 'read', 'goal': 'g', 'owns': ['src/read/**']}],
            shared=['src/layout.tsx'])
    s = select(c, ['src/write/page.tsx', 'README.md'])
    assert [j.name for j in s.journeys] == ['write'] and s.unclaimed == ['README.md']
    assert [j.name for j in select(c, ['src/layout.tsx']).journeys] == ['write', 'read']
    assert select(c, ['docs/x.md']).journeys == []
    assert len(select(c, []).journeys) == 2  # no diff: test everything


def test_goals_from_the_model_pass_the_filter_and_config_words_add_to_it():
    c = cfg(personas={'forbidden_goal_words': ['calendar']})
    assert safe_goal(c, 'Publish the post now') == FALLBACK_GOAL
    assert safe_goal(c, 'open the calendar view') == FALLBACK_GOAL
    assert safe_goal(c, 'Draft a post about hiring') == 'Draft a post about hiring'


def test_slots_cover_every_journey_and_put_someone_on_a_phone():
    c = cfg(personas={'count': 3}, journeys=[{'name': 'a', 'goal': 'g'}, {'name': 'b', 'goal': 'g', 'devices': ['desktop']}])
    seats = slots(c, c.journeys)
    assert {s['journey'].name for s in seats} == {'a', 'b'}
    assert any(s['device'] == 'mobile' for s in seats)
    assert all(s['device'] == 'desktop' for s in seats if s['journey'].name == 'b')


def test_preflight_fails_closed_on_shape_and_open_accounts():
    from swarm_persona.config import Preflight

    p = Preflight(path='/api/accounts', message='disconnect it', require_keys=['accounts', 'held'],
                  fail_if_nonempty=['accounts', 'held'], fail_if_true=['autoPublish'])
    assert judge(p, {'accounts': [], 'held': 0}) is None
    assert judge(p, {'accounts': [{'id': 1}], 'held': 0}) == 'disconnect it'
    assert judge(p, {'accounts': [], 'held': 2}) == 'disconnect it'
    assert judge(p, {'accounts': [], 'held': 0, 'cfg': {'auto_publish': True}}) == 'disconnect it'
    assert 'shape changed' in judge(p, {'data': {'accounts': []}})
    assert 'JSON object' in judge(p, [1])


def test_storage_state_cookies_must_belong_to_the_app():
    c = cfg()
    check_storage_state(c, 'https://preview.example.com', {'cookies': [{'domain': '.example.com', 'name': 'a', 'value': 'b'}]})
    with pytest.raises(PreflightError):
        check_storage_state(c, 'https://preview.example.com', {'cookies': [{'domain': 'evil.net', 'name': 'a', 'value': 'b'}]})
    with pytest.raises(PreflightError):  # a public suffix, or any parent not listed as an app host
        check_storage_state(c, 'https://preview.example.com', {'cookies': [{'domain': '.com', 'name': 'a', 'value': 'b'}]})
    assert cookie_header({'cookies': [{'domain': 'preview.example.com', 'name': 'a', 'value': 'b'},
                                      {'domain': '.example.com', 'name': 'c', 'value': 'd'}]}, 'preview.example.com') == 'a=b'


def test_report_text_is_defanged():
    text = safe('see @octocat ![x](https://evil.net/p.png) [click](https://evil.net) <img src=x> a|b `c` https://evil.net/z')
    assert '@octocat' not in text and '](' not in text and '<img' not in text and '|' not in text
    assert 'https://evil.net' not in text
    assert MARKER.startswith('<!--')


def test_merge_validation_keeps_ids_in_range_and_lists_every_drop():
    data = validate({'issues': [{'severity': 'huge', 'title': 't', 'source_ids': ['0', 7, 'x']}],
                     'dropped': [{'id': '1', 'reason': 'tester error'}]}, 3)
    assert data['issues'][0]['source_ids'] == [0] and data['issues'][0]['severity'] == 'minor'
    assert data['dropped'] == [{'id': 1, 'reason': 'tester error'}, {'id': 2, 'reason': 'no reason given'}]
    with pytest.raises(ValueError):
        validate([1, 2], 3)


def test_replay_rebuilds_the_report_from_the_fixture_alone(tmp_path):
    from swarm_persona.run import replay

    fixture = Path(__file__).parent / 'fixtures' / 'run' / 'evidence.json'
    (tmp_path / 'evidence.json').write_text(fixture.read_text())
    result = replay(tmp_path)
    report = (tmp_path / 'report.md').read_text()
    assert report.startswith(MARKER) and '### New' in report and '### Pre-existing' in report
    assert not result['passed']
    assert json.loads((tmp_path / 'report.json').read_text())['gating'] == result['gating']
    assert replay(tmp_path, ['polish'])['passed'] is True


@pytest.mark.parametrize('guard', [{'denied_paths': ['/api/x']}, {'allowed_writes': ['^/API/x$']},
                                   {'allowed_writes': ['GET ^/api/x$']}])
def test_guard_patterns_must_be_anchored_lower_case_write_patterns(guard):
    with pytest.raises(ValueError):
        cfg(guard=guard)
    assert cfg(guard={'allowed_writes': [r'POST|PATCH ^/api/x/\S+$']})


def test_preflight_never_follows_a_redirect():
    from swarm_persona.preflight import NoRedirect
    import urllib.request

    req = urllib.request.Request('https://preview.example.com/api/x', headers={'Cookie': 's=1'})
    with pytest.raises(PreflightError):
        NoRedirect().redirect_request(req, None, 302, 'Found', {}, 'https://idp.example.net/')


def test_the_merge_model_cannot_raise_a_corroborated_group():
    from evidence_builder import evidence, issue
    from swarm_persona.judge import judge

    raw = [issue('preview', 1, severity='minor'), issue('preview', 2, severity='minor')]
    merged = {'issues': [{'severity': 'blocker', 'category': 'ux', 'title': 't', 'detail': 'd', 'source_ids': [0, 1]}],
              'dropped': []}
    assert judge(evidence(raw=raw, merge=merged), ['blocker'])['findings'][0]['severity'] == 'minor'


def test_report_text_has_no_bare_autolinks_or_cross_references():
    text = safe('go to www.evil.com/x or see #12 and owner/repo#3')
    assert 'www.evil' not in text and '#12' not in text and 'repo#3' not in text


def test_open_page_only_takes_app_pages():
    from swarm_persona.agent import app_page

    origin = 'https://preview.example.com'
    assert app_page(origin, '/notes') == origin + '/notes'
    assert app_page(origin, origin + '/a?b=1') == origin + '/a?b=1'
    for url in ['data:text/html,<form>', 'blob:https://preview.example.com/x', '//evil.net/x',
                'https://evil.net/', 'http://preview.example.com/', 'javascript:alert(1)',
                'https://preview.example.com.evil.net/']:
        assert app_page(origin, url) is None, url


def test_auth_commands_replace_only_their_placeholders(tmp_path, monkeypatch):
    from swarm_persona import run as run_mod

    calls = []
    monkeypatch.setattr(run_mod.subprocess, 'run', lambda argv, check: calls.append(argv))
    c = cfg(auth={'command': 'login --url {url} --out {state} --extra {"a": 1}'})
    run_mod.Session(c, 'https://preview.example.com', tmp_path, 'preview').command(c.auth.command)
    assert calls[0][:3] == ['login', '--url', 'https://preview.example.com']
    assert calls[0][4].endswith('preview.state.json') and calls[0][-2:] == ['{a:', '1}']  # no KeyError on braces


def test_a_url_override_must_be_a_bare_https_origin(tmp_path):
    import asyncio

    from swarm_persona.run import run

    for url in ['http://preview.example.com', 'https://preview.example.com/path']:
        with pytest.raises(ValueError):
            asyncio.run(run(cfg(), out=tmp_path, url=url))


def test_custom_actions_register_with_browser_use():
    """String annotations once made every persona crash before its agent ran."""
    pytest.importorskip('browser_use')
    import asyncio

    import browser_use

    from swarm_persona import agent

    class Proxy:
        server = 'http://127.0.0.1:1'

        def records(self, which):
            return []

    async def stub(self, max_steps):
        raise RuntimeError('agent loop reached')

    original = browser_use.Agent.run
    browser_use.Agent.run = stub
    try:
        persona = {'id': 1, 'name': 'P', 'who': 'x', 'device': 'desktop', 'patience': 'low', 'journey': 'j',
                   'goal': 'g', 'success_criteria': '', 'start_path': '/'}
        result = asyncio.run(agent.run_persona(cfg(), {'title': 't'}, persona, 'preview',
                                               'https://preview.example.com', Proxy(), None, Path('.')))
    finally:
        browser_use.Agent.run = original
    assert 'agent loop reached' in result['verdict']


def test_guard_refusals_are_grouped_and_other_sites_counted():
    from swarm_persona.report import guard_lines

    sessions = [{'name': n, 'guard_blocked': [
        {'method': 'POST', 'url': 'https://app.example.com/cdn-cgi/rum', 'reason': 'write outside the allowlist'},
        {'method': 'POST', 'url': 'https://accounts.google.com/ListAccounts', 'reason': 'cross-origin write'}]}
        for n in ('A', 'B')]
    [_, text] = guard_lines(sessions, ['app.example.com'])
    assert 'POST /cdn-cgi/rum (write outside the allowlist, 2 personas)' in text
    assert '2 request(s) to other sites' in text and 'google' not in text
    assert guard_lines([{'name': 'A', 'guard_blocked': []}], ['app.example.com']) == []
