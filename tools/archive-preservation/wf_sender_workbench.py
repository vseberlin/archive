#!/usr/bin/env python3
"""Bounded data adapter for WF-Sender collection-552 visual review."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class WfSenderWorkbenchError(RuntimeError):
    """Raised when the bounded WF-Sender review contract is violated."""


DECISIONS = {
    "confirm_local_candidate",
    "reject_local_candidates",
    "needs_more_review",
    "retain_as_archive_candidate",
    "reject_as_duplicate_or_non_object",
    "accept_md_only_fallback",
    "needs_more_local_search",
}
PROTECTED_ROOTS = (
    Path("/mnt/volume/archive"),
    Path("/mnt/volume/archive-intake"),
    Path("/media/vladimir/Elements"),
)
MD_HOSTS = {"berlin.museum-digital.de", "www.museum-digital.de"}
MAX_MD_PDF_BYTES = 256 * 1024 * 1024
PREVIEW_EXTENSIONS = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class WfSenderReview:
    """Read the frozen evidence runs and append review decisions to work DB."""

    def __init__(self, root: Path, primary_run: Path, fallback_run: Path) -> None:
        self.root = root.resolve()
        reconciliation_root = (self.root / "work" / "reconciliation").resolve()
        self.primary_run = primary_run.expanduser().resolve(strict=True)
        self.fallback_run = fallback_run.expanduser().resolve(strict=True)
        for path in (self.primary_run, self.fallback_run):
            if not path.is_dir() or not path.is_relative_to(reconciliation_root):
                raise WfSenderWorkbenchError(
                    f"WF-Sender evidence run escaped reconciliation root: {path}"
                )
        self.database = self.primary_run / "reconciliation.sqlite"
        if not self.database.is_file():
            raise WfSenderWorkbenchError(
                f"primary reconciliation database is missing: {self.database}"
            )
        self.cross_candidate_path = (
            self.primary_run.parents[1] / "cross-candidates.json"
        )
        self.cross_candidates: list[dict[str, Any]] = []
        if self.cross_candidate_path.is_file():
            try:
                cross_manifest = json.loads(
                    self.cross_candidate_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise WfSenderWorkbenchError(
                    f"cannot read cross-candidate manifest: "
                    f"{self.cross_candidate_path}"
                ) from error
            if (
                not isinstance(cross_manifest, dict)
                or cross_manifest.get("format")
                != "iss-wf-sender-cross-candidates-v1"
                or not isinstance(cross_manifest.get("candidates"), list)
            ):
                raise WfSenderWorkbenchError(
                    "WF-Sender cross-candidate manifest format is invalid"
                )
            self.cross_candidates = [
                dict(item)
                for item in cross_manifest["candidates"]
                if isinstance(item, dict)
            ]
        self.fallback_targets = {
            row["md_object_id"]: row
            for row in _read_csv(self.fallback_run / "target-objects.csv")
        }
        self.fallback_candidates: dict[str, list[dict[str, Any]]] = {}
        for index, row in enumerate(
            _read_csv(self.fallback_run / "filesystem-candidates.csv"), start=1
        ):
            if row["extension"].casefold() not in PREVIEW_EXTENSIONS:
                continue
            candidate = dict(row)
            candidate["candidate_id"] = f"fallback:{index}"
            candidate["files"] = [
                {
                    "source_path": row["source_path"],
                    "filename": row["filename"],
                    "extension": row["extension"],
                    "size_bytes": int(row["size_bytes"]),
                    "file_role": "fallback_path_candidate",
                }
            ]
            self.fallback_candidates.setdefault(row["md_object_id"], []).append(
                candidate
            )
        for candidates in self.fallback_candidates.values():
            candidates.sort(
                key=lambda item: (
                    0 if item["alias_method"] == "exact_inventory" else 1,
                    1 if "/Uploads" in item["source_path"] else 0,
                    1
                    if any(
                        marker in item["source_path"].casefold()
                        for marker in ("wasserzeichen", "komprimiert", "compress")
                    )
                    else 0,
                    item["source_path"],
                )
            )
        self.cache_root = self.root / "work" / "workbench-cache" / "wf-sender"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        with closing(self.connect(read_only=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise WfSenderWorkbenchError(
                    f"WF-Sender reconciliation database integrity failed: {result}"
                )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        required = {
            "md_objects",
            "md_resources",
            "local_objects",
            "local_groups",
            "local_files",
            "candidate_matches",
            "decisions",
        }
        if not required.issubset(tables):
            raise WfSenderWorkbenchError("primary reconciliation schema is incomplete")
        with closing(self.connect(read_only=True)) as connection:
            seen: set[tuple[str, int]] = set()
            for item in self.cross_candidates:
                md_object_id = str(item.get("md_object_id") or "")
                local_object_id = int(item.get("local_object_id") or 0)
                key = (md_object_id, local_object_id)
                if (
                    not md_object_id.isdigit()
                    or local_object_id <= 0
                    or key in seen
                    or item.get("review_state") != "needs_review"
                    or item.get("confidence") not in {"high", "medium", "low"}
                    or not str(item.get("basis") or "").strip()
                    or connection.execute(
                        "SELECT 1 FROM md_objects WHERE md_object_id=?",
                        (md_object_id,),
                    ).fetchone()
                    is None
                    or connection.execute(
                        "SELECT 1 FROM local_objects WHERE local_object_id=?",
                        (local_object_id,),
                    ).fetchone()
                    is None
                ):
                    raise WfSenderWorkbenchError(
                        f"invalid WF-Sender cross candidate: {key}"
                    )
                seen.add(key)

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.database}?mode=ro", uri=True, timeout=10
            )
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _subject(record_key: str) -> tuple[str, str]:
        prefix, separator, subject_id = record_key.partition(":")
        if not separator or prefix not in {"md", "local"} or not subject_id.isdigit():
            raise WfSenderWorkbenchError(f"invalid review record: {record_key}")
        return ("md_object" if prefix == "md" else "local_object", subject_id)

    def _group_candidates(
        self, connection: sqlite3.Connection, *, where: str, value: str | int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"""SELECT DISTINCT local.local_group_id, local.local_object_id,
                       local.source_family, local.issue_key_raw,
                       local.source_directory, local.file_count,
                       local.total_size_bytes
                FROM local_groups AS local
                {where}
                ORDER BY CASE local.source_family WHEN 'PDF' THEN 0 ELSE 1 END,
                         local.local_group_id""",
            (value,),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            candidate = dict(row)
            candidate["candidate_id"] = f"group:{row['local_group_id']}"
            candidate["files"] = [
                dict(item)
                for item in connection.execute(
                    """SELECT source_path, filename, extension, size_bytes,
                              file_role, page_sequence
                       FROM local_files
                       WHERE local_group_id=?
                       ORDER BY CASE extension
                                  WHEN '.pdf' THEN 0
                                  WHEN '.tif' THEN 1
                                  WHEN '.tiff' THEN 1 ELSE 2 END,
                                COALESCE(page_sequence, 0), filename""",
                    (row["local_group_id"],),
                )
            ]
            candidates.append(candidate)
        return candidates

    def records(self) -> list[dict[str, Any]]:
        with closing(self.connect(read_only=True)) as connection:
            latest = {
                (row["subject_type"], row["subject_id"]): dict(row)
                for row in connection.execute(
                    """SELECT decision_id, decided_utc, actor, subject_type,
                              subject_id, decision, basis
                       FROM decisions
                       WHERE decision_id IN (
                         SELECT MAX(decision_id) FROM decisions
                         GROUP BY subject_type, subject_id
                    )"""
                )
            }
            cross_by_md: dict[str, list[dict[str, Any]]] = {}
            cross_by_local: dict[int, list[dict[str, Any]]] = {}
            for candidate in self.cross_candidates:
                md_object_id = str(candidate["md_object_id"])
                local_object_id = int(candidate["local_object_id"])
                cross_by_md.setdefault(md_object_id, []).append(candidate)
                cross_by_local.setdefault(local_object_id, []).append(candidate)
            linked_local_decisions: dict[int, dict[str, Any]] = {}
            for (subject_type, subject_id), decision in latest.items():
                if (
                    subject_type != "md_object"
                    or decision["decision"] != "confirm_local_candidate"
                ):
                    continue
                try:
                    basis = json.loads(str(decision["basis"]))
                    candidate_id = str(basis.get("candidate_id") or "")
                    prefix, separator, group_id = candidate_id.partition(":")
                    if prefix != "group" or not separator or not group_id.isdigit():
                        continue
                except (json.JSONDecodeError, AttributeError):
                    continue
                group = connection.execute(
                    """SELECT local_object_id FROM local_groups
                       WHERE local_group_id=?""",
                    (int(group_id),),
                ).fetchone()
                if group is None:
                    continue
                derived = dict(decision)
                derived["decision"] = "linked_to_museum_digital"
                derived["linked_md_object_id"] = subject_id
                linked_local_decisions[int(group["local_object_id"])] = derived
            records: list[dict[str, Any]] = []
            cross_md_ids = sorted(cross_by_md)
            cross_clause = ""
            parameters: tuple[str, ...] = ()
            if cross_md_ids:
                placeholders = ",".join("?" for _ in cross_md_ids)
                cross_clause = f" OR md_object_id IN ({placeholders})"
                parameters = tuple(cross_md_ids)
            md_rows = connection.execute(
                f"""SELECT * FROM md_objects
                   WHERE duplicate_inventory=1 OR local_candidate_count=0
                      {cross_clause}
                   ORDER BY CASE WHEN duplicate_inventory=1 THEN 0 ELSE 1 END,
                            inventory_raw, md_object_id""",
                parameters,
            ).fetchall()
            for row in md_rows:
                item = dict(row)
                object_id = str(row["md_object_id"])
                if row["duplicate_inventory"]:
                    category = "duplicate_inventory"
                    candidates = self._group_candidates(
                        connection,
                        where=(
                            "JOIN candidate_matches AS match "
                            "ON match.local_group_id=local.local_group_id "
                            "WHERE match.md_object_id=?"
                        ),
                        value=object_id,
                    )
                elif row["local_candidate_count"] == 0:
                    target = self.fallback_targets.get(object_id, {})
                    candidates = self.fallback_candidates.get(object_id, [])
                    category = (
                        "fallback_candidates"
                        if candidates
                        else "missing_local"
                    )
                    item["fallback_search_state"] = target.get(
                        "fallback_search_state",
                        "fallback_run_missing_target",
                    )
                else:
                    category = "cross_candidate"
                    candidates = self._group_candidates(
                        connection,
                        where=(
                            "JOIN candidate_matches AS match "
                            "ON match.local_group_id=local.local_group_id "
                            "WHERE match.md_object_id=?"
                        ),
                        value=object_id,
                    )
                known_candidate_ids = {
                    str(candidate["candidate_id"]) for candidate in candidates
                }
                for suggestion in cross_by_md.get(object_id, []):
                    for candidate in self._group_candidates(
                        connection,
                        where="WHERE local.local_object_id=?",
                        value=int(suggestion["local_object_id"]),
                    ):
                        if candidate["candidate_id"] in known_candidate_ids:
                            continue
                        candidate["suggested_cross_match"] = True
                        candidate["suggestion_method"] = suggestion["method"]
                        candidate["suggestion_basis"] = suggestion["basis"]
                        candidate["suggestion_confidence"] = suggestion["confidence"]
                        candidates.append(candidate)
                        known_candidate_ids.add(str(candidate["candidate_id"]))
                item.update(
                    {
                        "record_key": f"md:{object_id}",
                        "subject_type": "md_object",
                        "subject_id": object_id,
                        "category": category,
                        "resources": [
                            dict(resource)
                            for resource in connection.execute(
                                """SELECT resource_id, resource_url, resource_role
                                   FROM md_resources WHERE md_object_id=?
                                   ORDER BY resource_id""",
                                (object_id,),
                            )
                        ],
                        "candidates": candidates,
                        "decision": latest.get(("md_object", object_id)),
                    }
                )
                records.append(item)
            for row in connection.execute(
                """SELECT * FROM local_objects
                   WHERE local_only_archive_candidate=1
                   ORDER BY representative_inventory, local_object_id"""
            ):
                object_id = str(row["local_object_id"])
                md_suggestions = []
                for suggestion in cross_by_local.get(int(object_id), []):
                    md = connection.execute(
                        """SELECT md_object_id, inventory_raw, title, date_text,
                                  object_url
                           FROM md_objects WHERE md_object_id=?""",
                        (str(suggestion["md_object_id"]),),
                    ).fetchone()
                    if md is None:
                        continue
                    md_item = dict(md)
                    md_item.update(
                        {
                            "method": suggestion["method"],
                            "basis": suggestion["basis"],
                            "confidence": suggestion["confidence"],
                        }
                    )
                    relationship_decision = latest.get(
                        ("md_object", str(suggestion["md_object_id"]))
                    )
                    if relationship_decision is not None:
                        try:
                            decision_basis = json.loads(
                                str(relationship_decision["basis"])
                            )
                            candidate_id = str(
                                decision_basis.get("candidate_id") or ""
                            )
                            prefix, separator, group_id = candidate_id.partition(
                                ":"
                            )
                            decided_group = (
                                connection.execute(
                                    """SELECT local_object_id FROM local_groups
                                       WHERE local_group_id=?""",
                                    (int(group_id),),
                                ).fetchone()
                                if prefix == "group"
                                and separator
                                and group_id.isdigit()
                                else None
                            )
                        except (json.JSONDecodeError, AttributeError):
                            decided_group = None
                        if (
                            decided_group is not None
                            and int(decided_group["local_object_id"])
                            == int(object_id)
                        ):
                            md_item["relationship_decision"] = str(
                                relationship_decision["decision"]
                            )
                            md_item["relationship_decided_utc"] = str(
                                relationship_decision["decided_utc"]
                            )
                    md_suggestions.append(md_item)
                item = dict(row)
                item.update(
                    {
                        "record_key": f"local:{object_id}",
                        "subject_type": "local_object",
                        "subject_id": object_id,
                        "category": "local_only",
                        "resources": [],
                        "md_suggestions": md_suggestions,
                        "candidates": self._group_candidates(
                            connection,
                            where="WHERE local.local_object_id=?",
                            value=int(object_id),
                        ),
                        "decision": latest.get(("local_object", object_id))
                        or linked_local_decisions.get(int(object_id)),
                    }
                )
                records.append(item)
        return records

    def record(self, record_key: str) -> dict[str, Any]:
        record = next(
            (item for item in self.records() if item["record_key"] == record_key),
            None,
        )
        if record is None:
            raise WfSenderWorkbenchError(f"unknown WF-Sender record: {record_key}")
        return record

    def evidence_roots(self) -> set[Path]:
        roots: set[Path] = set()
        for record in self.records():
            for candidate in record["candidates"]:
                for item in candidate.get("files") or []:
                    path = Path(str(item.get("source_path") or "")).expanduser()
                    for root in PROTECTED_ROOTS:
                        resolved_root = root.resolve()
                        try:
                            resolved_path = path.resolve()
                        except OSError:
                            continue
                        if resolved_path.is_relative_to(resolved_root):
                            roots.add(resolved_root)
                            break
        return roots

    def candidate(self, record_key: str, candidate_id: str) -> dict[str, Any]:
        record = self.record(record_key)
        candidate = next(
            (
                item
                for item in record["candidates"]
                if item["candidate_id"] == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise WfSenderWorkbenchError(
                f"candidate {candidate_id} is not bound to {record_key}"
            )
        return candidate

    @staticmethod
    def _safe_source(path_text: str) -> Path:
        path = Path(path_text).expanduser().resolve(strict=True)
        if not path.is_file() or not any(
            path != root.resolve() and path.is_relative_to(root.resolve())
            for root in PROTECTED_ROOTS
        ):
            raise WfSenderWorkbenchError(
                f"preview escaped protected evidence roots: {path}"
            )
        return path

    def candidate_media(
        self, record_key: str, candidate_id: str
    ) -> tuple[Path, str]:
        candidate = self.candidate(record_key, candidate_id)
        files = candidate.get("files") or []
        preferred = next(
            (item for item in files if item["extension"].casefold() == ".pdf"),
            None,
        ) or next(
            (
                item
                for item in files
                if item["extension"].casefold() in {".tif", ".tiff", ".jpg", ".jpeg", ".png"}
            ),
            None,
        )
        if not preferred:
            raise WfSenderWorkbenchError("candidate has no browser-previewable evidence")
        path = self._safe_source(str(preferred["source_path"]))
        return path, ("pdf" if path.suffix.casefold() == ".pdf" else "image")

    def md_pdf_url(self, record_key: str) -> str:
        record = self.record(record_key)
        if record["subject_type"] != "md_object":
            raise WfSenderWorkbenchError("local-only record has no museum-digital PDF")
        resource = next(
            (
                item
                for item in record["resources"]
                if item["resource_role"] == "public_pdf_reference"
            ),
            None,
        )
        if not resource:
            raise WfSenderWorkbenchError("museum-digital record has no public PDF")
        parsed = urllib.parse.urlsplit(str(resource["resource_url"]))
        if parsed.scheme != "https" or parsed.hostname not in MD_HOSTS:
            raise WfSenderWorkbenchError("museum-digital PDF URL is outside allowed hosts")
        return str(resource["resource_url"])

    def cached_md_pdf(self, record_key: str) -> Path:
        object_id = self._subject(record_key)[1]
        destination = self.cache_root / "museum-digital" / f"{object_id}.pdf"
        if destination.is_file() and destination.stat().st_size > 4:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(f".{os.getpid()}.part")
        request = urllib.request.Request(
            self.md_pdf_url(record_key),
            headers={"User-Agent": "ISS-Archive-Workbench/0.18"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                content_type = response.headers.get_content_type()
                announced = int(response.headers.get("Content-Length") or 0)
                if content_type != "application/pdf":
                    raise WfSenderWorkbenchError(
                        f"museum-digital resource is not PDF: {content_type}"
                    )
                if announced > MAX_MD_PDF_BYTES:
                    raise WfSenderWorkbenchError("museum-digital PDF exceeds 256 MB")
                total = 0
                with temporary.open("xb") as handle:
                    while block := response.read(1024 * 1024):
                        total += len(block)
                        if total > MAX_MD_PDF_BYTES:
                            raise WfSenderWorkbenchError(
                                "museum-digital PDF exceeds 256 MB"
                            )
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            with temporary.open("rb") as handle:
                signature = handle.read(5)
            if signature != b"%PDF-":
                raise WfSenderWorkbenchError("museum-digital response lacks PDF signature")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def image_preview(
        self, record_key: str, candidate_id: str, source: Path
    ) -> Path:
        digest = hashlib.sha256(
            f"{record_key}\0{candidate_id}\0{source}\0{source.stat().st_mtime_ns}".encode()
        ).hexdigest()
        destination = self.cache_root / "local-images" / f"{digest}.png"
        if destination.is_file():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".part.png")
        import subprocess

        subprocess.run(
            [
                "magick",
                f"{source}[0]",
                "-auto-orient",
                "-thumbnail",
                "1600x1600>",
                str(temporary),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        temporary.replace(destination)
        return destination

    def pdf_preview(self, source: Path, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(
            f"{namespace}\0{key}\0{source}\0{source.stat().st_mtime_ns}".encode()
        ).hexdigest()
        destination = self.cache_root / "pdf-images" / f"{digest}.png"
        if destination.is_file():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_base = destination.parent / f"{destination.stem}.{os.getpid()}.part"
        temporary = Path(str(temporary_base) + ".png")
        import subprocess

        try:
            subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-scale-to",
                    "1600",
                    "-singlefile",
                    "-png",
                    str(source),
                    str(temporary_base),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=90,
            )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def save_decision(
        self,
        *,
        record_key: str,
        actor: str,
        decision: str,
        candidate_id: str,
        note: str,
    ) -> None:
        record = self.record(record_key)
        if decision not in DECISIONS:
            raise WfSenderWorkbenchError(f"invalid decision: {decision}")
        if record["category"] == "local_only":
            allowed = {
                "retain_as_archive_candidate",
                "reject_as_duplicate_or_non_object",
                "needs_more_review",
            }
        elif record["category"] == "missing_local":
            allowed = {
                "accept_md_only_fallback",
                "needs_more_local_search",
                "needs_more_review",
            }
        else:
            allowed = {
                "confirm_local_candidate",
                "reject_local_candidates",
                "needs_more_review",
            }
        if decision not in allowed:
            raise WfSenderWorkbenchError(
                f"decision {decision} is not valid for {record['category']}"
            )
        selected_candidate: dict[str, Any] | None = None
        if decision in {"confirm_local_candidate", "retain_as_archive_candidate"}:
            selected_candidate = self.candidate(record_key, candidate_id)
        elif candidate_id:
            selected_candidate = self.candidate(record_key, candidate_id)
        basis = json.dumps(
            {
                "format": "iss-wf-sender-visual-decision-v1",
                "category": record["category"],
                "candidate_id": candidate_id,
                "local_object_id": (
                    selected_candidate.get("local_object_id")
                    if selected_candidate
                    else None
                ),
                "suggested_cross_match": bool(
                    selected_candidate
                    and selected_candidate.get("suggested_cross_match")
                ),
                "suggestion_basis": (
                    selected_candidate.get("suggestion_basis")
                    if selected_candidate
                    else ""
                ),
                "note": note.strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        subject_type, subject_id = self._subject(record_key)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO decisions (
                     decided_utc, actor, subject_type, subject_id, decision, basis
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (_utc_now(), actor, subject_type, subject_id, decision, basis),
            )
            connection.commit()
