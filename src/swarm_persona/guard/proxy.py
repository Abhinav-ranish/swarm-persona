"""One guard proxy (mitmdump + addon.py) per browser, and the proof that it refuses before use."""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..config import Config

ADDON = Path(__file__).with_name('addon.py')


def policy_dict(cfg: Config, origin: str) -> dict:
    g = cfg.guard
    return {
        'origin': origin, 'app_hosts': sorted(cfg.app_hosts(origin)), 'denied_paths': g.denied_paths,
        'allowed_writes': g.allowed_writes, 'forbidden_body_keys': g.forbidden_body_keys,
        'forbidden_body_values': g.forbidden_body_values, 'json_only_paths': g.json_only_paths,
        'allow_delete': g.allow_delete,
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class GuardProxy:
    """mitmdump with the guard for one browser. start() does not return until the proxy has
    refused a write outside the allowlist, another site as a frame and every configured self-test
    request, so a broken guard stops the run rather than the check."""

    def __init__(self, cfg: Config, origin: str, confdir: Path, logdir: Path, name: str) -> None:
        self.cfg, self.origin, self.confdir = cfg, origin, confdir
        self.log = logdir / f'{name}.guard.jsonl'
        self.server_log = logdir / f'{name}.server.jsonl'
        self.policy_file = logdir / f'{name}.policy.json'
        self.port, self.proc = free_port(), None

    def start(self) -> None:
        self.policy_file.write_text(json.dumps(policy_dict(self.cfg, self.origin)))
        env = {**os.environ, 'SWARM_PERSONA_POLICY': str(self.policy_file),
               'SWARM_PERSONA_GUARD_LOG': str(self.log), 'SWARM_PERSONA_SERVER_LOG': str(self.server_log)}
        stderr = self.log.with_suffix('.stderr')
        self.proc = subprocess.Popen(
            [os.environ.get('SWARM_PERSONA_MITMDUMP', 'mitmdump'), '--quiet', '--listen-host', '127.0.0.1',
             '--listen-port', str(self.port), '--set', f'confdir={self.confdir}', '-s', str(ADDON)],
            # stderr to a file, never an unread pipe: a full pipe would block mitmdump mid-run.
            env=env, stdout=subprocess.DEVNULL, stderr=stderr.open('ab'),
        )
        ca = self.ca
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f'guard proxy exited: {stderr.read_text(errors="replace")[-400:]}')
            try:
                socket.create_connection(('127.0.0.1', self.port), timeout=1).close()
                if ca.exists():
                    break
            except OSError:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError('guard proxy did not start')
        self.self_test()

    @property
    def ca(self) -> Path:
        return self.confdir / 'mitmproxy-ca-cert.pem'

    @property
    def server(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    def probes(self) -> list[tuple[str, str, dict]]:
        hosts = self.cfg.app_hosts(self.origin)
        foreign = next(h for h in ('example.org', 'example.net', 'iana.org') if h not in hosts)
        probes = [('POST', f'{self.origin}/__swarm_persona_guard_selftest', {}),
                  ('GET', f'https://{foreign}/', {'Sec-Fetch-Dest': 'iframe'})]
        probes += [(t.method.upper(), f'{self.origin}{t.path}', {}) for t in self.cfg.guard.self_test]
        return probes

    def self_test(self) -> None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'https': self.server}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(self.ca))),
        )
        for method, url, headers in self.probes():
            request = urllib.request.Request(url, method=method, headers=headers,
                                             data=b'{}' if method not in ('GET', 'HEAD') else None)
            try:
                opener.open(request, timeout=20)
                status, body = 200, ''
            except urllib.error.HTTPError as error:
                status, body = error.code, error.read().decode('utf-8', 'replace')
            if status != 403 or 'swarm-persona guard' not in body:
                raise RuntimeError(f'guard proxy let {method} {url} through (HTTP {status}); not running')
        self.log.write_text('')  # the self-test's own refusals are not the persona's

    def records(self, which: str = 'guard') -> list[dict]:
        path = self.log if which == 'guard' else self.server_log
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:  # the proxy may be mid-write (a live log, or one cut off by stop): skip what does not parse
                out.append(json.loads(line))
            except ValueError:
                continue
        return [r for r in out if isinstance(r, dict)]

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
