#!/usr/bin/env python3
"""Prepare a conservative exact-path Bäcker drain review without deleting sources."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from scipy.fft import dctn


SOURCE = Path("/media/vladimir/Elements/restore/archive/WF/Baecker")
PREPARED = Path("/mnt/volume/iss-archive-work/collection-827/work/accession")
UNIT = Path("/mnt/volume/iss-archive/units/baeckerhandwerk")
DECISIONS = PREPARED / "accession-decisions.csv"
DRAIN = PREPARED / "drain-list.csv"
DOCUMENT_DISPOSITION = PREPARED / "document-source-disposition.csv"
SUMMARY = PREPARED / "drain-review-summary.json"
BLOCK = 8 * 1024 * 1024
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}
TIFF_EXTENSIONS = {".tif", ".tiff"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".url", ".part"}
DRAIN_FIELDS = [
    "source_path", "source_kind", "unit", "inventory", "source_role", "final_path",
    "preservation_status", "preservation_basis", "source_sha256", "final_sha256",
    "source_size_bytes", "exact_copy_proven", "drain_state", "deletion_authorized", "notes",
]
DOCUMENT_FIELDS = [
    "source_path", "filename", "format", "size_bytes", "sha256", "disposition",
    "basis", "selected_document_id", "deletion_authorized",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def perceptual_features(path: Path) -> list[tuple[int, int, float]]:
    with Image.open(path) as source:
        source.draft("L", (256, 256))
        image = ImageOps.exif_transpose(source).convert("L")
        variants = [
            image,
            image.transpose(Image.Transpose.ROTATE_90),
            image.transpose(Image.Transpose.ROTATE_180),
            image.transpose(Image.Transpose.ROTATE_270),
        ]
        result = []
        for variant in variants:
            small = np.asarray(variant.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32)
            low = dctn(small, type=2, norm="ortho")[:8, :8]
            median = float(np.median(low[1:, :]))
            phash = 0
            for value in (low > median).ravel():
                phash = (phash << 1) | int(value)
            dh = np.asarray(variant.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.float32)
            dhash = 0
            for value in (dh[:, 1:] > dh[:, :-1]).ravel():
                dhash = (dhash << 1) | int(value)
            ratio = variant.width / variant.height if variant.height else 0.0
            result.append((phash, dhash, ratio))
        return result


def distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def canonical_document(paths: list[Path], selected: set[Path]) -> Path:
    selected_members = [path for path in paths if path in selected]
    if selected_members:
        return sorted(selected_members)[0]
    def rank(path: Path) -> tuple[int, int, str]:
        lowered = path.as_posix().casefold()
        penalty = sum(token in lowered for token in ("wetransfer", "/archiv/", " - kopie", "scans-baecker"))
        return penalty, len(path.parts), path.as_posix()
    return min(paths, key=rank)


def source_guard() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise RuntimeError("Baecker source did not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise RuntimeError(f"Elements read-only guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def main() -> int:
    mount = source_guard()
    decisions = read_csv(DECISIONS)
    selected_sources = {Path(row["selected_master_source_path"]): row for row in decisions}
    selected_documents = {
        SOURCE / "P220240_Chronik_Bäckerinnung_165x240_241014.pdf": "BAECKER-DOC-001",
        SOURCE / "101xBaeckerhandwerk__mit-Abb._final_03.03.2024.docx": "BAECKER-DOC-002",
    }
    selected_document_paths = set(selected_documents)
    catalog = {row["inventory"]: row for row in read_csv(UNIT / "CATALOG.csv")}
    access_by_inventory = {
        row["inventory"]: UNIT / row["access_final_relative_path"] for row in decisions
    }
    references = {
        inventory: perceptual_features(path)[0]
        for inventory, path in access_by_inventory.items()
    }
    drain_rows: dict[str, dict[str, object]] = {}

    def add_drain(
        path: Path,
        *,
        inventory: str = "",
        role: str,
        final_path: str = "",
        status: str,
        basis: str,
        exact: bool,
        notes: str,
        digest: str | None = None,
    ) -> None:
        key = str(path)
        if path in selected_sources or path in selected_document_paths or key in drain_rows:
            return
        final_sha = catalog.get(inventory, {}).get("master_sha256", "") if inventory else ""
        drain_rows[key] = {
            "source_path": key,
            "source_kind": "file",
            "unit": "baeckerhandwerk",
            "inventory": inventory,
            "source_role": role,
            "final_path": final_path,
            "preservation_status": status,
            "preservation_basis": basis,
            "source_sha256": f"sha256:{digest or sha256_file(path)}",
            "final_sha256": f"sha256:{final_sha}" if final_sha else "",
            "source_size_bytes": path.stat().st_size,
            "exact_copy_proven": str(exact).lower(),
            "drain_state": "proposed_redundant",
            "deletion_authorized": "false",
            "notes": notes,
        }

    # Exact TIFF copies of a selected source are safe redundant candidates.
    selected_tiff_hashes = {
        catalog[row["inventory"]]["source_sha256"]: row for row in decisions
    }
    selected_tiff_sizes = {int(row["selected_master_size_bytes"]) for row in decisions}
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in TIFF_EXTENSIONS or path in selected_sources:
            continue
        if path.stat().st_size not in selected_tiff_sizes:
            continue
        digest = sha256_file(path)
        row = selected_tiff_hashes.get(digest)
        if row:
            add_drain(
                path,
                inventory=row["inventory"],
                role="exact_duplicate_tiff",
                final_path=row["master_final_relative_path"],
                status="reviewed_redundant_not_accessioned",
                basis="exact_sha256_duplicate_of_selected_tiff_source",
                exact=True,
                notes="Exact byte duplicate of the selected source TIFF; source remains unchanged; deletion not authorized.",
                digest=digest,
            )

    # Byte-identical project-image copies are redundant while one canonical
    # source path remains explicitly retained in the protected source tree.
    image_paths = [
        path for path in sorted(SOURCE.rglob("*"))
        if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS
    ]
    image_sizes: dict[int, list[Path]] = defaultdict(list)
    for path in image_paths:
        image_sizes[path.stat().st_size].append(path)
    image_hash_groups: dict[str, list[Path]] = defaultdict(list)
    for paths in image_sizes.values():
        if len(paths) < 2:
            continue
        for path in paths:
            image_hash_groups[sha256_file(path)].append(path)
    for digest, paths in image_hash_groups.items():
        if len(paths) < 2:
            continue
        canonical = canonical_document(paths, set())
        for path in paths:
            if path == canonical:
                continue
            add_drain(
                path,
                role="exact_duplicate_image",
                status="reviewed_redundant_not_accessioned",
                basis="exact_sha256_duplicate_of_retained_project_image",
                exact=True,
                notes=(
                    f"Exact byte duplicate of retained source image {canonical}; "
                    "source remains unchanged; deletion not authorized."
                ),
                digest=digest,
            )

    # Strict visual matches identify JPEG/PNG/BMP derivatives of accessioned objects.
    image_errors = []
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in IMAGE_EXTENSIONS:
            continue
        try:
            variants = perceptual_features(path)
        except (OSError, UnidentifiedImageError, ValueError) as error:
            image_errors.append({"source_path": str(path), "error": str(error)})
            continue
        best: tuple[int, int, float, str] | None = None
        for inventory, reference in references.items():
            rp, rd, rr = reference
            for cp, cd, cr in variants:
                candidate = (distance(rp, cp), distance(rd, cd), abs(rr - cr) / max(rr, cr), inventory)
                if best is None or candidate < best:
                    best = candidate
        assert best is not None
        pd, dd, aspect_delta, inventory = best
        stem = path.stem.casefold().replace("-", "_")
        token = inventory.casefold().replace("-", "_")
        exact_token = bool(re.search(rf"(^|[^a-z0-9]){re.escape(token)}([^a-z0-9]|$)", stem))
        strict = pd <= 4 and dd <= 5 and aspect_delta <= 0.015
        token_supported = exact_token and pd <= 8 and dd <= 9 and aspect_delta <= 0.025
        if strict or token_supported:
            row = next(item for item in decisions if item["inventory"] == inventory)
            add_drain(
                path,
                inventory=inventory,
                role="derived_image_candidate",
                final_path=row["master_final_relative_path"],
                status="reviewed_redundant_not_accessioned",
                basis="strict_perceptual_match_to_accessioned_object" if strict else "inventory_token_and_perceptual_match",
                exact=False,
                notes=(
                    f"Visual derivative/copy match to {inventory}; pHash distance={pd}; dHash distance={dd}; "
                    f"aspect delta={aspect_delta:.6f}; deletion not authorized."
                ),
            )

    # Content-aware related-document disposition, with exact duplicate groups.
    document_paths = [
        path for path in sorted(SOURCE.rglob("*"))
        if path.is_file() and path.suffix.casefold() in DOCUMENT_EXTENSIONS
    ]
    document_hashes = {path: sha256_file(path) for path in document_paths}
    groups: dict[str, list[Path]] = defaultdict(list)
    for path, digest in document_hashes.items():
        groups[digest].append(path)
    duplicate_of: dict[Path, Path] = {}
    for paths in groups.values():
        if len(paths) < 2:
            continue
        canonical = canonical_document(paths, selected_document_paths)
        for path in paths:
            if path != canonical:
                duplicate_of[path] = canonical

    document_rows: list[dict[str, object]] = []
    for path in document_paths:
        lower = path.name.casefold()
        selected_id = selected_documents.get(path, "")
        disposition = "retain_unique_reference"
        basis = "unique supporting source or reference; not proven redundant"
        if selected_id:
            disposition = "selected_document_master"
            basis = "content-reviewed final project document accessioned as exact bytes"
        elif path in duplicate_of:
            disposition = "exact_duplicate_candidate"
            basis = f"exact SHA-256 duplicate of {duplicate_of[path]}"
        elif lower.startswith("~$"):
            disposition = "office_lock_noise"
            basis = "temporary Microsoft Office lock file"
        elif path.suffix.casefold() in {".url", ".part"}:
            disposition = "transfer_or_link_noise"
            basis = "incomplete transfer fragment or web shortcut; not archival content"
        elif "stundenabrechnung" in lower:
            disposition = "retain_restricted_administrative_record"
            basis = "project-related administrative record; not a drain candidate"
        elif re.match(r"101 abbildungen", lower) or lower in {"bib_fehlende-fotos.xlsx", "101 abbildungen_ag-kommentare.xlsx"}:
            disposition = "superseded_image_selection_workbook"
            basis = "numbered production/selection workbook superseded by final publication and manuscript"
        elif lower in {
            "101 x bäckerhandwerk in berlin.pdf",
            "101 x bäckerhandwerk in berlin-1.docx",
            "101xbaeckerhandwerk_bildtexte_final_03.03.2024.docx",
            "101xbaeckerhandwerk_fließtext_final_03.03.2024.docx",
        }:
            disposition = "superseded_publication_component"
            basis = "earlier draft or component incorporated into selected final documents"
        elif lower == "berningbackofen_crpd.pdf":
            disposition = "compressed_reference_derivative"
            basis = "compressed 34-page derivative of retained BerningBackofen_Katalog.pdf scan set"

        candidate_dispositions = {
            "exact_duplicate_candidate", "office_lock_noise", "transfer_or_link_noise",
            "superseded_image_selection_workbook", "superseded_publication_component",
            "compressed_reference_derivative",
        }
        if disposition in candidate_dispositions:
            add_drain(
                path,
                role="related_document",
                status="reviewed_redundant_not_accessioned",
                basis=disposition,
                exact=disposition == "exact_duplicate_candidate",
                notes=f"{basis}; deletion not authorized.",
                digest=document_hashes[path],
            )
        document_rows.append(
            {
                "source_path": str(path),
                "filename": path.name,
                "format": path.suffix.casefold().lstrip("."),
                "size_bytes": path.stat().st_size,
                "sha256": f"sha256:{document_hashes[path]}",
                "disposition": disposition,
                "basis": basis,
                "selected_document_id": selected_id,
                "deletion_authorized": "false",
            }
        )

    ordered_drain = sorted(drain_rows.values(), key=lambda row: str(row["source_path"]))
    write_csv(DRAIN, DRAIN_FIELDS, ordered_drain)
    write_csv(DOCUMENT_DISPOSITION, DOCUMENT_FIELDS, document_rows)
    summary = {
        "format": "iss-baeckerhandwerk-drain-review-v1",
        "source_root": str(SOURCE),
        "source_mount": mount,
        "source_files": sum(path.is_file() for path in SOURCE.rglob("*")),
        "drain_rows": len(ordered_drain),
        "drain_roles": dict(sorted(__import__("collections").Counter(str(row["source_role"]) for row in ordered_drain).items())),
        "document_rows": len(document_rows),
        "document_dispositions": dict(sorted(__import__("collections").Counter(str(row["disposition"]) for row in document_rows).items())),
        "image_decode_errors": image_errors,
        "deletion_authorized": False,
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
