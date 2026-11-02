# Settings overrides of the dev stack (deploy/supervisor/dev.conf), sourced by scripts/stack.sh after
# scripts/env.sh, before it seeds or starts supervisord. Settings come from $LAUEPORTAL_CONFIG
# (the workspace's config/dev.toml); these LAUEPORTAL_* variables replace single keys.

# Serve this checkout's SPA (frontend/dist, `make build`), whichever checkout dev.toml names: a worktree's
# stack runs its own backend (PYTHONPATH, scripts/env.sh) and its own frontend.
export LAUEPORTAL_APP__STATIC_DIR="$REPO_ROOT/frontend/dist"
