from __future__ import annotations

from pathlib import Path

import pytest

from venvprune.analyzer import analyze
from venvprune.graph import Options
from venvprune.model import ArgShape
from venvprune.risk import Severity, assess, package_risk

from .conftest import write


@pytest.fixture
def plugins(tmp_path: Path) -> Path:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "plug" / "__init__.py", "")
    for name in ("backend_a", "backend_b", "unrelated"):
        write(site / "plug" / f"{name}.py", "")
    write(site / "elsewhere" / "__init__.py", "")
    return tmp_path


def _unused(root: Path, opts: Options | None = None) -> set[str]:
    return {i.name for i in analyze([root / "code"], root / "venv", opts).unused()}


def test_literal_import_module_resolves_exactly(plugins: Path):
    write(plugins / "code" / "app.py", "import importlib\n\nimportlib.import_module('plug.backend_a')\n")
    assert _unused(plugins) == {"plug.backend_b", "plug.unrelated", "elsewhere"}


def test_prefix_folds_to_matching_modules(plugins: Path):
    write(
        plugins / "code" / "app.py",
        "import importlib\n\n\ndef load(name):\n    return importlib.import_module(f'plug.backend_{name}')\n",
    )
    unused = _unused(plugins)
    assert "plug.backend_a" not in unused and "plug.backend_b" not in unused
    assert "plug.unrelated" in unused, "the prefix cannot reach it"
    assert "elsewhere" in unused


def test_bounded_choice_list(plugins: Path):
    write(
        plugins / "code" / "app.py",
        "import importlib\n\nNAMES = ['plug.backend_a', 'plug.unrelated']\n\n"
        "for n in NAMES:\n    importlib.import_module(n)\n",
    )
    unused = _unused(plugins)
    assert unused == {"plug.backend_b", "elsewhere"}


def test_relative_import_module_resolves_against_package(plugins: Path):
    site = plugins / "venv" / "lib" / "python3.12" / "site-packages"
    write(
        site / "plug" / "__init__.py",
        "import importlib\n\n\ndef load(n):\n    return importlib.import_module('.' + n, __package__)\n",
    )
    write(plugins / "code" / "app.py", "import plug\n")
    unused = _unused(plugins)
    assert unused == {"elsewhere"}, "the relative form stays inside plug"


def test_unbounded_keeps_package_but_not_the_world(plugins: Path):
    site = plugins / "venv" / "lib" / "python3.12" / "site-packages"
    write(
        site / "plug" / "__init__.py",
        "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n",
    )
    write(plugins / "code" / "app.py", "import plug\n")
    assert _unused(plugins) == {"elsewhere"}
    assert _unused(plugins, Options(strict_dynamic=True)) == {
        "elsewhere",
        "plug.backend_a",
        "plug.backend_b",
        "plug.unrelated",
    }


def test_shapes_are_recorded(plugins: Path):
    write(
        plugins / "code" / "app.py",
        "import importlib\n\n"
        "importlib.import_module('plug.backend_a')\n"
        "importlib.import_module(f'plug.backend_{x}')\n"
        "importlib.import_module(y)\n",
    )
    analysis = analyze([plugins / "code"], plugins / "venv")
    shapes = [h.shape for h in analysis.dynamic_hints()]
    assert ArgShape.LITERAL in shapes
    assert ArgShape.PREFIX in shapes
    assert ArgShape.UNKNOWN in shapes


def test_risk_severities(plugins: Path):
    site = plugins / "venv" / "lib" / "python3.12" / "site-packages"
    write(
        site / "plug" / "__init__.py",
        "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n",
    )
    write(plugins / "code" / "app.py", "import importlib\nimport plug\n\nimportlib.import_module('elsewhere')\n")
    analysis = analyze([plugins / "code"], plugins / "venv")
    sites = assess(analysis)
    severities = {s.hint.module: s.severity for s in sites}
    assert severities["plug"] is Severity.OPEN
    assert severities["app"] is Severity.RESOLVED
    assert package_risk(sites)["plug"] is Severity.OPEN


def test_getattr_over_module_is_confined(plugins: Path):
    site = plugins / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "plug" / "__init__.py", "import plug.backend_a\n\n\ndef pick(n):\n    return getattr(plug, n)\n")
    write(plugins / "code" / "app.py", "import plug\n")
    analysis = analyze([plugins / "code"], plugins / "venv")
    site_risk = next(s for s in assess(analysis) if s.hint.module == "plug")
    assert site_risk.severity is Severity.CONFINED
    assert "elsewhere" not in site_risk.candidates


def test_entry_point_group_becomes_a_root(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "pluginpkg" / "__init__.py", "")
    write(site / "pluginpkg" / "impl.py", "")
    info = site / "pluginpkg-1.0.dist-info"
    write(info / "METADATA", "Name: pluginpkg\nVersion: 1.0\n\nx")
    write(info / "RECORD", "pluginpkg/__init__.py,,\npluginpkg/impl.py,,\n")
    write(info / "entry_points.txt", "[myapp.plugins]\nimpl = pluginpkg.impl:Plugin\n")
    write(tmp_path / "code" / "app.py", "x = 1\n")

    assert "pluginpkg.impl" in _unused(tmp_path)
    kept = analyze([tmp_path / "code"], tmp_path / "venv", Options(entry_point_groups=("myapp.plugins",)))
    assert "pluginpkg.impl" not in {i.name for i in kept.unused()}


def test_entry_points_call_in_code_discovers_the_group(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "pluginpkg" / "__init__.py", "")
    write(site / "pluginpkg" / "impl.py", "")
    info = site / "pluginpkg-1.0.dist-info"
    write(info / "METADATA", "Name: pluginpkg\nVersion: 1.0\n\nx")
    write(info / "RECORD", "pluginpkg/__init__.py,,\npluginpkg/impl.py,,\n")
    write(info / "entry_points.txt", "[myapp.plugins]\nimpl = pluginpkg.impl:Plugin\n")
    write(
        tmp_path / "code" / "app.py",
        "from importlib.metadata import entry_points\n\n"
        "for ep in entry_points(group='myapp.plugins'):\n    ep.load()\n",
    )
    assert "pluginpkg.impl" not in _unused(tmp_path)
