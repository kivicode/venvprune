"""AST-level extraction of import edges and dynamic-import hints from one module."""

from __future__ import annotations

import ast

from venvprune.model import (
    ArgShape,
    Binding,
    Demand,
    DynamicHint,
    DynamicKind,
    EdgeKind,
    ImportEdge,
    ModuleInfo,
)

_IMPORTLIB_FUNCS = {"import_module", "__import__", "find_spec", "reload", "invalidate_caches"}
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

        self.used: dict[str, set[str] | Demand] = {}
        """Imported binding -> attributes read off it, or Demand.ALL for opaque use."""

        self.exported: tuple[str, ...] | None = None
        self._literals: dict[str, tuple[str, ...]] = {}
        """Local name -> the literal strings it can hold, for bounded dynamic imports."""

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
            root = alias.name.split(".")[0]
            bound = alias.asname or root
            # `import a.b` binds `a`, so attribute use is tracked against the root, not a.b.
            self.edges.append(
                ImportEdge(
                    self.module,
                    alias.name,
                    kind,
                    node.lineno,
                    node.end_lineno or node.lineno,
                    bindings=(Binding(bound, ""),),
                )
            )
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
        bindings = tuple(Binding(alias.asname or alias.name, alias.name) for alias in node.names if alias.name != "*")
        self.edges.append(
            ImportEdge(self.module, target, self._kind(), node.lineno, is_from=True, names=names, bindings=bindings)
        )
        for binding in bindings:
            self._module_locals.add(binding.local)
        if target == "importlib" or target.startswith("importlib."):
            # Only the functions that actually import: `importlib.resources.files` and friends
            # read package data and must not be mistaken for a dynamic import.
            self._importlib_aliases.update(
                a.asname or a.name for a in node.names if a.name in _IMPORTLIB_FUNCS or a.name == "util"
            )
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

    # --- usage ----------------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        base = node.value
        if isinstance(base, ast.Name):
            self._record(base.id, node.attr)
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # A bare reference that is not `name.attr` means the object escapes our sight.
        self._record(node.id, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self.exported is None and (names := _all_literal(node)) is not None:
            self.exported = names
        strings = _string_literals(node.value)
        if strings:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._literals[target.id] = strings
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        strings = _string_literals(node.iter)
        if not strings and isinstance(node.iter, ast.Name):
            strings = self._literals.get(node.iter.id, ())
        if strings and isinstance(node.target, ast.Name):
            self._literals[node.target.id] = strings
        self.generic_visit(node)

    def _record(self, name: str, attr: str | None) -> None:
        current = self.used.get(name)
        if current is Demand.ALL:
            return
        if attr is None:
            self.used[name] = Demand.ALL
            return
        if current is None:
            self.used[name] = {attr}
        else:
            current.add(attr)

    # --- dynamic hints --------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        dotted = _dotted(node.func)
        if dotted:
            hint = self._classify_call(dotted, node)
            if hint is not None:
                shape, values, package = self._shape_of(hint, node)
                self.hints.append(
                    DynamicHint(self.module, hint, node.lineno, _render(node, dotted), shape, values, package)
                )
        self.generic_visit(node)

    def _shape_of(self, kind: DynamicKind, node: ast.Call) -> tuple[ArgShape, tuple[str, ...], str | None]:
        if kind in {DynamicKind.PKGUTIL, DynamicKind.GETATTR_MODULE, DynamicKind.ENTRY_POINTS}:
            return self._plain_arg_shape(node)
        if not node.args:
            return ArgShape.UNKNOWN, (), None
        shape, values = self._expr_shape(node.args[0])
        package = None
        for candidate in [*node.args[1:2], *(kw.value for kw in node.keywords if kw.arg == "package")]:
            if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                package = candidate.value
            elif _dotted(candidate) == "__package__":
                package = self.package
        return shape, values, package

    def _plain_arg_shape(self, node: ast.Call) -> tuple[ArgShape, tuple[str, ...], str | None]:
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return ArgShape.LITERAL, (arg.value,), None
        return ArgShape.UNKNOWN, (), None

    def _expr_shape(self, expr: ast.expr) -> tuple[ArgShape, tuple[str, ...]]:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return ArgShape.LITERAL, (expr.value,)
        if isinstance(expr, ast.Name) and expr.id in self._literals:
            return ArgShape.CHOICES, self._literals[expr.id]
        prefix = _literal_prefix(expr)
        if prefix:
            return ArgShape.PREFIX, (prefix,)
        return ArgShape.UNKNOWN, ()

    def _classify_call(self, dotted: str, node: ast.Call) -> DynamicKind | None:
        head, _, tail = dotted.partition(".")
        leaf = dotted.rsplit(".", 1)[-1]
        if dotted == "__import__":
            return DynamicKind.DUNDER_IMPORT
        if leaf in {"entry_points", "load_entry_point"}:
            return DynamicKind.ENTRY_POINTS
        if head in self._importlib_aliases and (leaf in _IMPORTLIB_FUNCS or not tail):
            return DynamicKind.IMPORTLIB
        if head in self._pkgutil_aliases and (leaf in _PKGUTIL_FUNCS or not tail):
            return DynamicKind.PKGUTIL
        if dotted == "getattr" and node.args:
            base = _dotted(node.args[0])
            if base and base.split(".")[0] in self._module_locals and not _is_literal(node.args[1:2]):
                return DynamicKind.GETATTR_MODULE
        return None


def _is_literal(args: list[ast.expr]) -> bool:
    """A constant attribute name is a plain lookup, not a dynamic dispatch."""
    return bool(args) and isinstance(args[0], ast.Constant)


def _string_literals(expr: ast.expr) -> tuple[str, ...]:
    """Every string in a literal list/tuple/set, or a single constant; empty if not all literal."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return (expr.value,)
    if not isinstance(expr, ast.List | ast.Tuple | ast.Set):
        return ()
    items = [e.value for e in expr.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return tuple(items) if items and len(items) == len(expr.elts) else ()


def _literal_prefix(expr: ast.expr) -> str:
    """The constant head of an f-string or `+` concatenation, e.g. `pkg.backend_`."""
    if isinstance(expr, ast.JoinedStr):
        head = expr.values[0] if expr.values else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
        return ""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _literal_prefix(expr.left)
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return ""


def _all_literal(node: ast.Assign) -> tuple[str, ...] | None:
    if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
        return None
    if not isinstance(node.value, ast.List | ast.Tuple):
        return None
    items = [e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return tuple(items) if len(items) == len(node.value.elts) else None


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
    info.used_attrs = visitor.used
    info.exported = visitor.exported
    return info


def scan_all(modules: dict[str, ModuleInfo]) -> None:
    for info in modules.values():
        scan_module(info)
