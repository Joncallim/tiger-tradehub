#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_SERVER=1

usage() {
  cat <<'EOF'
Usage: ./setup.sh [--no-run]

Creates/updates .venv, installs Tiger TradeHub with MCP support, and starts the local setup UI.

Options:
  --no-run   Install dependencies but do not start TradeHub.
  -h, --help Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-run)
      RUN_SERVER=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "python3 was not found. Install Python 3.10+ and run ./setup.sh again." >&2
  exit 1
fi

cd "${ROOT_DIR}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating virtual environment in .venv"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_TRADEHUB="${VENV_DIR}/bin/tradehub"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Virtual environment is missing ${VENV_PYTHON}" >&2
  echo "Remove .venv and run ./setup.sh again." >&2
  exit 1
fi

echo "Installing Tiger TradeHub with MCP support"
"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install -e ".[mcp]"

cat <<'EOF'

Setup install complete.

Next page:
  http://127.0.0.1:8787/setup

Use that page to save .env, generate the local API token, add Tiger credentials, and write MCP config.
EOF

if [[ "${RUN_SERVER}" -eq 0 ]]; then
  exit 0
fi

echo
echo "Starting TradeHub. Keep this terminal open while testing."
exec "${VENV_TRADEHUB}"
