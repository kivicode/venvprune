"""Phase 2: patches that let an incidentally-imported module actually be deleted.

A package's `__init__.py` commonly imports every submodule just to republish names. If the
code only ever uses some of those names, the rest are dead weight — but the submodules cannot
be deleted while the `__init__` still imports them eagerly. These rewrites remove exactly those
statements and leave a `__getattr__` (PEP 562) that raises a clear error if a pruned name is
touched after all, so a mistake surfaces as an explicit message rather than an `ImportError`
from a missing file.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from venvprune.analyzer import Analysis
from venvprune.model import ImportEdge

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
