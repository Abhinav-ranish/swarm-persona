# Six nits from the final v0.2.0 review

Status: open. None blocks a run; all are small and can land in one PR with tests.

1. **Secret-name filter is too broad.** `SECRET_NAME` in `src/swarm_persona/fixes.py:52` matches
   `token`, `secret`, `passw` and `api[_-]?key` anywhere in any path segment, so ordinary files
   (`design-tokens.css`, `TokenInput.tsx`, `password-reset-page.tsx`) never reach the fix prompt. It
   fails safe (fewer fixes, no leak). Tighten to whole-word or file-name patterns, keep the
   folder rule for `secrets/`, and add the false-positive cases to `tests/test_fixes.py`.
2. **Merged ticket bodies have no length cap, and steps are numbered across findings.**
   `issue_body` in `src/swarm_persona/tickets.py:81` joins every detail and step of every merged
   finding. GitHub refuses issue bodies over 65536 characters, so a large merge fails to file. The
   steps list (`tickets.py:88`) numbers the steps of all findings as one sequence. Cap the body,
   and group steps per finding.
3. **auth.log open errors read as "could not start".** In `Session.command`
   (`src/swarm_persona/run.py:48`) the `os.open` of `SWARM_PERSONA_AUTH_LOG` sits inside the same
   `try` as the program launch, so a missing or unwritable log is reported as
   `could not start <program>`. Open the log before the `try` with its own message.
4. **status and close raise a raw JSONDecodeError on a corrupt run.json.** `runs()` skips corrupt
   directories, but `status`, `findings` and `close` call `read_meta` directly
   (`src/swarm_persona/mcp_server.py:250`, `:259`, `:301`). Raise a ValueError naming the run.
5. **No fsync before os.replace.** `save_meta` (`src/swarm_persona/mcp_server.py:133`) renames a
   file that may not be on disk yet; after a crash run.json can be empty. Flush and fsync the file
   before `os.replace`.
6. **A merged ticket's classification comes from its first finding.** `build` in
   `src/swarm_persona/tickets.py:57` sets classification once, from the first finding of a
   fingerprint. It is right today only because `judge` sorts new findings first. Take the most
   urgent classification explicitly, as severity already does.

Done when: each has a test, `deploy/aws/test.sh` is green, and this file is deleted with its
index.md line.
