"""Phase 3: how dangerous is each dynamic-import site, and what can it actually reach."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from venvprune.analysis.analyzer import Analysis
from venvprune.model import DynamicHint, DynamicKind


class Severity(IntEnum):
    RESOLVED = 0
    """The name folds to a bounded set of modules, all of them accounted for."""

    CONFINED = 1
    """Unbounded, but it can only reach the package it lives in."""

    OPEN = 2
    """The name comes from outside: any installed module could be loaded."""

    @property
    def label(self) -> str:
        return self.name.lower()


_CONFINED_KINDS = {DynamicKind.GETATTR_MODULE, DynamicKind.PKGUTIL}


@dataclass(frozen=True)
class RiskSite:
    hint: DynamicHint
    severity: Severity
    candidates: tuple[str, ...]
    holds: int = 0
    """Modules this site alone keeps alive, i.e. what bounding it would free."""

    @property
    def location(self) -> str:
        return f"{self.hint.module}:{self.hint.lineno}"


def assess(analysis: Analysis) -> list[RiskSite]:
    graph = analysis.graph
    sites: list[RiskSite] = []
    for hint in analysis.dynamic_hints():
        info = analysis.modules.get(hint.module)
        if info is None:
            continue
        if hint.kind is DynamicKind.ENTRY_POINTS:
            severity = Severity.RESOLVED if hint.bounded else Severity.OPEN
            sites.append(RiskSite(hint, severity, ()))
            continue
        resolved = graph.resolve_hint(info, hint)
        if resolved is None:
            severity = Severity.OPEN
            candidates = ()
            held = len(graph.dynamic_targets(info, analysis.options))
            sites.append(RiskSite(hint, severity, candidates, held))
            continue
        if hint.kind in _CONFINED_KINDS and not hint.bounded:
            severity = Severity.CONFINED
            candidates = tuple(sorted(resolved))
        else:
            severity = Severity.RESOLVED
            candidates = tuple(sorted(resolved))
        sites.append(RiskSite(hint, severity, candidates, len(candidates)))
    return sorted(sites, key=lambda s: (-s.severity, -s.holds, s.hint.module, s.hint.lineno))


def package_risk(sites: list[RiskSite]) -> dict[str, Severity]:
    """Worst severity seen anywhere inside each top-level package."""
    out: dict[str, Severity] = {}
    for site in sites:
        top = site.hint.module.split(".")[0]
        out[top] = max(out.get(top, Severity.RESOLVED), site.severity)
    return out


def render(sites: list[RiskSite], verbose: bool = False) -> str:
    if not sites:
        return "No dynamic-import sites in reachable code."
    counts = {severity: 0 for severity in Severity}
    for site in sites:
        counts[site.severity] += 1
    lines = ["Dynamic-import sites by severity:", ""]
    for severity in sorted(Severity, reverse=True):
        lines.append(f"  {severity.label:<9} {counts[severity]}")
    lines.append("")

    costly = [s for s in sites if s.severity is Severity.OPEN and s.holds]
    if costly:
        lines.append("Unbounded sites holding the most modules (--strict-dynamic drops these):")
        for site in costly[:10]:
            lines.append(f"  {site.holds:>5} modules  {site.location}  {site.hint.detail}")
        lines.append("")

    risky = package_risk(sites)
    unsafe = sorted(p for p, s in risky.items() if s is Severity.OPEN)
    if unsafe:
        lines.append(f"Packages that cannot be pruned safely ({len(unsafe)}):")
        lines.append(f"  {', '.join(unsafe)}")
        lines.append("")

    if verbose:
        lines.append("Sites:")
        for site in sites:
            lines.append(f"  [{site.severity.label}] {site.location}  {site.hint.detail}")
            if site.candidates:
                shown = ", ".join(site.candidates[:6])
                more = f" (+{len(site.candidates) - 6} more)" if len(site.candidates) > 6 else ""
                lines.append(f"      -> {shown}{more}")
    return "\n".join(lines)
