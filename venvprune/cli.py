"""Command line interface.

Every setting can also live in `[tool.venvprune]`; a flag given here overrides the file, and
list settings (`keep`, `code`, `--root`, group names) add to what the file declares.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from venvprune import apply as apply_mod
from venvprune import config as config_mod
from venvprune import native, progress, report, rewrite, risk, tree
from venvprune.analyzer import analyze
from venvprune.graph import Options
from venvprune.projectmeta import DEFAULT_DEV_GROUPS
from venvprune.trace import run_trace, venv_python


def _flag(group: argparse._ArgumentGroup | argparse.ArgumentParser, name: str, help_: str) -> None:
    """A tri-state flag: --x, --no-x, or unset so the config file decides."""
    dest = name.replace("-", "_")
    group.add_argument(f"--{name}", dest=dest, action="store_true", default=None, help=help_)
    group.add_argument(f"--no-{name}", dest=dest, action="store_false", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="venvprune",
        description="Find modules in a virtualenv that the given code can never import.",
    )
    parser.add_argument("code", nargs="*", type=Path, help="code root(s) to analyse")
    parser.add_argument("--venv", type=Path, default=None, help="virtualenv (or site-packages) to prune")
    parser.add_argument("--config", type=Path, default=None, help="TOML file holding [tool.venvprune]")
    parser.add_argument("--no-config", action="store_true", help="ignore any [tool.venvprune] section")
    parser.add_argument(
        "--format",
        choices=("text", "json", "paths", "tree", "rewrites", "defs", "risk", "native"),
        default=None,
        help="output format (default: text)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="list individual modules and hint sites")
    parser.add_argument(
        "--progress", choices=("auto", "never"), default=None, help="progress bar (default: auto on a terminal)"
    )
    parser.add_argument(
        "--keep",
        action="append",
        default=None,
        metavar="PATTERN",
        help="module, `pkg.*` subtree, or distribution that must never be pruned (repeatable)",
    )
    parser.add_argument(
        "--root",
        dest="extra_roots",
        action="append",
        default=None,
        metavar="MODULE",
        help="extra entry-point module to treat as used (repeatable)",
    )

    follow = parser.add_argument_group("edge following")
    _flag(follow, "lazy", "follow imports inside function bodies (default: on)")
    _flag(follow, "type-only", "follow TYPE_CHECKING-guarded imports (default: off)")
    _flag(follow, "reexport", "follow __init__.py re-export imports (default: on)")
    _flag(follow, "dynamic-expand", "keep a whole package that imports dynamically (default: on)")
    _flag(follow, "symbols", "follow an __init__ re-export only if the name it binds is used")
    _flag(follow, "prune-defs", "prune unreachable definitions inside surviving modules (implies --symbols)")
    _flag(follow, "risky-defs", "with --prune-defs, also cut definitions a decorator or base class might register")
    _flag(follow, "scan-binaries", "recover imports embedded in compiled extension modules")
    _flag(follow, "strict-dynamic", "keep nothing for an unbounded dynamic import")
    follow.add_argument(
        "--entry-point-group",
        dest="entry_point_groups",
        action="append",
        default=None,
        metavar="GROUP",
        help="treat modules advertised in this entry-point group as roots (repeatable)",
    )

    viz = parser.add_argument_group("tree view")
    viz.add_argument("--tree-depth", type=int, default=None, help="tree nesting depth (default: 3)")
    viz.add_argument("--tree-prunable", action="store_true", help="show only branches containing prunable modules")
    viz.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="colourise the tree")

    dev = parser.add_argument_group("dev dependencies")
    _flag(dev, "prune-dev", "prune distributions declared only in a dev group")
    dev.add_argument(
        "--dev-group",
        dest="dev_groups",
        action="append",
        default=None,
        metavar="NAME",
        help=f"dev group to treat as prunable (repeatable; default: {', '.join(DEFAULT_DEV_GROUPS)})",
    )
    dev.add_argument("--pyproject", type=Path, default=None, help="pyproject.toml to read groups from")

    fix = parser.add_argument_group("rewrites")
    fix.add_argument("--diff", action="store_true", help="show the full patch for --format rewrites/defs")
    fix.add_argument("--apply-rewrites", action="store_true", help="write the __init__ re-export rewrites to disk")
    fix.add_argument("--apply-defs", action="store_true", help="write the dead-definition removals to disk")
    fix.add_argument("--rewrite-dynamic", action="store_true", help="allow rewrites in dynamically-importing packages")
    fix.add_argument("--no-shim", action="store_true", help="omit the __getattr__ guard from rewrites")

    delete = parser.add_argument_group("deletion")
    delete.add_argument("--apply", action="store_true", help="delete unused modules and whole unused distributions")
    delete.add_argument("--dry-run", action="store_true", help="print the removal plan without deleting")
    _flag(delete, "keep-distributions", "remove only individual files, never a whole dist-info")
    _flag(delete, "prune-script-packages", "also remove unreachable distributions that install a command")

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


def _resolve(args: argparse.Namespace) -> tuple[list[Path], Path | None, Options, config_mod.FileConfig, str]:
    search = [*args.code, Path.cwd()]
    cfg = config_mod.FileConfig() if args.no_config else config_mod.load(args.config, search)
    if args.config is not None and cfg.empty_section:
        raise config_mod.ConfigError(f"{args.config} has no [tool.{config_mod.SECTION}] section")
    base = cfg.path.parent if cfg.path is not None else None

    code = [*(Path(p) for p in (cfg.get("code") or [])), *args.code]
    code = [p if p.is_absolute() or base is None else base / p for p in code]
    venv_value = args.venv or cfg.get("venv")
    venv = Path(venv_value) if venv_value else None
    if venv is not None and not venv.is_absolute() and base is not None and not args.venv:
        venv = base / venv

    pick = config_mod.pick
    prune_defs = pick(args.prune_defs, cfg, "prune-defs", False)
    dev_groups = config_mod.merge_list(args.dev_groups, cfg, "dev-groups")
    options = Options(
        follow_lazy=pick(args.lazy, cfg, "lazy", True),
        follow_type_only=pick(args.type_only, cfg, "type-only", False),
        follow_reexport=pick(args.reexport, cfg, "reexport", True),
        dynamic_expands_package=pick(args.dynamic_expand, cfg, "dynamic-expand", True),
        extra_roots=config_mod.merge_list(args.extra_roots, cfg, "roots"),
        keep=tuple(config_mod.merge_list(args.keep, cfg, "keep")),
        symbol_precision=pick(args.symbols, cfg, "symbols", False) or prune_defs,
        prune_definitions=prune_defs,
        include_risky_definitions=pick(args.risky_defs, cfg, "risky-defs", False),
        scan_binaries=pick(args.scan_binaries, cfg, "scan-binaries", False),
        strict_dynamic=pick(args.strict_dynamic, cfg, "strict-dynamic", False),
        entry_point_groups=tuple(config_mod.merge_list(args.entry_point_groups, cfg, "entry-point-groups")),
        prune_dev_groups=(tuple(dev_groups) or DEFAULT_DEV_GROUPS)
        if pick(args.prune_dev, cfg, "prune-dev", False)
        else None,
        pyproject=args.pyproject,
    )
    return code, venv, options, cfg, str(pick(args.format, cfg, "format", "text"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code, venv, options, cfg, output = _resolve(args)
    except config_mod.ConfigError as exc:
        print(f"venvprune: {exc}", file=sys.stderr)
        return 2

    if cfg.unknown:
        print(f"venvprune: ignoring unknown [tool.venvprune] keys: {', '.join(cfg.unknown)}", file=sys.stderr)
    if not code:
        print("venvprune: no code roots given (pass them, or set `code` in [tool.venvprune])", file=sys.stderr)
        return 2
    if venv is None:
        print("venvprune: no venv given (pass --venv, or set `venv` in [tool.venvprune])", file=sys.stderr)
        return 2
    for path in [*code, venv]:
        if not path.exists():
            print(f"venvprune: path does not exist: {path}", file=sys.stderr)
            return 2

    trace_result = None
    if args.trace:
        trace_result = run_trace(venv_python(venv), list(args.trace), cwd=args.trace_cwd, timeout=args.trace_timeout)
        if trace_result.returncode != 0:
            print(
                f"venvprune: traced command exited with {trace_result.returncode}; "
                "results reflect only the imports it reached before exiting",
                file=sys.stderr,
            )

    show_progress = config_mod.pick(args.progress, cfg, "progress", "auto") == "auto"
    try:
        with progress.reporter(show_progress) as reporter:
            analysis = analyze(code, venv, options, trace_result, reporter)
    except ValueError as exc:
        print(f"venvprune: {exc}", file=sys.stderr)
        return 2

    if analysis.unmatched_keep:
        print(
            f"venvprune: keep patterns matched nothing: {', '.join(sorted(analysis.unmatched_keep))}",
            file=sys.stderr,
        )

    if args.apply_defs:
        plans = rewrite.plan_definition_rewrites(analysis, include_risky=options.include_risky_definitions)
        for plan in plans:
            plan.apply()
        print(f"venvprune: rewrote {len(plans)} module(s)", file=sys.stderr)

    if args.apply_rewrites:
        shim_plans = rewrite.plan_rewrites(analysis, shim=not args.no_shim, allow_dynamic=args.rewrite_dynamic)
        for shim_plan in shim_plans:
            shim_plan.apply()
        print(f"venvprune: rewrote {len(shim_plans)} __init__.py file(s)", file=sys.stderr)

    if args.apply or args.dry_run:
        plan = apply_mod.build_plan(
            analysis,
            whole_distributions=not config_mod.pick(args.keep_distributions, cfg, "keep-distributions", False),
            prune_script_packages=config_mod.pick(args.prune_script_packages, cfg, "prune-script-packages", False),
        )
        print(apply_mod.render_plan(plan))
        if args.apply:
            manifest = apply_mod.execute(plan, analysis.site_dirs)
            print(f"venvprune: removed; manifest written to {manifest}", file=sys.stderr)
        return 0

    if output == "defs":
        print(
            rewrite.render_definition_plan(
                rewrite.plan_definition_rewrites(analysis, include_risky=options.include_risky_definitions),
                show_diff=args.diff,
            )
        )
    elif output == "risk":
        print(risk.render(risk.assess(analysis), verbose=args.verbose))
    elif output == "rewrites":
        print(
            rewrite.render_plan(
                rewrite.plan_rewrites(analysis, shim=not args.no_shim, allow_dynamic=args.rewrite_dynamic),
                show_diff=args.diff,
            )
        )
    elif output == "native":
        extensions = analysis.extension_modules()
        unused_names = {i.name for i in analysis.unused()}
        kept_paths = [i.path for n, i in extensions.items() if n not in unused_names]
        libs = native.attribute_libraries(kept_paths, native.bundled_libraries(analysis.site_dirs))
        print(native.render_report(native.find_variants(analysis.site_dirs), libs, extensions, unused_names))
    elif output == "tree":
        print(
            tree.render_tree(
                analysis,
                max_depth=config_mod.pick(args.tree_depth, cfg, "tree-depth", 3),
                only_prunable=args.tree_prunable,
                color={"auto": None, "always": True, "never": False}[args.color],
            )
        )
    elif output == "json":
        print(report.render_json(analysis))
    elif output == "paths":
        print(report.render_paths(analysis))
    else:
        print(report.render_text(analysis, verbose=args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
