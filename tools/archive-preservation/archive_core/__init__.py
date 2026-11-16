"""Core invariants for the ISS archive preservation application."""

from __future__ import annotations


class ArchiveError(RuntimeError):
    """Raised when an archive invariant fails."""
