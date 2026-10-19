import json
import shutil
import signal
import subprocess
from pathlib import Path

import pytest
import yaml

from swarm_persona import mcp_server
from swarm_persona.run import replay

EXAMPLE = Path(__file__).parents[1] / 'src' / 'swarm_persona' / 'example.yml'
FIXTURE = Path(__file__).parent / 'fixtures' / 'run' / 'evidence.json'


class Proc:
    def __init__(self, argv, **kw):
        self.argv, self.kw, self.pid, self.returncode = argv, kw, 4242, None
        Proc.last = self

    def poll(self):
        return self.returncode


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('SWARM_PERSONA_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(mcp_server.subprocess, 'Popen', Proc)
    monkeypatch.setattr(mcp_server, 'PROCS', {})
    return tmp_path / 'home' / 'runs'


def test_test_starts_the_cli_run_in_the_background_with_a_per_run_config(home):
    started = mcp_server.start(str(EXAMPLE), audience='Night-shift nurses', journeys=['write-a-note'])
    path = home / started['run_id']
    assert started['state'] == 'running' and Path(started['dir']) == path
    argv = Proc.last.argv
    assert argv[1:4] == ['-m', 'swarm_persona.cli', 'run'] and argv[argv.index('--out') + 1] == str(path / 'out')
    assert Proc.last.kw['stdin'] is subprocess.DEVNULL and Proc.last.kw['start_new_session'] is True
    derived = yaml.safe_load((path / 'config.yml').read_text())
    original = yaml.safe_load(EXAMPLE.read_text())
    assert derived['personas']['audience'] == 'Night-shift nurses'
    assert derived['guard']['allowed_writes'] == original['guard']['allowed_writes']  # the guard is untouched
    assert derived['preflight'][0]['path'] == original['preflight'][0]['path']
    assert json.loads((path / 'run.json').read_text())['pid'] == 4242


def test_a_goal_becomes_an_ad_hoc_journey_and_must_pass_the_forbidden_words(home):
    path = home / mcp_server.start(str(EXAMPLE), goal='Find the note called Standup')['run_id']
    derived = yaml.safe_load((path / 'config.yml').read_text())
    assert [(j['name'], j['goal']) for j in derived['journeys']] == [('mcp-goal', 'Find the note called Standup')]
    kept = home / mcp_server.start(str(EXAMPLE), goal='Find a note', journeys=['write-a-note'])['run_id']
    assert [j['name'] for j in yaml.safe_load((kept / 'config.yml').read_text())['journeys']] == ['write-a-note', 'mcp-goal']
    for bad in ('Publish the note to everyone', 'delete all notes'):
        with pytest.raises(ValueError):
            mcp_server.start(str(EXAMPLE), goal=bad)
    with pytest.raises(ValueError):
        mcp_server.start(str(EXAMPLE), journeys=['nope'])


def test_status_reads_the_log_and_the_exit_code(home, monkeypatch):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    (home / run_id / 'run.log').write_text(
        'Personas: Ana (desktop, write-a-note); Ben (mobile, write-a-note)\n'
        '  [preview] start Ana (desktop): x\n  [preview] done  Ana: 5 steps, 10s\n'
        '  [preview] Ben failed: RuntimeError: boom\n')
    now = mcp_server.status(run_id)
    assert now['state'] == 'running' and now['personas_planned'] == 2 and now['personas_done'] == {'preview': 2}
    Proc.last.returncode = 1
    assert mcp_server.status(run_id)['state'] == 'gate-failed'
    assert (home / run_id / 'exit_code').read_text() == '1'
    monkeypatch.setattr(mcp_server, 'PROCS', {})  # a restarted server still knows how it ended
    assert mcp_server.status(run_id)['state'] == 'gate-failed'


def test_a_run_from_before_a_restart_is_judged_by_its_pid(home, monkeypatch):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    monkeypatch.setattr(mcp_server, 'PROCS', {})
    monkeypatch.setattr(mcp_server, 'alive', lambda pid: True)
    assert mcp_server.status(run_id)['state'] == 'running'
    monkeypatch.setattr(mcp_server, 'alive', lambda pid: False)
    assert mcp_server.status(run_id)['state'] == 'ended'


def test_findings_reads_the_runs_report(home):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    assert mcp_server.findings(run_id)['error'] == 'no report yet'
    out = home / run_id / 'out'
    out.mkdir()
    shutil.copy(FIXTURE, out / 'evidence.json')
    replay(out)
    Proc.last.returncode = 1
    got = mcp_server.findings(run_id)
    assert got['passed'] is False and got['summary'].startswith('0/2 personas completed')
    assert got['findings'][0]['screenshots'] == ['preview-1-1.png'] and len(got['personas']) == 4


def test_list_runs_and_close(home, monkeypatch):
    first = mcp_server.start(str(EXAMPLE))['run_id']
    killed = []
    monkeypatch.setattr(mcp_server.os, 'kill', lambda pid, sig: killed.append((pid, sig)))
    assert mcp_server.close(first) == {'run_id': first, 'state': 'closing'}
    assert killed == [(4242, signal.SIGTERM)]
    assert mcp_server.status(first)['state'] == 'closing'
    Proc.last.returncode = 143
    assert mcp_server.status(first)['state'] == 'closed'
    assert mcp_server.close(first)['message'] == 'not running' and len(killed) == 1
    assert [r['run_id'] for r in mcp_server.list_runs()] == [first]


@pytest.mark.parametrize('run_id', ['../../etc', '20260101-000000-abcdef', '', None])
def test_unknown_or_malformed_run_ids_are_refused(home, run_id):
    with pytest.raises(ValueError):
        mcp_server.status(run_id)


def test_the_server_registers_the_five_tools():
    pytest.importorskip('mcp')
    import asyncio

    tools = asyncio.run(mcp_server.build().list_tools())
    assert {t.name for t in tools} == {'test', 'status', 'findings', 'list_runs', 'close'}
    schema = next(t for t in tools if t.name == 'test')
    params = getattr(schema, 'input_schema', None) or schema.inputSchema
    assert params['required'] == ['config_path'] and set(params['properties']) == {'config_path', 'goal', 'audience', 'journeys'}
