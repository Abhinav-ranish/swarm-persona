"""The write guard's policy, as a pure function. guard/addon.py applies it inside mitmdump.

Decided per request and failing closed:
- Only https, and never loopback, private, link-local or otherwise internal addresses (Chromium's
  debugging port, the other personas' proxies, a cloud metadata endpoint).
- Another site: reads only, and never as a page or frame (a booking widget, an OAuth screen). A
  request without Sec-Fetch-Dest counts as a page.
- Any host of the app: denied_paths are refused for every method (routes that act when opened);
  any other write must match allowed_writes, and its body must not set a forbidden key or value.
- WebSocket and other protocol upgrades are refused: messages after an upgrade are never seen.
"""

from __future__ import annotations

import ipaddress
import json
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit

SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
# Sec-Fetch-Dest values for something that renders as a page: a top-level navigation or a frame.
PAGE_DESTS = {'document', 'iframe', 'frame', 'embed', 'object', 'fencedframe'}


WRITE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
OVERRIDE_HEADERS = ('x-http-method-override', 'x-http-method', 'x-method-override')


def parse_write(entry: str) -> tuple[frozenset[str] | None, re.Pattern]:
    """'^/api/notes$' allows any write method; 'POST|PATCH ^/api/notes$' only those."""
    match = re.fullmatch(r'([A-Z]+(?:\|[A-Z]+)*)\s+(\S.*)', entry.strip())
    if match:
        return frozenset(match.group(1).split('|')), re.compile(match.group(2))
    return None, re.compile(entry.strip())


def norm_key(key: str) -> str:
    """scheduled_publish_at, scheduledPublishAt and Scheduled-Publish-At are one key."""
    return re.sub(r'[^a-z0-9]', '', str(key).lower())


@dataclass
class Policy:
    origin: str
    app_hosts: set[str]
    denied_paths: list[re.Pattern] = field(default_factory=list)
    allowed_writes: list[tuple[frozenset[str] | None, re.Pattern]] = field(default_factory=list)
    forbidden_body_keys: set[str] = field(default_factory=set)
    forbidden_body_values: dict[str, set[str]] = field(default_factory=dict)
    json_only_paths: list[re.Pattern] = field(default_factory=list)
    allow_delete: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> 'Policy':
        origin = data['origin'].rstrip('/')
        return cls(
            origin=origin,
            app_hosts={h.lower() for h in data.get('app_hosts', [])} | {urlsplit(origin).hostname or ''},
            denied_paths=[re.compile(p) for p in data.get('denied_paths', [])],
            allowed_writes=[parse_write(p) for p in data.get('allowed_writes', [])],
            forbidden_body_keys={norm_key(k) for k in data.get('forbidden_body_keys', [])},
            forbidden_body_values={norm_key(k): {str(v).lower() for v in vs}
                                   for k, vs in data.get('forbidden_body_values', {}).items()},
            json_only_paths=[re.compile(p) for p in data.get('json_only_paths', [])],
            allow_delete=bool(data.get('allow_delete')),
        )


def internal_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved
            or ip.is_multicast or not ip.is_global)


def internal_host(host: str) -> bool:
    host = host.strip('[]').rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.internal', '.local')):
        return True
    try:
        return internal_ip(ipaddress.ip_address(host))
    except ValueError:
        pass
    # 2130706433, 0x7f.1, 0177.0.0.1: forms a resolver may read as an address.
    return all(re.fullmatch(r'0x[0-9a-f]*|[0-9]+', label) for label in host.split('.'))


def normal_path(path: str) -> str:
    """The path as the app's router will see it: percent-decoding undone (a double encoding too),
    repeated slashes collapsed, dot segments resolved, lower-cased for matching."""
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    path = re.sub(r';[^/]*', '', path or '/')  # matrix parameters: /invite;x is /invite
    path = posixpath.normpath(re.sub(r'/{2,}', '/', path or '/'))
    return (path if path.startswith('/') else '/' + path).lower()


