#!/usr/bin/env bash
# Before `pnpm install` (make setup, step 7): refuse to re-link a checkout's node_modules to another pnpm store.
#
#   scripts/pnpm-store-check.sh <frontend dir>
#
# pnpm hard-links node_modules from its store, and records the store in node_modules/.modules.yaml
# (storeDir). scripts/env.sh puts the store in the workspace ($LAUE_DEV_WORKSPACE/pnpm-store). With
# LAUE_DEV_WORKSPACE pointing at another workspace, `pnpm install` would purge node_modules and re-link it from that
# workspace's store, tying this checkout to it (and leaving it broken once that workspace is wiped). So
# when node_modules was installed with another store this exits 1 and says so, unless
# SETUP_ALLOW_STORE_SWITCH=1. Exit 0: no node_modules yet, the same store, or the switch allowed.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

[ $# -eq 1 ] || { echo "usage: scripts/pnpm-store-check.sh <frontend dir>" >&2; exit 2; }
modules="$1/node_modules/.modules.yaml"
[ -f "$modules" ] || exit 0

# .modules.yaml is JSON today ("storeDir": "..."); plain YAML (storeDir: ...) is read too.
installed="$(sed -n 's/^[[:space:]]*"\{0,1\}storeDir"\{0,1\}:[[:space:]]*"\{0,1\}\([^",]*\)"\{0,1\},\{0,1\}[[:space:]]*$/\1/p' \
  "$modules" | head -1)"
[ -n "$installed" ] || exit 0

store="$(realpath -m "$pnpm_config_store_dir")"
case "$(realpath -m "$installed")" in
  "$store" | "$store"/*) exit 0 ;;   # pnpm records the store's versioned subdirectory (e.g. v11)
esac

if [ "${SETUP_ALLOW_STORE_SWITCH:-0}" = 1 ]; then
  echo "    SETUP_ALLOW_STORE_SWITCH=1: re-linking $1/node_modules from $store (it was installed from $installed)"
  exit 0
fi
cat >&2 <<EOF
pnpm-store-check: $1/node_modules was installed from the pnpm store
  $installed
but LAUE_DEV_WORKSPACE ($LAUE_DEV_WORKSPACE) uses the store
  $store
pnpm install would purge node_modules and re-link it from that store, tying this checkout to that workspace.
For another workspace, use a separate worktree or clone (docs/runbooks/dev-setup.md, "Another workspace").
To re-link this checkout on purpose, set SETUP_ALLOW_STORE_SWITCH=1.
EOF
exit 1
