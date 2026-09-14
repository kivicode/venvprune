"""The package layers follow the pipeline, and nothing imports upwards.

    model, progress   shared types and reporting, depend on nothing
    scan              reads the world: filesystem, source, binaries, a live process
    analysis          decides what is reachable
    render, edit      present the answer, or act on it
    cli               wires it together

A module may import its own layer or a lower one, never a higher one. Keeping that true is
what stops the graph, the scanners and the rewriters from growing into each other.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

LAYERS = {
    "model": 0,
    "progress": 0,
    "scan": 1,
    "analysis": 2,
    "render": 3,
    "edit": 3,
    "config": 3,
    "cli": 4,
}

ROOT = Path(__file__).resolve().parent.parent / "venvprune"


def _layer(module: str) -> int | None:
    """`venvprune.scan.astscan` -> the rank of `scan`."""
    parts = module.split(".")
    return LAYERS.get(parts[1]) if len(parts) > 1 else None


def _own_layer(path: Path) -> int:
    parts = path.relative_to(ROOT).parts
    key = parts[0] if len(parts) > 1 else parts[0].removesuffix(".py")
    return LAYERS.get(key, 0)


def _internal_imports(tree: ast.Module) -> list[str]:
    """Every `venvprune.*` module an import statement pulls in, however it is spelled."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("venvprune"):
            if node.module == "venvprune":
                # `from venvprune import progress` names the submodule in the alias list.
                out.extend(f"venvprune.{a.name}" for a in node.names)
            else:
                out.append(node.module)
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names if a.name.startswith("venvprune"))
    return out


def _sources() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.py") if p.name != "__init__.py")


def test_every_module_is_placed_in_a_known_layer():
    for path in _sources():
        parts = path.relative_to(ROOT).parts
        key = parts[0] if len(parts) > 1 else parts[0].removesuffix(".py")
        assert key in LAYERS, f"{path.relative_to(ROOT)} sits outside the declared layers"


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_module_imports_a_higher_layer(path: Path):
    mine = _own_layer(path)
    for module in _internal_imports(ast.parse(path.read_text(encoding="utf-8"))):
        theirs = _layer(module)
        if theirs is None:
            continue
        assert theirs <= mine, f"{path.relative_to(ROOT)} (layer {mine}) imports {module} (layer {theirs})"


def test_the_public_api_is_importable():
    import venvprune

    assert set(venvprune.__all__) == {"Analysis", "ModuleGraph", "Options", "analyze"}
    for name in venvprune.__all__:
        assert hasattr(venvprune, name)
    assert venvprune.__version__.count(".") == 2


def test_the_shared_layer_stays_dependency_free():
    """`model` and `progress` are imported by everything, so they may import nothing back."""
    for name in ("model.py", "progress.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        assert not _internal_imports(tree), f"{name} must not depend on the rest of the package"
