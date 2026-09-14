from __future__ import annotations

from pathlib import Path

import pytest

from venvprune.analysis.analyzer import analyze
from venvprune.analysis.graph import Options
from venvprune.scan import native

from .conftest import write


def write_blob(path: Path, strings: list[str], pad: int = 256) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = b"\x00" * 16 + b"\x00".join(s.encode() for s in strings) + b"\x00" * pad
    path.write_bytes(blob)


@pytest.fixture
def venv(tmp_path: Path) -> Path:
    return tmp_path / "venv" / "lib" / "python3.12" / "site-packages"


def test_extension_modules_are_indexed(venv: Path, tmp_path: Path):
    write(venv / "fast" / "__init__.py", "")
    write_blob(venv / "fast" / "_core.cpython-312-darwin.so", ["hello"])
    write(tmp_path / "code" / "app.py", "import fast\n")
    analysis = analyze([tmp_path / "code"], tmp_path / "venv")
    assert "fast._core" in analysis.modules
    assert "fast._core" in {i.name for i in analysis.unused()}, "nothing imports it"


def test_binary_string_scan_recovers_imports(venv: Path, tmp_path: Path):
    write(venv / "fast" / "__init__.py", "import fast._core\n")
    write_blob(venv / "fast" / "_core.cpython-312-darwin.so", ["helper", "not_a_module", "PyImport_ImportModule"])
    write(venv / "helper" / "__init__.py", "")
    write(tmp_path / "code" / "app.py", "import fast\n")

    blind = analyze([tmp_path / "code"], tmp_path / "venv")
    assert "helper" in {i.name for i in blind.unused()}

    seeing = analyze([tmp_path / "code"], tmp_path / "venv", Options(scan_binaries=True))
    assert "helper" not in {i.name for i in seeing.unused()}


def test_binary_scan_ignores_names_that_are_not_modules(venv: Path, tmp_path: Path):
    write(venv / "fast" / "__init__.py", "import fast._core\n")
    write_blob(venv / "fast" / "_core.cpython-312-darwin.so", ["json", "sys", "zzz_unknown"])
    write(tmp_path / "code" / "app.py", "import fast\n")
    analysis = analyze([tmp_path / "code"], tmp_path / "venv", Options(scan_binaries=True))
    targets = {e.target for e in analysis.modules["fast._core"].edges}
    assert "zzz_unknown" not in targets


def test_finds_unloadable_build_variants(venv: Path):
    write_blob(venv / "pkg" / "_speed.cpython-312-darwin.so", ["a"], pad=100)
    write_blob(venv / "pkg" / "_speed.cpython-311-darwin.so", ["a"], pad=900)
    write_blob(venv / "pkg" / "_solo.cpython-312-darwin.so", ["a"])
    variants = native.find_variants([venv], abi_tags={"cpython-312-darwin"})
    assert len(variants) == 1
    variant = variants[0]
    assert variant.module == "pkg._speed"
    assert variant.chosen.name == "_speed.cpython-312-darwin.so"
    assert [p.name for p in variant.rejected] == ["_speed.cpython-311-darwin.so"]
    assert variant.wasted_bytes > 0


def test_single_build_is_not_a_variant(venv: Path):
    write_blob(venv / "pkg" / "_only.cpython-312-darwin.so", ["a"])
    assert native.find_variants([venv], abi_tags={"cpython-312-darwin"}) == []


def test_bundled_libraries_are_collected(venv: Path):
    write_blob(venv / "pkg" / ".dylibs" / "libfoo.1.dylib", ["x"])
    write_blob(venv / "pkg.libs" / "libbar.so.6", ["x"])
    write_blob(venv / "pkg" / "_core.cpython-312-darwin.so", ["x"])
    libs = native.bundled_libraries([venv])
    assert set(libs) == {"libfoo.1.dylib", "libbar.so.6"}, "the extension itself is not a bundled lib"


def test_bundled_libs_in_lib_dirs_are_not_treated_as_modules(venv: Path, tmp_path: Path):
    write(venv / "pkg" / "__init__.py", "")
    write_blob(venv / "pkg" / ".dylibs" / "libfoo.1.dylib", ["x"])
    write(tmp_path / "code" / "app.py", "import pkg\n")
    analysis = analyze([tmp_path / "code"], tmp_path / "venv")
    assert not any("dylibs" in name for name in analysis.modules)


def test_unreferenced_libs_reported(venv: Path):
    write_blob(venv / "pkg" / ".dylibs" / "libfoo.1.dylib", ["x"])
    libs = native.attribute_libraries([], native.bundled_libraries([venv]))
    assert libs["libfoo.1.dylib"].referenced_by == set()


