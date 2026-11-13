#!/usr/bin/env bash
# `make setup`: build the development environment. Idempotent: each step
# checks whether its result is already in place and skips if so.
#
#   scripts/setup.sh [backend] [frontend]     # no part: both
#
# The backend part (`make setup-backend`) is steps 2-5, 8 and 9: the conda env, the locked Python packages,
# lauelab, the backend, the smoke test and the dev config. The frontend part (`make setup-frontend`) is steps 6
# and 7: pnpm and node_modules. Each CI job installs only the parts that its checks use (.github/actions/setup).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"
cd "$REPO_ROOT"

BACKEND=0
FRONTEND=0
[ $# -gt 0 ] || set -- backend frontend
for part in "$@"; do
  case "$part" in
    backend) BACKEND=1 ;;
    frontend) FRONTEND=1 ;;
    *) echo "usage: scripts/setup.sh [backend] [frontend]" >&2; exit 2 ;;
  esac
done

# Pins (bump deliberately).
LAUELAB_COMMIT=b846009a9628d9dfea2fd531eb5e6607d9b8094b
LAUELAB_URL="git+https://github.com/AdvancedPhotonSource/lauelab.git@$LAUELAB_COMMIT"
PNPM_VERSION=12.10.1
PNPM_SHA256=502a275984a4cd01e5579ac72049a241091cac5118e44becf84951130c3e079e   # pnpm-linux-x64.tar.gz
MICROMAMBA_VERSION=2.9.0-0
MICROMAMBA_SHA256=366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3   # micromamba-linux-64 (release asset digest)

ENV="$LAUE_DEV_WORKSPACE/env"
PY="$ENV/bin/python"
TOOLS="$LAUE_DEV_WORKSPACE/tools"
MM="$TOOLS/micromamba"
EXCLUDE_NEWER=""
[ -f requirements/dev.lock ] && EXCLUDE_NEWER="$(sed -n 's/^# exclude-newer: //p' requirements/dev.lock | head -1)"

step() { printf '\n==> %s\n' "$*"; }
skip() { printf '    (already done: %s)\n' "$*"; }

# 1. Scratch directories -------------------------------------------------------
step "workspace directories in $LAUE_DEV_WORKSPACE"
mkdir -p "$LAUE_DEV_WORKSPACE"/{env,mamba,tools,config,db,runs,logs,tmp,worktrees}
chmod 700 "$LAUE_DEV_WORKSPACE/config"

# The backend part: steps 2-5.
if [ "$BACKEND" = 1 ]; then
  # 2. micromamba + conda env ----------------------------------------------------
  step "micromamba $MICROMAMBA_VERSION"
  is_pinned_micromamba() { echo "$MICROMAMBA_SHA256  $1" | sha256sum -c --quiet >/dev/null 2>&1; }
  if [ -x "$MM" ] && is_pinned_micromamba "$MM"; then
    skip "$("$MM" --version)"
  else
    rm -f "$MM.tmp"
    curl -fsSL -o "$MM.tmp" \
      "https://github.com/mamba-org/micromamba-releases/releases/download/$MICROMAMBA_VERSION/micromamba-linux-64"
    echo "$MICROMAMBA_SHA256  $MM.tmp" | sha256sum -c --quiet || {
      rm -f "$MM.tmp"
      echo "micromamba-linux-64 $MICROMAMBA_VERSION does not match MICROMAMBA_SHA256" >&2
      exit 1
    }
    chmod +x "$MM.tmp" && mv "$MM.tmp" "$MM"
    echo "    micromamba $("$MM" --version)"
  fi

  # environment.lock (explicit URLs + md5, from `make lock-env`) makes the env reproducible;
  # environment.yml is only used to create it.
  ENV_SPEC=environment.lock
  [ -f "$ENV_SPEC" ] || ENV_SPEC=environment.yml
  step "conda env at $ENV ($ENV_SPEC)"
  ENV_STAMP="$ENV/.env-spec.sha256"
  want="$(sha256sum "$ENV_SPEC" | cut -d' ' -f1)"
  if [ -x "$PY" ] && [ "$(cat "$ENV_STAMP" 2>/dev/null)" = "$want" ]; then
    skip "$ENV_SPEC unchanged"
  else
    if [ -e "$ENV/conda-meta" ]; then
      echo "    $ENV_SPEC changed: recreating the env"
      rm -rf "$ENV"
    fi
    if [ "$ENV_SPEC" = environment.lock ]; then
      "$MM" create -y -q -p "$ENV" --file environment.lock
    else
      "$MM" create -y -q -p "$ENV" -f environment.yml
    fi
    echo "$want" > "$ENV_STAMP"
  fi

  # 3. Locked Python packages ------------------------------------------------------
  step "Python packages from requirements/dev.lock"
  if [ ! -f requirements/dev.lock ]; then
    echo "requirements/dev.lock is missing: run 'make lock' first" >&2
    exit 1
  fi
  # The lock is complete except numpy, h5py, cffi and pycparser (conda's cffi brings it), which stay
  # conda's (constrained at compile time, left out of the lock; CONDA_PY in the Makefile). So install
  # exactly the lock with --no-deps, then check consistency.
  # micromamba run activates the env: laue-index compiles its C indexer here, with the conda compilers. CPU only.
  LAUEMATCHING_CUDA=0 "$MM" run -p "$ENV" uv pip install -q --python "$PY" --require-hashes --no-deps -r requirements/dev.lock
  uv pip check -q --python "$PY"
  "$PY" - <<'EOF'
