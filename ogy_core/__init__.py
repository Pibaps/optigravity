"""Core stdlib-only package for Optigravity."""

from __future__ import annotations

from importlib import metadata as _metadata


def get_version() -> str:
    """Return installed package version from importlib.metadata (fallback '0.0.0-dev')."""
    try:
        return _metadata.version("optigravity")
    except _metadata.PackageNotFoundError:
        return "0.0.0-dev"


__version__ = get_version()
