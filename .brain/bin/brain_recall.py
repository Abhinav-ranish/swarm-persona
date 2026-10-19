"""Ranking for `brain recall` and `brain context`. Standard library only.

recall: BM25F over every fact (id, tags, paths, text; id weighted highest) and the heading sections
of the repo's own docs (AGENTS.md, .brain/index.md, release.md, adr/, plans/), so a rule that lives
in a doc is found next to the facts. Tokens are whole words, so "em" never matches "item".

context: the facts anchored (by `paths`) to the files being touched, the doc sections that name
those files, and the tests that import them.
"""

from __future__ import annotations

import ast
import json
import math
import re
import subprocess
from pathlib import Path

FACT_FILES = {'gotcha': 'gotchas', 'decision': 'decisions', 'convention': 'conventions', 'note': 'notes'}
DOC_FILES = ['AGENTS.md', '.brain/index.md', '.brain/release.md']
DOC_GLOBS = ['.brain/adr/*.md', '.brain/plans/*.md']

STOP = set(('a an and are as at be but by can do does for from has have how if in into is it its not of on or so '
            'than that the then there this to was we what when where which who why will with you').split())
FIELD_WEIGHT = {'id': 3.0, 'tags': 2.0, 'paths': 1.5, 'text': 1.0}
K1, B = 1.2, 0.75


def stem(t: str) -> str:
    if len(t) <= 3 or re.search(r'\d', t):
        return t
    if t.endswith('ies'):
        return t[:-3] + 'y'
    if re.search(r'(sh|ch|x|ss)es$', t):
        return t[:-2]
    if t.endswith('s') and not t.endswith('ss'):
        return t[:-1]
    return t


def tokenize(text) -> list[str]:
    """Whole words: camelCase and snake/kebab/path parts split, lower-cased, stemmed."""
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', str(text or '')).lower()
    return [stem(t) for t in re.split(r'[^a-z0-9]+', text) if t and t not in STOP and (len(t) > 1 or t.isdigit())]


def repo_root(brain_dir: Path) -> Path:
    return brain_dir.parent


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback


def fact_paths(fact: dict) -> list[str]:
    """The fact's anchors, tolerating a malformed `paths` (doctor reports it; nothing may crash on it)."""
    paths = fact.get('paths')
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


def load_facts(brain_dir: Path) -> list[dict]:
    out = []
    for base in FACT_FILES.values():
        data = load_json(brain_dir / 'facts' / f'{base}.json', [])
        out += [{**f, 'source_file': base} for f in data if isinstance(f, dict)] if isinstance(data, list) else []
    return out


def doc_files(brain_dir: Path) -> list[str]:
    root = repo_root(brain_dir)
    found = [rel for rel in DOC_FILES if (root / rel).is_file()]
    for pattern in DOC_GLOBS:
        found += sorted(str(p.relative_to(root)) for p in root.glob(pattern) if p.is_file())
    return found


def doc_sections(brain_dir: Path) -> list[dict]:
    """Each markdown file split at its headings; long sections cut into chunks of about 1200 chars."""
    root, out = repo_root(brain_dir), []
    for rel in doc_files(brain_dir):
        heading, body, n = rel, [], 0

        def flush():
            nonlocal body, n
            text = '\n'.join(body).strip()
            body = []
            if not text:
                return
            chunk = ''
            for para in re.split(r'\n\s*\n', text):
                if chunk and len(chunk) + len(para) > 1200:
                    n += 1
                    out.append(section(rel, heading, n, chunk))
                    chunk = ''
                chunk += para + '\n\n'
            if chunk.strip():
                n += 1
                out.append(section(rel, heading, n, chunk))

        for line in (root / rel).read_text(errors='replace').splitlines():
            match = re.match(r'#{1,4}\s+(.*)', line)
            if match:
                flush()
                heading = match.group(1).strip()
            else:
                body.append(line)
        flush()
    return out


def section(rel: str, heading: str, n: int, text: str) -> dict:
    return {'id': f'{rel}#{n}', 'kind': 'doc', 'title': f'{rel} > {heading}', 'text': text.strip(),
            'paths': [rel], 'tags': [], 'source_file': 'doc'}


def fields(d: dict) -> dict[str, list[str]]:
    return {'id': tokenize(d.get('title') or d.get('id')), 'tags': tokenize(' '.join(d.get('tags') or [])),
            'paths': tokenize(' '.join(fact_paths(d))), 'text': tokenize(d.get('text'))}


