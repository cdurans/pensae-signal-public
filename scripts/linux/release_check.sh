#!/usr/bin/bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly RELEASE_RUNTIME_DIR="$(mktemp -d /tmp/pensae-release-check.XXXXXX)"
readonly RELEASE_PYCACHE_DIR="${RELEASE_RUNTIME_DIR}/pycache"

export TMPDIR=/tmp TMP=/tmp TEMP=/tmp
export PENSAE_RELEASE_OWNERSHIP_DIR="${RELEASE_RUNTIME_DIR}/ownership"
export PYTHONPYCACHEPREFIX="${RELEASE_PYCACHE_DIR}"

mkdir -m 700 "${RELEASE_PYCACHE_DIR}"

cd "${REPO_ROOT}"

cleanup_release_runtime() {
  if [[ -d "${PENSAE_RELEASE_OWNERSHIP_DIR}" ]]; then
    rmdir "${PENSAE_RELEASE_OWNERSHIP_DIR}"
  fi
  if [[ -d "${RELEASE_PYCACHE_DIR}" ]]; then
    find "${RELEASE_PYCACHE_DIR}" -depth -delete
  fi
  rmdir "${RELEASE_RUNTIME_DIR}"
}
trap cleanup_release_runtime EXIT

run_stage() {
  local number="$1"
  local label="$2"
  local target="$3"
  echo "G8 ${number}/10 — ${label}"
  make --no-print-directory "${target}"
}

run_stage 1 "freeze locks" release-freeze-locks
run_stage 2 "format, lint, and type checks" release-static
run_stage 3 "deterministic suites" release-deterministic
run_stage 4 "disposable integration suites" release-integration
run_stage 5 "empty-database migration" release-empty-migration
run_stage 6 "temporary OpenAPI/client regeneration and diff" release-generated-client
run_stage 7 "production frontend build" release-frontend-build
run_stage 8 "required fake Chromium scenarios" release-browser-scenarios
run_stage 9 "packaging, secret, notice, license, model, migration, generated, and Chromium audits" release-packaging
run_stage 10 "final launcher-owned process and disposable-artifact cleanup verification" release-cleanup

echo "G8 release-check passed in the frozen order."
