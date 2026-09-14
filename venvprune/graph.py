"""Import resolution and reachability over the combined local+venv module index."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from venvprune import symbols
from venvprune.model import (
    ArgShape,
    Demand,
    DynamicHint,
    DynamicKind,
    EdgeKind,
    ImportEdge,
    ModuleInfo,
    Origin,
    Reachability,
)
from venvprune.symbols import SymbolTable

_CERTAINTY = {
    EdgeKind.EAGER: 3,
    EdgeKind.REEXPORT: 2,
    EdgeKind.LAZY: 1,
    EdgeKind.TYPE_ONLY: 0,
}


@dataclass
class Options:
    follow_lazy: bool = True
    follow_type_only: bool = False
    follow_reexport: bool = True
    dynamic_expands_package: bool = True
    """A reached module with dynamic-import hints keeps its whole sibling subtree."""

    extra_roots: list[str] = field(default_factory=list)

    prune_dev_groups: tuple[str, ...] | None = None
    """Dev dependency groups to prune wholesale; `None` disables dev-group pruning."""

    pyproject: Path | None = None

    keep: tuple[str, ...] = ()
    """Patterns for modules or distributions that must survive whatever the graph says."""

    entry_point_groups: tuple[str, ...] = ()
    """Entry-point groups whose advertised modules count as roots."""

    scan_binaries: bool = False
    """Recover imports embedded in compiled extension modules by scanning their strings."""

    strict_dynamic: bool = False
    """Keep nothing for a dynamic site that cannot be bounded, instead of its whole package."""

    symbol_precision: bool = False
    """Follow a package `__init__`'s re-export only when the name it binds is actually used."""

    prune_definitions: bool = False
    """Extend precision inside each module: an import survives only if a live definition uses it."""

    include_risky_definitions: bool = False
    """Also cut definitions that a decorator or foreign base class might register."""


