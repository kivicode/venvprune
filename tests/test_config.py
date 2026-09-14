from __future__ import annotations

from pathlib import Path

from venvprune import config as config_mod
from venvprune.analyzer import analyze
from venvprune.cli import _resolve, build_parser, main
from venvprune.graph import Options

from .conftest import write


def _venv(tmp_path: Path) -> Path:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    for pkg in ("used", "spare", "tooling"):
        write(site / pkg / "__init__.py", "")
        write(site / f"{pkg}-1.0.dist-info" / "METADATA", f"Name: {pkg}\nVersion: 1.0\n\nx")
        write(site / f"{pkg}-1.0.dist-info" / "RECORD", f"{pkg}/__init__.py,,\n")
    write(site / "spare" / "deep.py", "")
    write(tmp_path / "code" / "app.py", "import used\n")
    return tmp_path


def test_keep_pattern_by_distribution(tmp_path: Path):
    root = _venv(tmp_path)
    analysis = analyze([root / "code"], root / "venv", Options(keep=("tooling",)))
    unused = {i.name for i in analysis.unused()}
    assert "tooling" not in unused
    assert "spare" in unused
    assert analysis.kept_by_config == {"tooling"}


def test_keep_pattern_by_module_glob(tmp_path: Path):
    root = _venv(tmp_path)
    analysis = analyze([root / "code"], root / "venv", Options(keep=("spare.*",)))
    unused = {i.name for i in analysis.unused()}
    assert "spare.deep" not in unused
    assert "tooling" in unused


def test_unmatched_keep_is_reported(tmp_path: Path):
    root = _venv(tmp_path)
    analysis = analyze([root / "code"], root / "venv", Options(keep=("nosuchthing",)))
    assert analysis.unmatched_keep == {"nosuchthing"}


def test_keep_survives_dev_group_pruning(tmp_path: Path):
    root = _venv(tmp_path)
    write(
        root / "pyproject.toml",
        '[project]\nname = "p"\nversion = "0"\ndependencies = []\n\n[dependency-groups]\ndev = ["tooling"]\n',
    )
    pruned = analyze([root / "code"], root / "venv", Options(prune_dev_groups=("dev",), keep=("tooling",)))
    assert "tooling" not in {i.name for i in pruned.unused()}
    assert "tooling" not in pruned.dev_forced


def test_config_file_supplies_code_and_venv(tmp_path: Path):
    root = _venv(tmp_path)
    write(
        root / "pyproject.toml",
        '[project]\nname = "p"\nversion = "0"\n\n[tool.venvprune]\ncode = ["code"]\nvenv = "venv"\n'
        'keep = ["tooling"]\nformat = "paths"\n',
    )
    args = build_parser().parse_args(["--config", str(root / "pyproject.toml")])
    code, venv, options, _cfg, output = _resolve(args)
    assert [p.name for p in code] == ["code"]
    assert venv is not None and venv.name == "venv"
    assert options.keep == ("tooling",)
    assert output == "paths"


def test_cli_overrides_config(tmp_path: Path):
    root = _venv(tmp_path)
    write(root / "pyproject.toml", '[tool.venvprune]\nsymbols = true\nformat = "json"\nlazy = true\n')
    args = build_parser().parse_args(
        ["--config", str(root / "pyproject.toml"), "--no-symbols", "--format", "text", "--no-lazy"]
    )
    _, _, options, _, output = _resolve(args)
    assert options.symbol_precision is False
    assert options.follow_lazy is False
    assert output == "text"


def test_list_settings_accumulate(tmp_path: Path):
    root = _venv(tmp_path)
    write(root / "pyproject.toml", '[tool.venvprune]\nkeep = ["a", "b"]\n')
    args = build_parser().parse_args(["--config", str(root / "pyproject.toml"), "--keep", "c"])
    _, _, options, _, _ = _resolve(args)
    assert options.keep == ("a", "b", "c")


def test_unknown_keys_are_reported(tmp_path: Path):
    write(tmp_path / "pyproject.toml", '[tool.venvprune]\nnonsense = 1\nkeep = ["x"]\n')
    cfg = config_mod.load(tmp_path / "pyproject.toml")
    assert cfg.unknown == ["nonsense"]
    assert cfg.get("keep") == ["x"]


def test_no_config_ignores_the_file(tmp_path: Path):
    root = _venv(tmp_path)
    write(root / "pyproject.toml", '[tool.venvprune]\nkeep = ["tooling"]\n')
    args = build_parser().parse_args(["--config", str(root / "pyproject.toml"), "--no-config"])
    _, _, options, cfg, _ = _resolve(args)
    assert options.keep == ()
    assert cfg.path is None


def test_end_to_end_run_from_config(tmp_path: Path, capsys):
    root = _venv(tmp_path)
    write(
        root / "pyproject.toml",
        '[tool.venvprune]\ncode = ["code"]\nvenv = "venv"\nkeep = ["tooling"]\nformat = "paths"\nprogress = "never"\n',
    )
    assert main(["--config", str(root / "pyproject.toml")]) == 0
    out = capsys.readouterr().out
    assert "spare" in out
    assert "tooling" not in out


def test_missing_roots_is_an_error(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--no-config"]) == 2
    assert "no code roots" in capsys.readouterr().err


def test_missing_config_file_is_an_explicit_error(tmp_path: Path, capsys):
    assert main(["--config", str(tmp_path / "nope.toml")]) == 2
    err = capsys.readouterr().err
    assert "config file not found" in err
    assert "no code roots" not in err, "the real cause must not be masked"


def test_config_without_a_section_is_an_error(tmp_path: Path, capsys):
    write(tmp_path / "pyproject.toml", '[project]\nname = "p"\nversion = "0"\n')
    assert main(["--config", str(tmp_path / "pyproject.toml")]) == 2
    assert "has no [tool.venvprune] section" in capsys.readouterr().err


def test_invalid_toml_is_an_error(tmp_path: Path, capsys):
    write(tmp_path / "bad.toml", "[tool.venvprune\nkeep = ")
    assert main(["--config", str(tmp_path / "bad.toml")]) == 2
    assert "not valid TOML" in capsys.readouterr().err


def test_stale_bytecode_goes_with_the_module(tmp_path: Path):
    from venvprune.apply import build_plan

    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "pkg" / "__init__.py", "")
    write(site / "pkg" / "dead.py", "")
    write(site / "pkg" / "__pycache__" / "dead.cpython-312.pyc", "x")
    write(site / "pkg" / "__pycache__" / "__init__.cpython-312.pyc", "x")
    write(tmp_path / "code" / "app.py", "import pkg\n")

    planned = {p.name for p in build_plan(analyze([tmp_path / "code"], tmp_path / "venv")).files}
    assert "dead.py" in planned
    assert "dead.cpython-312.pyc" in planned, "its bytecode goes too"
    assert "__init__.cpython-312.pyc" not in planned, "the live module keeps its cache"


def test_strip_pycache_removes_every_cache(tmp_path: Path):
    from venvprune.apply import build_plan

    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "pkg" / "__init__.py", "")
    write(site / "pkg" / "__pycache__" / "__init__.cpython-312.pyc", "x")
    write(tmp_path / "code" / "app.py", "import pkg\n")

    analysis = analyze([tmp_path / "code"], tmp_path / "venv")
    assert not build_plan(analysis).dirs
    assert any(d.name == "__pycache__" for d in build_plan(analysis, strip_pycache=True).dirs)