import importlib.metadata as md, json, pathlib, sys
env = pathlib.Path(sys.prefix)
bad = []
for name in ("numpy", "h5py", "cffi", "pycparser"):
    dist = md.distribution(name)
    installer = (dist.read_text("INSTALLER") or "").strip()
    conda = list((env / "conda-meta").glob(f"{name}-[0-9]*.json"))
    conda_ver = json.loads(conda[0].read_text())["version"] if conda else None
    if conda_ver != dist.version:
        bad.append(f"{name}: pip sees {dist.version} ({installer}), conda has {conda_ver}")
if bad:
    sys.exit("conda and pip disagree:\n  " + "\n  ".join(bad))
print("    numpy/h5py/cffi/pycparser: pip and conda agree")
EOF

  # 4. lauelab from GitHub -----------------------------------------------------------
  step "lauelab @ ${LAUELAB_COMMIT:0:7}"
  have_lauelab="$("$PY" - <<'EOF' 2>/dev/null || true
import importlib.metadata as md, json
d = md.distribution("lauelab")
print(json.loads(d.read_text("direct_url.json") or "{}").get("vcs_info", {}).get("commit_id", ""))
EOF
  )"
  if [ "$have_lauelab" = "$LAUELAB_COMMIT" ]; then
    skip "lauelab installed at the pinned commit"
  else
    # micromamba run activates the env, so the conda compilers and libraries are used.
    UV_EXCLUDE_NEWER="${EXCLUDE_NEWER:-}" "$MM" run -p "$ENV" \
      uv pip install --python "$PY" --no-deps --reinstall-package lauelab "lauelab @ $LAUELAB_URL"
  fi
  # The remote runners merge multi-node results with lauelab.indexing's merge APIs (cd9318b); live runs grow a
  # scan point by point with the incremental PreparedScan (d5de751).
  "$PY" -c "import lauelab, lauelab.visualization
from lauelab.indexing import merge_results_files, merge_xml_files, read_frame
from lauelab.reconstruct import PreparedScan
assert all(hasattr(PreparedScan, name) for name in ('add', 'outcomes', 'export_state', 'restore')), 'no incremental PreparedScan'
print('    lauelab imports OK (with the merge and live APIs)')"

  # 5. Backend (editable) ------------------------------------------------------------
  step "backend (editable)"
  if "$PY" -c "import importlib.metadata as md; md.distribution('laueportal')" 2>/dev/null; then
    skip "laueportal installed"
  else
    UV_EXCLUDE_NEWER="${EXCLUDE_NEWER:-}" uv pip install -q --python "$PY" --no-deps -e backend
  fi
fi

# The frontend part: steps 6 and 7.
if [ "$FRONTEND" = 1 ]; then
  # 6. pnpm (standalone binary) --------------------------------------------------------
  step "pnpm $PNPM_VERSION"
  if [ -x "$TOOLS/pnpm" ] && [ "$("$TOOLS/pnpm" --version 2>/dev/null)" = "$PNPM_VERSION" ]; then
    skip "pnpm $PNPM_VERSION"
  else
    dl="$TMPDIR/pnpm-$PNPM_VERSION"; rm -rf "$dl"; mkdir -p "$dl"
    curl -fsSL -o "$dl/pnpm.tar.gz" \
      "https://github.com/pnpm/pnpm/releases/download/v$PNPM_VERSION/pnpm-linux-x64.tar.gz"
    echo "$PNPM_SHA256  $dl/pnpm.tar.gz" | sha256sum -c --quiet
    tar -xzf "$dl/pnpm.tar.gz" -C "$dl"
    bin="$(find "$dl" -type f -name pnpm -perm -u+x | head -1)"
    [ -n "$bin" ] || { echo "pnpm binary not found in the release archive" >&2; exit 1; }
    # The archive may ship support files beside the binary; keep them together.
    rm -rf "$TOOLS/pnpm-dist"; mv "$(dirname "$bin")" "$TOOLS/pnpm-dist"
    ln -sfn "pnpm-dist/pnpm" "$TOOLS/pnpm"
    rm -rf "$dl"
    echo "    pnpm $("$TOOLS/pnpm" --version)"
  fi

  # 7. Frontend dependencies -------------------------------------------------------------
  step "frontend dependencies"
  if [ -f frontend/pnpm-lock.yaml ]; then
    # Refuses when node_modules was installed from another workspace's pnpm store (SETUP_ALLOW_STORE_SWITCH=1).
    scripts/pnpm-store-check.sh frontend
    pnpm -C frontend install --frozen-lockfile
  else
    skip "no frontend/pnpm-lock.yaml yet"
  fi
fi

# The backend part: steps 8 and 9.
if [ "$BACKEND" = 1 ]; then
  # 8. Sample data -------------------------------------------------------------------------
  # Every development input is synthetic: the committed fixtures (backend/tests/fixtures/lauelab) and what
  # `laueportal seed demo` makes from them (laueportal.science.synthetic). Nothing to copy; native lauelab is checked.
  step "native lauelab on the committed synthetic frames"
  "$PY" scripts/lauelab_smoke.py backend/tests/fixtures/lauelab

  # 9. Dev config ---------------------------------------------------------------------------
  # The template's paths are relative to {{WORKSPACE}} (this workspace) and {{CHECKOUT}} (this checkout).
  step "dev config in $LAUE_DEV_WORKSPACE/config"
  if [ -f "$LAUE_DEV_WORKSPACE/config/dev.toml" ]; then
    skip "dev.toml exists (not overwritten)"
  else
    "$PY" scripts/render-dev-config.py "$LAUE_DEV_WORKSPACE" "$REPO_ROOT" "$LAUE_DEV_WORKSPACE/config/dev.toml"
    echo "    wrote dev.toml"
  fi
fi

step "done"
