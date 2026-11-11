"""Resolve plugin data against the active Hermes profile, not the launch home."""
from pathlib import Path


def data_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "llmux"
