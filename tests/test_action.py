"""Exercise the action's actual shell step locally, with external commands stubbed."""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize('fail,has_report,expected', [('false', True, 0), ('true', True, 2), ('false', False, 2)])
def test_incomplete_action_sets_outputs_before_applying_failure_policy(tmp_path, fail, has_report, expected):
    script = next(s['run'] for s in yaml.safe_load(Path('action.yml').read_text())['runs']['steps'] if s.get('id') == 'run')
    bins = tmp_path / 'bin'
    bins.mkdir()
    for name, content in {
        'uv': '#!/bin/sh\nprintf /tmp\n',
        'gh': '#!/bin/sh\nprintf "{}"\n',
        'swarm-persona': '#!/bin/sh\nexit 2\n',
    }.items():
        p = bins / name
        p.write_text(content)
        p.chmod(0o700)
    if has_report:
        out = tmp_path / 'swarm-persona'
        out.mkdir()
        (out / 'report.json').write_text(json.dumps({'passed': False, 'run_health': {'state': 'incomplete'}}))
    outputs = tmp_path / 'outputs'
    env = {**os.environ, 'PATH': str(bins) + os.pathsep + os.environ['PATH'], 'RUNNER_TEMP': str(tmp_path),
           'SP_PR': '1', 'SP_ARGS': '', 'SP_CONFIG': 'test.yml', 'SP_URL': '', 'SP_COMMENT': 'false',
           'SP_FAIL': fail, 'GITHUB_OUTPUT': str(outputs)}
    result = subprocess.run(['bash', '-eo', 'pipefail', '-c', script], env=env, capture_output=True, text=True)
    assert result.returncode == expected, result.stderr
    assert 'passed=false' in outputs.read_text() and 'report=' in outputs.read_text()
    if has_report:
        assert 'run-health=incomplete' in outputs.read_text()
