# venvprune

Static (optionally runtime-assisted) analysis that answers: **which modules in this virtualenv
can I delete without breaking this code?**

It indexes every module in your code roots and in the venv's `site-packages`, parses each one,
builds an import graph across libraries, and reports everything unreachable from your code.

```bash
uv run venvprune ./myapp --venv ./.venv
uv run venvprune ./myapp --venv ./.venv -v --format json > prune.json
uv run venvprune ./myapp --venv ./.venv --format paths | xargs rm   # at your own risk
```

## What it models

| Import form | Classified as | Followed by default |
| --- | --- | --- |
| module-level `import x` | `eager` | yes |
| `import x` in a function body | `lazy` | yes (`--no-lazy` to drop) |
| import in `__init__.py` | `reexport` | yes (`--no-reexport` to drop) |
| import under `if TYPE_CHECKING:` | `type_only` | no (`--type-only` to keep) |

Relative imports, PEP 420 namespace packages, `.pyi` stubs, and native extension modules
(`.so` / `.pyd`) are all indexed. A submodule always drags in every parent `__init__`.

Reaching a module records the *weakest* link in the chain: a package pulled in only through a
lazy import inside another lazily-imported module is reported as a `lazy` keep, which is what
phase 2 will act on.

## Dynamic imports

`importlib.import_module`, `__import__`, `pkgutil.walk_packages`, `from x import *`,
entry-point loading and `getattr(<module>, <non-literal>)` are detected as risk sites. By
default a module containing one keeps its whole sibling subtree alive (conservative);
`--no-dynamic-expand` turns that off and reports the sites instead.

## Runtime tracing

Static analysis cannot see through plugin registries or config-driven dispatch, so you can feed
in ground truth from an actual run:

```bash
uv run venvprune ./myapp --venv ./.venv --trace -m myapp.main --serve
```

The traced process is launched with the venv's own interpreter under a `sitecustomize` hook that
dumps `sys.modules` at exit. Traced modules are only ever *added* to the keep set — a trace
covers one execution path, so it is a lower bound, never a ceiling.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run ty check
```

See [ROADMAP.md](ROADMAP.md) for phases 2 and 3.
