"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from venvprune import report
from venvprune.analyzer import analyze
from venvprune.graph import Options
from venvprune.trace import run_trace, venv_python


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="venvprune",
        description="Find modules in a virtualenv that the given code can never import.",
    )
    parser.add_argument("code", nargs="+", type=Path, help="code root(s) to analyse")
    parser.add_argument("--venv", required=True, type=Path, help="virtualenv (or site-packages) to prune")
    parser.add_argument(
        "--format", choices=("text", "json", "paths"), default="text", help="output format (default: text)"
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

    renderers = {"text": report.render_text, "json": report.render_json, "paths": report.render_paths}
    if args.format == "text":
        print(report.render_text(analysis, verbose=args.verbose))
    else:
        print(renderers[args.format](analysis))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
