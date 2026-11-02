#!/usr/bin/env bash
# The state of frontend/: a sha256 of the name and content of every file there that Git tracks or would track (not
# ignored: node_modules, dist and the reports are left out), and of the names of the tracked files deleted from the
# working tree. `make check-frontend` keeps ESLint's results under it, and CI's cache key contains it: ESLint's
# type-aware rules read the types of other files, so a result is reused only for the same files, configs and lock.
#
#   scripts/frontend-digest.sh
set -euo pipefail
cd "$(dirname "$0")/.."
{
  git ls-files -z --deleted -- frontend | xargs -0 -r printf 'deleted  %s\n'
  git ls-files -z --cached --others --exclude-standard -- frontend |
    while IFS= read -r -d '' path; do
      if [ -f "$path" ]; then printf '%s\0' "$path"; fi
    done |
    xargs -0 -r sha256sum --
} | sha256sum | cut -d ' ' -f 1
