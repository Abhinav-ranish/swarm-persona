import json
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from swarm_persona import mcp_server
from swarm_persona.run import replay

EXAMPLE = Path(__file__).parents[1] / 'src' / 'swarm_persona' / 'example.yml'
FIXTURE = Path(__file__).parent / 'fixtures' / 'run' / 'evidence.json'


class Proc:
    def __init__(self, argv, log, cwd, env):
        self.argv, self.cwd, self.env, self.pid, self.returncode = argv, cwd, env, 4242, None
        Proc.last = self

    def poll(self):
        return self.returncode


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('SWARM_PERSONA_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('SWARM_PERSONA_CONFIGS', raising=False)
    monkeypatch.setattr(mcp_server, 'spawn', Proc)
    monkeypatch.setattr(mcp_server, 'PROCS', {})
    return tmp_path / 'home' / 'runs'


def finish(code=0):
    Proc.last.returncode = code


def test_test_starts_the_cli_run_in_the_background_with_a_per_run_config(home):
    started = mcp_server.start(str(EXAMPLE), audience='Night-shift nurses', journeys=['write-a-note'])
    path = home / started['run_id']
    assert started['state'] == 'running' and Path(started['dir']) == path
    assert started['guard'] == {'allowed_writes': 3, 'allow_delete': False, 'self_test': 1, 'preflight': 1}
    argv = Proc.last.argv
    assert argv[1:4] == ['-m', 'swarm_persona.cli', 'run'] and argv[argv.index('--out') + 1] == str(path / 'out')
    derived = yaml.safe_load((path / 'config.yml').read_text())
    original = yaml.safe_load(EXAMPLE.read_text())
    assert derived['personas']['audience'] == 'Night-shift nurses'
    assert derived['guard']['allowed_writes'] == original['guard']['allowed_writes']  # the guard is untouched
    assert derived['preflight'][0]['path'] == original['preflight'][0]['path']
    assert json.loads((path / 'run.json').read_text())['pid'] == 4242


def test_the_cli_is_spawned_detached_from_the_mcp_pipe(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_server.subprocess, 'Popen', lambda argv, **kw: seen.update(kw) or 'proc')
    with open(tmp_path / 'log', 'wb') as log:
        assert mcp_server.spawn(['x'], log, '/tmp', {'A': '1'}) == 'proc'
    assert seen['stdin'] is subprocess.DEVNULL and seen['start_new_session'] is True and seen['cwd'] == '/tmp'


def test_the_run_starts_in_the_configs_git_root_or_its_folder(home, tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'ci').mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    shutil.copy(EXAMPLE, repo / 'ci' / 'sp.yml')
    started = mcp_server.start(str(repo / 'ci' / 'sp.yml'))
    assert started['cwd'] == str(repo.resolve()) == Proc.last.cwd
    assert json.loads((home / started['run_id'] / 'run.json').read_text())['cwd'] == str(repo.resolve())
    finish()
    loose = tmp_path / 'loose'
    loose.mkdir()
    shutil.copy(EXAMPLE, loose / 'sp.yml')
    assert mcp_server.run_cwd((loose / 'sp.yml').resolve()) in (str(loose.resolve()), )


def test_a_goal_becomes_an_ad_hoc_journey_and_must_pass_the_forbidden_words(home):
    path = home / mcp_server.start(str(EXAMPLE), goal='Find the note called Standup')['run_id']
    derived = yaml.safe_load((path / 'config.yml').read_text())
    assert [(j['name'], j['goal']) for j in derived['journeys']] == [('mcp-goal', 'Find the note called Standup')]
    finish()
    kept = home / mcp_server.start(str(EXAMPLE), goal='Find a note', journeys=['write-a-note'])['run_id']
    assert [j['name'] for j in yaml.safe_load((kept / 'config.yml').read_text())['journeys']] == ['write-a-note', 'mcp-goal']
    for bad in ('Publish the note to everyone', 'delete all notes'):
        with pytest.raises(ValueError):
            mcp_server.start(str(EXAMPLE), goal=bad)
    with pytest.raises(ValueError):
        mcp_server.start(str(EXAMPLE), journeys=['nope'])


def test_parse_errors_never_echo_the_file(home, tmp_path):
    secret = tmp_path / 'not-a-config.yml'
    for content in ('AWS_SECRET=hunter2-abc\n', 'app: {name: hunter2-abc}\n', 'key: [unclosed hunter2-abc\n'):
        secret.write_text(content)
        with pytest.raises(ValueError) as error:
            mcp_server.start(str(secret))
        assert str(error.value) == mcp_server.BAD_CONFIG and error.value.__cause__ is None
    with pytest.raises(ValueError) as error:
        mcp_server.start(str(tmp_path / 'missing.yml'))
    assert str(error.value) == mcp_server.BAD_CONFIG


def test_the_config_allowlist(home, tmp_path, monkeypatch):
    other = tmp_path / 'other.yml'
    shutil.copy(EXAMPLE, other)
    monkeypatch.setenv('SWARM_PERSONA_CONFIGS', os.pathsep.join([str(EXAMPLE), str(tmp_path / 'x.yml')]))
    with pytest.raises(ValueError, match='SWARM_PERSONA_CONFIGS'):
        mcp_server.start(str(other))
    assert mcp_server.start(str(EXAMPLE))['state'] == 'running'


def test_at_most_two_runs_at_a_time(home):
    mcp_server.start(str(EXAMPLE))
    mcp_server.start(str(EXAMPLE))
    with pytest.raises(ValueError, match='at most 2 at a time'):
        mcp_server.start(str(EXAMPLE))
    finish()
    assert mcp_server.start(str(EXAMPLE))['state'] == 'running'


def test_status_reads_the_log_and_the_exit_code(home, monkeypatch):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    (home / run_id / 'run.log').write_text(
        'Personas: Ana (desktop, write-a-note); Ben (mobile, write-a-note)\n'
        '  [preview] start Ana (desktop): x\n  [preview] done  Ana: 5 steps, 10s\n'
        '  [preview] Ben failed: RuntimeError: boom\n')
    (home / run_id / 'auth.log').write_text('token=hunter2-abc\n')
    now = mcp_server.status(run_id)
    assert now['state'] == 'running' and now['personas_planned'] == 2 and now['personas_done'] == {'preview': 2}
    assert 'hunter2' not in json.dumps(now) and 'hunter2' not in json.dumps(mcp_server.list_runs())
    finish(1)
    assert mcp_server.status(run_id)['state'] == 'gate-failed'
    assert (home / run_id / 'exit_code').read_text() == '1'
    monkeypatch.setattr(mcp_server, 'PROCS', {})  # a restarted server still knows how it ended
    assert mcp_server.status(run_id)['state'] == 'gate-failed'


def test_the_auth_log_is_private_and_the_auth_command_writes_there(home, tmp_path, monkeypatch):
    from swarm_persona import run as run_mod
    from swarm_persona.config import Config

    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    log = home / run_id / 'auth.log'
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert Proc.last.env['SWARM_PERSONA_AUTH_LOG'] == str(log)
    monkeypatch.setenv('SWARM_PERSONA_AUTH_LOG', str(log))
    c = Config.model_validate({'app': {'name': 'A', 'description': 'd', 'url': 'https://p.example.com'}})
    run_mod.Session(c, 'https://p.example.com', tmp_path, 'preview').command('echo signed-in-token-abc')
    assert 'signed-in-token-abc' in log.read_text()
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_mod.Session(c, 'https://p.example.com', tmp_path, 'preview').command('sh -c "exit 3" --password=pw-abc')
    assert 'pw-abc' not in str(error.value) and error.value.returncode == 3


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
    finish(1)
    got = mcp_server.findings(run_id)
    assert got['passed'] is False and got['summary'].startswith('0/2 personas completed')
    assert got['findings'][0]['screenshots'] == ['preview-1-1.png'] and len(got['personas']) == 4


def test_close_sends_sigterm_then_sigkill_after_150_seconds(home, monkeypatch):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    killed = []
    monkeypatch.setattr(mcp_server.os, 'kill', lambda pid, sig: killed.append((pid, sig)))
    assert mcp_server.close(run_id) == {'run_id': run_id, 'state': 'closing'}
    assert killed == [(4242, signal.SIGTERM)]
    assert mcp_server.status(run_id)['state'] == 'closing' and len(killed) == 1
    meta_file = home / run_id / 'run.json'
    meta = json.loads(meta_file.read_text())
    meta['closed'] = time.time() - 151
    meta_file.write_text(json.dumps(meta))
    assert mcp_server.status(run_id)['state'] == 'killed'
    assert killed[-1] == (4242, signal.SIGKILL)
    mcp_server.status(run_id)
    assert len(killed) == 2  # once
    finish(-9)
    assert mcp_server.status(run_id)['state'] == 'killed'
    assert mcp_server.close(run_id)['message'] == 'not running' and len(killed) == 2
    assert [r['run_id'] for r in mcp_server.list_runs()] == [run_id]


def test_a_clean_close_is_closed(home, monkeypatch):
    run_id = mcp_server.start(str(EXAMPLE))['run_id']
    monkeypatch.setattr(mcp_server.os, 'kill', lambda pid, sig: None)
    mcp_server.close(run_id)
    finish(143)
    assert mcp_server.status(run_id)['state'] == 'closed'


def test_prune_removes_old_finished_runs_and_keeps_running_ones(home):
    old_done = mcp_server.start(str(EXAMPLE))['run_id']
    finish(0)
    old_running = mcp_server.start(str(EXAMPLE))['run_id']
    for run_id in (old_done, old_running):
        meta = json.loads((home / run_id / 'run.json').read_text())
        meta['started'] -= 8 * 86400
        (home / run_id / 'run.json').write_text(json.dumps(meta))
    assert mcp_server.prune(7) == {'removed': [old_done], 'kept_running': [old_running]}
    assert not (home / old_done).exists() and (home / old_running).exists()
    assert mcp_server.prune(30) == {'removed': [], 'kept_running': []}
    with pytest.raises(ValueError):
        mcp_server.prune(-1)


@pytest.mark.parametrize('run_id', ['../../etc', '20260101-000000-abcdef', '', None])
def test_unknown_or_malformed_run_ids_are_refused(home, run_id):
    with pytest.raises(ValueError):
        mcp_server.status(run_id)


def test_the_server_registers_its_tools():
    pytest.importorskip('mcp')
    import asyncio

    tools = asyncio.run(mcp_server.build().list_tools())
    assert {t.name for t in tools} == {'test', 'status', 'findings', 'list_runs', 'close', 'prune'}
    schema = next(t for t in tools if t.name == 'test')
    params = getattr(schema, 'input_schema', None) or schema.inputSchema
    assert params['required'] == ['config_path'] and set(params['properties']) == {'config_path', 'goal', 'audience', 'journeys'}
