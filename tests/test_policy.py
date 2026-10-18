import ipaddress

import pytest

from swarm_persona.guard.policy import Policy, internal_host, internal_ip, normal_path, verdict

APP = 'https://app.example.com'
POST_ID = '0f8e5c1a-1111-4222-8333-944455556666'
POLICY = Policy.from_dict({
    'origin': APP,
    'app_hosts': ['www.app.example.com'],
    'denied_paths': [r'^/api/integrations/', r'^/book-demo(/|$)', r'^/api/cron/'],
    'allowed_writes': [r'^/api/generate$', r'^/api/posts$', rf'^/api/posts/[0-9a-f-]{{36}}$', r'^/api/auth/refresh$'],
    'forbidden_body_keys': ['scheduled_publish_at', 'scheduled_date', 'social_account_id'],
    'forbidden_body_values': {'status': ['scheduled', 'published'], 'series_approved': [True]},
    'json_only_paths': [r'^/api/posts'],
})


def v(method, url, dest='', body=None, upgrade=False):
    return verdict(POLICY, method, url, dest, body, upgrade)


@pytest.mark.parametrize('method,url,body', [
    ('GET', f'{APP}/write', None),
    ('GET', f'{APP}/api/posts', None),
    ('POST', f'{APP}/api/generate', b'{"prompt":"x"}'),
    ('POST', f'{APP}/api/posts', b'{"status":"draft"}'),
    ('PATCH', f'{APP}/api/posts/{POST_ID}', b'{"content":"x","scheduled_date":null}'),
    ('PATCH', f'{APP}/api/posts/{POST_ID}', b'{"series_approved":false}'),
])
def test_reads_and_drafting_pass(method, url, body):
    assert v(method, url, body=body) is None


def test_cross_site_subresources_pass_but_pages_and_frames_do_not():
    assert v('GET', 'https://cdn.example.net/a.js', dest='script') is None
    assert v('GET', 'https://calendar.example.net/book', dest='iframe') == 'another site as a page or frame'
    assert v('GET', 'https://oauth.example.net/', dest='document') == 'another site as a page or frame'
    assert v('GET', 'https://oauth.example.net/') == 'another site as a page or frame'  # no Sec-Fetch-Dest
    assert v('POST', 'https://api.example.net/beacon', dest='empty') == 'cross-origin write'


@pytest.mark.parametrize('path', ['/api/integrations/notion/connect', '/book-demo', '/book-demo/thanks',
                                  '/api/cron/publish', '/API/Integrations/x', '/api/%69ntegrations/x',
                                  '/api/%2569ntegrations/x', '//api//integrations/x', '/x/../api/integrations/y'])
def test_routes_that_act_when_opened_are_refused_even_as_gets(path):
    assert v('GET', APP + path, dest='document') == 'route that acts when opened'


def test_denied_routes_apply_on_every_app_host():
    assert v('GET', 'https://www.app.example.com/book-demo', dest='document') == 'route that acts when opened'
    assert v('POST', 'https://www.app.example.com/api/generate', body=b'{}') == 'write to another app host'


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/generate/jobs/run'), ('POST', '/api/write/mentions'), ('POST', '/api/billing'),
    ('PUT', '/api/settings'), ('POST', '/api/%70osts'),
])
def test_writes_outside_the_exact_allowlist_are_refused(method, path):
    assert v(method, APP + path, body=b'{}') == 'write outside the allowlist'


def test_delete_is_refused_unless_allowed():
    assert v('DELETE', f'{APP}/api/posts/{POST_ID}') == 'DELETE'
    allowing = Policy.from_dict({'origin': APP, 'allowed_writes': [r'^/api/posts/'], 'allow_delete': True})
    assert verdict(allowing, 'DELETE', f'{APP}/api/posts/{POST_ID}', '', None) is None


@pytest.mark.parametrize('body', [
    b'{"scheduled_publish_at":"2030-01-01T00:00:00Z"}',
    b'{"scheduled\\u005fpublish_at":"2030-01-01"}',          # escaped key
    b'{"scheduledPublishAt":"2030-01-01"}',                   # camelCase
    b'{"Scheduled-Date":"2030-01-01"}',                       # other separators
    b'{"post":{"meta":[{"scheduled_date":"x"}]}}',            # nested
    b'{"status":"Published"}',
    b'{"series_approved":true}',
    b'{"social_account_id":"abc"}',
])
def test_bodies_that_schedule_publish_or_bind_are_refused(body):
    reason = v('PATCH', f'{APP}/api/posts/{POST_ID}', body=body)
    assert reason and reason.startswith('body sets')


def test_form_bodies_are_read_and_non_json_post_bodies_refused():
    assert v('POST', f'{APP}/api/generate', body=b'prompt=x&scheduled_date=2030').startswith('body sets')
    assert v('POST', f'{APP}/api/posts', body=b'not json') == 'body is not JSON'
    assert v('POST', f'{APP}/api/generate', body=b'prompt=x') is None


