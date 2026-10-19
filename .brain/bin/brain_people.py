"""Who is working, and what they prefer. One committed file per person, so two people never edit the
same file:

    .brain/people/<handle>.json
    {"handle": ..., "name": ..., "identities": [git email, ...],
     "preferences": [{"id": ..., "text": ..., "added": "YYYY-MM-DD"}]}

The current person is BRAIN_USER (a handle or an identity) when set, else the git author's email
matched against every file's identities.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

HANDLE = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')


def norm(s) -> str:
    return str(s or '').strip().lower()


def people_dir(brain_dir: Path) -> Path:
    return brain_dir / 'people'


def load_people(brain_dir: Path) -> list[dict]:
    out = []
    for path in sorted(people_dir(brain_dir).glob('*.json')) if people_dir(brain_dir).is_dir() else []:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def save_json(path: Path, data) -> None:
    """Write, flush to disk, then rename: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with open(tmp, 'w') as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_person(brain_dir: Path, person: dict) -> None:
    # The handle becomes a file name: refuse anything that could leave .brain/people.
    if not HANDLE.fullmatch(str(person.get('handle'))):
        raise ValueError(f"invalid handle {person.get('handle')!r}")
    save_json(people_dir(brain_dir) / f"{person['handle']}.json", person)


def git_identity(root: Path) -> dict | None:
    """The git author of this checkout, read with `git var` (no config writes)."""
    try:
        ident = subprocess.run(['git', '-C', str(root), 'var', 'GIT_AUTHOR_IDENT'], capture_output=True,
                               text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.match(r'(.*?)\s*<([^>]*)>', ident)
    return {'name': match.group(1), 'email': match.group(2)} if match else None


def slug_handle(s: str) -> str:
    """A file-safe handle. An email keeps its local part plus a digest of the whole address, so two
    addresses with the same local part never share a file; a plain handle is only slugged."""
    n = norm(s)

    def slug(x: str) -> str:
        return re.sub(r'[^a-z0-9]+', '-', x).strip('-')[:40]

    if '@' not in n:
        return slug(n) or 'unknown'
    return f"{slug(n.split('@')[0]) or 'user'}-{hashlib.sha256(n.encode()).hexdigest()[:8]}"


def current_user(brain_dir: Path) -> dict:
    """{handle, person (or None), via}."""
    people = load_people(brain_dir)

    def find(ident: str):
        return next((p for p in people if norm(p.get('handle')) == norm(ident)
                     or any(norm(i) == norm(ident) for i in p.get('identities') or [])), None)

    if os.environ.get('BRAIN_USER'):
        person = find(os.environ['BRAIN_USER'])
        return {'handle': person['handle'] if person else slug_handle(os.environ['BRAIN_USER']),
                'person': person, 'via': 'BRAIN_USER'}
    ident = git_identity(brain_dir.parent)
    # Email only: a shared display name must never resolve to someone else's file. The name is a
    # fallback only for an author with no email at all.
    person = (find(ident['email']) if ident['email'] else find(ident['name'])) if ident else None
    if person:
        return {'handle': person['handle'], 'person': person, 'via': 'git'}
    return {'handle': slug_handle(ident['email'] or ident['name']) if ident else 'unknown', 'person': None,
            'via': 'git (no people file)' if ident else 'none'}


def ensure_person(brain_dir: Path, as_handle: str | None) -> dict:
    """The current person's record, or a new one. `as_handle` only names a new file: each person
    edits only their own, so a handle someone else holds is refused."""
    me = current_user(brain_dir)
    if me['person']:
        if as_handle and norm(as_handle) != norm(me['person']['handle']):
            raise ValueError(f"you are {me['person']['handle']}; edit only your own .brain/people file")
        return me['person']
    handle = slug_handle(as_handle) if as_handle else me['handle']
    if any(norm(p.get('handle')) == handle for p in load_people(brain_dir)):
        raise ValueError(f'.brain/people/{handle}.json belongs to someone else; add your git email to your own '
                         'file or pick another --as handle')
    ident = git_identity(brain_dir.parent) or {}
    # A BRAIN_USER email is an identity too, or the next call would not find this file again.
    chosen = os.environ.get('BRAIN_USER', '')
    identities = [x for x in (ident.get('email'), chosen if '@' in chosen else None) if x]
    return {'handle': handle, 'name': ident.get('name') or handle,
            'identities': list(dict.fromkeys(identities)), 'preferences': []}


def add_preference(brain_dir: Path, text: str, as_handle: str | None = None) -> tuple[str, dict]:
    text = ' '.join(str(text).split())
    if not text:
        raise ValueError('a preference needs text')
    person = ensure_person(brain_dir, as_handle)
    pref = {'id': secrets.token_hex(4), 'text': text, 'added': datetime.date.today().isoformat()}
    person['preferences'] = [*(person.get('preferences') or []), pref]
    save_person(brain_dir, person)
    return person['handle'], pref


def remove_preference(brain_dir: Path, pref_id: str) -> str | None:
    """Removes one of the current person's preferences; nobody else's file is touched."""
    person = current_user(brain_dir)['person']
    if not person:
        return None
    before = person.get('preferences') or []
    person['preferences'] = [p for p in before if p.get('id') != pref_id]
    if len(person['preferences']) == len(before):
        return None
    save_person(brain_dir, person)
    return person['handle']
