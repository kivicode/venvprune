"""Actually removing what the analysis found, with a manifest that can put it all back.

A distribution nothing reaches is removed whole — its packages, its `dist-info`, its data
files and its bundled shared libraries — not just the `.py` files the module graph knows about.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from venvprune import native, projectmeta
from venvprune.analyzer import Analysis

MANIFEST_NAME = "venvprune-manifest.json"


@dataclass
class Plan:
    files: list[Path] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)
    distributions: list[str] = field(default_factory=list)
    kept_for_scripts: list[str] = field(default_factory=list)
    """Unreachable distributions spared because they install a command."""

    @property
    def total_bytes(self) -> int:
        total = sum(p.stat().st_size for p in self.files if p.is_file())
        for directory in self.dirs:
            total += sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        return total

    def __bool__(self) -> bool:
        return bool(self.files or self.dirs)


def build_plan(
    analysis: Analysis,
    whole_distributions: bool = True,
    include_libs: bool = True,
    prune_script_packages: bool = False,
) -> Plan:
    plan = Plan()
    seen: set[Path] = set()
    dead_dists = set(analysis.fully_unused_distributions()) if whole_distributions else set()
    if not prune_script_packages:
        scripts = analysis.script_distributions()
        held = {d for d in dead_dists if projectmeta.canonical(d) in scripts}
        plan.kept_for_scripts = sorted(held)
        dead_dists -= held
    plan.distributions = sorted(dead_dists)

    dist_dirs: set[Path] = set()
    if dead_dists:
        canonical_dead = {projectmeta.canonical(d) for d in dead_dists}
        for name, dist in analysis.distributions.items():
            if projectmeta.canonical(name) not in canonical_dead:
                continue
            for path in dist.files:
                top = _top_level_entry(path, analysis.site_dirs)
                if top is None:
                    continue
                if top.is_dir():
                    dist_dirs.add(top)
                elif top.is_file():
                    seen.add(top)
            dist_dirs.update(_metadata_dirs(name, dist.version, analysis.site_dirs))

    # Everything the module graph found, minus anything already covered by a whole directory
    # and anything belonging to a distribution spared for its console scripts.
    spared = {projectmeta.canonical(d) for d in plan.kept_for_scripts}
    for info in analysis.unused():
        if not info.path.is_file() or _inside(info.path, dist_dirs):
            continue
        if spared and analysis.dist_of.get(info.name) in spared:
            continue
        seen.add(info.path)

    if include_libs:
        kept = [
            i.path
            for n, i in analysis.extension_modules().items()
            if n in analysis.reach.reached and n not in {u.name for u in analysis.unused()}
        ]
        libs = native.attribute_libraries(kept, native.bundled_libraries(analysis.site_dirs))
        for lib in libs.values():
            if not lib.referenced_by and not _inside(lib.path, dist_dirs):
                seen.add(lib.path)

    plan.files = sorted(seen)
    plan.dirs = sorted(dist_dirs)
    return plan


def _inside(path: Path, directories: set[Path]) -> bool:
    return any(path.is_relative_to(d) for d in directories)


def _top_level_entry(path: Path, site_dirs: list[Path]) -> Path | None:
    """The package directory (or lone file) a RECORD entry belongs to."""
    for site in site_dirs:
        site = site.resolve()
        try:
            rel = path.resolve().relative_to(site)
        except ValueError:
            continue
        return site / rel.parts[0]
    return None


def _metadata_dirs(name: str, version: str, site_dirs: list[Path]) -> set[Path]:
    out: set[Path] = set()
    for site in site_dirs:
        for pattern in (f"{name}-{version}.dist-info", f"{name.replace('-', '_')}-{version}.dist-info"):
            candidate = site / pattern
            if candidate.is_dir():
                out.add(candidate)
    return out


def execute(plan: Plan, site_dirs: list[Path], manifest_dir: Path | None = None) -> Path:
    """Delete everything in the plan, writing a manifest of what went first."""
    target = (manifest_dir or site_dirs[0]) / MANIFEST_NAME
    manifest = {
        "site_packages": [str(d) for d in site_dirs],
        "distributions": plan.distributions,
        "kept_for_scripts": plan.kept_for_scripts,
        "files": [str(p) for p in plan.files],
        "dirs": [str(p) for p in plan.dirs],
        "bytes": plan.total_bytes,
    }
    target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    for directory in plan.dirs:
        shutil.rmtree(directory, ignore_errors=True)
    for path in plan.files:
        path.unlink(missing_ok=True)
    _prune_empty_dirs(site_dirs)
    return target


def _prune_empty_dirs(site_dirs: list[Path]) -> None:
    for site in site_dirs:
        for path in sorted(site.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()


def render_plan(plan: Plan) -> str:
    lines = [f"Removal plan: {len(plan.files)} file(s), {len(plan.dirs)} director(ies)"]
    lines.append(f"reclaims {plan.total_bytes / 1024 / 1024:.1f} MiB")
    if plan.distributions:
        lines.append("")
        lines.append(f"Distributions removed whole ({len(plan.distributions)}):")
        lines.append(f"  {', '.join(plan.distributions)}")
    if plan.kept_for_scripts:
        lines.append("")
        lines.append(f"Unreachable but kept, they install a command ({len(plan.kept_for_scripts)}):")
        lines.append(f"  {', '.join(plan.kept_for_scripts)}")
        lines.append("  (--prune-script-packages removes these too)")
    return "\n".join(lines)
