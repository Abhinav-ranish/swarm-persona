"""swarm-persona command line. Exit codes: 0 passed, 1 a new finding hit the gate, 2 could not run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import sys
import tempfile
from importlib import resources
from pathlib import Path

from . import __version__
from .config import load


def result_code(result: dict, no_gate: bool = False) -> int:
    if result.get('run_health', {}).get('state') not in ('complete', 'skipped'):
        return 2
    return 0 if result['passed'] or no_gate else 1


def cmd_run(args) -> int:
    from .github import step_summary
    from .run import publish, run

    cfg = load(args.config)
    result = asyncio.run(run(cfg, out=Path(args.out), pr=args.pr, pr_context_file=args.pr_context, url=args.url,
                             baseline=not args.no_baseline, model_selection=not args.no_model_selection,
                             headed=args.headed, checks_only=args.checks_only))
    step_summary(result['report'])
    if args.comment:
        if not args.pr:
            sys.exit('--comment needs --pr')
        print(f'Comment: {publish(Path(args.out), args.pr, app_file=os.environ.get("SWARM_PERSONA_GITHUB_APP"))}')
    print(f"\nReport: {Path(args.out) / 'report.html'} ({result['run_health']['state']})")
    return result_code(result, args.no_gate)


def cmd_replay(args) -> int:
    from .run import replay

    result = replay(Path(args.out), args.fail_on, args.fail_on_unclassified or None)
    print(result['report'])
    return result_code(result)


def cmd_publish(args) -> int:
    from .run import publish

    print(publish(Path(args.out), args.pr, args.repo, args.images_repo,
                  args.github_app or os.environ.get('SWARM_PERSONA_GITHUB_APP')))
    return 0


def cmd_tickets(args) -> int:
    from .tickets import build, file_issues, write_json

    out = Path(args.out)
    tickets = build(out, args.min_severity)
    print(f'Wrote {write_json(out, tickets)} ({len(tickets)} tickets).')
    if args.github_issues:
        from .github import Client, repo_name

        repo = args.repo or repo_name()
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            sys.exit(f'--repo must be owner/name, got {repo!r}')
        app = args.github_app or os.environ.get('SWARM_PERSONA_GITHUB_APP')
        client = (Client.from_app(json.loads(Path(app).read_text()), repo, {'issues': 'write'}) if app
                  else Client.from_cli())
        for r in file_issues(client, repo, tickets, args.label, __version__, args.reopen_closed, args.max_issues):
            print(f"{r['action']:9} {r['url'] or r['id']}")
    return 0


def cmd_select(args) -> int:
    from .journeys import select
    from .planner import pr_context

    cfg = load(args.config)
    files = args.files or pr_context(args.pr, args.pr_context)['files']
    selection = select(cfg, files)
    print(json.dumps({'journeys': [j.name for j in selection.journeys], 'reasons': selection.reasons,
                      'unclaimed': selection.unclaimed}, indent=2))
    return 0


def cmd_check_config(args) -> int:
    cfg = load(args.config)
    print(f'{args.config}: valid. {len(cfg.journeys)} journeys, {len(cfg.guard.allowed_writes)} allowed writes, '
          f'{len(cfg.guard.denied_paths)} denied paths, {len(cfg.preflight)} preflight checks, '
          f'provider {cfg.llm.provider}:{cfg.llm.model}.')
    return 0


def cmd_guard_test(args) -> int:
    """Start one guard proxy for the app and run its self-test. Needs mitmdump and the network."""
    from .guard.proxy import GuardProxy

    cfg = load(args.config)
    origin = (args.url or cfg.app.url).rstrip('/')
    work = Path(tempfile.mkdtemp(prefix='swarm-persona-guard-'))
    proxy = GuardProxy(cfg, origin, work, work, 'test')
    try:
        proxy.start()
        print(f'Guard refused every self-test request for {origin} ({len(proxy.probes())} probes).')
        return 0
    finally:
        proxy.stop()
        shutil.rmtree(work, ignore_errors=True)


def cmd_mcp(args) -> int:
    from .mcp_server import serve

    serve()
    return 0


def cmd_init(args) -> int:
    target = Path(args.path)
    if target.exists():
        sys.exit(f'{target} exists; not overwriting')
    target.write_text(resources.files('swarm_persona').joinpath('example.yml').read_text())
    print(f'Wrote {target}. Fill in app, guard and journeys, then: swarm-persona check-config --config {target}')
    return 0


def cmd_start(args) -> int:
    from .run import run
    from .workflow import configure

    cfg = configure(args)
    if args.setup_only:
        mode = ' --checks-only' if args.checks_only else ''
        print(f'When ready: swarm-persona run --config {args.config} --out {args.out}{mode}')
        return 0
    result = asyncio.run(run(cfg, out=Path(args.out), baseline=False, checks_only=args.checks_only))
    print(f"Report: {Path(args.out) / 'report.html'} ({result['run_health']['state']})")
    return result_code(result)


def cmd_retest(args) -> int:
    from .workflow import retest

    result = asyncio.run(retest(load(args.config), Path(args.previous), Path(args.out), args.url))
    print(f"Report: {Path(args.out) / 'report.html'} ({result['run_health']['state']})")
    return result_code(result)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog='swarm-persona', description=__doc__)
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='command', required=True)

    start = sub.add_parser('start', help='guided first test: URL, task, audience and success criteria')
    for flag in ('url', 'name', 'description', 'goal', 'audience', 'success', 'model', 'storage-state'):
        start.add_argument('--' + flag)
    start.add_argument('--provider', choices=['bedrock', 'anthropic', 'openai'])
    start.add_argument('--config', default='founder-test.yml')
    start.add_argument('--out', default='out/first-test')
    start.add_argument('--setup-only', action='store_true', help='save the scenario without running browsers or models')
    start.add_argument('--checks-only', action='store_true', help='entry checks only, no model calls')
    start.set_defaults(func=cmd_start)

    retest = sub.add_parser('retest', help='repeat a saved test with the same personas and compare observations')
    retest.add_argument('previous', help='original run directory')
    retest.add_argument('--config', required=True, help='trusted original scenario config')
    retest.add_argument('--out', required=True, help='a separate new output directory')
    retest.add_argument('--url', help='new public HTTPS origin after a change')
    retest.set_defaults(func=cmd_retest)

    r = sub.add_parser('run', help='run the swarm and write report.md, report.json, evidence.json')
    r.add_argument('--config', required=True)
    r.add_argument('--out', required=True)
    r.add_argument('--pr', help='PR number: context from gh, and where --comment posts')
    r.add_argument('--pr-context', help='saved `gh pr view --json title,body,files` (no gh login needed)')
    r.add_argument('--url', help='the preview to test, overriding app.url')
    r.add_argument('--no-baseline', action='store_true', help='skip the baseline even if configured')
    r.add_argument('--no-model-selection', action='store_true', help='journeys from globs only')
    r.add_argument('--comment', action='store_true', help='create or update the sticky PR comment')
    r.add_argument('--no-gate', action='store_true', help='ignore the defect gate; incomplete tests still exit 2')
    r.add_argument('--checks-only', action='store_true', help='entry checks and screenshots, no personas or model calls')
    r.add_argument('--headed', action='store_true')
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser('replay', help='rebuild the report from OUT/evidence.json, no browser or model')
    rp.add_argument('out')
    rp.add_argument('--fail-on', nargs='+', choices=['blocker', 'major', 'minor', 'polish'])
    rp.add_argument('--fail-on-unclassified', action='store_true')
    rp.set_defaults(func=cmd_replay)

    pb = sub.add_parser('publish', help='post OUT as the sticky PR comment, screenshots embedded')
    pb.add_argument('out')
    pb.add_argument('--pr', required=True)
    pb.add_argument('--repo', help='owner/name of the PR (default: this checkout or $GITHUB_REPOSITORY)')
    pb.add_argument('--images-repo', help='owner/name to commit screenshots to (default: report.images_repo)')
    pb.add_argument('--github-app', help='JSON file {app_id, installation_id, private_key}: comment as the app')
    pb.set_defaults(func=cmd_publish)

    t = sub.add_parser('tickets', help='write OUT/tickets.json from a finished run; optionally file GitHub Issues')
    t.add_argument('out')
    t.add_argument('--format', choices=['json'], default='json')
    t.add_argument('--min-severity', choices=['blocker', 'major', 'minor', 'polish'], default='minor')
    t.add_argument('--github-issues', action='store_true', help='open an issue per ticket that has no open one yet')
    t.add_argument('--repo', help='owner/name for the issues (default: this checkout or $GITHUB_REPOSITORY)')
    t.add_argument('--label', action='append', default=[], help='label for new issues; repeat for more')
    t.add_argument('--github-app', help='JSON file {app_id, installation_id, private_key}: file as the app')
    t.add_argument('--reopen-closed', action='store_true', help='reopen a closed issue of ours that a finding matches')
    t.add_argument('--max-issues', type=int, default=10, help='create or reopen at most this many issues (default 10)')
    t.set_defaults(func=cmd_tickets)

    s = sub.add_parser('select', help='which journeys a change selects (globs only, no model)')
    s.add_argument('--config', required=True)
    s.add_argument('--pr')
    s.add_argument('--pr-context')
    s.add_argument('--files', nargs='*')
    s.set_defaults(func=cmd_select)

    c = sub.add_parser('check-config', help='validate a config file')
    c.add_argument('--config', required=True)
    c.set_defaults(func=cmd_check_config)

    g = sub.add_parser('guard-test', help='start a guard proxy and prove it refuses writes')
    g.add_argument('--config', required=True)
    g.add_argument('--url')
    g.set_defaults(func=cmd_guard_test)

    m = sub.add_parser('mcp', help='serve the test, status, findings, list_runs and close tools over stdio (MCP)')
    m.set_defaults(func=cmd_mcp)

    i = sub.add_parser('init', help='write an example swarm-persona.yml')
    i.add_argument('path', nargs='?', default='swarm-persona.yml')
    i.set_defaults(func=cmd_init)

    args = p.parse_args(argv)
    # `timeout` and CI cancellation send SIGTERM: unwind normally so the guard proxies stop and the
    # auth sign-out command runs, instead of dying with a live test session.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        code = args.func(args)
    except SystemExit as stop:  # a refusal with a message (preflight, config) could not run: 2, not 1
        if isinstance(stop.code, str):
            print(f'swarm-persona: {stop.code}', file=sys.stderr)
            sys.exit(2)
        raise
    except Exception as error:
        print(f'swarm-persona: {type(error).__name__}: {error}', file=sys.stderr)
        sys.exit(2)
    sys.exit(code)


if __name__ == '__main__':
    main()
