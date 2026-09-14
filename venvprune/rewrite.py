"""Phase 2: patches that let an incidentally-imported module actually be deleted.

A package's `__init__.py` commonly imports every submodule just to republish names. If the
code only ever uses some of those names, the rest are dead weight — but the submodules cannot
be deleted while the `__init__` still imports them eagerly. These rewrites remove exactly those
statements and leave a `__getattr__` (PEP 562) that raises a clear error if a pruned name is
touched after all, so a mistake surfaces as an explicit message rather than an `ImportError`
from a missing file.
"""

from __future__ import annotations

import ast
import copy
import difflib
from dataclasses import dataclass, field
from pathlib import Path

from venvprune.analyzer import Analysis
from venvprune.model import Demand, ImportEdge
from venvprune.symbols import Definition, SymbolTable, dead_definitions, live_symbols

_SHIM_HEADER = "# --- venvprune: lazily-removed re-exports ---"

_SHIM = """

{header}
_VENVPRUNE_PRUNED = {names!r}


def __getattr__(name):
    if name in _VENVPRUNE_PRUNED:
        raise AttributeError(
            f"{{name!r}} was removed from {module!r} by venvprune because no analysed code used it"
        )
    raise AttributeError(f"module {{__name__!r}} has no attribute {{name!r}}")
"""


@dataclass
class Rewrite:
    module: str
    path: Path
    dropped: list[ImportEdge] = field(default_factory=list)
    original: str = ""
    patched: str = ""

    @property
    def names(self) -> list[str]:
        return sorted({b.local for edge in self.dropped for b in edge.bindings})

    @property
    def unlocked(self) -> list[str]:
        return sorted({edge.target for edge in self.dropped})

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.patched.splitlines(keepends=True),
                fromfile=str(self.path),
                tofile=f"{self.path} (venvprune)",
            )
        )

    def apply(self, backup_suffix: str = ".venvprune-bak") -> None:
        if backup_suffix:
            self.path.with_suffix(self.path.suffix + backup_suffix).write_text(self.original, encoding="utf-8")
        self.path.write_text(self.patched, encoding="utf-8")


def plan_rewrites(analysis: Analysis, shim: bool = True, allow_dynamic: bool = False) -> list[Rewrite]:
    """One Rewrite per package whose `__init__` re-exports nothing anybody asked for.

    Packages that resolve names dynamically are skipped: their re-exports may be read by
    `getattr` or a plugin registry that no AST pass can see.
    """
    out: list[Rewrite] = []
    risky = set() if allow_dynamic else _dynamic_packages(analysis)
    for module, edges in sorted(analysis.reach.skipped_reexports.items()):
        info = analysis.modules.get(module)
        if info is None or not info.path.is_file() or info.path.suffix != ".py":
            continue
        if module in risky:
            continue
        rewrite = _build(module, info.path, edges, shim)
        if rewrite is not None:
            out.append(rewrite)
    return out


def _dynamic_packages(analysis: Analysis) -> set[str]:
    """Top-level packages containing any reachable module that imports dynamically."""
    out: set[str] = set()
    for hint in analysis.dynamic_hints():
        parts = hint.module.split(".")
        out.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    return out


def _build(module: str, path: Path, edges: list[ImportEdge], shim: bool) -> Rewrite | None:
    original = path.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines(keepends=True)
    drop: set[int] = set()
    for edge in edges:
        end = edge.end_lineno or edge.lineno
        drop.update(range(edge.lineno - 1, end))
    if not drop or max(drop) >= len(lines):
        return None

    kept = [line for index, line in enumerate(lines) if index not in drop]
    patched = "".join(kept)
    rewrite = Rewrite(module=module, path=path, dropped=list(edges), original=original)
    if shim and rewrite.names:
        patched = patched.rstrip("\n") + "\n" + _SHIM.format(header=_SHIM_HEADER, names=rewrite.names, module=module)
    rewrite.patched = patched
    return rewrite


def render_plan(rewrites: list[Rewrite], show_diff: bool = False) -> str:
    if not rewrites:
        return "No re-export rewrites available (run with --symbols to find them)."
    lines = [f"{len(rewrites)} package(s) can drop unused re-exports:", ""]
    for rewrite in rewrites:
        lines.append(f"  {rewrite.module}  ({rewrite.path})")
        lines.append(f"      drops {len(rewrite.dropped)} import(s), freeing: {', '.join(rewrite.unlocked)}")
        lines.append(f"      names no longer exported: {', '.join(rewrite.names)}")
        if show_diff:
            lines.append("")
            lines.extend(f"      {line}" for line in rewrite.diff().splitlines())
        lines.append("")
    return "\n".join(lines)


@dataclass
class DefinitionRewrite:
    """Removal of top-level definitions nothing in the analysed program can reach."""

    module: str
    path: Path
    dead: list[Definition] = field(default_factory=list)
    dead_imports: list[Definition] = field(default_factory=list)
    original: str = ""
    patched: str = ""

    @property
    def names(self) -> list[str]:
        return sorted({d.name for d in self.dead})

    @property
    def freed_lines(self) -> int:
        return len(self.original.splitlines()) - len(self.patched.splitlines())

    @property
    def freed_bytes(self) -> int:
        return max(0, len(self.original.encode()) - len(self.patched.encode()))

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.patched.splitlines(keepends=True),
                fromfile=str(self.path),
                tofile=f"{self.path} (venvprune)",
            )
        )

    def apply(self, backup_suffix: str = ".venvprune-bak") -> None:
        if backup_suffix:
            self.path.with_suffix(self.path.suffix + backup_suffix).write_text(self.original, encoding="utf-8")
        self.path.write_text(self.patched, encoding="utf-8")


