"""Phase 4: compiled artifacts shipped in wheels.

Three things no AST pass can see:

* an extension module imports Python modules from C, so its edges are invisible;
* a wheel often ships several builds of the same module (ABI tags, SIMD or CUDA
  variants) where only one can ever be loaded on this interpreter;
* extensions link bundled shared libraries (`pkg/.dylibs`, `pkg.libs`) that are
  dead weight once the extension that needs them is gone.
"""

from __future__ import annotations

import re
import subprocess
import sys
import sysconfig
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from venvprune.model import ModuleInfo, Origin
from venvprune.progress import Tracker

EXT_SUFFIXES = (".so", ".pyd")
_LIB_DIRS = (".dylibs", ".libs")
_STRING = re.compile(rb"[\x20-\x7e]{3,128}")
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def is_extension(path: Path) -> bool:
    return path.name.endswith(EXT_SUFFIXES)


def current_abi_tags() -> set[str]:
    """Tags this interpreter will accept in an extension filename."""
    tags = {""}
    for key in ("EXT_SUFFIX", "SOABI"):
        value = sysconfig.get_config_var(key)
        if value:
            tags.add(value.lstrip(".").removesuffix(".so").removesuffix(".pyd"))
    tags.add(f"cpython-{sys.version_info.major}{sys.version_info.minor}")
    return {t for t in tags if t}


def _tag_of(path: Path) -> str:
    """`_speedups.cpython-312-darwin.so` -> `cpython-312-darwin`."""
    name = path.name
    for suffix in EXT_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    _, _, tag = name.partition(".")
    return tag


@dataclass
class Variant:
    module: str
    chosen: Path
    rejected: list[Path] = field(default_factory=list)
    """Builds of the same module this interpreter can never load."""

    @property
    def wasted_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.rejected if p.is_file())


def find_variants(site_dirs: list[Path], abi_tags: set[str] | None = None) -> list[Variant]:
    """Group extension files by module name and pick the build this interpreter can load."""
    tags = abi_tags if abi_tags is not None else current_abi_tags()
    groups: dict[tuple[Path, str], list[Path]] = {}
    for site in site_dirs:
        for path in site.rglob("*"):
            if not path.is_file() or not is_extension(path) or _in_lib_dir(path):
                continue
            stem = path.name.split(".")[0]
            groups.setdefault((path.parent, stem), []).append(path)

    out: list[Variant] = []
    for (parent, stem), paths in sorted(groups.items()):
        if len(paths) < 2:
            continue
        loadable = [p for p in paths if _tag_matches(_tag_of(p), tags)]
        chosen = loadable[0] if loadable else max(paths, key=lambda p: p.stat().st_size)
        rejected = [p for p in paths if p != chosen]
        module = _module_name_for(parent, stem, site_dirs)
        out.append(Variant(module=module, chosen=chosen, rejected=rejected))
    return out


def _tag_matches(tag: str, tags: set[str]) -> bool:
    if not tag:
        return True
    return any(tag == t or tag.startswith(t) or t.startswith(tag) for t in tags)


def _module_name_for(parent: Path, stem: str, site_dirs: list[Path]) -> str:
    for site in site_dirs:
        try:
            rel = parent.resolve().relative_to(site.resolve())
        except ValueError:
            continue
        return ".".join([*rel.parts, stem])
    return stem


def _in_lib_dir(path: Path) -> bool:
    return any(part in _LIB_DIRS or part.endswith(_LIB_DIRS) for part in path.parts)


def strings_in(path: Path, limit: int = 40_000_000) -> set[str]:
    try:
        blob = path.read_bytes()[:limit]
    except OSError:
        return set()
    return {m.group().decode("ascii", "replace") for m in _STRING.finditer(blob)}


def imports_from_binary(path: Path, known: Iterable[str], package: str = "") -> set[str]:
    """Module names embedded in an extension that also exist in the analysed index.

    C code imports by name, so the name survives in the binary's string table. Cython and
    hand-written extensions usually store the *bare* name of a sibling (`_elementpath`, not
    `lxml._elementpath`), so bare strings are also tried against the importing package.
    Matching against the index keeps the false-positive rate low, at the cost of missing
    names built at runtime.
    """
    known_set = set(known)
    found: set[str] = set()
    for text in strings_in(path):
        if not _DOTTED.match(text):
            continue
        if text in known_set:
            found.add(text)
        elif package and (sibling := f"{package}.{text}") in known_set:
            found.add(sibling)
    return found


