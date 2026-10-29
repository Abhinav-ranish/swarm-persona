"""budget.persona_timeout_minutes: one hung persona is cut off on its own clock, frees its slot, and
is reported like a persona cut off at the run's timeout."""

import asyncio
import json
import time

import pytest
from evidence_builder import evidence

from swarm_persona import agent, checks, run
from swarm_persona.config import Config
from swarm_persona.judge import judge
from swarm_persona.run import walk

APP = {'name': 'A', 'description': 'd', 'url': 'https://preview.example.com'}
LIMIT = 0.002  # minutes: 0.12 s


def cfg(**extra) -> Config:
    return Config.model_validate({'app': APP, 'personas': {'concurrency': 1, 'count': 3},
                                  'budget': {'timeout_minutes': 1, 'persona_timeout_minutes': LIMIT}, **extra})


def result(persona, label, **kw):
    return {**persona, 'target': label, 'verdict': 'ACHIEVED: ok', 'achieved': True, 'crashed': False,
            'done': True, 'success': True, 'worked_well': [], 'steps': 4, 'tokens': 10, 'seconds': 1,
            'issues': [], 'guard_blocked': [], 'server_errors': [], **kw}


class FakeProxy:
    def __init__(self, name, events):
        self.name, self.events, self.stopped = name, events, False

    def records(self, which='guard'):
        if which == 'guard':
            return [{'method': 'POST', 'path': f'/{self.name}', 'reason': 'write outside the allowlist'}]
        return [{'method': 'GET', 'path': f'/{self.name}', 'status': 503}]

    def stop(self):
        self.stopped = True
        self.events.append(('stop', self.name))


def test_a_hung_persona_is_cut_off_on_its_own_clock_and_frees_its_slot(monkeypatch, tmp_path):
    events, cancels, finished = [], {1: 0}, []
    personas = [{'id': n, 'name': f'P{n}', 'device': 'desktop', 'goal': 'g', 'journey': 'write'} for n in (1, 2, 3)]
    proxies = [FakeProxy(f'p{n}', events) for n in (1, 2, 3)]

    async def fake_run(cfg, ctx, persona, label, origin, proxy, state, shots, headed):
        events.append(('walk', persona['name']))
        if persona['id'] == 2:
            return result(persona, label)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append(('cancelled', persona['name']))
            if persona['id'] == 3:  # gave back what it had when it was cancelled
                return result(persona, label, steps=7, tokens=50, issues=[{'title': 'half-seen'}],
                              server_errors=[{'method': 'GET', 'path': '/x', 'status': 502}])
            cancels[1] += 1  # P1 is truly hung: it ignores the first cancel
        try:
            await asyncio.sleep(2)
        finally:
            finished.append(persona['name'])

    monkeypatch.setattr(agent, 'run_persona', fake_run)
    monkeypatch.setattr(run, 'CLEANUP_SECONDS', 0.1)
    notes = []

    async def main():  # a slot never freed would leave P2 queued until the 1-minute run timeout
        started = time.monotonic()
        out = await asyncio.wait_for(walk(cfg(), {}, personas, 'preview', APP['url'], proxies, None, tmp_path,
                                          {'tokens': 0}, notes, False), 5)
        # P1 ignores its cancel for 2 s more: the slot comes back after CLEANUP_SECONDS regardless.
        assert time.monotonic() - started < 1.5
        return out, list(finished)


    sessions, ended = asyncio.run(main())
    assert [s['id'] for s in sessions] == [1, 2, 3]
    p1, p2, p3 = sessions
    assert p2.get('timed_out') is None and p2['achieved'] and not proxies[1].stopped
    for s in (p1, p3):
        assert s['timed_out'] and s['timeout_minutes'] == LIMIT and not s['achieved'] and not s['crashed']
        assert s['verdict'] == f'TIMED OUT: cut off at the {LIMIT}-minute persona timeout' and s['issues'] == []
    assert proxies[0].stopped and proxies[2].stopped  # each one's guard proxy stopped when it was cut off
    assert events.index(('stop', 'p1')) < events.index(('walk', 'P2'))  # before the next persona took the slot
    assert events.index(('stop', 'p1')) < events.index(('cancelled', 'P1'))  # no network while it unwinds
    assert p1['guard_blocked'] == proxies[0].records('guard') and p1['server_errors'] == proxies[0].records('server')
    assert (p3['steps'], p3['tokens'], len(p3['server_errors'])) == (7, 50, 1)  # what it returned is kept
    assert cancels[1] == 1 and ended == ['P1']  # the straggler was cancelled again before walk returned
    assert notes == [f'Timeout: 2 persona run(s) on preview hit the persona timeout ({LIMIT} minutes): P1, P3.']
    ev = evidence(sessions=[{k: v for k, v in s.items() if k != 'issues'} for s in sessions])
    verdict = judge(ev, ['blocker'])
    assert verdict['summary'].startswith('1/3 personas completed; 2 timed out')
    assert not [f for f in verdict['findings'] if f['source'] == 'journey']  # kept out of the journey rule


