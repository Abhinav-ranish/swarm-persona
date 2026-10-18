"""The guard proxy for real: mitmdump with the addon, over the network. Skipped without mitmdump
(set SWARM_PERSONA_MITMDUMP or put it on PATH) or without network access."""

import os
import shutil
import ssl
import urllib.error
import urllib.request

import pytest

from swarm_persona.config import Config
from swarm_persona.guard.proxy import GuardProxy

MITMDUMP = os.environ.get('SWARM_PERSONA_MITMDUMP') or shutil.which('mitmdump')
pytestmark = pytest.mark.skipif(not MITMDUMP, reason='mitmdump not installed')


@pytest.fixture(scope='module')
def proxy(tmp_path_factory):
    os.environ.setdefault('SWARM_PERSONA_MITMDUMP', MITMDUMP or '')
    cfg = Config.model_validate({
        'app': {'name': 'Example', 'description': 'd', 'url': 'https://example.com'},
        'guard': {'denied_paths': ['^/book-demo'], 'allowed_writes': ['^/api/drafts$'],
                  'self_test': [{'method': 'GET', 'path': '/book-demo'}]},
    })
    work = tmp_path_factory.mktemp('guard')
    p = GuardProxy(cfg, 'https://example.com', work, work, 'live')
    try:
        p.start()
    except RuntimeError as error:
        if 'resolve' in str(error) or 'did not start' in str(error):
            pytest.skip(f'no network for the live guard test: {error}')
        raise
    yield p
    p.stop()


def fetch(proxy, url, method='GET', headers=None, data=None):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({'https': proxy.server}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(proxy.ca))),
    )
    try:
        with opener.open(urllib.request.Request(url, method=method, headers=headers or {}, data=data), timeout=30) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except urllib.error.URLError as e:
        return None, str(e.reason)


def test_reads_pass_through(proxy):
    status, body = fetch(proxy, 'https://example.com/', headers={'Sec-Fetch-Dest': 'document'})
    assert status == 200 and 'Example Domain' in body


def test_writes_outside_the_allowlist_are_refused_and_logged(proxy):
    status, body = fetch(proxy, 'https://example.com/api/other', method='POST', data=b'{}')
    assert status == 403 and 'swarm-persona guard' in body
    assert any(r['reason'] == 'write outside the allowlist' for r in proxy.records())


def test_a_public_name_resolving_to_loopback_never_connects(proxy):
    # localtest.me and its subdomains resolve to 127.0.0.1 in public DNS.
    status, body = fetch(proxy, 'https://localtest.me/', headers={'Sec-Fetch-Dest': 'script'})
    assert status != 200
    assert any(r['reason'] == 'resolves to an internal address' for r in proxy.records())


def test_connect_to_another_port_is_refused(proxy):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy.server}))
    with pytest.raises(Exception):
        opener.open('https://example.com:8443/', timeout=15)
