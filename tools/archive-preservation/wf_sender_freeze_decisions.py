#!/usr/bin/env python3
"""Freeze WF-Sender curator decisions to exact, stable evidence observations.

Early workbench decisions stored row-local ``fallback:N`` identifiers.  The
row numbers changed when a later fallback run added documented crosswalk
candidates.  This unit-specific tool resolves every append-only decision
against the evidence run active when it was made and emits immutable
path/stat/hash bindings.  It never rewrites the decision log or source files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-wf-sender-stable-decision-bindings-v1"
SAFE_WORK_ROOT = Path("/mnt/volume/iss-archive-work")
DEFAULT_WORK_ROOT = SAFE_WORK_ROOT / "wf-sender"
DEFAULT_PRIMARY_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/runs/"
    "20260728T062423Z-collection-552-primary-match"
)
DEFAULT_INITIAL_FALLBACK_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/fallback-search/runs/"
    "20260728T062928Z-collection-552-fallback-search"
)
DEFAULT_CURRENT_FALLBACK_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/fallback-search/runs/"
    "20260728T083759Z-collection-552-documented-crosswalk"
)
PREVIEW_EXTENSIONS = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}
INITIAL_FALLBACK_DECISION_IDS = set(range(10, 29))
CURRENT_FALLBACK_DECISION_IDS = set(range(36, 47))

BINDING_FIELDS = [
    "decision_id",
    "decided_utc",
    "actor",
    "subject_type",
    "subject_id",
    "decision",
    "category",
    "legacy_candidate_id",
    "candidate_kind",
    "evidence_run",
    "local_object_id",
    "local_group_id",
    "source_family",
    "source_path",
    "source_size_bytes",
    "source_mtime_ns",
    "source_sha256",
    "stable_evidence_id",
    "binding_status",
    "note",
]


class DecisionBindingError(RuntimeError):
    """Raised when a decision cannot be bound without guessing."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_read_only_source(path: Path) -> str:
    result = subprocess.run(
        [
            "findmnt",
            "-n",
            "-o",
            "SOURCE,TARGET,FSTYPE,OPTIONS",
            "--target",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(None, 3)
    if len(fields) != 4 or "ro" not in set(fields[3].split(",")):
        raise DecisionBindingError(
            f"decision evidence mount is not read-only: {result.stdout.strip()}"
        )
    return result.stdout.strip()


def require_new_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    safe = SAFE_WORK_ROOT.resolve()
    if resolved == safe or not resolved.is_relative_to(safe):
        raise DecisionBindingError(f"output escaped archive work root: {resolved}")
    if resolved.exists():
        raise DecisionBindingError(f"refusing existing output: {resolved}")
    return resolved


def atomic_write(path: Path, payload: bytes) -> None:
    if path.exists():
        raise DecisionBindingError(f"refusing to replace artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def csv_bytes(rows: Iterable[dict[str, Any]], fields: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(path: Path, size: int, mtime_ns: int, digest: str) -> str:
    payload = f"{path}\0{size}\0{mtime_ns}\0{digest}".encode()
    return "evidence:sha256:" + hashlib.sha256(payload).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def fallback_candidate(run: Path, candidate_id: str) -> dict[str, str]:
    prefix, separator, number = candidate_id.partition(":")
    if prefix != "fallback" or not separator or not number.isdigit():
        raise DecisionBindingError(f"invalid fallback candidate ID: {candidate_id}")
    wanted = int(number)
    for index, row in enumerate(
        read_csv(run / "filesystem-candidates.csv"), start=1
    ):
        if row["extension"].casefold() not in PREVIEW_EXTENSIONS:
            continue
        if index == wanted:
            return row
    raise DecisionBindingError(
        f"candidate {candidate_id} does not exist in fallback run {run}"
    )


def fallback_run_for_decision(
    decision_id: int, initial_run: Path, current_run: Path
) -> Path:
    if decision_id in INITIAL_FALLBACK_DECISION_IDS:
        return initial_run
    if decision_id in CURRENT_FALLBACK_DECISION_IDS:
        return current_run
    raise DecisionBindingError(
        f"fallback decision {decision_id} is outside the reviewed run intervals"
    )


def preferred_group_file(
    connection: sqlite3.Connection, group_id: int
) -> tuple[sqlite3.Row, sqlite3.Row]:
    group = connection.execute(
        """SELECT groups.local_group_id, groups.local_object_id,
                  groups.source_family, groups.source_directory
           FROM local_groups AS groups WHERE groups.local_group_id=?""",
        (group_id,),
    ).fetchone()
    if group is None:
        raise DecisionBindingError(f"unknown local group: {group_id}")
    source = connection.execute(
        """SELECT source_path, size_bytes, mtime_ns, extension
           FROM local_files WHERE local_group_id=?
           ORDER BY CASE lower(extension)
                      WHEN '.pdf' THEN 0
                      WHEN '.tif' THEN 1
                      WHEN '.tiff' THEN 1
                      WHEN '.jpg' THEN 2
                      WHEN '.jpeg' THEN 2
                      WHEN '.png' THEN 2
                      ELSE 3 END,
                    COALESCE(page_sequence, 0), filename
           LIMIT 1""",
        (group_id,),
    ).fetchone()
    if source is None or source["extension"].casefold() not in PREVIEW_EXTENSIONS:
        raise DecisionBindingError(f"group {group_id} has no preview evidence")
    return group, source


def observed_source(
    source_path: str, recorded_size: int, recorded_mtime_ns: int
) -> tuple[Path, int, int, str, str]:
    path = Path(source_path).resolve(strict=True)
    if not path.is_file():
        raise DecisionBindingError(f"decision evidence is not a file: {path}")
    stat = path.stat()
    if stat.st_size != recorded_size or stat.st_mtime_ns != recorded_mtime_ns:
        raise DecisionBindingError(
            f"decision evidence changed since discovery: {path}"
        )
    digest = sha256(path)
    return (
        path,
        stat.st_size,
        stat.st_mtime_ns,
        digest,
        stable_id(path, stat.st_size, stat.st_mtime_ns, digest),
    )


def freeze(args: argparse.Namespace) -> Path:
    output = require_new_output(args.output)
    primary_run = args.primary_run.resolve(strict=True)
    initial_run = args.initial_fallback_run.resolve(strict=True)
    current_run = args.current_fallback_run.resolve(strict=True)
    database = primary_run / "reconciliation.sqlite"
    if not database.is_file():
        raise DecisionBindingError(f"primary database is missing: {database}")
    mount_state = ensure_read_only_source(Path("/media/vladimir/Elements"))
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DecisionBindingError("decision database integrity failed")
        decisions = connection.execute(
            """SELECT decision_id, decided_utc, actor, subject_type, subject_id,
                      decision, basis
               FROM decisions ORDER BY decision_id"""
        ).fetchall()
        if len(decisions) != 50:
            raise DecisionBindingError(
                f"expected frozen 50-row review log, found {len(decisions)}"
            )
        rows: list[dict[str, Any]] = []
        for decision in decisions:
            try:
                basis = json.loads(decision["basis"])
            except json.JSONDecodeError as error:
                raise DecisionBindingError(
                    f"invalid decision basis JSON: {decision['decision_id']}"
                ) from error
            candidate_id = str(basis.get("candidate_id") or "")
            row: dict[str, Any] = {
                **dict(decision),
                "category": str(basis.get("category") or ""),
                "legacy_candidate_id": candidate_id,
                "candidate_kind": "none",
                "evidence_run": "",
                "local_object_id": basis.get("local_object_id") or "",
                "local_group_id": "",
                "source_family": "",
                "source_path": "",
                "source_size_bytes": "",
                "source_mtime_ns": "",
                "source_sha256": "",
                "stable_evidence_id": "",
                "binding_status": "no_candidate_required",
                "note": str(basis.get("note") or ""),
            }
            if not candidate_id:
                rows.append(row)
                continue
            prefix, separator, raw_id = candidate_id.partition(":")
            if not separator or not raw_id.isdigit():
                raise DecisionBindingError(
                    f"unsupported decision candidate: {candidate_id}"
                )
            if prefix == "group":
                group, source = preferred_group_file(connection, int(raw_id))
                source_path = str(source["source_path"])
                size = int(source["size_bytes"])
                mtime_ns = int(source["mtime_ns"])
                evidence_run = primary_run
                row.update(
                    {
                        "candidate_kind": "primary_group",
                        "local_object_id": int(group["local_object_id"]),
                        "local_group_id": int(group["local_group_id"]),
                        "source_family": str(group["source_family"]),
                    }
                )
            elif prefix == "fallback":
                evidence_run = fallback_run_for_decision(
                    int(decision["decision_id"]), initial_run, current_run
                )
                candidate = fallback_candidate(evidence_run, candidate_id)
                if candidate["md_object_id"] != decision["subject_id"]:
                    raise DecisionBindingError(
                        f"{candidate_id} from {evidence_run.name} belongs to "
                        f"MD {candidate['md_object_id']}, not {decision['subject_id']}"
                    )
                source_path = candidate["source_path"]
                size = int(candidate["size_bytes"])
                mtime_ns = int(candidate["mtime_ns"])
                row.update(
                    {
                        "candidate_kind": "fallback_path",
                        "source_family": str(candidate["fallback_root"]),
                    }
                )
            else:
                raise DecisionBindingError(
                    f"unsupported decision candidate kind: {candidate_id}"
                )
            path, size, mtime_ns, digest, evidence_id = observed_source(
                source_path, size, mtime_ns
            )
            row.update(
                {
                    "evidence_run": str(evidence_run),
                    "source_path": str(path),
                    "source_size_bytes": size,
                    "source_mtime_ns": mtime_ns,
                    "source_sha256": digest,
                    "stable_evidence_id": evidence_id,
                    "binding_status": "bound_and_stat_verified",
                }
            )
            rows.append(row)
    finally:
        connection.close()
    output.mkdir(parents=True)
    atomic_write(output / "decision-bindings.csv", csv_bytes(rows, BINDING_FIELDS))
    relationship_latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        relationship_key = (
            f"local-object:{row['local_object_id']}"
            if row["local_object_id"]
            else str(row["stable_evidence_id"] or "none")
        )
        key = (str(row["subject_type"]), str(row["subject_id"]), relationship_key)
        relationship_latest[key] = {**row, "relationship_key": relationship_key}
    relationship_fields = [*BINDING_FIELDS, "relationship_key"]
    atomic_write(
        output / "latest-per-relationship.csv",
        csv_bytes(
            sorted(
                relationship_latest.values(),
                key=lambda row: (
                    row["subject_type"],
                    row["subject_id"],
                    row["relationship_key"],
                ),
            ),
            relationship_fields,
        ),
    )
    summary = {
        "format": FORMAT,
        "created_utc": utc_now(),
        "source_mount": mount_state,
        "source_policy": "read-only; decisions not rewritten; no accession",
        "primary_run": str(primary_run),
        "initial_fallback_run": str(initial_run),
        "current_fallback_run": str(current_run),
        "decisions": len(rows),
        "bound_candidates": sum(
            row["binding_status"] == "bound_and_stat_verified" for row in rows
        ),
        "no_candidate_decisions": sum(
            row["binding_status"] == "no_candidate_required" for row in rows
        ),
        "relationships": len(relationship_latest),
        "errors": 0,
    }
    atomic_write(
        output / "summary.json",
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    findings = f"""# WF-Sender stable curator-decision bindings

Created: {summary['created_utc']}

- Append-only decisions preserved: **{summary['decisions']}**
- Decisions bound to exact path/stat/SHA-256 evidence: **{summary['bound_candidates']}**
- Decisions requiring no local candidate: **{summary['no_candidate_decisions']}**
- Candidate-specific latest relationships: **{summary['relationships']}**

Decisions 10–28 were resolved against the initial fallback run. Decisions
36–46 were resolved against the documented-crosswalk run. Primary `group:N`
decisions were resolved against the frozen primary database. The original
decision table was not changed.

Candidate-specific relationship projection is deliberate: rejecting local
object `WFS-1971-21` for MD 86428 does not erase the separately confirmed
relationship between MD 86428 and `WFS-1971-22`.
"""
    atomic_write(output / "FINDINGS.md", findings.encode("utf-8"))
    return output


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--primary-run", type=Path, default=DEFAULT_PRIMARY_RUN)
    result.add_argument(
        "--initial-fallback-run",
        type=Path,
        default=DEFAULT_INITIAL_FALLBACK_RUN,
    )
    result.add_argument(
        "--current-fallback-run",
        type=Path,
        default=DEFAULT_CURRENT_FALLBACK_RUN,
    )
    result.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_WORK_ROOT
        / "work/reconciliation/collection-552/decision-bindings/runs"
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-stable-bindings",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        output = freeze(args)
    except (
        DecisionBindingError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        raise SystemExit(f"error: {error}") from error
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