def test_the_run_timeout_cuts_off_a_bounded_persona_and_cancels_a_hung_one_again(monkeypatch, tmp_path):
    """persona_timeout_minutes longer than the run: the run's own timeout reaches into bounded()."""
    finished, cancels = [], []
    personas = [{'id': n, 'name': f'P{n}', 'device': 'desktop', 'goal': 'g', 'journey': 'write'} for n in (1, 2)]

    async def fake_run(cfg, ctx, persona, label, origin, proxy, state, shots, headed):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancels.append(persona['name'])
            if persona['id'] == 2:  # gave back what it had when it was cancelled
                return result(persona, label, steps=7, tokens=50)
        try:  # P1 is truly hung: it ignores the first cancel
            await asyncio.sleep(2)
        finally:
            finished.append(persona['name'])

    real_wait = asyncio.wait

    async def short_run_timeout(tasks, timeout=None):  # only the run's 1-minute wait is shortened
        return await real_wait(tasks, timeout=0.2 if timeout == 60 else timeout)

    monkeypatch.setattr(agent, 'run_persona', fake_run)
    monkeypatch.setattr(run.asyncio, 'wait', short_run_timeout)
    monkeypatch.setattr(run, 'CLEANUP_SECONDS', 0.1)
    c = Config.model_validate({'app': APP, 'personas': {'concurrency': 2, 'count': 2},
                               'budget': {'timeout_minutes': 1, 'persona_timeout_minutes': 5}})
    notes = []

    async def main():
        started = time.monotonic()
        out = await asyncio.wait_for(walk(c, {}, personas, 'preview', APP['url'], [None, None], None, tmp_path,
                                          {'tokens': 0}, notes, False), 5)
        assert time.monotonic() - started < 1.5
        return out, list(finished)

    sessions, ended = asyncio.run(main())
    # P1 ignored its first cancel and would sleep 2 s more: it ended inside walk, so it was tracked
    # as a straggler and cancelled again.
    assert ended == ['P1'] and sorted(cancels) == ['P1', 'P2']
    for s in sessions:
        assert s['timed_out'] and s['verdict'] == 'TIMED OUT: cut off at the 1-minute timeout'
    assert [(s['steps'], s['tokens']) for s in sessions] == [(0, 0), (7, 50)]  # P2's returned result is kept
    assert notes == ['Timeout: 2 persona run(s) on preview were cut off at 1 minutes.']


def test_a_half_written_guard_log_line_is_skipped(tmp_path):
    proxy = run.GuardProxy(cfg(), APP['url'], tmp_path, tmp_path, 'preview-1')
    proxy.log.write_text('{"method": "POST", "path": "/a"}\n{"method": "PO')
    proxy.server_log.write_text('[1]\n{"method": "GET", "path": "/b", "status": 502}\n')
    assert proxy.records('guard') == [{'method': 'POST', 'path': '/a'}]
    assert proxy.records('server') == [{'method': 'GET', 'path': '/b', 'status': 502}]


def test_without_a_persona_timeout_a_slow_persona_keeps_its_slot(monkeypatch, tmp_path):
    personas = [{'id': n, 'name': f'P{n}', 'device': 'desktop', 'goal': 'g', 'journey': None} for n in (1, 2)]

    async def fake_run(cfg, ctx, persona, label, origin, proxy, state, shots, headed):
        await asyncio.sleep(0.3)
        return result(persona, label)

    monkeypatch.setattr(agent, 'run_persona', fake_run)
    c = Config.model_validate({'app': APP, 'personas': {'concurrency': 1, 'count': 2}})
    sessions = asyncio.run(walk(c, {}, personas, 'preview', APP['url'], [None, None], None, tmp_path,
                                {'tokens': 0}, [], False))
    assert [s.get('timed_out') for s in sessions] == [None, None]


