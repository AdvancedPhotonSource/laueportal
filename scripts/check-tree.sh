#!/usr/bin/env bash
# `make check-hygiene`'s Git checks: no whitespace errors in the changes since a base (`git diff --check`: the changes
# to tracked files, committed, staged or not; untracked files are not checked), and no generated files tracked (build
# output, node_modules, bytecode, tool caches, a development workspace, test reports).
#
#   scripts/check-tree.sh [<base>]
#
# The base is a commit: CI gives the pull request's base or the commit before a push. Without one, it is the commit
# where HEAD left main (main, else origin/main). A base that is not a commit here (a push that created the branch has
# 0000000; a force push can leave the earlier commit unfetched) has no diff to check: the whitespace check is skipped,
# with a line that says so. .gitattributes turns the whitespace check off for the generated API client.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

base="${1:-}"
if [ -z "$base" ]; then
  for ref in main origin/main; do
    if git rev-parse --verify -q "$ref^{commit}" >/dev/null; then
      base="$(git merge-base HEAD "$ref")"
      break
    fi
  done
  [ -n "$base" ] || { echo "check-tree: no main or origin/main to compare with: give a base commit" >&2; exit 2; }
fi

status=0
if git rev-parse --verify -q "$base^{commit}" >/dev/null; then
  if git diff --check "$base"; then
    echo "check-tree: no whitespace errors since ${base:0:12}"
  else
    echo "check-tree: whitespace errors since ${base:0:12} (above)" >&2
    status=1
  fi
else
  echo "check-tree: the base ${base:0:12} is not a commit here: the whitespace check is skipped"
fi

# Each pattern matches generated files that Git ignores (.gitignore, or the .gitignore that mypy writes in its cache);
# a tracked match was added by force or before the rule.
generated='(^|/)(node_modules|__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache|\.vite|dist|build|test-results|playwright-report|blob-report)/|(^|/)[^/]+\.egg-info/|\.(py[cod]|tsbuildinfo)$|^\.workspace(/|$)'
if found="$(git ls-files | grep -E "$generated")"; then
  echo "check-tree: generated files are tracked:" >&2
  echo "$found" >&2
  status=1
else
  echo "check-tree: no generated files tracked"
fi
exit $status
