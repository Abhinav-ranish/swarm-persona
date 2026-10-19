"""The checks behind `brain doctor` and tests/test_brain.py: the brain stays useful only if what lands
or goes stale leaves it. Standard library only, and no git: CodeBuild's source zip has no .git.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from brain_people import HANDLE
from brain_recall import FACT_FILES, glob_re

REQUIRED = {'id': str, 'kind': str, 'text': str, 'paths': list, 'created': str, 'source': str}
OPTIONAL = {'tags': list}
FACT_ID = re.compile(r'[a-z0-9][a-z0-9-]{2,63}')
# Work-in-flight and decision docs: each must be listed in index.md or it is a leftover.
LISTED_DIRS = ['plans', 'adr']
EM_DASH = chr(0x2014)


def fact_problems(brain_dir: Path) -> list[str]:
    """Schema problems in facts/*.json: shape, types, kind matching its file, dates, unique ids."""
    problems, seen = [], {}
    facts_dir = brain_dir / 'facts'
    known = {f'{base}.json' for base in FACT_FILES.values()}
    for extra in sorted(p.name for p in facts_dir.glob('*.json') if p.name not in known) if facts_dir.is_dir() else []:
        problems.append(f'facts/{extra}: not a fact file (one of {", ".join(sorted(known))})')
    for kind, base in FACT_FILES.items():
        rel = f'facts/{base}.json'
        path = brain_dir / rel
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except ValueError as error:
            problems.append(f'{rel}: not JSON ({error})')
            continue
        if not isinstance(data, list):
            problems.append(f'{rel}: must be a JSON list')
            continue
        for n, fact in enumerate(data):
            where = f"{rel}[{n}]" + (f" {fact.get('id')}" if isinstance(fact, dict) and fact.get('id') else '')
            if not isinstance(fact, dict):
                problems.append(f'{where}: not an object')
                continue
            for key, typ in REQUIRED.items():
                if not isinstance(fact.get(key), typ):
                    problems.append(f'{where}: {key} must be a {typ.__name__}')
            for key in set(fact) - set(REQUIRED) - set(OPTIONAL):
                problems.append(f'{where}: unknown key {key!r}')
            for key, typ in OPTIONAL.items():
                if key in fact and not isinstance(fact[key], typ):
                    problems.append(f'{where}: {key} must be a {typ.__name__}')
            if fact.get('kind') != kind:
                problems.append(f'{where}: kind {fact.get("kind")!r} does not belong in {rel}')
            if isinstance(fact.get('id'), str):
                if not FACT_ID.fullmatch(fact['id']):
                    problems.append(f'{where}: id must be lower-case kebab-case')
                if fact['id'] in seen:
                    problems.append(f"{where}: id also used in {seen[fact['id']]}")
                seen.setdefault(fact['id'], rel)
            if isinstance(fact.get('text'), str) and not fact['text'].strip():
                problems.append(f'{where}: empty text')
            if isinstance(fact.get('source'), str) and not fact['source'].strip():
                problems.append(f'{where}: empty source (a commit, a PR, or who said so)')
            try:
                datetime.date.fromisoformat(str(fact.get('created')))
            except ValueError:
                problems.append(f'{where}: created must be YYYY-MM-DD')
            if isinstance(fact.get('paths'), list) and not all(isinstance(p, str) and p.strip() for p in fact['paths']):
                problems.append(f'{where}: paths must be non-empty strings')
    return problems


def anchor_exists(root: Path, anchor: str) -> bool:
    anchor = anchor.strip().removeprefix('./')
    if any(c in anchor for c in '*?['):
        pattern = re.compile(glob_re(anchor))
        return any(pattern.fullmatch(str(p.relative_to(root))) for p in root.rglob('*') if '.git' not in p.parts)
    return (root / anchor).exists()


def missing_anchors(brain_dir: Path) -> list[str]:
    """`<fact id>: <path>` for every anchored path that is gone from the tree."""
    from brain_recall import fact_paths, load_facts

    root = brain_dir.parent
    return [f"{f.get('id')}: {p}" for f in load_facts(brain_dir) for p in fact_paths(f)
            if p.strip() and not anchor_exists(root, p)]


def index_refs(brain_dir: Path) -> list[str]:
    index = brain_dir / 'index.md'
    text = index.read_text() if index.exists() else ''
    return [m.group(1).removeprefix('.brain/') for m in re.finditer(r'`([^`\s]+)`', text)]


def unlisted_docs(brain_dir: Path) -> list[str]:
    """Files under plans/ and adr/ (relative to .brain/) that index.md never names in backticks.
    Exact paths only: a directory or a glob would list a doc nobody has looked at."""
    refs = set(index_refs(brain_dir))
    docs = [str(p.relative_to(brain_dir)) for d in LISTED_DIRS for p in sorted((brain_dir / d).rglob('*'))
            if p.is_file()]
    return [f for f in docs if f not in refs]


def stale_index_refs(brain_dir: Path) -> list[str]:
    """Paths under plans/ or adr/ that index.md names but that no longer exist."""
    return [r for r in dict.fromkeys(index_refs(brain_dir))
            if r.split('/')[0] in LISTED_DIRS and not (brain_dir / r).exists()]


def people_problems(brain_dir: Path) -> list[str]:
    problems = []
    folder = brain_dir / 'people'
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
        rel = f'people/{path.name}'
        try:
            person = json.loads(path.read_text())
        except ValueError:
            problems.append(f'{rel}: not JSON')
            continue
        if not isinstance(person, dict) or not HANDLE.fullmatch(str(person.get('handle'))):
            problems.append(f'{rel}: needs a handle')
            continue
        if f"{person['handle']}.json" != path.name:
            problems.append(f"{rel}: handle {person['handle']!r} does not match the file name")
        if not isinstance(person.get('identities'), list):
            problems.append(f'{rel}: identities must be a list')
        prefs = person.get('preferences')
        if not isinstance(prefs, list) or not all(isinstance(p, dict) and p.get('id') and p.get('text') for p in prefs):
            problems.append(f'{rel}: preferences must be a list of {{id, text, added}}')
    return problems


def em_dashes(brain_dir: Path) -> list[str]:
    """Files of the brain and the agent guides that contain an em dash (the owner's rule)."""
    root = brain_dir.parent
    files = [p for p in sorted(brain_dir.rglob('*')) if p.is_file() and '__pycache__' not in p.parts]
    files += [root / name for name in ('AGENTS.md', 'CLAUDE.md') if (root / name).exists()]
    return [str(p.relative_to(root)) for p in files if EM_DASH in p.read_text(errors='ignore')]


def doctor(brain_dir: Path) -> list[str]:
    """Every problem as one line; [] when the brain is healthy."""
    issues = []
    for name in ('index.md', 'release.md'):
        if not (brain_dir / name).exists():
            issues.append(f'.brain/{name} is missing')
    issues += fact_problems(brain_dir)
    issues += [f'anchored path is gone (update or drop the fact): {m}' for m in missing_anchors(brain_dir)]
    issues += [f'not listed in .brain/index.md (list it or delete it): {d}' for d in unlisted_docs(brain_dir)]
    issues += [f'.brain/index.md lists a doc that does not exist: {r}' for r in stale_index_refs(brain_dir)]
    issues += people_problems(brain_dir)
    issues += [f'em dash in {f}' for f in em_dashes(brain_dir)]
    return issues
