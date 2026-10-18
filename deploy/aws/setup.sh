#!/bin/bash
# One-time, idempotent setup of a swarm-persona runner on AWS CodeBuild.
# Creates: a private S3 bucket (30-day expiry), an IAM role that may call only the Bedrock models
# you list, write its own logs and bucket, and read secrets under one prefix; and the CodeBuild
# project. Nothing else in the account is touched.
#
#   SP_EXPECT_ACCOUNT=123456789012 deploy/aws/setup.sh
#
# Settings (environment):
#   SP_EXPECT_ACCOUNT  refuse to run in any other account (recommended)
#   SP_REGION          default us-east-2
#   SP_PROJECT         CodeBuild project and role name prefix, default swarm-persona
#   SP_BUCKET          default <project>-<account>
#   SP_MODELS          space-separated Bedrock model ids, default us.moonshotai.kimi-k3
#   SP_SECRET_PREFIX   Secrets Manager names the build may read, default <project>/
#   SP_COMPUTE         CodeBuild compute type, default BUILD_GENERAL1_LARGE
set -euo pipefail
REGION=${SP_REGION:-us-east-2}
PROJECT=${SP_PROJECT:-swarm-persona}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
if [ -n "${SP_EXPECT_ACCOUNT:-}" ] && [ "$ACCOUNT" != "$SP_EXPECT_ACCOUNT" ]; then
  echo "Wrong AWS account: $ACCOUNT, expected $SP_EXPECT_ACCOUNT" >&2; exit 1
fi
BUCKET=${SP_BUCKET:-$PROJECT-$ACCOUNT}
ROLE=$PROJECT-codebuild
SECRET_PREFIX=${SP_SECRET_PREFIX:-$PROJECT/}
HERE=$(cd "$(dirname "$0")" && pwd)

if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [ "$REGION" = us-east-1 ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
  '{"Rules":[{"ID":"expire-30d","Status":"Enabled","Filter":{},"Expiration":{"Days":30}}]}'

# Only this project may assume the role, not any CodeBuild project in the account.
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"'$ACCOUNT'"},"ArnLike":{"aws:SourceArn":"arn:aws:codebuild:'$REGION':'$ACCOUNT':project/'$PROJECT'"}}}]}'
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" \
    --description "swarm-persona CodeBuild runner" >/dev/null
else
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$TRUST"
fi
MODEL_ARNS=$(python3 - "$ACCOUNT" ${SP_MODELS:-us.moonshotai.kimi-k3} <<'PY'
import json, sys
account, models = sys.argv[1], sys.argv[2:]
arns = []
for m in models:
    base = m.split('.', 1)[1] if m.split('.', 1)[0] in ('us', 'eu', 'apac', 'global') else m
    arns += [f'arn:aws:bedrock:*:{account}:inference-profile/{m}', f'arn:aws:bedrock:*::foundation-model/{base}']
print(json.dumps(arns))
PY
)
POLICY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"Models","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream","bedrock:Converse","bedrock:ConverseStream"],"Resource":$MODEL_ARNS},
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
  "Resource":"arn:aws:logs:$REGION:$ACCOUNT:log-group:/aws/codebuild/$PROJECT*"},
 {"Sid":"ReadSource","Effect":"Allow","Action":"s3:GetObject","Resource":"arn:aws:s3:::$BUCKET/source/*"},
 {"Sid":"WriteRuns","Effect":"Allow","Action":"s3:PutObject","Resource":"arn:aws:s3:::$BUCKET/runs/*"},
 {"Sid":"BucketList","Effect":"Allow","Action":["s3:GetBucketLocation","s3:ListBucket"],"Resource":"arn:aws:s3:::$BUCKET"},
 {"Sid":"Secrets","Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"arn:aws:secretsmanager:$REGION:$ACCOUNT:secret:$SECRET_PREFIX*"}
]}
JSON
)
aws iam put-role-policy --role-name "$ROLE" --policy-name "$PROJECT" --policy-document "$POLICY"

# The run caps itself at 40 minutes (buildspec.yml); the project allows 60 so a timed-out run
# still reaches its sign-out before CodeBuild kills the container.
PROJECT_JSON=$(cat <<JSON
{"name":"$PROJECT","description":"swarm-persona persona UX runs",
 "source":{"type":"S3","location":"$BUCKET/source/placeholder.zip","buildspec":"buildspec.yml"},
 "artifacts":{"type":"S3","location":"$BUCKET","path":"runs","namespaceType":"BUILD_ID","packaging":"NONE","name":"out"},
 "environment":{"type":"LINUX_CONTAINER","image":"aws/codebuild/standard:7.0","computeType":"${SP_COMPUTE:-BUILD_GENERAL1_LARGE}",
   "environmentVariables":[{"name":"SP_REGION","value":"$REGION"}]},
 "serviceRole":"arn:aws:iam::$ACCOUNT:role/$ROLE","timeoutInMinutes":60,
 "logsConfig":{"cloudWatchLogs":{"status":"ENABLED","groupName":"/aws/codebuild/$PROJECT"}}}
JSON
)
if aws codebuild batch-get-projects --names "$PROJECT" --region "$REGION" --query 'projects[0].name' --output text | grep -qx "$PROJECT"; then
  aws codebuild update-project --region "$REGION" --cli-input-json "$PROJECT_JSON" >/dev/null
else
  ERR=$(mktemp)
  for _ in 1 2 3 4 5 6; do  # a fresh role takes a few seconds to become assumable
    aws codebuild create-project --region "$REGION" --cli-input-json "$PROJECT_JSON" >/dev/null 2>"$ERR" && break
    sleep 10
  done
  aws codebuild batch-get-projects --names "$PROJECT" --region "$REGION" --query 'projects[0].name' --output text \
    | grep -qx "$PROJECT" || { cat "$ERR" >&2; exit 1; }
fi
echo "Ready: bucket $BUCKET, role $ROLE, project $PROJECT ($REGION). Run with $HERE/run.sh."
