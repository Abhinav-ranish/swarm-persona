"""Checks on the repository itself: what git ignores, the CodeBuild test script, and names that must
never appear in this open-source tree."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
# Split so that grepping the tree for these names never finds this file.
FORBIDDEN = re.compile('|'.join(['con' + r'tent[-_ ]?os', 'try' + 'cos', 'ins' + 'forge', 'use' + 'swarm',
                                 'compe' + 'titor']), re.IGNORECASE)


SKIP_DIRS = {'.git', '.venv', '__pycache__', '.pytest_cache', 'dist', 'out'}
needs_git = pytest.mark.skipif(not (ROOT / '.git').exists(), reason='not a git checkout (a CodeBuild source zip)')


@needs_git
@pytest.mark.parametrize('path', ['.env', '.env.local', 'AuthKey_X.p8', 'server.pem', 'tls.key', '.dev.vars'])
def test_secret_files_are_ignored(path):
    assert subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '-q', path]).returncode == 0


@needs_git
@pytest.mark.parametrize('path', ['.env.example', 'uv.lock', 'constraints.txt'])
def test_the_env_example_and_the_lock_stay_trackable(path):
    # constraints.txt is made from uv.lock, and the CodeBuild tests run on it with --locked.
    assert subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '-q', path]).returncode == 1


def test_the_codebuild_test_script_stores_symlinks_as_links_and_names_the_account_first():
    script = (ROOT / 'deploy' / 'aws' / 'test.sh').read_text()
    assert 'zip -q -y ' in script
    assert script.index('echo "AWS account $ACCOUNT') < script.index('aws s3 cp')


def test_no_file_names_other_products():
    hits = []
    for path in ROOT.rglob('*'):
        if path.is_file() and not SKIP_DIRS & set(path.relative_to(ROOT).parts[:-1]) and path.suffix != '.png':
            hits += [f'{path.relative_to(ROOT)}: {m.group(0)}' for m in FORBIDDEN.finditer(path.read_text(errors='ignore'))]
    assert hits == []


EXPORT = ['export', '--frozen', '--no-hashes', '--no-emit-project', '--no-dev', '--extra', 'bedrock']


@pytest.mark.skipif(shutil.which('uv') is None, reason='uv is not on PATH')
def test_constraints_txt_is_what_uv_lock_resolved():
    """constraints.txt pins the runtime and bedrock dependencies a git install would otherwise
    resolve fresh. After any change to uv.lock, regenerate it with the command in its header."""
    text = (ROOT / 'constraints.txt').read_text()
    assert f"uv {' '.join(EXPORT)} -o constraints.txt" in text.splitlines()[1]
    fresh = subprocess.run(['uv', *EXPORT], cwd=ROOT, capture_output=True, text=True, check=True).stdout

    def pins(lines):
        return [line for line in lines.splitlines() if line.strip() and not line.lstrip().startswith('#')]

    assert pins(text) == pins(fresh)
    assert any(line.startswith('playwright==') for line in pins(text))


def test_installs_from_a_commit_use_that_commits_constraints():
    spec = (ROOT / 'deploy' / 'aws' / 'buildspec.yml').read_text()
    url = 'https://raw.githubusercontent.com/Abhinav-ranish/swarm-persona/$SP_REF/constraints.txt'
    fetch = next(line for line in spec.splitlines() if url in line)
    install = next(line for line in spec.splitlines() if 'swarm-persona[bedrock] @ git+' in line)
    assert 'curl -fsSL' in fetch and '-o /tmp/sp-constraints.txt' in fetch
    assert '--constraints /tmp/sp-constraints.txt' in install and spec.index(fetch) < spec.index(install)
    assert spec.index('SP_REF must be a 40-character SHA') < spec.index(fetch)  # immutable only as a full SHA
    action = (ROOT / 'action.yml').read_text()
    assert '--constraints "${GITHUB_ACTION_PATH}/constraints.txt" "${GITHUB_ACTION_PATH}[bedrock]"' in action
    assert 'uv run -q --locked ' in (ROOT / 'deploy' / 'aws' / 'test.sh').read_text()
