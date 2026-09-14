"""`[tool.venvprune]` configuration, with the command line taking precedence.

Every setting can live in `pyproject.toml` so a project records its own pruning policy —
especially the keep list, which is where knowledge no analysis can recover (a package invoked
as a subprocess, a plugin loaded from a config file) gets written down.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from venvprune.scan.projectmeta import find_pyproject

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
    "jobs",
    "keep-main-modules",
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
