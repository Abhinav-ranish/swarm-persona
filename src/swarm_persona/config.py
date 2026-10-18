"""swarm-persona.yml: everything that is specific to the app under test.

The tool itself knows nothing about any one product. Hosts, the write guard's policy, the
workspace preflight, sign-in, the journeys and the product description all come from here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal['blocker', 'major', 'minor', 'polish']
SEVERITIES: list[str] = ['blocker', 'major', 'minor', 'polish']
Device = Literal['desktop', 'mobile']


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


def https_origin(value: str) -> str:
    value = value.rstrip('/')
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.path or parts.query:
        raise ValueError(f'must be https://host[:port] with no path, got {value!r}')
    return value


def path_patterns(patterns: list[str], methods: bool = False) -> list[str]:
    """Each pattern is matched against the lower-cased, normalised path, so it must be anchored
    at the start and written in lower case; an allowed write may lead with methods (POST|PATCH)."""
    for entry in patterns:
        pattern = entry.strip()
        if methods:
            match = re.fullmatch(r'([A-Z]+(?:\|[A-Z]+)*)\s+(\S.*)', pattern)
            if match:
                bad = set(match.group(1).split('|')) - {'POST', 'PUT', 'PATCH', 'DELETE'}
                if bad:
                    raise ValueError(f'{entry!r}: not write methods: {sorted(bad)}')
                pattern = match.group(2)
        if not pattern.startswith('^'):
            raise ValueError(f'{entry!r} must be anchored with ^')
        if re.search(r'[A-Z]', re.sub(r'\\.', '', pattern)):
            raise ValueError(f'{entry!r} must be lower case: paths are lower-cased before matching')
        re.compile(pattern)
    return patterns


class App(Strict):
    name: str
    description: str = Field(description='One paragraph the planner reads to understand the product.')
    url: str = Field(description='The build under test (a preview). --url overrides it.')
    baseline_url: str | None = Field(None, description='Production, run with the same journeys as the oracle.')
    hosts: list[str] = Field(default_factory=list, description='Every other host that serves this app.')
    routes: list[str] = Field(default_factory=lambda: ['/'], description='Start paths when no journey applies.')

    @field_validator('url', 'baseline_url')
    @classmethod
    def _origin(cls, v: str | None) -> str | None:
        return https_origin(v) if v else v

    @field_validator('routes')
    @classmethod
    def _routes(cls, v: list[str]) -> list[str]:
        for r in v:
            if not re.fullmatch(r'/[A-Za-z0-9/_=&.%-]*', r):
                raise ValueError(f'bad route {r!r}')
        return v


class SelfTest(Strict):
    method: str = 'GET'
    path: str


class Guard(Strict):
    """The network policy every browser request is judged by (see guard/policy.py)."""

    denied_paths: list[str] = Field(default_factory=list, description='Regexes refused for every method.')
    allowed_writes: list[str] = Field(default_factory=list, description="Regexes, optionally 'POST|PATCH ^/x$'.")
    forbidden_body_keys: list[str] = Field(default_factory=list, description='Refused when set to non-null.')
    forbidden_body_values: dict[str, list[str]] = Field(default_factory=dict)
    json_only_paths: list[str] = Field(default_factory=list, description='Writes here must have a JSON body.')
    allow_delete: bool = False
    self_test: list[SelfTest] = Field(default_factory=list, description='Requests the guard must refuse at start.')

    @field_validator('denied_paths', 'json_only_paths')
    @classmethod
    def _re(cls, v: list[str]) -> list[str]:
        return path_patterns(v)

    @field_validator('allowed_writes')
    @classmethod
    def _writes(cls, v: list[str]) -> list[str]:
        return path_patterns(v, methods=True)


class Auth(Strict):
    """Sign-in is the app's own business: a command that writes a Playwright storage state."""

    command: str | None = Field(None, description='Runs with {url} and {state}; must write the storage state.')
    sign_out_command: str | None = Field(None, description='Runs with {url} and {state} when the run ends.')
    storage_state: str | None = Field(None, description='A storage state file to use as is.')


class Preflight(Strict):
    """A GET the run must pass before any browser starts. Fails closed on any HTTP or shape error."""

    path: str
    message: str
    require_keys: list[str] = Field(default_factory=list)
    fail_if_nonempty: list[str] = Field(default_factory=list, description='Top-level keys that must be empty.')
    fail_if_true: list[str] = Field(default_factory=list, description='Keys, anywhere in the reply, never true.')


class Journey(Strict):
    name: str
    goal: str
    start_path: str = '/'
    success_criteria: str = ''
    owns: list[str] = Field(default_factory=list, description='Globs of the files that implement it.')
    critical_selectors: list[str] = Field(default_factory=list)
    devices: list[Device] = Field(default_factory=lambda: ['desktop', 'mobile'])


class Personas(Strict):
    count: int = Field(4, ge=1)
    max_steps: int = Field(25, ge=1)
    concurrency: int = Field(3, ge=1)
    forbidden_goal_words: list[str] = Field(default_factory=list, description='Added to the built-in list.')


class LLM(Strict):
    provider: Literal['bedrock', 'anthropic', 'openai'] = 'bedrock'
    model: str = 'us.moonshotai.kimi-k3'
    region: str | None = Field(None, description='Bedrock region.')
    base_url: str | None = Field(None, description='OpenAI-compatible endpoint.')
    api_key_env: str | None = Field(None, description='Env var holding the key (anthropic, openai).')


class Budget(Strict):
    max_personas: int = Field(8, ge=1)
    max_total_steps: int = Field(400, ge=1, description='Personas x steps x targets may not exceed this.')
    max_tokens: int = Field(4_000_000, ge=1, description='Stop starting personas once spent.')
    timeout_minutes: int = Field(30, ge=1)


class Report(Strict):
    title: str = 'Persona swarm'
    fail_on: list[Severity] = Field(default_factory=lambda: ['blocker'])
    fail_on_unclassified: bool = Field(False, description='Gate on findings when no baseline ran.')
    no_em_dashes: bool = False
    images_repo: str | None = Field(None, description='owner/name to commit screenshots to so the comment shows them.')

    @field_validator('images_repo')
    @classmethod
    def _images_repo(cls, v: str | None) -> str | None:
        if v and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', v):
            raise ValueError(f'images_repo must be owner/name, got {v!r}')
        return v


class Config(Strict):
    app: App
    guard: Guard = Guard()
    auth: Auth = Auth()
    preflight: list[Preflight] = Field(default_factory=list)
    journeys: list[Journey] = Field(default_factory=list)
    shared: list[str] = Field(default_factory=list, description='Globs that select every journey.')
    personas: Personas = Personas()
    llm: LLM = LLM()
    budget: Budget = Budget()
    report: Report = Report()

    @model_validator(mode='after')
    def _journeys(self) -> 'Config':
        names = [j.name for j in self.journeys]
        if len(names) != len(set(names)):
            raise ValueError('journey names must be unique')
        if self.personas.count > self.budget.max_personas:
            raise ValueError(f'personas.count {self.personas.count} exceeds budget.max_personas')
        return self

    def app_hosts(self, origin: str) -> set[str]:
        hosts = {h.lower() for h in self.app.hosts} | {urlsplit(self.app.url).hostname or ''}
        if self.app.baseline_url:
            hosts.add(urlsplit(self.app.baseline_url).hostname or '')
        return hosts | {urlsplit(origin).hostname or ''}


def load(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f'{path} is not a YAML mapping')
    return Config.model_validate(data)
