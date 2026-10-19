import asyncio
import json
from pathlib import Path

from evidence_builder import check, evidence, issue

from swarm_persona import fixes
from swarm_persona.config import Config
from swarm_persona.judge import judge
from swarm_persona.run import replay

FIXTURE = Path(__file__).parent / 'fixtures' / 'run' / 'evidence.json'
DIFF = """diff --git a/src/app/write/page.tsx b/src/app/write/page.tsx
--- a/src/app/write/page.tsx
+++ b/src/app/write/page.tsx
@@ -1 +1 @@
-<Save/>
+<Save hidden/>
diff --git a/src/billing/pay.ts b/src/billing/pay.ts
--- a/src/billing/pay.ts
+++ b/src/billing/pay.ts
@@ -1 +1 @@
-secret
+other
"""


def cfg(**report) -> Config:
    return Config.model_validate({
        'app': {'name': 'A', 'description': 'd', 'url': 'https://preview.example.com'},
        'journeys': [{'name': 'write', 'goal': 'Write.', 'owns': ['src/app/write/**', 'config/**']}],
        'report': {'suggest_fixes': True, 'fix_max_tokens': 500, **report}, 'budget': {'max_tokens': 1000}})


def fake_llm(monkeypatch, reply=None):
    calls = []

    async def complete(llm, prompt, max_tokens=12000):
        calls.append((prompt, max_tokens))
        return json.dumps(reply or {'file': 'src/app/write/page.tsx', 'change': 'Remove hidden.'}), 100

    monkeypatch.setattr(fixes, 'complete', complete)
    return calls


def findings(baseline_ran=False):
    ev = evidence(checks=[check('preview', status=500), check('preview', device='mobile', status=500),
                          check('preview', console_errors=['noise'])],
                  raw=[issue('preview', 1, severity='minor', category='copy')], baseline_ran=baseline_ran)
    return judge(ev, ['blocker'])['findings']


def test_the_diff_is_split_per_file_restricted_to_owned_globs_and_capped():
    files = fixes.split_diff(DIFF)
    assert list(files) == ['src/app/write/page.tsx', 'src/billing/pay.ts']
    text, paths = fixes.owned_diff(files, ['src/app/write/**'], 10_000)
    assert paths == ['src/app/write/page.tsx'] and 'pay.ts' not in text and '+<Save hidden/>' in text
    assert len(fixes.owned_diff(files, ['src/**'], 50)[0]) == 50


def test_one_capped_call_per_new_or_unclassified_major_finding_counted_in_the_budget(monkeypatch):
    calls = fake_llm(monkeypatch)
    spent, notes = {'tokens': 0}, []
    out = asyncio.run(fixes.suggest(cfg(fix_max_tokens=300), findings(), DIFF, spent, notes))
    [blocker] = [f for f in findings() if f['severity'] == 'blocker']
    assert list(out) == [blocker['key']] and len(calls) == 1  # minor console and copy findings skipped
    assert out[blocker['key']] == {'file': 'src/app/write/page.tsx', 'change': 'Remove hidden.'}
    prompt, max_tokens = calls[0]
    assert max_tokens == 300 and 'pay.ts' not in prompt and 'Save hidden' in prompt
    assert spent['tokens'] == 100 and notes == []


def test_pre_existing_findings_get_no_fix(monkeypatch):
    calls = fake_llm(monkeypatch)
    ev = evidence(checks=[check('preview', status=500), check('baseline', status=500)])
    assert asyncio.run(fixes.suggest(cfg(), judge(ev, ['blocker'])['findings'], DIFF, {'tokens': 0}, [])) == {}
    assert calls == []


def test_no_diff_disabled_or_spent_budget_means_no_call(monkeypatch):
    calls = fake_llm(monkeypatch)
    assert asyncio.run(fixes.suggest(cfg(), findings(), '', {'tokens': 0}, [])) == {}
    assert asyncio.run(fixes.suggest(cfg(suggest_fixes=False), findings(), DIFF, {'tokens': 0}, [])) == {}
    notes = []
    assert asyncio.run(fixes.suggest(cfg(), findings(), DIFF, {'tokens': 501}, notes)) == {}  # 501 + 500 > 1000
    assert calls == [] and notes == ['Budget: possible fixes stopped, budget.max_tokens would be exceeded.']
    assert asyncio.run(fixes.suggest(cfg(), findings(), DIFF, {'tokens': 500}, [])) and len(calls) == 1


def test_a_file_outside_the_owned_diff_is_dropped_and_an_empty_change_is_no_fix():
    assert fixes.parse('{"file": "src/billing/pay.ts", "change": "x"}', ['a.ts']) == {'file': None, 'change': 'x'}
    assert fixes.parse('{"file": "a.ts", "change": ""}', ['a.ts']) is None
    assert fixes.parse('no json', ['a.ts']) is None


def test_the_diff_comes_from_the_saved_context_file(tmp_path):
    ctx = tmp_path / 'pr.json'
    ctx.write_text(json.dumps({'title': 't', 'files': [], 'diff': DIFF}))
    assert fixes.pr_diff(None, str(ctx)) == DIFF
    ctx.write_text(json.dumps({'title': 't'}))
    assert fixes.pr_diff(None, str(ctx)) == ''


