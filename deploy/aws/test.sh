#!/bin/bash
# Run the test suite on the CodeBuild project from setup.sh, uncommitted changes included. The
# caller's machine only zips the working tree (tracked files plus untracked ones git does not
# ignore), starts the build and prints the log.
#
#   deploy/aws/test.sh [pytest args, e.g. tests/test_judge.py -k verdict]
#
# Exits with pytest's result. Settings: SP_REGION, SP_PROJECT and SP_BUCKET as for setup.sh, and
# SP_EXPECT_ACCOUNT.
set -euo pipefail
REGION=${SP_REGION:-us-east-2}
PROJECT=${SP_PROJECT:-swarm-persona}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
for a in "$@"; do
  case "$a" in *[!A-Za-z0-9_./:=-]*) echo "unsupported pytest argument: $a" >&2; exit 2 ;; esac
done

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
if [ -n "${SP_EXPECT_ACCOUNT:-}" ] && [ "$ACCOUNT" != "$SP_EXPECT_ACCOUNT" ]; then
  echo "Wrong AWS account: $ACCOUNT, expected $SP_EXPECT_ACCOUNT (aws sso login?)" >&2; exit 1
fi
BUCKET=${SP_BUCKET:-$PROJECT-$ACCOUNT}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/swarm-persona-test.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
(cd "$ROOT" && git ls-files -z --cached --others --exclude-standard | xargs -0 zip -q "$WORK/source.zip")
KEY="source/tests-$(date +%Y%m%d%H%M%S).zip"
aws s3 cp --quiet "$WORK/source.zip" "s3://$BUCKET/$KEY" --region "$REGION"

SPEC=$(cat <<YAML
version: 0.2
phases:
  install:
    commands:
      - python3 -m pip install -q --user uv==0.12.15
      # The live guard tests drive the real proxy; without mitmdump they skip.
      - \$HOME/.local/bin/uv tool install -q --python 3.12 mitmproxy==11.1.3
  build:
    commands:
      - export PATH="\$HOME/.local/bin:\$PATH"
      - export SWARM_PERSONA_MITMDUMP="\$(uv tool dir --bin)/mitmdump"
      - uv run -q --python 3.12 --extra dev --extra bedrock --extra mcp pytest -q $*
YAML
)
BUILD=$(aws codebuild start-build --region "$REGION" --project-name "$PROJECT" \
  --source-location-override "$BUCKET/$KEY" --buildspec-override "$SPEC" \
  --artifacts-override type=NO_ARTIFACTS --query build.id --output text)
echo "Started $BUILD"

STATUS=IN_PROGRESS
while [ "$STATUS" = IN_PROGRESS ]; do
  sleep 15
  STATUS=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD" --query 'builds[0].buildStatus' --output text)
done
aws logs tail "/aws/codebuild/$PROJECT" --region "$REGION" --log-stream-names "${BUILD#*:}" --format short 2>/dev/null \
  | grep -v '\[Container\]' | tail -60 || true
echo "Tests $STATUS"
[ "$STATUS" = SUCCEEDED ]
