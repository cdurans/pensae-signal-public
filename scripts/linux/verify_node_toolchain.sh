#!/usr/bin/bash
set -euo pipefail

readonly EXPECTED_NODE_VERSION="24.18.0"
readonly NODE_HOME="${1:-}"
readonly NODE_EXECUTABLE="${NODE_HOME}/bin/node"
readonly NPM_CLI="${NODE_HOME}/lib/node_modules/npm/bin/npm-cli.js"

actual_version="$("${NODE_EXECUTABLE}" --version 2>/dev/null || true)"
if [[ "${actual_version}" != "v${EXPECTED_NODE_VERSION}" ]] || \
  [[ ! -f "${NPM_CLI}" ]] || [[ -L "${NPM_CLI}" ]]; then
  echo "Pensae Signal requires Node ${EXPECTED_NODE_VERSION} with its pinned archive npm CLI." >&2
  exit 2
fi