def linked_libraries(path: Path) -> list[str]:
    """Shared libraries an extension links against, as recorded in its load commands."""
    if sys.platform == "darwin":
        cmd = ["otool", "-L", str(path)]
    elif sys.platform.startswith("linux"):
        cmd = ["objdump", "-p", str(path)]
    else:
        return []
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if sys.platform == "darwin" and line and "(" in line:
            out.append(line.split("(")[0].strip())
        elif line.startswith("NEEDED"):
            out.append(line.split()[-1])
    return out


@dataclass
class BundledLib:
    path: Path
    referenced_by: set[str] = field(default_factory=set)

    @property
    def size(self) -> int:
        return self.path.stat().st_size if self.path.is_file() else 0


def bundled_libraries(site_dirs: list[Path], tracker: Tracker | None = None) -> dict[str, BundledLib]:
    """Every shared library sitting in a wheel's bundled-library directory, by basename."""
    out: dict[str, BundledLib] = {}
    for site in site_dirs:
        for path in site.rglob("*"):
            if tracker is not None:
                tracker.advance()
            if not path.is_file() or not _in_lib_dir(path):
                continue
            # `.so.6`-style version suffixes are shared libraries too.
            if path.suffix in {".so", ".dylib", ".dll"} or ".so." in path.name:
                out[path.name] = BundledLib(path=path)
    return out


def attribute_libraries(
    kept_extensions: Iterable[Path], libs: dict[str, BundledLib], tracker: Tracker | None = None
) -> dict[str, BundledLib]:
    """Mark which bundled libraries are still reachable from an extension that survives."""
    if not libs:
        # Nothing to attribute, so skip the one `otool`/`objdump` call per extension.
        return libs
    for ext in kept_extensions:
        if tracker is not None:
            tracker.advance()
        for link in linked_libraries(ext):
            name = link.rsplit("/", 1)[-1]
            if name in libs:
                libs[name].referenced_by.add(ext.name)
    return libs


def extension_modules(modules: dict[str, ModuleInfo]) -> dict[str, ModuleInfo]:
    return {name: info for name, info in modules.items() if info.origin is Origin.SITE and is_extension(info.path)}


def render_report(
    variants: list[Variant], libs: dict[str, BundledLib], extensions: dict[str, ModuleInfo], unused: set[str]
) -> str:
    lines = ["Compiled artifacts", "=" * 60]
    kept = {n: i for n, i in extensions.items() if n not in unused}
    dropped = {n: i for n, i in extensions.items() if n in unused}
    lines.append(f"extension modules  : {len(extensions)} ({len(dropped)} unreachable)")
    dropped_bytes = sum(i.path.stat().st_size for i in dropped.values() if i.path.is_file())
    lines.append(f"unreachable bytes  : {_human(dropped_bytes)}")
    lines.append("")

    if variants:
        waste = sum(v.wasted_bytes for v in variants)
        lines.append(f"Multi-build extensions ({len(variants)}, {_human(waste)} unloadable):")
        for variant in variants:
            lines.append(f"  {variant.module}  keeps {variant.chosen.name}")
            for path in variant.rejected:
                lines.append(f"      drop {path.name}  ({_human(path.stat().st_size if path.is_file() else 0)})")
        lines.append("")

    if libs:
        orphaned = [lib for lib in libs.values() if not lib.referenced_by]
        total = sum(lib.size for lib in orphaned)
        lines.append(f"Bundled shared libraries: {len(libs)} ({len(orphaned)} unreferenced, {_human(total)}):")
        for lib in sorted(orphaned, key=lambda x: -x.size)[:20]:
            lines.append(f"  {lib.path.name}  {_human(lib.size)}")
        lines.append("")

    if kept:
        lines.append(f"Reachable extensions: {len(kept)}")
    return "\n".join(lines)


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"
