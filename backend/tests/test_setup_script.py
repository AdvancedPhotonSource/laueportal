"""The environment tooling: ``make setup``'s parts and guards (``scripts/setup.sh``: the pinned micromamba download
and the pnpm store check), where ``scripts/env.sh`` puts the development workspace, ``make lock``'s header,
``make lock-env``'s cleanup, and ``make check-hygiene``'s path check (``scripts/check-paths.sh``) and Git checks
(``scripts/check-tree.sh``).

Everything runs with a stand-in workspace (``$LAUE_DEV_WORKSPACE`` in ``tmp_path``): ``scripts/pnpm-store-check.sh``
with a stand-in ``frontend/node_modules/.modules.yaml``, ``make lock-env`` in a copy of the Makefile with a
stand-in micromamba and ``HOME``, ``make lock`` with a stand-in uv, and the scripts in throwaway Git repositories.
Nothing is installed, and the real ``~/.conda`` is never touched.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "scripts" / "setup.sh"
STORE_CHECK = REPO_ROOT / "scripts" / "pnpm-store-check.sh"
LOCKS = ("dev", "base", "launcher", "compute")
"""The lock sets, in the order ``make lock`` compiles them."""
PINS = ("PNPM_VERSION", "MICROMAMBA_VERSION")
"""The tool pins in ``scripts/setup.sh``."""


def check_store(frontend: Path, workspace: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("LAUEPORTAL_")}
    env.pop("SETUP_ALLOW_STORE_SWITCH", None)
    env = {**env, "LAUE_DEV_WORKSPACE": str(workspace), **extra}
    return subprocess.run(
        [str(STORE_CHECK), str(frontend)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def installed_from(frontend: Path, store: Path, *, json: bool = True) -> None:
    """A ``node_modules/.modules.yaml`` as pnpm writes it (JSON today; YAML in older releases)."""
    modules = frontend / "node_modules" / ".modules.yaml"
    modules.parent.mkdir(parents=True)
    if json:
        modules.write_text(f'{{\n  "layoutVersion": 5,\n  "storeDir": "{store}",\n  "virtualStoreDir": ".pnpm"\n}}\n')
    else:
        modules.write_text(f"layoutVersion: 5\nstoreDir: {store}\nvirtualStoreDir: .pnpm\n")


def test_a_checkout_without_node_modules_may_install(tmp_path: Path) -> None:
    result = check_store(tmp_path / "frontend", tmp_path / "ws")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("json", [True, False])
def test_node_modules_from_this_workspaces_store_may_be_reinstalled(tmp_path: Path, json: bool) -> None:
    workspace = tmp_path / "ws"
    installed_from(tmp_path / "frontend", workspace / "pnpm-store" / "v11", json=json)
    result = check_store(tmp_path / "frontend", workspace)
    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.parametrize("json", [True, False])
def test_node_modules_from_another_workspaces_store_are_refused(tmp_path: Path, json: bool) -> None:
    """``LAUE_DEV_WORKSPACE=<other> make setup`` in an existing checkout would re-link its node_modules from the other
    workspace's store: step 7 refuses and says how to proceed."""
    real_store = tmp_path / "real-ws" / "pnpm-store" / "v11"
    installed_from(tmp_path / "frontend", real_store, json=json)
    other = tmp_path / "other-ws"

    result = check_store(tmp_path / "frontend", other)

    assert result.returncode == 1
    assert str(real_store) in result.stderr and str(other / "pnpm-store") in result.stderr
    assert "separate worktree or clone" in result.stderr and "SETUP_ALLOW_STORE_SWITCH=1" in result.stderr
    # A store whose path only starts like this workspace's is another store.
    sibling = tmp_path / "frontend2"
    installed_from(sibling, Path(f"{other}/pnpm-store-old/v11"), json=json)
    assert check_store(sibling, other).returncode == 1


def test_the_store_switch_can_be_allowed(tmp_path: Path) -> None:
    installed_from(tmp_path / "frontend", tmp_path / "real-ws" / "pnpm-store" / "v11")
    result = check_store(tmp_path / "frontend", tmp_path / "other-ws", SETUP_ALLOW_STORE_SWITCH="1")
    assert result.returncode == 0
    assert "SETUP_ALLOW_STORE_SWITCH=1: re-linking" in result.stdout


