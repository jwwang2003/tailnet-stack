#!/usr/bin/env bash
set -euo pipefail
integration_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$integration_root"
for suite in tests/*/; do
    python3 -m unittest discover -s "$suite" -v
done
for script in scripts/*.sh; do bash -n "$script"; done
git diff --check
