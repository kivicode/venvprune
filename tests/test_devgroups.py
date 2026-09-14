from __future__ import annotations

from pathlib import Path

from venvprune.analysis.analyzer import analyze
from venvprune.analysis.graph import Options
from venvprune.scan.projectmeta import canonical, read_project

from .conftest import write


def test_reads_pep735_groups_and_extras(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    write(
        pyproject,
        "[project]\n"
        'name = "p"\n'
        'version = "0"\n'
        'dependencies = ["Requests >= 2", "click; python_version > \'3.9\'"]\n\n'
        "[project.optional-dependencies]\n"
        'docs = ["sphinx"]\n\n'
        "[dependency-groups]\n"
        'test = ["pytest", "coverage"]\n'
        'dev = ["ruff", {include-group = "test"}]\n',
    )
    meta = read_project(pyproject)
    assert meta.runtime == {"requests", "click"}
    assert meta.dev_only(("dev",)) == {"ruff", "pytest", "coverage"}
    assert meta.dev_only(("docs",)) == {"sphinx"}


def test_runtime_declaration_wins_over_dev(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    write(
        pyproject,
        '[project]\nname = "p"\nversion = "0"\ndependencies = ["rich"]\n\n'
        '[dependency-groups]\ndev = ["rich", "ruff"]\n',
    )
    assert read_project(pyproject).dev_only(("dev",)) == {"ruff"}


def test_poetry_groups(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    write(
        pyproject,
        '[tool.poetry.dependencies]\npython = "^3.12"\nhttpx = "*"\n\n'
        '[tool.poetry.group.dev.dependencies]\nblack = "*"\n',
    )
    meta = read_project(pyproject)
    assert meta.runtime == {"httpx"}
    assert meta.dev_only(("dev",)) == {"black"}


def test_canonical_names():
    assert canonical("Zope.Interface") == "zope-interface"
    assert canonical("typing_extensions") == "typing-extensions"


def _project(tmp_path: Path) -> Path:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    for pkg, dist in (("toolkit", "toolkit"), ("checker", "checker"), ("shared", "shared")):
        write(site / pkg / "__init__.py", "")
        write(site / f"{dist}-1.0.dist-info" / "METADATA", f"Name: {dist}\nVersion: 1.0\n\nx")
        write(site / f"{dist}-1.0.dist-info" / "RECORD", f"{pkg}/__init__.py,,\n")
    # A dev tool that the runtime code also happens to import must survive.
    write(site / "checker" / "__init__.py", "import shared\n")
    write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "p"\nversion = "0"\ndependencies = []\n\n'
        '[dependency-groups]\ndev = ["toolkit", "checker"]\n',
    )
    return tmp_path


def test_dev_group_pruned_wholesale(tmp_path: Path):
    root = _project(tmp_path)
    write(root / "code" / "app.py", "import toolkit\n")
    analysis = analyze([root / "code"], root / "venv", Options(prune_dev_groups=("dev",)))
    assert analysis.dev_only == {"checker"}, "toolkit is imported by the code, so it is protected"
    assert "toolkit" not in {i.name for i in analysis.unused()}
    assert "checker" in {i.name for i in analysis.unused()}


def test_dev_group_prune_overrides_transitive_reachability(tmp_path: Path):
    root = _project(tmp_path)
    write(root / "code" / "app.py", "import checker\n")
    plain = analyze([root / "code"], root / "venv")
    assert "shared" in plain.reach.reached

    pruned = analyze([root / "code"], root / "venv", Options(prune_dev_groups=("dev",)))
    assert pruned.dev_only == {"toolkit"}
    assert "checker" not in {i.name for i in pruned.unused()}, "imported directly, so protected"
    assert "toolkit" in {i.name for i in pruned.unused()}


def test_dev_forced_drops_reachable_modules(tmp_path: Path):
    root = _project(tmp_path)
    write(root / "code" / "app.py", "import shared\nimport checker\n")
    write(
        root / "pyproject.toml",
        '[project]\nname = "p"\nversion = "0"\ndependencies = []\n\n[dependency-groups]\ndev = ["shared"]\n',
    )
    analysis = analyze([root / "code"], root / "venv", Options(prune_dev_groups=("dev",)))
    assert analysis.dev_only == set(), "shared is imported by the code"

    write(root / "code" / "app.py", "import checker\n")
    analysis = analyze([root / "code"], root / "venv", Options(prune_dev_groups=("dev",)))
    assert analysis.dev_only == {"shared"}
    assert "shared" in analysis.dev_forced, "reachable via checker, but dev-only wins"
    assert "shared" in {i.name for i in analysis.unused()}


def test_declared_dependency_audit(tmp_path: Path):
    """Classify each declared dependency by what actually imports it."""
    from venvprune.analysis import deps

    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    for pkg, dist in (("direct", "direct"), ("middle", "middle"), ("leaf", "leaf"), ("idle", "idle")):
        write(site / pkg / "__init__.py", "")
        write(site / f"{dist}-1.0.dist-info" / "METADATA", f"Name: {dist}\nVersion: 1.0\n\nx")
        write(site / f"{dist}-1.0.dist-info" / "RECORD", f"{pkg}/__init__.py,,\n")
    write(site / "middle" / "__init__.py", "import leaf\n")
    write(tmp_path / "code" / "app.py", "import direct\nimport middle\n")
    write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "p"\nversion = "0"\n'
        'dependencies = ["direct", "middle", "leaf", "idle", "winonly ; sys_platform == \'win32\'", "absent"]\n',
    )

    analysis = analyze([tmp_path / "code"], tmp_path / "venv")
    by_name = {d.name: d for d in deps.assess(analysis, tmp_path / "pyproject.toml")}

    assert by_name["direct"].status is deps.Status.USED
    assert by_name["direct"].importers == ("app",)
    assert by_name["leaf"].status is deps.Status.INDIRECT, "only middle imports it"
    assert by_name["leaf"].importers == ("middle",)
    assert by_name["idle"].status is deps.Status.UNUSED
    assert by_name["absent"].status is deps.Status.MISSING
    assert by_name["winonly"].status is deps.Status.CONDITIONAL, "its marker excludes this platform"
    assert "win32" in by_name["winonly"].importers[0]


def test_dependency_audit_maps_a_renamed_module(tmp_path: Path):
    """`html-for-docx` installs `html4docx`, so the module name cannot be guessed."""
    from venvprune.analysis import deps

    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "odd4name" / "__init__.py", "")
    info = site / "odd_name-1.0.dist-info"
    write(info / "METADATA", "Name: odd-name\nVersion: 1.0\n\nx")
    write(info / "RECORD", "odd4name/__init__.py,,\n")
    write(tmp_path / "code" / "app.py", "x = 1\n")
    write(tmp_path / "pyproject.toml", '[project]\nname = "p"\nversion = "0"\ndependencies = ["odd-name"]\n')

    row = deps.assess(analyze([tmp_path / "code"], tmp_path / "venv"), tmp_path / "pyproject.toml")[0]
    assert row.name == "odd-name"
    assert row.modules == ("odd4name",), "resolved through RECORD, not by guessing"
    assert row.status is deps.Status.UNUSED
