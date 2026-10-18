"""Deterministic evidence: each journey's entry page, per device, loaded by plain Playwright through
the same guard proxy. These signals are what can confirm a finding; an agent's opinion cannot.

Recorded per page: the HTTP status, uncaught page errors, console errors, failed requests, 5xx
answers from the app's own hosts, and for each critical selector whether it exists, is visible,
is enabled and would receive a click (click(trial=True) runs the actionability checks without
clicking, which is how an overlay covering a button shows up).
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config

VIEWPORTS = {'desktop': {'width': 1440, 'height': 900}, 'mobile': {'width': 390, 'height': 844}}
MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 '
             '(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1')


def normalize(text: str) -> str:
    """Stable across runs: ids, hashes, numbers and query strings removed."""
    text = re.sub(r'\?[^\s"\']*', '', str(text).lower())
    text = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '#', text)
    text = re.sub(r'\b[0-9a-f]{12,}\b|\d+', '#', text)
    return ' '.join(text.split())[:200]


def signals(check: dict) -> list[dict]:
    """The check's hard signals, each with a fingerprint that matches across preview and baseline."""
    out = []

    def add(kind: str, detail: str) -> None:
        out.append({'kind': kind, 'detail': detail, 'journey': check['journey'], 'device': check['device'],
                    'path': check['path'],
                    'fingerprint': '|'.join([check['journey'], check['device'], kind, normalize(detail)])})

    status = check.get('status')
    if status is None:
        add('load-failed', check.get('error') or 'the page did not load')
    elif status >= 400:
        add('http-status', f'entry page answered HTTP {status}')
    for sel in check.get('selectors', []):
        if sel['state'] != 'ok':
            add('selector', f"{sel['selector']} is {sel['state']}")
    for e in check.get('page_errors', []):
        add('page-error', e)
    for e in check.get('server_errors', []):
        add('server-error', e)
    for e in check.get('console_errors', []):
        add('console-error', e)
    for e in check.get('failed_requests', []):
        add('failed-request', e)
    return out


async def check_page(browser, cfg: Config, origin: str, journey: str, path: str, device: str,
                     selectors: list[str], storage_state: dict | None, shot: Path) -> dict:
    hosts = cfg.app_hosts(origin)
    context = await browser.new_context(
        viewport=VIEWPORTS[device], is_mobile=device == 'mobile', has_touch=device == 'mobile',
        user_agent=MOBILE_UA if device == 'mobile' else None, ignore_https_errors=True,
        storage_state=storage_state, service_workers='block',
    )
    page = await context.new_page()
    result = {'journey': journey, 'device': device, 'path': path, 'status': None, 'error': None,
              'console_errors': [], 'page_errors': [], 'failed_requests': [], 'server_errors': [],
              'selectors': [], 'screenshot': None}

    def same_app(url: str) -> bool:
        return (urlsplit(url).hostname or '').lower() in hosts

    page.on('console', lambda m: m.type == 'error' and result['console_errors'].append(m.text[:300]))
    page.on('pageerror', lambda e: result['page_errors'].append(str(e)[:300]))
    page.on('requestfailed', lambda r: result['failed_requests'].append(
        f"{r.method} {r.url.split('?')[0]} ({r.failure or 'failed'})"[:300]))
    page.on('response', lambda r: r.status >= 500 and same_app(r.url) and result['server_errors'].append(
        f"{r.request.method} {urlsplit(r.url).path} answered {r.status}"))
    try:
        response = await page.goto(origin + path, wait_until='domcontentloaded', timeout=45_000)
        result['status'] = response.status if response else None
        try:
            await page.wait_for_load_state('networkidle', timeout=10_000)
        except Exception:
            pass  # pages that poll never go idle; what loaded is what is judged
        for selector in selectors:
            result['selectors'].append({'selector': selector, 'state': await selector_state(page, selector)})
        await page.screenshot(path=str(shot), full_page=True)
        result['screenshot'] = str(shot)
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {str(error)[:200]}'
    finally:
        await context.close()
    for key in ('console_errors', 'failed_requests', 'server_errors', 'page_errors'):
        result[key] = sorted(set(result[key]))
    return result


async def selector_state(page, selector: str) -> str:
    loc = page.locator(selector).first
    try:
        if await loc.count() == 0:
            return 'missing'
        if not await loc.is_visible():
            return 'hidden'
        if not await loc.is_enabled():
            return 'disabled'
        await loc.click(trial=True, timeout=3_000)
        return 'ok'
    except Exception:
        return 'not actionable (covered or detached)'


async def run_checks(cfg: Config, origin: str, label: str, targets: list[dict], proxy: str,
                     storage_state: dict | None, shots: Path, sandbox: bool) -> list[dict]:
    """targets: [{journey, path, device, selectors}]. One browser, one context per page."""
    from playwright.async_api import async_playwright

    out = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=sandbox, proxy={'server': proxy, 'bypass': '<-loopback>'},
                                           args=['--disable-quic', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
        try:
            for t in targets:
                slug = re.sub(r'[^a-z0-9]+', '-', f"{label}-{t['journey']}-{t['device']}".lower()).strip('-')
                out.append({'target': label, **await check_page(
                    browser, cfg, origin, t['journey'], t['path'], t['device'], t['selectors'],
                    storage_state, shots / f'check-{slug}.png')})
        finally:
            await browser.close()
    return out
