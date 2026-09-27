#!/usr/bin/bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly PROJECT_NAME="pensae_release_migration"
readonly UV_CACHE_DIR="${REPO_ROOT}/.uv-cache"
readonly UV_PYTHON_INSTALL_DIR="${REPO_ROOT}/.tools/python"

export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR
export TMPDIR=/tmp TMP=/tmp TEMP=/tmp
export COMPOSE_PROJECT_NAME="${PROJECT_NAME}"
export PENSAE_ENVIRONMENT=test
# Disposable fixture secrets; never reuse the operator installation's values.
export POSTGRES_USER=pensae POSTGRES_DB=pensae
POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
SEARXNG_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export POSTGRES_PASSWORD SEARXNG_SECRET
export PENSAE_POSTGRES_DSN="postgresql+psycopg://pensae:${POSTGRES_PASSWORD}@127.0.0.1:5432/pensae"

cd "${REPO_ROOT}"

cleanup() {
  timeout --foreground --signal=TERM --kill-after=5s 60s \
    docker compose --project-name "${PROJECT_NAME}" down --volumes --remove-orphans
}
trap cleanup EXIT

timeout --foreground --signal=TERM --kill-after=5s 180s \
  docker compose --project-name "${PROJECT_NAME}" up --detach --wait --wait-timeout 120 postgres
timeout --foreground --signal=TERM --kill-after=5s 60s \
  uv run --frozen --offline --python 3.13.14 alembic upgrade head
readonly current_revision="$(
  timeout --foreground --signal=TERM --kill-after=5s 30s \
    uv run --frozen --offline --python 3.13.14 alembic current
)"
if [[ "${current_revision}" != *"20260722_0005 (head)"* ]]; then
  echo "empty migration did not reach the single expected head" >&2
  exit 1
fi
echo "Disposable empty database migrated to 20260722_0005."