def bm25(docs: list[dict], query: str) -> list[tuple[float, dict]]:
    """BM25F scores, highest first, zero scores dropped. Documents that cover every query term and
    that contain the query as a phrase rank higher."""
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms:
        return []
    prepared = []
    for d in docs:
        tf, length = {}, 0.0
        for name, toks in fields(d).items():
            length += len(toks) * FIELD_WEIGHT[name]
            for t in toks:
                tf[t] = tf.get(t, 0.0) + FIELD_WEIGHT[name]
        prepared.append((d, tf, length))
    avg = (sum(p[2] for p in prepared) / max(len(prepared), 1)) or 1.0
    df = {t: sum(1 for p in prepared if t in p[1]) for t in terms}
    total = len(prepared)
    words = [re.escape(w) for w in re.split(r'[\s-]+', query.strip().lower()) if w]
    phrase = re.compile(r'\b' + r'[\s-]+'.join(words), re.I) if len(words) > 1 else None
    scored = []
    for d, tf, length in prepared:
        score, matched = 0.0, 0
        for t in terms:
            f = tf.get(t)
            if not f:
                continue
            matched += 1
            idf = math.log(1 + (total - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * length / avg))
        if not score:
            continue
        score *= 0.5 + 0.5 * matched / len(terms)
        if phrase and phrase.search(f"{d.get('title') or d.get('id')}\n{d.get('text')}"):
            score *= 1.5
        scored.append((score, d))
    return sorted(scored, key=lambda s: -s[0])


def recall(brain_dir: Path, query: str, limit: int = 8) -> list[tuple[float, dict]]:
    return bm25(load_facts(brain_dir) + doc_sections(brain_dir), query)[:limit]


def normal_file(file: str) -> str:
    return file.strip().removeprefix('./')


def path_affinity(anchor: str, file: str) -> float:
    """How strongly a fact anchored at `anchor` (a file, a dir/ or a glob) relates to `file`."""
    a, file = anchor.strip().removeprefix('./'), file.strip().removeprefix('./')
    if a == file:
        return 3.0
    if any(c in a for c in '*?['):
        return 2.5 if re.fullmatch(glob_re(a), file) else 0.0
    if file.startswith(a.rstrip('/') + '/'):
        return 2.0
    if '/' in a and '/' in file and a.rsplit('/', 1)[0] == file.rsplit('/', 1)[0]:
        return 1.0
    return 0.0


def glob_re(pattern: str) -> str:
    out, i = '', 0
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out, i = out + '(?:.*/)?', i + 3
        elif pattern.startswith('**', i):
            out, i = out + '.*', i + 2
        elif pattern[i] == '*':
            out, i = out + '[^/]*', i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return out


def context_for(brain_dir: Path, files: list[str], limit: int = 12) -> list[tuple[float, dict]]:
    """Facts and doc sections relevant to the files being edited, strongest first."""
    files = [normal_file(f) for f in files]
    hits = []
    for fact in load_facts(brain_dir):
        best = max([path_affinity(a, f) for a in fact_paths(fact) for f in files] or [0.0])
        if best:
            hits.append((best, fact))
    for d in doc_sections(brain_dir):
        named = [f for f in files if f in d['text']]
        if named and d['paths'][0] not in files:
            hits.append((2.0 * len(named), d))
    return sorted(hits, key=lambda h: -h[0])[:limit]


def git(root: Path, *args: str) -> list[str]:
    try:
        done = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=False)
    except OSError:
        return []
    return [line for line in done.stdout.splitlines() if line] if done.returncode == 0 else []


def changed_files(brain_dir: Path) -> list[str]:
    """What this branch touches: commits since the merge base with origin/main (or main), plus
    uncommitted and untracked changes."""
    root = repo_root(brain_dir)
    base = next((b[0] for b in (git(root, 'merge-base', 'HEAD', ref) for ref in ('origin/main', 'main')) if b), None)
    files = (git(root, 'diff', '--name-only', base) if base else []) + git(root, 'diff', '--name-only', 'HEAD')
    files += git(root, 'ls-files', '--others', '--exclude-standard')
    return list(dict.fromkeys(files))


def module_name(file: str) -> str | None:
    """src/swarm_persona/guard/policy.py -> swarm_persona.guard.policy; tests/x.py -> x."""
    if not file.endswith('.py'):
        return None
    parts = file[:-3].split('/')
    if parts[0] in ('src', 'tests'):
        parts = parts[1:]
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts) or None


def imports_of(path: Path) -> set[str]:
    """Every module a file imports, `from a import b` counted as both a and a.b."""
    try:
        tree = ast.parse(path.read_text(errors='replace'))
    except (OSError, SyntaxError, ValueError):
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found |= {f'{node.module}.{alias.name}' for alias in node.names}
    return found


def tests_for(brain_dir: Path, file: str) -> list[str]:
    """The test files that import `file`'s module."""
    module = module_name(normal_file(file))
    tests = repo_root(brain_dir) / 'tests'
    if not module or not tests.is_dir():
        return []
    return [f'tests/{p.name}' for p in sorted(tests.glob('test_*.py')) if module in imports_of(p)]
