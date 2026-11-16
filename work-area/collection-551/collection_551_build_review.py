#!/usr/bin/env python3
"""Build the small, difference-first review projection for collection 551.

Only objects already flagged for local binary review are included. Source files
are read-only. Rendered previews and their drain rows live below iss-archive-work.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import os
import subprocess
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path("/mnt/volume/iss-archive-work/collection-551/work/reconciliation/unit-reconciliation-v1")
PREVIEW_ROOT = ROOT / "preview-cache"
OBJECTS_CSV = ROOT / "objects.csv"
RESOURCES_CSV = ROOT / "md-resources.csv"
CANDIDATES_CSV = ROOT / "master-candidates.csv"
PROPOSALS_CSV = ROOT / "master-proposals.csv"
METADATA_CSV = ROOT / "metadata-comparison.csv"
METADATA_EVIDENCE_CSV = ROOT / "metadata-evidence.csv"
METADATA_DIFFERENCES_CSV = ROOT / "metadata-differences.csv"
BINARY_DIFFERENCES_CSV = ROOT / "binary-differences.csv"
PREVIEW_MANIFEST_CSV = ROOT / "preview-manifest.csv"
DRAIN_CSV = ROOT / "drain-list.csv"
SUMMARY_JSON = ROOT / "preparation-summary.json"
RECEIPT_JSON = ROOT / "difference-review-build-receipt.json"

PREVIEW_FIELDS = [
    "preview_id", "md_object_id", "inventory", "candidate_id", "source_path",
    "preview_path", "preview_sha256", "preview_size_bytes", "page_number",
    "render_state", "deletion_authorized",
]
BINARY_FIELDS = [
    "comparison_id", "md_object_id", "inventory", "md_representation_type",
    "md_resource_ids", "md_candidate_ids", "local_candidate_id",
    "duplicate_candidate_ids", "local_path", "local_format", "local_page_count",
    "local_width_px", "local_height_px", "local_size_bytes", "local_sha256",
    "md_page_count", "md_size_bytes", "page_difference", "size_difference_bytes",
    "size_ratio", "text_similarity", "difference_summary", "local_preview_id",
    "deletion_authorized",
]
METADATA_FIELDS = [
    "difference_id", "md_object_id", "inventory", "field", "md_value",
    "local_value", "evidence_sources", "decision_action", "effective_value",
    "decision_note", "actor", "decided_at", "deletion_authorized",
]
DRAIN_FIELDS = [
    "md_object_id", "inventory", "source_path", "source_class", "source_kind",
    "classification", "relationship", "evidence", "sha256", "size_bytes",
    "selected_master_candidate_id", "covered_by_selected_master", "coverage_state",
    "drain_state", "deletion_authorized", "note",
]


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def csv_payload(fields: list[str], rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def atomic_replace(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
    return f"{prefix}-{digest}"


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)


def render_preview(candidate: dict[str, str], preview_path: Path) -> None:
    source = Path(candidate["candidate_path"])
    temporary = preview_path.with_name(f".{preview_path.stem}.{uuid.uuid4().hex}.jpg")
    temporary.unlink(missing_ok=True)
    try:
        if candidate["format"].casefold() == "pdf":
            prefix = temporary.parent / f".{candidate['candidate_id']}-{uuid.uuid4().hex}"
            run([
                "pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-scale-to", "900",
                "-jpeg", "-jpegopt", "quality=85,optimize=y", str(source), str(prefix),
            ])
            generated = prefix.parent / f"{prefix.name}.jpg"
            os.replace(generated, temporary)
        else:
            run([
                "vipsthumbnail", str(source), "--size", "900x900",
                "--output", f"{temporary}[Q=85,strip]", "--vips-concurrency", "2",
            ])
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError(f"preview renderer produced no file for {source}")
        os.replace(temporary, preview_path)
    finally:
        temporary.unlink(missing_ok=True)


def extract_text(path: Path, pages: int = 3) -> str:
    result = subprocess.run(
        ["pdftotext", "-f", "1", "-l", str(pages), str(path), "-"],
        check=True, capture_output=True, text=True, errors="replace", timeout=120,
    )
    return " ".join(result.stdout.casefold().split())[:80_000]


def main() -> None:
    for executable in ("pdftoppm", "pdftotext", "vipsthumbnail"):
        if subprocess.run(["sh", "-c", f"command -v {executable}"], capture_output=True).returncode:
            raise RuntimeError(f"required preview tool is missing: {executable}")
    proposals = read_csv(PROPOSALS_CSV)
    vague_ids = {
        row["md_object_id"] for row in proposals
        if row["proposal_state"] == "local_binary_enrichment_review_required"
    }
    if len(vague_ids) != 13:
        raise RuntimeError(f"expected 13 vague objects, found {len(vague_ids)}")

    objects = {row["md_object_id"]: row for row in read_csv(OBJECTS_CSV)}
    resources_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(RESOURCES_CSV):
        if row["md_object_id"] in vague_ids:
            resources_by_object[row["md_object_id"]].append(row)
    for rows in resources_by_object.values():
        rows.sort(key=lambda row: (int(row["resource_order"]), int(row["resource_id"])))

    candidates = read_csv(CANDIDATES_CSV)
    candidates_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
    candidates_by_id = {}
    for row in candidates:
        candidates_by_id[row["candidate_id"]] = row
        if row["md_object_id"] in vague_ids:
            candidates_by_object[row["md_object_id"]].append(row)

    preview_candidates: list[dict[str, str]] = []
    for object_id in sorted(vague_ids, key=int):
        preview_candidates.extend(
            row for row in candidates_by_object[object_id]
            if row["source_kind"] == "museum_digital_capture"
            or row["candidate_role"] == "potential_local_binary_enrichment"
        )
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    expected_names = {f"{row['candidate_id']}-p1.jpg" for row in preview_candidates}
    unexpected = [path for path in PREVIEW_ROOT.iterdir() if path.is_file() and path.name not in expected_names]
    if unexpected:
        raise RuntimeError(f"unexpected files in preview cache: {unexpected[:3]}")

    preview_rows: list[dict[str, str]] = []
    preview_by_candidate: dict[str, dict[str, str]] = {}
    rendered = 0
    for candidate in preview_candidates:
        preview_path = PREVIEW_ROOT / f"{candidate['candidate_id']}-p1.jpg"
        if not preview_path.is_file():
            render_preview(candidate, preview_path)
            rendered += 1
        row = {
            "preview_id": candidate["candidate_id"] + "-p1",
            "md_object_id": candidate["md_object_id"],
            "inventory": candidate["inventory"],
            "candidate_id": candidate["candidate_id"],
            "source_path": candidate["candidate_path"],
            "preview_path": str(preview_path),
            "preview_sha256": sha256_file(preview_path),
            "preview_size_bytes": str(preview_path.stat().st_size),
            "page_number": "1", "render_state": "rendered_first_page",
            "deletion_authorized": "false",
        }
        preview_rows.append(row)
        preview_by_candidate[candidate["candidate_id"]] = row

    # Build a bounded text sample for each PDF that appears in a comparison.
    text_by_path: dict[str, str] = {}
    for candidate in preview_candidates:
        if candidate["format"].casefold() == "pdf" and candidate["candidate_path"] not in text_by_path:
            text_by_path[candidate["candidate_path"]] = extract_text(Path(candidate["candidate_path"]))

    binary_rows: list[dict[str, str]] = []
    for object_id in sorted(vague_ids, key=int):
        obj = objects[object_id]
        md_candidates = [
            row for row in candidates_by_object[object_id]
            if row["source_kind"] == "museum_digital_capture"
        ]
        local_candidates = [
            row for row in candidates_by_object[object_id]
            if row["candidate_role"] == "potential_local_binary_enrichment"
        ]
        # Repeated index occurrences of the same bytes are one review alternative.
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in local_candidates:
            preview_hash = preview_by_candidate[row["candidate_id"]]["preview_sha256"]
            key = row["known_sha256"] or ("preview:" + preview_hash)
            if row["format"].casefold() != "pdf":
                key = "preview:" + preview_hash
            grouped[key].append(row)

        md_pages_known = all(row.get("page_count") for row in md_candidates)
        md_pages = sum(int(row["page_count"]) for row in md_candidates if row.get("page_count"))
        md_size = sum(int(row["size_bytes"]) for row in md_candidates)
        md_text = " ".join(text_by_path.get(row["candidate_path"], "") for row in md_candidates).strip()
        md_resource_ids = [row["resource_id"] for row in md_candidates]
        md_representation = "ordered_md_resource_set" if len(md_candidates) > 1 else "single_md_file"
        for group in grouped.values():
            local = group[0]
            local_pages = int(local["page_count"]) if local.get("page_count") else None
            page_difference = local_pages - md_pages if local_pages is not None and md_pages_known else None
            size_difference = int(local["size_bytes"]) - md_size
            ratio = int(local["size_bytes"]) / md_size if md_size else 0.0
            local_text = text_by_path.get(local["candidate_path"], "")
            similarity = difflib.SequenceMatcher(None, md_text, local_text, autojunk=False).ratio() if md_text and local_text else None
            details = []
            if page_difference is not None:
                details.append("same page extent" if page_difference == 0 else f"{abs(page_difference)} page(s) {'more' if page_difference > 0 else 'fewer'}")
            else:
                details.append(f"{local['format'].upper()} image versus MD image set")
            details.append(f"{abs(size_difference)} bytes {'larger' if size_difference > 0 else 'smaller'}")
            if similarity is not None:
                details.append(f"first 3 pages text {similarity:.0%} similar")
            if len(group) > 1:
                details.append(f"{len(group)} indexed occurrences collapsed")
            binary_rows.append({
                "comparison_id": stable_id("binary", object_id, local["known_sha256"] or local["candidate_path"]),
                "md_object_id": object_id, "inventory": obj["inventory"],
                "md_representation_type": md_representation,
                "md_resource_ids": json.dumps(md_resource_ids),
                "md_candidate_ids": json.dumps([row["candidate_id"] for row in md_candidates]),
                "local_candidate_id": local["candidate_id"],
                "duplicate_candidate_ids": json.dumps([row["candidate_id"] for row in group[1:]]),
                "local_path": local["candidate_path"], "local_format": local["format"],
                "local_page_count": local.get("page_count", ""),
                "local_width_px": local.get("width_px", ""), "local_height_px": local.get("height_px", ""),
                "local_size_bytes": local["size_bytes"], "local_sha256": local["known_sha256"],
                "md_page_count": str(md_pages) if md_pages_known else "", "md_size_bytes": str(md_size),
                "page_difference": str(page_difference) if page_difference is not None else "",
                "size_difference_bytes": str(size_difference), "size_ratio": f"{ratio:.6f}",
                "text_similarity": f"{similarity:.6f}" if similarity is not None else "",
                "difference_summary": "; ".join(details),
                "local_preview_id": preview_by_candidate[local["candidate_id"]]["preview_id"],
                "deletion_authorized": "false",
            })

    existing_metadata = {
        row["difference_id"]: row for row in read_csv(METADATA_DIFFERENCES_CSV)
    } if METADATA_DIFFERENCES_CSV.exists() else {}
    evidence_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(METADATA_EVIDENCE_CSV):
        if row.get("md_object_id") in vague_ids:
            evidence_by_object[row["md_object_id"]].append(row)
    metadata_rows: list[dict[str, str]] = []
    for comparison in read_csv(METADATA_CSV):
        object_id = comparison["md_object_id"]
        if object_id not in vague_ids:
            continue
        md_values = json.loads(comparison["md_values_json"])
        local_values = json.loads(comparison["local_values_json"])
        fields = [field.strip() for field in comparison["local_enrichment_fields"].split(";") if field.strip()]
        for field in fields:
            values = local_values.get(field, [])
            if not isinstance(values, list):
                values = [values]
            for value in values:
                local_value = str(value)
                difference_id = stable_id("metadata", object_id, field, local_value)
                sources = []
                for evidence in evidence_by_object[object_id]:
                    evidence_fields = json.loads(evidence["metadata_fields_json"])
                    if str(evidence_fields.get(field, "")) == local_value:
                        sources.append({
                            "path": evidence["source_path"], "sheet": evidence["sheet"],
                            "row": evidence["row_number"],
                        })
                prior = existing_metadata.get(difference_id, {})
                action = prior.get("decision_action", "pending")
                if action not in {"pending", "accept", "ignore", "substitute", "add"}:
                    raise RuntimeError(f"invalid preserved metadata action: {action}")
                metadata_rows.append({
                    "difference_id": difference_id, "md_object_id": object_id,
                    "inventory": comparison["inventory"], "field": field,
                    "md_value": str(md_values.get(field, "")), "local_value": local_value,
                    "evidence_sources": json.dumps(sources, ensure_ascii=False),
                    "decision_action": action, "effective_value": prior.get("effective_value", ""),
                    "decision_note": prior.get("decision_note", ""), "actor": prior.get("actor", ""),
                    "decided_at": prior.get("decided_at", ""), "deletion_authorized": "false",
                })

    preview_rows.sort(key=lambda row: (int(row["md_object_id"]), row["candidate_id"]))
    binary_rows.sort(key=lambda row: (int(row["md_object_id"]), row["local_candidate_id"]))
    metadata_rows.sort(key=lambda row: (int(row["md_object_id"]), row["field"], row["local_value"].casefold()))
    atomic_replace(PREVIEW_MANIFEST_CSV, csv_payload(PREVIEW_FIELDS, preview_rows))
    atomic_replace(BINARY_DIFFERENCES_CSV, csv_payload(BINARY_FIELDS, binary_rows))
    atomic_replace(METADATA_DIFFERENCES_CSV, csv_payload(METADATA_FIELDS, metadata_rows))

    drain_rows = read_csv(DRAIN_CSV)
    by_path = {row["source_path"]: row for row in drain_rows if row["classification"] != "transient_workbench_preview"}
    for preview in preview_rows:
        row = {
            "md_object_id": preview["md_object_id"], "inventory": preview["inventory"],
            "source_path": preview["preview_path"], "source_class": "work_capture",
            "source_kind": "jpg", "classification": "transient_workbench_preview",
            "relationship": "derivative_or_transient_cleanup_candidate",
            "evidence": "rebuildable first-page workbench preview",
            "sha256": preview["preview_sha256"], "size_bytes": preview["preview_size_bytes"],
            "selected_master_candidate_id": "", "covered_by_selected_master": "false",
            "coverage_state": "not_yet_accessioned",
            "drain_state": "blocked_until_human_decisions_and_validated_accession",
            "deletion_authorized": "false",
            "note": "Drain listing records cleanup intent only; it never authorizes deletion.",
        }
        by_path[row["source_path"]] = row
    final_drain = sorted(by_path.values(), key=lambda row: (row["source_class"], row["source_path"].casefold()))
    atomic_replace(DRAIN_CSV, csv_payload(DRAIN_FIELDS, final_drain))

    summary = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
    summary.update({
        "drain_rows": len(final_drain),
        "drain_protected_source_rows": sum(row["source_class"] == "protected_usb_source" for row in final_drain),
        "drain_work_capture_rows": sum(row["source_class"] == "work_capture" for row in final_drain),
        "drain_classifications": dict(sorted(Counter(row["classification"] for row in final_drain).items())),
        "review_queue_objects": len(vague_ids), "binary_difference_rows": len(binary_rows),
        "metadata_difference_rows": len(metadata_rows), "preview_rows": len(preview_rows),
    })
    atomic_replace(SUMMARY_JSON, (json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
    receipt = {
        "format": "iss-collection-551-difference-review-build-v1", "created_at": now(),
        "result": "ok", "review_queue_objects": len(vague_ids),
        "clear_objects_hidden": len(objects) - len(vague_ids),
        "binary_difference_rows": len(binary_rows), "metadata_difference_rows": len(metadata_rows),
        "preview_rows": len(preview_rows), "previews_rendered_this_run": rendered,
        "drain_rows": len(final_drain), "source_files_modified": 0,
        "master_decisions_changed": 0, "accession_performed": False,
        "deletion_authorized": False,
    }
    atomic_replace(RECEIPT_JSON, (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
