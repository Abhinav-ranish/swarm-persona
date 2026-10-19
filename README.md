# swarm-persona

Simulated users test your pull request. A small swarm of LLM-driven personas (a hurried founder on
a phone, a careful first-timer on a laptop) walk the journeys a change touches on your preview
deploy, file UX issues with screenshots as they go, and leave one ranked report on the PR.

Two things make it usable against a real app with a real signed-in test account:

- **A network write guard, not a polite prompt.** Every browser request goes through its own
  mitmproxy instance that refuses anything outside an allowlist you write: no publishing, sending,
  paying, deleting or connecting accounts, however the page or the model tries. The run will not
  start until each proxy has proven that it refuses.
- **A verdict you can gate CI on.** Hard signals (an entry page that fails, an uncaught error, a
  5xx, a button covered by an overlay) set severity by rule. Agent opinions are capped unless
  corroborated. With a baseline (production) configured, every finding is classified as **new**,
  **pre-existing** or **improved**, so a PR is never blamed for a bug it did not cause.

```
PR diff ──► journey selection ──► personas ──► preflight + guard self-test
                                                   │
          ┌────────────── preview ─────────────────┼──────────── baseline (optional) ──┐
          │ Playwright entry checks (hard signals) │  same checks, same journeys        │
          │ browser-use personas, vision, guarded  │  same personas                     │
          └────────────────────────┬───────────────┴────────────────────────────────────┘
                                   ▼
          merge duplicates (model) ──► judge (rules, deterministic) ──► report.md / .json
                                                                    └─► sticky PR comment
```

## Quick start

```bash
uv tool install "swarm-persona[bedrock] @ git+https://github.com/Abhinav-ranish/swarm-persona"
uv tool install mitmproxy==11.1.3
"$(uv tool dir)/swarm-persona/bin/python" -m playwright install chromium

swarm-persona init swarm-persona.yml          # then edit it
swarm-persona check-config --config swarm-persona.yml
swarm-persona guard-test --config swarm-persona.yml
swarm-persona run --config swarm-persona.yml --pr 123 --out out --comment
```

`swarm-persona run` exits 0 when nothing new hit the gate, 1 when something did, 2 when it could not
run (bad config, preflight refused, the guard failed its self-test).

Personas run real Chromium instances with vision models; four personas take 10 to 20 minutes and
several GB of memory. Running them in CI or on AWS (below) is usually nicer than on a laptop.

## The config

Everything about your app lives in `swarm-persona.yml`. The annotated
[example](src/swarm_persona/example.yml) covers every key. The sections:

| Section | What it holds |
|---|---|
| `app` | name, a one-paragraph description for the planner, the preview `url`, the optional `baseline_url`, other `hosts`, fallback `routes` |
| `guard` | `denied_paths` (refused even as GETs), `allowed_writes` (the only writes allowed), `forbidden_body_keys` / `forbidden_body_values`, `json_only_paths`, `self_test` requests |
| `auth` | a `command` that writes a Playwright storage state to `{state}`, a `sign_out_command`, or a ready `storage_state` file |
| `preflight` | GETs that must pass before any browser starts (for example "the test workspace has no connected social account") |
| `journeys` | `name`, `goal`, `start_path`, `success_criteria`, `owns` globs, `critical_selectors`, `devices` |
| `shared` | globs whose change selects every journey (root layout, middleware, global CSS, lockfile) |
| `personas`, `llm`, `budget`, `report` | count and steps; provider and model; persona, step, token and time caps; `fail_on` severities |

## How a run decides

**Journey selection.** Ownership globs are authoritative: a changed file a journey `owns` selects it,
and a `shared` file selects all of them. The model may then add journeys for files no glob
claimed, never remove one. A PR that touches no journey runs nothing and costs nothing.
`swarm-persona select --config ... --pr 123` shows the selection without spending anything.

**Personas.** Each persona walks one selected journey on one device. Its goal is the journey's goal
from your config, which PR text cannot change; the model only writes who the person is. Without
journeys, the model writes goals too, and they pass a forbidden-words filter.

**Entry checks.** Plain Playwright loads each journey's entry page per device, through the same
guard, and records the HTTP status, uncaught page errors, console errors, failed requests, 5xx
answers from the app, and whether each critical selector exists, is visible, is enabled and would
receive a click (`click(trial=True)` runs the actionability checks without clicking, which catches
overlays).

**Severity rules** (a model can lower nothing it did not set, and can raise nothing):

| Evidence | Severity |
|---|---|
| Entry page fails to load, answers an error, or a critical selector is unusable, on every device | blocker |
| The same on some devices; an uncaught page error; a 5xx from the app (in a check or mid-journey) | major |
| Console errors, failed requests | minor (never confirms anything alone) |
| An agent's report | what the persona said (the most severe, when merged), capped at major unless two personas hit it or a hard signal fired on the same journey; the merge model's severity is ignored |
| Personas gave up on the preview where the baseline's finished | major |

