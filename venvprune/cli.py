"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from venvprune import report, rewrite, tree
from venvprune.analyzer import analyze
from venvprune.graph import Options
from venvprune.projectmeta import DEFAULT_DEV_GROUPS
from venvprune.trace import run_trace, venv_python


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="venvprune",
        description="Find modules in a virtualenv that the given code can never import.",
    )
    parser.add_argument("code", nargs="+", type=Path, help="code root(s) to analyse")
    parser.add_argument("--venv", required=True, type=Path, help="virtualenv (or site-packages) to prune")
    parser.add_argument(
        "--format",
        choices=("text", "json", "paths", "tree", "rewrites"),
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="list individual modules and hint sites")
    parser.add_argument(
        "--root",
        dest="extra_roots",
        action="append",
        default=[],
        metavar="MODULE",
        help="extra entry-point module to treat as used (repeatable)",
    )

    follow = parser.add_argument_group("edge following")
    follow.add_argument("--no-lazy", action="store_true", help="ignore imports inside function bodies")
    follow.add_argument("--type-only", action="store_true", help="also follow TYPE_CHECKING-guarded imports")
    follow.add_argument("--no-reexport", action="store_true", help="ignore __init__.py re-export imports")
    follow.add_argument(
        "--no-dynamic-expand",
        action="store_true",
        help="do not keep a whole package just because it performs dynamic imports",
    )

    follow.add_argument(
        "--symbols",
        action="store_true",
        help="symbol precision: follow an __init__ re-export only if the name it binds is used",
    )

    viz = parser.add_argument_group("tree view")
    viz.add_argument("--tree-depth", type=int, default=3, help="tree nesting depth (default: 3)")
    viz.add_argument("--tree-prunable", action="store_true", help="show only branches containing prunable modules")
    viz.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="colourise the tree")

    dev = parser.add_argument_group("dev dependencies")
    dev.add_argument(
        "--prune-dev",
        action="store_true",
        help="also prune distributions declared only in a dev group, unless your code imports them",
    )
    dev.add_argument(
        "--dev-group",
        dest="dev_groups",
        action="append",
        default=[],
        metavar="NAME",
        help=f"dev group name to treat as prunable (repeatable; default: {', '.join(DEFAULT_DEV_GROUPS)})",
    )
    dev.add_argument("--pyproject", type=Path, default=None, help="pyproject.toml to read groups from")

    fix = parser.add_argument_group("phase 2 rewrites")
    fix.add_argument("--diff", action="store_true", help="show the full patch for --format rewrites")
    fix.add_argument(
        "--apply-rewrites",
        action="store_true",
        help="write the re-export rewrites to disk (keeps a .venvprune-bak beside each file)",
    )
    fix.add_argument(
        "--rewrite-dynamic",
        action="store_true",
        help="allow rewrites in packages that import dynamically (unsafe)",
    )
    fix.add_argument("--no-shim", action="store_true", help="omit the __getattr__ guard from rewrites")

    tracing = parser.add_argument_group("runtime tracing")
    tracing.add_argument(
        "--trace",
        metavar="ARG",
        nargs=argparse.REMAINDER,
        help="run the venv's python with these args and keep everything it imports",
    )
    tracing.add_argument("--trace-cwd", type=Path, default=None, help="working directory for --trace")
    tracing.add_argument("--trace-timeout", type=float, default=None, help="seconds before the traced run is killed")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for path in [*args.code, args.venv]:
        if not path.exists():
            print(f"venvprune: path does not exist: {path}", file=sys.stderr)
            return 2

    options = Options(
        follow_lazy=not args.no_lazy,
        follow_type_only=args.type_only,
        follow_reexport=not args.no_reexport,
        dynamic_expands_package=not args.no_dynamic_expand,
        extra_roots=list(args.extra_roots),
        symbol_precision=args.symbols,
        prune_dev_groups=(tuple(args.dev_groups) or DEFAULT_DEV_GROUPS) if args.prune_dev else None,
        pyproject=args.pyproject,
    )

    trace_result = None
    if args.trace:
        trace_result = run_trace(
            venv_python(args.venv), list(args.trace), cwd=args.trace_cwd, timeout=args.trace_timeout
        )
        if trace_result.returncode != 0:
            print(
                f"venvprune: traced command exited with {trace_result.returncode}; "
                "results reflect only the imports it reached before exiting",
                file=sys.stderr,
            )

    try:
        analysis = analyze(args.code, args.venv, options, trace_result)
    except ValueError as exc:
        print(f"venvprune: {exc}", file=sys.stderr)
        return 2

    if args.apply_rewrites:
        plans = rewrite.plan_rewrites(analysis, shim=not args.no_shim, allow_dynamic=args.rewrite_dynamic)
        for plan in plans:
            plan.apply()
        print(f"venvprune: rewrote {len(plans)} __init__.py file(s)", file=sys.stderr)

    if args.format == "rewrites":
        print(
            rewrite.render_plan(
                rewrite.plan_rewrites(analysis, shim=not args.no_shim, allow_dynamic=args.rewrite_dynamic),
                show_diff=args.diff,
            )
        )
        return 0

    if args.format == "text":
        print(report.render_text(analysis, verbose=args.verbose))
    elif args.format == "tree":
        print(
            tree.render_tree(
                analysis,
                max_depth=args.tree_depth,
                only_prunable=args.tree_prunable,
                color={"auto": None, "always": True, "never": False}[args.color],
            )
        )
    elif args.format == "json":
        print(report.render_json(analysis))
    else:
        print(report.render_paths(analysis))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
