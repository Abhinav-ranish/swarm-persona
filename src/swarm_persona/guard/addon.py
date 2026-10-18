"""mitmproxy addon: the write guard at the network layer. Loaded with `mitmdump -s addon.py`.

guard/proxy.py starts one mitmdump per persona with this addon and points that persona's browser
at it, so every request the browser makes passes through here: every tab, cross-site frame,
service worker, beacon, form post and plain navigation, whatever the page's own JavaScript does.

mitmdump runs from its own tool environment, so this file imports only the standard library, its
sibling policy.py, and mitmproxy.

Environment:
  SWARM_PERSONA_POLICY      JSON file: origin, app_hosts, denied_paths, allowed_writes, ...
  SWARM_PERSONA_GUARD_LOG   JSONL file of refused requests
  SWARM_PERSONA_SERVER_LOG  JSONL file of 5xx answers from the app's own hosts (hard signals)
"""

import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent))
from policy import Policy, internal_host, internal_ip, verdict  # noqa: E402

import ipaddress  # noqa: E402


def append(path: str, record: dict) -> None:
    with open(path, 'a') as fh:
        fh.write(json.dumps({**record, 'at': time.time()}) + '\n')


def resolve(host: str, port: int) -> list[str]:
    """Every address the name resolves to, IPv4 first (CI runners often have no IPv6 route), in the
    resolver's order otherwise."""
    seen: list[str] = []
    for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
        if info[4][0] not in seen:
            seen.append(info[4][0])
    return [a for a in seen if ':' not in a] + [a for a in seen if ':' in a]


class Guard:
    def __init__(self) -> None:
        self.policy = Policy.from_dict(json.loads(Path(os.environ['SWARM_PERSONA_POLICY']).read_text()))
        if not self.policy.origin.startswith('https://'):
            raise SystemExit(f'policy origin must be https, got {self.policy.origin!r}')
        self.log = os.environ['SWARM_PERSONA_GUARD_LOG']
        self.server_log = os.environ.get('SWARM_PERSONA_SERVER_LOG')
        self.ports = {443, urlsplit(self.policy.origin).port or 443}

    def refuse(self, flow, reason: str) -> None:
        from mitmproxy import http

        flow.response = http.Response.make(
            403, json.dumps({'error': f'Blocked by swarm-persona guard ({reason})'}),
            {'Content-Type': 'application/json'},
        )
        req = flow.request
        append(self.log, {'method': req.method, 'url': req.pretty_url.split('?')[0],
                          'dest': req.headers.get('sec-fetch-dest', ''), 'reason': reason})

    def http_connect(self, flow) -> None:
        """CONNECT host:port. Only 443 (or the app's own port) on a host that is not internal by name."""
        host, port = flow.request.host, flow.request.port
        if port not in self.ports:
            self.refuse(flow, f'CONNECT to port {port}')
        elif internal_host(host):
            self.refuse(flow, 'internal address')

    def server_connect(self, data) -> None:
        """Resolve the upstream once, refuse if any address is internal, and pin the connection to
        the address checked, so a DNS answer that changes between the check and the connect
        (rebinding) cannot reach an internal address."""
        host, port = data.server.address
        try:
            ipaddress.ip_address(host.strip('[]'))
            data.server.error = 'swarm-persona guard: connection to an IP literal'
            return
        except ValueError:
            pass
        try:
            addresses = resolve(host, port)
        except OSError as error:
            data.server.error = f'swarm-persona guard: cannot resolve {host}: {error}'
            return
        if not addresses or any(internal_ip(ipaddress.ip_address(a.split('%')[0])) for a in addresses):
            data.server.error = f'swarm-persona guard: {host} resolves to an internal address'
            append(self.log, {'method': 'CONNECT', 'url': f'https://{host}:{port}', 'dest': '',
                              'reason': 'resolves to an internal address'})
            return
        if data.server.sni is None:
            data.server.sni = host
        data.server.address = (addresses[0], port)

    def request(self, flow) -> None:
        req = flow.request
        try:
            upgrade = bool(req.headers.get('upgrade')) or 'upgrade' in req.headers.get('connection', '').lower()
            # The decoded body: a gzip or br body is judged as the app will read it.
            body = req.get_content(strict=False)
            headers = {k.lower(): v for k, v in req.headers.items(multi=True)}
            reason = verdict(self.policy, req.method, req.pretty_url, headers.get('sec-fetch-dest', ''),
                             body, upgrade, headers)
        except Exception as error:  # an unparseable request is refused, never waved through
            reason = f'guard error: {type(error).__name__}'
        if reason:
            self.refuse(flow, reason)

    def response(self, flow) -> None:
        if not self.server_log or flow.response is None or flow.response.status_code < 500:
            return
        if (flow.request.host or '').lower() in self.policy.app_hosts:
            append(self.server_log, {'method': flow.request.method, 'path': flow.request.path.split('?')[0],
                                     'status': flow.response.status_code})

    def websocket_start(self, flow) -> None:
        flow.kill()

    def tcp_start(self, flow) -> None:
        flow.kill()

    def udp_start(self, flow) -> None:
        flow.kill()


if os.environ.get('SWARM_PERSONA_POLICY'):
    addons = [Guard()]
