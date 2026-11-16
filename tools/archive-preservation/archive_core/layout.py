"""Canonical human-browsable paths for preservation representations."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath


OBJECT_ROLE_TOKENS = {
    "preservation_master": "master",
    "access_derivative": "access",
    "preservation_surrogate": "surrogate",
    "preservation_surrogate_source": "source",
    "preservation_document": "document",
    "preservation_document_surrogate": "document-surrogate",
}
OBJECT_ROLE_DIRECTORIES = {
    "preservation_master": "masters",
    "preservation_surrogate": "masters",
    # Historical catalogs may retain this superseded role. New surrogate
    # accession registers the exact selected JPEG as access_derivative.
    "preservation_surrogate_source": "derivatives",
    "access_derivative": "derivatives",
    "preservation_document": "documents",
    "preservation_document_surrogate": "documents",
}
OBJECT_LAYOUT_VERSION = "role-flat-readable-v1"


def filename_component(value: str, *, fallback: str) -> str:
    """Return one conservative, readable, cross-filesystem filename part."""
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip(".-")
    return normalized or fallback


def object_filename(
    *,
    inventory: str,
    entity_id: str,
    role: str,
    suffix: str,
) -> str:
    """Name one representation so related files sort next to each other."""
    role_token = OBJECT_ROLE_TOKENS[role]
    inventory_token = filename_component(inventory, fallback="unlabelled")
    entity_token = filename_component(entity_id, fallback="unknown-entity")
    extension = suffix.casefold()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
        raise ValueError(f"unsafe representation suffix: {suffix!r}")
    return f"{inventory_token}__{entity_token}__{role_token}{extension}"


def object_relative_path(
    *,
    unit_id: str,
    inventory: str,
    entity_id: str,
    role: str,
    suffix: str,
) -> PurePosixPath:
    """Return the canonical role-flat representation path inside one unit."""
    return (
        PurePosixPath("units")
        / unit_id
        / "data"
        / OBJECT_ROLE_DIRECTORIES[role]
        / object_filename(
            inventory=inventory,
            entity_id=entity_id,
            role=role,
            suffix=suffix,
        )
    )


def matches_object_path(
    path: PurePosixPath,
    *,
    unit_id: str,
    entity_id: str,
    role: str,
) -> bool:
    """Check the stable flat-path contract when the inventory is catalog data."""
    role_token = OBJECT_ROLE_TOKENS[role]
    entity_token = filename_component(entity_id, fallback="unknown-entity")
    expected_parent = (
        PurePosixPath("units")
        / unit_id
        / "data"
        / OBJECT_ROLE_DIRECTORIES[role]
    )
    marker = f"__{entity_token}__{role_token}"
    return (
        path.parent == expected_parent
        and path.stem.endswith(marker)
        and len(path.stem) > len(marker)
        and bool(path.suffix)
    )
