"""Shared fixtures, including real virtualenvs built with uv."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

UV = shutil.which("uv")

requires_uv = pytest.mark.skipif(UV is None, reason="uv is not installed")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@dataclass
class RealEnv:
    venv: Path
    code: Path
    project: Path

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def site(self) -> Path:
        return next((self.venv / "lib").glob("python*/site-packages"))


def build_venv(target: Path, packages: list[str]) -> None:
    assert UV is not None
    subprocess.run([UV, "venv", "-q", str(target)], check=True, capture_output=True)
    if packages:
        subprocess.run(
            [UV, "pip", "install", "-q", "--python", str(target / "bin" / "python"), *packages],
            check=True,
            capture_output=True,
        )


@pytest.fixture(scope="session")
def real_env(tmp_path_factory: pytest.TempPathFactory) -> RealEnv:
    """A real venv with real wheels, plus a dummy app that uses only part of them.

    `click` is imported and used, `rich` only lazily, `requests` not at all — so the
    analyser has a known-correct answer to be measured against.
    """
    if UV is None:
        pytest.skip("uv is not installed")
    project = tmp_path_factory.mktemp("realproj")
    venv = project / ".venv"
    build_venv(venv, ["click", "rich", "requests"])

    code = project / "app"
    write(
        code / "__init__.py",
        "from app.main import cli\n\n__all__ = ['cli']\n",
    )
    write(
        code / "main.py",
        (
            "from __future__ import annotations\n\n"
            "from typing import TYPE_CHECKING\n\n"
            "import click\n\n"
            "if TYPE_CHECKING:\n"
            "    import requests\n\n\n"
            "@click.command()\n"
            "def cli() -> None:\n"
            "    from rich.console import Console\n\n"
            "    Console().print('hi')\n\n\n"
            "def fetch(session: requests.Session) -> None:\n"
            "    raise NotImplementedError\n"
        ),
    )
    write(
        project / "pyproject.toml",
        (
            "[project]\n"
            'name = "realproj"\n'
            'version = "0.1.0"\n'
            'dependencies = ["click", "rich"]\n\n'
            "[dependency-groups]\n"
            'dev = ["requests"]\n'
        ),
    )
    return RealEnv(venv=venv, code=code, project=project)