def test_replay_shows_the_possible_fix_defanged(tmp_path):
    (tmp_path / 'evidence.json').write_text(FIXTURE.read_text())
    report = replay(tmp_path)['report']
    assert 'Possible fix (unverified): in `src/app/write/page.tsx`: Give the cookie bar a lower z-index' in report
    assert '@octocat' not in report
    saved = json.loads((tmp_path / 'report.json').read_text())
    assert sum(1 for f in saved['findings'] if f['possible_fix']) == 1


def test_fixes_are_opt_in():
    assert Config.model_validate({'app': {'name': 'A', 'description': 'd',
                                          'url': 'https://p.example.com'}}).report.suggest_fixes is False


SECRET_DIFF = DIFF + """diff --git a/config/.env.production b/config/.env.production
+API_KEY=sk-live-123
diff --git a/config/server.pem b/config/server.pem
+-----BEGIN PRIVATE KEY-----
diff --git a/config/db_secrets.yml b/config/db_secrets.yml
+password: hunter2
diff --git a/config/app.yml b/config/app.yml
+z_index: 3
"""


def test_secret_like_files_never_reach_the_model(monkeypatch):
    calls = fake_llm(monkeypatch)
    asyncio.run(fixes.suggest(cfg(), findings(), SECRET_DIFF, {'tokens': 0}, []))
    prompt = calls[0][0]
    assert 'config/app.yml' in prompt and 'z_index' in prompt
    for leaked in ('sk-live-123', 'PRIVATE KEY', 'hunter2', '.env.production'):
        assert leaked not in prompt
    assert [p for p in ['.env', 'a/.env.local', 'k.key', 'id_rsa', 'x/.npmrc', 'Secrets.json', 'credentials.ts']
            if not fixes.secret_like(p)] == []
    assert not fixes.secret_like('src/app/write/page.tsx')


def many_majors():
    from evidence_builder import check as chk
    ev = evidence(checks=[chk('preview', status=500, page_errors=[f'Error {c}' for c in 'abcdefg'])], baseline_ran=False)
    return [f for f in judge(ev, ['blocker'])['findings'] if f['severity'] in ('blocker', 'major')]


def test_one_failed_call_does_not_drop_the_others(monkeypatch):
    replies = iter([RuntimeError('throttled'), {'file': None, 'change': 'Fix it.'}])

    async def complete(llm, prompt, max_tokens=12000):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply), 10

    monkeypatch.setattr(fixes, 'complete', complete)
    notes = []
    out = asyncio.run(fixes.suggest(cfg(), many_majors()[:2], DIFF, {'tokens': 0}, notes))
    assert len(out) == 1 and notes == ['No possible fix for finding 1: RuntimeError.']


def test_the_cap_counts_attempts_not_successes(monkeypatch):
    calls = fake_llm(monkeypatch, {'file': None, 'change': ''})  # every reply unusable
    notes = []
    majors = many_majors()
    assert len(majors) > fixes.MAX_SUGGESTIONS
    assert asyncio.run(fixes.suggest(cfg(), majors, DIFF, {'tokens': 0}, notes)) == {}
    assert len(calls) == fixes.MAX_SUGGESTIONS
    assert notes == [f'Possible fixes stopped at the cap of {fixes.MAX_SUGGESTIONS} calls per run.']


def test_an_error_in_the_fix_step_never_loses_the_run(tmp_path, monkeypatch):
    from swarm_persona import run as run_mod

    async def boom(*a, **k):
        raise RuntimeError('model down')

    ctx = tmp_path / 'pr.json'
    ctx.write_text(json.dumps({'diff': DIFF}))
    monkeypatch.setattr(run_mod, 'suggest', boom)
    notes = []
    ev = json.loads(FIXTURE.read_text())
    assert asyncio.run(run_mod.add_fixes(cfg(), ev, None, str(ctx), {'tokens': 0}, notes)) == {}
    assert notes == ['Possible fixes were skipped: RuntimeError.']
    monkeypatch.setattr(run_mod, 'pr_diff', lambda *a: (_ for _ in ()).throw(ValueError('bad json')))
    assert asyncio.run(run_mod.add_fixes(cfg(), ev, '7', None, {'tokens': 0}, notes)) == {}
    assert asyncio.run(run_mod.add_fixes(cfg(suggest_fixes=False), ev, '7', None, {'tokens': 0}, [])) == {}


def test_each_finding_gets_its_own_fix_even_when_fingerprints_collide(monkeypatch, tmp_path):
    from evidence_builder import issue as raw_issue

    replies = iter([{'file': None, 'change': 'Fix the save handler.'}, {'file': None, 'change': 'Narrow the sidebar.'}])

    async def complete(llm, prompt, max_tokens=12000):
        return json.dumps(next(replies)), 10

    monkeypatch.setattr(fixes, 'complete', complete)
    ev = evidence(raw=[raw_issue('preview', 1, title='Save does nothing', detail='a'),
                       raw_issue('preview', 2, title='Sidebar covers text', detail='b')], baseline_ran=False)
    found = [f for f in judge(ev, ['blocker'])['findings'] if f['source'] == 'agent']
    assert found[0]['fingerprint'] == found[1]['fingerprint']
    ev['fixes'] = asyncio.run(fixes.suggest(cfg(), found, DIFF, {'tokens': 0}, []))
    rejudged = {f['title']: f['possible_fix']['change'] for f in judge(ev, ['blocker'])['findings'] if f['source'] == 'agent'}
    assert rejudged == {'Save does nothing': 'Fix the save handler.', 'Sidebar covers text': 'Narrow the sidebar.'}
