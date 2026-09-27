#!/usr/bin/bash
set -euo pipefail

readonly EXPECTED_UV_VERSION="0.11.28"
readonly UV_EXECUTABLE="${1:-uv}"

actual_version="$(${UV_EXECUTABLE} --version 2>/dev/null || true)"
if [[ ! "${actual_version}" =~ ^uv[[:space:]]${EXPECTED_UV_VERSION}([[:space:]]|$) ]]; then
  echo "Pensae Signal requires uv ${EXPECTED_UV_VERSION}; found ${actual_version:-unavailable}." >&2
  exit 2
fi
