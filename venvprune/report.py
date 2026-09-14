"""Human and machine readable renderings of an Analysis."""

from __future__ import annotations

import json
from collections.abc import Iterable

from venvprune.analyzer import Analysis
from venvprune.model import EdgeKind, ModuleInfo


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def _top_level_groups(modules: Iterable[ModuleInfo]) -> dict[str, list[ModuleInfo]]:
    groups: dict[str, list[ModuleInfo]] = {}
    for info in modules:
        groups.setdefault(info.name.split(".")[0], []).append(info)
    return groups


def render_text(analysis: Analysis, verbose: bool = False) -> str:
    unused = analysis.unused()
    site_total = sum(1 for i in analysis.modules.values() if i.origin.value == "site")
    lines: list[str] = []
    lines.append("venvprune report")
    lines.append("=" * 60)
    lines.append(f"site-packages dirs : {', '.join(str(d) for d in analysis.site_dirs)}")
    lines.append(f"modules in venv    : {site_total}")
    lines.append(f"reachable          : {site_total - len(unused)}")
    lines.append(f"unused             : {len(unused)}  ({_human_bytes(analysis.unused_bytes())})")
    if analysis.traced:
        lines.append(f"runtime-traced     : {len(analysis.traced)} modules kept by trace evidence")
    lines.append("")

    dists = analysis.fully_unused_distributions()
    if dists:
        lines.append(f"Distributions with no reachable module ({len(dists)}):")
        lines.extend(f"  - {d}" for d in dists)
        lines.append("")

    lines.append("Unused modules by top-level package:")
    for top, infos in sorted(_top_level_groups(unused).items()):
        lines.append(f"  {top}  ({len(infos)} modules)")
        if verbose:
            lines.extend(f"      {i.name}" for i in infos)
    if not unused:
        lines.append("  (none)")
    lines.append("")

    lazy = analysis.kept(EdgeKind.LAZY)
    reexport = analysis.kept(EdgeKind.REEXPORT)
    type_only = analysis.kept(EdgeKind.TYPE_ONLY)
    lines.append("Kept, but only weakly (phase-2 rewrite candidates):")
    lines.append(f"  lazy imports only  : {len(lazy)}")
    lines.append(f"  re-export only     : {len(reexport)}")
    lines.append(f"  type-annotation    : {len(type_only)}")
    if verbose:
        for label, names in (("lazy", lazy), ("reexport", reexport), ("type-only", type_only)):
            lines.extend(f"      [{label}] {n}" for n in names)
    lines.append("")

    hints = analysis.dynamic_hints()
    lines.append(f"Dynamic-import risk sites in reachable code: {len(hints)}")
    if hints:
        by_kind: dict[str, int] = {}
        for hint in hints:
            by_kind[hint.kind.value] = by_kind.get(hint.kind.value, 0) + 1
        for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {kind:<16} {count}")
        if verbose:
            lines.extend(f"      {h.module}:{h.lineno}  {h.kind.value}  {h.detail}" for h in hints)
    lines.append("")

    if analysis.reach.unresolved:
        lines.append(f"Unresolved imports (not stdlib, not found): {len(analysis.reach.unresolved)}")
        if verbose:
            seen: set[str] = set()
            for edge in analysis.reach.unresolved:
                if edge.target in seen:
                    continue
                seen.add(edge.target)
                lines.append(f"      {edge.target}  (from {edge.src}:{edge.lineno})")
    return "\n".join(lines)


def render_json(analysis: Analysis) -> str:
    payload = {
        "site_packages": [str(d) for d in analysis.site_dirs],
        "unused_modules": [
            {"module": i.name, "path": str(i.path), "size": i.path.stat().st_size if i.path.is_file() else 0}
            for i in analysis.unused()
        ],
        "unused_bytes": analysis.unused_bytes(),
        "fully_unused_distributions": analysis.fully_unused_distributions(),
        "weak_keeps": {
            "lazy": analysis.kept(EdgeKind.LAZY),
            "reexport": analysis.kept(EdgeKind.REEXPORT),
            "type_only": analysis.kept(EdgeKind.TYPE_ONLY),
        },
        "dynamic_hints": [
            {"module": h.module, "line": h.lineno, "kind": h.kind.value, "detail": h.detail}
            for h in analysis.dynamic_hints()
        ],
        "unresolved": sorted({e.target for e in analysis.reach.unresolved}),
        "traced_modules": sorted(analysis.traced),
    }
    return json.dumps(payload, indent=2)


def render_paths(analysis: Analysis) -> str:
    """One filesystem path per line, for piping into a deletion tool."""
    return "\n".join(str(i.path) for i in analysis.unused() if i.path.exists())
