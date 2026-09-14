"""Parsing across processes must produce exactly what parsing in one produces."""

from __future__ import annotations

from pathlib import Path

import pytest

from venvprune.analysis.analyzer import analyze
from venvprune.analysis.graph import Options
from venvprune.model import Origin
from venvprune.scan import astscan, discovery
from venvprune.scan.symbols import SymbolTable

from .conftest import write


@pytest.fixture
def many_modules(tmp_path: Path) -> Path:
    """Enough modules to clear the floor where the pool becomes worthwhile."""
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "big" / "__init__.py", "from big.m0 import thing0\n")
    for index in range(astscan._PARALLEL_FLOOR + 50):
        body = f"import os\n\n\ndef thing{index}():\n    return os.sep\n"
        if index:
            body = f"from big.m{index - 1} import thing{index - 1}\n" + body
        write(site / "big" / f"m{index}.py", body)
    write(tmp_path / "code" / "app.py", "import big\n")
    return tmp_path


def _scanned(root: Path, jobs: int) -> dict[str, tuple]:
    site = discovery.find_site_packages(root / "venv")
    modules, _ = discovery.index_venv(site)
    astscan.scan_all(modules, jobs=jobs)
    return {
        name: (
            tuple((e.src, e.target, e.kind, e.lineno, e.bindings) for e in info.edges),
            tuple((h.module, h.kind, h.lineno, h.shape) for h in info.hints),
            info.exported,
            tuple(sorted(info.used_attrs)),
            tuple(sorted(info.table.definitions)) if isinstance(info.table, SymbolTable) else (),
        )
        for name, info in modules.items()
    }


def test_parallel_parsing_matches_sequential(many_modules: Path):
    assert _scanned(many_modules, jobs=4) == _scanned(many_modules, jobs=1)


def test_parallel_analysis_reaches_the_same_modules(many_modules: Path):
    one = analyze([many_modules / "code"], many_modules / "venv", Options(jobs=1))
    many = analyze([many_modules / "code"], many_modules / "venv", Options(jobs=4))
    assert set(one.reach.reached) == set(many.reach.reached)
    assert {i.name for i in one.unused()} == {i.name for i in many.unused()}


def test_a_small_venv_stays_in_process(tmp_path: Path, monkeypatch):
    """Below the floor the pool costs more than it saves, so it is never started."""
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "tiny" / "__init__.py", "import os\n")
    write(tmp_path / "code" / "app.py", "import tiny\n")

    started = False

    def boom(*args, **kwargs):
        nonlocal started
        started = True
        raise AssertionError("should not spawn a pool for a handful of modules")

    monkeypatch.setattr(astscan, "_scan_parallel", boom)
    analyze([tmp_path / "code"], tmp_path / "venv", Options(jobs=8))
    assert not started


def test_a_broken_pool_falls_back_to_one_process(many_modules: Path, monkeypatch):
    from concurrent.futures.process import BrokenProcessPool

    def broken(*args, **kwargs):
        raise BrokenProcessPool("no spawning here")

    monkeypatch.setattr(astscan, "_scan_parallel", broken)
    site = discovery.find_site_packages(many_modules / "venv")
    modules, _ = discovery.index_venv(site)
    astscan.scan_all(modules, jobs=8)
    assert modules["big.m0"].edges, "every module is still scanned in place"


def test_local_and_site_modules_all_get_scanned(many_modules: Path):
    analysis = analyze([many_modules / "code"], many_modules / "venv", Options(jobs=4))
    local = [i for i in analysis.modules.values() if i.origin is Origin.LOCAL]
    assert local and all(i.table is not None for i in local)
