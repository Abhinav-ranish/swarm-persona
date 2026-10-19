"""swarm-persona as an MCP server over stdio: start a run in the background, poll it, read its
findings, stop it.

Every tool wraps the CLI. `test` writes a per-run config and starts `swarm-persona run` in its own
process with its output under ~/.swarm-persona/runs/<run_id>, so the guard self-test, preflight,
budgets and sign-out happen exactly as they do on the command line. `close` sends that process
SIGTERM, which the CLI unwinds to sign-out.

No `from __future__ import annotations`: the MCP SDK reads the tools' annotations for their schemas.
"""

import inspect
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .config import Config, Journey, load
from .planner import forbidden_re

RUN_ID = re.compile(r'\d{8}-\d{6}-[0-9a-f]{6}')
EXIT_STATES = {0: 'passed', 1: 'gate-failed', 2: 'error', 143: 'closed', -15: 'closed'}
PROCS: dict[str, subprocess.Popen] = {}


def runs_home() -> Path:
    return Path(os.environ.get('SWARM_PERSONA_HOME') or '~/.swarm-persona').expanduser() / 'runs'


def run_dir(run_id: str) -> Path:
    if not RUN_ID.fullmatch(str(run_id or '')) or not (runs_home() / run_id / 'run.json').exists():
        raise ValueError(f'no run {run_id!r}; list_runs shows the known ones')
    return runs_home() / run_id


def derive(config_path: str, goal: str | None, audience: str | None, journeys: list[str] | None) -> dict:
    """The config for one run: the file as is, narrowed to some journeys, an ad-hoc goal, an audience."""
    cfg = load(config_path)
    if journeys:
        unknown = sorted(set(journeys) - {j.name for j in cfg.journeys})
        if unknown:
            raise ValueError(f'unknown journeys {unknown}; the config has {[j.name for j in cfg.journeys]}')
        cfg.journeys = [j for j in cfg.journeys if j.name in journeys]
    if goal:
        text = ' '.join(goal.split())[:300]
        if forbidden_re(cfg).search(text):
            raise ValueError('the goal asks for something personas never do (publish, send, pay, delete, ...)')
        cfg.journeys = [*(cfg.journeys if journeys else []), Journey(name='mcp-goal', goal=text,
                                                                     start_path=cfg.app.routes[0])]
    if audience:
        cfg.personas.audience = ' '.join(audience.split())[:1000]
    data = cfg.model_dump(mode='json')
    Config.model_validate(data)
    return data


def alive(pid: int) -> bool:
    """True while pid is a running swarm-persona CLI (not a reused pid)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    ps = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
    return 'swarm_persona.cli' in ps.stdout


def read_meta(path: Path) -> dict:
    return json.loads((path / 'run.json').read_text())


def state_of(path: Path, meta: dict) -> str:
    exit_file = path / 'exit_code'
    proc = PROCS.get(meta['run_id'])
    if proc is not None and proc.poll() is not None and not exit_file.exists():
        exit_file.write_text(str(proc.returncode))
    if exit_file.exists():
        return EXIT_STATES.get(int(exit_file.read_text()), 'ended')
    if (proc is not None and proc.poll() is None) or (proc is None and alive(meta['pid'])):
        return 'closing' if meta.get('closed') else 'running'
    if (path / 'out' / 'report.json').exists():
        return 'finished'
    return 'closed' if meta.get('closed') else 'ended'


def progress(path: Path) -> dict:
    """Personas planned and walks finished per target, read from the run's log."""
    log = (path / 'run.log').read_text(errors='replace') if (path / 'run.log').exists() else ''
    planned = re.search(r'^Personas: (.*)$', log, re.M)
    done: dict[str, int] = {}
    for target in re.findall(r'^\s+\[(\w+)\] (?:done |\S.* failed: )', log, re.M):
        done[target] = done.get(target, 0) + 1
    return {'personas_planned': len(planned.group(1).split('; ')) if planned else None, 'personas_done': done,
            'log_tail': log.strip().splitlines()[-5:]}