def test_the_run_signs_out_after_a_persona_timeout(monkeypatch, tmp_path):
    events = []

    class Proxy(FakeProxy):
        server = 'http://127.0.0.1:1'

        def __init__(self, cfg, origin, confdir, logdir, name):
            super().__init__(name, events)

        def start(self):
            pass

    def command(self, template):
        events.append(('command', template.split()[0]))
        if template.startswith('login'):
            open(self.vars['state'], 'w').write('{}')

    async def no_checks(*a):
        return []

    personas = [{'id': n, 'name': f'P{n}', 'who': 'w', 'device': 'desktop', 'tech_comfort': 'medium',
                 'patience': 'medium', 'saved': False, 'journey': None, 'goal': 'g', 'success_criteria': '',
                 'start_path': '/'} for n in (1, 2)]

    async def fake_plan(cfg, ctx, journeys):
        return personas, 0

    async def fake_merge(cfg, ctx, raw, sessions):
        return None, 0

    async def fake_run(cfg, ctx, persona, label, origin, proxy, state, shots, headed):
        events.append(('walk', persona['name']))
        if persona['id'] == 1:
            await asyncio.sleep(3600)
        return result(persona, label)

    monkeypatch.setattr(run, 'GuardProxy', Proxy)
    monkeypatch.setattr(run.Session, 'command', command)
    monkeypatch.setattr(run.preflight, 'run', lambda *a: [])
    monkeypatch.setattr(checks, 'run_checks', no_checks)
    monkeypatch.setattr(run, 'plan', fake_plan)
    monkeypatch.setattr(run, 'merge', fake_merge)
    monkeypatch.setattr(agent, 'run_persona', fake_run)
    c = cfg(auth={'command': 'login {state}', 'sign_out_command': 'logout {state}'})
    report = asyncio.run(asyncio.wait_for(run.run(c, out=tmp_path / 'out'), 5))
    assert report['summary'].startswith('1/2 personas completed; 1 timed out')
    assert events.index(('stop', 'preview-1')) < events.index(('walk', 'P2'))
    assert events[-1] == ('command', 'logout') and events.count(('command', 'logout')) == 1
    saved = json.loads((tmp_path / 'out' / 'evidence.json').read_text())
    assert [s.get('timed_out', False) for s in saved['sessions']] == [True, False]


def test_a_cancelled_persona_closes_its_own_browser(monkeypatch, tmp_path):
    import browser_use

    killed = []

    async def kill(self):
        killed.append(self.browser_profile.proxy.server)

    class Agent:
        def __init__(self, browser_session, **kw):
            self.browser_session = browser_session

        async def run(self, max_steps):
            await asyncio.sleep(3600)

    class Proxy:
        server = 'http://127.0.0.1:9'

    monkeypatch.setattr(browser_use, 'Agent', Agent)
    monkeypatch.setattr(browser_use.BrowserSession, 'kill', kill)
    monkeypatch.setattr(agent, 'chat_model', lambda llm: None)
    persona = {'id': 1, 'name': 'P1', 'who': 'w', 'device': 'desktop', 'patience': 'low', 'goal': 'g',
               'start_path': '/'}

    async def main():
        task = asyncio.create_task(agent.run_persona(cfg(), {'title': ''}, persona, 'preview', APP['url'], Proxy(),
                                                     None, tmp_path))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert killed == ['http://127.0.0.1:9']


@pytest.mark.parametrize('value', [0, -1, '.nan', '.inf'])
def test_the_persona_timeout_must_be_a_positive_number(value):
    import yaml

    with pytest.raises(ValueError):
        Config.model_validate({'app': APP, 'budget': {'persona_timeout_minutes': yaml.safe_load(str(value))}})
    assert Config.model_validate({'app': APP}).budget.persona_timeout_minutes is None
    assert Config.model_validate({'app': APP, 'budget': {'persona_timeout_minutes': 2.5}}).budget.persona_timeout_minutes == 2.5
