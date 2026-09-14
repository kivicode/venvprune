# venvprune

Static (optionally runtime-assisted) analysis that answers: **which modules in this virtualenv
can I delete without breaking this code?**

It indexes every module in your code roots and in the venv's `site-packages`, parses each one,
builds an import graph across libraries, and reports everything unreachable from your code.

```bash
uv run venvprune ./myapp --venv ./.venv
uv run venvprune ./myapp --venv ./.venv --format tree --tree-prunable
uv run venvprune ./myapp --venv ./.venv --symbols --format rewrites --diff
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

Reaching a module records the *weakest* link in the chain, so a package pulled in only through
a lazy import inside another lazily-imported module is reported as a `lazy` keep.

## Symbol precision (`--symbols`)

`import numpy` followed by one `numpy.array(...)` does not mean the code needs all of numpy —
it needs whatever provides `array`. With `--symbols`, venvprune tracks which names each import
binds and which attributes the code actually reads off them, then propagates that demand set
through the graph. A package `__init__`'s re-export is followed only when someone needs the name
it binds.

Demand widens to *everything* when the module is used opaquely (`dir(pkg)`, passing `pkg`
around) or when a live `from pkg import *` is reached, so the narrowing is never a guess.

`--format rewrites` then emits the patch that makes those modules actually deletable: it drops
the dead re-export statements from `__init__.py` and leaves a PEP 562 `__getattr__` that names
venvprune if a removed symbol is touched after all. `--apply-rewrites` writes them (keeping a
`.venvprune-bak` beside each file). `__future__` imports, self-imports, and any package that
imports dynamically are never rewritten.

## Dynamic imports

`importlib.import_module`, `__import__`, `pkgutil.walk_packages`, `from x import *`,
entry-point loading and `getattr(<module>, <non-literal>)` are detected, and their argument is
folded as far as it can be:

| Call | Resolved to |
| --- | --- |
| `import_module("pkg.backend_a")` | that one module |
| `import_module(f"pkg.backend_{name}")` | everything under the literal prefix |
| `for n in ["a", "b"]: import_module(n)` | those two |
| `import_module("." + n, __package__)` | the importing package's subtree |
| `import_module(name)` | unbounded — the whole package is kept |

`--strict-dynamic` keeps nothing for a site that stays unbounded. `--format risk` rates every
site `resolved` / `confined` / `open` and names the packages that cannot be pruned safely.

Entry points are read from `dist-info` and become roots when your code calls `entry_points()`
with a literal group, or when you pass `--entry-point-group`.

## Compiled artifacts (`--format native`)

Wheels ship binaries that no AST pass can see into:

- **Embedded imports** — C code imports by name, so `--scan-binaries` recovers module names
  from an extension's string table and feeds them back into the graph as lazy edges.
- **Unloadable builds** — when a wheel ships several builds of one module, only the one
  matching this interpreter's ABI tag can ever load; the rest are reported as dead weight.
- **Bundled shared libraries** — `pkg/.dylibs` and `pkg.libs` are walked and attributed to the
  extensions that link them (`otool -L` / `objdump -p`), so libraries no surviving extension
  needs are listed.

## Dev dependencies (`--prune-dev`)

Distributions declared only in a dev group are not needed to run the code, so they are pruned
wholesale — even if a runtime dependency happens to import them — unless your own code imports
them directly. PEP 735 `[dependency-groups]`, `[project.optional-dependencies]` extras, and
poetry groups are all read. `--dev-group NAME` overrides the default set (`dev`, `test`,
`lint`, `docs`, `typing`, …).

## Runtime tracing

Static analysis cannot see through plugin registries or config-driven dispatch, so you can feed
in ground truth from an actual run:

```bash
uv run venvprune ./myapp --venv ./.venv --trace -m myapp.main --serve
```

The traced process is launched with the venv's own interpreter under a `sitecustomize` hook that
dumps `sys.modules` at exit. Traced modules are only ever *added* to the keep set — a trace
covers one execution path, so it is a lower bound, never a ceiling.

## Output formats

`text` (default), `tree`, `json`, `paths`, `rewrites`, `risk`, `native`. `-v` expands every
section into individual modules and sites.

## Development

```bash
uv sync
uv run pytest              # integration tests build real venvs with uv
uv run pytest -m "not integration"
uv run ruff check . && uv run ruff format --check .
uv run ty check
```

See [ROADMAP.md](ROADMAP.md) for what is done and what is left.
