"""Integration tests against real virtualenvs with real wheels installed."""

from __future__ import annotations

import pytest

from venvprune.analyzer import analyze
from venvprune.graph import Options
from venvprune.model import EdgeKind
from venvprune.trace import run_trace

from .conftest import RealEnv, requires_uv

pytestmark = [requires_uv, pytest.mark.integration]


@pytest.fixture(scope="module")
def analysis(real_env: RealEnv):
    return analyze([real_env.code], real_env.venv)


def test_eagerly_used_package_is_kept(analysis):
    assert "click" in analysis.reach.reached
    assert "click.core" in analysis.reach.reached


def test_unused_package_is_pruned(analysis):
    unused = {i.name for i in analysis.unused()}
    assert "requests" in unused, "requests is only under TYPE_CHECKING, so it never runs"
    assert "urllib3" in unused


def test_lazily_used_package_is_kept_but_weak(analysis):
    assert "rich.console" in analysis.reach.reached
    assert analysis.reach.reached["rich.console"] is EdgeKind.LAZY


def test_type_only_flag_keeps_requests(real_env: RealEnv):
    kept = analyze([real_env.code], real_env.venv, Options(follow_type_only=True))
    assert "requests" not in {i.name for i in kept.unused()}


def test_fully_unused_distribution_detected(analysis):
    dists = analysis.fully_unused_distributions()
    assert "requests" in dists
    assert "click" not in dists


def test_kept_set_actually_imports(real_env: RealEnv, analysis, tmp_path):
    """The survivors must be enough to import the app: move the pruned files aside and retry."""
    graveyard = tmp_path / "pruned"
    moved = []
    for info in analysis.unused():
        if not info.path.is_file():
            continue
        dest = graveyard / info.path.relative_to(real_env.site)
        dest.parent.mkdir(parents=True, exist_ok=True)
        info.path.rename(dest)
        moved.append((info.path, dest))
    try:
        result = run_trace(
            real_env.python,
            ["-c", "import sys; sys.path.insert(0, sys.argv[1]); import app; app.cli.name", str(real_env.code.parent)],
            timeout=120,
        )
        assert result.returncode == 0, "app failed to import after pruning"
    finally:
        for original, dest in moved:
            dest.rename(original)


def test_trace_promotes_lazy_import_to_observed(real_env: RealEnv):
    result = run_trace(
        real_env.python,
        ["-c", "import sys; sys.path.insert(0, sys.argv[1]); import rich.console", str(real_env.code.parent)],
        timeout=120,
    )
    assert result.returncode == 0
    assert "rich.console" in result.modules
    traced = analyze([real_env.code], real_env.venv, Options(follow_lazy=False), trace=result)
    assert "rich.console" in traced.reach.reached
