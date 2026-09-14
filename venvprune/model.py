"""Core data model shared by the scanner, the resolver and the reporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Origin(StrEnum):
    LOCAL = "local"
    SITE = "site"
    STDLIB = "stdlib"


class EdgeKind(StrEnum):
    EAGER = "eager"
    """Executed at module import time."""

    LAZY = "lazy"
    """Inside a function/method body: executed only if that callable runs."""

    TYPE_ONLY = "type_only"
    """Guarded by `typing.TYPE_CHECKING` (or equivalent), never executed at runtime."""

    REEXPORT = "reexport"
    """Eager, but performed by an `__init__.py` purely to republish a name."""


class DynamicKind(StrEnum):
    IMPORTLIB = "importlib"
    DUNDER_IMPORT = "__import__"
    PKGUTIL = "pkgutil"
    GETATTR_MODULE = "getattr_module"
    IMPORT_STAR = "import_star"
    ENTRY_POINTS = "entry_points"


class Demand(StrEnum):
    ALL = "*"
    """The module is used opaquely, so every name it exposes must keep working."""


@dataclass(frozen=True)
class Binding:
    local: str
    """The name this import binds in the importing module's namespace."""

    remote: str
    """The attribute fetched from the target (`""` for a plain `import x`)."""


@dataclass(frozen=True)
class ImportEdge:
    src: str
    """Dotted name of the importing module."""

    target: str
    """Dotted name as written, already resolved for relative imports."""

    kind: EdgeKind
    lineno: int
    end_lineno: int = 0
    is_from: bool = False
    names: tuple[str, ...] = ()
    """`from X import a, b` -> ("a", "b"); each may be a submodule or an attribute."""

    bindings: tuple[Binding, ...] = ()
    """What this statement puts into the importing module's namespace, for symbol precision."""


class ArgShape(StrEnum):
    LITERAL = "literal"
    """A constant module name: fully resolved."""

    PREFIX = "prefix"
    """A literal prefix followed by something computed, e.g. f"pkg.backend_{name}"."""

    CHOICES = "choices"
    """The name comes from a bounded set of literals visible in the same module."""

    UNKNOWN = "unknown"
    """Nothing can be said about the name."""


@dataclass(frozen=True)
class DynamicHint:
    module: str
    kind: DynamicKind
    lineno: int
    detail: str
    shape: ArgShape = ArgShape.UNKNOWN
    values: tuple[str, ...] = ()
    """The literal(s) or prefix recovered from the call argument."""

    package_arg: str | None = None
    """The `package=` argument of `importlib.import_module`, when it was a literal."""

    @property
    def bounded(self) -> bool:
        return self.shape is not ArgShape.UNKNOWN


@dataclass
class ModuleInfo:
    name: str
    path: Path
    origin: Origin
    is_package: bool = False
    is_stub_only: bool = False
    shadowed: list[Path] = field(default_factory=list)
    """Other files backing the same dotted name (a `.so` beside a `.py`, a `.pyi` stub).

    Only `path` is analysed, but every one of these must go when the module is pruned.
    """
    edges: list[ImportEdge] = field(default_factory=list)
    hints: list[DynamicHint] = field(default_factory=list)
    parse_error: str | None = None
    used_attrs: dict[str, set[str] | Demand] = field(default_factory=dict)
    """Local binding -> the attributes this module actually reads off it."""

    exported: tuple[str, ...] | None = None
    """`__all__`, when declared as a literal list of strings."""

    table: object = None
    """A `symbols.SymbolTable`; typed loosely to keep the model free of analysis imports."""

    @property
    def parent(self) -> str | None:
        return self.name.rpartition(".")[0] or None


@dataclass
class Distribution:
    name: str
    version: str
    top_level: set[str] = field(default_factory=set)
    files: list[Path] = field(default_factory=list)
    entry_points: dict[str, dict[str, str]] = field(default_factory=dict)
    """group -> {name: "module.path:attr"}."""

    @property
    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.files if p.exists())


@dataclass
class Reachability:
    reached: dict[str, EdgeKind]
    """Module -> the strongest (most certain) edge kind by which it was reached."""

    why: dict[str, ImportEdge]
    """Module -> the edge that first reached it, for `--explain`."""

    unresolved: list[ImportEdge]
    demand: dict[str, set[str] | Demand] = field(default_factory=dict)
    """Module -> the names anything actually asks of it."""

    skipped_reexports: dict[str, list[ImportEdge]] = field(default_factory=dict)
    """Package -> `__init__` imports skipped because nothing needed the names they bind."""