def verdict(policy: Policy, method: str, url: str, dest: str, body: bytes | None,
            upgrade: bool = False, headers: dict[str, str] | None = None) -> str | None:
    """Why this request is refused, or None to let it through. headers: lower-cased names."""
    method = method.upper()
    headers = headers or {}
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if parts.scheme != 'https':
        return 'not https'
    if not host or internal_host(host):
        return 'internal address'
    if upgrade:
        return 'WebSocket or protocol upgrade'
    if host not in policy.app_hosts:
        if method not in SAFE_METHODS:
            return 'cross-origin write'
        if not dest or dest in PAGE_DESTS:
            return 'another site as a page or frame'
        return None
    path = normal_path(parts.path)
    if any(p.search(path) for p in policy.denied_paths):
        return 'route that acts when opened'
    if f'{parts.scheme}://{parts.netloc}'.lower() != policy.origin.lower():
        return None if method in SAFE_METHODS else 'write to another app host'
    # Frameworks honour these on any method (Rails, Laravel, Express, Symfony): a GET or POST that
    # claims to be a DELETE is judged as nothing at all, and refused.
    if any(headers.get(h) for h in OVERRIDE_HEADERS):
        return 'method override header'
    if method in SAFE_METHODS:
        return None
    if method not in WRITE_METHODS:
        return f'method {method}'
    if method == 'DELETE' and not policy.allow_delete:
        return 'DELETE'
    # A path that only matches after normalisation (/api/%70osts, /api/posts;x) is refused: the
    # allowlist is for the paths the app itself sends.
    if path != re.sub(r'/{2,}', '/', parts.path).lower() or not any(
            p.search(path) and (methods is None or method in methods) for methods, p in policy.allowed_writes):
        return 'write outside the allowlist'
    return body_verdict(policy, path, body, headers.get('content-type', ''))


class Pairs(list):
    """A JSON object as its (key, value) pairs, so a duplicated key is judged every time it appears."""


def body_verdict(policy: Policy, path: str, body: bytes | None, content_type: str = '') -> str | None:
    """Judge the body as the app will read it, or refuse it when it cannot be judged.

    JSON is parsed (escapes such as scheduled\\u005fpublish_at decode to the real key, a duplicated
    key is checked every time) and so are urlencoded forms (bracket and dot keys such as
    note[publish_at] are split and every segment checked, every value of a repeated key too). Keys
    match whatever their case or separators. Anything else, a multipart body, another charset, a
    byte-order mark in UTF-16, is refused: the guard cannot see what the app would read in it."""
    if not body:
        return None
    ctype = content_type.split(';')[0].strip().lower()
    charset = re.search(r'charset\s*=\s*"?([\w-]+)', content_type, re.I)
    if charset and charset.group(1).lower() not in ('utf-8', 'utf8', 'us-ascii', 'ascii'):
        return f'body charset {charset.group(1)}'
    try:
        text = body.decode('utf-8')
    except UnicodeDecodeError:
        return 'body is not UTF-8'
    text = text.lstrip('\ufeff')
    if '\x00' in text:
        return 'body is not UTF-8'
    looks_json = text.lstrip()[:1] in ('{', '[', '"')
    pairs: list[tuple[str, object]] = []
    if 'json' in ctype or looks_json:
        try:
            value = json.loads(text, object_pairs_hook=Pairs)
        except (ValueError, RecursionError):
            return 'body is not JSON'
        try:
            pairs += json_pairs(value)
        except RecursionError:
            return 'body nested too deep to judge'
    elif ctype in ('', 'application/x-www-form-urlencoded'):
        if any(p.search(path) for p in policy.json_only_paths):
            return 'body is not JSON'
        pairs += form_pairs(text)
    else:
        return f'body type {ctype} cannot be judged'
    hit = forbidden(policy, pairs)
    return hit


def json_pairs(value, depth: int = 0) -> list[tuple[str, object]]:
    if depth > 40:
        raise RecursionError
    out: list[tuple[str, object]] = []
    if isinstance(value, Pairs):
        for k, v in value:
            out.append((k, v))
            out += json_pairs(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            out += json_pairs(v, depth + 1)
    return out


def form_pairs(text: str) -> list[tuple[str, object]]:
    """Every key, and every bracket or dot segment of it, with each of its values."""
    out = []
    for key, value in parse_qsl(text, keep_blank_values=True):
        out.append((key, value))
        out += [(seg, value) for seg in re.split(r'[\[\].]+', key) if seg and seg != key]
    return out


def forbidden(policy: Policy, pairs: list[tuple[str, object]]) -> str | None:
    for key, v in pairs:
        if str(key).strip().lower() == '_method':
            return 'method override field'
        k = norm_key(key)
        if k in policy.forbidden_body_keys and v is not None and v != '':
            return f'body sets {key}'
        if k in policy.forbidden_body_values and str(v).lower() in policy.forbidden_body_values[k]:
            return f'body sets {key}={v}'
    return None
