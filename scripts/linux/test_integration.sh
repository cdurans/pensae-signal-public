#!/usr/bin/bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly PROJECT_NAME="pensae_integration"
readonly UV_CACHE_DIR="${REPO_ROOT}/.uv-cache"
readonly UV_PYTHON_INSTALL_DIR="${REPO_ROOT}/.tools/python"

export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR
export TMPDIR=/tmp TMP=/tmp TEMP=/tmp
export PENSAE_INTEGRATION=1
export COMPOSE_PROJECT_NAME="${PROJECT_NAME}"
export PENSAE_ENVIRONMENT=test
# Disposable fixture secrets; never reuse the operator installation's values.
export POSTGRES_USER=pensae POSTGRES_DB=pensae
POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
SEARXNG_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export POSTGRES_PASSWORD SEARXNG_SECRET
export PENSAE_POSTGRES_DSN="postgresql+psycopg://pensae:${POSTGRES_PASSWORD}@127.0.0.1:5432/pensae"
export PENSAE_REDIS_URL="redis://127.0.0.1:6379/0"
export PENSAE_SEARXNG_URL="http://127.0.0.1:8888"

cd "${REPO_ROOT}"

cleanup() {
  timeout --foreground --signal=TERM --kill-after=5s 60s \
    docker compose --project-name "${PROJECT_NAME}" down --volumes --remove-orphans
}
trap cleanup EXIT

timeout --foreground --signal=TERM --kill-after=5s 300s \
  docker compose --project-name "${PROJECT_NAME}" up --detach --wait --wait-timeout 120
timeout --foreground --signal=TERM --kill-after=5s 60s \
  uv run --frozen --offline --python 3.13.14 alembic upgrade head
timeout --foreground --signal=TERM --kill-after=5s 120s \
  uv run --frozen --offline --python 3.13.14 pytest -m integration
timeout --foreground --signal=TERM --kill-after=5s 300s \
  ./scripts/linux/test_database_operations.sh
