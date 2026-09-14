"""Filesystem discovery: module indexes for code roots and for a virtualenv."""

from __future__ import annotations

import sys
import sysconfig
import zipfile
from collections.abc import Iterator
from pathlib import Path

from venvprune.model import Distribution, ModuleInfo, Origin

_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    ".nox",
    "node_modules",
}

_SOURCE_SUFFIXES = (".py", ".pyi")
_EXT_SUFFIXES = (".so", ".pyd", ".dylib")


def stdlib_module_names() -> frozenset[str]:
    return frozenset(sys.stdlib_module_names)


def find_site_packages(venv: Path) -> list[Path]:
    """Return every site-packages dir of a venv, newest layout first."""
    candidates: list[Path] = []
    if venv.name == "site-packages":
        return [venv]
    for pattern in ("lib/python*/site-packages", "Lib/site-packages", "lib64/python*/site-packages"):
        candidates.extend(sorted(venv.glob(pattern)))
    # De-duplicate lib64 symlinks pointing at the same real directory.
    seen: dict[Path, Path] = {}
    for c in candidates:
        seen.setdefault(c.resolve(), c)
    return list(seen.values())


def _iter_python_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix in _SOURCE_SUFFIXES or path.name.endswith(_EXT_SUFFIXES):
            yield path


def _module_name(root: Path, path: Path) -> str | None:
    rel = path.relative_to(root)
    parts = list(rel.parts)
    stem = parts[-1]
    for suffix in _EXT_SUFFIXES:
        if stem.endswith(suffix):
            # Native extensions carry an ABI tag: `_speedups.cpython-312-darwin.so`.
            stem = stem[: -len(suffix)].split(".")[0]
            break
    else:
        stem = stem.rsplit(".", 1)[0]
    parts[-1] = stem
    if stem == "__init__":
        parts.pop()
    if not parts:
        return None
    if not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def index_root(root: Path, origin: Origin, prefix: str = "") -> dict[str, ModuleInfo]:
    """Map dotted module name -> ModuleInfo for every module found under `root`."""
    modules: dict[str, ModuleInfo] = {}
    for path in _iter_python_files(root):
        name = _module_name(root, path)
        if name is None:
            continue
        if prefix:
            name = f"{prefix}.{name}"
        is_pkg = path.name.startswith("__init__.")
        stub_only = path.suffix == ".pyi"
        existing = modules.get(name)
        if existing is not None:
            # A real .py always wins over a stub; otherwise first-seen root wins.
            if existing.is_stub_only and not stub_only:
                modules[name] = ModuleInfo(name, path, origin, is_pkg, stub_only)
            continue
        modules[name] = ModuleInfo(name, path, origin, is_pkg, stub_only)

    # Namespace packages (PEP 420) have no __init__.py but must still resolve.
    for name in list(modules):
        parent = name.rpartition(".")[0]
        while parent and parent not in modules:
            dir_path = root / Path(*parent.split(".")[len(prefix.split(".")) if prefix else 0 :])
            modules[parent] = ModuleInfo(parent, dir_path, origin, is_package=True)
            parent = parent.rpartition(".")[0]
    return modules


def index_code_roots(roots: list[Path]) -> dict[str, ModuleInfo]:
    modules: dict[str, ModuleInfo] = {}
    for root in roots:
        # A package dir given directly (`src/mypkg`) is indexed from its parent so
        # that its own name stays part of the dotted path.
        base = root.parent if (root / "__init__.py").exists() else root
        for name, info in index_root(base.resolve(), Origin.LOCAL).items():
            modules.setdefault(name, info)
    return modules


def index_venv(site_dirs: list[Path]) -> tuple[dict[str, ModuleInfo], dict[str, Distribution]]:
    modules: dict[str, ModuleInfo] = {}
    for site in site_dirs:
        for name, info in index_root(site.resolve(), Origin.SITE).items():
            modules.setdefault(name, info)
    return modules, read_distributions(site_dirs)


def read_distributions(site_dirs: list[Path]) -> dict[str, Distribution]:
    """Parse *.dist-info RECORD files without importing anything from the target venv."""
    dists: dict[str, Distribution] = {}
    for site in site_dirs:
        for info_dir in sorted(site.glob("*.dist-info")):
            meta = _read_metadata(info_dir / "METADATA")
            name = meta.get("name") or info_dir.name.split("-")[0]
            dist = dists.setdefault(name, Distribution(name=name, version=meta.get("version", "")))
            for rel in _record_paths(info_dir):
                path = (site / rel).resolve()
                dist.files.append(path)
                top = rel.split("/", 1)[0]
                if top.endswith(".py"):
                    top = top[:-3]
                if top.isidentifier():
                    dist.top_level.add(top)
    return dists


def _read_metadata(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep and key.lower() in {"name", "version"}:
            out[key.lower()] = value.strip()
    return out


def _record_paths(info_dir: Path) -> Iterator[str]:
    record = info_dir / "RECORD"
    if not record.exists():
        return
    for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
        rel = line.split(",", 1)[0].strip()
        if rel and not rel.startswith(".."):
            yield rel


def is_zipapp(path: Path) -> bool:
    return path.is_file() and zipfile.is_zipfile(path)


def current_stdlib_dir() -> Path:
    return Path(sysconfig.get_paths()["stdlib"])
