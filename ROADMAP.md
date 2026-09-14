# Roadmap

## Phase 1 — find unreachable modules ✅

Cross-library import graph over code roots + `site-packages`, eager/lazy/type-only/re-export
edge classification, dynamic-import risk detection, optional runtime trace merge. Reports
unused modules, fully unused distributions, and reclaimable bytes.

## Phase 2 — modules imported only incidentally

Distinguish "your code needs this" from "this got dragged in anyway". The edge kinds are
already recorded; what is missing is the rewrite side.

- [ ] `type_only` promotion: an annotation-only import that is *not* under `TYPE_CHECKING`
      still executes. Detect when it is only ever used in an annotation position and the module
      already has (or can be given) `from __future__ import annotations`, making it droppable.
- [ ] Re-export chains: `pkg/__init__.py` imports `pkg.heavy` purely to republish one name that
      the code never touches. Needs per-name usage tracking, not just per-module — resolve
      `from pkg import X` to the defining module and drop the rest of the `__init__` body.
- [ ] Emit a patch set: minimal edits to a library's `__init__.py` (lazy `__getattr__`
      per PEP 562, or deleting unused re-export lines) that let the unused modules go.
- [ ] Verify each proposed rewrite by re-running the analysis plus the project's own tests.

## Phase 3 — dynamic-import safety gating

Sites are already detected (`importlib`, `__import__`, `pkgutil`, `import *`, entry points,
`getattr` over a module). What is missing is severity and resolution.

- [ ] Constant folding: `importlib.import_module("pkg.backend_" + name)` where `name` comes from
      a bounded literal set — enumerate the candidates instead of keeping the whole subtree.
- [ ] Distinguish self-referential dynamic imports (a package importing its own submodules) from
      open-ended ones (importing a user-supplied name); only the latter is unbounded.
- [ ] Entry-point resolution: read `entry_points.txt` from every dist-info and treat advertised
      targets as roots when the code loads that group.
- [ ] Per-package risk score, and a `--unsafe` gate that refuses to propose rewrites inside a
      package whose dynamic behaviour cannot be bounded.

## Phase 4 — shipped binary artifacts

- [ ] Decide necessity of shipped `.so` / `.pyd` / `.dylib` extension modules. They are indexed
      today and participate in reachability by name, but nothing looks *inside* them: a native
      module can import Python modules from C (`PyImport_ImportModule`), and one wheel often
      ships several alternative builds (SIMD variants, CUDA vs CPU, per-arch fat binaries) where
      only one is ever loaded.
- [ ] Extract imported names from the binary (symbol/string scan, `PyImport_*` call sites) and
      feed them back into the graph as edges.
- [ ] Detect multi-variant artifact sets and mark the ones the target platform can never dlopen.
- [ ] Follow shared-library dependencies (`otool -L` / `ldd`) so bundled `.dylibs`/`*.libs`
      directories are pruned alongside the extension that needs them.

## Cross-cutting

- [ ] Performance: ~45 s on a 10.5k-module venv, single-threaded. Parallelise the AST pass and
      cache parse results keyed by (path, mtime, size).
- [ ] `--explain <module>` to print the shortest import chain that keeps a module alive
      (the data is already in `Reachability.why`).
- [ ] Apply mode: actually delete pruned files, with a manifest for rollback.
