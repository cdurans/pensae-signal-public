#!/usr/bin/bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
temporary_root=$(mktemp -d /tmp/pensae-generated-client.XXXXXX)
trap 'rm -rf -- "$temporary_root"' EXIT

cd "$repo_root"
uv run --frozen --offline --python 3.13.14 python scripts/generate_openapi.py \
  --output "$temporary_root/openapi.json"
diff --unified openapi.json "$temporary_root/openapi.json"

pnpm --dir frontend exec openapi-ts \
  -i "$temporary_root/openapi.json" \
  -o "$temporary_root/generated"
diff --recursive --unified frontend/src/api/generated "$temporary_root/generated"
