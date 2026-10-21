import json

import pytest
from evidence_builder import check, evidence

from swarm_persona.run import replay


def png(width, height):
    return b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' + width.to_bytes(4, 'big') + height.to_bytes(4, 'big')


def page_with(tmp_path, data):
    (tmp_path / 'shots').mkdir()
    (tmp_path / 'shots' / 'a.png').write_bytes(data)
    ev = evidence(checks=[check('preview', screenshot='shots/a.png')], baseline_ran=False)
    ev['meta'].update(mode='checks-only', targets=['preview'], checks_per_target=1)
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    return (tmp_path / 'report.html').read_text()


@pytest.mark.parametrize('width,height', [(4_000_000_000, 900), (1440, 0), (0, 900), (100_001, 900)])
def test_implausible_png_dimensions_are_not_emitted(tmp_path, width, height):
    page = page_with(tmp_path, png(width, height))
    assert ' width="' not in page and '--capture-ratio:1.000' in page


@pytest.mark.parametrize('width,height,ratio', [(1440, 90000, '0.250'), (90000, 100, '3.000')])
def test_capture_aspect_ratio_is_clamped(tmp_path, width, height, ratio):
    assert f'--capture-ratio:{ratio}' in page_with(tmp_path, png(width, height))


def test_footer_does_not_claim_embedded_screenshots_when_there_are_none(tmp_path):
    ev = evidence(baseline_ran=False)
    (tmp_path / 'evidence.json').write_text(json.dumps(ev))
    replay(tmp_path)
    assert 'embedded locally' not in (tmp_path / 'report.html').read_text()
