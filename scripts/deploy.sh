#!/usr/bin/env bash
# Build the Lambda bundle and deploy the CDK stack. Extra args are passed to `cdk deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/build_lambda.sh
cd infra
npx --yes aws-cdk@latest deploy MarketingAgent --require-approval broadening "$@"
