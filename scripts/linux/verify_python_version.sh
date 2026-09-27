#!/usr/bin/bash
set -euo pipefail

readonly EXPECTED_PYTHON_VERSION="3.13.14"
readonly PYTHON_EXECUTABLE="${1:-python3.13}"

actual_version="$("${PYTHON_EXECUTABLE}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
if [[ "${actual_version}" != "${EXPECTED_PYTHON_VERSION}" ]]; then
  echo "Pensae Signal requires Python ${EXPECTED_PYTHON_VERSION}; found ${actual_version:-unavailable}." >&2
  exit 2
fi
