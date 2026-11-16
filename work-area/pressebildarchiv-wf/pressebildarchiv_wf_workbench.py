#!/usr/bin/env python3
"""Small side-by-side Master reconciliation workbench for MD collection 555.

The workbench records human decisions and rebuilds CSV projections.  It never
copies, modifies, renames, drains, deletes, embeds metadata, or accessions a
file.  Every drain row is non-authorizing (`deletion_authorized=false`).
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import html
import io
import json
import os
import shutil
import subprocess
import threading
import uuid
import webbrowser
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from PIL import Image, ImageOps


VERSION = "1.1.0-post-accession-drain"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/pressebildarchiv-wf")
INIT_ROOT = WORK_ROOT / "work/accession/collection-555-init-20260829"
OBJECTS_CSV = INIT_ROOT / "accession-objects.csv"
CANDIDATES_CSV = INIT_ROOT / "master-candidates.csv"
RESOURCES_CSV = INIT_ROOT / "related-md-resources.csv"
INITIAL_DECISIONS_CSV = INIT_ROOT / "master-decisions-initial.csv"
DECISIONS_CSV = INIT_ROOT / "master-decisions.csv"
DECISION_LOG_CSV = INIT_ROOT / "decision-log.csv"
CANDIDATE_DISPOSITIONS_CSV = INIT_ROOT / "candidate-dispositions.csv"
SOURCE_DISPOSITION_CSV = INIT_ROOT / "source-disposition.csv"
DRAIN_LIST_CSV = INIT_ROOT / "drain-list.csv"
LOCK_FILE = INIT_ROOT / ".workbench.lock"

DECISION_LOG_FIELDS = [
    "decision_id",
    "saved_at",
    "actor",
    "md_object_id",
    "selected_candidate_id",
    "rejected_candidate_ids",
    "noise_candidate_ids",
    "note",
    "deletion_authorized",
]
DECISION_FIELDS = [
    "md_object_id",
    "inventory",
    "decision_state",
    "selected_candidate_id",
    "decision_note",
    "actor",
    "deletion_authorized",
]
DISPOSITIONS = {"pending", "selected_master", "redundant", "rejected", "noise"}


class WorkbenchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def atomic_write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def require_read_only_source() -> str:
    result = subprocess.run(
        [
            "findmnt", "-S", "/dev/sdf1", "-n", "-o",
            "SOURCE,FSTYPE,OPTIONS,TARGET",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    record = result.stdout.strip()
    fields = record.split()
    if len(fields) != 4:
        raise WorkbenchError(f"unexpected Elements mount record: {record!r}")
    source, filesystem, options, target = fields
    if (
        source != "/dev/sdf1"
        or filesystem != "ntfs3"
        or target != "/media/vladimir/Elements"
        or "ro" not in options.split(",")
    ):
        raise WorkbenchError(f"protected Elements mount guard failed: {record}")
    return record


def decide_dispositions(
    candidate_ids: set[str],
    selected_candidate_id: str,
    rejected: set[str],
    noise: set[str],
) -> dict[str, str]:
    """Apply the one-Master rule without overwriting explicit wrong/noise states."""
    if selected_candidate_id and selected_candidate_id not in candidate_ids:
        raise WorkbenchError("selected Master is not a candidate for this object")
    if not rejected.issubset(candidate_ids) or not noise.issubset(candidate_ids):
        raise WorkbenchError("rejected/noise selection contains a foreign candidate")
    if rejected & noise:
        raise WorkbenchError("a candidate cannot be both rejected and noise")
    if selected_candidate_id in rejected | noise:
        raise WorkbenchError("the selected Master cannot be rejected or noise")

    dispositions: dict[str, str] = {}
    for candidate_id in candidate_ids:
        if candidate_id == selected_candidate_id:
            dispositions[candidate_id] = "selected_master"
        elif candidate_id in rejected:
            dispositions[candidate_id] = "rejected"
        elif candidate_id in noise:
            dispositions[candidate_id] = "noise"
        elif selected_candidate_id:
            dispositions[candidate_id] = "redundant"
        else:
            dispositions[candidate_id] = "pending"
    return dispositions


def drain_classification(path: str) -> tuple[str, str]:
    if path.startswith("/media/vladimir/Elements/restore/archive/"):
        return "protected_source_archive", "redundant_source_candidate"
    if path.startswith("/mnt/volume/iss-archive/units/"):
        return "published_archive", "blocked_published_master_predecessor_review"
    if path.startswith("/mnt/volume/iss-archive-work/"):
        return "work_reference", "redundant_work_reference_review"
    return "other", "blocked_unknown_root"


class Workbench:
    def __init__(self, actor: str) -> None:
        self.actor = " ".join(actor.split())
        if not self.actor:
            raise WorkbenchError("actor is required")
        self._thread_lock = threading.Lock()
        self.objects = read_csv(OBJECTS_CSV)
        self.candidates = read_csv(CANDIDATES_CSV)
        self.resources = read_csv(RESOURCES_CSV)
        self.objects_by_md = {row["md_object_id"]: row for row in self.objects}
        self.candidates_by_id = {row["candidate_id"]: row for row in self.candidates}
        self.candidates_by_md: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.candidates:
            self.candidates_by_md[row["md_object_id"]].append(row)
        self._validate_static_inputs()
        self._ensure_state_files()
        self.refresh_projections()

    def _validate_static_inputs(self) -> None:
        if len(self.objects) != 48 or len(self.objects_by_md) != 48:
            raise WorkbenchError("workbench requires exactly 48 unique objects")
        if len(self.candidates) != 75 or len(self.candidates_by_id) != 75:
            raise WorkbenchError("workbench requires exactly 75 unique candidates")
        if len(self.resources) != 61:
            raise WorkbenchError("workbench requires all 61 MD resources")
        if set(self.objects_by_md) != set(self.candidates_by_md):
            raise WorkbenchError("every object must have at least one candidate")
        for candidate in self.candidates:
            if not Path(candidate["candidate_path"]).is_file():
                raise WorkbenchError(
                    f"candidate path is missing: {candidate['candidate_path']}"
                )
            if candidate["deletion_authorized"] != "false":
                raise WorkbenchError("candidate carries deletion authority")

    def _ensure_state_files(self) -> None:
        INIT_ROOT.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if not INITIAL_DECISIONS_CSV.exists():
                shutil.copyfile(DECISIONS_CSV, INITIAL_DECISIONS_CSV)
            if not DECISION_LOG_CSV.exists():
                atomic_write_csv(DECISION_LOG_CSV, DECISION_LOG_FIELDS, [])

    @contextmanager
    def _locked(self):
        self._thread_lock.acquire()
        handle = LOCK_FILE.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            self._thread_lock.release()

    def initial_decisions(self) -> dict[str, dict[str, str]]:
        return {row["md_object_id"]: row for row in read_csv(INITIAL_DECISIONS_CSV)}

    def decision_log(self) -> list[dict[str, str]]:
        return read_csv(DECISION_LOG_CSV)

    def latest_decisions(self) -> dict[str, dict[str, str]]:
        initial = self.initial_decisions()
        result: dict[str, dict[str, str]] = {}
        for md_id, row in initial.items():
            result[md_id] = {
                "decision_id": "initial",
                "saved_at": "",
                "actor": row["actor"],
                "md_object_id": md_id,
                "selected_candidate_id": row["selected_candidate_id"],
                "rejected_candidate_ids": "",
                "noise_candidate_ids": "",
                "note": row["decision_note"],
                "deletion_authorized": "false",
            }
        for row in self.decision_log():
            result[row["md_object_id"]] = row
        return result

    def current_dispositions(self) -> dict[str, dict[str, str]]:
        latest = self.latest_decisions()
        result: dict[str, dict[str, str]] = {}
        for md_id, candidates in self.candidates_by_md.items():
            decision = latest[md_id]
            rejected = set(filter(None, decision["rejected_candidate_ids"].split(";")))
            noise = set(filter(None, decision["noise_candidate_ids"].split(";")))
            dispositions = decide_dispositions(
                {row["candidate_id"] for row in candidates},
                decision["selected_candidate_id"],
                rejected,
                noise,
            )
            for candidate in candidates:
                result[candidate["candidate_id"]] = {
                    **candidate,
                    "disposition": dispositions[candidate["candidate_id"]],
                    "selected_master_candidate_id": decision["selected_candidate_id"],
                    "decision_id": decision["decision_id"],
                    "decision_actor": decision["actor"],
                    "decision_saved_at": decision["saved_at"],
                    "decision_note": decision["note"],
                }
        return result

    def refresh_projections(self) -> dict[str, int]:
        dispositions = self.current_dispositions()
        latest = self.latest_decisions()
        decision_rows: list[dict[str, str]] = []
        source_rows: list[dict[str, str]] = []

        for object_row in self.objects:
            md_id = object_row["md_object_id"]
            decision = latest[md_id]
            selected = decision["selected_candidate_id"]
            decision_rows.append(
                {
                    "md_object_id": md_id,
                    "inventory": object_row["inventory"],
                    "decision_state": (
                        "selected_master" if selected else "pending_human_selection"
                    ),
                    "selected_candidate_id": selected,
                    "decision_note": decision["note"],
                    "actor": decision["actor"],
                    "deletion_authorized": "false",
                }
            )

        for candidate in sorted(
            dispositions.values(),
            key=lambda row: (int(row["md_object_id"]), row["candidate_id"]),
        ):
            selected_id = candidate["selected_master_candidate_id"]
            selected_path = (
                self.candidates_by_id[selected_id]["candidate_path"] if selected_id else ""
            )
            source_rows.append(
                {
                    "md_object_id": candidate["md_object_id"],
                    "inventory": candidate["inventory"],
                    "candidate_id": candidate["candidate_id"],
                    "candidate_path": candidate["candidate_path"],
                    "candidate_source": candidate["candidate_source"],
                    "candidate_role": candidate["candidate_role"],
                    "disposition": candidate["disposition"],
                    "selected_master_candidate_id": selected_id,
                    "selected_master_path": selected_path,
                    "decision_note": candidate["decision_note"],
                    "deletion_authorized": "false",
                }
            )

        atomic_write_csv(DECISIONS_CSV, DECISION_FIELDS, decision_rows)
        atomic_write_csv(
            CANDIDATE_DISPOSITIONS_CSV,
            list(next(iter(dispositions.values()))),
            list(dispositions.values()),
        )
        atomic_write_csv(SOURCE_DISPOSITION_CSV, list(source_rows[0]), source_rows)
        drain_rows = read_csv(DRAIN_LIST_CSV) if DRAIN_LIST_CSV.exists() else []
        return {
            "objects": len(decision_rows),
            "resolved": sum(bool(row["selected_candidate_id"]) for row in decision_rows),
            "unresolved": sum(not row["selected_candidate_id"] for row in decision_rows),
            "drain_rows": len(drain_rows),
        }

    def save(
        self,
        md_object_id: str,
        selected_candidate_id: str,
        rejected: set[str],
        noise: set[str],
        note: str,
    ) -> dict[str, int]:
        require_read_only_source()
        if md_object_id not in self.objects_by_md:
            raise WorkbenchError("unknown MD object")
        candidate_ids = {
            row["candidate_id"] for row in self.candidates_by_md[md_object_id]
        }
        decide_dispositions(candidate_ids, selected_candidate_id, rejected, noise)
        note = " ".join(note.split())[:500]
        row = {
            "decision_id": str(uuid.uuid4()),
            "saved_at": utc_now(),
            "actor": self.actor,
            "md_object_id": md_object_id,
            "selected_candidate_id": selected_candidate_id,
            "rejected_candidate_ids": ";".join(sorted(rejected)),
            "noise_candidate_ids": ";".join(sorted(noise)),
            "note": note,
            "deletion_authorized": "false",
        }
        with self._locked():
            with DECISION_LOG_CSV.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=DECISION_LOG_FIELDS)
                writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
            return self.refresh_projections()

    def check(self) -> dict[str, object]:
        mount = require_read_only_source()
        dispositions = self.current_dispositions()
        decisions = read_csv(DECISIONS_CSV)
        drains = read_csv(DRAIN_LIST_CSV)
        errors: list[str] = []
        by_md: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in dispositions.values():
            by_md[row["md_object_id"]].append(row)
            if row["disposition"] not in DISPOSITIONS:
                errors.append(f"invalid disposition {row['candidate_id']}")
            if row["deletion_authorized"] != "false":
                errors.append(f"candidate deletion authority {row['candidate_id']}")
        for md_id, rows in by_md.items():
            selected = [row for row in rows if row["disposition"] == "selected_master"]
            if len(selected) > 1:
                errors.append(f"multiple Masters for MD {md_id}")
            if selected and any(row["disposition"] == "pending" for row in rows):
                errors.append(f"resolved MD {md_id} still has pending candidates")
        drain_paths = [row.get("source_path", "") for row in drains]
        if len(drain_paths) != len(set(drain_paths)):
            errors.append("drain list contains duplicate source paths")
        for row in drains:
            source_path = row.get("source_path", "")
            if row.get("deletion_authorized") != "false":
                errors.append(f"drain deletion authority {source_path}")
            if row.get("covered_by_unit") != "true":
                errors.append(f"drain source is not unit-covered {source_path}")
            if row.get("drain_state") != "blocked_until_validated_accession":
                errors.append(f"drain source is not accession-blocked {source_path}")
            if source_path.startswith("/mnt/volume/iss-archive/units/"):
                errors.append(f"published archive path in drain {source_path}")
            if source_path and not Path(source_path).is_file():
                errors.append(f"drain source is missing {source_path}")

        expected_paths = {
            row["candidate_path"]
            for row in self.candidates
            if row["candidate_path"].startswith("/media/vladimir/Elements/")
        }
        expected_paths.update(
            row["candidate_path"]
            for row in dispositions.values()
            if row["disposition"] == "selected_master"
            and row["candidate_path"].startswith("/mnt/volume/iss-archive-work/")
        )
        audit_path = WORK_ROOT / "work/usb-master-search/runs/20260803T101000Z-pressebildarchiv-wf-usb-master-search-corrected/candidates.csv"
        expected_paths.update(
            row["candidate_path"]
            for row in read_csv(audit_path)
            if row["candidate_kind"] == "whole_usb_filename" and row["candidate_path"]
        )
        document_path = INIT_ROOT / "document-source-disposition.csv"
        expected_paths.update(
            row["source_path"]
            for row in read_csv(document_path)
            if row["disposition"].startswith("retain_project_document")
        )
        missing_covered_paths = expected_paths - set(drain_paths)
        if missing_covered_paths:
            errors.append(
                f"unit-covered sources missing from drain: {len(missing_covered_paths)}"
            )
        if errors:
            raise WorkbenchError("; ".join(errors))
        state_counts = Counter(row["disposition"] for row in dispositions.values())
        return {
            "result": "ok",
            "version": VERSION,
            "mount": mount,
            "objects": len(self.objects),
            "candidates": len(self.candidates),
            "md_resources": len(self.resources),
            "resolved_objects": sum(bool(row["selected_candidate_id"]) for row in decisions),
            "unresolved_objects": sum(not row["selected_candidate_id"] for row in decisions),
            "candidate_dispositions": dict(sorted(state_counts.items())),
            "drain_rows": len(drains),
            "deletion_authorized_rows": sum(
                row.get("deletion_authorized") != "false" for row in drains
            ),
        }

    def ordered_objects(self) -> list[dict[str, str]]:
        return sorted(self.objects, key=lambda row: int(row["md_object_id"]))

    def next_unresolved(self, after_md: str = "") -> str:
        selected_by_md = {
            row["md_object_id"]: row["selected_candidate_id"]
            for row in read_csv(DECISIONS_CSV)
        }
        objects = self.ordered_objects()
        start = 0
        if after_md:
            for index, row in enumerate(objects):
                if row["md_object_id"] == after_md:
                    start = index + 1
                    break
        ordered = objects[start:] + objects[:start]
        for row in ordered:
            if not selected_by_md[row["md_object_id"]]:
                return row["md_object_id"]
        return ""


def human_size(value: str) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def page_shell(title: str, body: str) -> bytes:
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--paper:#f7f3ea;--ink:#1d2528;--muted:#657174;--line:#c9c4b8;--blue:#225f75;--red:#96382d;--noise:#735b2d;--green:#2e6950}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.4 system-ui,sans-serif}}
header{{padding:14px 22px;background:#fff;border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:center;justify-content:space-between}}
header a{{color:var(--blue)}}main{{padding:20px;max-width:1800px;margin:auto}}h1{{font-size:1.45rem;margin:.2rem 0}}h2{{font-size:1.08rem;margin:.25rem 0}}.muted{{color:var(--muted)}}
.layout{{display:grid;grid-template-columns:minmax(340px,45%) 1fr;gap:20px;align-items:start}}.panel{{background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px}}
.candidate-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}}.card{{border:2px solid var(--line);border-radius:7px;padding:10px;background:#fff}}.card.master{{border-color:var(--green)}}.card.redundant{{border-color:#9a947f}}.card.rejected{{border-color:var(--red)}}.card.noise{{border-color:var(--noise)}}
.preview{{display:block;width:100%;height:min(48vh,520px);object-fit:contain;background:#171717;border-radius:4px}}.candidate-grid .preview{{height:300px}}.path{{overflow-wrap:anywhere;font:12px/1.35 ui-monospace,monospace;color:#4e595c}}
.controls{{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}}label.choice{{padding:7px 9px;border:1px solid var(--line);border-radius:5px;cursor:pointer}}.status{{display:inline-block;padding:3px 7px;border-radius:4px;background:#eee;font-size:.8rem}}
textarea{{width:100%;min-height:62px;padding:8px}}button,.button{{display:inline-block;border:0;border-radius:5px;padding:10px 15px;color:#fff;background:var(--blue);cursor:pointer;text-decoration:none}}button.secondary,.button.secondary{{background:#687478}}.savebar{{position:sticky;bottom:0;margin-top:18px;padding:12px;background:#fff;border:1px solid var(--line);border-radius:7px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
@media(max-width:900px){{.layout{{grid-template-columns:1fr}}.preview{{height:420px}}}}
</style></head><body>{body}</body></html>"""
    return document.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server: "WorkbenchServer"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {format % args}")

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            try:
                body = json.dumps(self.server.workbench.check(), indent=2).encode()
                self.send_bytes(body, "application/json; charset=utf-8")
            except Exception as error:
                self.send_bytes(
                    json.dumps({"result": "error", "error": str(error)}).encode(),
                    "application/json; charset=utf-8",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return
        if parsed.path == "/preview":
            self.preview(parse_qs(parsed.query).get("candidate_id", [""])[0])
            return
        if parsed.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(parsed.query)
        md_id = query.get("md", [""])[0]
        if not md_id:
            md_id = self.server.workbench.next_unresolved()
        self.index(md_id)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/save":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                raise WorkbenchError("form is too large")
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            md_id = fields.get("md_object_id", [""])[0]
            selected = fields.get("master_candidate_id", [""])[0]
            rejected = set(fields.get("rejected", []))
            noise = set(fields.get("noise", []))
            note = fields.get("note", [""])[0]
            self.server.workbench.save(md_id, selected, rejected, noise, note)
            if fields.get("after", ["next"])[0] == "stay":
                target = md_id
            else:
                target = self.server.workbench.next_unresolved(md_id) or md_id
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/?" + urlencode({"md": target, "saved": "1"}))
            self.end_headers()
        except Exception as error:
            self.send_bytes(
                page_shell("Decision not saved", f"<main><h1>Decision not saved</h1><p>{html.escape(str(error))}</p><p><a href='/'>Return</a></p></main>"),
                "text/html; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )

    def preview(self, candidate_id: str) -> None:
        candidate = self.server.workbench.candidates_by_id.get(candidate_id)
        if not candidate:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            path = Path(candidate["candidate_path"])
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((1600, 1400), Image.Resampling.LANCZOS)
                if image.mode in {"RGBA", "LA"}:
                    background = Image.new("RGB", image.size, "white")
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                output = io.BytesIO()
                image.save(output, "JPEG", quality=86, optimize=True)
            self.send_bytes(output.getvalue(), "image/jpeg")
        except Exception as error:
            self.send_bytes(str(error).encode(), "text/plain; charset=utf-8", 415)

    def candidate_card(self, candidate: dict[str, str], disposition: str, left: bool = False) -> str:
        cid = candidate["candidate_id"]
        escaped_id = html.escape(cid, quote=True)
        classes = ["card"]
        if disposition == "selected_master":
            classes.append("master")
        elif disposition in {"redundant", "rejected", "noise"}:
            classes.append(disposition)
        checked_master = " checked" if disposition == "selected_master" else ""
        checked_reject = " checked" if disposition == "rejected" else ""
        checked_noise = " checked" if disposition == "noise" else ""
        label = {
            "selected_master": "Selected Master",
            "redundant": "Redundant / drain projection",
            "rejected": "Rejected candidate",
            "noise": "Ignored as noise",
            "pending": "Pending",
        }[disposition]
        role = html.escape(candidate["candidate_role"])
        evidence = html.escape(candidate["evidence_state"])
        return f"""
<article class="{' '.join(classes)}" data-candidate="{escaped_id}">
  <img class="preview" src="/preview?{urlencode({'candidate_id': cid})}" alt="Candidate preview">
  <h2>{'MD main reference' if left else html.escape(Path(candidate['candidate_path']).name)}</h2>
  <span class="status">{html.escape(label)}</span>
  <p><strong>{role}</strong><br>{evidence}<br>
  {html.escape(candidate['width_px'])} × {html.escape(candidate['height_px'])}, {human_size(candidate['size_bytes'])}</p>
  <p class="path">{html.escape(candidate['candidate_path'])}</p>
  <p class="muted">{html.escape(candidate['provenance_note'])}</p>
  <div class="controls">
    <label class="choice"><input type="radio" name="master_candidate_id" value="{escaped_id}"{checked_master}> Select Master</label>
    <label class="choice"><input type="checkbox" name="rejected" value="{escaped_id}"{checked_reject}> Reject</label>
    <label class="choice"><input type="checkbox" name="noise" value="{escaped_id}"{checked_noise}> Ignore noise</label>
  </div>
</article>"""

    def index(self, md_id: str) -> None:
        wb = self.server.workbench
        if not md_id:
            check = wb.check()
            body = f"<header><strong>Collection 555 reconciliation</strong></header><main><h1>All objects have a selected Master</h1><pre>{html.escape(json.dumps(check, indent=2))}</pre></main>"
            self.send_bytes(page_shell("Reconciliation complete", body), "text/html; charset=utf-8")
            return
        if md_id not in wb.objects_by_md:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        obj = wb.objects_by_md[md_id]
        dispositions = wb.current_dispositions()
        candidates = wb.candidates_by_md[md_id]
        md_candidates = [
            row for row in candidates
            if row["candidate_source"] == "museum-digital retained main image"
        ]
        other_candidates = [row for row in candidates if row not in md_candidates]
        if len(md_candidates) != 1:
            raise WorkbenchError(f"MD {md_id} must have one retained main reference")
        decisions = read_csv(DECISIONS_CSV)
        decision = next(row for row in decisions if row["md_object_id"] == md_id)
        resolved = sum(bool(row["selected_candidate_id"]) for row in decisions)
        open_ids = [
            row["md_object_id"]
            for row in wb.ordered_objects()
            if not next(
                decision["selected_candidate_id"]
                for decision in decisions
                if decision["md_object_id"] == row["md_object_id"]
            )
        ]
        if md_id in open_ids and len(open_ids) > 1:
            open_index = open_ids.index(md_id)
            previous_open = open_ids[open_index - 1]
            next_open = open_ids[(open_index + 1) % len(open_ids)]
            open_navigation = (
                f'<a href="/?{urlencode({"md": previous_open})}">Previous open</a> · '
                f'<a href="/?{urlencode({"md": next_open})}">Next open</a> · '
            )
        else:
            open_navigation = ""
        saved = "<strong>Decision saved.</strong> " if "saved=1" in self.path else ""
        other_html = "".join(
            self.candidate_card(row, dispositions[row["candidate_id"]]["disposition"])
            for row in other_candidates
        ) or "<div class='panel'><h2>No confirmed local candidate</h2><p>Select the MD surrogate if it is the best extant representation, or leave this object unresolved.</p></div>"
        md_card = self.candidate_card(
            md_candidates[0], dispositions[md_candidates[0]["candidate_id"]]["disposition"], True
        )
        no_master_checked = " checked" if not decision["selected_candidate_id"] else ""
        body = f"""
<header><div><strong>Collection 555 reconciliation</strong> · {resolved}/48 resolved · {48-resolved} open</div>
<nav>{open_navigation}<a href="/">Open queue</a> · <a href="/health">Health</a></nav></header>
<main><p>{saved}<span class="muted">Selecting a Master makes every other unmarked candidate Redundant and projects it into the non-authorizing drain list.</span></p>
<h1>MD {html.escape(md_id)} · {html.escape(obj['inventory'])}</h1>
<p>{html.escape(obj['title'])} · {html.escape(obj['date'])} · <a href="{html.escape(obj['object_url'], quote=True)}" target="_blank">museum-digital record</a></p>
<form method="post" action="/save" id="decision-form"><input type="hidden" name="md_object_id" value="{html.escape(md_id, quote=True)}">
<div class="layout"><section><h2>museum-digital reference</h2>{md_card}</section><section><h2>Local and existing archive candidates</h2><div class="candidate-grid">{other_html}</div></section></div>
<div class="savebar"><label class="choice"><input type="radio" name="master_candidate_id" value=""{no_master_checked}> No Master yet</label>
<textarea name="note" placeholder="Optional short note">{html.escape(decision['decision_note'])}</textarea>
<button name="after" value="next">Save and next unresolved</button><button class="secondary" name="after" value="stay">Save and stay</button></div></form></main>
<script>
document.querySelectorAll('[data-candidate]').forEach(card=>{{
 const master=card.querySelector('[name=master_candidate_id]');
 const rejected=card.querySelector('[name=rejected]');
 const noise=card.querySelector('[name=noise]');
 master.addEventListener('change',()=>{{if(master.checked){{rejected.checked=false;noise.checked=false}}}});
 rejected.addEventListener('change',()=>{{if(rejected.checked){{noise.checked=false;if(master.checked)document.querySelector('[name=master_candidate_id][value=""]').checked=true}}}});
 noise.addEventListener('change',()=>{{if(noise.checked){{rejected.checked=false;if(master.checked)document.querySelector('[name=master_candidate_id][value=""]').checked=true}}}});
}});
</script>"""
        self.send_bytes(page_shell(f"MD {md_id} reconciliation", body), "text/html; charset=utf-8")


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], workbench: Workbench) -> None:
        self.workbench = workbench
        super().__init__(address, Handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", default="vladimir")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workbench = Workbench(args.actor)
    check = workbench.check()
    if args.check:
        print(json.dumps(check, indent=2, sort_keys=True))
        return
    server = WorkbenchServer((args.host, args.port), workbench)
    url = f"http://{args.host}:{args.port}/"
    print(json.dumps({**check, "url": url}, indent=2, sort_keys=True), flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