def test_setup_checks_the_store_before_installing_and_pins_micromamba() -> None:
    """Step 7 runs the store check before ``pnpm install``; step 2 downloads micromamba from its GitHub release and
    installs it only after checking it against MICROMAMBA_SHA256 (the release asset's digest). Like pnpm's, nothing
    is copied from elsewhere on the machine."""
    setup = SETUP.read_text()
    lines = setup.splitlines()
    check = lines.index("    scripts/pnpm-store-check.sh frontend")
    install = lines.index("    pnpm -C frontend install --frozen-lockfile")
    assert check < install

    assert re.search(r"^MICROMAMBA_SHA256=[0-9a-f]{64}\s", setup, re.MULTILINE)
    verify = setup.index('echo "$MICROMAMBA_SHA256  $MM.tmp" | sha256sum -c --quiet')
    assert setup.index("micromamba-releases/releases/download/") < verify < setup.index('mv "$MM.tmp" "$MM"')
    assert setup.count('-o "$MM.tmp"') == 1 and not re.search(r"^\s*cp\b", setup, re.MULTILINE)


def test_setup_installs_only_the_parts_asked_for(tmp_path: Path) -> None:
    """``make setup-frontend`` runs steps 1, 6 and 7 alone (no micromamba, conda env or dev config), and
    ``make setup-backend`` starts with micromamba and runs no pnpm: CI's jobs install only the parts they use. In a
    copy of the Makefile and the scripts, with a stand-in workspace, a stand-in pnpm at the pinned version and a curl
    that fails, so that a download stops the run."""
    checkout, workspace, log = tmp_path / "checkout", tmp_path / "ws", tmp_path / "calls.log"
    for name in ("Makefile", "scripts/setup.sh", "scripts/env.sh", "scripts/pnpm-store-check.sh"):
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text((REPO_ROOT / name).read_text())
        (checkout / name).chmod(0o755)
    (checkout / "frontend").mkdir()
    (checkout / "frontend" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    pnpm, micromamba = (re.findall(rf"^{name}=(\S+)$", SETUP.read_text(), re.MULTILINE) for name in PINS)
    stand_ins = {
        workspace / "tools" / "pnpm": f'[ "$1" = --version ] && {{ echo {pnpm[0]}; exit 0; }}\necho "pnpm $*" >> {log}',
        tmp_path / "bin" / "curl": f'echo "curl $*" >> {log}\nexit 1',
    }
    for path, body in stand_ins.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("LAUEPORTAL_") and key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
    }
    env = {**env, "LAUE_DEV_WORKSPACE": str(workspace), "PATH": f"{tmp_path / 'bin'}:{env['PATH']}"}

    def setup(target: str) -> tuple[int, list[str], list[str]]:
        log.unlink(missing_ok=True)
        result = subprocess.run(
            ["make", "-C", str(checkout), target], env=env, capture_output=True, text=True, timeout=60, check=False
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return result.returncode, re.findall(r"^==> (.*)$", result.stdout, re.MULTILINE), calls

    steps = [f"workspace directories in {workspace}", f"pnpm {pnpm[0]}", "frontend dependencies", "done"]
    assert setup("setup-frontend") == (0, steps, ["pnpm -C frontend install --frozen-lockfile"])
    assert not any((workspace / "env").iterdir()) and not (workspace / "config" / "dev.toml").exists()

    status, steps, calls = setup("setup-backend")
    assert status != 0 and steps == [f"workspace directories in {workspace}", f"micromamba {micromamba[0]}"]
    assert len(calls) == 1 and calls[0].startswith("curl ") and "micromamba-linux-64" in calls[0]

    unknown = subprocess.run([checkout / "scripts" / "setup.sh", "frontnd"], env=env, capture_output=True, text=True)
    assert (unknown.returncode, unknown.stderr) == (2, "usage: scripts/setup.sh [backend] [frontend]\n")


# ---------------------------------------------------------------------------
# scripts/env.sh: where the development workspace is


def workspace_of(checkout: Path, variable: str = "LAUE_DEV_WORKSPACE", **extra: str) -> str:
    """``$LAUE_DEV_WORKSPACE`` (or another ``variable``) after sourcing ``checkout``'s scripts/env.sh from elsewhere."""
    env = {key: value for key, value in os.environ.items() if key not in {"LAUE_DEV_WORKSPACE", variable}}
    result = subprocess.run(
        ["bash", "-c", f'. "{checkout}/scripts/env.sh" && printf %s "${variable}"'],
        env={**env, **extra},
        cwd="/",
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout


def test_the_workspace_is_the_variable_else_the_local_file_else_in_the_main_checkout(tmp_path: Path) -> None:
    """An exported ``LAUE_DEV_WORKSPACE`` wins; else the main checkout's ``scripts/env.local.sh`` (ignored by Git)
    sets it; else it is the main checkout's ``.workspace`` (ignored by Git). A worktree shares its main checkout's.
    The local file's other settings hold in every shell, the variable exported or not."""
    main, worktree = tmp_path / "main", tmp_path / "worktree"
    (main / "scripts").mkdir(parents=True)
    for name in ("scripts/env.sh", ".gitignore"):
        (main / name).write_text((REPO_ROOT / name).read_text())
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.org", "-C", str(main)]
    for command in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "env"], ["worktree", "add", "-q", worktree]):
        subprocess.run([*git, *command], capture_output=True, timeout=60, check=True)

    assert workspace_of(main) == workspace_of(worktree) == str(main.resolve() / ".workspace")
    assert workspace_of(worktree, LAUE_DEV_WORKSPACE="/elsewhere") == "/elsewhere"
    (main / "scripts" / "env.local.sh").write_text(
        "export LAUE_DEV_WORKSPACE=/home/user/workspace LAUE_LOCAL_SETTING=kept\n"
    )
    assert workspace_of(main) == workspace_of(worktree) == "/home/user/workspace"
    assert workspace_of(main, LAUE_DEV_WORKSPACE="/elsewhere") == "/elsewhere"
    assert workspace_of(worktree, "LAUE_LOCAL_SETTING", LAUE_DEV_WORKSPACE="/elsewhere") == "kept"
    assert subprocess.run([*git, "check-ignore", "-q", ".workspace/env"], check=False).returncode == 0
    (main / ".workspace").symlink_to(tmp_path / "elsewhere")  # a workspace kept on another disk
    ignored = subprocess.run([*git, "status", "--porcelain", "--ignored"], capture_output=True, text=True, check=True)
    assert ignored.stdout == "!! .workspace\n!! scripts/env.local.sh\n"


