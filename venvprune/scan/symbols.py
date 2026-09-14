"""Intra-module liveness: which top-level definitions a module actually has to keep.

Module-level pruning stops at the file boundary. A module that survives because one function
in it is needed still ships every other function, class and constant it defines — and every
import those pull in. This builds a symbol table per module so the graph can ask "given that
only these names are wanted, what inside this file is still live?".

Everything here errs towards keeping code: an unresolvable reference, an unrecognised
decorator, or any sign of runtime name lookup marks the definition (or the whole module) as
untouchable rather than guessing.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import StrEnum

from venvprune.model import Demand

_PURE_DECORATORS = {
    "abstractmethod",
    "abstractproperty",
    "cache",
    "cached_property",
    "classmethod",
    "dataclass",
    "final",
    "lru_cache",
    "overload",
    "override",
    "property",
    "runtime_checkable",
    "staticmethod",
    "total_ordering",
    "wraps",
}

_SAFE_BASES = {"object", "Exception", "BaseException", "Protocol", "Enum", "StrEnum", "IntEnum", "TypedDict"}

_NAME_LOOKUP_CALLS = {"eval", "exec", "globals", "locals", "vars", "compile"}


class DefKind(StrEnum):
    FUNCTION = "function"
    CLASS = "class"
    ASSIGN = "assign"
    IMPORT = "import"


class Tier(StrEnum):
    SAFE = "safe"
    """Nothing suggests the name is reached other than by being referenced."""

    RISKY = "risky"
    """Could be reached by a registry, a decorator side effect, or a subclass hook."""


@dataclass(frozen=True)
class Definition:
    name: str
    kind: DefKind
    lineno: int
    end_lineno: int
    refs: frozenset[str]
    tier: Tier = Tier.SAFE
    reason: str = ""

    @property
    def span(self) -> range:
        return range(self.lineno - 1, self.end_lineno)


@dataclass
class SymbolTable:
    definitions: dict[str, list[Definition]] = field(default_factory=dict)
    side_effect_refs: frozenset[str] = frozenset()
    """Names used by module-level statements that must run, so they can never be removed."""

    prunable: bool = True
    unsafe_reason: str = ""
    exported: tuple[str, ...] | None = None
    statements: dict[int, ast.stmt] = field(default_factory=dict)
    """Import and `__all__` statements by start line, so a rewrite can rebuild them narrowed."""

    pinned: frozenset[str] = frozenset()
    """Names that must never be removed, whatever references them."""

    def names(self) -> set[str]:
        return set(self.definitions)

    def all_definitions(self) -> list[Definition]:
        return [d for defs in self.definitions.values() for d in defs]


def build_table(tree: ast.Module, hints_are_dynamic: bool = False) -> SymbolTable:
    table = SymbolTable()
    side_effects: set[str] = set()
    pinned: set[str] = set()
    unsafe = ""

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            table.definitions.setdefault(node.name, []).append(_function_def(node))
        elif isinstance(node, ast.ClassDef):
            table.definitions.setdefault(node.name, []).append(_class_def(node, table))
        elif isinstance(node, ast.Import | ast.ImportFrom):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                # Dropping a __future__ import changes how the rest of the file compiles.
                pinned.update(a.asname or a.name for a in node.names)
            table.statements[node.lineno] = node
            for definition in _import_defs(node):
                table.definitions.setdefault(definition.name, []).append(definition)
        elif (simple := _simple_assign(node)) is not None:
            if any(d.name == "__all__" for d in simple):
                table.statements[node.lineno] = node
                table.exported = _all_strings(node)
            for definition in simple:
                table.definitions.setdefault(definition.name, []).append(definition)
        else:
            side_effects |= _free_names(node)

    table.side_effect_refs = frozenset(side_effects)
    table.pinned = frozenset(pinned)
    if "__getattr__" in table.definitions:
        unsafe = "module defines __getattr__, so any name may be produced on demand"
    elif hints_are_dynamic:
        unsafe = "module performs dynamic imports"
    else:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in _NAME_LOOKUP_CALLS:
                    unsafe = f"module calls {node.func.id}(), which can reach any name"
                    break
            elif isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                unsafe = "module uses a star import, so its namespace is not enumerable"
                break
    table.prunable = not unsafe
    table.unsafe_reason = unsafe
    return table


def _all_strings(node: ast.stmt) -> tuple[str, ...] | None:
    value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
    if not isinstance(value, ast.List | ast.Tuple):
        return None
    items = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return tuple(items) if len(items) == len(value.elts) else None


def _function_def(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Definition:
    tier, reason = _decorator_tier(node.decorator_list)
    return Definition(
        name=node.name,
        kind=DefKind.FUNCTION,
        lineno=_start_line(node),
        end_lineno=node.end_lineno or node.lineno,
        refs=frozenset(_free_names(node)),
        tier=tier,
        reason=reason,
    )


def _class_def(node: ast.ClassDef, table: SymbolTable) -> Definition:
    tier, reason = _decorator_tier(node.decorator_list)
    if tier is Tier.SAFE:
        for base in [*node.bases, *(kw.value for kw in node.keywords)]:
            name = _head_name(base)
            if name is None:
                tier, reason = Tier.RISKY, "class has a computed base"
                break
            local_class = any(d.kind is DefKind.CLASS for d in table.definitions.get(name, ()))
            if name not in _SAFE_BASES and not local_class:
                # A foreign base may register subclasses via __init_subclass__ or a metaclass.
                tier, reason = Tier.RISKY, f"class derives from {name}, which may register subclasses"
                break
    return Definition(
        name=node.name,
        kind=DefKind.CLASS,
        lineno=_start_line(node),
        end_lineno=node.end_lineno or node.lineno,
        refs=frozenset(_free_names(node)),
        tier=tier,
        reason=reason,
    )


def _import_defs(node: ast.Import | ast.ImportFrom) -> list[Definition]:
    out: list[Definition] = []
    for alias in node.names:
        if alias.name == "*":
            continue
        bound = alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
        out.append(
            Definition(
                name=bound,
                kind=DefKind.IMPORT,
                lineno=node.lineno,
                end_lineno=node.end_lineno or node.lineno,
                refs=frozenset(),
            )
        )
    return out


def _simple_assign(node: ast.stmt) -> list[Definition] | None:
    """Only plain `NAME = expr` / `NAME: T = expr` bindings; anything else is a side effect."""
    if isinstance(node, ast.Assign):
        targets = node.targets
        value = node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets = [node.target]
        value = node.value
    else:
        return None
    names = [t.id for t in targets if isinstance(t, ast.Name)]
    if not names or len(names) != len(targets):
        return None
    annotation = _annotation(node)
    refs = frozenset(_free_names(value) | (_free_names(annotation) if annotation is not None else set()))
    return [
        Definition(
            name=name,
            kind=DefKind.ASSIGN,
            lineno=node.lineno,
            end_lineno=node.end_lineno or node.lineno,
            refs=refs,
        )
        for name in names
    ]


def _annotation(node: ast.stmt) -> ast.expr | None:
    return node.annotation if isinstance(node, ast.AnnAssign) else None


def _decorator_tier(decorators: list[ast.expr]) -> tuple[Tier, str]:
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = _leaf_name(target)
        if name not in _PURE_DECORATORS:
            # A decorator that is not a known no-op may register the object somewhere.
            return Tier.RISKY, f"decorated with @{name or '<expr>'}, which may register it"
    return Tier.SAFE, ""


def _start_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def _leaf_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _head_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Call):
        return _head_name(node.func)
    return node.id if isinstance(node, ast.Name) else None


def _free_names(node: ast.AST) -> set[str]:
    """Every name a statement could read. Scoping is ignored on purpose: over-keeping is safe."""
    out: set[str] = _annotation_names(node)
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            # A store-only name is a binding, not a read: `a, b = pair` does not use `b`.
            if isinstance(child.ctx, ast.Load | ast.Del):
                out.add(child.id)
        elif isinstance(child, ast.AugAssign) and isinstance(child.target, ast.Name):
            # `x += 1` reads x before writing it, but the target still carries a Store context.
            out.add(child.target.id)
        elif isinstance(child, ast.Attribute) and (head := _head_name(child)) is not None:
            out.add(head)
    return out


def _annotation_names(node: ast.AST) -> set[str]:
    """Names hidden in string annotations, which typing.get_type_hints resolves later."""
    out: set[str] = set()
    for child in ast.walk(node):
        annotations: list[ast.expr | None] = []
        if isinstance(child, ast.AnnAssign):
            annotations.append(child.annotation)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            annotations.append(child.returns)
            annotations.extend(a.annotation for a in ast.walk(child.args) if isinstance(a, ast.arg))
        for annotation in annotations:
            if annotation is None:
                continue
            for leaf in ast.walk(annotation):
                if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                    out |= _names_in_string_annotation(leaf.value)
    return out


def _names_in_string_annotation(text: str) -> set[str]:
    if len(text) > 200 or not text or text[0].isdigit():
        return set()
    candidate = text.split("[")[0].split(".")[0].strip()
    return {candidate} if candidate.isidentifier() else set()


def live_symbols(table: SymbolTable, demand: set[str] | Demand, include_risky: bool = False) -> set[str]:
    """Names that must stay, given what the outside world asks of this module."""
    if demand is Demand.ALL or not table.prunable:
        return table.names()

    live: set[str] = set(table.side_effect_refs) | set(table.pinned)
    live |= {n for n in demand if isinstance(demand, set)}
    for name, defs in table.definitions.items():
        if (name.startswith("__") and name.endswith("__")) or (
            not include_risky and any(d.tier is Tier.RISKY for d in defs)
        ):
            live.add(name)

    queue = [n for n in live if n in table.definitions]
    seen: set[str] = set()
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        for definition in table.definitions.get(name, ()):
            for ref in definition.refs:
                live.add(ref)
                if ref in table.definitions and ref not in seen:
                    queue.append(ref)
    return live


def dead_definitions(table: SymbolTable, demand: set[str] | Demand, include_risky: bool = False) -> list[Definition]:
    """Top-level definitions nothing needs, ready to be cut out of the file."""
    if not table.prunable or demand is Demand.ALL:
        return []
    live = live_symbols(table, demand, include_risky)
    out = [
        definition
        for name, defs in table.definitions.items()
        if name not in live
        for definition in defs
        if definition.kind is not DefKind.IMPORT
    ]
    return sorted(out, key=lambda d: d.lineno)