**Classification.** With `baseline_url` set, the same checks and personas run against production.
Hard signals match by fingerprint (journey, device, kind, and the detail with ids, hashes, numbers
and query strings removed); agent reports match by journey, page and category. No model decides
whether something is new. Without a baseline every finding is `unclassified`, and the gate ignores
them unless `report.fail_on_unclassified` is set.

**Gate.** The run fails when a `new` finding (or an `unclassified` one, if you opted in) has a
severity in `report.fail_on` (default `[blocker]`).

## Audience and saved personas

`personas.audience` is free text about who uses the app ("team leads at small agencies, mostly on
laptops"); the planner casts every generated persona from it. `personas.saved` lists fixed people
(`name`, `background`, `tech_comfort`, `patience`). They take the seats in order before any are
generated, and the model writes personas only for the seats left over, so a regression run meets
the same testers every time. When the saved list fills every seat, planning makes no model call.

```yaml
personas:
  count: 3
  audience: Team leads at agencies of 5 to 20 people; few read documentation.
  saved:
    - {name: Priya, background: Runs a five-person design studio., tech_comfort: high, patience: low}
    - {name: Tom, background: A new account manager., tech_comfort: low}
```

## Per-persona verdicts

Every persona gets **pass**, **fail** or **partial** against its journey's `success_criteria`,
decided from the evidence, in this order:

| Evidence | Verdict |
|---|---|
| The persona crashed | fail |
| It finished and marked the goal met (browser-use's done action, `success: true`) | pass; partial if a hard signal fired on its journey and device, or its own verdict says it gave up |
| It finished and marked the goal not met | fail |
| It ran out of `max_steps` without finishing | fail |
| None of the above | its own ACHIEVED / GAVE UP verdict, labelled **self-report** (partial when a hard signal fired) |

The report opens with a one-line summary ("3/4 personas completed; 2 issues"), shows each preview
persona's verdict and why, and folds a **Worked well** list: personas return up to three things
that went smoothly in their structured final answer. All of it is rebuilt from `evidence.json`, so
`replay` shows it too.

## Possible fixes

When the PR's diff is available (`gh pr diff`, or a `diff` key in the `--pr-context` file), each
**new** or **unclassified** finding of severity major or worse gets one model call. It sees the
finding and only the part of the diff its journey `owns`, truncated to `report.fix_diff_chars`,
and answers with the likely file and the smallest change. The report shows it as **Possible fix
(unverified)**: nothing runs or checks it. At most five calls per run, each capped at
`report.fix_max_tokens`, and their tokens count toward `budget.max_tokens`. Pre-existing findings,
findings whose journey owns no changed file, and runs without a diff make no call. Turn it off with
`report.suggest_fixes: false`. The answer is stored in `evidence.json`, so `replay` shows it.

## Output

`--out` holds `report.md` (also the PR comment), `report.json` (findings, gate, metadata),
`evidence.json` (everything the verdict was built from), `shots/` (per-issue and per-check
screenshots) and `guard/` (each proxy's refusals and observed 5xx).

`swarm-persona replay out/` rebuilds the report from `evidence.json` alone, with no browser and no
model: change the rules or `--fail-on` and see the new verdict for free. The test suite runs on
recorded evidence the same way.

The PR comment opens with two small tables (gate, personas, findings; then one row per finding); each
finding's detail and screenshots, the sessions, the entry checks and the guard log are folded below.
Set `report.images_repo` (a repository you create for this; private keeps them private) and
screenshots are committed there, one commit per run, and embedded in the comment pinned to that
commit. Post as a GitHub App instead of yourself with `--github-app app.json` (or
`SWARM_PERSONA_GITHUB_APP`): `{app_id, installation_id, private_key}`, needing only Pull requests
write. `swarm-persona publish out/ --pr 123` does all of this for a finished run.

The PR comment is a single comment, found by a hidden marker and its author (the account running
swarm-persona; anyone can paste the marker), and edited in place on every run. Text from pages and
models is defanged before it lands there: no @mentions, issue cross-references, links, autolinks,
images or HTML. Issue URLs are recorded without query strings.

## The write guard

Each persona's browser, and the checker's, talks only to its own `mitmdump` running
[`guard/addon.py`](src/swarm_persona/guard/addon.py). The policy
([`guard/policy.py`](src/swarm_persona/guard/policy.py)) is a pure function, decided per request and
failing closed:

- Only https. CONNECT only to port 443 (or the app's own port). WebSocket upgrades, raw TCP and UDP
  are refused, QUIC is disabled and loopback goes through the proxy too, so no request leaves the
  browser any other way. The agent cannot open `data:` or `blob:` pages either: browser-use's own
  navigate action is replaced by one that only opens pages of the app.
- Every upstream name is resolved once and refused if any address is loopback, private,
  link-local, carrier-grade NAT, multicast or otherwise not global (an IPv4-mapped IPv6 address is
  judged by the IPv4 address inside it); the connection is pinned to the address that was checked,
  so DNS rebinding cannot reach an internal host. IP-literal upstreams are refused.
- Another site: reads only, never as a page or frame (a booking widget, an OAuth screen).
- The app's hosts: `denied_paths` are refused for every method; any other write must match
  `allowed_writes` exactly as sent (no `%`-encoded, dot-segment or `;matrix` tricks), optionally
  per method (`POST|PATCH ^/api/notes$`). DELETE is refused unless allowed, and so is any method
  override (`X-HTTP-Method-Override`, a `_method` field).
- The body is judged as the app will read it, decompressed: JSON (escapes decoded, every duplicate
  key checked, any depth, a byte-order mark stripped) or an urlencoded form (`note[publish_at]` and
  `note.publish_at` split into segments, every value of a repeated key). It must not set a
  forbidden key or value. Keys match whatever their case or separators: `scheduled_publish_at`,
  `scheduledPublishAt`, `Scheduled-Publish-At` and `scheduled\u005fpublish_at` are one key. A
  body the guard cannot judge (multipart, another charset, non-JSON text) is refused.

Before any browser starts, every proxy must refuse a write outside the allowlist, another site in a
frame, and each `guard.self_test` request. `preflight` checks run against the signed-in session and
fail closed on an HTTP error, a redirect (never followed, so the session cookie cannot leak), a
non-JSON reply or a missing key, so an API that changes shape stops the run instead of passing it.
A storage state may only hold cookies scoped exactly to an app host.

The guard is the safety; the prompt rules are a courtesy. Still, point the swarm at a dedicated test
account with nothing connected that could publish or pay.

## Running it

**GitHub Actions.** [`examples/workflow.yml`](examples/workflow.yml) runs on every successful preview
deployment, reads the config from the default branch (so a PR cannot loosen its own guard), skips
forks, and uses this repository as an action. The evidence artifact (signed-in screenshots) is kept
for one day by default (`evidence-retention-days`); on a public repository any GitHub user can
download artifacts:

```yaml
- uses: Abhinav-ranish/swarm-persona@<commit-sha>
  env: { GH_TOKEN: "${{ github.token }}", ANTHROPIC_API_KEY: "${{ secrets.ANTHROPIC_API_KEY }}" }
  with: { config: swarm-persona.yml, pr: "${{ steps.pr.outputs.number }}", url: "${{ github.event.deployment_status.environment_url }}" }
```

**AWS CodeBuild.** For teams that keep test credentials and model access in AWS:
[`deploy/aws/setup.sh`](deploy/aws/setup.sh) creates a private bucket, a least-privilege role (only
the Bedrock models you list, its own logs and bucket, secrets under one prefix) and the project.
[`deploy/aws/run.sh`](deploy/aws/run.sh) zips your config and any auth helper, starts a build that
installs swarm-persona at an exact commit SHA, waits, downloads the evidence and updates the PR
comment.

```bash
SP_EXPECT_ACCOUNT=123456789012 deploy/aws/setup.sh
deploy/aws/run.sh --config swarm-persona.yml --pr 123 --include scripts/qa_login.py
```

**Model providers.** `llm.provider` is `bedrock` (ambient AWS credentials; tool use is forced for
models that tend to answer in prose), `anthropic` (`api_key_env`, default `ANTHROPIC_API_KEY`) or
`openai` (any OpenAI-compatible `base_url`). One model drives the personas, the planner and the
merge pass. Vision is required.

## Budgets

`budget.max_personas` caps the plan, `max_total_steps` caps personas x steps x targets (the plan is
trimmed to fit), `max_tokens` stops new personas from starting once spent, and `timeout_minutes`
cuts off the walk and keeps what finished. On SIGTERM the run unwinds: proxies stop and the
sign-out command runs.

## Limits

- Simulated users show where an agent got stuck, not what real users do. Treat agent-only findings
  as leads; the hard signals are the part to gate on.
- No retries yet: an agent-only finding is never confirmed by a second identical run.
- Agent reports match the baseline coarsely (journey, page, category): a new problem on a page that
  already had one of the same category reads as pre-existing.
- The baseline must accept the same test account as the preview. Where test accounts only exist in
  production, leave `baseline_url` out and read findings as unclassified.

## Credits

The persona swarm and the network guard grew out of ContentOS's internal UX swarm. The baseline
oracle, diff-aware journey selection, rule-first severity, trial-click overlay checks, sticky
comment and offline replay come from [ux-guardian](https://github.com/aranish-uk/ux-guardian), a
hackathon prototype built on TinyFish. Personas run on [browser-use](https://github.com/browser-use/browser-use).

## License

[MIT](LICENSE)
