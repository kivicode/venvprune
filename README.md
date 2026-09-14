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

| Import form                      | Classified as | Followed by default           |
| -------------------------------- | ------------- | ----------------------------- |
| module-level `import x`          | `eager`       | yes                           |
| `import x` in a function body    | `lazy`        | yes (`--no-lazy` to drop)     |
| import in `__init__.py`          | `reexport`    | yes (`--no-reexport` to drop) |
| import under `if TYPE_CHECKING:` | `type_only`   | no (`--type-only` to keep)    |

Relative imports, PEP 420 namespace packages, `.pyi` stubs, and native extension modules
(`.so` / `.pyd`) are all indexed. A submodule always drags in every parent `__init__`.

Reaching a module records the _weakest_ link in the chain, so a package pulled in only through
a lazy import inside another lazily-imported module is reported as a `lazy` keep.

## Symbol precision (`--symbols`)

`import numpy` followed by one `numpy.array(...)` does not mean the code needs all of numpy —
it needs whatever provides `array`. With `--symbols`, venvprune tracks which names each import
binds and which attributes the code actually reads off them, then propagates that demand set
through the graph. A package `__init__`'s re-export is followed only when someone needs the name
it binds.

Demand widens to _everything_ when the module is used opaquely (`dir(pkg)`, passing `pkg`
around) or when a live `from pkg import *` is reached, so the narrowing is never a guess.

`--format rewrites` then emits the patch that makes those modules actually deletable: it drops
the dead re-export statements from `__init__.py` and leaves a PEP 562 `__getattr__` that names
venvprune if a removed symbol is touched after all. `--apply-rewrites` writes them (keeping a
`.venvprune-bak` beside each file). `__future__` imports, self-imports, and any package that
imports dynamically are never rewritten.

## Definition pruning (`--prune-defs`)

A module that survives because one function in it is needed still ships every other function,
class and constant it defines. `--prune-defs` builds a symbol table per module, computes which
top-level definitions are still live given what the outside world asks of that module, and
reports the rest as removable — which in turn frees the imports only they used, which in turn
frees whole modules behind those imports.

```bash
uv run venvprune ./myapp --venv ./.venv --prune-defs --format defs --diff
uv run venvprune ./myapp --venv ./.venv --prune-defs --apply-defs
```

Rewrites narrow rather than blindly delete: `from .core import a, b` becomes
`from .core import a`, and `__all__` loses the names that went with it.

A module is left entirely alone when it defines `__getattr__`, calls `eval`/`exec`/`globals`,
uses a star import, or imports dynamically. Within a prunable module, a definition is kept when
it is decorated with anything not known to be a no-op (it may be registering itself), when a
class derives from a foreign base (`__init_subclass__` and metaclasses register subclasses),
or when it is a dunder. `--risky-defs` cuts those too. `__future__` imports are never touched —
removing one changes how the rest of the file compiles.

## Dynamic imports

`importlib.import_module`, `__import__`, `pkgutil.walk_packages`, `from x import *`,
entry-point loading and `getattr(<module>, <non-literal>)` are detected, and their argument is
folded as far as it can be:

| Call                                    | Resolved to                           |
| --------------------------------------- | ------------------------------------- |
| `import_module("pkg.backend_a")`        | that one module                       |
| `import_module(f"pkg.backend_{name}")`  | everything under the literal prefix   |
| `for n in ["a", "b"]: import_module(n)` | those two                             |
| `import_module("." + n, __package__)`   | the importing package's subtree       |
| `import_module(name)`                   | unbounded — the whole package is kept |

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
  extensions that link them, transitively, so a library needed only by another library is not
  deleted out from under it. Link tables are read with the platform's own tool, batched:
  `otool` on macOS, `objdump` or `readelf` on Linux, `dumpbin` or `objdump` on Windows. When
  none is installed nothing is reported as unreferenced, so no library is removed.

## What it will not remove

Some things are reachable only by mechanisms no import graph can see. These are protected by
default, and each one was learned by pruning a real project and then running it:

- **Distributions advertising entry points.** A `console_scripts` launcher imports the package;
  a `pytest11` or application-defined group is found by a framework scanning metadata. Nothing
  imports either. Pruning `pytest-asyncio` does not fail — the async tests simply stop being
  collected. `--prune-script-packages` removes them anyway.
