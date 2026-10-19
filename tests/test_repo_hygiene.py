"""Checks on the repository itself: what git ignores, the CodeBuild test script, and names that must
never appear in this open-source tree."""

import re
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
@pytest.mark.parametrize('path', ['.env', '.env.local', 'AuthKey_X.p8', 'server.pem', 'tls.key', '.dev.vars', 'uv.lock'])
def test_secret_files_are_ignored(path):
    assert subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '-q', path]).returncode == 0


@needs_git
def test_the_env_example_stays_trackable():
    assert subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '-q', '.env.example']).returncode == 1


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
