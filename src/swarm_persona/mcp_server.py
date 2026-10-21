"""swarm-persona as an MCP server over stdio: start a run in the background, poll it, read its
findings, stop it.

Every tool wraps the CLI. `test` writes a per-run config and starts `swarm-persona run` in its own
process with its output under ~/.swarm-persona/runs/<run_id>, so the guard self-test, preflight,
budgets and sign-out happen exactly as they do on the command line. `close` sends that process
SIGTERM, which the CLI unwinds to sign-out, and SIGKILL if it is still alive 150 seconds later.
The auth command's output goes to a 0600 auth.log that no tool returns.

No `from __future__ import annotations`: the MCP SDK reads the tools' annotations for their schemas.
"""

import fcntl
import inspect
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

from .config import Config, Journey, load
from .planner import forbidden_re

RUN_ID = re.compile(r'\d{8}-\d{6}-[0-9a-f]{6}')
EXIT_STATES = {0: 'passed', 1: 'gate-failed', 2: 'error', 143: 'closed', -15: 'closed', -9: 'killed'}
MAX_RUNNING = 2
KILL_AFTER = 150
BAD_CONFIG = 'config_path could not be loaded as a swarm-persona config; run swarm-persona check-config on it'
PROCS: dict[str, subprocess.Popen] = {}


def runs_home() -> Path:
    return Path(os.environ.get('SWARM_PERSONA_HOME') or '~/.swarm-persona').expanduser() / 'runs'


def run_dir(run_id: str) -> Path:
    if not RUN_ID.fullmatch(str(run_id or '')) or not (runs_home() / run_id / 'run.json').exists():
        raise ValueError(f'no run {run_id!r}; list_runs shows the known ones')
    return runs_home() / run_id


def derive(config_path: str, goal: str | None, audience: str | None, journeys: list[str] | None) -> dict:
    """The config for one run: the file as is, narrowed to some journeys, an ad-hoc goal, an audience."""
    try:
        cfg = load(config_path)
    except (OSError, ValueError, yaml.YAMLError):  # parse errors quote the file: never echo them
        raise ValueError(BAD_CONFIG) from None
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
    try:
        Config.model_validate(data)
    except ValueError:
        raise ValueError('the config is not valid with these goal, audience and journeys') from None
    return data


def allowed_config(config_path: str) -> Path:
    """The resolved path, which must be listed in SWARM_PERSONA_CONFIGS when that is set."""
    path = Path(config_path).expanduser().resolve()
    allow = [p for p in os.environ.get('SWARM_PERSONA_CONFIGS', '').split(os.pathsep) if p.strip()]
    if allow and path not in {Path(p).expanduser().resolve() for p in allow}:
        raise ValueError('config_path is not one of the configs listed in SWARM_PERSONA_CONFIGS')
    return path


