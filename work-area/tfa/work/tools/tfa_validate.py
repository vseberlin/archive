#!/usr/bin/env python3
"""Independently validate the proposal-only TFA reconciliation projection."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path("/mnt/volume/iss-archive-work/tfa/work/reconciliation/unit-reconciliation")
TOOL = Path("/mnt/volume/iss-archive-work/tfa/work/tools/tfa_prepare.py")
FINAL_UNIT = Path("/mnt/volume/iss-archive/units/tfa")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def read_csv(name: str) -> list[dict[str, str]]:
    with (ROOT / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    receipt = ROOT / "validation.json"
    if receipt.exists():
        raise RuntimeError(f"refusing to replace validation receipt: {receipt}")
    summary = json.loads((ROOT / "summary.json").read_text(encoding="utf-8"))
    if summary.get("result") != "ok":
        raise RuntimeError("projection summary is not ok")

    mount_payload = json.loads(
        subprocess.run(
            [
                "findmnt",
                "-J",
                "-T",
                "/media/vladimir/Elements/restore/archive/WF",
                "-o",
                "TARGET,SOURCE,FSTYPE,OPTIONS",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )["filesystems"][0]
    mount_options = set(mount_payload["options"].split(","))
    if (
        mount_payload["source"] != "/dev/sdf1"
        or mount_payload["fstype"] != "ntfs3"
        or "ro" not in mount_options
    ):
        raise RuntimeError(f"source mount guard failed: {mount_payload}")
    if FINAL_UNIT.exists():
        raise RuntimeError(f"final unit unexpectedly exists: {FINAL_UNIT}")

    objects = read_csv("objects.csv")
    subdivisions = read_csv("subdivision-register.csv")
    memberships = read_csv("subdivision-memberships.csv")
    candidates = read_csv("candidates.csv")
    dispositions = read_csv("source-disposition-proposal.csv")
    drain = read_csv("drain-list-proposal.csv")

    if len({row["record_key"] for row in objects}) != len(objects):
        raise RuntimeError("duplicate object record keys")
    if len({row["inventory"] for row in objects}) != len(objects):
        raise RuntimeError("duplicate object inventories")
    if any(row["decision_state"] != "unresolved" for row in objects):
        raise RuntimeError("unexpected object decision state")
    if len({row["candidate_id"] for row in candidates}) != len(candidates):
        raise RuntimeError("duplicate candidate IDs")
    if len({(row["record_key"], row["source_path"]) for row in candidates}) != len(candidates):
        raise RuntimeError("duplicate object/source candidates")
    if len({row["source_path"] for row in dispositions}) != len(dispositions):
        raise RuntimeError("duplicate source-disposition paths")
    if dispositions != drain:
        raise RuntimeError("proposal drain register differs from source-disposition proposal")
    if any(row["deletion_authorized"].casefold() != "false" for row in dispositions):
        raise RuntimeError("source-disposition proposal authorizes deletion")
    if any(row["exact_copy_proven"].casefold() != "false" for row in dispositions):
        raise RuntimeError("proposal claims exact-copy proof before accession")

    subdivision_counts = {
        row["subdivision_id"]: int(row["assigned_object_count"])
        for row in subdivisions
    }
    observed_counts: dict[str, int] = {}
    for row in memberships:
        observed_counts[row["subdivision_id"]] = observed_counts.get(row["subdivision_id"], 0) + 1
    if subdivision_counts != observed_counts:
        raise RuntimeError("subdivision register and memberships disagree")
    if observed_counts.get("md-collection:857") != 5282:
        raise RuntimeError("parent collection 857 coverage is not 5282")

    connection = sqlite3.connect(f"file:{ROOT / 'reconciliation.sqlite'}?mode=ro", uri=True)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        db_counts = {
            name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in (
                "objects",
                "subdivisions",
                "object_subdivisions",
                "candidates",
                "source_disposition_proposal",
                "drain_list_proposal",
                "reconciliation_decisions",
            )
        }
        db_nonfalse = connection.execute(
            "SELECT count(*) FROM source_disposition_proposal WHERE deletion_authorized != 'false'"
        ).fetchone()[0]
    finally:
        connection.close()
    expected_counts = {
        "objects": len(objects),
        "subdivisions": len(subdivisions),
        "object_subdivisions": len(memberships),
        "candidates": len(candidates),
        "source_disposition_proposal": len(dispositions),
        "drain_list_proposal": len(drain),
        "reconciliation_decisions": 0,
    }
    if quick_check != "ok" or foreign_keys or db_counts != expected_counts or db_nonfalse:
        raise RuntimeError(
            f"SQLite validation failed: quick={quick_check} fk={foreign_keys} "
            f"counts={db_counts} expected={expected_counts} nonfalse={db_nonfalse}"
        )

    fingerprint_checks = {}
    for name, expected in summary["output_fingerprints"].items():
        actual = sha256_file(ROOT / name)
        fingerprint_checks[name] = actual
        if actual != expected:
            raise RuntimeError(f"output fingerprint mismatch: {name}")

    payload = {
        "format": "iss-tfa-reconciliation-validation-v1",
        "validated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "result": "ok",
        "source_mount": mount_payload,
        "source_read_only": True,
        "final_unit_absent": True,
        "quick_check": quick_check,
        "foreign_key_violations": len(foreign_keys),
        "counts": db_counts,
        "deletion_authorized_rows": db_nonfalse,
        "exact_copy_proven_rows": 0,
        "output_fingerprints": fingerprint_checks,
        "summary_sha256": sha256_file(ROOT / "summary.json"),
        "prepare_tool_sha256": sha256_file(TOOL),
        "source_files_modified": 0,
    }
    with receipt.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
