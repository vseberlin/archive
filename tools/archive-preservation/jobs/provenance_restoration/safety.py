"""Shared filesystem boundaries for the temporary restoration job."""

from __future__ import annotations

from pathlib import Path


PROTECTED_EVIDENCE_ROOTS = (
    Path("/mnt/volume/archive"),
    Path("/mnt/volume/archive-intake"),
    Path("/media/vladimir"),
)


class ProtectedEvidenceError(ValueError):
    """Raised when a requested write enters read-only evidence."""


def require_safe_output(path: Path) -> Path:
    """Resolve and reject destinations inside protected evidence roots."""

    resolved = path.expanduser().resolve()
    for root in PROTECTED_EVIDENCE_ROOTS:
        resolved_root = root.resolve()
        if resolved == resolved_root or resolved_root in resolved.parents:
            raise ProtectedEvidenceError(
                f"output is inside protected read-only evidence root: {root}"
            )
    return resolved
