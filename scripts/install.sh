#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3}"

if command -v pipx >/dev/null 2>&1; then
  pipx install reidcli
else
  "$python_bin" -m pip install --user reidcli
fi

echo "reidcli installed. Run: reidcli"
