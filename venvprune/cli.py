"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from venvprune import apply as apply_mod
from venvprune import native, progress, report, rewrite, risk, tree
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
        choices=("text", "json", "paths", "tree", "rewrites", "defs", "risk", "native"),
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "--progress",
        choices=("auto", "never"),
        default="auto",
        help="show a progress bar while analysing (default: auto, when stderr is a terminal)",
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

    follow.add_argument(
        "--prune-defs",
        action="store_true",
        help="also prune unreachable functions/classes inside modules that survive (implies --symbols)",
    )
    follow.add_argument(
        "--risky-defs",
        action="store_true",
        help="with --prune-defs, also cut definitions a decorator or foreign base might register",
    )
    follow.add_argument(
        "--scan-binaries",
        action="store_true",
        help="recover imports embedded in compiled extension modules by scanning their strings",
    )
    follow.add_argument(
        "--strict-dynamic",
        action="store_true",
        help="keep nothing for an unbounded dynamic import instead of its whole package",
    )
    follow.add_argument(
        "--entry-point-group",
        dest="entry_point_groups",
        action="append",
        default=[],
        metavar="GROUP",
        help="treat modules advertised in this entry-point group as roots (repeatable)",
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
    fix.add_argument(
        "--apply-defs",
        action="store_true",
        help="write the dead-definition removals to disk (keeps a .venvprune-bak beside each file)",
    )
    fix.add_argument("--no-shim", action="store_true", help="omit the __getattr__ guard from rewrites")

    delete = parser.add_argument_group("deletion")
    delete.add_argument(
        "--apply",
        action="store_true",
        help="delete everything found: unused modules, whole unused distributions, orphaned libraries",
    )
    delete.add_argument("--dry-run", action="store_true", help="print the removal plan without deleting")
    delete.add_argument(
        "--keep-distributions",
        action="store_true",
        help="with --apply, remove only individual files, never a whole dist-info",
    )

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
        symbol_precision=args.symbols or args.prune_defs,
        prune_definitions=args.prune_defs,
        include_risky_definitions=args.risky_defs,
        strict_dynamic=args.strict_dynamic,
        scan_binaries=args.scan_binaries,
        entry_point_groups=tuple(args.entry_point_groups),
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
        with progress.reporter(args.progress == "auto") as reporter:
            analysis = analyze(args.code, args.venv, options, trace_result, reporter)
    except ValueError as exc:
        print(f"venvprune: {exc}", file=sys.stderr)
        return 2

    if args.apply_defs:
        plans = rewrite.plan_definition_rewrites(analysis, include_risky=args.risky_defs)
        for plan in plans:
            plan.apply()
        print(f"venvprune: rewrote {len(plans)} module(s)", file=sys.stderr)

    if args.apply_rewrites:
        plans = rewrite.plan_rewrites(analysis, shim=not args.no_shim, allow_dynamic=args.rewrite_dynamic)
        for plan in plans:
            plan.apply()
        print(f"venvprune: rewrote {len(plans)} __init__.py file(s)", file=sys.stderr)

    if args.apply or args.dry_run:
        plan = apply_mod.build_plan(analysis, whole_distributions=not args.keep_distributions)
        print(apply_mod.render_plan(plan))
        if args.apply:
            manifest = apply_mod.execute(plan, analysis.site_dirs)
            print(f"venvprune: removed; manifest written to {manifest}", file=sys.stderr)
        return 0

    if args.format == "defs":
        print(
            rewrite.render_definition_plan(
                rewrite.plan_definition_rewrites(analysis, include_risky=args.risky_defs),
                show_diff=args.diff,
            )
        )
        return 0

    if args.format == "native":
        extensions = analysis.extension_modules()
        unused_names = {i.name for i in analysis.unused()}
        kept_paths = [i.path for n, i in extensions.items() if n not in unused_names]
        libs = native.attribute_libraries(kept_paths, native.bundled_libraries(analysis.site_dirs))
        print(native.render_report(native.find_variants(analysis.site_dirs), libs, extensions, unused_names))
        return 0

    if args.format == "risk":
        print(risk.render(risk.assess(analysis), verbose=args.verbose))
        return 0

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