# ---------------------------------------------------------------------------
# make lock-env: the temporary env is removed and unregistered

FAKE_MICROMAMBA = r"""#!/usr/bin/env bash
# A stand-in micromamba: logs its arguments, and registers the envs it creates in ~/.conda/environments.txt
# (as micromamba does); `env remove` unregisters and deletes them.
set -euo pipefail
echo "$*" >> "$FAKE_MM_LOG"
prefix=""
args=("$@")
for i in "${!args[@]}"; do [ "${args[$i]}" = -p ] && prefix="${args[$((i + 1))]}"; done
registry="$HOME/.conda/environments.txt"
case "$1 ${2:-}" in
  create*)
    mkdir -p "$prefix/conda-meta" "$HOME/.conda"
    echo "$prefix" >> "$registry" ;;
  "env export")
    [ -z "${FAKE_MM_EXPORT_FAILS:-}" ] || exit 1
    printf '# platform: linux-64\n@EXPLICIT\nhttps://conda.anaconda.org/conda-forge/noarch/tzdata-2026a-0.conda#0123\n' ;;
  "env remove")
    [ -d "$prefix" ] || { echo "No prefix found at: $prefix" >&2; exit 1; }
    grep -vxF "$prefix" "$registry" > "$registry.new" || true
    mv "$registry.new" "$registry"
    rm -rf "$prefix" ;;
esac
"""