def start(config_path: str, goal: str | None = None, audience: str | None = None,
          journeys: list[str] | None = None) -> dict:
    """Start a persona swarm run in the background and return its run_id. config_path is a
    swarm-persona.yml; goal adds an ad-hoc journey (replacing the configured ones unless journeys
    names some to keep); audience describes who uses the app; journeys picks configured journeys by
    name. The write guard and preflight run as always. Poll with status, read with findings."""
    config = Path(config_path).expanduser().resolve()
    data = derive(str(config), goal, audience, journeys)
    run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    path = runs_home() / run_id
    path.mkdir(parents=True, mode=0o700)
    (path / 'config.yml').write_text(yaml.safe_dump(data, sort_keys=False))
    argv = [sys.executable, '-m', 'swarm_persona.cli', 'run', '--config', str(path / 'config.yml'),
            '--out', str(path / 'out')]
    with open(path / 'run.log', 'wb') as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                cwd=os.getcwd(), start_new_session=True)
    PROCS[run_id] = proc
    meta = {'run_id': run_id, 'pid': proc.pid, 'config': str(config), 'goal': goal, 'audience': audience,
            'journeys': journeys, 'started': time.time(), 'cwd': os.getcwd()}
    (path / 'run.json').write_text(json.dumps(meta, indent=2))
    return {'run_id': run_id, 'state': 'running', 'dir': str(path)}


def status(run_id: str) -> dict:
    """State (running, closing, passed, gate-failed, error, closed, finished, ended), elapsed seconds
    and personas done per target, as far as the run's log shows them."""
    path = run_dir(run_id)
    meta = read_meta(path)
    state = state_of(path, meta)
    end = (path / 'exit_code').stat().st_mtime if (path / 'exit_code').exists() else time.time()
    return {'run_id': run_id, 'state': state, 'elapsed_seconds': round(end - meta['started']), **progress(path)}


def findings(run_id: str) -> dict:
    """The run's verdict from its report.json: gate, summary, per-persona verdicts and findings."""
    path = run_dir(run_id)
    state = state_of(path, read_meta(path))
    report = path / 'out' / 'report.json'
    if not report.exists():
        return {'run_id': run_id, 'state': state, 'error': 'no report yet'}
    data = json.loads(report.read_text())
    keep = ('id', 'severity', 'classification', 'title', 'detail', 'journey', 'devices', 'personas', 'rule',
            'possible_fix')
    return {'run_id': run_id, 'state': state, 'passed': data['passed'], 'summary': data.get('summary'),
            'gating': data['gating'], 'personas': data.get('personas', []), 'worked_well': data.get('worked_well', []),
            'findings': [{k: f.get(k) for k in keep} | {'screenshots': [Path(s).name for s in f['screenshots']]}
                         for f in data['findings']],
            'report_md': str(path / 'out' / 'report.md')}


def list_runs() -> list[dict]:
    """Known runs, newest first."""
    home = runs_home()
    paths = sorted((p for p in home.iterdir() if (p / 'run.json').exists()), reverse=True) if home.exists() else []
    out = []
    for path in paths[:50]:
        meta = read_meta(path)
        out.append({'run_id': meta['run_id'], 'state': state_of(path, meta), 'started': meta['started'],
                    'config': meta['config'], 'goal': meta.get('goal')})
    return out


def close(run_id: str) -> dict:
    """Stop a running run: SIGTERM, which the CLI unwinds (proxies stop, sign-out runs)."""
    path = run_dir(run_id)
    meta = read_meta(path)
    state = state_of(path, meta)
    if state != 'running':
        return {'run_id': run_id, 'state': state, 'message': 'not running'}
    os.kill(meta['pid'], signal.SIGTERM)
    meta['closed'] = time.time()
    (path / 'run.json').write_text(json.dumps(meta, indent=2))
    return {'run_id': run_id, 'state': 'closing'}


TOOLS = {'test': start, 'status': status, 'findings': findings, 'list_runs': list_runs, 'close': close}


def build():
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as MCPServer
        except ImportError as error:
            raise SystemExit('swarm-persona mcp needs the mcp extra: pip install "swarm-persona[mcp]"') from error
    server = MCPServer('swarm-persona', instructions=(
        'Simulated users test a web app behind a network write guard. Start a run with test, poll status '
        'until it is no longer running (runs take 10 to 30 minutes), then read findings.'))
    for name, fn in TOOLS.items():
        server.add_tool(fn, name=name, description=inspect.cleandoc(fn.__doc__ or ''))
    return server


def serve() -> None:
    build().run()
