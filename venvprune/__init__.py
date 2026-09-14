"""Static (and optionally runtime-assisted) detection of unused virtualenv modules."""

from venvprune.analysis.analyzer import Analysis, analyze
from venvprune.analysis.graph import ModuleGraph, Options

__all__ = ["Analysis", "ModuleGraph", "Options", "analyze"]
__version__ = "0.1.0"