def test_render_report_runs(venv: Path, tmp_path: Path):
    write(venv / "pkg" / "__init__.py", "")
    write_blob(venv / "pkg" / "_core.cpython-312-darwin.so", ["x"])
    write_blob(venv / "pkg" / ".dylibs" / "libfoo.1.dylib", ["x"])
    write(tmp_path / "code" / "app.py", "import pkg\n")
    analysis = analyze([tmp_path / "code"], tmp_path / "venv")
    text = native.render_report(
        native.find_variants([venv]),
        native.bundled_libraries([venv]),
        analysis.extension_modules(),
        {i.name for i in analysis.unused()},
    )
    assert "Compiled artifacts" in text
    assert "pkg._core" in analysis.extension_modules()


def test_plain_dylib_is_not_an_importable_module(venv: Path, tmp_path: Path):
    write(venv / "pkg" / "__init__.py", "")
    write_blob(venv / "pkg" / "libextra.dylib", ["x"])
    write(tmp_path / "code" / "app.py", "import pkg\n")
    analysis = analyze([tmp_path / "code"], tmp_path / "venv")
    assert "pkg.libextra" not in analysis.modules


def test_all_files_backing_one_module_are_pruned(venv: Path, tmp_path: Path):
    """A `.so` beside a `.py` is the same module: pruning it must take both files."""
    from venvprune.edit.apply import build_plan

    write(venv / "pkg" / "__init__.py", "")
    write(venv / "pkg" / "dead.py", "")
    write_blob(venv / "pkg" / "dead.cpython-312-darwin.so", ["x"])
    write(venv / "pkg" / "dead.pyi", "")
    write(tmp_path / "code" / "app.py", "import pkg\n")

    analysis = analyze([tmp_path / "code"], tmp_path / "venv")
    info = analysis.modules["pkg.dead"]
    assert len(info.shadowed) == 2, "the other two files are recorded, not dropped"

    planned = {p.name for p in build_plan(analysis).files}
    assert planned == {"dead.py", "dead.cpython-312-darwin.so", "dead.pyi"}


def test_pruning_is_a_fixpoint_with_multi_file_modules(venv: Path, tmp_path: Path):
    from venvprune.edit.apply import build_plan, execute

    write(venv / "pkg" / "__init__.py", "")
    write(venv / "pkg" / "dead.py", "")
    write_blob(venv / "pkg" / "dead.cpython-312-darwin.so", ["x"])
    write(tmp_path / "code" / "app.py", "import pkg\n")

    first = analyze([tmp_path / "code"], tmp_path / "venv")
    execute(build_plan(first), first.site_dirs, manifest_dir=tmp_path)

    second = analyze([tmp_path / "code"], tmp_path / "venv")
    assert not build_plan(second).files, "a second pass must find nothing left"


def test_binary_scan_resolves_bare_sibling_names(venv: Path, tmp_path: Path):
    """Cython stores a sibling's bare name, so `_helper` must resolve to `pkg._helper`."""
    write(venv / "pkg" / "__init__.py", "import pkg.core\n")
    write_blob(venv / "pkg" / "core.cpython-312-darwin.so", ["_helper", "elsewhere"])
    write_blob(venv / "pkg" / "_helper.cpython-312-darwin.so", ["x"])
    write(venv / "elsewhere" / "__init__.py", "")
    write(tmp_path / "code" / "app.py", "import pkg\n")

    blind = analyze([tmp_path / "code"], tmp_path / "venv")
    assert "pkg._helper" in {i.name for i in blind.unused()}

    seeing = analyze([tmp_path / "code"], tmp_path / "venv", Options(scan_binaries=True))
    unused = {i.name for i in seeing.unused()}
    assert "pkg._helper" not in unused, "the bare sibling name resolves against its package"
    assert "elsewhere" not in unused, "a fully-qualified match still works"


def test_binary_scan_finds_a_sibling_inside_a_mangled_symbol(venv: Path, tmp_path: Path):
    """Cython leaves only `__pyx_v_4pkg_4core__helper`, never a clean `pkg._helper`."""
    write(venv / "pkg" / "__init__.py", "import pkg.core\n")
    write_blob(venv / "pkg" / "core.cpython-312-darwin.so", ["___pyx_v_3pkg_4core__helper"])
    write_blob(venv / "pkg" / "_helper.cpython-312-darwin.so", ["x"])
    write(venv / "pkg" / "spare.py", "")
    write(tmp_path / "code" / "app.py", "import pkg\n")

    seeing = analyze([tmp_path / "code"], tmp_path / "venv", Options(scan_binaries=True))
    unused = {i.name for i in seeing.unused()}
    assert "pkg._helper" not in unused, "the sibling hides inside the mangled symbol"
    assert "pkg.spare" in unused, "a sibling that is never mentioned still goes"


