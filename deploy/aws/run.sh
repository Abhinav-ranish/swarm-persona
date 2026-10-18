#!/bin/bash
# Run swarm-persona for a PR on the CodeBuild project from setup.sh. The caller's machine only zips,
# starts the build, waits and posts: browsers, proxies and the model all run in AWS.
#
#   deploy/aws/run.sh --config swarm-persona.yml --pr 123 [--include scripts/qa_login.py]
#                     [--url https://preview.example.com] [--ref <sha>] [--args "--no-baseline"]
#                     [--no-post]
#
# --include files are copied next to the config (an auth script the config's auth.command runs).
# --ref is the swarm-persona commit CodeBuild installs; it defaults to this checkout's HEAD.
# Exits 0 when the gate passed, 1 when it failed or the run did not complete. Settings: SP_REGION,
# SP_PROJECT and SP_BUCKET as for setup.sh, and SP_EXPECT_ACCOUNT.
set -euo pipefail
REGION=${SP_REGION:-us-east-2}
PROJECT=${SP_PROJECT:-swarm-persona}
HERE=$(cd "$(dirname "$0")" && pwd)
CONFIG=; PR=; URL=; REF=; ARGS=; POST=1; INCLUDE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG=$2; shift 2 ;;
    --pr) PR=$2; shift 2 ;;
    --url) URL=$2; shift 2 ;;
    --ref) REF=$2; shift 2 ;;
    --args) ARGS=$2; shift 2 ;;
    --include) INCLUDE+=("$2"); shift 2 ;;
    --no-post) POST=0; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$CONFIG" ] && [ -f "$CONFIG" ] || { echo "--config FILE is required" >&2; exit 2; }
case "$PR" in ''|*[!0-9]*) echo "--pr must be a number" >&2; exit 2 ;; esac
case "$ARGS" in *[!a-z\ -]*) echo "--args may only hold flags such as --no-baseline" >&2; exit 2 ;; esac
[ -z "$URL" ] || printf '%s' "$URL" | grep -Eq '^https://[A-Za-z0-9.-]+(:[0-9]+)?$' || { echo "bad --url $URL" >&2; exit 2; }
REF=${REF:-$(git -C "$HERE" rev-parse HEAD)}
printf '%s' "$REF" | grep -Eq '^[0-9a-f]{40}$' || { echo "--ref must be a full commit SHA" >&2; exit 2; }
if ! git -C "$HERE" branch -r --contains "$REF" 2>/dev/null | grep -q .; then
  echo "note: $REF is not on any fetched remote branch; CodeBuild installs it from GitHub" >&2
fi

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
if [ -n "${SP_EXPECT_ACCOUNT:-}" ] && [ "$ACCOUNT" != "$SP_EXPECT_ACCOUNT" ]; then
  echo "Wrong AWS account: $ACCOUNT, expected $SP_EXPECT_ACCOUNT (aws sso login?)" >&2; exit 1
fi
BUCKET=${SP_BUCKET:-$PROJECT-$ACCOUNT}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/swarm-persona-$PR.XXXXXX")
gh pr view "$PR" --json number,title,body,files,url > "$WORK/pr.json"
cp "$CONFIG" "$WORK/swarm-persona.yml"
cp "$HERE/buildspec.yml" "$WORK/"
for f in ${INCLUDE[@]+"${INCLUDE[@]}"}; do cp "$f" "$WORK/"; done
(cd "$WORK" && zip -q -r source.zip . -x source.zip)
KEY="source/pr-$PR-$(date +%Y%m%d%H%M%S).zip"
aws s3 cp --quiet "$WORK/source.zip" "s3://$BUCKET/$KEY" --region "$REGION"

ENV_JSON=$(python3 -c 'import json,sys; print(json.dumps([{"name":k,"value":v,"type":"PLAINTEXT"} for k,v in zip(sys.argv[1::2], sys.argv[2::2])]))' \
  SP_REF "$REF" SP_CONFIG swarm-persona.yml SP_PR "$PR" SP_URL "$URL" SP_ARGS "$ARGS")
BUILD=$(aws codebuild start-build --region "$REGION" --project-name "$PROJECT" \
  --source-location-override "$BUCKET/$KEY" --environment-variables-override "$ENV_JSON" \
  --query build.id --output text)
echo "Started $BUILD (PR #$PR, swarm-persona $REF)"

STATUS=IN_PROGRESS
while [ "$STATUS" = IN_PROGRESS ]; do
  sleep 30
  STATUS=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD" --query 'builds[0].buildStatus' --output text)
done
echo "Build $STATUS"

OUT="$WORK/out"
ART=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD" --query 'builds[0].artifacts.location' --output text)
if [ -n "$ART" ] && [ "$ART" != None ]; then
  ART="s3://${ART#arn:aws:s3:::}"
  aws s3 cp --quiet --recursive "$ART" "$OUT" --region "$REGION" || echo "no artifacts downloaded" >&2
fi
if [ "$STATUS" != SUCCEEDED ]; then
  aws logs tail "/aws/codebuild/$PROJECT" --region "$REGION" --log-stream-names "${BUILD#*:}" 2>/dev/null | tail -40 || true
  [ -d "$OUT" ] && echo "Partial output kept in $OUT; not posted." >&2
  exit 1
fi
# A passed build whose report did not come back is a failed run, never a silent success.
[ -s "$OUT/report.md" ] && [ -s "$OUT/report.json" ] || { echo "Build passed but the report was not downloaded; see $ART" >&2; exit 1; }
echo "Report: $OUT/report.md (screenshots: $OUT/shots, also $ART/shots)"
if [ "$POST" = 1 ]; then
  REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
  ME=$(gh api user --jq .login)  # anyone can paste the marker: edit only our own comment
  ID=$(gh api --paginate "repos/$REPO/issues/$PR/comments" \
    --jq ".[] | select(.user.login == \"$ME\" and (.body | contains(\"<!-- swarm-persona -->\"))) | .id" | head -1)
  if [ -n "$ID" ]; then
    gh api -X PATCH "repos/$REPO/issues/comments/$ID" -F "body=@$OUT/report.md" --jq .html_url
  else
    gh api -X POST "repos/$REPO/issues/$PR/comments" -F "body=@$OUT/report.md" --jq .html_url
  fi
fi
python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["passed"] else 1)' "$OUT/report.json"
