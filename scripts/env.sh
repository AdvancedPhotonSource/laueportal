# Environment for every command in this repo. Sourced by all make targets:
#   . scripts/env.sh
# Works from the main checkout and from worktrees: REPO_ROOT is this checkout.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export REPO_ROOT

# The main checkout: a worktree's is the checkout it was added from, whose workspace and local file it shares.
MAIN_ROOT="$REPO_ROOT"
if _git_common="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" &&
  [ "$(basename "$_git_common")" = .git ] &&
  [ "$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null)" = "$(cd "$REPO_ROOT" && pwd -P)" ]; then
  MAIN_ROOT="$(dirname "$_git_common")"
fi
unset _git_common

# This machine's settings: the main checkout's scripts/env.local.sh (ignored by Git), sourced when present, whatever
# else is exported (such as LAUEPORTAL_DATABASE__PROTECTED, which then holds under any LAUEPORTAL_CONFIG).
# The development workspace (never committed): the env, tools, config, databases, runs, logs and TMPDIR.
# An exported LAUE_DEV_WORKSPACE wins; else the local file may set it; else it is .workspace in the main checkout
# (ignored by Git).
_exported_workspace="${LAUE_DEV_WORKSPACE:-}"
if [ -f "$MAIN_ROOT/scripts/env.local.sh" ]; then
  # shellcheck disable=SC1091
  . "$MAIN_ROOT/scripts/env.local.sh"
fi
export LAUE_DEV_WORKSPACE="${_exported_workspace:-${LAUE_DEV_WORKSPACE:-$MAIN_ROOT/.workspace}}"
unset _exported_workspace
export TMPDIR="$LAUE_DEV_WORKSPACE/tmp"
export MAMBA_ROOT_PREFIX="$LAUE_DEV_WORKSPACE/mamba"
export PATH="$LAUE_DEV_WORKSPACE/env/bin:$LAUE_DEV_WORKSPACE/tools:$PATH"

# Import the backend from this checkout (not from whichever checkout was installed
# editable), so worktrees test their own code.
export PYTHONPATH="$REPO_ROOT/backend/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

# Tool caches stay in the scratch space.
export pnpm_config_store_dir="$LAUE_DEV_WORKSPACE/pnpm-store"
export npm_config_store_dir="$pnpm_config_store_dir"   # older pnpm spelling
export pnpm_config_cache_dir="$LAUE_DEV_WORKSPACE/pnpm-cache"
export PYRIGHT_PYTHON_CACHE_DIR="$LAUE_DEV_WORKSPACE/tools/pyright-cache"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"

export LAUEPORTAL_CONFIG="${LAUEPORTAL_CONFIG:-$LAUE_DEV_WORKSPACE/config/dev.toml}"
