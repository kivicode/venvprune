"""`[tool.venvprune]` configuration, with the command line taking precedence.

Every setting can live in `pyproject.toml` so a project records its own pruning policy —
especially the keep list, which is where knowledge no analysis can recover (a package invoked
as a subprocess, a plugin loaded from a config file) gets written down.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from venvprune.projectmeta import canonical, find_pyproject

SECTION = "venvprune"

_KEYS = {
    "code",
    "venv",
    "keep",
    "roots",
    "entry-point-groups",
    "dev-groups",
    "lazy",
    "type-only",
    "reexport",
    "dynamic-expand",
    "symbols",
    "prune-defs",
    "risky-defs",
    "prune-dev",
    "scan-binaries",
    "strict-dynamic",
    "keep-distributions",
    "prune-script-packages",
    "strip-pycache",
    "format",
    "progress",
    "tree-depth",
}


class ConfigError(Exception):
    """An explicitly requested config file is missing or unreadable."""


@dataclass
class FileConfig:
    path: Path | None = None
    values: dict[str, Any] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    empty_section: bool = False
    """The file was read but declares no [tool.venvprune]."""

    def get(self, key: str, fallback: Any = None) -> Any:
        return self.values.get(key, fallback)


def load(explicit: Path | None = None, search_from: list[Path] | None = None) -> FileConfig:
    """Read `[tool.venvprune]` from an explicit file, or the nearest pyproject.toml."""
    path = explicit
    if path is None:
        for start in search_from or []:
            found = find_pyproject(start if start.is_dir() else start.parent)
            if found is not None:
                path = found
                break
    if explicit is not None and not explicit.is_file():
        raise ConfigError(f"config file not found: {explicit}")
    if path is None or not path.is_file():
        return FileConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    section = data.get("tool", {}).get(SECTION)
    if not isinstance(section, dict):
        return FileConfig(path=path, empty_section=True)
    unknown = sorted(k for k in section if k not in _KEYS)
    return FileConfig(path=path, values=section, unknown=unknown)


def pick(cli_value: Any, config: FileConfig, key: str, default: Any) -> Any:
    """Command line wins; then the config file; then the built-in default."""
    if cli_value is not None:
        return cli_value
    value = config.get(key)
    return default if value is None else value


def merge_list(cli_value: list[str] | None, config: FileConfig, key: str) -> list[str]:
    """List settings accumulate: the config file's entries plus anything added on the CLI."""
    from_config = config.get(key) or []
    if not isinstance(from_config, list):
        from_config = [from_config]
    return [*(str(v) for v in from_config), *(cli_value or [])]


def expand_keep(
    patterns: list[str], modules: dict[str, Any], dist_of: dict[str, str] | Any
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
