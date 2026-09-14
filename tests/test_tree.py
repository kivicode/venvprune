from __future__ import annotations

from pathlib import Path

import pytest

from venvprune.analysis.analyzer import analyze
from venvprune.render.tree import build_tree, render_tree

from .conftest import write


@pytest.fixture
def analysis(tmp_path: Path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "keep" / "__init__.py", "from keep.core import x\n")
    write(site / "keep" / "core.py", "x = 1\n")
    write(site / "keep" / "extra.py", "y = 2\n")
    write(site / "drop" / "__init__.py", "")
    write(site / "drop" / "a.py", "")
    write(site / "drop" / "b.py", "")
    code = tmp_path / "code"
    write(code / "app.py", "import keep\n")
    return analyze([code], tmp_path / "venv")


def test_tree_structure(analysis):
    root = build_tree(analysis)
    assert set(root.children) == {"keep", "drop"}
    assert root.children["drop"].fully_prunable
    assert not root.children["keep"].fully_prunable
    assert root.children["keep"].counts == (1, 3)


def test_render_marks_and_collapse(analysis):
    out = render_tree(analysis, max_depth=4, color=False)
    assert "- drop" in out
    assert "(3 modules, all prunable)" in out, "a wholly prunable package collapses"
    assert "- extra" in out
    assert "4/6 modules prunable" in out


def test_render_only_prunable_hides_clean_branches(analysis):
    out = render_tree(analysis, max_depth=4, only_prunable=True, color=False)
    assert "drop" in out
    assert "core" not in out, "keep.core is reachable, so it is hidden"
    assert "extra" in out, "keep.extra is prunable, so its branch stays"


def test_depth_limit(analysis):
    out = render_tree(analysis, max_depth=1, color=False)
    assert "… 2 more" in out
    assert "core" not in out


def test_color_escapes(analysis):
    assert "\033[31m" in render_tree(analysis, color=True)
    assert "\033[" not in render_tree(analysis, color=False)
