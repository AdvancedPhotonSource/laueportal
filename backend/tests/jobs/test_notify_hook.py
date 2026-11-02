"""The acquisition hook's forced command (``jobs.notify_hook``; deploy.md, "The acquisition hook's key"): sshd runs it
for the hook's key with the command asked for in ``SSH_ORIGINAL_COMMAND``. It runs
``laueportal live notify <job id> <path> --complete`` alone, with no shell, and refuses anything else. The real command
runs as sshd runs it (a fresh ``python -I``, nothing of this process's environment). The refusals run in-process with
``os.execve`` stood in."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from laueportal.jobs import notify_hook
from laueportal.jobs.notify_hook import EXIT_REFUSED, MAX_PATH_BYTES
from tests.jobs.live_support import create_live_wire_run
from tests.settings_support import make_database
from tests.worker_support import write_config

LAUEPORTAL = Path(sys.executable).with_name("laueportal")


def _hook(config: Path, command: str, **environment: str) -> subprocess.CompletedProcess[str]:
    """The forced command as sshd runs it: ``command="<python> -I -m laueportal.jobs.notify_hook <config>"`` with the
    session's environment (``SSH_ORIGINAL_COMMAND``, ``HOME``, a system ``PATH``, and what the client sent)."""
    return subprocess.run(
        [sys.executable, "-I", "-m", "laueportal.jobs.notify_hook", os.fspath(config)],
        env={"SSH_ORIGINAL_COMMAND": command, "HOME": os.fspath(config.parent), "PATH": "/usr/bin:/bin", **environment},
        cwd=config.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_hook_runs_notify_with_no_shell_and_passes_its_output_and_status_through(tmp_path):
    """The allowed command is notify itself: its JSON line and status 0 for an admission, its one-line refusal and
    status 1 for a file outside the run's folder. A settings variable in the session's environment does not reach
    notify (the hook's environment is its own): the configured database is the one written."""
    _, engine = make_database(tmp_path)
    try:
        run = create_live_wire_run(engine, tmp_path)
        config = write_config(tmp_path / "prod.toml", database=tmp_path / "laueportal.db", runs_root=tmp_path / "runs")
        path = run.file(3)
        outside = tmp_path / "live_3.h5"
        outside.write_bytes(b"")
        elsewhere = {"LAUEPORTAL_DATABASE__PATH": os.fspath(tmp_path / "missing.db")}  # notify would exit 2 with it

        admitted = _hook(config, f"laueportal live notify {run.job_id} {path} --complete", **elsewhere)
        refused = _hook(config, f"laueportal live notify {run.job_id} {outside} --complete")
    finally:
        engine.dispose()

    assert (admitted.returncode, admitted.stderr) == (0, ""), admitted.stderr
    assert json.loads(admitted.stdout) == {"job_id": run.job_id, "n": 0, "path": os.fspath(path), "admitted": True}
    assert refused.returncode == 1 and refused.stdout == ""
    assert refused.stderr.startswith("laueportal live notify: refused: ") and refused.stderr.count("\n") == 1
    assert "is not directly in the run's folder" in refused.stderr
    lines = (run.run_directory / "arrivals.jsonl").read_text().splitlines()
    assert [json.loads(line)["path"] for line in lines] == [os.fspath(path)]


def test_the_exec_d_notify_never_loads_the_user_site_of_the_home_it_keeps(tmp_path):
    """The hook keeps the session's ``HOME`` (the portal account's), and the exec'd ``laueportal`` runs a plain
    ``python`` (not ``-I``), so without ``PYTHONNOUSERSITE`` a ``.pth`` file in that home's user site would run inside
    notify. Here one would write a marker; notify runs (its own refusal: the database does not exist) and the marker is
    never written."""
    config = write_config(tmp_path / "prod.toml", database=tmp_path / "laueportal.db", runs_root=tmp_path / "runs")
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    user_site = tmp_path / ".local" / "lib" / version / "site-packages"
    user_site.mkdir(parents=True)
    marker = tmp_path / "user-site-ran"
    (user_site / "planted.pth").write_text(f"import pathlib; pathlib.Path({os.fspath(marker)!r}).write_text('ran')\n")

    result = _hook(config, f"laueportal live notify 7 {tmp_path}/live_1.h5 --complete")

    assert result.returncode == 2 and result.stderr.startswith("laueportal live notify"), result.stderr
    assert "does not exist" in result.stderr
    assert not marker.exists()


def test_the_hook_execs_laueportal_beside_its_python_in_an_environment_of_its_own(monkeypatch, capsys):
    """Exactly ``laueportal live notify <job id> <path> --complete``, as separate arguments (a path with every
    punctuation allowed passes as it is), in the settings file's environment: nothing else of the session's."""
    calls = []
    monkeypatch.setattr(os, "execve", lambda *arguments: calls.append(arguments))
    path = "/data/exp_2026-3/user.name+x,y=z@host%1:2/Si_wire_17.h5"
    session = {
        "SSH_ORIGINAL_COMMAND": f"laueportal live notify 17 {path} --complete",
        "HOME": "/home/user",
        "LAUEPORTAL_DATABASE__PATH": "/elsewhere.db",
        "PYTHONPATH": "/elsewhere",
        "LD_PRELOAD": "/elsewhere.so",
    }

    notify_hook.main(["/local/laueportal/config/prod.toml"], session)

    assert calls == [
        (
            LAUEPORTAL,
            [os.fspath(LAUEPORTAL), "live", "notify", "17", path, "--complete"],
            {
                "LAUEPORTAL_CONFIG": "/local/laueportal/config/prod.toml",
                "PATH": f"{LAUEPORTAL.parent}:/usr/bin:/bin",
                "PYTHONNOUSERSITE": "1",
                "HOME": "/home/user",
            },
        )
    ]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        (None, "no command"),  # an interactive login
        ("", "no command"),
        ("sh", "not `laueportal live notify <job id> <path> --complete`"),
        ("internal-sftp", "not `laueportal"),
        ("laueportal db backup /tmp/copy.db", "not `laueportal"),
        ("laueportal live notify 7 /data/f_1.h5", "not `laueportal"),  # no --complete
        ("laueportal live notify 7 /data/f_1.h5 --complete /data/f_2.h5", "not `laueportal"),
        ("laueportal live notify 7 /data/f_1.h5 /data/f_2.h5 --complete", "not `laueportal"),
        ("laueportal live notify 7 /data/f_1.h5 /data/f_2.h5", "not `laueportal"),
        ("laueportal  live notify 7 /data/f_1.h5 --complete", "not `laueportal"),
        ("laueportal live notify 7 /data/f 1.h5 --complete", "not `laueportal"),
        ("laueportal live notify 7 '/data/f 1.h5' --complete", "not `laueportal"),
        ("laueportal live notify 7 /data/f_1.h5 --complete\nid", "a control or non-ASCII character"),
        ("laueportal live notify 7 /data/f_1.h5\t--complete", "a control or non-ASCII character"),
        ("laueportal live notify 7 /data/f_é.h5 --complete", "a control or non-ASCII character"),
        ("laueportal live notify 7;id /data/f_1.h5 --complete", "the job ID is not a number"),
        ("laueportal live notify $(id) /data/f_1.h5 --complete", "the job ID is not a number"),
        ("laueportal live notify -7 /data/f_1.h5 --complete", "the job ID is not a number"),
        ("laueportal live notify 1234567890123456789 /data/f_1.h5 --complete", "the job ID is not a number"),
        ("laueportal live notify 7 f_1.h5 --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 --help --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/f_1.h5;id --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/f_1.h5&&id --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/f_1.h5|id --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/`id`.h5 --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/$(id).h5 --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/${HOME}.h5 --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/f_*.h5 --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/f_1.h5>/tmp/x --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/~f.h5 --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/f\\_1.h5 --complete", "the path is not one absolute path"),
        ("laueportal live notify 7 /data/../etc/f_1.h5 --complete", "a . or .. component"),
        ("laueportal live notify 7 /data/./f_1.h5 --complete", "a . or .. component"),
        (f"laueportal live notify 7 /{'d' * MAX_PATH_BYTES}.h5 --complete", f"longer than {MAX_PATH_BYTES} bytes"),
    ],
)
def test_the_hook_refuses_any_other_command_with_one_line_and_status_2_and_runs_nothing(
    monkeypatch, capsys, command, reason
):
    """No shell ever sees the command: anything but the allowed command (a shell metacharacter, a space, a control
    character, a relative path or an option, another command) is refused, never repeated back."""
    calls = []
    monkeypatch.setattr(os, "execve", lambda *arguments: calls.append(arguments))
    session = {"HOME": "/home/user"} if command is None else {"SSH_ORIGINAL_COMMAND": command, "HOME": "/home/user"}

    assert notify_hook.main(["/local/laueportal/config/prod.toml"], session) == EXIT_REFUSED

    assert calls == []
    err = capsys.readouterr().err
    assert err.startswith("laueportal notify hook: refused: ") and err.count("\n") == 1 and reason in err, err


@pytest.mark.parametrize("argv", [[], ["prod.toml"], ["/a.toml", "/b.toml"]])
def test_the_hook_without_its_settings_file_refuses_every_command(monkeypatch, capsys, argv):
    """The forced command names the settings file by its absolute path; installed otherwise, nothing runs."""
    calls = []
    monkeypatch.setattr(os, "execve", lambda *arguments: calls.append(arguments))
    allowed = {"SSH_ORIGINAL_COMMAND": "laueportal live notify 7 /data/f_1.h5 --complete"}
    assert notify_hook.main(argv, allowed) == EXIT_REFUSED
    assert calls == [] and "usage: python -I -m laueportal.jobs.notify_hook <settings file>" in capsys.readouterr().err
