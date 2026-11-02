#!/usr/bin/env bash
# `make check`'s path check: fails, listing them, when tracked files name a home path (/home/<name>/...,
# /Users/<name>/..., ~<name>/...) or the folder proj in a home (after ~, $HOME, ${HOME}, "$HOME" or /root; with or
# without a slash after it). A machine's own paths belong in its local settings (the development workspace,
# scripts/env.local.sh), never in the repository.
#
# Allowed: the neutral name `user` (/home/user/..., /Users/user/..., ~user/...), which tests and examples use; and a
# line holding the marker "path-check: allow" (in a comment), for an example that needs another such path.
# Not checked: binary files.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git rev-parse --is-inside-work-tree >/dev/null   # it reads Git's list of tracked files

# POSIX extended regular expressions (git grep and awk). The bracket expressions ([$], [{], [j]) keep this file from
# matching itself.
home='/(home|Users)/+[A-Za-z0-9._-]+'                                 # /home/<name>, /Users/<name>
tilde='(^|[^A-Za-z0-9_~])~[A-Za-z_][A-Za-z0-9._-]*/'                  # ~<name>/
projects='(~|[$]HOME|[$][{]HOME[}]|/root)"?/pro[j]([^A-Za-z0-9_-]|$)'  # the folder proj in a home

found="$(git grep -nIE "$home|$tilde|$projects" |
  HOME_RE="$home" TILDE_RE="$tilde" PROJECTS_RE="$projects" awk '
  # 1 when `text` names a home through `pattern` with a name other than "user" (the name: the match less `head`
  # and `tail`).
  function named(text, pattern, head, tail,    name) {
    while (match(text, pattern)) {
      name = substr(text, RSTART, RLENGTH); sub(head, "", name); sub(tail, "", name)
      if (name != "user") return 1
      text = substr(text, RSTART + RLENGTH)
    }
    return 0
  }
  /path-check: allow/ { next }
  {
    text = $0; sub(/^[^:]*:[0-9]+:/, "", text)   # the line, without the file and line number git grep adds
    if (text ~ ENVIRON["PROJECTS_RE"] || named(text, ENVIRON["HOME_RE"], "^/(home|Users)/+", "") ||
        named(text, ENVIRON["TILDE_RE"], "^[^~]*~", "/$")) print
  }' || true)"

if [ -n "$found" ]; then
  echo "check-paths: machine paths in tracked files (use /home/user/..., or mark the line \"path-check: allow\"):" >&2
  echo "$found" >&2
  exit 1
fi
echo "check-paths: no machine paths"