def run_cwd(config: Path) -> str:
    """Where the run starts, so relative paths in the config resolve: its git root, else its folder."""
    try:
        top = subprocess.run(['git', '-C', str(config.parent), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return str(config.parent)
    return top.stdout.strip() if top.returncode == 0 and top.stdout.strip() else str(config.parent)


def guard_summary(data: dict) -> dict:
    guard = data.get('guard') or {}
    return {'allowed_writes': len(guard.get('allowed_writes') or []), 'allow_delete': bool(guard.get('allow_delete')),
            'self_test': len(guard.get('self_test') or []), 'preflight': len(data.get('preflight') or [])}


def alive(pid: int) -> bool:
    """True while pid is a running swarm-persona CLI (not a reused pid)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    ps = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, check=False)
    return 'swarm_persona.cli' in ps.stdout


def read_meta(path: Path) -> dict:
    return json.loads((path / 'run.json').read_text())


def state_of(path: Path, meta: dict) -> str:
    exit_file = path / 'exit_code'
    proc = PROCS.get(meta['run_id'])
    if proc is not None and proc.poll() is not None and not exit_file.exists():
        exit_file.write_text(str(proc.returncode))
    if exit_file.exists():
        if int(exit_file.read_text()) == 2:
            try:
                report = json.loads((path / 'out' / 'report.json').read_text())
                if report.get('run_health', {}).get('state') in ('incomplete', 'not-tested'):
                    return 'incomplete'
            except (OSError, ValueError):
                pass
        return EXIT_STATES.get(int(exit_file.read_text()), 'ended')
    if (proc is not None and proc.poll() is None) or (proc is None and alive(meta['pid'])):
        return 'closing' if meta.get('closed') else 'running'
    if (path / 'out' / 'report.json').exists():
        return 'finished'
    return 'closed' if meta.get('closed') else 'ended'


def save_meta(path: Path, meta: dict) -> None:
    """Atomic: a reader never sees half a run.json."""
    fd, tmp = tempfile.mkstemp(dir=path, prefix='.run.', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(meta, indent=2))
        os.replace(tmp, path / 'run.json')
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def signal_group(pid: int, sig: int) -> bool:
    """Signal the run's whole process group (spawned with start_new_session, so pgid == pid).
    False when it is already gone."""
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return False
    return True


def escalate(path: Path, meta: dict, state: str) -> str:
    """SIGKILL a run that is still alive KILL_AFTER seconds after close sent SIGTERM."""
    if state != 'closing' or meta.get('killed') or time.time() - meta['closed'] < KILL_AFTER:
        return state
    gone = not signal_group(meta['pid'], signal.SIGKILL)
    meta['killed'] = time.time()
    save_meta(path, meta)
    return 'closed' if gone else 'killed'


@contextmanager
def runs_lock():
    """Held around the concurrency check and the new run's first run.json, across MCP servers."""
    runs_home().mkdir(parents=True, exist_ok=True)
    with open(runs_home() / '.lock', 'a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def runs() -> list[tuple[Path, dict]]:
    """Every readable run, newest first; a corrupt or half-written run directory is skipped."""
    home = runs_home()
    out = []
    for path in sorted((p for p in home.iterdir() if p.is_dir()), reverse=True) if home.exists() else []:
        try:
            meta = read_meta(path)
            if not (RUN_ID.fullmatch(str(meta['run_id'])) and isinstance(meta['pid'], int)
                    and isinstance(meta['started'], (int, float))):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        out.append((path, meta))
    return out


def progress(path: Path) -> dict:
    """Personas planned and walks finished per target, read from the run's log."""
    log = (path / 'run.log').read_text(errors='replace') if (path / 'run.log').exists() else ''
    planned = re.search(r'^Personas: (.*)$', log, re.MULTILINE)
    done: dict[str, int] = {}
    for target in re.findall(r'^\s+\[(\w+)\] (?:done |\S.* failed: )', log, re.MULTILINE):
        done[target] = done.get(target, 0) + 1
    return {'personas_planned': len(planned.group(1).split('; ')) if planned else None, 'personas_done': done,
            'log_tail': log.strip().splitlines()[-5:]}


def spawn(argv: list[str], log, cwd: str, env: dict) -> subprocess.Popen:
    """The CLI in its own session, its stdin closed and its output in run.log, never on the MCP pipe."""
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=cwd, env=env,
                            start_new_session=True)


def start(config_path: str, goal: str | None = None, audience: str | None = None,
          journeys: list[str] | None = None) -> dict:
    """Start a persona swarm run in the background and return its run_id. config_path is a
    swarm-persona.yml; goal adds an ad-hoc journey (replacing the configured ones unless journeys
    names some to keep); audience describes who uses the app; journeys picks configured journeys by
    name. The write guard and preflight run as always; the result summarizes the guard. At most two
    runs at a time. Poll with status, read with findings."""
    config = allowed_config(config_path)
    data = derive(str(config), goal, audience, journeys)
    cwd = run_cwd(config)
    with runs_lock():
        return launch(config, data, cwd, goal, audience, journeys)


def launch(config: Path, data: dict, cwd: str, goal, audience, journeys) -> dict:
    """Under runs_lock: the concurrency check, the spawn and the first run.json."""
    active = [m['run_id'] for p, m in runs() if state_of(p, m) in ('running', 'closing')]
    if len(active) >= MAX_RUNNING:
        raise ValueError(f'{len(active)} runs are already running ({", ".join(active)}); '
                         f'at most {MAX_RUNNING} at a time. Wait for one, or close it.')
    run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    path = runs_home() / run_id
    path.mkdir(parents=True, mode=0o700)
    (path / 'config.yml').write_text(yaml.safe_dump(data, sort_keys=False))
    argv = [sys.executable, '-m', 'swarm_persona.cli', 'run', '--config', str(path / 'config.yml'),
            '--out', str(path / 'out')]
    os.close(os.open(path / 'auth.log', os.O_WRONLY | os.O_CREAT, 0o600))
    env = {**os.environ, 'SWARM_PERSONA_AUTH_LOG': str(path / 'auth.log')}
    with open(path / 'run.log', 'wb') as log:
        proc = spawn(argv, log, cwd, env)
    PROCS[run_id] = proc
    save_meta(path, {'run_id': run_id, 'pid': proc.pid, 'config': str(config), 'goal': goal, 'audience': audience,
                     'journeys': journeys, 'started': time.time(), 'cwd': cwd})
    return {'run_id': run_id, 'state': 'running', 'dir': str(path), 'cwd': cwd, 'guard': guard_summary(data)}


def status(run_id: str) -> dict:
    """State (running, closing, killed, passed, gate-failed, error, closed, finished, ended), elapsed seconds
    and personas done per target, as far as the run's log shows them."""
    path = run_dir(run_id)
    meta = read_meta(path)
    state = escalate(path, meta, state_of(path, meta))
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
            'run_health': data.get('run_health'), 'report_html': str(path / 'out' / 'report.html'),
            'gating': data['gating'], 'personas': data.get('personas', []), 'worked_well': data.get('worked_well', []),
            'findings': [{k: f.get(k) for k in keep} | {'screenshots': [Path(s).name for s in f['screenshots']]}
                         for f in data['findings']],
            'report_md': str(path / 'out' / 'report.md')}


def list_runs() -> list[dict]:
    """Known runs, newest first."""
    return [{'run_id': m['run_id'], 'state': escalate(p, m, state_of(p, m)), 'started': m['started'],
             'config': m['config'], 'goal': m.get('goal')} for p, m in runs()[:50]]


def prune(older_than_days: float = 7) -> dict:
    """Delete the directories (logs, screenshots, evidence) of runs started more than older_than_days
    ago. Running runs are kept."""
    if older_than_days < 0:
        raise ValueError('older_than_days must be 0 or more')
    cutoff, removed, kept = time.time() - older_than_days * 86400, [], []
    for path, meta in runs():
        if meta['started'] > cutoff:
            continue
        if state_of(path, meta) in ('running', 'closing'):
            kept.append(meta['run_id'])
            continue
        shutil.rmtree(path)
        PROCS.pop(meta['run_id'], None)
        removed.append(meta['run_id'])
    return {'removed': removed, 'kept_running': kept}


def close(run_id: str) -> dict:
    """Stop a running run: SIGTERM, which the CLI unwinds (proxies stop, sign-out runs). If it is
    still alive 150 seconds later, the next status, list_runs or close sends SIGKILL."""
    path = run_dir(run_id)
    meta = read_meta(path)
    state = escalate(path, meta, state_of(path, meta))
    if state != 'running':
        return {'run_id': run_id, 'state': state, 'message': 'not running'}
    gone = not signal_group(meta['pid'], signal.SIGTERM)
    meta['closed'] = time.time()
    save_meta(path, meta)
    return {'run_id': run_id, 'state': 'closed' if gone else 'closing'}


TOOLS = {'test': start, 'status': status, 'findings': findings, 'list_runs': list_runs, 'close': close,
         'prune': prune}


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
