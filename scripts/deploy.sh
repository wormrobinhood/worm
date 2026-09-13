#!/usr/bin/env bash
# Deploy a scanned, immutable committed tree to the explicit production service.
set -euo pipefail
PROJECT=3dbe1b57-8c65-44dc-9b49-1552fbd5c80b
ENVIRONMENT=production
ROOT=$(git rev-parse --show-toplevel)
REVISION=$(git -C "$ROOT" rev-parse HEAD)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
python3 "$ROOT/scripts/publish-check.py" --revision "$REVISION" --archive "$STAGE/verified.tar"
mkdir "$STAGE/source"
tar -xf "$STAGE/verified.tar" -C "$STAGE/source"
cd "$STAGE/source"
railway up --project "$PROJECT" --environment "$ENVIRONMENT" --service worm --detach \
    --message "WORM verified commit $REVISION"
echo "Deployment submitted for $REVISION; verify its health before treating it as complete."
