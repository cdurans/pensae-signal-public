#!/usr/bin/bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly FIXTURE_DATABASE="pensae_upgrade_fixture"
readonly OPERATIONS_DIR="$(mktemp -d /tmp/pensae-database-operations.XXXXXX)"
readonly BACKUP_DIR="${OPERATIONS_DIR}/backups"
readonly OWNERSHIP_DIR="${OPERATIONS_DIR}/ownership"
readonly FIXTURE_DSN="postgresql+psycopg://pensae:${POSTGRES_PASSWORD:?Run through the disposable integration harness}@127.0.0.1:5432/${FIXTURE_DATABASE}"

cleanup() {
  docker compose exec -T postgres dropdb --if-exists --username=pensae "${FIXTURE_DATABASE}" \
    >/dev/null 2>&1 || true
  rm -rf "${OPERATIONS_DIR}"
}
trap cleanup EXIT

cd "${REPO_ROOT}"
mkdir -p "${OWNERSHIP_DIR}"
docker compose exec -T postgres createdb --username=pensae "${FIXTURE_DATABASE}"

PENSAE_POSTGRES_DSN="${FIXTURE_DSN}" \
  uv run --frozen --offline --python 3.13.14 alembic upgrade 20260722_0004
docker compose exec -T postgres psql \
  --username=pensae \
  --dbname="${FIXTURE_DATABASE}" \
  --no-psqlrc \
  --command="INSERT INTO sources (id,url,title,retrieved_at,credibility_note,content_fingerprint) VALUES ('00000000-0000-0000-0000-000000000099','https://example.test/upgrade','Upgrade fixture',now(),'Disposable restore fixture',repeat('c',64))"

PENSAE_POSTGRES_DSN="${FIXTURE_DSN}" \
PENSAE_DATA_DIR="${OPERATIONS_DIR}" \
PENSAE_OWNERSHIP_DIR="${OWNERSHIP_DIR}" \
  uv run --frozen --offline --python 3.13.14 \
  python -m pensae.operations backup --if-nonempty

readonly BACKUP_FILE="$(find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'pensae-*.dump' -print -quit)"
test -n "${BACKUP_FILE}"
test "$(stat --format='%a' "${BACKUP_FILE}")" = "600"

if PENSAE_POSTGRES_DSN="${FIXTURE_DSN}" \
  PENSAE_DATA_DIR="${OPERATIONS_DIR}" \
  PENSAE_OWNERSHIP_DIR="${OWNERSHIP_DIR}" \
  PENSAE_RESTORE_BACKUP="${BACKUP_FILE}" \
  PENSAE_RESTORE_CONFIRMATION="RESTORE wrong.dump" \
    uv run --frozen --offline --python 3.13.14 \
    python -m pensae.operations restore; then
  echo "restore accepted an incorrect typed confirmation" >&2
  exit 1
fi

readonly RESTORE_OUTPUT="$(
  PENSAE_POSTGRES_DSN="${FIXTURE_DSN}" \
  PENSAE_DATA_DIR="${OPERATIONS_DIR}" \
  PENSAE_OWNERSHIP_DIR="${OWNERSHIP_DIR}" \
  PENSAE_RESTORE_BACKUP="${BACKUP_FILE}" \
  PENSAE_RESTORE_CONFIRMATION="RESTORE $(basename "${BACKUP_FILE}")" \
    uv run --frozen --offline --python 3.13.14 \
    python -m pensae.operations restore
)"
readonly FRESH_DATABASE="${RESTORE_OUTPUT#*fresh database }"
readonly RESTORED_DATABASE="${FRESH_DATABASE%% at revision*}"

case "${RESTORED_DATABASE}" in
  pensae_restore_*) ;;
  *)
    echo "restore did not report a generated fresh database" >&2
    exit 1
    ;;
esac

test "$(
  docker compose exec -T postgres psql \
    --username=pensae \
    --dbname="${RESTORED_DATABASE}" \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --command="SELECT version_num FROM alembic_version"
)" = "20260722_0005"
test "$(
  docker compose exec -T postgres psql \
    --username=pensae \
    --dbname="${RESTORED_DATABASE}" \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --command="SELECT count(*) FROM sources WHERE id='00000000-0000-0000-0000-000000000099'"
)" = "1"

docker compose exec -T postgres dropdb --username=pensae "${RESTORED_DATABASE}"
echo "Disposable backup/restore and non-empty migration matrix passed."
