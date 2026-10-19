"""The agent brain in .brain/: its doctor runs here so CodeBuild enforces it, plus the CLI's
recall, context and preference logic."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BRAIN = ROOT / '.brain'
sys.path.insert(0, str(BRAIN / 'bin'))

from brain_lifecycle import doctor  # noqa: E402
from brain_people import add_preference, current_user, remove_preference, slug_handle  # noqa: E402
from brain_recall import context_for, path_affinity, recall, tokenize  # noqa: E402
from brain_recall import tests_for as importing_tests  # noqa: E402  (pytest would collect test*)


def test_the_brain_is_healthy():
    assert doctor(BRAIN) == []


@pytest.fixture
def copy(tmp_path):
    """A copy of the brain inside a scratch repo root, with the files its facts anchor to."""
    shutil.copytree(BRAIN, tmp_path / '.brain', ignore=shutil.ignore_patterns('__pycache__', 'people'))
    for fact_file in (tmp_path / '.brain' / 'facts').glob('*.json'):
        for fact in json.loads(fact_file.read_text()):
            for anchor in fact['paths']:
                target = tmp_path / anchor
                if anchor.endswith('/'):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
    assert doctor(tmp_path / '.brain') == []
    return tmp_path / '.brain'


def test_an_unlisted_plan_fails(copy):
    (copy / 'plans' / 'new-idea.md').write_text('# New idea\n')
    assert doctor(copy) == ['not listed in .brain/index.md (list it or delete it): plans/new-idea.md']


def test_an_unlisted_adr_fails(copy):
    (copy / 'adr' / '0099-something.md').write_text('# 0099\n')
    assert any('adr/0099-something.md' in i for i in doctor(copy))


def test_a_listed_doc_that_is_gone_fails(copy):
    (copy / 'plans' / 'codebuild-lockfile.md').unlink()
    assert doctor(copy) == ['.brain/index.md lists a doc that does not exist: plans/codebuild-lockfile.md']


def test_a_deleted_anchor_fails(copy):
    (copy.parent / 'src' / 'swarm_persona' / 'agent.py').unlink()
    issues = doctor(copy)
    assert 'anchored path is gone (update or drop the fact): agent-no-future-annotations: src/swarm_persona/agent.py' in issues


@pytest.mark.parametrize('change, problem', [
    (lambda f: f.pop('source'), 'source must be a str'),
    (lambda f: f.update(kind='note'), "kind 'note' does not belong in facts/gotchas.json"),
    (lambda f: f.update(created='19 Oct 2026'), 'created must be YYYY-MM-DD'),
    (lambda f: f.update(id='Not Kebab'), 'id must be lower-case kebab-case'),
    (lambda f: f.update(extra=1), "unknown key 'extra'"),
    (lambda f: f.update(paths='src/x.py'), 'paths must be a list'),
])
def test_fact_schema_problems_fail(copy, change, problem):
    path = copy / 'facts' / 'gotchas.json'
    facts = json.loads(path.read_text())
    change(facts[0])
    path.write_text(json.dumps(facts))
    assert any(problem in i for i in doctor(copy)), doctor(copy)


def test_duplicate_fact_ids_fail(copy):
    path = copy / 'facts' / 'notes.json'
    facts = json.loads(path.read_text())
    path.write_text(json.dumps(facts + [{**facts[0], 'text': 'a second copy'}]))
    assert any('id also used in facts/notes.json' in i for i in doctor(copy))


def test_an_em_dash_fails(copy):
    (copy / 'plans' / 'unbuilt-ideas.md').write_text('# Ideas ' + chr(0x2014) + ' later\n')
    assert doctor(copy) == ['em dash in .brain/plans/unbuilt-ideas.md']


def test_recall_ranks_the_matching_fact_first():
    hits = recall(BRAIN, 'future annotations browser-use actions')
    assert hits[0][1]['id'] == 'agent-no-future-annotations'
    assert recall(BRAIN, 'zzzz qqqq') == []


def test_recall_finds_doc_sections():
    hits = recall(BRAIN, 'push tags by full ref', limit=20)
    assert any(d.get('kind') == 'doc' and 'release.md' in d['title'] for _, d in hits)


def test_tokens_are_whole_words():
    assert 'em' not in tokenize('item schema')
    assert tokenize('selfTestProbe') == ['self', 'test', 'probe']


def test_context_finds_anchored_facts_and_naming_docs():
    ids = [d['id'] for _, d in context_for(BRAIN, ['src/swarm_persona/agent.py'], limit=30)]
    assert 'agent-no-future-annotations' in ids
    assert any(i.startswith('.brain/adr/0001') for i in ids)


def test_path_affinity():
    assert path_affinity('src/swarm_persona/guard/', 'src/swarm_persona/guard/policy.py') == 2.0
    assert path_affinity('src/**/*.py', 'src/swarm_persona/run.py') == 2.5
    assert path_affinity('src/swarm_persona/run.py', 'src/swarm_persona/judge.py') == 1.0
    assert path_affinity('README.md', 'src/swarm_persona/run.py') == 0.0


def test_context_lists_the_tests_that_import_a_file():
    assert 'tests/test_judge.py' in importing_tests(BRAIN, 'src/swarm_persona/judge.py')
    assert 'tests/test_fixes.py' in importing_tests(BRAIN, 'src/swarm_persona/fixes.py')  # from swarm_persona import fixes
    assert 'tests/test_policy.py' in importing_tests(BRAIN, 'src/swarm_persona/guard/policy.py')
    assert importing_tests(BRAIN, 'README.md') == []


def test_preferences_live_in_one_file_per_person(copy, monkeypatch):
    monkeypatch.setenv('BRAIN_USER', 'sam')
    handle, pref = add_preference(copy, 'short commit subjects')
    assert handle == 'sam'
    person = json.loads((copy / 'people' / 'sam.json').read_text())
    assert [p['text'] for p in person['preferences']] == ['short commit subjects']
    assert current_user(copy)['person']['handle'] == 'sam'
    assert doctor(copy) == []
    with pytest.raises(ValueError, match='edit only your own'):
        add_preference(copy, 'x', as_handle='alex')
    assert remove_preference(copy, pref['id']) == 'sam'
    assert remove_preference(copy, pref['id']) is None


def test_email_handles_never_collide():
    assert slug_handle('alex@a.com') != slug_handle('alex@b.com')
    assert slug_handle('alex@a.com').startswith('alex-')
    assert slug_handle('../../etc') == 'etc'


def test_the_cli_runs_doctor_and_recall():
    cli = [sys.executable, str(BRAIN / 'bin' / 'brain')]
    done = subprocess.run([*cli, 'doctor'], capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == 'brain is healthy', done.stdout + done.stderr
    done = subprocess.run([*cli, 'recall', 'fingerprint', 'tickets'], capture_output=True, text=True)
    assert 'fingerprint-is-public-contract' in done.stdout


def test_the_package_build_leaves_the_brain_out():
    hatch = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['hatch']['build']['targets']
    assert hatch['wheel']['packages'] == ['src/swarm_persona']
    assert '.brain' in hatch['sdist']['exclude']


def test_a_brain_user_email_finds_its_own_file_again(copy, monkeypatch):
    monkeypatch.setenv('BRAIN_USER', 'sam@example.com')
    add_preference(copy, 'first')
    handle, _ = add_preference(copy, 'second')
    person = json.loads((copy / 'people' / f'{handle}.json').read_text())
    assert 'sam@example.com' in person['identities'] and len(person['preferences']) == 2


def test_a_handle_someone_else_holds_is_refused(copy, monkeypatch):
    monkeypatch.setenv('BRAIN_USER', 'sam')
    add_preference(copy, 'mine')
    monkeypatch.setenv('BRAIN_USER', 'nobody@example.com')
    with pytest.raises(ValueError, match='belongs to someone else'):
        add_preference(copy, 'x', as_handle='sam')


def test_a_shared_display_name_never_resolves_to_someone_else(copy, monkeypatch):
    import brain_people
    (copy / 'people').mkdir()
    (copy / 'people' / 'sam.json').write_text(json.dumps(
        {'handle': 'sam', 'name': 'Sam', 'identities': ['Sam'], 'preferences': []}))
    monkeypatch.delenv('BRAIN_USER', raising=False)
    monkeypatch.setattr(brain_people, 'git_identity', lambda root: {'name': 'Sam', 'email': 'other@example.com'})
    assert current_user(copy)['person'] is None


def test_a_handle_cannot_leave_the_people_folder(copy):
    from brain_people import save_person
    with pytest.raises(ValueError, match='invalid handle'):
        save_person(copy, {'handle': '../../x', 'identities': [], 'preferences': []})


def test_bad_people_files_fail(copy):
    (copy / 'people').mkdir()
    (copy / 'people' / 'sam.json').write_text(json.dumps({'handle': 'alex', 'identities': [], 'preferences': []}))
    assert doctor(copy) == ["people/sam.json: handle 'alex' does not match the file name"]


def test_malformed_paths_are_reported_not_crashed_on(copy):
    path = copy / 'facts' / 'gotchas.json'
    facts = json.loads(path.read_text())
    facts[0]['paths'] = 5
    path.write_text(json.dumps(facts))
    assert any('paths must be a list' in i for i in doctor(copy))
    assert recall(copy, 'annotations') and context_for(copy, ['src/swarm_persona/agent.py']) is not None
    done = subprocess.run([sys.executable, str(BRAIN / 'bin' / 'brain'), 'show', facts[0]['id']], capture_output=True,
                          text=True, env={**os.environ, 'BRAIN_DIR': str(copy)})
    assert done.returncode == 0 and 'paths: -' in done.stdout, done.stderr


def test_context_accepts_dot_slash_paths():
    assert 'tests/test_judge.py' in importing_tests(BRAIN, './src/swarm_persona/judge.py')
    ids = [d['id'] for _, d in context_for(BRAIN, ['./src/swarm_persona/agent.py'], limit=30)]
    assert 'agent-no-future-annotations' in ids
    assert any(i.startswith('.brain/adr/0001') for i in ids)  # docs that name the file match too


def test_remember_writes_only_ids_doctor_accepts(copy):
    cli = [sys.executable, str(BRAIN / 'bin' / 'brain')]
    env = {**os.environ, 'BRAIN_DIR': str(copy)}

    def run(*args):
        return subprocess.run([*cli, 'remember', *args], capture_output=True, text=True, env=env)

    assert run('note', 'go', '--source', 'x').returncode == 0
    assert run('note', 'go', '--source', 'x').returncode == 0  # same slug: a numbered id
    assert run('note', 'text', '--id', 'Bad ID').returncode != 0
    assert 'is taken' in run('note', 'text', '--id', 'no-em-dashes').stderr
    ids = [f['id'] for f in json.loads((copy / 'facts' / 'notes.json').read_text())]
    assert ids[-2:] == ['fact-go', 'fact-go-2']
    assert doctor(copy) == []


def test_the_cli_doctor_exits_non_zero_on_a_problem(copy):
    (copy / 'plans' / 'loose.md').write_text('# Loose\n')
    done = subprocess.run([sys.executable, str(BRAIN / 'bin' / 'brain'), 'doctor'], capture_output=True, text=True,
                          env={**os.environ, 'BRAIN_DIR': str(copy)})
    assert done.returncode == 1 and 'plans/loose.md' in done.stdout
