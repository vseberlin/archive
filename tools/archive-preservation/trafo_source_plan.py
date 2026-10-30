#!/usr/bin/env python3
"""Build lightweight Trafo local-source, drain, and OCR planning manifests."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-trafo-source-plan-v1"
BIBLIO_ROOT = "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt"
WF_TRAFO_ROOT = "/media/vladimir/Elements/restore/archive/WF/Trafo"
MIK_1953_TIFF_ROOT = (
    "/media/vladimir/Elements/restore/archive/MIK-Daten/TIF/1953"
)
HUMAN_TIFF_OVERRIDES = {
    "TRO-1970-018-02": (
        "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
        "Trafo tifs 1970-1990/TRO-1970-018"
    ),
    "TRO-1957-015-016": (
        "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
        "Charge 02 tif bis 1969/TRO-1957-015"
    ),
    "TRO-1952-SA-06-Juni": (
        "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
        "Charge 02 tif bis 1969/TRO-1952-Sonderausgabe-04-Juni"
    ),
    "TRO-1954-002": (
        "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
        "Charge 02 tif bis 1969/TRO-1952-002"
    ),
    "TRO-1974-051": (
        "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
        "Trafo tifs 1970-1990/TRO-1974-001"
    ),
    "TRO-1980-005": (
        "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
        "Trafo tifs 1970-1990/TRO-1980-032"
    ),
}
HUMAN_TIFF_OVERRIDE_NOTES = {
    "TRO-1954-002": (
        "curator-confirmed visual match; source TIFF folder is mislabeled "
        "TRO-1952-002"
    ),
    "TRO-1974-051": (
        "curator-confirmed visual match; source TIFF folder is mislabeled "
        "TRO-1974-001"
    ),
    "TRO-1980-005": (
        "curator-confirmed visual match; source TIFF folder is mislabeled "
        "TRO-1980-032"
    ),
}


class PlanError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_inventory(value: str) -> str:
    text = Path(str(value)).stem if "/" in str(value) else str(value)
    text = text.upper().replace("_", "-").replace(" ", "-")
    text = re.sub(r"\.(PDF|TIF|TIFF)$", "", text)
    replacements = {
        "SONDER": "SA",
        "SONDERAUSGABE": "SA",
        "SAAUSGABE": "SA",
        "SANUMMER": "SN",
        "SONDERNUMMER": "SN",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    match = re.match(r"^(TRO-\d{4})-0+(\d+)$", text)
    if match:
        text = f"{match.group(1)}-{int(match.group(2))}"
    return text


def tiff_issue_key(path: Path) -> str:
    parent = path.parent.name
    if parent.upper().startswith("TRO-"):
        return normalize_inventory(parent)
    stem = re.sub(r"[-_]\d{3}(?:[-_]\d{3})?$", "", path.stem)
    return normalize_inventory(stem)


def read_tsv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        file_path = parts[0]
        try:
            size = int(parts[1])
        except (IndexError, ValueError):
            size = 0
        rows.append({"path": file_path, "size_bytes": size})
    return rows


def classify_pdf(path: str) -> str:
    folded = path.casefold()
    if "wasserzeichen" in folded or "kompr" in folded or "upload" in folded:
        return "low_quality_pdf_candidate"
    if "bibliocopy" in folded:
        return "bibliocopy_pdf_candidate"
    return "local_pdf_candidate"


def pdf_score(row: dict[str, Any]) -> tuple[int, int, str]:
    path = str(row["path"])
    folded = path.casefold()
    score = 0
    if path.startswith(BIBLIO_ROOT):
        score += 1000
    if path.startswith(WF_TRAFO_ROOT):
        score += 500
    if "wasserzeichen" in folded:
        score -= 300
    if "kompr" in folded:
        score -= 250
    if "upload" in folded:
        score -= 100
    if "archiv" in folded:
        score += 25
    return (-score, -int(row["size_bytes"]), path)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def selected_role(has_tiff: bool, pdf: dict[str, Any] | None) -> str:
    if has_tiff:
        return "preservation_tiff_pages"
    if pdf:
        return "local_pdf_fallback"
    return "missing_local_source"


def override_tiff_rows(inventory: str) -> list[dict[str, Any]]:
    folder = HUMAN_TIFF_OVERRIDES.get(inventory)
    override = "human_confirmed_tiff_folder"
    match = re.fullmatch(r"TRO-1953-(\d{2})", inventory)
    if not folder and match:
        folder = str(Path(MIK_1953_TIFF_ROOT) / f"WFS-1953-{match.group(1)}")
        override = "mik_1953_tiff_folder"
    if not folder:
        return []
    root = Path(folder)
    if not root.is_dir():
        raise PlanError(f"human TIFF override folder is missing: {root}")
    rows: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.suffix.casefold() not in {".tif", ".tiff"}:
            continue
        rows.append(
            {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "normalized_inventory": normalize_inventory(inventory),
                "override": override,
            }
        )
    if not rows:
        raise PlanError(f"human TIFF override folder has no TIFF files: {root}")
    return rows


def build(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.expanduser().resolve()
    if not str(work_root).startswith("/mnt/volume/iss-archive-work/"):
        raise PlanError("work root must be below /mnt/volume/iss-archive-work")
    md_rows = list(csv.DictReader(args.md_objects.open(encoding="utf-8", newline="")))
    scan_dir = args.scan_dir.expanduser().resolve(strict=True)
    output_dir = work_root / "work" / "source-plan" / args.plan_id
    output_dir.mkdir(parents=True, exist_ok=False)

    pdf_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_tsv(scan_dir / "likely-pdf-hits.tsv"):
        key = normalize_inventory(Path(str(row["path"])).stem)
        row["normalized_inventory"] = key
        row["source_class"] = classify_pdf(str(row["path"]))
        pdf_by_key[key].append(row)

    tiff_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_tsv(scan_dir / "likely-tiff-hits.tsv"):
        key = tiff_issue_key(Path(str(row["path"])))
        row["normalized_inventory"] = key
        tiff_by_key[key].append(row)

    selected_rows: list[dict[str, Any]] = []
    drain_rows: list[dict[str, Any]] = []
    ocr_rows: list[dict[str, Any]] = []
    selected_pdf_paths: set[str] = set()
    selected_tiff_paths: set[str] = set()
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()

    for md in md_rows:
        inventory = str(md["inventory"])
        key = normalize_inventory(inventory)
        tiffs = sorted(tiff_by_key.get(key, []), key=lambda item: str(item["path"]))
        override_rows = override_tiff_rows(inventory)
        if override_rows:
            tiffs = override_rows
        pdfs = sorted(pdf_by_key.get(key, []), key=pdf_score)
        best_pdf = pdfs[0] if pdfs else None
        role = selected_role(bool(tiffs), best_pdf)
        status = "ready_tiff_pages" if tiffs else "ready_pdf_fallback" if best_pdf else "missing_local_source"
        selected_paths = tiffs if tiffs else ([best_pdf] if best_pdf else [])
        for row in selected_paths:
            if role == "preservation_tiff_pages":
                selected_tiff_paths.add(str(row["path"]))
            else:
                selected_pdf_paths.add(str(row["path"]))
        source_counts[role] += 1
        status_counts[status] += 1
        selected_rows.append(
            {
                "inventory": inventory,
                "normalized_inventory": key,
                "md_object_id": md["object_id"],
                "md_pdf_url": md["pdf_urls"],
                "selected_role": role,
                "status": status,
                "tiff_page_count": len(tiffs),
                "tiff_total_bytes": sum(int(row["size_bytes"]) for row in tiffs),
                "selected_local_pdf": str(best_pdf["path"]) if best_pdf else "",
                "selected_local_pdf_bytes": int(best_pdf["size_bytes"]) if best_pdf else "",
                "selected_paths": " | ".join(str(row["path"]) for row in selected_paths),
                "notes": (
                    "MIK 1953 TIFF folder override"
                    if any(
                        row.get("override") == "mik_1953_tiff_folder"
                        for row in override_rows
                    )
                    else HUMAN_TIFF_OVERRIDE_NOTES.get(
                        inventory,
                        "human-confirmed TIFF override",
                    )
                    if override_rows
                    else "review naming/special issue before copying" if not tiffs else ""
                ),
            }
        )
        ocr_rows.append(
            {
                "inventory": inventory,
                "md_object_id": md["object_id"],
                "source_role": role,
                "source_paths": " | ".join(str(row["path"]) for row in selected_paths),
                "page_count": len(tiffs) if tiffs else "",
                "needs_ocr": "yes",
                "ocr_priority": "normal",
                "ocr_input_kind": "tiff_pages" if tiffs else "pdf",
                "ocr_status": "ready" if selected_paths else "blocked",
                "notes": "derived OCR only; not preservation truth",
            }
        )

    for rows_by_key in (pdf_by_key,):
        for key, rows in rows_by_key.items():
            for row in rows:
                path = str(row["path"])
                if path in selected_pdf_paths:
                    continue
                drain_rows.append(
                    {
                        "normalized_inventory": key,
                        "path": path,
                        "size_bytes": int(row["size_bytes"]),
                        "candidate_class": row["source_class"],
                        "reason": "unselected duplicate or low-quality MD/local access PDF candidate",
                        "delete_authorized": "no",
                    }
                )

    for key, rows in tiff_by_key.items():
        for row in rows:
            path = str(row["path"])
            if path in selected_tiff_paths:
                continue
            drain_rows.append(
                {
                    "normalized_inventory": key,
                    "path": path,
                    "size_bytes": int(row["size_bytes"]),
                    "candidate_class": "unselected_tiff_candidate",
                    "reason": "not selected by current simple normalization",
                    "delete_authorized": "no",
                }
            )

    write_csv(
        output_dir / "selected-sources-proposal.csv",
        [
            "inventory",
            "normalized_inventory",
            "md_object_id",
            "md_pdf_url",
            "selected_role",
            "status",
            "tiff_page_count",
            "tiff_total_bytes",
            "selected_local_pdf",
            "selected_local_pdf_bytes",
            "selected_paths",
            "notes",
        ],
        selected_rows,
    )
    write_csv(
        output_dir / "drain-candidates.csv",
        [
            "normalized_inventory",
            "path",
            "size_bytes",
            "candidate_class",
            "reason",
            "delete_authorized",
        ],
        drain_rows,
    )
    write_csv(
        output_dir / "ocr-candidates.csv",
        [
            "inventory",
            "md_object_id",
            "source_role",
            "source_paths",
            "page_count",
            "needs_ocr",
            "ocr_priority",
            "ocr_input_kind",
            "ocr_status",
            "notes",
        ],
        ocr_rows,
    )
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "plan_id": args.plan_id,
        "work_root": str(work_root),
        "md_objects": len(md_rows),
        "selected_status_counts": dict(sorted(status_counts.items())),
        "selected_source_counts": dict(sorted(source_counts.items())),
        "drain_candidates": len(drain_rows),
        "ocr_candidates": len(ocr_rows),
        "hashing_policy": "No source bytes were hashed by this planning step.",
        "human_tiff_overrides": sorted(HUMAN_TIFF_OVERRIDES),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(
        "# Trafo Source Plan\n\n"
        "Lightweight planning output only. This step does not copy, hash, or delete "
        "source files and does not create a final archive package.\n\n"
        "- `selected-sources-proposal.csv`: one proposed local source chain per MD object.\n"
        "- `drain-candidates.csv`: unselected duplicate or low-quality local PDF/TIFF candidates; deletion is not authorized.\n"
        "- `ocr-candidates.csv`: future OCR batch seed manifest; OCR remains derived data.\n"
        "- `summary.json`: aggregate counts.\n",
        encoding="utf-8",
    )
    return {"result": "ok", "output": str(output_dir), **summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--md-objects", type=Path, required=True)
    parser.add_argument("--scan-dir", type=Path, required=True)
    parser.add_argument("--plan-id", default="plan-20260724T1720Z")
    return parser


def main() -> int:
    result = build(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
