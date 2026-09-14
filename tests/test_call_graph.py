"""One module, several disjoint call-graph clusters, only one of them used.

`toolkit.core` defines four clusters of functions:

    alpha  -> alpha_helper -> shared_util -> shared_lib      (the one the app uses)
    beta   -> beta_helper  -> heavy_b      -> deep_b
              beta         -> shared_util                    (shared with the live cluster)
    gamma  -> gamma_two    -> gamma_one    -> heavy_c
    delta                                  -> heavy_d

Importing only `alpha` should leave the first cluster intact, delete the other three whole,
drop the imports that only they used, and let the modules behind those imports be pruned in
the same pass.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from venvprune.analysis.analyzer import analyze
from venvprune.analysis.graph import Options
from venvprune.edit.apply import build_plan, execute
from venvprune.edit.rewrite import plan_definition_rewrites

from .conftest import write

DEFS = Options(symbol_precision=True, prune_definitions=True)

CORE = """\
import shared_lib

import heavy_b
import heavy_c
import heavy_d

PRECISION = 4
UNUSED_LIMIT = 99


def shared_util(value):
    return shared_lib.normalise(value, PRECISION)


def alpha_helper(value):
    return shared_util(value) * 2


def alpha(value):
    return alpha_helper(value) + 1


def beta_helper(value):
    return heavy_b.crunch(value)


def beta(value):
    return beta_helper(value) + shared_util(value)


def gamma_one(value):
    return heavy_c.step(value, UNUSED_LIMIT)


def gamma_two(value):
    return gamma_one(value) - 1


def gamma(value):
    return gamma_two(value)


def delta(value):
    return heavy_d.solo(value)
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(
        site / "toolkit" / "__init__.py",
        "from toolkit.core import alpha, beta, delta, gamma\n\n__all__ = ['alpha', 'beta', 'gamma', 'delta']\n",
    )
    write(site / "toolkit" / "core.py", CORE)
    write(site / "shared_lib" / "__init__.py", "def normalise(value, precision):\n    return round(value, precision)\n")
    write(site / "heavy_b" / "__init__.py", "import deep_b\n\n\ndef crunch(value):\n    return deep_b.go(value)\n")
    write(site / "deep_b" / "__init__.py", "def go(value):\n    return value\n")
    write(site / "heavy_c" / "__init__.py", "def step(value, limit):\n    return min(value, limit)\n")
    write(site / "heavy_d" / "__init__.py", "def solo(value):\n    return value\n")
    write(tmp_path / "code" / "app.py", "from toolkit import alpha\n\nprint(alpha(2))\n")
    return tmp_path


def _core_rewrite(project: Path):
    analysis = analyze([project / "code"], project / "venv", DEFS)
    return analysis, next(p for p in plan_definition_rewrites(analysis) if p.module == "toolkit.core")


def test_only_the_used_cluster_survives(project: Path):
    _, core = _core_rewrite(project)
    assert core.names == ["UNUSED_LIMIT", "beta", "beta_helper", "delta", "gamma", "gamma_one", "gamma_two"]

    for live in ("def alpha(", "def alpha_helper(", "def shared_util(", "PRECISION = 4"):
        assert live in core.patched, f"{live} is reachable from alpha"
    for dead in ("def beta(", "def beta_helper(", "def gamma(", "def gamma_one(", "def gamma_two(", "def delta("):
        assert dead not in core.patched, f"{dead} is in no cluster the app reaches"


def test_a_node_shared_with_a_dead_cluster_is_kept(project: Path):
    """`shared_util` is called by both alpha and beta; one live caller is enough."""
    _, core = _core_rewrite(project)
    assert "def shared_util(" in core.patched
    assert "shared_util" not in core.names


def test_imports_used_only_by_dead_clusters_are_dropped(project: Path):
    _, core = _core_rewrite(project)
    assert "import shared_lib" in core.patched, "still needed by shared_util"
    for gone in ("import heavy_b", "import heavy_c", "import heavy_d"):
        assert gone not in core.patched, f"{gone} was only used by a removed cluster"


