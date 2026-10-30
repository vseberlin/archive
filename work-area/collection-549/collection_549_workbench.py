#!/usr/bin/env python3
"""Minimal human Master-selection workbench for museum-digital collection 549.

The workbench writes only the existing decision and candidate-disposition CSV
projections below iss-archive-work.  It never edits source files, generates
derivatives, performs accession, or authorizes deletion.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import subprocess
import threading
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from PIL import Image, ImageOps


VERSION = "1.0.0-collection-549-review"
ROOT = Path("/mnt/volume/iss-archive-work/collection-549/work/reconciliation/unit-reconciliation-v1")
SOURCE_ROOT = Path("/media/vladimir/Elements")
OBJECTS_CSV = ROOT / "objects.csv"
CANDIDATES_CSV = ROOT / "master-candidates.csv"
DECISIONS_CSV = ROOT / "master-decisions.csv"
DISPOSITIONS_CSV = ROOT / "candidate-dispositions.csv"
RESOURCES_CSV = ROOT / "related-md-resources.csv"
METADATA_CSV = ROOT / "metadata-review.csv"

DECISION_FIELDS = [
    "md_object_id",
    "inventory",
    "decision_state",
    "selected_candidate_id",
    "decision_note",
    "actor",
    "decided_at",
    "deletion_authorized",
]
DISPOSITION_FIELDS = [
    "candidate_id",
    "md_object_id",
    "candidate_path",
    "disposition",
    "reason",
    "deletion_authorized",
]
METADATA_FIELDS = [
    "md_object_id",
    "inventory",
    "field",
    "current_value",
    "proposed_value",
    "proposal_basis",
    "decision",
    "final_value",
    "note",
    "deletion_authorized",
]
METADATA_DECISIONS = {"pending_human_review", "keep_current", "accept_proposed"}


class WorkbenchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_guard() -> str:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise WorkbenchError("Elements does not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise WorkbenchError(f"protected Elements mount guard failed: {row}")
    return f"{row['source']} {row['target']} {row['fstype']} {row['options']}"


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def human_size(value: str) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class Workbench:
    def __init__(self, actor: str) -> None:
        self.actor = " ".join(actor.split())[:120]
        if not self.actor:
            raise WorkbenchError("a decision actor is required")
        self.lock = threading.Lock()
        self.objects = read_csv(OBJECTS_CSV)
        self.candidates = read_csv(CANDIDATES_CSV)
        self.resources = read_csv(RESOURCES_CSV)
        self.metadata = read_csv(METADATA_CSV)
        for index, row in enumerate(self.metadata):
            row["_key"] = f"m{index}"
        self.objects_by_id = {row["md_object_id"]: row for row in self.objects}
        self.candidates_by_id = {row["candidate_id"]: row for row in self.candidates}
        self.resources_by_id = {row["resource_id"]: row for row in self.resources}
        self.candidates_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.resources_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.metadata_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.candidates:
            self.candidates_by_object[row["md_object_id"]].append(row)
        for row in self.resources:
            self.resources_by_object[row["md_object_id"]].append(row)
        for row in self.metadata:
            self.metadata_by_object[row["md_object_id"]].append(row)
        self.validate_static()

    def validate_static(self) -> None:
        source_guard()
        if len(self.objects) != 10 or len(self.objects_by_id) != 10:
            raise WorkbenchError("workbench requires exactly 10 unique collection-549 objects")
        if len(self.candidates) != 12 or len(self.candidates_by_id) != 12:
            raise WorkbenchError("workbench requires the prepared 12 unique candidates")
        if len(self.resources) != 18 or len(self.resources_by_id) != 18:
            raise WorkbenchError("workbench requires all 18 unique museum-digital resources")
        if set(self.objects_by_id) != set(self.candidates_by_object):
            raise WorkbenchError("each object must have at least one Master candidate")
        for row in self.objects + self.candidates + self.resources + self.metadata:
            if row.get("deletion_authorized") != "false":
                raise WorkbenchError("a proposal row carries deletion authority")
        for candidate in self.candidates:
            path = Path(candidate["candidate_path"])
            if not path.is_file() or not (inside(path, SOURCE_ROOT) or inside(path, ROOT)):
                raise WorkbenchError(f"unsafe or missing candidate: {path}")
        for resource in self.resources:
            path = Path(resource["work_path"])
            if not path.is_file() or not inside(path, ROOT):
                raise WorkbenchError(f"unsafe or missing retained resource: {path}")
        for row in self.metadata:
            decision = row.get("decision")
            if decision not in METADATA_DECISIONS:
                raise WorkbenchError(f"invalid metadata decision: {decision}")
            expected_final = {
                "pending_human_review": "",
                "keep_current": row["current_value"],
                "accept_proposed": row["proposed_value"],
            }[decision]
            if row.get("final_value") != expected_final:
                raise WorkbenchError(f"metadata final value disagrees for {row['_key']}")

    def decisions(self) -> dict[str, dict[str, str]]:
        rows = read_csv(DECISIONS_CSV)
        if len(rows) != 10 or {row["md_object_id"] for row in rows} != set(self.objects_by_id):
            raise WorkbenchError("decision projection does not cover exactly 10 objects")
        return {row["md_object_id"]: row for row in rows}

    def dispositions_for(self, decisions: dict[str, dict[str, str]]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for candidate in sorted(
            self.candidates,
            key=lambda row: (int(row["md_object_id"]), row["candidate_id"]),
        ):
            decision = decisions[candidate["md_object_id"]]
            selected = decision["selected_candidate_id"]
            if selected:
                disposition = "selected_master" if candidate["candidate_id"] == selected else "redundant"
                reason = (
                    "human selected this file as the single Master"
                    if disposition == "selected_master"
                    else "not selected after human confirmation of the single Master"
                )
            else:
                disposition = "pending"
                reason = "object left unresolved; awaiting human Master decision"
            rows.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "md_object_id": candidate["md_object_id"],
                    "candidate_path": candidate["candidate_path"],
                    "disposition": disposition,
                    "reason": reason,
                    "deletion_authorized": "false",
                }
            )
        return rows

    def save(
        self,
        md_id: str,
        selected: str,
        note: str,
        metadata_choices: dict[str, str],
    ) -> dict[str, object]:
        source_guard()
        if md_id not in self.objects_by_id:
            raise WorkbenchError("unknown collection-549 object")
        allowed = {row["candidate_id"] for row in self.candidates_by_object[md_id]}
        if selected and selected not in allowed:
            raise WorkbenchError("selected Master is not a candidate for this object")
        note = " ".join(note.split())[:500]
        object_metadata = self.metadata_by_object[md_id]
        expected_keys = {row["_key"] for row in object_metadata}
        if set(metadata_choices) != expected_keys:
            raise WorkbenchError("metadata choices do not cover this object's proposals")
        if any(choice not in {"pending", "current", "proposed"} for choice in metadata_choices.values()):
            raise WorkbenchError("invalid metadata proposal decision")
        with self.lock:
            decisions = self.decisions()
            row = decisions[md_id]
            row.update(
                {
                    "decision_state": "selected_master" if selected else "unresolved",
                    "selected_candidate_id": selected,
                    "decision_note": note,
                    "actor": self.actor,
                    "decided_at": utc_now(),
                    "deletion_authorized": "false",
                }
            )
            ordered = [decisions[obj["md_object_id"]] for obj in self.ordered_objects()]
            dispositions = self.dispositions_for(decisions)
            reviewed_at = utc_now()
            for metadata_row in object_metadata:
                choice = metadata_choices[metadata_row["_key"]]
                metadata_row["decision"] = {
                    "pending": "pending_human_review",
                    "current": "keep_current",
                    "proposed": "accept_proposed",
                }[choice]
                metadata_row["final_value"] = {
                    "pending": "",
                    "current": metadata_row["current_value"],
                    "proposed": metadata_row["proposed_value"],
                }[choice]
                metadata_row["note"] = (
                    "" if choice == "pending" else f"reviewed by {self.actor} at {reviewed_at}"
                )
            atomic_csv(DECISIONS_CSV, DECISION_FIELDS, ordered)
            atomic_csv(DISPOSITIONS_CSV, DISPOSITION_FIELDS, dispositions)
            atomic_csv(METADATA_CSV, METADATA_FIELDS, self.metadata)
        return self.check()

    def check(self) -> dict[str, object]:
        mount = source_guard()
        decisions = self.decisions()
        dispositions = read_csv(DISPOSITIONS_CSV)
        expected = self.dispositions_for(decisions)
        errors: list[str] = []
        if len(dispositions) != 12:
            errors.append("candidate disposition projection is not exactly 12 rows")
        actual_by_id = {row.get("candidate_id", ""): row for row in dispositions}
        for expected_row in expected:
            actual = actual_by_id.get(expected_row["candidate_id"])
            if not actual:
                errors.append(f"missing disposition {expected_row['candidate_id']}")
                continue
            # The preparation pass may use a more evidence-specific pending
            # reason.  Identity, state, path, and non-deletion authority are
            # the durable invariants; a later decision save normalizes reason.
            for key in ("candidate_id", "md_object_id", "candidate_path", "disposition", "deletion_authorized"):
                if actual.get(key) != expected_row[key]:
                    errors.append(f"disposition disagreement {expected_row['candidate_id']}:{key}")
        for md_id, decision in decisions.items():
            selected = decision["selected_candidate_id"]
            allowed = {row["candidate_id"] for row in self.candidates_by_object[md_id]}
            if selected and selected not in allowed:
                errors.append(f"foreign selected candidate for {md_id}")
            if decision.get("deletion_authorized") != "false":
                errors.append(f"decision carries deletion authority for {md_id}")
            if bool(selected) != (decision.get("decision_state") == "selected_master"):
                errors.append(f"decision state disagreement for {md_id}")
        for row in self.metadata:
            decision = row.get("decision")
            if decision not in METADATA_DECISIONS:
                errors.append(f"invalid metadata decision {row['_key']}")
                continue
            expected_final = {
                "pending_human_review": "",
                "keep_current": row["current_value"],
                "accept_proposed": row["proposed_value"],
            }[decision]
            if row.get("final_value") != expected_final:
                errors.append(f"metadata final value disagreement {row['_key']}")
            if row.get("deletion_authorized") != "false":
                errors.append(f"metadata row carries deletion authority {row['_key']}")
        if errors:
            raise WorkbenchError("; ".join(errors))
        counts = Counter(row["disposition"] for row in dispositions)
        return {
            "result": "ok",
            "version": VERSION,
            "mount": mount,
            "objects": 10,
            "museum_digital_resources": 18,
            "master_candidates": 12,
            "resolved_objects": sum(bool(row["selected_candidate_id"]) for row in decisions.values()),
            "unresolved_objects": sum(not row["selected_candidate_id"] for row in decisions.values()),
            "candidate_dispositions": dict(sorted(counts.items())),
            "metadata_proposals_pending": sum(row["decision"] == "pending_human_review" for row in self.metadata),
            "metadata_proposals_reviewed": sum(row["decision"] != "pending_human_review" for row in self.metadata),
            "deletion_authorized": False,
            "accession_performed": False,
            "derivatives_generated": 0,
        }

    def ordered_objects(self) -> list[dict[str, str]]:
        return sorted(self.objects, key=lambda row: (row["inventory"].casefold(), int(row["md_object_id"])))

    def next_unresolved(self, after: str = "") -> str:
        decisions = self.decisions()
        objects = self.ordered_objects()
        start = next((index + 1 for index, row in enumerate(objects) if row["md_object_id"] == after), 0)
        for row in objects[start:] + objects[:start]:
            if not decisions[row["md_object_id"]]["selected_candidate_id"]:
                return row["md_object_id"]
        return after or objects[0]["md_object_id"]


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--paper:#f5f1e8;--ink:#20282a;--muted:#637174;--line:#c9c3b6;--blue:#185c73;--green:#2e684c;--amber:#916b18}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.42 system-ui,sans-serif}}
header{{position:sticky;top:0;z-index:3;display:flex;gap:18px;align-items:center;justify-content:space-between;padding:12px 20px;background:#fff;border-bottom:1px solid var(--line)}}
main{{max-width:1700px;margin:auto;padding:18px}}a{{color:var(--blue)}}h1{{font-size:1.5rem;margin:.1rem 0}}h2{{font-size:1.12rem;margin:.2rem 0 .7rem}}h3{{font-size:1rem;margin:.3rem 0}}
.muted{{color:var(--muted)}}.status{{display:inline-block;padding:3px 7px;border-radius:4px;background:#eee;font-size:.8rem}}.resolved{{background:#d8eadf;color:#174c32}}.pending{{background:#f3e8c8;color:#684a08}}
.object-nav{{display:flex;gap:7px;flex-wrap:wrap}}.object-nav a{{padding:5px 8px;border:1px solid var(--line);border-radius:4px;text-decoration:none;background:#fff}}.object-nav a.current{{border-color:var(--blue);font-weight:700}}
.panel{{margin:14px 0;background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:13px}}.card{{border:2px solid var(--line);border-radius:7px;padding:10px;min-width:0}}.card.master{{border-color:var(--green);background:#f4fbf6}}.card.redundant{{border-color:#98917d;background:#faf9f5}}
.preview{{display:block;width:100%;height:300px;object-fit:contain;background:#171717;border-radius:4px}}iframe.preview{{border:0;background:#ddd}}.path{{font:12px/1.35 ui-monospace,monospace;overflow-wrap:anywhere}}.facts{{display:grid;grid-template-columns:max-content 1fr;gap:3px 12px}}.facts dt{{font-weight:700}}.facts dd{{margin:0;overflow-wrap:anywhere}}
.choice{{display:block;margin-top:9px;padding:8px;border:1px solid var(--line);border-radius:5px;cursor:pointer}}textarea{{width:100%;min-height:70px;padding:8px}}button{{border:0;border-radius:5px;padding:10px 15px;color:#fff;background:var(--blue);cursor:pointer}}.savebar{{position:sticky;bottom:0;background:#fff;border:1px solid var(--line);border-radius:7px;padding:12px;box-shadow:0 -4px 12px #0001}}
.metadata-row{{padding:8px 0;border-top:1px solid var(--line)}}.arrow{{color:var(--amber);font-weight:700}}@media(max-width:700px){{.preview{{height:240px}}header{{position:static}}}}
</style></head><body>{body}</body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server: "WorkbenchServer"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {fmt % args}")

    def send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self.send_bytes(json.dumps(self.server.workbench.check(), indent=2).encode(), "application/json; charset=utf-8")
            elif parsed.path == "/candidate":
                self.asset("candidate", query.get("id", [""])[0])
            elif parsed.path == "/resource":
                self.asset("resource", query.get("id", [""])[0])
            elif parsed.path == "/":
                md_id = query.get("md", [""])[0] or self.server.workbench.next_unresolved()
                self.index(md_id, query.get("saved", [""])[0] == "1")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as error:
            self.send_bytes(json.dumps({"result": "error", "error": str(error)}).encode(), "application/json; charset=utf-8", 503)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/save":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 128 * 1024:
                raise WorkbenchError("form is too large")
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            md_id = fields.get("md_object_id", [""])[0]
            selected = fields.get("master_candidate_id", [""])[0]
            note = fields.get("note", [""])[0]
            metadata_choices = {
                key.removeprefix("metadata_"): values[0]
                for key, values in fields.items()
                if key.startswith("metadata_")
            }
            self.server.workbench.save(md_id, selected, note, metadata_choices)
            target = self.server.workbench.next_unresolved(md_id)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/?" + urlencode({"md": target, "saved": "1"}))
            self.end_headers()
        except Exception as error:
            self.send_bytes(page("Decision not saved", f"<main><h1>Decision not saved</h1><p>{html.escape(str(error))}</p><p><a href='/'>Return</a></p></main>"), "text/html; charset=utf-8", 400)

    def asset(self, kind: str, item_id: str) -> None:
        wb = self.server.workbench
        if kind == "candidate":
            row = wb.candidates_by_id.get(item_id)
            path = Path(row["candidate_path"]) if row else None
        else:
            row = wb.resources_by_id.get(item_id)
            path = Path(row["work_path"]) if row else None
        if not row or path is None or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        suffix = path.suffix.casefold()
        if suffix == ".pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    self.wfile.write(block)
            return
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
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

    def media(self, endpoint: str, item_id: str, is_pdf: bool, alt: str) -> str:
        url = f"/{endpoint}?{urlencode({'id': item_id})}"
        if is_pdf:
            return f'<iframe class="preview" src="{html.escape(url, quote=True)}" title="{html.escape(alt, quote=True)}"></iframe>'
        return f'<img class="preview" src="{html.escape(url, quote=True)}" alt="{html.escape(alt, quote=True)}">'

    def index(self, md_id: str, saved: bool) -> None:
        wb = self.server.workbench
        if md_id not in wb.objects_by_id:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        decisions = wb.decisions()
        decision = decisions[md_id]
        obj = wb.objects_by_id[md_id]
        nav = []
        for row in wb.ordered_objects():
            rid = row["md_object_id"]
            marker = "✓" if decisions[rid]["selected_candidate_id"] else "·"
            current = " current" if rid == md_id else ""
            nav.append(f'<a class="{current.strip()}" href="/?{urlencode({"md": rid})}">{marker} {html.escape(row["inventory"])}</a>')
        status_class = "resolved" if decision["selected_candidate_id"] else "pending"
        status_text = "Master selected" if decision["selected_candidate_id"] else "Unresolved"

        resource_cards = []
        for resource in sorted(wb.resources_by_object[md_id], key=lambda row: int(row["resource_position"])):
            is_pdf = resource["format"].casefold() == "pdf"
            resource_cards.append(f"""<article class="card">
{self.media('resource', resource['resource_id'], is_pdf, resource['description'] or obj['title'])}
<h3>{'Main resource' if resource['is_main'] == 'true' else 'Additional resource'} · {html.escape(resource['resource_id'])}</h3>
<p>{html.escape(resource['description'] or 'No resource description')}</p>
<p class="muted">{html.escape(resource['creator'] or 'Creator not recorded')} · {html.escape(resource['rights'])}</p>
</article>""")

        current_dispositions = {row["candidate_id"]: row["disposition"] for row in wb.dispositions_for(decisions)}
        candidate_cards = []
        for candidate in wb.candidates_by_object[md_id]:
            cid = candidate["candidate_id"]
            disposition = current_dispositions[cid]
            classes = "card " + ("master" if disposition == "selected_master" else "redundant" if disposition == "redundant" else "")
            checked = " checked" if decision["selected_candidate_id"] == cid else ""
            is_pdf = candidate["format"].casefold() == "pdf"
            dimensions = (
                f"{candidate['width_px']} × {candidate['height_px']} px"
                if candidate["width_px"] and candidate["height_px"]
                else "page dimensions available in PDF preview"
            )
            candidate_cards.append(f"""<article class="{classes.strip()}">
{self.media('candidate', cid, is_pdf, Path(candidate['candidate_path']).name)}
<h3>{html.escape(Path(candidate['candidate_path']).name)}</h3>
<span class="status">{html.escape(disposition.replace('_', ' '))}</span>
<dl class="facts"><dt>Role</dt><dd>{html.escape(candidate['candidate_role'])}</dd><dt>Evidence</dt><dd>{html.escape(candidate['evidence_state'])}</dd><dt>Technical</dt><dd>{html.escape(dimensions)}, {human_size(candidate['size_bytes'])}</dd><dt>Source</dt><dd>{html.escape(candidate['candidate_source'])}</dd></dl>
<p>{html.escape(candidate['evidence_detail'])}</p><p class="path">{html.escape(candidate['candidate_path'])}</p>
<label class="choice"><input type="radio" name="master_candidate_id" value="{html.escape(cid, quote=True)}"{checked}> Select this file as the single Master</label>
</article>""")

        metadata_rows = []
        for row in wb.metadata_by_object[md_id]:
            selected_choice = {
                "pending_human_review": "pending",
                "keep_current": "current",
                "accept_proposed": "proposed",
            }[row["decision"]]
            name = f"metadata_{row['_key']}"
            metadata_rows.append(f"""<div class="metadata-row"><strong>{html.escape(row['field'])}</strong><br>