def test_library_dependencies_are_followed_transitively(venv: Path, monkeypatch):
    """A bundled lib needed only by another bundled lib must survive with it."""
    write_blob(venv / "pkg" / ".dylibs" / "libxcb.1.dylib", ["x"])
    write_blob(venv / "pkg" / ".dylibs" / "libXau.6.dylib", ["x"])
    write_blob(venv / "pkg" / ".dylibs" / "liborphan.1.dylib", ["x"])
    ext = venv / "pkg" / "_imaging.cpython-312-darwin.so"
    write_blob(ext, ["x"])

    links = {
        "_imaging.cpython-312-darwin.so": ["@loader_path/.dylibs/libxcb.1.dylib"],
        "libxcb.1.dylib": ["@loader_path/libXau.6.dylib"],
    }
    monkeypatch.setattr(native, "link_reader", lambda: ("stub", ()))
    monkeypatch.setattr(native, "linked_libraries_many", lambda ps: {p: links.get(p.name, []) for p in ps})

    libs = native.attribute_libraries([ext], native.bundled_libraries([venv]))
    assert libs["libxcb.1.dylib"].referenced_by, "linked straight from the extension"
    assert libs["libXau.6.dylib"].referenced_by == {"libxcb.1.dylib"}, "reached through libxcb"
    assert not libs["liborphan.1.dylib"].referenced_by, "nothing links it"


def test_nothing_is_pruned_when_no_reader_is_installed(venv: Path, monkeypatch):
    """Without otool/objdump/dumpbin, unreferenced cannot be distinguished from unknown."""
    write_blob(venv / "pkg" / ".dylibs" / "libfoo.1.dylib", ["x"])
    ext = venv / "pkg" / "_core.cpython-312-darwin.so"
    write_blob(ext, ["x"])
    monkeypatch.setattr(native, "link_reader", lambda: None)

    libs = native.attribute_libraries([ext], native.bundled_libraries([venv]))
    assert libs["libfoo.1.dylib"].referenced_by, "an unknown answer must keep the library"


@pytest.mark.parametrize(
    ("tool", "output", "expected"),
    [
        (
            "otool",
            "/p/ext.so:\n\t/p/ext.so (compat 1.0)\n\t/usr/lib/libSystem.B.dylib (compat 1.0)\n",
            ["/usr/lib/libSystem.B.dylib"],
        ),
        ("objdump", "/p/ext.so:     file format elf64\n  NEEDED               libc.so.6\n", ["libc.so.6"]),
        ("readelf", "File: /p/ext.so\n 0x01 (NEEDED) Shared library: [libm.so.6]\n", ["libm.so.6"]),
        ("dumpbin", "Dump of file /p/ext.so\n\n    python312.dll\n", ["python312.dll"]),
    ],
)
def test_each_reader_output_format_is_understood(tool: str, output: str, expected: list[str]):
    target = Path("/p/ext.so")
    out: dict[Path, list[str] | None] = {target: None}
    native._parse(tool, output, {str(target): target}, out)
    assert out[target] == expected


def test_a_fat_binary_header_does_not_misattribute():
    """`otool -L` repeats the header per architecture; the slices belong to one file."""
    first, second = Path("/p/a.so"), Path("/p/b.so")
    output = (
        "/p/a.so (architecture x86_64):\n"
        "\t/usr/lib/libA.dylib (compat 1.0)\n"
        "/p/a.so (architecture arm64):\n"
        "\t/usr/lib/libA.dylib (compat 1.0)\n"
        "/p/b.so:\n"
        "\t/usr/lib/libB.dylib (compat 1.0)\n"
    )
    out: dict[Path, list[str] | None] = {first: None, second: None}
    native._parse("otool", output, {str(first): first, str(second): second}, out)
    assert out[first] == ["/usr/lib/libA.dylib", "/usr/lib/libA.dylib"]
    assert out[second] == ["/usr/lib/libB.dylib"], "b's deps must not land on a"


def test_a_windows_path_is_not_split_on_its_drive_colon():
    target = Path("C:/site-packages/pkg/_core.pyd")
    out: dict[Path, list[str] | None] = {target: None}
    native._parse("dumpbin", f"Dump of file {target}\n\n    python312.dll\n", {str(target): target}, out)
    assert out[target] == ["python312.dll"]
