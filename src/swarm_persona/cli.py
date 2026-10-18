"""swarm-persona command line. Exit codes: 0 passed, 1 a new finding hit the gate, 2 could not run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from importlib import resources
from pathlib import Path

from . import __version__
from .config import load


def cmd_run(args) -> int:
    from .github import step_summary
    from .run import publish, run

    cfg = load(args.config)
    result = asyncio.run(run(cfg, out=Path(args.out), pr=args.pr, pr_context_file=args.pr_context, url=args.url,
                             baseline=not args.no_baseline, model_selection=not args.no_model_selection,
                             headed=args.headed))
    step_summary(result['report'])
    if args.comment:
        if not args.pr:
            sys.exit('--comment needs --pr')
        print(f'Comment: {publish(Path(args.out), args.pr, app_file=os.environ.get("SWARM_PERSONA_GITHUB_APP"))}')
    print(f"\nWrote {Path(args.out) / 'report.md'}: {'passed' if result['passed'] else 'FAILED the gate'}")
    return 0 if result['passed'] or args.no_gate else 1


def cmd_replay(args) -> int:
    from .run import replay

    result = replay(Path(args.out), args.fail_on, args.fail_on_unclassified or None)
    print(result['report'])
    return 0 if result['passed'] else 1


def cmd_publish(args) -> int:
    from .run import publish

    print(publish(Path(args.out), args.pr, args.repo, args.images_repo,
                  args.github_app or os.environ.get('SWARM_PERSONA_GITHUB_APP')))
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


def cmd_init(args) -> int:
    target = Path(args.path)
    if target.exists():
        sys.exit(f'{target} exists; not overwriting')
    target.write_text(resources.files('swarm_persona').joinpath('example.yml').read_text())
    print(f'Wrote {target}. Fill in app, guard and journeys, then: swarm-persona check-config --config {target}')
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog='swarm-persona', description=__doc__)
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='command', required=True)

    r = sub.add_parser('run', help='run the swarm and write report.md, report.json, evidence.json')
    r.add_argument('--config', required=True)
    r.add_argument('--out', required=True)
    r.add_argument('--pr', help='PR number: context from gh, and where --comment posts')
    r.add_argument('--pr-context', help='saved `gh pr view --json title,body,files` (no gh login needed)')
    r.add_argument('--url', help='the preview to test, overriding app.url')
    r.add_argument('--no-baseline', action='store_true', help='skip the baseline even if configured')
    r.add_argument('--no-model-selection', action='store_true', help='journeys from globs only')
    r.add_argument('--comment', action='store_true', help='create or update the sticky PR comment')
    r.add_argument('--no-gate', action='store_true', help='exit 0 even when the gate fails')
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
