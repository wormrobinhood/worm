#!/usr/bin/env bash
# Deploy the committed tree to Railway. Only files git tracks are uploaded, so no
# .env, no data/, no brand/, no rehearsal/ ever leaves this machine.
set -euo pipefail
PROJECT=3dbe1b57-8c65-44dc-9b49-1552fbd5c80b
ENVIRONMENT=production
ROOT=$(git rev-parse --show-toplevel)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
git -C "$ROOT" archive --format=tar HEAD | tar -x -C "$STAGE"
cd "$STAGE"
railway link --project "$PROJECT" --environment "$ENVIRONMENT" --service worm >/dev/null
railway up -d -y
echo "deploying $(git -C "$ROOT" rev-parse --short HEAD)"
