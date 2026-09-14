"""Runtime import tracing: run the target code and record what it actually imported.

Static analysis cannot see through `importlib`, plugin registries or config-driven
dispatch. A trace gives ground truth for one execution path, so results are a lower
bound on what is needed and are merged into (never subtracted from) the static set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_BOOTSTRAP = """
import atexit, json, os, sys

_OUT = os.environ["VENVPRUNE_TRACE_OUT"]
_seen = set(sys.modules)


def _dump():
    records = {}
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        records[name] = getattr(mod, "__file__", None)
    try:
        with open(_OUT, "w", encoding="utf-8") as fh:
            json.dump({"modules": records, "preloaded": sorted(_seen)}, fh)
    except OSError:
        pass


atexit.register(_dump)
"""


@dataclass
class TraceResult:
    modules: dict[str, str | None]
    preloaded: set[str]
    returncode: int
    command: list[str]

    @property
    def names(self) -> set[str]:
        return set(self.modules)


def run_trace(
    python: Path,
    command: list[str],
    cwd: Path | None = None,
    timeout: float | None = None,
) -> TraceResult:
    """Execute `python <command...>` with an atexit hook that dumps `sys.modules`."""
    with tempfile.TemporaryDirectory(prefix="venvprune-") as tmp:
        boot = Path(tmp)
        (boot / "sitecustomize.py").write_text(_BOOTSTRAP, encoding="utf-8")
        out = boot / "trace.json"
        env = dict(os.environ)
        env["VENVPRUNE_TRACE_OUT"] = str(out)
        env["PYTHONPATH"] = os.pathsep.join([str(boot), *filter(None, [env.get("PYTHONPATH")])])
        env.pop("PYTHONHOME", None)
        proc = subprocess.run(
            [str(python), *command],
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=timeout,
            check=False,
        )
        data = {}
        if out.exists():
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
    return TraceResult(
        modules=data.get("modules", {}),
        preloaded=set(data.get("preloaded", [])),
        returncode=proc.returncode,
        command=command,
    )


def venv_python(venv: Path) -> Path:
    for rel in ("bin/python", "bin/python3", "Scripts/python.exe"):
        candidate = venv / rel
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def site_relative_names(result: TraceResult, site_dirs: list[Path]) -> set[str]:
    """Names from the trace whose `__file__` lives inside one of the site-packages dirs."""
    roots = [d.resolve() for d in site_dirs]
    out: set[str] = set()
    for name, file in result.modules.items():
        if not file:
            continue
        path = Path(file).resolve()
        if any(path.is_relative_to(root) for root in roots):
            out.add(name)
    return out