def test_modules_behind_those_imports_are_pruned_transitively(project: Path):
    analysis = analyze([project / "code"], project / "venv", DEFS)
    unused = {i.name for i in analysis.unused()}
    assert unused == {"heavy_b", "heavy_c", "heavy_d", "deep_b"}
    assert "deep_b" in unused, "reached only through heavy_b, which only beta_helper used"
    assert "shared_lib" not in unused


def test_module_level_pruning_alone_finds_none_of_this(project: Path):
    """Without definition pruning every one of those modules looks reachable."""
    loose = analyze([project / "code"], project / "venv", Options(symbol_precision=True))
    assert not loose.unused()


def test_the_init_is_narrowed_to_the_used_name(project: Path):
    analysis = analyze([project / "code"], project / "venv", DEFS)
    init = next(p for p in plan_definition_rewrites(analysis) if p.module == "toolkit")
    assert "from toolkit.core import alpha" in init.patched
    for gone in ("beta", "gamma", "delta"):
        assert gone not in init.patched, f"{gone} is no longer re-exported or listed in __all__"


def test_the_pruned_package_still_computes_the_right_answer(project: Path):
    """Apply every rewrite, delete everything freed, then run the surviving function."""
    analysis = analyze([project / "code"], project / "venv", DEFS)
    for plan in plan_definition_rewrites(analysis):
        ast.parse(plan.patched)
        plan.apply(backup_suffix="")
    execute(build_plan(analyze([project / "code"], project / "venv", DEFS)), analysis.site_dirs, project)

    site = project / "venv" / "lib" / "python3.12" / "site-packages"
    probe = "\n".join(
        [
            "import sys",
            "sys.path.insert(0, sys.argv[1])",
            "from toolkit import alpha",
            "assert alpha(2) == 5, alpha(2)",
            "import toolkit.core as core",
            "assert not hasattr(core, 'beta')",
            "assert not hasattr(core, 'gamma')",
            "assert core.shared_util(3) == 3",
            "for gone in ('heavy_b', 'heavy_c', 'heavy_d', 'deep_b'):",
            "    assert gone not in sys.modules",
            "print('ok')",
        ]
    )
    result = subprocess.run([sys.executable, "-c", probe, str(site)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout

    for gone in ("heavy_b", "heavy_c", "heavy_d", "deep_b"):
        assert not (site / gone).exists(), f"{gone} should be off disk"
    assert (site / "shared_lib").exists()


def test_a_second_pass_finds_nothing_left(project: Path):
    analysis = analyze([project / "code"], project / "venv", DEFS)
    for plan in plan_definition_rewrites(analysis):
        plan.apply(backup_suffix="")
    execute(build_plan(analyze([project / "code"], project / "venv", DEFS)), analysis.site_dirs, project)

    second = analyze([project / "code"], project / "venv", DEFS)
    assert not second.unused()
    assert not plan_definition_rewrites(second)


def test_the_rewritten_file_is_tidy(project: Path):
    """Removing blocks must not leave ragged runs of blank lines behind."""
    _, core = _core_rewrite(project)
    assert "\n\n\n\n" not in core.patched
    assert core.patched.endswith("def alpha(value):\n    return alpha_helper(value) + 1\n")


def test_blank_lines_inside_a_string_are_never_touched(tmp_path: Path):
    """The tidy pass is guarded by an AST comparison, so string data survives it."""
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    banner = '"""line\n\n\n\n\nstill the same string"""'
    write(site / "lit" / "__init__.py", "from lit.core import wanted\n")
    write(
        site / "lit" / "core.py",
        f"BANNER = {banner}\n\n\ndef wanted():\n    return BANNER\n\n\ndef dead():\n    return 2\n",
    )
    write(tmp_path / "code" / "app.py", "from lit import wanted\n\nwanted()\n")

    analysis = analyze([tmp_path / "code"], tmp_path / "venv", DEFS)
    core = next(p for p in plan_definition_rewrites(analysis) if p.module == "lit.core")
    assert "def dead(" not in core.patched
    assert banner in core.patched, "the blank lines inside the literal are data, not layout"
    assert ast.parse(core.patched)
