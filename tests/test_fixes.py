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
        'journeys': [{'name': 'write', 'goal': 'Write.', 'owns': ['src/app/write/**']}],
        'report': report, 'budget': {'max_tokens': 1000}})


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
    assert list(out) == [blocker['fingerprint']] and len(calls) == 1  # minor console and copy findings skipped
    assert out[blocker['fingerprint']] == {'file': 'src/app/write/page.tsx', 'change': 'Remove hidden.'}
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
    assert asyncio.run(fixes.suggest(cfg(), findings(), DIFF, {'tokens': 1000}, notes)) == {}
    assert calls == [] and notes == ['Budget: some findings got no possible fix.']


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