def plan_definition_rewrites(analysis: Analysis, include_risky: bool = False) -> list[DefinitionRewrite]:
    """Cut unreachable functions, classes and constants out of the modules that survive."""
    out: list[DefinitionRewrite] = []
    unused = {i.name for i in analysis.unused()}
    for name, info in sorted(analysis.modules.items()):
        if name in unused or name not in analysis.reach.reached:
            continue
        table = info.table
        if not isinstance(table, SymbolTable) or not table.prunable:
            continue
        if not info.path.is_file() or info.path.suffix != ".py":
            continue
        demand = analysis.reach.demand.get(name, Demand.ALL)
        rewrite = _build_definition_rewrite(name, info.path, table, demand, include_risky)
        if rewrite is not None:
            out.append(rewrite)
    return out


def _build_definition_rewrite(
    module: str,
    path: Path,
    table: SymbolTable,
    demand: set[str] | Demand,
    include_risky: bool,
) -> DefinitionRewrite | None:
    dead = dead_definitions(table, demand, include_risky)
    live = live_symbols(table, demand, include_risky)
    original = path.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines(keepends=True)

    drop: set[int] = set()
    replace: dict[int, str] = {}
    for definition in dead:
        drop.update(definition.span)

    dead_imports: list[Definition] = []
    for start, node in sorted(table.statements.items()):
        span, text = _narrow_statement(node, live)
        if span is None or span.stop > len(lines):
            continue
        if text is None:
            drop.update(span)
        else:
            replace[span.start] = text
            drop.update(range(span.start + 1, span.stop))
        if isinstance(node, ast.Import | ast.ImportFrom):
            dead_imports.extend(d for d in table.all_definitions() if d.lineno == start and d.name not in live)

    if not drop and not replace:
        return None
    if drop and max(drop) >= len(lines):
        return None

    out: list[str] = []
    for index, line in enumerate(lines):
        if index in replace:
            out.append(replace[index])
        elif index not in drop:
            out.append(line)
    patched = "".join(out)
    return DefinitionRewrite(
        module=module,
        path=path,
        dead=dead,
        dead_imports=dead_imports,
        original=original,
        patched=patched if patched.strip() else "",
    )


def _narrow_statement(node: ast.stmt, live: set[str]) -> tuple[range | None, str | None]:
    """Rebuild an import or `__all__` without its dead names; a None text means delete it."""
    span = range(node.lineno - 1, node.end_lineno or node.lineno)
    indent = " " * node.col_offset
    if isinstance(node, ast.ImportFrom) and node.module == "__future__":
        return None, None
    if isinstance(node, ast.Import | ast.ImportFrom):
        keep = [a for a in node.names if a.name == "*" or (a.asname or _bound(node, a)) in live]
        if len(keep) == len(node.names):
            return None, None
        if not keep:
            return span, None
        rebuilt = copy.copy(node)
        rebuilt.names = keep
        return span, f"{indent}{ast.unparse(rebuilt)}\n"

    targets = node.targets if isinstance(node, ast.Assign) else []
    if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
        return None, None
    value = node.value if isinstance(node, ast.Assign) else None
    if not isinstance(value, ast.List | ast.Tuple):
        return None, None
    kept = [e for e in value.elts if not (isinstance(e, ast.Constant) and e.value not in live)]
    if len(kept) == len(value.elts):
        return None, None
    rebuilt_all = copy.deepcopy(node)
    if isinstance(rebuilt_all, ast.Assign) and isinstance(rebuilt_all.value, ast.List | ast.Tuple):
        rebuilt_all.value.elts = kept
    return span, f"{indent}{ast.unparse(rebuilt_all)}\n"


def _bound(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> str:
    if isinstance(node, ast.Import):
        return alias.name.split(".")[0]
    return alias.name


def render_definition_plan(rewrites: list[DefinitionRewrite], show_diff: bool = False) -> str:
    if not rewrites:
        return "No dead definitions found (run with --prune-defs)."
    total_lines = sum(r.freed_lines for r in rewrites)
    total_bytes = sum(r.freed_bytes for r in rewrites)
    lines = [
        f"{len(rewrites)} module(s) contain definitions nothing reaches "
        f"({total_lines} lines, {total_bytes / 1024:.1f} KiB):",
        "",
    ]
    for rewrite in sorted(rewrites, key=lambda r: -r.freed_bytes):
        lines.append(f"  {rewrite.module}  ({rewrite.freed_lines} lines)")
        lines.append(f"      removes: {', '.join(rewrite.names)}")
        if show_diff:
            lines.append("")
            lines.extend(f"      {line}" for line in rewrite.diff().splitlines())
        lines.append("")
    return "\n".join(lines)