def lock_env(tmp_path: Path, **extra: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """``make lock-env`` in a copy of the Makefile and env.sh, with a stand-in workspace, micromamba and HOME.

    Returns the result, the workspace and the copy."""
    checkout, workspace, home = tmp_path / "checkout", tmp_path / "ws", tmp_path / "home"
    (checkout / "scripts").mkdir(parents=True)
    for name in ("Makefile", "environment.yml", "scripts/env.sh"):
        (checkout / name).write_text((REPO_ROOT / name).read_text())
    (workspace / "tools").mkdir(parents=True)
    (workspace / "tmp").mkdir()
    micromamba = workspace / "tools" / "micromamba"
    micromamba.write_text(FAKE_MICROMAMBA)
    micromamba.chmod(0o755)
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("LAUEPORTAL_") and key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
    }
    env = {
        **env,
        "LAUE_DEV_WORKSPACE": str(workspace),
        "HOME": str(home),
        "FAKE_MM_LOG": str(tmp_path / "mm.log"),
        **extra,
    }
    result = subprocess.run(
        ["make", "-C", str(checkout), "lock-env"], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    return result, workspace, checkout


def registered(tmp_path: Path) -> list[str]:
    registry = tmp_path / "home" / ".conda" / "environments.txt"
    return registry.read_text().splitlines() if registry.exists() else []


def test_lock_env_removes_and_unregisters_its_temporary_env(tmp_path: Path) -> None:
    """micromamba registers the env it creates in ~/.conda/environments.txt; ``rm -rf`` alone leaves the line behind.
    The recipe removes it with ``micromamba env remove``."""
    result, workspace, checkout = lock_env(tmp_path)

    assert result.returncode == 0, result.stderr
    temporary = workspace / "tmp" / "lock-env"
    assert not temporary.exists()
    assert registered(tmp_path) == []
    calls = (tmp_path / "mm.log").read_text().splitlines()
    assert calls == [
        f"create -y -q -p {temporary} -f environment.yml",
        f"env export -p {temporary} --explicit --md5",
        f"env remove -y -q -p {temporary}",
    ]
    lock = (checkout / "environment.lock").read_text().splitlines()
    assert lock[0].startswith("# Generated by 'make lock-env' from environment.yml on ")
    assert lock[1:] == ["@EXPLICIT", "https://conda.anaconda.org/conda-forge/noarch/tzdata-2026a-0.conda#0123"]


def test_lock_env_removes_its_temporary_env_when_it_fails(tmp_path: Path) -> None:
    result, workspace, _ = lock_env(tmp_path, FAKE_MM_EXPORT_FAILS="1")

    assert result.returncode != 0
    assert not (workspace / "tmp" / "lock-env").exists()
    assert registered(tmp_path) == []


# ---------------------------------------------------------------------------
# make lock: the locks' header names no local path

FAKE_UV = r"""#!/usr/bin/env bash
# A stand-in uv: logs its arguments (one a line, a blank line after each call) and writes the -o file as
# `uv pip compile` does, its header the --custom-compile-command, then one pin.
set -euo pipefail
printf '%s\n' "$@" "" >> "$FAKE_UV_LOG"
args=("$@")
for i in "${!args[@]}"; do
  case "${args[$i]}" in
    -o) out="${args[$((i + 1))]}" ;;
    --custom-compile-command) shown="${args[$((i + 1))]}" ;;
  esac
done
printf '# This file was autogenerated by uv via the following command:\n#    %s\nidna==3.10 \\\n    --hash=sha256:00\n' \
  "$shown" > "$out"
"""


def test_lock_records_its_command_with_the_workspace_as_the_variable(tmp_path: Path) -> None:
    """``make lock`` gives uv the command it ran as ``--custom-compile-command``, the env's python named through
    ``$LAUE_DEV_WORKSPACE``, so no lock's header names the workspace's path (a stand-in uv, a checkout copy)."""
    checkout, workspace = tmp_path / "checkout", tmp_path / "ws"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "requirements").mkdir()
    for name in ("Makefile", "scripts/env.sh", *(f"requirements/{lock}.in" for lock in LOCKS)):
        (checkout / name).write_text((REPO_ROOT / name).read_text())
    for name, text in (("tools/uv", FAKE_UV), ("env/bin/python", f'#!/bin/sh\nexec {sys.executable} "$@"\n')):
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(text)
        (workspace / name).chmod(0o755)
    (workspace / "tmp").mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("LAUEPORTAL_") and key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
    }
    log = tmp_path / "uv.log"
    env = {**env, "LAUE_DEV_WORKSPACE": str(workspace), "FAKE_UV_LOG": str(log)}

    result = subprocess.run(
        ["make", "-C", str(checkout), "lock", "LOCK_DATE=2026-10-01T00:00:00Z"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = [call.splitlines() for call in log.read_text().split("\n\n") if call]
    assert [call[call.index("-o") + 1] for call in calls] == [f"requirements/{lock}.lock.tmp" for lock in LOCKS]
    for call in calls:
        shown = call.pop(call.index("--custom-compile-command") + 1)
        ran = [argument for argument in call if argument not in ("-q", "--custom-compile-command")]
        assert ran[ran.index("--python") + 1] == f"{workspace}/env/bin/python"
        assert shown == "uv " + " ".join(ran).replace(str(workspace), "$LAUE_DEV_WORKSPACE")  # ran: pip compile ...
    for lock in LOCKS:
        header = (checkout / "requirements" / f"{lock}.lock").read_text().splitlines()[:3]
        assert header[0] == "# exclude-newer: 2026-10-01T00:00:00Z"
        assert "--python $LAUE_DEV_WORKSPACE/env/bin/python --exclude-newer 2026-10-01T00:00:00Z " in header[2]
        assert str(tmp_path) not in "\n".join(header)


# ---------------------------------------------------------------------------
# make check: scripts/check-paths.sh


def test_the_path_check_fails_on_a_tracked_home_path_but_not_the_neutral_one_or_a_marked_line(tmp_path: Path) -> None:
    """A tracked file naming ``/home/<name>/``, ``/Users/<name>``, ``~<name>/``, or the folder ``proj`` in a home
    (after ``~``, ``$HOME``, ``${HOME}`` or ``/root``, with or without a slash) fails the check, each line listed;
    ``/home/user/...``, ``~user/...``, a line marked ``path-check: allow`` and an untracked file do not."""
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "check-paths.sh").write_text((REPO_ROOT / "scripts" / "check-paths.sh").read_text())
    (checkout / "scripts" / "check-paths.sh").chmod(0o755)
    # Split, so this file stays clean.
    home, users, projects, proj, tilde = "/ho" + "me/", "/Us" + "ers/", "~/pr" + "oj/", "pr" + "oj", "~"
    files = {
        "clean.md": f"cd {home}user/repo && ls {home}user\nHOME=/home <name> {home}$USER/x\n",
        "marked.md": f"{home}someone/x  <!-- path-check: allow -->\n",
        "flagged.py": f'A = "{home}someone/.ssh"\nB = "{home}user/ok"\nC = "{users}someone"\nD = "{projects}x"\n',
        "mixed.sh": f"cp {home}user/a {home}other/b\n",
        "relative.sh": (  # the forms a home-relative path takes in scripts
            f"cd $HOME/{proj}/laue/workspace\n"
            f'MM="${{HOME}}/{proj}/laue/envs/.tools/bin/micromamba"\n'
            f'cp "$HOME"/{proj}/a b\n'
            f"ls ~/{proj}\n"
            f"cd ~/{proj} && ls\n"
            f"cat {tilde}someone/log.xml\n"
            f"ls ~user/log.xml ~/{proj}ects ~nobody $HOME/.cache /data/~f.h5\n"
            f"cd {home}/someone/x\n"
            f"cd /root/{proj}/laue\n"
        ),
    }
    for name, text in files.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.org", "-C", str(checkout)]
    for command in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "files"]):
        subprocess.run([*git, *command], capture_output=True, timeout=60, check=True)
    (checkout / "untracked.md").write_text(f"{home}someone/x\n")

    result = subprocess.run(
        [str(checkout / "scripts" / "check-paths.sh")], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 1
    assert [line.split(":", 2)[:2] for line in result.stderr.splitlines()[1:]] == [
        ["flagged.py", "1"],
        ["flagged.py", "3"],
        ["flagged.py", "4"],
        ["mixed.sh", "1"],
        *[["relative.sh", str(line)] for line in (1, 2, 3, 4, 5, 6, 8, 9)],
    ]
    (checkout / "flagged.py").unlink()
    (checkout / "relative.sh").unlink()
    (checkout / "mixed.sh").write_text(f"cp {home}user/a {home}user/b\n")
    subprocess.run([*git, "commit", "-q", "-am", "clean"], capture_output=True, timeout=60, check=True)
    clean = subprocess.run([str(checkout / "scripts" / "check-paths.sh")], capture_output=True, text=True, check=False)
    assert (clean.returncode, clean.stdout, clean.stderr) == (0, "check-paths: no machine paths\n", "")


# ---------------------------------------------------------------------------
# make check-hygiene: scripts/check-tree.sh


def test_the_tree_check_fails_on_new_whitespace_errors_and_on_tracked_generated_files(tmp_path: Path) -> None:
    """``scripts/check-tree.sh`` fails on a whitespace error in the changes since the base, committed or not (by
    default the base is where HEAD left ``main``), but not on one the base already has or one in the generated API
    client (``.gitattributes``); a base that is not a commit skips the whitespace check. It fails on tracked generated
    files (build output, ``node_modules``, bytecode, tool caches, the workspace), listing each, and on nothing else."""
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    script = checkout / "scripts" / "check-tree.sh"
    script.write_text((REPO_ROOT / "scripts" / "check-tree.sh").read_text())
    script.chmod(0o755)
    (checkout / ".gitattributes").write_text((REPO_ROOT / ".gitattributes").read_text())
    (checkout / "old.txt").write_text("a trailing space \n")
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.org", "-C", str(checkout)]

    def run_git(*command: str) -> str:
        return subprocess.run([*git, *command], capture_output=True, text=True, timeout=60, check=True).stdout

    def check(*base: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(script), *base], capture_output=True, text=True, timeout=60, check=False)

    run_git("init", "-q", "-b", "main")
    run_git("add", ".")
    run_git("commit", "-q", "-m", "base")
    base = run_git("rev-parse", "HEAD").strip()
    run_git("checkout", "-q", "-b", "topic")
    assert check().returncode == 0

    (checkout / "new.py").write_text("x = 1 \n")
    client = checkout / "frontend" / "src" / "api" / "generated" / "jobs.ts"
    client.parent.mkdir(parents=True)
    client.write_text("export {} \n\n")
    run_git("add", ".")
    uncommitted = check()
    assert uncommitted.returncode == 1
    assert uncommitted.stdout.startswith("new.py:1: trailing whitespace.\n") and "jobs.ts" not in uncommitted.stdout
    run_git("commit", "-q", "-m", "change")
    assert check(base).returncode == 1
    skipped = check("0" * 40)
    assert skipped.returncode == 0 and "is not a commit here: the whitespace check is skipped" in skipped.stdout
    (checkout / "new.py").write_text("x = 1\n")
    run_git("commit", "-q", "-am", "fix")
    assert check(base).returncode == 0

    generated = [
        ".mypy_cache/x.json",
        ".ruff_cache/x",
        ".workspace/env/bin/python",
        "backend/laueportal.egg-info/PKG-INFO",
        "backend/src/__pycache__/m.cpython-312.pyc",
        "backend/src/m.pyc",
        "frontend/dist/index.html",
        "frontend/node_modules/a/index.js",
        "frontend/playwright-report/index.html",
        "frontend/test-results/a/trace.zip",
        "frontend/tsconfig.tsbuildinfo",
    ]
    kept = ["backend/src/laueportal/science/cache.py", "docs/distribution.md", "scripts/build-notes.md"]
    for name in generated + kept:
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text("x\n")
    run_git("add", ".")
    run_git("commit", "-q", "-m", "generated")
    tracked = check(base)
    assert tracked.returncode == 1
    assert tracked.stderr.splitlines() == ["check-tree: generated files are tracked:", *generated]
