"""Builds evidence.json shapes the way run.py writes them, for judge and replay tests."""


def check(target, journey='write', device='desktop', status=200, **kw):
    base = {'target': target, 'journey': journey, 'device': device, 'path': f'/{journey}', 'status': status,
            'error': None, 'console_errors': [], 'page_errors': [], 'failed_requests': [], 'server_errors': [],
            'selectors': [], 'screenshot': None}
    return {**base, **kw}


def session(target, pid, journey='write', device='desktop', achieved=True, **kw):
    base = {'id': pid, 'name': f'P{pid}', 'who': 'a tester', 'device': device, 'patience': 'medium',
            'journey': journey, 'goal': 'g', 'success_criteria': '', 'start_path': f'/{journey}',
            'target': target, 'verdict': 'ACHIEVED: fine' if achieved else 'GAVE UP: stuck',
            'achieved': achieved, 'crashed': False, 'steps': 5, 'tokens': 100, 'seconds': 10,
            'guard_blocked': [], 'server_errors': []}
    return {**base, **kw}


def issue(target, pid, journey='write', severity='major', category='ux', url='https://app.example.com/write', **kw):
    base = {'target': target, 'persona': f'P{pid}', 'persona_id': pid, 'journey': journey, 'device': 'desktop',
            'severity': severity, 'category': category, 'title': 'Button does nothing', 'detail': 'Clicked it.',
            'url': url, 'screenshot': f'shots/{target}-{pid}-1.png'}
    return {**base, **kw}


def evidence(checks=(), sessions=(), raw=(), merge=None, baseline_ran=True):
    return {'meta': {'title': 'Persona swarm', 'pr': '7', 'url': 'https://preview.example.com',
                     'baseline_url': 'https://app.example.com', 'model': 'bedrock:test', 'version': '0.1.0',
                     'fail_on': ['blocker'], 'notes': []},
            'baseline_ran': baseline_ran, 'checks': list(checks), 'sessions': list(sessions),
            'raw_issues': list(raw), 'merge': merge}
