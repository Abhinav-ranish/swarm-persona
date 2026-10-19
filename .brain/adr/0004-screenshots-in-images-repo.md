# 0004: Screenshots are committed to an images repository and linked by commit

Status: accepted (v0.1.4, 5661e3d)

## Context

A PR comment needs images GitHub will render. Artifacts need a login and expire; the comment API
takes no uploads; branch-relative links change when a later run overwrites a file.

## Decision

`upload_images` in `src/swarm_persona/github.py` commits the run's referenced screenshots to
`report.images_repo` in one commit per run, under `<repo>/pr-<n>/<run_id>/`, and links each image
by that commit's SHA. `publish` in `src/swarm_persona/run.py` rebuilds the report with them embedded.
Without an images repository, screenshots are named, not shown.

## Consequences

- An earlier comment's pictures never change.
- On a private images repository only people who can read it see them; signed-in screenshots in a
  public one are public. Point it at a private repository.
- The images repository grows forever; pruning it is the owner's job.
- Uploads use the gh login (`Client.from_cli`), even when the comment is posted as the app.
