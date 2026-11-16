#!/usr/bin/env python3
"""Album-level reconciliation workbench for WF-Alben.

One physical album is one archival object. A resolved object has one whole-album
PDF Master and one retained, ordered TIFF page set. All other visual candidates
become redundant. Source files are never changed, moved, or deleted.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse

from PIL import Image, ImageOps


VERSION = "2.0.0-album-model"
SOURCE_ROOT = Path("/media/vladimir/Elements")
EXPECTED_SOURCE = "/dev/sdf1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work")
PROJECT = WORK_ROOT / "wf-alben"
RECON = PROJECT / "work/reconciliation"
SOURCE_CANDIDATES = RECON / "candidate-files.csv"
SOURCE_DOCUMENTS = RECON / "document-candidates.csv"
SOURCE_COLLECTION = RECON / "collection-records.csv"

DECISIONS = RECON / "wf-alben-album-decisions.csv"
SOURCE_DISPOSITION = RECON / "wf-alben-album-source-disposition.csv"
DRAIN_LIST = RECON / "wf-alben-album-drain-list.csv"
DATABASE = RECON / "wf-alben-album-workbench.sqlite"
PREFLIGHT = RECON / "wf-alben-album-workbench-preflight.json"

ALBUMS = {
    "ALB-001": {
        "title": "Fotoalbum Labor, Konstruktions- und Versuchswerk Oberspree, 1946",
        "inventory_prefixes": ("ALB-001", "ALB_001"),
        "tiff_families": (
            {
                "label": "Named original page scans",
                "directory": SOURCE_ROOT / "restore/archive/SCANS-fertig/Fotoalben/Labor, Konstruktionsbüro, Versuchswerk Oberspree (nur kyrillisch",
                "pattern": re.compile(r"ALB-001-.*\.tiff?$", re.I),
                "eligible": True,
            },
        ),
    },
    "ALB-002": {
        "title": "Fotoalbum Produkte LKVO, 1946",
        "inventory_prefixes": ("ALB-002", "ALB_002"),
        "tiff_families": (
            {
                "label": "Named original page scans",
                "directory": SOURCE_ROOT / "restore/archive/SCANS-fertig/Fotoalben/Produkte 1946",
                "pattern": re.compile(r"ALB-002-.*\.tiff?$", re.I),
                "eligible": True,
            },
        ),
    },
    "ALB-003": {
        "title": "Fotoalbum Produktion im Werk für Fernmeldewesen, 1951",
        "inventory_prefixes": ("ALB-003", "ALB_003"),
        "tiff_families": (
            {
                "label": "Named album page scans",
                "directory": SOURCE_ROOT / "restore/archive/SCANS-fertig/Fotoalben/Die größten Objekte (russisch und deutsch)",
                "pattern": re.compile(r"ALB[-_]003-.*\.tiff?$", re.I),
                "eligible": True,
            },
            {
                "label": "Camera-import TIFF sequence",
                "directory": SOURCE_ROOT / "restore/archive/SCANS-fertig/Fotoalben/Die größten Objekte (russisch und deutsch)",
                "pattern": re.compile(r"IMG_20190920_\d+\.tiff?$", re.I),
                "eligible": True,
            },
        ),
    },
    "ALBUM-NEF": {
        "title": "Fotoalbum NEF, 1946",
        "inventory_prefixes": ("ALBUM_NEF",),
        "tiff_families": (
            {
                "label": "Ordered original page scans",
                "directory": SOURCE_ROOT / "restore/archive/WF/Geschichte-und-WF-Archiv/NEF",
                "pattern": re.compile(r"Album_NEF_Seite_\d+\.tiff?$", re.I),
                "eligible": True,
            },
        ),
    },
}

DECISION_FIELDS = (
    "carrier_key",
    "outcome",
    "selected_pdf_candidate_id",
    "selected_pdf_master_path",
    "retained_tiff_candidate_id",
    "retained_tiff_paths",
    "redundant_source_paths",
    "note",
    "actor",
    "decided_utc",
    "accession_state",
    "deletion_authorized",
)
DISPOSITION_FIELDS = (
    "source_path",
    "carrier_key",
    "source_kind",
    "candidate_role",
    "candidate_id",
    "disposition",
    "accession_relation",
    "drain_state",
    "size_bytes",
    "content_sha256",
    "evidence_basis",
    "deletion_authorized",
)


class WorkbenchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(64 * 1024 * 1024)
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_payload(rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(fields),
        extrasaction="ignore",
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        cooked = dict(row)
        for key, value in list(cooked.items()):
            if isinstance(value, (list, tuple, set)):
                cooked[key] = json.dumps(list(value), ensure_ascii=False)
            elif isinstance(value, bool):
                cooked[key] = "true" if value else "false"
        writer.writerow(cooked)
    return buffer.getvalue().encode("utf-8-sig")


def atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.resolve(strict=True).relative_to(WORK_ROOT.resolve(strict=True))
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o644)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def source_guard() -> str:
    result = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", "--target", str(SOURCE_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(maxsplit=3)
    if (
        len(fields) != 4
        or fields[0] != EXPECTED_SOURCE
        or fields[1] != str(SOURCE_ROOT)
        or fields[2] != "ntfs3"
        or "ro" not in fields[3].split(",")
    ):
        raise WorkbenchError(
            "protected source must be /dev/sdf1 mounted at "
            f"{SOURCE_ROOT} as ntfs3 read-only; observed: {result.stdout.strip()}"
        )
    return result.stdout.strip()


def carrier_for_inventory(inventory: str) -> str:
    normalized = inventory.strip().upper()
    for key, album in ALBUMS.items():
        if any(normalized.startswith(prefix) for prefix in album["inventory_prefixes"]):
            return key
    return ""


def natural_key(path: str | Path) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(path))]


def stable_id(role: str, paths: Iterable[str]) -> str:
    binding = role + "\0" + "\0".join(sorted(paths))
    return "CAND-" + hashlib.sha256(binding.encode("utf-8")).hexdigest()[:20].upper()


def human_size(value: int | str) -> str:
    amount = float(int(value or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return ""


def path_under_source(value: str) -> Path:
    path = Path(value).resolve(strict=True)
    path.relative_to(SOURCE_ROOT.resolve(strict=True))
    return path


def pdf_evidence(path: Path) -> tuple[bool, str]:
    with path.open("rb") as handle:
        magic = handle.read(5)
    if magic != b"%PDF-":
        return False, f"invalid PDF signature {magic.hex()}; file is not eligible"
    result = subprocess.run(
        ["pdfinfo", str(path)], capture_output=True, text=True, timeout=30
    )
    if result.returncode:
        return False, "pdfinfo rejected file: " + result.stderr.strip().replace("\n", " ")[:300]
    pages = ""
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            pages = line.split(":", 1)[1].strip()
    return True, f"valid PDF; {pages or 'unknown'} page(s)"


class AlbumWorkbench:
    def __init__(self, actor: str) -> None:
        self.actor = actor.strip() or "human-reviewer"
        self.lock = threading.Lock()
        self.mount = source_guard()
        self.image_rows = read_csv(SOURCE_CANDIDATES)
        self.document_rows = read_csv(SOURCE_DOCUMENTS)
        self.collection_rows = read_csv(SOURCE_COLLECTION)
        if not self.image_rows or not self.collection_rows:
            raise WorkbenchError("required existing WF-Alben projections are missing or empty")
        self.objects = self._objects()
        self.candidates = self._candidates()
        self._validate_candidates()
        self.refresh()

    def _objects(self) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
        for row in self.collection_rows:
            key = carrier_for_inventory(row["inventory"])
            if key:
                grouped[key].append(row)
        output: dict[str, dict[str, Any]] = {}
        for key, definition in ALBUMS.items():
            records = grouped[key]
            output[key] = {
                "carrier_key": key,
                "title": definition["title"],
                "md_records": records,
                "md_object_ids": sorted({row["md_object_id"] for row in records}),
                "inventories": sorted({row["inventory"] for row in records}, key=natural_key),
                "metadata_authority": "unit after accession; current museum-digital capture is comparison/projection evidence",
            }
        return output

    def _member(self, path: Path, source_kind: str, indexed: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
        stat = path.stat()
        source = str(path)
        row = (indexed or {}).get(source, {})
        return {
            "source_path": source,
            "filename": path.name,
            "source_kind": source_kind,
            "extension": path.suffix.casefold().lstrip("."),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "width_px": row.get("width_px", ""),
            "height_px": row.get("height_px", ""),
            "content_sha256": row.get("content_sha256", ""),
        }

    def _group(self, carrier: str, role: str, label: str, members: list[dict[str, Any]], eligible: bool, evidence: str) -> dict[str, Any]:
        paths = [row["source_path"] for row in members]
        return {
            "carrier_key": carrier,
            "candidate_id": stable_id(role, paths),
            "role": role,
            "label": label,
            "members": members,
            "paths": paths,
            "eligible": bool(eligible and members),
            "evidence": evidence,
            "size_bytes": sum(int(row["size_bytes"]) for row in members),
        }

    def _candidates(self) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = {key: [] for key in ALBUMS}
        indexed_images = {row["candidate_source_path"]: row for row in self.image_rows}

        # Whole-PDF evidence is drawn only from the reviewed document projection.
        for row in self.document_rows:
            if row["extension"].casefold() != ".pdf":
                continue
            source = path_under_source(row["source_path"])
            key = "ALBUM-NEF" if "album_nef" in source.name.casefold() else carrier_for_inventory(source.stem)
            if not key:
                continue
            valid, evidence = pdf_evidence(source)
            member = self._member(source, "document", {str(source): row})
            output[key].append(self._group(key, "whole_album_pdf", "Whole-album PDF candidate", [member], valid, evidence))

        # Directly inspect the bounded, approved TIFF source families.
        for key, definition in ALBUMS.items():
            for family in definition["tiff_families"]:
                directory = Path(family["directory"])
                paths = sorted(
                    (path for path in directory.iterdir() if path.is_file() and family["pattern"].fullmatch(path.name)),
                    key=natural_key,
                )
                members = [self._member(path, "image", indexed_images) for path in paths]
                sequence = ", ".join(path.name for path in paths[:3])
                if len(paths) > 3:
                    sequence += f", …, {paths[-1].name}"
                output[key].append(
                    self._group(
                        key,
                        "corresponding_tiff_page_set",
                        family["label"],
                        members,
                        family["eligible"],
                        f"{len(paths)} ordered TIFF file(s): {sequence}",
                    )
                )

        # Group every reviewed visual occurrence not already represented above.
        selected_paths = {
            path
            for groups in output.values()
            for group in groups
            for path in group["paths"]
        }
        grouped: dict[tuple[str, str, str], dict[str, dict[str, str]]] = collections.defaultdict(dict)
        for row in self.image_rows:
            key = carrier_for_inventory(row["inventory"])
            path = row["candidate_source_path"]
            if not key or path in selected_paths:
                continue
            grouped[(key, str(Path(path).parent), row["extension"].casefold())][path] = row
        for (key, directory, extension), rows in sorted(grouped.items()):
            members = [self._member(path_under_source(path), "image", rows) for path in sorted(rows, key=natural_key)]
            output[key].append(
                self._group(
                    key,
                    "alternate_or_derivative_visuals",
                    f"Other {extension.upper()} occurrences — {Path(directory).name}",
                    members,
                    False,
                    f"{len(members)} reviewed occurrence(s); redundant only after Master selection",
                )
            )
        return output

    def _validate_candidates(self) -> None:
        for key, groups in self.candidates.items():
            ids = [group["candidate_id"] for group in groups]
            if len(ids) != len(set(ids)):
                raise WorkbenchError(f"duplicate candidate group IDs for {key}")
            for group in groups:
                for source in group["paths"]:
                    path_under_source(source)

    def decisions(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in read_csv(DECISIONS):
            if row.get("carrier_key") not in ALBUMS:
                continue
            cooked: dict[str, Any] = dict(row)
            for field in ("retained_tiff_paths", "redundant_source_paths"):
                try:
                    cooked[field] = json.loads(row.get(field, "[]"))
                except json.JSONDecodeError as exc:
                    raise WorkbenchError(f"invalid JSON array in {DECISIONS.name}: {field}") from exc
            cooked["deletion_authorized"] = False
            result[row["carrier_key"]] = cooked
        return result

    def records(self) -> list[dict[str, Any]]:
        decisions = self.decisions()
        records = []
        for key in ALBUMS:
            row = dict(self.objects[key])
            row["candidates"] = self.candidates[key]
            row["decision"] = decisions.get(key)
            row["queue_state"] = "resolved" if row["decision"] and row["decision"]["outcome"] == "selected_master" else "unresolved"
            records.append(row)
        return records

    def save_decision(self, carrier: str, outcome: str, pdf_id: str, tiff_id: str, note: str) -> None:
        source_guard()
        if carrier not in ALBUMS:
            raise WorkbenchError("unknown carrier")
        if outcome not in {"selected_master", "unresolved"}:
            raise WorkbenchError("unsupported outcome")
        by_id = {item["candidate_id"]: item for item in self.candidates[carrier]}
        pdf = by_id.get(pdf_id)
        tiff = by_id.get(tiff_id)
        if outcome == "selected_master":
            if not pdf or pdf["role"] != "whole_album_pdf" or not pdf["eligible"]:
                raise WorkbenchError("select exactly one eligible whole-album PDF Master")
            if not tiff or tiff["role"] != "corresponding_tiff_page_set" or not tiff["eligible"]:
                raise WorkbenchError("select exactly one eligible corresponding TIFF page set")
        elif pdf_id or tiff_id:
            raise WorkbenchError("unresolved records cannot select files")

        selected_paths = set(pdf["paths"] if pdf else [])
        retained_paths = set(tiff["paths"] if tiff else [])
        redundant = sorted(
            {
                path
                for group in self.candidates[carrier]
                for path in group["paths"]
            }
            - selected_paths
            - retained_paths
        ) if outcome == "selected_master" else []
        if selected_paths & retained_paths or selected_paths & set(redundant) or retained_paths & set(redundant):
            raise WorkbenchError("selected, retained, and redundant path sets must be disjoint")

        row = {
            "carrier_key": carrier,
            "outcome": outcome,
            "selected_pdf_candidate_id": pdf_id if pdf else "",
            "selected_pdf_master_path": pdf["paths"][0] if pdf else "",
            "retained_tiff_candidate_id": tiff_id if tiff else "",
            "retained_tiff_paths": sorted(retained_paths, key=natural_key),
            "redundant_source_paths": redundant,
            "note": note.strip(),
            "actor": self.actor,
            "decided_utc": utc_now(),
            "accession_state": "not_accessioned",
            "deletion_authorized": False,
        }
        with self.lock:
            decisions = self.decisions()
            decisions[carrier] = row
            atomic_replace(DECISIONS, csv_payload([decisions[key] for key in ALBUMS if key in decisions], DECISION_FIELDS))
            self.refresh()

    def _all_source_rows(self) -> list[dict[str, Any]]:
        decisions = self.decisions()
        bindings: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for groups in self.candidates.values():
            for group in groups:
                for member in group["members"]:
                    bindings[member["source_path"]].append(group)

        image_index = {row["candidate_source_path"]: row for row in self.image_rows}
        paths = set(image_index) | set(bindings)
        rows: list[dict[str, Any]] = []
        for path in sorted(paths, key=natural_key):
            groups = bindings.get(path, [])
            carrier = groups[0]["carrier_key"] if groups else carrier_for_inventory(image_index[path]["inventory"])
            decision = decisions.get(carrier)
            ids = sorted({group["candidate_id"] for group in groups})
            roles = sorted({group["role"] for group in groups}) or ["reviewed_visual_occurrence"]
            if decision and decision["outcome"] == "selected_master":
                if path == decision["selected_pdf_master_path"]:
                    disposition, relation = "selected_pdf_master_source", "master_input"
                elif path in decision["retained_tiff_paths"]:
                    disposition, relation = "retained_original_tiff_page", "corresponding_tiff_page_input"
                else:
                    disposition, relation = "redundant_after_master_selection", "redundant_source_evidence"
                drain = "planned_deferred_drain_after_verified_accession"
            else:
                disposition, relation = "candidate_pending_reconciliation", "unresolved_source_evidence"
                drain = "blocked_until_master_selection_and_verified_accession"
            stat = path_under_source(path).stat()
            rows.append({
                "source_path": path,
                "carrier_key": carrier,
                "source_kind": "image" if Path(path).suffix.casefold() not in {".pdf"} else "document",
                "candidate_role": ";".join(roles),
                "candidate_id": ";".join(ids),
                "disposition": disposition,
                "accession_relation": relation,
                "drain_state": drain,
                "size_bytes": stat.st_size,
                "content_sha256": "",
                "evidence_basis": "reviewed WF-Alben visual projection and bounded carrier source family",
                "deletion_authorized": False,
            })

        # Documentary paths are always represented in source disposition. Only
        # album working tables are confirmed unit metadata and enter drain planning.
        existing = {row["source_path"] for row in rows}
        for doc in self.document_rows:
            path = doc["source_path"]
            if path in existing:
                continue
            confirmed = doc["document_role"] == "album_working_table"
            rows.append({
                "source_path": path,
                "carrier_key": "WF-ALBEN" if confirmed else "",
                "source_kind": "metadata" if confirmed else "context_document",
                "candidate_role": doc["document_role"],
                "candidate_id": doc["document_id"],
                "disposition": "unit_metadata_input" if confirmed else "excluded_context_not_confirmed_unit_file",
                "accession_relation": "metadata_provenance_input" if confirmed else "comparison_evidence_only",
                "drain_state": "blocked_until_all_four_albums_have_verified_accession" if confirmed else "excluded_from_drain_scope",
                "size_bytes": doc["size_bytes"],
                "content_sha256": doc["content_sha256"],
                "evidence_basis": doc["proposed_review_state"],
                "deletion_authorized": False,
            })
        return sorted(rows, key=lambda row: (row["carrier_key"], natural_key(row["source_path"])))

    def refresh(self) -> None:
        if not DECISIONS.is_file():
            atomic_replace(DECISIONS, csv_payload([], DECISION_FIELDS))
        rows = self._all_source_rows()
        atomic_replace(SOURCE_DISPOSITION, csv_payload(rows, DISPOSITION_FIELDS))
        drain_rows = [row for row in rows if row["drain_state"] != "excluded_from_drain_scope"]
        atomic_replace(DRAIN_LIST, csv_payload(drain_rows, DISPOSITION_FIELDS))
        self._write_database(rows)
        decisions = self.decisions()
        preflight = {
            "generated_utc": utc_now(),
            "version": VERSION,
            "source_mount": self.mount,
            "model": "one physical album object; one whole PDF Master; one retained ordered TIFF page set",
            "objects": len(ALBUMS),
            "resolved_objects": sum(row["outcome"] == "selected_master" for row in decisions.values()),
            "source_disposition_rows": len(rows),
            "drain_list_rows": len(drain_rows),
            "deletion_authorized_true": 0,
            "published_unit_modified": False,
        }
        atomic_replace(PREFLIGHT, (json.dumps(preflight, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def _write_database(self, source_rows: list[dict[str, Any]]) -> None:
        temporary = DATABASE.with_name(f".{DATABASE.name}.tmp-{os.getpid()}")
        if temporary.exists():
            temporary.unlink()
        con = sqlite3.connect(temporary)
        try:
            con.executescript("""
                PRAGMA journal_mode=DELETE;
                CREATE TABLE objects (
                    carrier_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    metadata_authority TEXT NOT NULL,
                    md_object_ids_json TEXT NOT NULL,
                    inventories_json TEXT NOT NULL
                );
                CREATE TABLE candidate_groups (
                    candidate_id TEXT PRIMARY KEY,
                    carrier_key TEXT NOT NULL REFERENCES objects(carrier_key),
                    role TEXT NOT NULL,
                    label TEXT NOT NULL,
                    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
                    evidence TEXT NOT NULL,
                    member_count INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL
                );
                CREATE TABLE candidate_members (
                    candidate_id TEXT NOT NULL REFERENCES candidate_groups(candidate_id),
                    sequence INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    PRIMARY KEY(candidate_id, sequence)
                );
                CREATE TABLE decisions (
                    carrier_key TEXT PRIMARY KEY REFERENCES objects(carrier_key),
                    outcome TEXT NOT NULL,
                    selected_pdf_candidate_id TEXT NOT NULL,
                    retained_tiff_candidate_id TEXT NOT NULL,
                    note TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    decided_utc TEXT NOT NULL,
                    accession_state TEXT NOT NULL,
                    deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
                );
                CREATE TABLE source_disposition (
                    source_path TEXT PRIMARY KEY,
                    carrier_key TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    candidate_role TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    accession_relation TEXT NOT NULL,
                    drain_state TEXT NOT NULL,
                    deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
                );
            """)
            for obj in self.objects.values():
                con.execute("INSERT INTO objects VALUES (?,?,?,?,?)", (
                    obj["carrier_key"], obj["title"], obj["metadata_authority"],
                    json.dumps(obj["md_object_ids"]), json.dumps(obj["inventories"], ensure_ascii=False),
                ))
            for groups in self.candidates.values():
                for group in groups:
                    con.execute("INSERT INTO candidate_groups VALUES (?,?,?,?,?,?,?,?)", (
                        group["candidate_id"], group["carrier_key"], group["role"], group["label"],
                        int(group["eligible"]), group["evidence"], len(group["members"]), group["size_bytes"],
                    ))
                    for sequence, member in enumerate(group["members"], 1):
                        con.execute("INSERT INTO candidate_members VALUES (?,?,?,?,?,?,?)", (
                            group["candidate_id"], sequence, member["source_path"], member["filename"],
                            member["extension"], member["size_bytes"], member["mtime_ns"],
                        ))
            for row in self.decisions().values():
                con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)", (
                    row["carrier_key"], row["outcome"], row["selected_pdf_candidate_id"],
                    row["retained_tiff_candidate_id"], row["note"], row["actor"], row["decided_utc"],
                    row["accession_state"], 0,
                ))
            for row in source_rows:
                con.execute("INSERT INTO source_disposition VALUES (?,?,?,?,?,?,?,0)", (
                    row["source_path"], row["carrier_key"], row["source_kind"], row["candidate_role"],
                    row["disposition"], row["accession_relation"], row["drain_state"],
                ))
            con.commit()
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise WorkbenchError("generated SQLite integrity check failed")
        finally:
            con.close()
        os.chmod(temporary, 0o644)
        os.replace(temporary, DATABASE)

    def check(self) -> dict[str, Any]:
        mount = source_guard()
        decisions = self.decisions()
        rows = read_csv(SOURCE_DISPOSITION)
        drains = read_csv(DRAIN_LIST)
        if any(row.get("deletion_authorized", "").casefold() != "false" for row in rows + drains):
            raise WorkbenchError("deletion authorization must remain false")
        for carrier, decision in decisions.items():
            if decision["outcome"] != "selected_master":
                continue
            selected = {decision["selected_pdf_master_path"]}
            retained = set(decision["retained_tiff_paths"])
            redundant = set(decision["redundant_source_paths"])
            if selected & retained or selected & redundant or retained & redundant:
                raise WorkbenchError(f"decision path sets overlap for {carrier}")
        con = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            object_count = con.execute("SELECT count(*) FROM objects").fetchone()[0]
        finally:
            con.close()
        if integrity != "ok" or object_count != 4:
            raise WorkbenchError("derived SQLite validation failed")
        return {
            "ok": True,
            "version": VERSION,
            "source_mount": mount,
            "objects": 4,
            "resolved": sum(row["outcome"] == "selected_master" for row in decisions.values()),
            "source_disposition_rows": len(rows),
            "drain_list_rows": len(drains),
            "deletion_authorized_true": 0,
            "sqlite_integrity": integrity,
        }

    def allowed_preview_paths(self) -> set[str]:
        return {member["source_path"] for groups in self.candidates.values() for group in groups for member in group["members"]}


STYLE = """
body{font-family:system-ui,sans-serif;background:#f4f1ea;color:#20201d;margin:0}main{max-width:1180px;margin:auto;padding:24px}
h1{margin:.1em 0}.sub{color:#5f5b52}.notice{background:#fff1c7;border-left:5px solid #b56a00;padding:12px;margin:16px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}.card{background:#fff;border:1px solid #d2ccc0;border-radius:10px;padding:16px}
.resolved{border-color:#39804c}.missing{color:#9b2d20;font-weight:700}.ok{color:#28713e;font-weight:700}.muted{color:#716c63;font-size:.9em}
.candidate{border-top:1px solid #ddd5c8;padding:12px 0}.thumbs{display:flex;gap:6px;overflow:auto}.thumbs img{height:120px;max-width:180px;object-fit:contain;background:#222}
label{display:block;margin:8px 0}textarea{width:100%;min-height:58px}button{background:#204e3a;color:white;border:0;border-radius:6px;padding:9px 13px;cursor:pointer}
button.secondary{background:#665f54}code{overflow-wrap:anywhere}details{margin-top:7px}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}
"""


def page(workbench: AlbumWorkbench, message: str = "", error: str = "") -> bytes:
    cards = []
    for record in workbench.records():
        pdfs = [item for item in record["candidates"] if item["role"] == "whole_album_pdf"]
        tiffs = [item for item in record["candidates"] if item["role"] == "corresponding_tiff_page_set"]
        other = [item for item in record["candidates"] if item["role"] == "alternate_or_derivative_visuals"]
        decision = record["decision"]
        parts = [f"<article class='card {'resolved' if record['queue_state']=='resolved' else ''}'>"]
        parts.append(f"<h2>{esc(record['carrier_key'])}</h2><p>{esc(record['title'])}</p>")
        parts.append(f"<p class='muted'>{len(record['md_records'])} museum-digital record(s), comparison/projection only</p>")
        if decision and decision["outcome"] == "selected_master":
            parts.append(f"<p class='ok'>Resolved — PDF Master and TIFF set explicitly selected</p><p><code>{esc(decision['selected_pdf_master_path'])}</code></p>")
        else:
            eligible_pdfs = sum(item["eligible"] for item in pdfs)
            parts.append(f"<p class='{'ok' if eligible_pdfs else 'missing'}'>{eligible_pdfs} eligible PDF Master candidate(s)</p>")
        parts.append(f"<form method='post' action='/decision'><input type='hidden' name='carrier' value='{esc(record['carrier_key'])}'><input type='hidden' name='outcome' value='selected_master'>")
        parts.append("<h3>1. Whole-album PDF Master</h3>")
        if not pdfs:
            parts.append("<p class='missing'>No whole-album PDF candidate discovered. This object must remain unresolved.</p>")
        for item in pdfs:
            disabled = "" if item["eligible"] else " disabled"
            parts.append(f"<div class='candidate'><label><input type='radio' name='pdf_id' value='{item['candidate_id']}'{disabled}> {esc(item['label'])} — {human_size(item['size_bytes'])}</label><p class='{'ok' if item['eligible'] else 'missing'}'>{esc(item['evidence'])}</p><code>{esc(item['paths'][0])}</code></div>")
        parts.append("<h3>2. Corresponding original TIFF page set</h3>")
        for item in tiffs:
            thumbs = "".join(f"<img loading='lazy' src='/preview?path={quote(member['source_path'])}' alt='{esc(member['filename'])}'>" for member in item["members"][:3])
            parts.append(f"<div class='candidate'><label><input type='radio' name='tiff_id' value='{item['candidate_id']}'> {esc(item['label'])} — {len(item['members'])} files, {human_size(item['size_bytes'])}</label><p>{esc(item['evidence'])}</p><div class='thumbs'>{thumbs}</div><details><summary>Show ordered paths</summary><ol>{''.join(f'<li><code>{esc(p)}</code></li>' for p in item['paths'])}</ol></details></div>")
        parts.append(f"<p class='muted'>{sum(len(item['members']) for item in other)} other reviewed visual occurrence(s) will be Redundant only after selection. No source file is deleted.</p>")
        parts.append("<label>Decision note<textarea name='note'></textarea></label><button type='submit'>Save PDF Master + TIFF set</button></form>")
        parts.append(f"<form method='post' action='/decision'><input type='hidden' name='carrier' value='{esc(record['carrier_key'])}'><input type='hidden' name='outcome' value='unresolved'><button class='secondary' type='submit'>Leave unresolved</button></form>")
        parts.append("</article>")
        cards.append("".join(parts))
    status = f"<p class='ok'>{esc(message)}</p>" if message else ""
    if error:
        status += f"<p class='missing'>{esc(error)}</p>"
    body = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>WF-Alben album workbench</title><style>{STYLE}</style></head><body><main>
    <h1>WF-Alben — album reconciliation</h1><p class='sub'>One physical album object · one whole PDF Master · one retained ordered TIFF page set</p>
    <div class='notice'><strong>No deletion is authorized.</strong> Drain rows are planning records only and stay blocked until verified accession. Museum-digital is comparison/projection evidence; the accessioned unit will become authority.</div>
    {status}<div class='grid'>{''.join(cards)}</div>
    <p class='muted'>Version {VERSION}. Source guard: {esc(workbench.mount)}</p></main></body></html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    workbench: AlbumWorkbench

    def send_bytes(self, payload: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(page(self.workbench), "text/html; charset=utf-8")
            return
        if parsed.path == "/health":
            try:
                payload = self.workbench.check()
                self.send_bytes((json.dumps(payload, indent=2) + "\n").encode(), "application/json")
            except Exception as exc:
                self.send_bytes((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode(), "application/json", HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path == "/preview":
            try:
                source = unquote(parse_qs(parsed.query).get("path", [""])[0])
                if source not in self.workbench.allowed_preview_paths():
                    raise WorkbenchError("path is not a workbench candidate")
                path = path_under_source(source)
                if path.suffix.casefold() == ".pdf":
                    result = subprocess.run(["pdftoppm", "-f", "1", "-singlefile", "-scale-to", "1000", "-jpeg", str(path), "-"], capture_output=True, timeout=45)
                    if result.returncode or not result.stdout:
                        raise WorkbenchError("PDF preview unavailable")
                    payload = result.stdout
                else:
                    with Image.open(path) as image:
                        image = ImageOps.exif_transpose(image)
                        image.thumbnail((1000, 1000))
                        if image.mode != "RGB":
                            image = image.convert("RGB")
                        buffer = io.BytesIO()
                        image.save(buffer, "JPEG", quality=82)
                        payload = buffer.getvalue()
                self.send_bytes(payload, "image/jpeg")
            except Exception as exc:
                self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/decision":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 128 * 1024:
                raise WorkbenchError("request too large")
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            carrier = form.get("carrier", [""])[0]
            self.workbench.save_decision(
                carrier,
                form.get("outcome", [""])[0],
                form.get("pdf_id", [""])[0],
                form.get("tiff_id", [""])[0],
                form.get("note", [""])[0],
            )
            payload = page(self.workbench, message=f"Saved {carrier}")
        except Exception as exc:
            payload = page(self.workbench, error=str(exc))
        self.send_bytes(payload, "text/html; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", default=os.environ.get("USER", "human-reviewer"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    try:
        workbench = AlbumWorkbench(args.actor)
        if args.check or args.prepare:
            print(json.dumps(workbench.check(), ensure_ascii=False, indent=2))
            return 0
        Handler.workbench = workbench
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print(f"WF-Alben album workbench: http://{args.host}:{args.port}/", flush=True)
        server.serve_forever()
        return 0
    except (WorkbenchError, OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