<span>{html.escape(row['current_value'] or '[empty]')}</span> <span class="arrow">→</span> <span>{html.escape(row['proposed_value'] or '[empty]')}</span><br>
<span class="muted">{html.escape(row['proposal_basis'])}</span><div>
<label class="choice"><input type="radio" name="{name}" value="pending"{' checked' if selected_choice == 'pending' else ''}> Leave pending</label>
<label class="choice"><input type="radio" name="{name}" value="current"{' checked' if selected_choice == 'current' else ''}> Keep current value</label>
<label class="choice"><input type="radio" name="{name}" value="proposed"{' checked' if selected_choice == 'proposed' else ''}> Accept proposed value</label>
</div></div>""")
        if not metadata_rows:
            metadata_rows.append('<p class="muted">No metadata change proposed for this object.</p>')

        saved_note = '<p class="status resolved">Decision saved. No accession or deletion was performed.</p>' if saved else ""
        body = f"""<header><div><strong>Collection 549 reconciliation</strong><br><span class="muted">proposal-only · actor {html.escape(wb.actor)}</span></div><a href="/health">health/check</a></header>
<main>{saved_note}<div class="object-nav">{''.join(nav)}</div>
<section class="panel"><span class="status {status_class}">{status_text}</span><h1>{html.escape(obj['title'])}</h1>
<dl class="facts"><dt>Inventory</dt><dd>{html.escape(obj['inventory'])}</dd><dt>MD object</dt><dd><a href="{html.escape(obj['object_url'], quote=True)}" target="_blank" rel="noreferrer">{html.escape(md_id)}</a></dd><dt>Type / date</dt><dd>{html.escape(obj['object_type'])} · {html.escape(obj['date'])}</dd><dt>Dimensions</dt><dd>{html.escape(obj['dimensions'] or '[not recorded]')}</dd></dl>
<p>{html.escape(obj['description'])}</p></section>
<section class="panel"><h2>Catalogue resources — retained separately</h2><p class="muted">These views and documents provide object context. Only the main resource is a fallback Master candidate.</p><div class="grid">{''.join(resource_cards)}</div></section>
<form method="post" action="/save"><input type="hidden" name="md_object_id" value="{html.escape(md_id, quote=True)}">
<section class="panel"><h2>Master candidates</h2><p>Select exactly one file, or keep the object unresolved. On selection, every other candidate is recorded as redundant; this never authorizes deletion.</p>
<label class="choice"><input type="radio" name="master_candidate_id" value=""{' checked' if not decision['selected_candidate_id'] else ''}> Leave this object unresolved</label>
<div class="grid">{''.join(candidate_cards)}</div></section>
<section class="panel"><h2>Metadata proposals</h2><p class="muted">Bounded choices only: accept the proposal, keep the current value, or leave it pending. This records a local reconciliation decision and does not edit museum-digital.</p>{''.join(metadata_rows)}</section>
<div class="savebar"><label style="flex:1">Decision note<br><textarea name="note" maxlength="500">{html.escape(decision['decision_note'])}</textarea></label><button type="submit">Save decision and continue</button><span class="muted">No source changes · no derivatives · no accession · no deletion</span></div></form></main>"""
        self.send_bytes(page(f"{obj['inventory']} — collection 549", body), "text/html; charset=utf-8")


class WorkbenchServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], workbench: Workbench):
        super().__init__(address, Handler)
        self.workbench = workbench


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", default="Vladimir", help="name recorded with decisions")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("collection-549 workbench is localhost-only")
    workbench = Workbench(args.actor)
    if args.check:
        print(json.dumps(workbench.check(), ensure_ascii=False, indent=2, sort_keys=True))
        return
    server = WorkbenchServer((args.host, args.port), workbench)
    print(f"Collection 549 workbench: http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
