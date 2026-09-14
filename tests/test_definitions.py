from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from venvprune.analyzer import analyze
from venvprune.graph import Options
from venvprune.model import Demand
from venvprune.rewrite import plan_definition_rewrites
from venvprune.symbols import Tier, build_table, dead_definitions, live_symbols

from .conftest import write

DEFS = Options(prune_definitions=True, symbol_precision=True)


def table_of(source: str):
    return build_table(ast.parse(source))


def test_live_symbols_follow_references():
    table = table_of(
        "def helper():\n    return 1\n\n\ndef wanted():\n    return helper()\n\n\ndef orphan():\n    return 2\n"
    )
    live = live_symbols(table, {"wanted"})
    assert "wanted" in live and "helper" in live
    assert "orphan" not in live


def test_dead_definitions_reported():
    table = table_of("def a():\n    pass\n\n\ndef b():\n    pass\n\n\nclass C:\n    pass\n")
    dead = {d.name for d in dead_definitions(table, {"a"})}
    assert dead == {"b", "C"}


def test_module_level_side_effects_keep_names_alive():
    table = table_of("def setup():\n    pass\n\n\nsetup()\n\n\ndef unused():\n    pass\n")
    dead = {d.name for d in dead_definitions(table, set())}
    assert "setup" not in dead, "it is called at import time"
    assert "unused" in dead


def test_decorated_function_is_risky():
    table = table_of("import app\n\n\n@app.route('/x')\ndef handler():\n    pass\n")
    assert table.definitions["handler"][0].tier is Tier.RISKY
    assert not dead_definitions(table, set())
    assert {d.name for d in dead_definitions(table, set(), include_risky=True)} == {"handler"}


def test_pure_decorators_stay_safe():
    table = table_of("import functools\n\n\n@functools.lru_cache\ndef cheap():\n    pass\n")
    assert table.definitions["cheap"][0].tier is Tier.SAFE


def test_class_with_foreign_base_is_risky():
    table = table_of("from ext import Base\n\n\nclass Thing(Base):\n    pass\n")
    assert table.definitions["Thing"][0].tier is Tier.RISKY


def test_class_with_local_base_is_safe():
    table = table_of("class Root:\n    pass\n\n\nclass Thing(Root):\n    pass\n")
    assert table.definitions["Thing"][0].tier is Tier.SAFE


@pytest.mark.parametrize(
    "source",
    [
        "def __getattr__(name):\n    return 1\n",
        "x = eval('1')\n",
        "from other import *\n",
    ],
)
def test_unsafe_modules_are_never_pruned(source: str):
    table = table_of(source)
    assert not table.prunable
    assert dead_definitions(table, {"anything"}) == []
    assert live_symbols(table, {"anything"}) == table.names()


def test_demand_all_prunes_nothing():
    table = table_of("def a():\n    pass\n\n\ndef b():\n    pass\n")
    assert dead_definitions(table, Demand.ALL) == []


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(
        site / "util" / "__init__.py",
        "from util.core import needed, unneeded\n\n__all__ = ['needed', 'unneeded']\n",
    )
    write(
        site / "util" / "core.py",
        "import heavylib\n\n\n"
        "def needed():\n    return 'yes'\n\n\n"
        "def unneeded():\n    return heavylib.work()\n\n\n"
        "class AlsoUnneeded:\n    pass\n",
    )
    write(site / "heavylib" / "__init__.py", "def work():\n    return 1\n")
    write(tmp_path / "code" / "app.py", "from util import needed\n\nprint(needed())\n")
    return tmp_path


def test_unused_function_frees_its_import_and_the_module_behind_it(lib: Path):
    loose = analyze([lib / "code"], lib / "venv", Options(symbol_precision=True))
    assert "heavylib" not in {i.name for i in loose.unused()}, "module-level precision keeps it"

    tight = analyze([lib / "code"], lib / "venv", DEFS)
    assert "heavylib" in {i.name for i in tight.unused()}, "only unneeded() used heavylib"


def test_definition_plan_lists_the_dead_code(lib: Path):
    analysis = analyze([lib / "code"], lib / "venv", DEFS)
    plans = plan_definition_rewrites(analysis)
    core = next(p for p in plans if p.module == "util.core")
    assert core.names == ["AlsoUnneeded", "unneeded"]
    removed = [line[1:] for line in core.diff().splitlines() if line.startswith("-") and line[1:].strip()]
    assert any("import heavylib" in line for line in removed), "the import only it used goes too"
    assert not any("def needed" in line for line in removed)
    assert ast.parse(core.patched)


def test_init_import_is_narrowed_not_deleted(lib: Path):
    analysis = analyze([lib / "code"], lib / "venv", DEFS)
    init = next(p for p in plan_definition_rewrites(analysis) if p.module == "util")
    assert "from util.core import needed" in init.patched
    assert "unneeded" not in init.patched, "__all__ and the import both lose the dead name"


def test_future_import_is_never_removed(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "ann" / "__init__.py", "from ann.core import keep\n")
    write(
        site / "ann" / "core.py",
        "from __future__ import annotations\n\n\ndef keep() -> str:\n    return ''\n\n\ndef drop():\n    pass\n",
    )
    write(tmp_path / "code" / "app.py", "from ann import keep\n\nkeep()\n")
    analysis = analyze([tmp_path / "code"], tmp_path / "venv", DEFS)
    core = next(p for p in plan_definition_rewrites(analysis) if p.module == "ann.core")
    assert "from __future__ import annotations" in core.patched
    assert "def drop" not in core.patched


def test_rewritten_module_is_valid_python_and_still_works(lib: Path):
    analysis = analyze([lib / "code"], lib / "venv", DEFS)
    plans = plan_definition_rewrites(analysis)
    for plan in plans:
        ast.parse(plan.patched)
        plan.apply()

    for info in analyze([lib / "code"], lib / "venv", DEFS).unused():
        if info.path.is_file():
            info.path.unlink()

    site = lib / "venv" / "lib" / "python3.12" / "site-packages"
    probe = "\n".join(
        [
            "import sys",
            "sys.path.insert(0, sys.argv[1])",
            "from util import needed",
            "assert needed() == 'yes'",
            "import util.core",
            "assert not hasattr(util.core, 'unneeded')",
        ]
    )
    result = subprocess.run([sys.executable, "-c", probe, str(site)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_risky_definitions_are_kept_by_default(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "reg" / "__init__.py", "from reg.impl import wanted\n")
    write(
        site / "reg" / "impl.py",
        "import registry\n\n\ndef wanted():\n    pass\n\n\n@registry.register\ndef plugin():\n    pass\n",
    )
    write(site / "registry" / "__init__.py", "def register(f):\n    return f\n")
    write(tmp_path / "code" / "app.py", "from reg import wanted\n\nwanted()\n")

    analysis = analyze([tmp_path / "code"], tmp_path / "venv", DEFS)
    assert not plan_definition_rewrites(analysis), "the decorated function may be reached via the registry"

    risky = plan_definition_rewrites(analysis, include_risky=True)
    assert {n for p in risky for n in p.names} == {"plugin"}
