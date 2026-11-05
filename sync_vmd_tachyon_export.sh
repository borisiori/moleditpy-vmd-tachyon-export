#!/usr/bin/env bash
# Sync the VMD-Tachyon Export plugin into the local MoleditPy plugins folder.
# (A symlink would not be discovered: plugin discovery walks without following links.)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

# VMD-Tachyon Export (single-file plugin)
install -m 0644 \
  "${ROOT}/VMD-tachyon_export.py" \
  "${HOME}/.moleditpy/plugins/VMD-tachyon_export.py"

echo "Synced ${ROOT}/VMD-tachyon_export.py -> ${HOME}/.moleditpy/plugins/VMD-tachyon_export.py"
