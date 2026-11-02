#!/bin/bash
# The launcher's DM stage command: the DM workflows (workflow-bdp*.json, rendered by render-workflows.py) run it as
# `dm-stage.sh submit|monitor|permissions|check|cancel|cleanup <uuid>`; `dm-stage.sh auth` prepares the Globus
# authorization. It runs `python -m laueportal.remote.stage` from the portal's checkout installed at <root>/src (this
# script is <root>/src/deploy/remote/dm-stage.sh), with the launcher's Python <root>/envs/laue-portal and the private
# settings <root>/config/bdp.yaml. LAUE_PORTAL_ROOT, LAUE_PORTAL_SOURCE, LAUE_PORTAL_PYTHON and LAUE_BDP_CONFIG override
# them (the launcher installation's names; the portal's own settings are LAUEPORTAL_CONFIG, which the launcher never
# reads).
set -euo pipefail
laue_root="${LAUE_PORTAL_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)}"
laue_source="${LAUE_PORTAL_SOURCE:-$laue_root/src}"
laue_python="${LAUE_PORTAL_PYTHON:-$laue_root/envs/laue-portal/bin/python}"
export PYTHONNOUSERSITE=1
export PYTHONPATH="$laue_source/backend/src"
cd "$laue_source"
exec "$laue_python" -m laueportal.remote.stage \
    --config "${LAUE_BDP_CONFIG:-$laue_root/config/bdp.yaml}" "$@"
