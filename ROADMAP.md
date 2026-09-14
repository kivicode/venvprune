# Roadmap

## Phase 1 — find unreachable modules ✅

Cross-library import graph over code roots + `site-packages`, eager/lazy/type-only/re-export
edge classification, optional runtime trace merge. Reports unused modules, fully unused
distributions, and reclaimable bytes.

## Phase 2 — modules imported only incidentally ✅

`--symbols` tracks what each import binds and which attributes are actually read off it, then
propagates a demand set through the graph, so `import pkg` plus one `pkg.thing` no longer keeps
the whole package. `--format rewrites` emits (and `--apply-rewrites` writes) the `__init__.py`
patch that makes the freed modules deletable, guarded by a PEP 562 `__getattr__`.

Remaining:

- [ ] Annotation-only imports that are *not* under `TYPE_CHECKING` still execute. Detect when a
      name is used only in annotation position and the module can take
      `from __future__ import annotations`, then move the import into a `TYPE_CHECKING` block.
- [ ] Narrow a partially-used `from .x import A, B` statement instead of keeping it whole.
- [ ] Re-run the project's own tests after applying a rewrite, as an automatic verification gate.

## Phase 3 — dynamic-import safety gating ✅

Argument shapes are folded (literal, prefix, bounded literal set, package-relative), entry
points are read from `dist-info` and become roots, and `--format risk` scores every site
`resolved` / `confined` / `open`. `--strict-dynamic` refuses to keep anything for an unbounded
site.

Remaining:

- [ ] Follow a name through a local variable across statements (simple constant propagation),
      not just direct assignment and `for` targets.
- [ ] Resolve `getattr(module, name)` where `name` comes from a literal registry dict.
- [ ] Treat a package's own `__getattr__` (PEP 562 lazy loader) as a re-export table rather
      than an opaque dynamic site — several large libraries now ship one.

## Phase 4 — shipped binary artifacts ✅

`--scan-binaries` recovers module names from an extension's string table and feeds them back as
lazy edges. `--format native` reports extension modules, multi-build wheels whose other builds
this interpreter can never load, and bundled `.dylibs` / `.libs` attributed to the extensions
that link them.

Remaining:

- [ ] Parse the import table properly (Mach-O / ELF symbol and relocation entries for
      `PyImport_*`) instead of matching every plausible string against the index.
- [ ] Detect SIMD/CUDA variant sets that share one ABI tag but differ by runtime dispatch.
- [ ] Transitively prune bundled libraries that only other pruned libraries link against.

## Cross-cutting

- [ ] Performance: ~20-45 s on a 10.5k-module venv, single-threaded. Parallelise the AST pass
      and cache parse results keyed by (path, mtime, size).
- [ ] `--explain <module>` to print the shortest import chain that keeps a module alive
      (`Reachability.why` already holds the data).
- [ ] Apply mode: delete pruned files, with a manifest for rollback.
- [ ] Windows: `linked_libraries` has no implementation there (returns empty).
