"""Reads a project's declared dependencies to find dev-only distributions.

Anything declared *only* in a dev group (or a dev extra) is not needed to run the code, so it
can be pruned wholesale — unless the code actually imports it, or a runtime dependency pulls it
in transitively.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

DEFAULT_DEV_GROUPS = ("dev", "test", "tests", "lint", "docs", "typing", "bench", "benchmark")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_marker(spec: str) -> str:
    """The environment marker of a requirement, if it carries one."""
    _, _, marker = spec.partition(";")
    return marker.strip()


def _requirement_name(spec: str) -> str | None:
    spec = spec.split(";", 1)[0].strip()
    if not spec or spec.startswith("-"):
        return None
    match = _REQ_NAME.match(spec)
    return canonical(match.group(1)) if match else None


def _markers(specs: object) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(specs, list):
        for spec in specs:
            if isinstance(spec, str) and (name := _requirement_name(spec)) and (m := requirement_marker(spec)):
                out[name] = m
    return out


def _names(specs: object) -> set[str]:
    out: set[str] = set()
    if isinstance(specs, list):
        for spec in specs:
            if isinstance(spec, str) and (name := _requirement_name(spec)):
                out.add(name)
            elif isinstance(spec, dict) and isinstance(spec.get("include-group"), str):
                out.add(f"@{spec['include-group']}")
    return out


@dataclass
class ProjectMeta:
    path: Path
    runtime: set[str] = field(default_factory=set)
    groups: dict[str, set[str]] = field(default_factory=dict)
    declared: dict[str, set[str]] = field(default_factory=dict)
    """Distribution -> where it was declared (`dependencies`, `extra:docs`, `group:dev`)."""

    markers: dict[str, str] = field(default_factory=dict)
    """Distribution -> its environment marker, so a platform-specific dep is not "missing"."""

    def record(self, name: str, origin: str, marker: str = "") -> None:
        self.declared.setdefault(name, set()).add(origin)
        if marker:
            self.markers[name] = marker

    def dev_only(self, dev_groups: tuple[str, ...] = DEFAULT_DEV_GROUPS) -> set[str]:
        """Distributions declared in a dev group and in no runtime requirement."""
        selected: set[str] = set()
        for group in dev_groups:
            selected |= self._expand(group, set())
        return selected - self.runtime

    def _expand(self, group: str, seen: set[str]) -> set[str]:
        if group in seen or group not in self.groups:
            return set()
        seen.add(group)
        out: set[str] = set()
        for entry in self.groups[group]:
            if entry.startswith("@"):
                out |= self._expand(entry[1:], seen)
            else:
                out.add(entry)
        return out


def find_pyproject(start: Path) -> Path | None:
    start = start.resolve()
    for candidate in [start, *start.parents]:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file():
            return pyproject
    return None


def read_project(pyproject: Path) -> ProjectMeta:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    meta = ProjectMeta(path=pyproject)
    meta.runtime |= _names(project.get("dependencies"))
    markers = _markers(project.get("dependencies"))
    for name in meta.runtime:
        meta.record(name, "dependencies", markers.get(name, ""))

    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for extra, specs in optional.items():
            meta.groups[extra] = _names(specs)
            extra_markers = _markers(specs)
            for name in meta.groups[extra]:
                if not name.startswith("@"):
                    meta.record(name, f"extra:{extra}", extra_markers.get(name, ""))

    groups = data.get("dependency-groups", {})
    if isinstance(groups, dict):
        for group, specs in groups.items():
            meta.groups.setdefault(group, set())
            meta.groups[group] |= _names(specs)
            group_markers = _markers(specs)
            for name in _names(specs):
                if not name.startswith("@"):
                    meta.record(name, f"group:{group}", group_markers.get(name, ""))

    poetry = data.get("tool", {}).get("poetry", {})
    if isinstance(poetry, dict):
        deps = poetry.get("dependencies", {})
        if isinstance(deps, dict):
            poetry_runtime = {canonical(k) for k in deps if k.lower() != "python"}
            meta.runtime |= poetry_runtime
            for name in poetry_runtime:
                meta.record(name, "dependencies")
        for group, body in (poetry.get("group") or {}).items():
            if isinstance(body, dict) and isinstance(body.get("dependencies"), dict):
                meta.groups.setdefault(group, set())
                poetry_group = {canonical(k) for k in body["dependencies"]}
                meta.groups[group] |= poetry_group
                for name in poetry_group:
                    meta.record(name, f"group:{group}")

    return meta


def read_requirements(paths: list[Path]) -> set[str]:
    out: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if (name := _requirement_name(line)) is not None:
                out.add(name)
    return out
