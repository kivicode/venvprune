from __future__ import annotations

import sys

import pytest

from venvprune import progress


def test_null_reporter_when_disabled():
    with progress.reporter(enabled=False) as reporter:
        assert isinstance(reporter, progress.NullReporter)
        tracker = reporter.task("x", total=3)
        tracker.advance()
        tracker.done()


def test_null_reporter_when_stderr_is_not_a_tty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    with progress.reporter(enabled=True) as reporter:
        assert isinstance(reporter, progress.NullReporter)


def test_plain_reporter_writes_to_stderr(capsys: pytest.CaptureFixture[str]):
    tracker = progress.PlainReporter().task("Parsing", total=2)
    tracker.advance()
    tracker.advance()
    tracker.done()
    assert "Parsing" in capsys.readouterr().err


def test_falls_back_to_plain_without_rich(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(progress, "HAVE_RICH", False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    with progress.reporter(enabled=True) as reporter:
        assert isinstance(reporter, progress.PlainReporter)


@pytest.mark.skipif(not progress.HAVE_RICH, reason="rich is not installed")
def test_rich_reporter_tracks_counts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    with progress.reporter(enabled=True) as reporter:
        assert isinstance(reporter, progress.RichReporter)
        tracker = reporter.task("Parsing", total=4)
        tracker.advance(3)
        tracker.done()


def test_analysis_accepts_a_reporter(tmp_path):
    from venvprune.analyzer import analyze

    from .conftest import write

    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    write(site / "pkg" / "__init__.py", "")
    write(tmp_path / "code" / "app.py", "import pkg\n")

    seen: list[str] = []

    class Recording:
        def task(self, description: str, total: int | None = None):
            seen.append(description)
            return progress.NullReporter().task(description, total)

    analyze([tmp_path / "code"], tmp_path / "venv", reporter=Recording())
    assert "Parsing" in seen and "Indexing modules" in seen
