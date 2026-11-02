import os
import subprocess
import sys
import textwrap
from pathlib import Path

import laueportal

SOURCE_DIR = Path(laueportal.__file__).resolve().parent.parent


def test_version_is_set():
    assert laueportal.__version__


def test_importing_every_module_reads_no_settings_and_creates_no_files(tmp_path):
    """No import-time side effects (docs/contributing/backend.md): every module imports without a usable config."""

    script = textwrap.dedent(
        """
        import importlib, os, pkgutil, sys
        import laueportal
        from laueportal.core import settings

        names = sorted(
            info.name
            for info in pkgutil.walk_packages(laueportal.__path__, "laueportal.")
            if info.name != "laueportal.__main__"  # runs the CLI
        )
        for name in names:
            importlib.import_module(name)
        assert settings.get_settings.cache_info().currsize == 0, "a module called get_settings() at import"
        print(len(names), sorted(os.listdir(os.getcwd())))
        """
    )
    env = {
        **{key: value for key, value in os.environ.items() if not key.upper().startswith("LAUEPORTAL_")},
        "LAUEPORTAL_CONFIG": str(tmp_path / "missing.toml"),
        "PYTHONPATH": str(SOURCE_DIR),
    }
    work = tmp_path / "cwd"
    work.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=work, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    count, files = result.stdout.split(" ", 1)
    assert int(count) > 50
    assert files.strip() == "[]"  # nothing was written to the working directory
    assert not (tmp_path / "missing.toml").exists()
