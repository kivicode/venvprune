from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from venvprune.analyzer import analyze
from venvprune.graph import Options
from venvprune.model import Demand
from venvprune.rewrite import plan_rewrites

from .conftest import write


@pytest.fixture
def pkg(tmp_path: Path) -> Path:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(
        site / "big" / "__init__.py",
        "from big.fast import speed\n"
        "from big.heavy import Heavy\n"
        "from big.other import misc\n\n"
        "__all__ = ['speed', 'Heavy', 'misc']\n",
    )
    write(site / "big" / "fast.py", "speed = 1\n")
    write(site / "big" / "heavy.py", "import weighty\n\n\nclass Heavy:\n    pass\n")
    write(site / "big" / "other.py", "misc = 2\n")
    write(site / "weighty" / "__init__.py", "payload = 'x' * 1000\n")
    return tmp_path


def test_whole_package_imported_but_one_symbol_used(pkg: Path):
    write(pkg / "code" / "app.py", "import big\n\nprint(big.speed)\n")
    loose = analyze([pkg / "code"], pkg / "venv")
    assert not loose.unused(), "without symbol precision the whole package is kept"

    tight = analyze([pkg / "code"], pkg / "venv", Options(symbol_precision=True))
    assert {i.name for i in tight.unused()} == {"big.heavy", "big.other", "weighty"}
    assert tight.reach.demand["big"] == {"speed"}


def test_from_import_narrows_demand(pkg: Path):
    write(pkg / "code" / "app.py", "from big import misc\n\nprint(misc)\n")
    tight = analyze([pkg / "code"], pkg / "venv", Options(symbol_precision=True))
    assert {i.name for i in tight.unused()} == {"big.fast", "big.heavy", "weighty"}


def test_star_import_forces_everything(pkg: Path):
    write(pkg / "code" / "app.py", "from big import *\n")
    tight = analyze([pkg / "code"], pkg / "venv", Options(symbol_precision=True))
    assert tight.reach.demand["big"] is Demand.ALL
    assert not tight.unused()


def test_opaque_use_forces_everything(pkg: Path):
    write(pkg / "code" / "app.py", "import big\n\nprint(dir(big))\n")
    tight = analyze([pkg / "code"], pkg / "venv", Options(symbol_precision=True))
    assert tight.reach.demand["big"] is Demand.ALL
    assert not tight.unused()


def test_init_using_its_own_import_is_never_dropped(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(
        site / "cfg" / "__init__.py",
        "from cfg.setup import configure\nfrom cfg.extra import Extra\nfrom cfg.spare import Spare\n\nconfigure()\n",
    )
    write(site / "cfg" / "setup.py", "def configure():\n    pass\n")
    write(site / "cfg" / "extra.py", "class Extra:\n    pass\n")
    write(site / "cfg" / "spare.py", "class Spare:\n    pass\n")
    write(tmp_path / "code" / "app.py", "from cfg import Extra\n\nprint(Extra)\n")
    tight = analyze([tmp_path / "code"], tmp_path / "venv", Options(symbol_precision=True))
    unused = {i.name for i in tight.unused()}
    assert "cfg.setup" not in unused, "__init__ calls configure() itself, so its import must stay"
    assert "cfg.extra" not in unused, "the code asked for Extra"
    assert unused == {"cfg.spare"}


def test_rewrite_applies_and_package_still_imports(pkg: Path):
    write(pkg / "code" / "app.py", "import big\n\nprint(big.speed)\n")
    analysis = analyze([pkg / "code"], pkg / "venv", Options(symbol_precision=True))
    plans = plan_rewrites(analysis)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.module == "big"
    assert plan.names == ["Heavy", "misc"]
    assert "from big.heavy import Heavy" in plan.diff()

    plan.apply()
    for info in analysis.unused():
        if info.path.is_file():
            info.path.unlink()

    site = pkg / "venv" / "lib" / "python3.12" / "site-packages"
    probe = "\n".join(
        [
            "import sys",
            "sys.path.insert(0, sys.argv[1])",
            "import big",
            "assert big.speed == 1",
            "try:",
            "    big.Heavy",
            "except AttributeError as exc:",
            "    assert 'venvprune' in str(exc), exc",
            "else:",
            "    raise SystemExit('Heavy should be gone')",
        ]
    )
    check = subprocess.run([sys.executable, "-c", probe, str(site)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_rewrite_keeps_a_backup(pkg: Path):
    write(pkg / "code" / "app.py", "import big\n\nprint(big.speed)\n")
    plan = plan_rewrites(analyze([pkg / "code"], pkg / "venv", Options(symbol_precision=True)))[0]
    plan.apply()
    backup = plan.path.with_suffix(".py.venvprune-bak")
    assert backup.read_text() == plan.original
