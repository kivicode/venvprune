"""Which declared dependencies the code actually uses.

This asks a different question from the rest of the tool. Module pruning asks "can this file
be deleted"; here the unit is a line in `pyproject.toml`. A dependency can be installed, its
modules perfectly reachable, and the declaration still be wrong -- because what reaches them is
another library, or the test suite, rather than the code that ships.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from venvprune.analysis.analyzer import Analysis
from venvprune.model import Origin
from venvprune.scan import projectmeta


class Status(IntEnum):
    USED = 0
    """Imported by first-party code."""

    INDIRECT = 1
    """Installed and reachable, but only because something else imports it."""

    UNUSED = 2
    """Nothing reaches it at all."""

    MISSING = 3
    """Declared but not installed, so nothing can be said."""

    CONDITIONAL = 4
    """Not installed, but its environment marker excludes this platform."""

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Declared:
    name: str
    origins: tuple[str, ...]
    modules: tuple[str, ...]
    status: Status
    importers: tuple[str, ...]
    """First-party modules importing it, or the libraries that pull it in."""

    @property
    def runtime(self) -> bool:
        return "dependencies" in self.origins


def importers_by_distribution(analysis: Analysis) -> dict[str, set[str]]:
    """Distribution -> the first-party modules whose import statements name it."""
    out: dict[str, set[str]] = {}
    for info in analysis.modules.values():
        if info.origin is not Origin.LOCAL:
            continue
        for edge in info.edges:
            parts = edge.target.split(".")
            for index in range(len(parts)):
                candidate = ".".join(parts[: index + 1])
                owner = analysis.dist_of.get(candidate)
                if owner is not None:
                    out.setdefault(owner, set()).add(info.name)
                    break
    return out


def _library_importers(analysis: Analysis, modules: tuple[str, ...]) -> tuple[str, ...]:
    """Reached library modules that import this distribution, for the INDIRECT case."""
    wanted = set(modules)
    found: set[str] = set()
    for name, info in analysis.modules.items():
        if info.origin is Origin.LOCAL or name not in analysis.reach.reached:
            continue
        if analysis.dist_of.get(name) in {analysis.dist_of.get(m) for m in wanted}:
            continue
        for edge in info.edges:
            if edge.target.split(".")[0] in wanted:
                found.add(name)
                break
    return tuple(sorted(found))


def assess(analysis: Analysis, pyproject: Path | None = None) -> list[Declared]:
    """Classify every declared dependency against what the analysed code actually imports."""
    source = pyproject or projectmeta.find_pyproject(Path.cwd())
    if source is None:
        return []
    meta = projectmeta.read_project(source)
    if not meta.declared:
        return []

    modules_by_dist: dict[str, set[str]] = {}
    for module, dist in analysis.dist_of.items():
        if "." not in module:
            modules_by_dist.setdefault(dist, set()).add(module)
    direct = importers_by_distribution(analysis)

    out: list[Declared] = []
    for name, origins in sorted(meta.declared.items()):
        modules = tuple(sorted(modules_by_dist.get(name, ())))
        if not modules:
            marker = meta.markers.get(name, "")
            status = Status.CONDITIONAL if marker else Status.MISSING
            out.append(Declared(name, tuple(sorted(origins)), (), status, (marker,) if marker else ()))
            continue
        if name in direct:
            importers = tuple(sorted(direct[name]))
            out.append(Declared(name, tuple(sorted(origins)), modules, Status.USED, importers[:4]))
        elif any(m in analysis.reach.reached for m in modules):
            via = _library_importers(analysis, modules)
            out.append(Declared(name, tuple(sorted(origins)), modules, Status.INDIRECT, via[:4]))
        else:
            out.append(Declared(name, tuple(sorted(origins)), modules, Status.UNUSED, ()))
    return out


def render(declared: list[Declared], verbose: bool = False) -> str:
    if not declared:
        return "No declared dependencies found (is there a [project] table?)."

    lines = ["Declared dependencies", "=" * 60]
    counts = {status: sum(1 for d in declared if d.status is status) for status in Status}
    lines.append(f"declared : {len(declared)}")
    for status in Status:
        lines.append(f"{status.label:<9}: {counts[status]}")
    lines.append("")

    for status, blurb in (
        (Status.UNUSED, "nothing imports these, directly or transitively"),
        (Status.MISSING, "declared but not installed, and not excluded by a marker"),
        (Status.CONDITIONAL, "not installed, but their environment marker excludes this platform"),
        (Status.INDIRECT, "reachable, but only because another package imports them"),
    ):
        rows = [d for d in declared if d.status is status]
        if not rows:
            continue
        lines.append(f"{status.label.upper()} ({len(rows)}) -- {blurb}:")
        for row in rows:
            where = ", ".join(row.origins)
            names = f"  [{', '.join(row.modules)}]" if row.modules and row.modules != (row.name,) else ""
            lines.append(f"  {row.name:<28} {where}{names}")
            if row.importers:
                label = "marker" if row.status is Status.CONDITIONAL else "reached from"
                lines.append(f"      {label}: {', '.join(row.importers)}")
        lines.append("")

    used = [d for d in declared if d.status is Status.USED]
    lines.append(f"USED ({len(used)}) -- imported by first-party code")
    if verbose:
        for row in used:
            lines.append(f"  {row.name:<28} {', '.join(row.importers)}")
    return "\n".join(lines)