- **`pkg/__main__.py`** of a surviving package, which is what `python -m pkg` runs.
- **Modules named by a `.pth` file**, which the interpreter executes at startup.
- **Bundled libraries a surviving library links**, walked transitively: `libxcb` needs
  `libXau`, and only the extension-to-library hop is visible from the module graph.
- **Anything in `keep`** — for a package invoked as a subprocess, or a plugin named in a
  config file. Patterns that match nothing are reported rather than ignored.

Sizes are reported per module, and every file backing one goes together: `cu2qu.py`,
`cu2qu.cpython-312-darwin.so` and `cu2qu.pyi` are one module, not three.

## Auditing the declarations (`--format deps`)

A different question from module pruning: not "can this file go" but "is this line in
`pyproject.toml` earning its place". Each declared dependency — main, extras and groups — is
classified against what the analysed code actually imports:

| status | meaning |
| --- | --- |
| `used` | imported by first-party code |
| `indirect` | reachable, but only because another package imports it — you are relying on someone else's dependency |
| `unused` | nothing reaches it at all |
| `missing` | declared but not installed |
| `conditional` | not installed, but an environment marker excludes this platform |

Distributions are mapped to modules through `RECORD`, never by guessing from the name, because
they often differ — `html-for-docx` installs `html4docx`.

Point it at the production roots only (leave `tests/` out) and a dependency used solely by the
test suite shows up as `unused`, which is usually a sign it belongs in a dev group.

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
dumps `sys.modules` at exit. Traced modules are only ever _added_ to the keep set — a trace
covers one execution path, so it is a lower bound, never a ceiling.

## Deleting it (`--apply`)

`--dry-run` prints the removal plan, and honours `--format`, so a dry run can show the analysis
and the plan together:

```bash
uv run venvprune ./myapp --venv ./.venv --dry-run --format tree --tree-prunable
```

`--tree-full` expands every module: no depth limit, no collapsing of wholly-prunable subtrees.

`--apply` carries the plan out and writes a manifest first. `--apply` carries it out and writes a manifest first.
A distribution nothing reaches goes whole — its package directories, its `dist-info`, its data
files and its bundled shared libraries — not just the `.py` files the module graph knows about.
Modules named by a `.pth` file are always kept, since the interpreter runs those at startup.

## Speed

A 12k-module virtualenv analyses in roughly 20 seconds. Parsing runs across processes — one per
CPU by default, `-j N` to change it, `-j 1` to stay in-process; results are identical either
way, and a pool that cannot start falls back rather than failing. `ast.parse` holds the GIL, so
threads would buy nothing here.

Two things that are not parallelism mattered more: a package root used to walk its entire
parent directory (including the venv) before filtering by name, and link tables were read one
subprocess per extension module.

## Progress

Large virtualenvs take a while (~50 s to parse 11.5k modules), so the slow phases report
progress on stderr: a `rich` bar when `rich` is installed and stderr is a terminal, one
rewritten stderr line otherwise, nothing when piped. `--progress never` turns it off.

`rich` is an optional extra (`pip install venvprune[ui]`) rather than a dependency: this tool
deletes things out of virtualenvs, so it has to keep working when `rich` is not there.

## Output formats

`text` (default), `tree`, `json`, `paths`, `rewrites`, `defs`, `risk`, `native`. `-v` expands
every section into individual modules and sites.

## Layout

The package is layered along the pipeline; a module may import its own layer or a lower one,
never a higher one, which `tests/test_layout.py` enforces.

| layer | modules | role |
| --- | --- | --- |
| `model`, `progress` | — | shared types and progress reporting; depend on nothing |
| `scan/` | `discovery`, `astscan`, `symbols`, `native`, `trace`, `projectmeta` | read the world: filesystem, source, binaries, metadata, a live process |
| `analysis/` | `graph`, `analyzer`, `risk` | resolve imports, decide reachability, rate dynamic sites |
| `render/`, `edit/`, `config` | `report`, `tree`, `rewrite`, `apply` | present the answer, or act on it |
| `cli` | — | wires it together |

## Development

```bash
uv sync
uv run pytest              # integration tests build real venvs with uv
uv run pytest -m "not integration"
uv run ruff check . && uv run ruff format --check .
uv run ty check
```