def test_transport_rules():
    assert v('GET', 'http://app.example.com/') == 'not https'
    assert v('GET', f'{APP}/', upgrade=True) == 'WebSocket or protocol upgrade'
    for url in ['https://127.0.0.1:9222/json', 'https://localhost/', 'https://169.254.169.254/latest',
                'https://[::1]/', 'https://[::ffff:127.0.0.1]/', 'https://10.0.0.5/', 'https://2130706433/',
                'https://0x7f.0.0.1/', 'https://metadata.internal/']:
        assert v('GET', url, dest='script') == 'internal address', url


def test_internal_detection_does_not_flag_ordinary_names():
    for host in ['bad.cafe', 'deadbeef.com', 'app.example.com', 'face.be']:
        assert not internal_host(host), host
    assert internal_ip(ipaddress.ip_address('::ffff:10.0.0.1'))
    assert internal_ip(ipaddress.ip_address('100.64.0.1'))  # carrier-grade NAT is not global
    assert not internal_ip(ipaddress.ip_address('93.184.216.34'))


def test_normal_path():
    assert normal_path('/A//b/%2E%2E/c') == '/a/c'
    assert normal_path('') == '/'


# Regressions from the pre-publication review: bodies and methods the app would read differently.
NOTES = Policy.from_dict({
    'origin': APP, 'allowed_writes': ['POST ^/api/notes$', '^/api/notes/[0-9]+$'],
    'forbidden_body_keys': ['publish_at'], 'forbidden_body_values': {'visibility': ['public']},
})


def n(method, path, body, ctype='', headers=None):
    return verdict(NOTES, method, APP + path, 'empty', body, False, {'content-type': ctype, **(headers or {})})


@pytest.mark.parametrize('body,ctype', [
    (b'\xef\xbb\xbf{"publish_at":"x"}', 'application/json'),          # UTF-8 byte-order mark
    (b'  \n{"publish_at":"x"}', 'text/plain'),                         # JSON sent as text/plain
    (b'{"a":1,"publish_at":"x","publish_at":null}', 'application/json'),  # duplicate key, last null
    (b'note[publish_at]=x', 'application/x-www-form-urlencoded'),       # Rails / qs nesting
    (b'note.publish_at=x', ''),
    (b'visibility=private&visibility=public', 'application/x-www-form-urlencoded'),
    (b'{"publish_at":{"at":"x"}}', 'application/json'),                  # object value counts as set
])
def test_bodies_the_app_would_read_are_judged(body, ctype):
    assert (n('POST', '/api/notes', body, ctype) or '').startswith('body sets'), body


@pytest.mark.parametrize('body,ctype,reason', [
    ('{"publish_at":"x"}'.encode('utf-16'), 'application/json', 'body is not UTF-8'),
    (b'{"a":1}', 'application/json; charset=utf-16', 'body charset utf-16'),
    (b'--b\r\nContent-Disposition: form-data; name="publish_at"\r\n\r\nx\r\n--b--', 'multipart/form-data; boundary=b',
     'body type multipart/form-data cannot be judged'),
    (b'publish it now', 'text/plain', 'body type text/plain cannot be judged'),
    (b'{"a":', 'application/json', 'body is not JSON'),
])
def test_bodies_that_cannot_be_judged_are_refused(body, ctype, reason):
    assert n('POST', '/api/notes', body, ctype) == reason


def test_method_overrides_are_refused():
    assert n('POST', '/api/notes', b'{}', 'application/json', {'x-http-method-override': 'DELETE'}) == 'method override header'
    assert n('GET', '/api/notes', None, '', {'x-http-method': 'DELETE'}) == 'method override header'
    assert n('POST', '/api/notes', b'_method=DELETE&t=x', 'application/x-www-form-urlencoded') == 'method override field'
    assert n('POST', '/api/notes', b'{"_method":"DELETE"}', 'application/json') == 'method override field'


def test_allowlist_entries_can_name_their_methods():
    assert n('POST', '/api/notes', b'{"t":1}', 'application/json') is None
    assert n('PUT', '/api/notes', b'{"t":1}', 'application/json') == 'write outside the allowlist'
    assert n('PATCH', '/api/notes/12', b'{"t":1}', 'application/json') is None
    assert n('PROPFIND', '/api/notes', None) == 'method PROPFIND'
    assert n('CONNECT', '/api/notes', None) == 'method CONNECT'


def test_matrix_parameters_cannot_dodge_a_denied_path_or_the_allowlist():
    assert v('GET', f'{APP}/book-demo;jsessionid=1', dest='document') == 'route that acts when opened'
    assert n('POST', '/api/notes;x=1', b'{}', 'application/json') == 'write outside the allowlist'
