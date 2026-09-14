"""Orchestration: index, scan, resolve, and classify into a prune report."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from venvprune import astscan, discovery
from venvprune.graph import ModuleGraph, Options
from venvprune.model import Distribution, DynamicHint, EdgeKind, ModuleInfo, Origin, Reachability
from venvprune.trace import TraceResult, site_relative_names


@dataclass
class Analysis:
    graph: ModuleGraph
    reach: Reachability
    options: Options
    site_dirs: list[Path]
    distributions: Mapping[str, Distribution]
    traced: set[str] = field(default_factory=set)

    @property
    def modules(self) -> dict[str, ModuleInfo]:
        return self.graph.modules

    def unused(self) -> list[ModuleInfo]:
        return self.graph.unused_site_modules(self.reach)

    def kept(self, kind: EdgeKind) -> list[str]:
        return sorted(n for n, k in self.reach.reached.items() if k is kind and self._is_site(n))

    def _is_site(self, name: str) -> bool:
        info = self.modules.get(name)
        return info is not None and info.origin is Origin.SITE

    def dynamic_hints(self, reached_only: bool = True) -> list[DynamicHint]:
        hints: list[DynamicHint] = []
        for name, info in self.modules.items():
            if info.origin is Origin.STDLIB:
                continue
            if reached_only and name not in self.reach.reached:
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
    reach = graph.reachable(roots, options)

    traced: set[str] = set()
    if trace is not None:
        traced = site_relative_names(trace, site_dirs)
        for name in traced:
            for parent in _self_and_parents(name):
                if parent in merged:
                    reach.reached.setdefault(parent, EdgeKind.EAGER)

    return Analysis(
        graph=graph,
        reach=reach,
        options=options,
        site_dirs=site_dirs,
        distributions=dists,
        traced=traced,
    )


def _self_and_parents(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]
