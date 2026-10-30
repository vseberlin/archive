#!/usr/bin/env python3
"""Reopen one reconciled recovery record and attach surviving visual candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import reconciliation
from workflows import reconcile


CAMPAIGN_ID = "MD-RECOVERY-WF-PARAGON"
MANIFEST_FORMAT = "iss-recovery-exceptions-v1"
ALLOWED_SOURCE_ROOTS = (
    Path("/mnt/volume/archive"),
    Path("/mnt/volume/archive-intake"),
    Path("/media/vladimir/Elements"),
)


class MismatchReopenError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24].upper()}"


def backup_database(path: Path, destination: Path) -> None:
    source = sqlite3.connect(path)
    backup = sqlite3.connect(destination)
    try:
        source.backup(backup)
        backup.commit()
    finally:
        backup.close()
        source.close()
    check = sqlite3.connect(f"file:{destination}?mode=ro&immutable=1", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MismatchReopenError(f"backup integrity failed: {destination}")
    finally:
        check.close()


def safe_candidate(path: Path) -> Path:
    source = path.expanduser().resolve(strict=True)
    if not source.is_file() or not any(
        source != root.resolve()
        and source.is_relative_to(root.resolve())
        for root in ALLOWED_SOURCE_ROOTS
    ):
        raise MismatchReopenError(
            f"candidate is outside protected evidence roots: {source}"
        )
    return source


def apply(
    *,
    root: Path,
    inventory: str,
    candidates: list[Path],
    actor: str,
    reason: str,
) -> dict[str, object]:
    root = catalog_core.assert_safe_work_root(root.resolve(strict=True))
    catalog_core.require_core_mode(root)
    sources = [safe_candidate(path) for path in candidates]
    if not sources:
        raise MismatchReopenError("at least one surviving candidate is required")
    catalog_path = root / "work" / "catalog.sqlite"
    campaign_path = root / "work" / "reconciliation" / "museum-digital.sqlite"
    manifest_path = (
        root
        / "work"
        / "recovery"
        / "WF-PARAGON"
        / "reconciliation-exceptions.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != MANIFEST_FORMAT:
        raise MismatchReopenError("recovery exception manifest is invalid")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = root / "work" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    catalog_backup = backup_dir / f"{stamp}-PRE-MISMATCH-{inventory}-CATALOG.sqlite"
    campaign_backup = backup_dir / f"{stamp}-PRE-MISMATCH-{inventory}-CAMPAIGN.sqlite"
    manifest_backup = backup_dir / f"{stamp}-PRE-MISMATCH-{inventory}-EXCEPTIONS.json"
    backup_database(catalog_path, catalog_backup)
    backup_database(campaign_path, campaign_backup)
    shutil.copy2(manifest_path, manifest_backup)

    catalog = catalog_core.connect_catalog(catalog_path)
    campaign = reconciliation.connect_campaign(root)
    try:
        entity = catalog.execute(
            """SELECT entity_id FROM archive_objects
               WHERE archival_label=? AND status='active'""",
            (inventory,),
        ).fetchone()
        campaign_record = campaign.execute(
            """SELECT * FROM reconciliation_records
               WHERE campaign_id=? AND inventory_label=?""",
            (CAMPAIGN_ID, inventory),
        ).fetchone()
        if not entity or not campaign_record:
            raise MismatchReopenError("record is not in the bounded campaign")
        decision = reconciliation.review_identity(
            campaign,
            catalog,
            record_id=str(campaign_record["record_id"]),
            outcome="wrong_master",
            actor=actor,
            reason=reason,
        )
        catalog_core.set_reconciliation_review_status(
            catalog,
            entity_id=str(entity["entity_id"]),
            status="not_ready",
            actor=actor,
            reason=reason,
        )
        catalog_core.set_recovery_correction_status(
            catalog,
            entity_id=str(entity["entity_id"]),
            state="open",
            actor=actor,
            reason=reason,
        )
        inserted = 0
        campaign.execute("BEGIN IMMEDIATE")
        try:
            for index, source in enumerate(sources, start=1):
                digest = sha256_file(source)
                evidence_id = stable_id(
                    "VIS", str(campaign_record["record_id"]), digest
                )
                if campaign.execute(
                    "SELECT 1 FROM visual_evidence WHERE evidence_id=?",
                    (evidence_id,),
                ).fetchone():
                    continue
                cache_dir = (
                    root
                    / "work"
                    / "reconciliation"
                    / "cache"
                    / CAMPAIGN_ID
                    / str(campaign_record["record_id"])
                )
                cache_dir.mkdir(parents=True, exist_ok=True)
                preview = cache_dir / f"mismatch-{index:02d}-{source.stem}.jpg"
                ok, detail = reconcile.create_visual_preview(source, preview)
                width, height = reconcile.image_dimensions(source)
                campaign.execute(
                    """INSERT INTO visual_evidence (
                           evidence_id, record_id, role, label, cache_relpath,
                           source_locator, content_digest, media_type, width_px,
                           height_px, status, detail
                       ) VALUES (?, ?, 'source_record_image', ?, ?, ?, ?, ?,
                                 ?, ?, ?, ?)""",
                    (
                        evidence_id,
                        campaign_record["record_id"],
                        f"Mismatch search candidate: {source.name}",
                        preview.relative_to(root).as_posix() if ok else "",
                        str(source),
                        f"sha256:{digest}",
                        mimetypes.guess_type(source.name)[0] or "",
                        int(width) if str(width).isdigit() else None,
                        int(height) if str(height).isdigit() else None,
                        "available" if ok else "preview_unavailable",
                        detail,
                    ),
                )
                inserted += 1
            campaign.commit()
        except Exception:
            campaign.rollback()
            raise
        if campaign.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MismatchReopenError("campaign integrity check failed")
        catalog_core.assert_catalog_integrity(catalog)
    finally:
        campaign.close()
        catalog.close()

    record = next(
        (
            item
            for item in manifest.get("records") or []
            if item.get("inventory") == inventory
        ),
        None,
    )
    if record is None:
        record = {
            "inventory": inventory,
            "categories": [],
            "correction_required": True,
            "reason": reason,
            "local": {},
            "files": [],
        }
        manifest.setdefault("records", []).append(record)
    categories = list(record.get("categories") or [])
    if "visual_mismatch" not in categories:
        categories.append("visual_mismatch")
    record["categories"] = categories
    record["correction_required"] = True
    record["reason"] = reason
    known = {str(item.get("sha256") or "") for item in record.get("files") or []}
    for source in sources:
        digest = sha256_file(source)
        if digest not in known:
            record.setdefault("files", []).append(
                {
                    "path": str(source),
                    "sha256": digest,
                    "size_bytes": source.stat().st_size,
                }
            )
            known.add(digest)
    manifest["records"] = sorted(
        manifest["records"], key=lambda item: str(item["inventory"])
    )
    manifest.setdefault("summary", {})["records"] = len(manifest["records"])
    manifest["summary"]["corrections"] = sum(
        bool(item.get("correction_required")) for item in manifest["records"]
    )
    temporary = manifest_path.with_name(f".{manifest_path.name}.{stamp}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return {
        "result": "ok",
        "inventory": inventory,
        "identity_state": decision["state"],
        "candidates": len(sources),
        "inserted_visuals": inserted,
        "backups": [
            str(catalog_backup),
            str(campaign_backup),
            str(manifest_backup),
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--candidate", type=Path, action="append", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = {
        "inventory": args.inventory,
        "candidates": [
            {
                "path": str(safe_candidate(path)),
                "sha256": sha256_file(safe_candidate(path)),
            }
            for path in args.candidate
        ],
        "reason": args.reason,
    }
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    print(
        json.dumps(
            apply(
                root=args.root,
                inventory=args.inventory,
                candidates=args.candidate,
                actor=args.actor,
                reason=args.reason,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
