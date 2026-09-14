"""Progress reporting for the slow phases.

`rich` is optional on purpose: this tool exists to delete things out of virtualenvs, so it has
to keep working when it is not installed. Without it, progress degrades to a single line on
stderr; with `--quiet` or a non-TTY stderr, to nothing at all.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol

try:  # pragma: no cover - exercised by whichever branch the environment provides
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

    HAVE_RICH = True
except ImportError:  # pragma: no cover
    HAVE_RICH = False


if TYPE_CHECKING:
    from rich.progress import Progress as RichProgress
    from rich.progress import TaskID


class Reporter(Protocol):
    def task(self, description: str, total: int | None = None) -> Tracker: ...


class Tracker(Protocol):
    def advance(self, step: int = 1) -> None: ...
    def done(self) -> None: ...


class _NullTracker:
    def advance(self, step: int = 1) -> None:
        return

    def done(self) -> None:
        return


class NullReporter:
    def task(self, description: str, total: int | None = None) -> Tracker:
        return _NullTracker()


class _PlainTracker:
    """One rewritten stderr line, updated at most a few times a second."""

    def __init__(self, description: str, total: int | None) -> None:
        self.description = description
        self.total = total
        self.count = 0
        self._last = 0.0

    def advance(self, step: int = 1) -> None:
        self.count += step
        now = time.monotonic()
        if now - self._last < 0.1:
            return
        self._last = now
        self._draw()

    def _draw(self) -> None:
        suffix = f"{self.count}/{self.total}" if self.total else str(self.count)
        print(f"\r\033[K{self.description}: {suffix}", end="", file=sys.stderr, flush=True)

    def done(self) -> None:
        self._draw()
        print(file=sys.stderr)


class PlainReporter:
    def task(self, description: str, total: int | None = None) -> Tracker:
        return _PlainTracker(description, total)


class _RichTracker:
    def __init__(self, progress: RichProgress, task_id: TaskID) -> None:
        self._progress = progress
        self._task_id = task_id

    def advance(self, step: int = 1) -> None:
        self._progress.advance(self._task_id, step)

    def done(self) -> None:
        task = self._progress.tasks[self._task_id]
        self._progress.update(self._task_id, completed=task.total or task.completed)


class RichReporter:
    def __init__(self, progress: RichProgress) -> None:
        self._progress = progress

    def task(self, description: str, total: int | None = None) -> Tracker:
        task_id = self._progress.add_task(description, total=total)
        return _RichTracker(self._progress, task_id)


@contextmanager
def reporter(enabled: bool = True) -> Iterator[Reporter]:
    """Pick the best reporter the environment supports, and tear it down afterwards."""
    if not enabled or not sys.stderr.isatty():
        yield NullReporter()
        return
    if not HAVE_RICH:
        yield PlainReporter()
        return
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        transient=True,
        refresh_per_second=12,
    )
    with progress:
        yield RichReporter(progress)


def track[T](items: Iterable[T], description: str, reporter_: Reporter, total: int | None = None) -> Iterator[T]:
    tracker = reporter_.task(description, total)
    try:
        for item in items:
            yield item
            tracker.advance()
    finally:
        tracker.done()
