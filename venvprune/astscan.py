"""AST-level extraction of import edges and dynamic-import hints from one module."""

from __future__ import annotations

import ast

from venvprune.model import DynamicHint, DynamicKind, EdgeKind, ImportEdge, ModuleInfo

_IMPORTLIB_FUNCS = {"import_module", "__import__", "find_spec", "util.find_spec"}
_PKGUTIL_FUNCS = {"iter_modules", "walk_packages", "get_loader", "resolve_name", "extend_path"}


class _Visitor(ast.NodeVisitor):
    def __init__(self, module: str, package: str, is_init: bool) -> None:
        self.module = module
        self.package = package
        self.is_init = is_init
        self.edges: list[ImportEdge] = []
        self.hints: list[DynamicHint] = []
        self._depth = 0  # function nesting: >0 means the import is deferred
        self._type_checking = 0
        self._importlib_aliases: set[str] = set()
        self._pkgutil_aliases: set[str] = set()
        self._module_locals: set[str] = set()
        """Names bound to a module object, so `getattr(name, ...)` is a dynamic import risk."""

    # --- classification -------------------------------------------------
    def _kind(self) -> EdgeKind:
        if self._type_checking:
            return EdgeKind.TYPE_ONLY
        if self._depth:
            return EdgeKind.LAZY
        if self.is_init:
            return EdgeKind.REEXPORT
        return EdgeKind.EAGER

    def _resolve(self, level: int, module: str | None) -> str | None:
        if level == 0:
            return module
        base = self.package.split(".") if self.package else []
        # `from . import x` inside a package body resolves against the package itself.
        trim = level - 1
        if trim > len(base):
            return None
        anchor = base[: len(base) - trim] if trim else base
        parts = [*anchor, *(module.split(".") if module else [])]
        return ".".join(parts) if parts else None

    # --- imports --------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        kind = self._kind()
        for alias in node.names:
            self.edges.append(ImportEdge(self.module, alias.name, kind, node.lineno))
            root = alias.name.split(".")[0]
            bound = alias.asname or root
            self._module_locals.add(bound)
            if root == "importlib":
                self._importlib_aliases.add(bound)
            elif root == "pkgutil":
                self._pkgutil_aliases.add(bound)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        target = self._resolve(node.level, node.module)
        if target is None:
            return
        names = tuple(a.name for a in node.names)
        if "*" in names:
            self.hints.append(DynamicHint(self.module, DynamicKind.IMPORT_STAR, node.lineno, f"from {target} import *"))
        self.edges.append(ImportEdge(self.module, target, self._kind(), node.lineno, is_from=True, names=names))
        for alias in node.names:
            bound = alias.asname or alias.name
            if bound != "*":
                self._module_locals.add(bound)
        if target == "importlib" or target.startswith("importlib."):
            self._importlib_aliases.update(a.asname or a.name for a in node.names)
        elif target == "pkgutil":
            self._pkgutil_aliases.update(a.asname or a.name for a in node.names)

    # --- scopes ---------------------------------------------------------
    def _visit_func(self, node: ast.AST) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            self._type_checking += 1
            for child in node.body:
                self.visit(child)
            self._type_checking -= 1
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    # --- dynamic hints --------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        dotted = _dotted(node.func)
        if dotted:
            hint = self._classify_call(dotted, node)
            if hint is not None:
                self.hints.append(DynamicHint(self.module, hint, node.lineno, _render(node, dotted)))
        self.generic_visit(node)

    def _classify_call(self, dotted: str, node: ast.Call) -> DynamicKind | None:
        head, _, tail = dotted.partition(".")
        leaf = dotted.rsplit(".", 1)[-1]
        if dotted == "__import__":
            return DynamicKind.DUNDER_IMPORT
        if head in self._importlib_aliases and (leaf in _IMPORTLIB_FUNCS or not tail):
            return DynamicKind.IMPORTLIB
        if head in self._pkgutil_aliases and (leaf in _PKGUTIL_FUNCS or not tail):
            return DynamicKind.PKGUTIL
        if leaf in {"entry_points", "load_entry_point"}:
            return DynamicKind.ENTRY_POINTS
        if dotted == "getattr" and node.args:
            base = _dotted(node.args[0])
            if base and base.split(".")[0] in self._module_locals and not _is_literal(node.args[1:2]):
                return DynamicKind.GETATTR_MODULE
        return None


def _is_literal(args: list[ast.expr]) -> bool:
    """A constant attribute name is a plain lookup, not a dynamic dispatch."""
    return bool(args) and isinstance(args[0], ast.Constant)


def _is_type_checking(test: ast.expr) -> bool:
    dotted = _dotted(test)
    if dotted in {"TYPE_CHECKING", "typing.TYPE_CHECKING", "t.TYPE_CHECKING"}:
        return True
    return isinstance(test, ast.Constant) and test.value is False


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _render(node: ast.Call, dotted: str) -> str:
    args = []
    for arg in node.args[:2]:
        if isinstance(arg, ast.Constant):
            args.append(repr(arg.value))
        else:
            args.append(_dotted(arg) or "<expr>")
    return f"{dotted}({', '.join(args)})"


def scan_module(info: ModuleInfo) -> ModuleInfo:
    """Populate `info.edges` / `info.hints` in place. Non-source modules are left empty."""
    if info.path.suffix not in {".py", ".pyi"} or not info.path.exists():
        return info
    try:
        source = info.path.read_bytes()
        tree = ast.parse(source, filename=str(info.path))
    except (SyntaxError, ValueError, OSError) as exc:
        info.parse_error = f"{type(exc).__name__}: {exc}"
        return info
    package = info.name if info.is_package else info.name.rpartition(".")[0]
    visitor = _Visitor(info.name, package, info.is_package)
    visitor.visit(tree)
    info.edges = visitor.edges
    info.hints = visitor.hints
    return info


def scan_all(modules: dict[str, ModuleInfo]) -> None:
    for info in modules.values():
        scan_module(info)
