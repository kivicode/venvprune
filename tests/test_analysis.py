from __future__ import annotations

import sys
from pathlib import Path

import pytest

from venvprune.analyzer import analyze
from venvprune.graph import Options
from venvprune.model import EdgeKind
from venvprune.trace import run_trace


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def fake_env(tmp_path: Path) -> tuple[Path, Path]:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "libA" / "__init__.py", "from libA.used import thing\n")
    write(site / "libA" / "used.py", "import libB\n\nthing = 1\n")
    write(site / "libA" / "unused.py", "import libC\n")
    write(site / "libA" / "lazy_only.py", "value = 2\n")
    write(site / "libB" / "__init__.py", "")
    write(site / "libC" / "__init__.py", "")
    write(site / "libD" / "__init__.py", "")
    write(site / "libD" / "deep.py", "")

    code = tmp_path / "code"
    write(code / "app.py", "import libA\n\n\ndef later():\n    from libA import lazy_only\n\n    return lazy_only\n")
    return code, tmp_path / "venv"


def test_finds_unused_modules(fake_env):
    code, venv = fake_env
    analysis = analyze([code], venv)
    unused = {i.name for i in analysis.unused()}
    assert "libA.unused" in unused
    assert "libC" in unused
    assert "libD" in unused and "libD.deep" in unused
    assert "libA" not in unused
    assert "libA.used" not in unused
    assert "libB" not in unused


def test_lazy_import_keeps_module_but_marks_it_weak(fake_env):
    code, venv = fake_env
    analysis = analyze([code], venv)
    assert "libA.lazy_only" in analysis.reach.reached
    assert "libA.lazy_only" in analysis.kept(EdgeKind.LAZY)


def test_no_lazy_drops_it(fake_env):
    code, venv = fake_env
    analysis = analyze([code], venv, Options(follow_lazy=False))
    assert "libA.lazy_only" in {i.name for i in analysis.unused()}


def test_fully_unused_distributions(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "libX" / "__init__.py", "")
    info = site / "libx-1.0.dist-info"
    write(info / "METADATA", "Name: libx\nVersion: 1.0\n\nbody")
    write(info / "RECORD", "libX/__init__.py,,\n")
    code = tmp_path / "code"
    write(code / "app.py", "x = 1\n")
    analysis = analyze([code], tmp_path / "venv")
    assert analysis.fully_unused_distributions() == ["libx"]


def test_type_checking_import_is_not_followed_by_default(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "libT" / "__init__.py", "")
    code = tmp_path / "code"
    write(code / "app.py", "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import libT\n")
    assert "libT" in {i.name for i in analyze([code], tmp_path / "venv").unused()}
    kept = analyze([code], tmp_path / "venv", Options(follow_type_only=True))
    assert "libT" not in {i.name for i in kept.unused()}


def test_dynamic_import_keeps_sibling_subtree(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(
        site / "plug" / "__init__.py",
        "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n",
    )
    write(site / "plug" / "backend_a.py", "")
    write(site / "plug" / "backend_b.py", "")
    code = tmp_path / "code"
    write(code / "app.py", "import plug\n")
    analysis = analyze([code], tmp_path / "venv")
    assert not analysis.unused()
    strict = analyze([code], tmp_path / "venv", Options(dynamic_expands_package=False))
    assert {i.name for i in strict.unused()} == {"plug.backend_a", "plug.backend_b"}


def test_relative_imports_resolve(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "rel" / "__init__.py", "from .core import go\n")
    write(site / "rel" / "core" / "__init__.py", "from ..helpers import go\n")
    write(site / "rel" / "helpers.py", "go = 1\n")
    code = tmp_path / "code"
    write(code / "app.py", "import rel\n")
    assert not analyze([code], tmp_path / "venv").unused()


def test_trace_keeps_modules_static_analysis_misses(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "hidden" / "__init__.py", "")
    code = tmp_path / "code"
    write(
        code / "app.py",
        "import importlib\nimport sys\n\nsys.path.insert(0, sys.argv[1])\nimportlib.import_module('hidden')\n",
    )

    static = analyze([code], tmp_path / "venv", Options(dynamic_expands_package=False))
    assert "hidden" in {i.name for i in static.unused()}

    result = run_trace(Path(sys.executable), ["app.py", str(site)], cwd=code, timeout=60)
    assert result.returncode == 0
    assert "hidden" in result.modules

    traced = analyze([code], tmp_path / "venv", Options(dynamic_expands_package=False), trace=result)
    assert traced.traced == {"hidden"}
    assert "hidden" not in {i.name for i in traced.unused()}
