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
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
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


def _read(path: Path, limit: int = 40_000_000) -> bytes:
    try:
        return path.read_bytes()[:limit]
    except OSError:
        return b""


def _strings(blob: bytes) -> set[str]:
    return {m.group().decode("ascii", "replace") for m in _STRING.finditer(blob)}


def strings_in(path: Path, limit: int = 40_000_000) -> set[str]:
    return _strings(_read(path, limit))


def imports_from_binary(path: Path, known: Iterable[str], package: str = "", siblings: Iterable[str] = ()) -> set[str]:
    """Module names an extension imports from C, recovered from its bytes.

    Three passes, loosening as they go. A whole printable run that is a known dotted name is
    taken as-is; a bare run is tried against the importing package. Cython leaves neither —
    `lxml/etree.so` references `_elementpath` only inside the mangled symbol
    `___pyx_v_4lxml_5etree__elementpath` — so a sibling's name appearing anywhere in the
    binary also counts. That last pass over-keeps, which is the safe direction, and is limited
    to siblings so it cannot reach across the venv.
    """
    blob = _read(path)
    if not blob:
        return set()
    known_set = set(known)
    found: set[str] = set()
    for text in _strings(blob):
        if not _DOTTED.match(text):
            continue
        if text in known_set:
            found.add(text)
        elif package and (sibling := f"{package}.{text}") in known_set:
            found.add(sibling)

    for bare in siblings:
        # Short names would match far too much text to mean anything.
        if len(bare) >= 4 and bare.encode() in blob:
            found.add(f"{package}.{bare}" if package else bare)
    return found


# Readers in preference order per platform. Each takes a list of files and is batched, since
# one process per extension module dominates the run on a venv with hundreds of them.
_READERS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "darwin": (("otool", ("-L",)), ("llvm-otool", ("-L",)), ("objdump", ("-p",))),
    "linux": (("objdump", ("-p",)), ("readelf", ("-d",)), ("llvm-objdump", ("-p",))),
    "win32": (("dumpbin", ("/nologo", "/dependents")), ("objdump", ("-p",)), ("llvm-objdump", ("-p",))),
}
_LINK_BATCH = 128


@lru_cache(maxsize=1)
def link_reader() -> tuple[str, tuple[str, ...]] | None:
    """The first available reader for this platform, or None when none is installed.

    None must be read as "unknown", never "no dependencies": without it every bundled library
    looks unreferenced and would be deleted out from under a working extension.
    """
    family = "linux" if sys.platform.startswith("linux") else sys.platform
    for name, flags in _READERS.get(family, ()):
        found = shutil.which(name)
        if found:
            return found, flags
    return None


def missing_reader_hint() -> str:
    family = "linux" if sys.platform.startswith("linux") else sys.platform
    names = " or ".join(name for name, _ in _READERS.get(family, ())) or "a link-table reader"
    return f"install {names} to let bundled shared libraries be pruned"


def linked_libraries(path: Path) -> list[str]:
    """Shared libraries this binary links against; empty when nothing can read it."""
    return linked_libraries_many([path]).get(path) or []


def linked_libraries_many(paths: Sequence[Path]) -> dict[Path, list[str] | None]:
    """Link tables for many files at once. A None value means nothing could read that file."""
    reader = link_reader()
    if not paths or reader is None:
        return dict.fromkeys(paths)
    command, flags = reader
    tool = Path(command).name.lower()

    out: dict[Path, list[str] | None] = dict.fromkeys(paths)
    by_name = {str(p): p for p in paths}
    for start in range(0, len(paths), _LINK_BATCH):
        batch = paths[start : start + _LINK_BATCH]
        try:
            proc = subprocess.run(
                [command, *flags, *(str(p) for p in batch)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        _parse(tool, proc.stdout, by_name, out)
    return out


def _parse(tool: str, text: str, by_name: dict[str, Path], out: dict[Path, list[str] | None]) -> None:
    current: Path | None = None
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if (found := _header(line, by_name)) is not None:
            current = found
            # A fat binary repeats its header per architecture; keep appending to one list.
            existing = out.get(found)
            names = existing if existing is not None else []
            out[current] = names
            continue
        if current is None or not line:
            continue
        if (name := _dependency(tool, line, current)) is not None:
            names.append(name)


def _dependency(tool: str, line: str, owner: Path) -> str | None:
    if "otool" in tool:
        name = line.split("(")[0].strip() if "(" in line else ""
        # `otool -L` opens with the file's own install name, which is not a dependency.
        return name if name and not name.endswith(owner.name) else None
    if "readelf" in tool:
        if "(NEEDED)" in line and "[" in line:
            return line.split("[", 1)[1].split("]", 1)[0]
        return None
    if line.startswith("NEEDED"):
        return line.split()[-1]
    if "dumpbin" in tool and line.lower().endswith(".dll"):
        return line
    return None


def _header(line: str, by_name: dict[str, Path]) -> Path | None:
    """Spot the line each reader prints before a file's dependencies.

    The four shapes are `path:` (otool), `path:  file format ...` (objdump), `File: path`
    (readelf) and `Dump of file path` (dumpbin). A Windows path contains a colon of its own,
    so the filename is matched against the batch rather than split on punctuation.
    """
    for prefix in ("Dump of file ", "File: "):
        if line.startswith(prefix) and (name := line.removeprefix(prefix).strip()) in by_name:
            return by_name[name]
    if line in by_name:
        return by_name[line]
    for name, path in by_name.items():
        # `path:`, `path:  file format ...`, and a fat binary's `path (architecture arm64):`.
        rest = line[len(name) :] if line.startswith(name) else None
        if rest is not None and (rest.startswith(":") or rest.startswith(" (")):
            return path
    return None


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
    """Mark bundled libraries reachable from a surviving extension, transitively.

    Bundled libraries link each other (`libxcb` needs `libXau`), and only the first hop is
    visible from the extension modules, so the closure has to be walked or the second hop is
    deleted out from under a library that is still loaded.
    """
    if not libs:
        return libs
    extensions = list(kept_extensions)
    if link_reader() is None:
        # Nothing can be shown to be unreferenced here, so nothing may be removed.
        for lib in libs.values():
            lib.referenced_by.add(f"(unreadable: {missing_reader_hint()})")
        return libs

    pending: list[str] = []
    for ext, links in linked_libraries_many(extensions).items():
        if tracker is not None:
            tracker.advance()
        for link in links or ():
            name = link.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if name in libs:
                libs[name].referenced_by.add(ext.name)
                pending.append(name)

    seen: set[str] = set()
    while pending:
        frontier = [n for n in dict.fromkeys(pending) if n not in seen]
        pending = []
        seen.update(frontier)
        if tracker is not None:
            tracker.advance(len(frontier))
        for lib, links in linked_libraries_many([libs[n].path for n in frontier]).items():
            for link in links or ():
                dep = link.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                if dep in libs and dep != lib.name:
                    libs[dep].referenced_by.add(lib.name)
                    if dep not in seen:
                        pending.append(dep)
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
