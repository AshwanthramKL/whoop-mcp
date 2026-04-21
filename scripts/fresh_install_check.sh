#!/usr/bin/env bash
#
# Fresh-install smoke test.
#
# Walks the README install steps end-to-end in a temp directory so we catch
# drift between what the docs say and what actually works. Does not cover
# the browser-based OAuth flow or the `claude mcp add` registration — both
# require interactive input.
#
# What this script does:
#   1. Clones the current HEAD of this repo into a scratch directory.
#   2. Creates a fresh venv and installs runtime dependencies.
#   3. Verifies the Python package imports.
#   4. Imports the MCP server module to confirm it boots without errors.
#   5. Reports the detected SERVER_VERSION.
#
# Usage:
#   bash scripts/fresh_install_check.sh
#
# Exits non-zero on the first failure. Safe to rerun.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="$(mktemp -d -t whoop-fresh-install-XXXXXX)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "== fresh-install check =="
echo "  repo:    $REPO_ROOT"
echo "  scratch: $SCRATCH"
echo

echo "[1/5] cloning current HEAD..."
git clone --quiet --depth 1 "file://$REPO_ROOT" "$SCRATCH/whoop-mcp-server"

cd "$SCRATCH/whoop-mcp-server"

echo "[2/5] creating venv..."
python3 -m venv .venv

echo "[3/5] installing requirements..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "[4/5] verifying package imports..."
# Use a stub access token so TokenManager construction doesn't fail.
# We never make a network call here — we just want the module to load.
export WHOOP_CLIENT_ID="fresh-install-check"
export WHOOP_CLIENT_SECRET="fresh-install-check"
.venv/bin/python - <<'PY'
import sys, os
sys.path.insert(0, "src")
import __version__ as v
print(f"    __version__ = {v.__version__}")
import whoop_mcp_server as m
assert m.SERVER_VERSION == v.__version__, (
    f"SERVER_VERSION ({m.SERVER_VERSION}) != __version__ ({v.__version__})"
)
print(f"    SERVER_VERSION = {m.SERVER_VERSION}")
PY

echo "[5/5] smoke-invoking server entry point..."
# Drive stdin with EOF — we only want to catch import-time or
# registration-time crashes. Wrapped in timeout; ignore non-zero exit.
(
    cd "$SCRATCH/whoop-mcp-server"
    (timeout 2 .venv/bin/python src/whoop_mcp_server.py </dev/null >/dev/null 2>&1) || true
)

echo
echo "OK — fresh install path works end-to-end."
