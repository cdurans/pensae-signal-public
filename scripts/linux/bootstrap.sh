#!/usr/bin/bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly PYTHON_VERSION="3.13.14"
readonly NODE_VERSION="24.18.0"
readonly NODE_ARCHIVE="node-v${NODE_VERSION}-linux-x64.tar.xz"
readonly NODE_SHA256="55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742"
readonly NODE_HOME="${REPO_ROOT}/.tools/node-v${NODE_VERSION}-linux-x64"
readonly PNPM_HOME="${REPO_ROOT}/.tools/pnpm"
readonly UV_CACHE_DIR="${REPO_ROOT}/.uv-cache"
readonly UV_PYTHON_INSTALL_DIR="${REPO_ROOT}/.tools/python"
readonly PYTHON_HOME="${UV_PYTHON_INSTALL_DIR}/cpython-${PYTHON_VERSION}-linux-x86_64-gnu"
readonly PYTHON_EXECUTABLE="${PYTHON_HOME}/bin/python3.13"

export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR
export PATH="${NODE_HOME}/bin:${PNPM_HOME}/bin:/usr/local/bin:/usr/bin:/bin"
export TMPDIR=/tmp TMP=/tmp TEMP=/tmp

cd "${REPO_ROOT}"

if [[ ! -f /etc/fedora-release ]] || ! grep -qx "Fedora release 44 (Forty Four)" /etc/fedora-release; then
  echo "Pensae Signal bootstrap requires native Fedora 44." >&2
  exit 2
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "Install the pinned uv launcher before bootstrap." >&2
  exit 2
fi
"${REPO_ROOT}/scripts/linux/verify_uv_version.sh" "$(command -v uv)"

mkdir -p "${REPO_ROOT}/.tools" "${UV_CACHE_DIR}"
if [[ -e "${PYTHON_HOME}" ]]; then
  if [[ ! -d "${PYTHON_HOME}" ]] || \
    ! "${REPO_ROOT}/scripts/linux/verify_python_version.sh" "${PYTHON_EXECUTABLE}"; then
    echo "Protected Python ${PYTHON_VERSION} path exists but is not a compatible runtime; refusing to overwrite it." >&2
    exit 3
  fi
else
  uv python install --no-bin "${PYTHON_VERSION}"
  if ! "${REPO_ROOT}/scripts/linux/verify_python_version.sh" "${PYTHON_EXECUTABLE}"; then
    echo "Pinned Python ${PYTHON_VERSION} installation could not be verified." >&2
    exit 3
  fi
fi

if [[ -e "${NODE_HOME}" ]] || [[ -L "${NODE_HOME}" ]]; then
  if ! "${REPO_ROOT}/scripts/linux/verify_node_toolchain.sh" "${NODE_HOME}"; then
    echo "Protected Node ${NODE_VERSION} path is incomplete or incompatible; refusing to overwrite it." >&2
    exit 3
  fi
else
  readonly TEMP_DIR="$(mktemp -d)"
  readonly TEMP_ARCHIVE="${TEMP_DIR}/${NODE_ARCHIVE}"
  readonly EXTRACTED_NODE_HOME="${TEMP_DIR}/node-v${NODE_VERSION}-linux-x64"
  trap 'rm -rf -- "${TEMP_DIR}"' EXIT
  curl --fail --location --proto '=https' --tlsv1.2 \
    "https://nodejs.org/download/release/v${NODE_VERSION}/${NODE_ARCHIVE}" \
    --output "${TEMP_ARCHIVE}"
  readonly ACTUAL_SHA256="$(sha256sum "${TEMP_ARCHIVE}" | cut -d ' ' -f 1)"
  if [[ "${ACTUAL_SHA256}" != "${NODE_SHA256}" ]]; then
    echo "Node archive checksum mismatch; refusing installation." >&2
    exit 3
  fi
  tar -xJf "${TEMP_ARCHIVE}" -C "${TEMP_DIR}"
  if ! "${REPO_ROOT}/scripts/linux/verify_node_toolchain.sh" "${EXTRACTED_NODE_HOME}"; then
    echo "Pinned Node archive does not contain the expected runtime and npm CLI." >&2
    exit 3
  fi
  if [[ -e "${NODE_HOME}" ]] || [[ -L "${NODE_HOME}" ]]; then
    echo "Protected Node path appeared during installation; refusing to overwrite it." >&2
    exit 3
  fi
  mv --no-clobber --no-target-directory -- "${EXTRACTED_NODE_HOME}" "${NODE_HOME}"
  if [[ -e "${EXTRACTED_NODE_HOME}" ]]; then
    echo "Protected Node path appeared during installation; no file was overwritten." >&2
    exit 3
  fi
fi

if [[ ! -x "${PNPM_HOME}/bin/pnpm" ]] || [[ "$(pnpm --version 2>/dev/null || true)" != "11.15.1" ]]; then
  "${NODE_HOME}/bin/node" \
    "${NODE_HOME}/lib/node_modules/npm/bin/npm-cli.js" \
    install --global --prefix "${PNPM_HOME}" --cache "${REPO_ROOT}/.tools/npm-cache" \
    pnpm@11.15.1
fi

if [[ "$(node --version)" != "v${NODE_VERSION}" ]]; then
  echo "Pinned Node ${NODE_VERSION} is not active." >&2
  exit 3
fi
if [[ "$(pnpm --version)" != "11.15.1" ]]; then
  echo "Pinned pnpm 11.15.1 is not active." >&2
  exit 3
fi

uv sync --frozen --python "${PYTHON_VERSION}"
pnpm install --frozen-lockfile

echo "Pensae Signal bootstrap ready: Python ${PYTHON_VERSION}, Node ${NODE_VERSION}, pnpm 11.15.1."
