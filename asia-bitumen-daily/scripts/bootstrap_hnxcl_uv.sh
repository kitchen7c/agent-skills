#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQUIREMENTS_FILE="${SCRIPT_DIR}/requirements-hnxcl.txt"

cd "${PROJECT_ROOT}"

if [[ ! -f "${REQUIREMENTS_FILE}" ]]; then
  echo "Missing dependency manifest: ${REQUIREMENTS_FILE}" >&2
  exit 1
fi

export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"
mkdir -p "${UV_CACHE_DIR}"

exec uv run \
  --python 3.11 \
  --with-requirements "${REQUIREMENTS_FILE}" \
  python -m playwright install chromium
