"""The acquisition hook's forced command (``docs/runbooks/deploy.md``, "The acquisition hook's
key"): the one command a dedicated SSH key in the portal account's ``authorized_keys`` runs, its ``command="..."``::

    <ROOT>/envs/laue-portal/bin/python -I -m laueportal.jobs.notify_hook <ROOT>/config/prod.toml

sshd hands it the command the hook asked for in ``SSH_ORIGINAL_COMMAND``. It allows exactly
``laueportal live notify <job id> <path> --complete``, words separated by one space (what ``ssh`` sends for
``ssh <host> laueportal live notify <job id> <path> --complete``): a job ID of digits, and one absolute path of
``PATH_CHARACTERS`` alone (no shell metacharacter, space or control character), with no ``.`` or ``..`` component and
at most ``MAX_PATH_BYTES``. It then replaces itself (``os.execve``: no shell) with the env's ``laueportal``, beside
its Python, in an environment of its own (the settings file it was given, the env's ``PATH``, ``HOME``, and
``PYTHONNOUSERSITE=1``; nothing else of the session's), so notify's output and exit status are the hook's. ``HOME``
is the portal account's, kept for what looks a home up (``~`` in a path); ``PYTHONNOUSERSITE`` keeps that home's
user site (``~/.local/lib/python3.X/site-packages``, its ``.pth`` files) out of notify, whose ``laueportal`` script
runs a plain ``python``, not this module's ``-I``. Anything else is refused with one line on
stderr and status 2, as ``argparse`` refuses a command line: nothing runs (127 when the env has no ``laueportal``).
The standard library only."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

EXIT_REFUSED = 2
"""A command refused (or this module run without its settings file): nothing ran."""
EXIT_NOT_RUN = 127
"""The env's ``laueportal`` could not be run (the shell's "command not found")."""
ALLOWED = "laueportal live notify <job id> <path> --complete"
PATH_CHARACTERS = "letters, digits and . _ - + , = @ % : /"
_JOB_ID = re.compile(r"[0-9]{1,18}", re.ASCII)
_PATH = re.compile(r"/[A-Za-z0-9._\-+,=@%:/]*", re.ASCII)
MAX_PATH_BYTES = 4096
"""The longest path allowed (Linux's ``PATH_MAX``; ``files.roots.MAX_PATH_BYTES``)."""
PREFIX = "laueportal notify hook"


class Refused(ValueError):
    """The command asked for is not the one allowed."""


def notify_arguments(command: str | None) -> tuple[str, str]:
    """The job ID and the path of ``command`` (``SSH_ORIGINAL_COMMAND``) when it is exactly the allowed command; else
    ``Refused``, with the reason (never the command's text)."""
    if not command:
        raise Refused(f"no command: this key runs only `{ALLOWED}`")
    if not (command.isascii() and command.isprintable()):
        raise Refused("a control or non-ASCII character in the command")
    words = command.split(" ")
    if len(words) != 6 or words[:3] != ALLOWED.split(" ")[:3] or words[5] != "--complete":
        raise Refused(f"not `{ALLOWED}`")
    job_id, path = words[3], words[4]
    if not _JOB_ID.fullmatch(job_id):
        raise Refused("the job ID is not a number")
    if not _PATH.fullmatch(path):
        raise Refused(f"the path is not one absolute path of {PATH_CHARACTERS}")
    if len(path.encode()) > MAX_PATH_BYTES:
        raise Refused(f"the path is longer than {MAX_PATH_BYTES} bytes")
    if {".", ".."} & set(path.split("/")):
        raise Refused("the path has a . or .. component")
    return job_id, path


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Run notify for the allowed command (never returns then), else refuse it (``EXIT_REFUSED``)."""
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    if len(argv) != 1 or not os.path.isabs(argv[0]):
        print(f"{PREFIX}: usage: python -I -m laueportal.jobs.notify_hook <settings file>", file=sys.stderr)
        return EXIT_REFUSED
    try:
        job_id, path = notify_arguments(environ.get("SSH_ORIGINAL_COMMAND"))
    except Refused as refused:
        print(f"{PREFIX}: refused: {refused}", file=sys.stderr)
        return EXIT_REFUSED
    program = Path(sys.executable).with_name("laueportal")
    environment = {
        "LAUEPORTAL_CONFIG": argv[0],
        "PATH": os.pathsep.join([os.fspath(program.parent), "/usr/bin:/bin"]),
        "PYTHONNOUSERSITE": "1",  # the exec'd laueportal's Python is not -I: no user site from HOME
    }
    if environ.get("HOME"):
        environment["HOME"] = environ["HOME"]
    sys.stdout.flush()
    try:
        os.execve(program, [os.fspath(program), "live", "notify", job_id, path, "--complete"], environment)
    except OSError as error:  # the env has no laueportal beside this Python
        print(f"{PREFIX}: failed: {program}: {error.strerror}", file=sys.stderr)
        return EXIT_NOT_RUN
    return EXIT_NOT_RUN  # not reached: execve replaced this process


if __name__ == "__main__":
    sys.exit(main())
