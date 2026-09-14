"""Orchestration: index, scan, resolve, and classify into a prune report."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from venvprune import progress
from venvprune.analysis.graph import ModuleGraph, Options
from venvprune.model import (
    Distribution,
    DynamicHint,
    DynamicKind,
    EdgeKind,
    ImportEdge,
    ModuleInfo,
    Origin,
    Reachability,
)
from venvprune.scan import astscan, discovery, native, projectmeta
from venvprune.scan.projectmeta import canonical
from venvprune.scan.trace import TraceResult, site_relative_names


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

    kept_by_config: set[str] = field(default_factory=set)
    """Modules forced to survive by a keep pattern."""

    unmatched_keep: set[str] = field(default_factory=set)
    """Keep patterns that matched nothing, so a stale entry can be reported."""

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

    def extension_modules(self) -> dict[str, ModuleInfo]:
        return native.extension_modules(self.modules)

    def unused_bytes(self) -> int:
        return sum(p.stat().st_size for i in self.unused() for p in (i.path, *i.shadowed) if p.is_file())

    def plugin_distributions(self) -> set[str]:
        """Distributions advertising any entry point, i.e. discoverable without an import.

        A console script's launcher imports the package; a `pytest11`, `flake8.extension` or
        application-defined group is loaded by a framework scanning metadata. Either way the
        import graph cannot see it, so removing the modules silently breaks whatever looks the
        distribution up -- pytest-asyncio simply stops collecting async tests.
        """
        return {projectmeta.canonical(name) for name, dist in self.distributions.items() if dist.entry_points}

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
    reporter: progress.Reporter | None = None,
) -> Analysis:
    options = options or Options()
    reporter = reporter or progress.NullReporter()
    site_dirs = discovery.find_site_packages(venv)
    if not site_dirs:
        raise ValueError(f"no site-packages directory found under {venv}")

    index_task = reporter.task("Indexing modules", total=None)
    site_modules, dists = discovery.index_venv(site_dirs, index_task)
    local_modules = discovery.index_code_roots(code_roots, index_task)
    index_task.done()

    site_paths = {d.resolve() for d in site_dirs}
    merged: dict[str, ModuleInfo] = dict(site_modules)
    for name, info in local_modules.items():
        # Code roots that live inside the venv are analysed as local entry points.
        if name in merged and info.path.resolve().parent in site_paths:
            continue
        merged[name] = info

    scan_task = reporter.task("Parsing", total=len(merged))
    astscan.scan_all(merged, scan_task)
    scan_task.done()
    if options.scan_binaries:
        _add_binary_edges(merged, reporter)
    graph = ModuleGraph(merged, discovery.stdlib_module_names(), options)

    dist_of = _map_modules_to_dists(merged, dists)
    kept_by_config, unmatched_keep = expand_keep(list(options.keep), merged, dist_of)

    walk_task = reporter.task("Resolving imports", total=None)
    roots = graph.local_roots() + [r for r in options.extra_roots if r in merged]
    roots += _entry_point_roots(merged, dists, options)
    roots += [name for name in discovery.pth_imports(site_dirs) if name in merged]
    roots += sorted(kept_by_config)
    reach = graph.reachable(roots, options)
    walk_task.advance(len(reach.reached))
    walk_task.done()

    if options.keep_main_modules:
        _keep_main_modules(graph, reach, options)

    traced: set[str] = set()
    if trace is not None:
        traced = site_relative_names(trace, site_dirs)
        for name in traced:
            for parent in _self_and_parents(name):
                if parent in merged:
                    reach.reached.setdefault(parent, EdgeKind.EAGER)

    analysis = Analysis(
        graph=graph,
        reach=reach,
        options=options,
        site_dirs=site_dirs,
        distributions=dists,
        traced=traced,
        dist_of=dist_of,
        kept_by_config=kept_by_config,
        unmatched_keep=unmatched_keep,
    )

    if options.prune_dev_groups is not None:
        _apply_dev_prune(analysis, code_roots, venv, options)
    return analysis


def _add_binary_edges(modules: dict[str, ModuleInfo], reporter: progress.Reporter) -> None:
    """Give each extension module the imports its string table reveals, as lazy edges."""
    names = set(modules)
    by_package: dict[str, set[str]] = {}
    for module in modules:
        parent, _, leaf = module.rpartition(".")
        if parent:
            by_package.setdefault(parent, set()).add(leaf)
    extensions = native.extension_modules(modules)
    task = reporter.task("Scanning binaries", total=len(extensions))
    for name, info in extensions.items():
        task.advance()
        package, _, leaf = name.rpartition(".")
        siblings = by_package.get(package, set()) - {leaf}
        found = native.imports_from_binary(info.path, names, package, siblings) - {name}
        info.edges = [ImportEdge(name, target, EdgeKind.LAZY, 0, 0) for target in sorted(found) if target != name]


def expand_keep(
    patterns: list[str], modules: Mapping[str, ModuleInfo], dist_of: Mapping[str, str]
) -> tuple[set[str], set[str]]:
    """Resolve keep patterns against module and distribution names.

    A pattern may name a module (`numpy.linalg`), a whole subtree (`numpy.*`), or a
    distribution (`tqt-plugin-license`). Returns (matched modules, patterns that matched
    nothing) so a stale keep entry can be reported rather than silently ignored.
    """
    kept: set[str] = set()
    unmatched: set[str] = set()
    canonical_patterns = {canonical(p): p for p in patterns}
    for pattern in patterns:
        hits = {name for name in modules if name == pattern or fnmatch.fnmatchcase(name, pattern)}
        if not hits:
            wanted = canonical(pattern)
            hits = {name for name, dist in dist_of.items() if canonical(dist) == wanted}
        if hits:
            kept |= hits
        else:
            unmatched.add(canonical_patterns.get(canonical(pattern), pattern))
    return kept, unmatched


def _keep_main_modules(graph: ModuleGraph, reach: Reachability, options: Options) -> None:
    """`python -m pkg` runs pkg.__main__, which nothing imports, so walk it from its package."""
    mains = [name for name in graph.modules if name.endswith(".__main__") and name.rpartition(".")[0] in reach.reached]
    if not mains:
        return
    extra = graph.reachable(mains, options)
    for name, kind in extra.reached.items():
        reach.reached.setdefault(name, kind)
    reach.unresolved.extend(extra.unresolved)


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
        if owner in analysis.dev_only and name in analysis.reach.reached and name not in analysis.kept_by_config
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
