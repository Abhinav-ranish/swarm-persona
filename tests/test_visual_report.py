import json

from evidence_builder import check, evidence, issue, session

from swarm_persona.run import replay

PNG = b'\x89PNG\r\n\x1a\n' + b'fixture'


def test_replay_builds_a_portable_visual_report_with_top_three_and_all_findings(tmp_path):
    (tmp_path / 'shots').mkdir()
    (tmp_path / 'shots' / 'one.png').write_bytes(PNG)
    raw = [issue('preview', i, title=f'Issue {i}', screenshot='shots/one.png') for i in range(5)]
    ev = evidence(sessions=[session('preview', 1)], raw=raw, baseline_ran=False)
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    page = (tmp_path / 'report.html').read_text()
    assert 'Start with these 3' in page and '2 more findings' in page
    assert 'data:image/png;base64,' in page
    assert 'Suggested next step' in page
    assert 'Agent-reported' in page
    assert 'Content-Security-Policy' in page and 'script-src' not in page
    assert '<script' not in page
    assert 'Issue 4' in page


def test_visual_report_escapes_page_text_and_never_reads_screenshot_symlinks(tmp_path):
    secret = tmp_path / 'private.png'
    secret.write_bytes(PNG + b'secret')
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'shots').mkdir()
    (out / 'shots' / 'leak.png').symlink_to(secret)
    ev = evidence(sessions=[session('preview', 1)],
                  raw=[issue('preview', 1, title='<script>alert(1)</script>',
                             detail='<img src=x onerror=alert(1)>', screenshot='shots/leak.png')], baseline_ran=False)
    (out / 'evidence.json').write_text(json.dumps(ev))
    replay(out)
    page = (out / 'report.html').read_text()
    assert '<script>' not in page and '<img src=x' not in page
    assert '&lt;script&gt;' in page
    assert 'data:image' not in page


def test_visual_report_explains_incomplete_tests_and_check_only_limitations(tmp_path):
    ev = evidence(sessions=[session('preview', 1, crashed=True, steps=0)], baseline_ran=False)
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    page = (tmp_path / 'report.html').read_text()
    assert 'Testing incomplete' in page and 'Could not finish testing' in page
    assert 'No issues reported' not in page
    ev = evidence(checks=[check('preview')], baseline_ran=False)
    ev['meta'].update(mode='checks-only', targets=['preview'], checks_per_target=1)
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    page = (tmp_path / 'report.html').read_text()
    assert 'Entry checks only' in page and 'No personas attempted the task' in page
    assert 'Checks completed' in page


def test_retest_comparison_is_visible_but_does_not_claim_a_fix(tmp_path):
    ev = evidence(sessions=[session('preview', 1)], baseline_ran=False)
    ev['comparison'] = {'previous_run': 'one', 'conclusive': True, 'seen_again': 0,
                        'not_observed': 1, 'newly_observed': 0}
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    page = (tmp_path / 'report.html').read_text()
    assert '1 not observed this time' in page and 'does not prove' in page


def test_comparison_counts_are_escaped_when_replaying_untrusted_evidence(tmp_path):
    ev = evidence(sessions=[session('preview', 1)], baseline_ran=False)
    ev['comparison'] = {'previous_run': 'one', 'conclusive': True, 'seen_again': '<meta http-equiv="refresh" content="0">',
                        'not_observed': 0, 'newly_observed': 0}
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    page = (tmp_path / 'report.html').read_text()
    assert '<meta http-equiv="refresh"' not in page
    assert '&lt;meta' in page


def test_embedded_images_share_one_document_budget(tmp_path):
    from swarm_persona.visual_report import screenshot
    (tmp_path / 'shots').mkdir()
    (tmp_path / 'shots' / 'one.png').write_bytes(PNG)
    budget = [len(PNG)]
    assert 'data:image/png' in screenshot(tmp_path, 'one.png', budget)
    assert 'Image omitted' in screenshot(tmp_path, 'one.png', budget)
    assert budget == [0]


def test_missing_screenshots_are_explained_instead_of_silently_hidden(tmp_path):
    ev = evidence(sessions=[session('preview', 1)], raw=[issue('preview', 1)], baseline_ran=False)
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    page = (tmp_path / 'report.html').read_text()
    assert 'Screenshot unavailable' in page
    assert 'not included in this saved run' in page


def test_entry_screenshots_are_visible_outside_collapsed_run_details(tmp_path):
    (tmp_path / 'shots').mkdir()
    (tmp_path / 'shots' / 'desktop.png').write_bytes(PNG)
    ev = evidence(checks=[check('preview', screenshot='shots/desktop.png')], baseline_ran=False)
    ev['meta'].update(mode='checks-only', targets=['preview'], checks_per_target=1)
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    page = (tmp_path / 'report.html').read_text()
    assert 'Page captures' in page
    assert page.index('data:image/png') < page.index('<details><summary>Run details')
    assert 'first-task' in page or 'write' in page


def test_findings_are_not_duplicated_and_run_notes_are_prominent(tmp_path):
    ev = evidence(sessions=[session('preview', 1)],
                  raw=[issue('preview', i, title=f'Unique finding {i}') for i in range(5)], baseline_ran=False)
    ev['meta']['notes'] = ['Recorded demonstration, not a live persona test.']
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    page = (tmp_path / 'report.html').read_text()
    assert page.count('Unique finding 0') == 1
    assert '2 more findings' in page
    assert page.index('Recorded demonstration') < page.index('</header>')
    assert page.count('Confirm the observation before changing the product.') == 1


def test_captures_have_dimensions_result_labels_and_explain_missing_checks(tmp_path):
    (tmp_path / 'shots').mkdir()
    data = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' + (1440).to_bytes(4, 'big') + (900).to_bytes(4, 'big')
    (tmp_path / 'shots' / 'desktop.png').write_bytes(data)
    ev = evidence(checks=[check('preview', screenshot='shots/desktop.png'), check('preview', device='mobile')], baseline_ran=False)
    ev['meta'].update(mode='checks-only', targets=['preview'], checks_per_target=2)
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    page = (tmp_path / 'report.html').read_text()
    assert 'width="1440" height="900"' in page
    assert 'HTTP 200' in page
    assert '1 of 2 entry checks have no saved screenshot' in page
    assert 'No issues reported' not in page