class ModuleGraph:
    def __init__(self, modules: dict[str, ModuleInfo], stdlib: frozenset[str], options: Options | None = None) -> None:
        self.modules = modules
        self.stdlib = stdlib
        self.options = options or Options()

    def resolve(self, edge: ImportEdge) -> tuple[list[str], bool]:
        """Return (module names this edge requires, whether anything resolved)."""
        out: list[str] = []
        target = edge.target
        if target in self.modules:
            out.extend(self._with_parents(target))
        elif self._is_stdlib(target):
            return [], True
        if edge.is_from:
            for name in edge.names:
                if name == "*":
                    continue
                sub = f"{target}.{name}"
                if sub in self.modules:
                    out.extend(self._with_parents(sub))
        return out, bool(out) or self._is_stdlib(target)

    def _is_stdlib(self, dotted: str) -> bool:
        return dotted.split(".")[0] in self.stdlib

    def _with_parents(self, name: str) -> list[str]:
        """A submodule cannot be imported without every parent package's `__init__`."""
        parts = name.split(".")
        return [".".join(parts[: i + 1]) for i in range(len(parts)) if ".".join(parts[: i + 1]) in self.modules]

    def subtree(self, name: str) -> list[str]:
        prefix = f"{name}."
        return [m for m in self.modules if m == name or m.startswith(prefix)]

    def reachable(self, roots: list[str], opts: Options) -> Reachability:
        self.options = opts
        allowed = {EdgeKind.EAGER}
        if opts.follow_lazy:
            allowed.add(EdgeKind.LAZY)
        if opts.follow_type_only:
            allowed.add(EdgeKind.TYPE_ONLY)
        if opts.follow_reexport:
            allowed.add(EdgeKind.REEXPORT)

        reached: dict[str, EdgeKind] = {}
        why: dict[str, ImportEdge] = {}
        unresolved: list[ImportEdge] = []
        demand: dict[str, set[str] | Demand] = {}
        skipped: dict[str, list[ImportEdge]] = {}
        queue: deque[tuple[str, EdgeKind]] = deque()

        def want(target: str, asked: set[str] | Demand) -> bool:
            """Widen a module's demand set; True when it grew (so it must be re-walked)."""
            current = demand.get(target)
            if current is Demand.ALL:
                return False
            if asked is Demand.ALL:
                demand[target] = Demand.ALL
                return True
            if current is None:
                demand[target] = set(asked)
                return True
            if asked - current:
                current |= asked
                return True
            return False

        for root in roots:
            if root in self.modules:
                reached[root] = EdgeKind.EAGER
                demand[root] = Demand.ALL
                queue.append((root, EdgeKind.EAGER))

        while queue:
            name, incoming = queue.popleft()
            info = self.modules.get(name)
            if info is None:
                continue
            wanted = demand.get(name, Demand.ALL)
            targets: list[tuple[str, EdgeKind, ImportEdge | None, set[str] | Demand]] = []
            for edge in info.edges:
                if edge.kind not in allowed:
                    continue
                if (opts.symbol_precision or opts.prune_definitions) and not self._edge_is_wanted(info, edge, wanted):
                    skipped.setdefault(name, []).append(edge)
                    continue
                names, ok = self.resolve(edge)
                if not ok:
                    unresolved.append(edge)
                # An eager import inside a lazily-reached module is still only lazy overall.
                kind = edge.kind if _CERTAINTY[edge.kind] < _CERTAINTY[incoming] else incoming
                for target in names:
                    targets.append((target, kind, edge, self._demand_for(info, edge, target, opts, wanted)))
            if opts.dynamic_expands_package:
                for candidate in self.dynamic_targets(info, opts):
                    targets.append((candidate, EdgeKind.LAZY, None, Demand.ALL))

            for target, kind, edge, asked in targets:
                grew = want(target, asked)
                previous = reached.get(target)
                stronger = previous is None or _CERTAINTY[previous] < _CERTAINTY[kind]
                if not stronger and not grew:
                    continue
                if stronger:
                    reached[target] = kind
                    if edge is not None:
                        why.setdefault(target, edge)
                queue.append((target, reached[target]))

        return Reachability(reached=reached, why=why, unresolved=unresolved, demand=demand, skipped_reexports=skipped)

    def dynamic_targets(self, info: ModuleInfo, opts: Options) -> set[str]:
        """Modules that a module's dynamic-import sites could plausibly load."""
        out: set[str] = set()
        for hint in info.hints:
            if hint.kind is DynamicKind.ENTRY_POINTS:
                continue
            resolved = self.resolve_hint(info, hint)
            if resolved is None:
                if opts.strict_dynamic:
                    continue
                out.update(self.subtree(_package_of(info)))
            else:
                out.update(resolved)
        return out

    def resolve_hint(self, info: ModuleInfo, hint: DynamicHint) -> set[str] | None:
        """Candidate modules for one dynamic site, or None when it cannot be bounded."""
        anchor = hint.package_arg or _package_of(info)
        if hint.kind is DynamicKind.GETATTR_MODULE:
            # `getattr(pkg, name)` can only reach what is already under that package.
            return set(self.subtree(_package_of(info)))
        if hint.kind is DynamicKind.PKGUTIL:
            return set(self.subtree(_package_of(info)))
        if not hint.bounded:
            return None
        out: set[str] = set()
        for value in hint.values:
            absolute = self._absolutise(value, anchor)
            if hint.shape is ArgShape.PREFIX:
                out.update(m for m in self.modules if m.startswith(absolute))
            elif absolute in self.modules:
                out.update(self._with_parents(absolute))
        if hint.shape is ArgShape.PREFIX and not out:
            return None
        return out

    def _absolutise(self, value: str, anchor: str) -> str:
        if not value.startswith("."):
            return value
        stripped = value.lstrip(".")
        level = len(value) - len(stripped)
        parts = anchor.split(".") if anchor else []
        base = parts[: len(parts) - (level - 1)] if level > 1 else parts
        return ".".join([*base, *([stripped] if stripped else [])])

    def _edge_is_wanted(self, info: ModuleInfo, edge: ImportEdge, wanted: set[str] | Demand) -> bool:
        """Under symbol precision, an import survives only if something live still needs it."""
        if wanted is Demand.ALL:
            return True
        if not edge.bindings:
            return True
        # `__future__` changes how the file compiles, and a self/parent import is part of the
        # package's own structure: neither is a re-export that can be dropped.
        if edge.target == "__future__" or edge.target == info.name or info.name.startswith(f"{edge.target}."):
            return True
        live = self.live_names(info, wanted)
        if live is None:
            return True
        return any(b.local in live for b in edge.bindings)

    def live_names(self, info: ModuleInfo, wanted: set[str] | Demand, opts: Options | None = None) -> set[str] | None:
        """Names still live inside a module, or None when it must be kept whole."""
        opts = opts or self.options
        table = info.table
        if isinstance(table, SymbolTable) and not table.prunable:
            # A module that can produce names on demand must keep every one of them.
            return None
        if opts.prune_definitions and isinstance(table, SymbolTable):
            return symbols.live_symbols(table, wanted, opts.include_risky_definitions)
        if info.is_package and isinstance(wanted, set):
            # The narrower pre-phase-2 rule: only an __init__'s re-exports are negotiable.
            return set(wanted) | set(info.used_attrs)
        return None

    def _demand_for(
        self,
        info: ModuleInfo,
        edge: ImportEdge,
        target: str,
        opts: Options,
        wanted: set[str] | Demand = Demand.ALL,
    ) -> set[str] | Demand:
        if not (opts.symbol_precision or opts.prune_definitions):
            return Demand.ALL
        if target != edge.target:
            # A parent package or a submodule pulled in alongside the named target.
            return Demand.ALL if target.startswith(f"{edge.target}.") else set()
        if edge.is_from:
            if "*" in edge.names:
                return Demand.ALL
            live = self.live_names(info, wanted)
            # Ask the target only for the names this module still has a live use for; a submodule
            # is satisfied by importing it, not by anything in the package body.
            attrs = {
                b.remote
                for b in edge.bindings
                if f"{target}.{b.remote}" not in self.modules and (live is None or b.local in live)
            }
            return attrs or set()
        binding = edge.bindings[0].local if edge.bindings else None
        used = info.used_attrs.get(binding) if binding else None
        if used is None or used is Demand.ALL:
            return Demand.ALL
        return set(used)

    def local_roots(self) -> list[str]:
        return [name for name, info in self.modules.items() if info.origin is Origin.LOCAL]

    def unused_site_modules(self, reach: Reachability) -> list[ModuleInfo]:
        return sorted(
            (info for name, info in self.modules.items() if info.origin is Origin.SITE and name not in reach.reached),
            key=lambda i: i.name,
        )


def _package_of(info: ModuleInfo) -> str:
    return info.name if info.is_package else (info.name.rpartition(".")[0] or info.name)
