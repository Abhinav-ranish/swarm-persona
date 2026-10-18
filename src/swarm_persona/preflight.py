"""Checks that run before any browser starts. Each fails closed: an HTTP error, a reply that is not
a JSON object, or a missing key stops the run instead of passing it."""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config, Preflight
from .guard.policy import internal_host, internal_ip, norm_key


class PreflightError(SystemExit):
    pass


def cookie_header(state: dict, host: str) -> str:
    return '; '.join(f"{c['name']}={c['value']}" for c in state.get('cookies', [])
                     if (c.get('domain') or '').lstrip('.').lower() == host)


def check_storage_state(cfg: Config, origin: str, state: dict) -> None:
    """A storage state may only hold cookies scoped to one of the app's own hosts, exactly: a cookie
    for a parent domain (or a public suffix such as vercel.app) would go to other sites too. List a
    parent domain under app.hosts if the app really sets its session there."""
    hosts = cfg.app_hosts(origin)
    for cookie in state.get('cookies', []):
        domain = (cookie.get('domain') or '').lstrip('.').lower()
        if domain not in hosts:
            raise PreflightError(f'storage state holds a cookie for {domain!r}, which is not an app host')


def truthy_anywhere(value, keys: set[str], depth: int = 0) -> str | None:
    if depth > 20:
        return None
    if isinstance(value, list):
        return next((h for v in value if (h := truthy_anywhere(v, keys, depth + 1))), None)
    if isinstance(value, dict):
        for k, v in value.items():
            if norm_key(k) in keys and v is True:
                return k
            if (h := truthy_anywhere(v, keys, depth + 1)):
                return h
    return None


def judge(check: Preflight, reply) -> str | None:
    """Why this reply fails the check, or None."""
    if not isinstance(reply, dict):
        return f'{check.path} did not answer with a JSON object'
    for key in check.require_keys:
        node = reply
        for part in key.split('.'):
            if not isinstance(node, dict) or part not in node:
                return f'{check.path} has no {key!r}: its shape changed, so it cannot be judged'
            node = node[part]
    for key in check.fail_if_nonempty:
        v = reply.get(key)
        if v not in (None, False, 0, '', [], {}):
            return check.message
    if truthy_anywhere(reply, {norm_key(k) for k in check.fail_if_true}):
        return check.message
    return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies the Cookie header onto a redirect, to any host: never follow one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PreflightError(f'preflight {urlsplit(req.full_url).path} redirected (HTTP {code}); refusing to follow')


def check_host(url: str) -> None:
    host = urlsplit(url).hostname or ''
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except OSError as error:
        raise PreflightError(f'cannot resolve {host}: {error}')
    if internal_host(host) or any(internal_ip(ipaddress.ip_address(a.split('%')[0])) for a in addresses):
        raise PreflightError(f'{host} resolves to an internal address')


def get_json(url: str, cookies: str):
    check_host(url)
    req = urllib.request.Request(url, headers={'Cookie': cookies, 'User-Agent': 'swarm-persona',
                                               'Accept': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as resp:
            return json.loads(resp.read() or b'null')
    except urllib.error.HTTPError as error:
        raise PreflightError(f'preflight {urlsplit(url).path} answered HTTP {error.code}')
    except (urllib.error.URLError, ValueError) as error:
        raise PreflightError(f'preflight {urlsplit(url).path} failed: {error}')


def run(cfg: Config, origin: str, storage_state: str | None) -> list[str]:
    state = json.loads(Path(storage_state).read_text()) if storage_state else {'cookies': []}
    check_storage_state(cfg, origin, state)
    cookies = cookie_header(state, urlsplit(origin).hostname or '')
    passed = []
    for check in cfg.preflight:
        reason = judge(check, get_json(f'{origin}{check.path}', cookies))
        if reason:
            raise PreflightError(f'Preflight refused the run: {reason}')
        passed.append(check.path)
    return passed
