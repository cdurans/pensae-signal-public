#!/usr/bin/bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly UV_CACHE_DIR="${REPO_ROOT}/.uv-cache"
readonly UV_PYTHON_INSTALL_DIR="${REPO_ROOT}/.tools/python"
readonly NODE_HOME="${REPO_ROOT}/.tools/node-v24.18.0-linux-x64"
readonly PNPM_BIN="${REPO_ROOT}/.tools/pnpm/bin"

export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR
export PATH="${NODE_HOME}/bin:${PNPM_BIN}:/usr/local/bin:/usr/bin:/bin"
export PLAYWRIGHT_BASE_URL="http://127.0.0.1:8123"
export TMPDIR=/tmp TMP=/tmp TEMP=/tmp

cd "${REPO_ROOT}"
if [[ "${PENSAE_SKIP_FRONTEND_BUILD:-0}" != "1" ]]; then
  pnpm --dir frontend build
fi
setsid uv run --frozen --offline --python 3.13.14 uvicorn tests.browser.fake_server:app \
  --host 127.0.0.1 --port 8123 --no-access-log &
readonly server_pid=$!

cleanup() {
  kill -- "-${server_pid}" 2>/dev/null || true
  for _cleanup_attempt in {1..20}; do
    if ! kill -0 "${server_pid}" 2>/dev/null; then
      wait "${server_pid}" 2>/dev/null || true
      return
    fi
    sleep 0.1
  done
  kill -KILL -- "-${server_pid}" 2>/dev/null || true
  wait "${server_pid}" 2>/dev/null || true
}
trap cleanup EXIT

for _attempt in {1..100}; do
  if uv run --frozen --offline --python 3.13.14 python -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8123/api/bootstrap", timeout=1)' \
    >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

if [[ "${PENSAE_BROWSER_RELEASE_ONLY:-0}" == "1" ]]; then
  timeout --foreground --signal=TERM --kill-after=5s 120s \
    pnpm --dir frontend exec playwright test --config playwright.config.ts \
      run_control.spec.ts p7_five_opportunity.spec.ts \
      --grep "start streams bounded progress|stop after an earlier commit|P7 commits five sequential|P7 reports an honest shortfall"
else
  timeout --foreground --signal=TERM --kill-after=5s 120s \
    pnpm --dir frontend exec playwright test --config playwright.config.ts
fi
