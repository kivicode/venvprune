"""Orchestration: index, scan, resolve, and classify into a prune report."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from venvprune import astscan, discovery, projectmeta
from venvprune.graph import ModuleGraph, Options
from venvprune.model import Distribution, DynamicHint, DynamicKind, EdgeKind, ModuleInfo, Origin, Reachability
from venvprune.trace import TraceResult, site_relative_names


@dataclass
class Analysis:
    graph: ModuleGraph
    reach: Reachability
    options: Options
    site_dirs: list[Path]
    distributions: Mapping[str, Distribution]
    traced: set[str] = field(default_factory=set)
    dist_of: Mapping[str, str] = field(default_factory=dict)
    """Module name -> owning distribution (canonical name)."""

    dev_only: set[str] = field(default_factory=set)
    """Distributions declared only in a dev group, minus those the code imports directly."""

    dev_forced: set[str] = field(default_factory=set)
    """Modules pruned because they belong to a dev-only distribution, despite being reachable."""

    project: projectmeta.ProjectMeta | None = None

    @property
    def modules(self) -> dict[str, ModuleInfo]:
        return self.graph.modules

    def unused(self) -> list[ModuleInfo]:
        base = self.graph.unused_site_modules(self.reach)
        if not self.dev_forced:
            return base
        extra = [self.modules[n] for n in self.dev_forced if n in self.modules]
        return sorted({i.name: i for i in [*base, *extra]}.values(), key=lambda i: i.name)

    def kept(self, kind: EdgeKind) -> list[str]:
        return sorted(
            n for n, k in self.reach.reached.items() if k is kind and self._is_site(n) and n not in self.dev_forced
        )

    def _is_site(self, name: str) -> bool:
        info = self.modules.get(name)
        return info is not None and info.origin is Origin.SITE

    def dynamic_hints(self, reached_only: bool = True) -> list[DynamicHint]:
        hints: list[DynamicHint] = []
        for name, info in self.modules.items():
            if info.origin is Origin.STDLIB:
                continue
            if reached_only and (name not in self.reach.reached or name in self.dev_forced):
                continue
            hints.extend(info.hints)
        return sorted(hints, key=lambda h: (h.module, h.lineno))

    def unused_bytes(self) -> int:
        return sum(i.path.stat().st_size for i in self.unused() if i.path.exists() and i.path.is_file())

    def fully_unused_distributions(self) -> list[str]:
        unused = {i.name for i in self.unused()}
        out = []
        for dist_name, dist in self.distributions.items():
            tops = dist.top_level
            if not tops:
                continue
            owned = [m for m in self.modules if m.split(".")[0] in tops and self._is_site(m)]
            if owned and all(m in unused for m in owned):
                out.append(dist_name)
        return sorted(out)


def analyze(
    code_roots: list[Path],
    venv: Path,
    options: Options | None = None,
    trace: TraceResult | None = None,
) -> Analysis:
    options = options or Options()
    site_dirs = discovery.find_site_packages(venv)
    if not site_dirs:
        raise ValueError(f"no site-packages directory found under {venv}")

    site_modules, dists = discovery.index_venv(site_dirs)
    local_modules = discovery.index_code_roots(code_roots)

    site_paths = {d.resolve() for d in site_dirs}
    merged: dict[str, ModuleInfo] = dict(site_modules)
    for name, info in local_modules.items():
        # Code roots that live inside the venv are analysed as local entry points.
        if name in merged and info.path.resolve().parent in site_paths:
            continue
        merged[name] = info

    astscan.scan_all(merged)
    graph = ModuleGraph(merged, discovery.stdlib_module_names())

    roots = graph.local_roots() + [r for r in options.extra_roots if r in merged]
    roots += _entry_point_roots(merged, dists, options)
    reach = graph.reachable(roots, options)

    traced: set[str] = set()
    if trace is not None:
        traced = site_relative_names(trace, site_dirs)
        for name in traced:
            for parent in _self_and_parents(name):
                if parent in merged:
                    reach.reached.setdefault(parent, EdgeKind.EAGER)

    dist_of = _map_modules_to_dists(merged, dists)
    analysis = Analysis(
        graph=graph,
        reach=reach,
        options=options,
        site_dirs=site_dirs,
        distributions=dists,
        traced=traced,
        dist_of=dist_of,
    )

    if options.prune_dev_groups is not None:
        _apply_dev_prune(analysis, code_roots, venv, options)
    return analysis


def _entry_point_roots(
    modules: Mapping[str, ModuleInfo], dists: Mapping[str, Distribution], options: Options
) -> list[str]:
    """Modules advertised by the entry-point groups the code is known to load."""
    groups = set(options.entry_point_groups)
    for info in modules.values():
        if info.origin is not Origin.LOCAL:
            continue
        groups.update(value for hint in info.hints if hint.kind is DynamicKind.ENTRY_POINTS for value in hint.values)
    if not groups:
        return []
    roots: list[str] = []
    for dist in dists.values():
        for group, entries in dist.entry_points.items():
            if group not in groups:
                continue
            for target in entries.values():
                module = target.split(":", 1)[0].strip()
                if module in modules:
                    roots.append(module)
    return roots


def _map_modules_to_dists(modules: Mapping[str, ModuleInfo], dists: Mapping[str, Distribution]) -> dict[str, str]:
    """Resolve ownership by file path, since two dists can share a top-level namespace."""
    by_path: dict[Path, str] = {}
    for name, dist in dists.items():
        canon = projectmeta.canonical(name)
        for file in dist.files:
            by_path[file] = canon
    out: dict[str, str] = {}
    for name, info in modules.items():
        if info.origin is not Origin.SITE:
            continue
        owner = by_path.get(info.path.resolve())
        if owner is None:
            top = name.split(".")[0]
            owner = next(
                (projectmeta.canonical(d) for d, dist in dists.items() if top in dist.top_level),
                None,
            )
        if owner is not None:
            out[name] = owner
    return out


def _apply_dev_prune(analysis: Analysis, code_roots: list[Path], venv: Path, options: Options) -> None:
    pyproject = options.pyproject or next(
        (p for root in [*code_roots, venv.parent] if (p := projectmeta.find_pyproject(root))), None
    )
    if pyproject is None:
        return
    meta = projectmeta.read_project(pyproject)
    analysis.project = meta
    dev_only = meta.dev_only(options.prune_dev_groups or projectmeta.DEFAULT_DEV_GROUPS)
    protected = _dists_imported_by_local_code(analysis)
    analysis.dev_only = dev_only - protected
    analysis.dev_forced = {
        name
        for name, owner in analysis.dist_of.items()
        if owner in analysis.dev_only and name in analysis.reach.reached
    }


def _dists_imported_by_local_code(analysis: Analysis) -> set[str]:
    """Distributions named by an import statement written in the user's own code."""
    protected: set[str] = set()
    for info in analysis.modules.values():
        if info.origin is not Origin.LOCAL:
            continue
        for edge in info.edges:
            for candidate in _self_and_parents(edge.target):
                owner = analysis.dist_of.get(candidate)
                if owner is not None:
                    protected.add(owner)
                    break
    return protected


def _self_and_parents(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]
