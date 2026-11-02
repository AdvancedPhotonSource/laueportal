#!/usr/bin/env bash
# Add shadcn/ui components with the pinned CLI, then keep them on our approved dependencies.
#   usage (from frontend/, after `. ../scripts/env.sh`): scripts/shadcn-add.sh button card ...
# Review the result: sonner.tsx must take the theme from our ThemeProvider (useTheme from
# src/app/theme-context.ts), not from next-themes. The script fails if anything still imports a
# removed package.
set -euo pipefail
cd "$(dirname "$0")/.."

SHADCN_VERSION=4.21.4 # at least 7 days old when pinned

# pnpm's cache (including `pnpm dlx` installs) stays in the scratch space, like the store (env.sh).
: "${LAUE_DEV_WORKSPACE:?source scripts/env.sh first}"
export pnpm_config_cache_dir="${pnpm_config_cache_dir:-$LAUE_DEV_WORKSPACE/pnpm-cache}"

# The CLI installs packages with pnpm; keep the 7-day release age for its own install too.
pnpm_config_minimum_release_age=10080 pnpm dlx "shadcn@$SHADCN_VERSION" add --yes "$@"

# shadcn 4.21 imports cn() from the `cn` package (not approved). Ours lives in src/lib/utils.ts
# (clsx + tailwind-merge).
{ grep -rlE --include='*.ts' --include='*.tsx' "from ['\"]cn['\"]" src || true; } \
  | xargs -r sed -i -E "s#from ['\"]cn['\"]#from \"@/lib/utils\"#"

# Drop what the CLI adds that is not on the approved list (next-themes: see src/app/ThemeProvider.tsx).
REMOVED=(cn shadcn next-themes @fontsource-variable/inter)
for pkg in "${REMOVED[@]}"; do
  if grep -q "\"$pkg\":" package.json; then pnpm remove "$pkg"; fi
done
pnpm exec prettier --write --log-level warn src/components/ui

# A re-added component can still import a removed package (e.g. sonner.tsx and next-themes after
# --overwrite). Fail loudly here rather than leave an import that only tsc or the build catches.
pattern="$(printf '%s|' "${REMOVED[@]}" | sed 's/|$//; s/\./\\./g')"
if stale="$(grep -rnE --include='*.ts' --include='*.tsx' --include='*.css' \
  "(from|import) +['\"]($pattern)(/[^'\"]*)?['\"]" src)"; then
  echo "error: these files import packages that are not approved:" >&2
  echo "$stale" >&2
  echo "Restore our version (e.g. git checkout -- src/components/ui/sonner.tsx) or port the change." >&2
  exit 1
fi
