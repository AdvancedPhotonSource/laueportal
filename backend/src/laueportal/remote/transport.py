"""Run shell commands through SSH even when the remote login shell is tcsh.

The shell is never a login shell (``bash -c``), and no parameter asks for one, so
no DM call can get a login shell (a login profile's banner would corrupt the JSON a DM call prints). Standard
library only (import-linter rule 3).
"""

from __future__ import annotations

import base64
import shlex


def shell_command(script: str, host: str | None = None) -> list[str]:
    if not host:
        return ["/bin/bash", "-c", script]
    encoded = base64.b64encode(script.encode()).decode("ascii")
    # Only an alphanumeric base64 token reaches the login shell. JSON stdin is
    # preserved for the Python API instead of being consumed as a shell script.
    wrapper = f'exec /bin/bash -c "$(printf %s {encoded} | base64 -d)"'
    return ["ssh", "-T", "-o", "BatchMode=yes", host, "/bin/bash -c " + shlex.quote(wrapper)]
