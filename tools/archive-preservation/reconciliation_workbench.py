#!/usr/bin/env python3
"""Bounded, file-authoritative reconciliation. Never accession or modify sources."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import fcntl
from functools import lru_cache
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit


HERE = Path(__file__).resolve().parent
DECISIONS = "reconciliation-decisions.jsonl"
PROJECTION = "reconciliation-dispositions.csv"
FORMAT = "archive-reconciliation-1"
ELEMENTS = Path("/media/vladimir/Elements")
RENDER_LIMIT = threading.BoundedSemaphore(2)


class ReviewError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReviewError(message)


def text(value):
    return str(value or "")


def flag(value):
    return value is True or str(value).lower() == "true"


def atomic(path, payload):
    """Replace one readable file, retaining no second authority or generations."""
    require(not path.is_symlink(), "Refusing a symlink for a decision file")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        Path(name).unlink(missing_ok=True)


def csv_payload(rows, fields):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def elements_guard(paths):
    if not any(p.is_relative_to(ELEMENTS) for p in paths):
        return
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(ELEMENTS), "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    require(result.returncode == 0, "The protected Elements source is not mounted")
    mounts = json.loads(result.stdout).get("filesystems", [])
    require(len(mounts) == 1, "Ambiguous Elements mount")
    m = mounts[0]
    require((m.get("source"), m.get("target"), m.get("fstype")) ==
            ("/dev/sdf1", str(ELEMENTS), "ntfs3") and
            "ro" in m.get("options", "").split(","),
            "Require /dev/sdf1 /media/vladimir/Elements ntfs3 ro")


class Workbench:
    def __init__(self, root, actor):
        original = Path(root).absolute()
        self.root = original.resolve(strict=True)
        require(original == self.root, "Work root must be a real path, not a symlink")
        require(self.root.is_dir(), "Work root must exist")
        for protected in (ELEMENTS, Path("/mnt/volume/iss-archive"),
                          Path("/mnt/volume/archive"), Path("/mnt/volume/archive-intake")):
            require(not self.root.is_relative_to(protected), "Work root is a protected archive/source")
        self.actor = actor.strip()
        require(0 < len(self.actor) <= 100, "Supply an actor name (at most 100 characters)")
        self.lock = threading.RLock()
        self.load()
        self.read_decisions()

    @contextmanager
    def locked(self):
        with self.lock:
            lock = self.root / ".reconciliation.lock"
            require(not lock.is_symlink(), "Unsafe lock file")
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def evidence(self, name, optional=False):
        path = self.root / name
        require(path.parent == self.root and not path.is_symlink(), "Unsafe evidence filename")
        if optional and not path.exists():
            self.inputs[name] = b""
            return b""
        raw = path.read_bytes()
        self.inputs[name] = raw
        return raw

    def csv(self, name, optional=False):
        return list(csv.DictReader(io.StringIO(self.evidence(name, optional).decode("utf-8-sig"))))

    def load(self):
        self.inputs = {}
        manifest = self.root / "reconciliation-input.json"
        if manifest.exists():
            config = json.loads(self.evidence(manifest.name))
            require(config.get("format") == FORMAT, "Unsupported reconciliation input format")
            require(isinstance(config.get("read_only"), bool), "Input must explicitly declare read_only")
            if "csv_profile" in config:
                require(config["csv_profile"] in ("bibliothek", "collection-591"), "Unsupported CSV handoff profile")
                require("objects" not in config and "initial_decisions" not in config,
                        "CSV adoption must use the unit's prepared evidence and existing decisions")
                base, self.objects, self.initial = self.load_csv(config["csv_profile"])
                self.config = {**base, **config}
            else:
                self.config = config
                self.objects = config["objects"]
                self.initial = config.get("initial_decisions", [])
        else:
            self.config, self.objects, self.initial = self.load_csv()
        require(isinstance(self.objects, list) and self.objects, "No prepared objects")
        self.roots = [self.root]
        for value in self.config.get("read_roots", []):
            p = Path(value)
            require(p.is_absolute(), "Read roots must be absolute")
            resolved = p.resolve()
            require(not self.root.is_relative_to(resolved), "A protected read root contains the writable work area")
            self.roots.append(resolved)
        self.by_object, self.by_candidate = {}, {}
        for obj in self.objects:
            oid = obj.get("id")
            require(isinstance(oid, str) and oid and oid not in self.by_object, "Duplicate or missing object ID")
            self.by_object[oid] = obj
            require(isinstance(obj.get("candidates"), list), "Object needs a candidate list")
            obj.setdefault("metadata", [])
            fields = set()
            for field in obj["metadata"]:
                require(isinstance(field.get("field"), str) and field["field"] not in fields,
                        "Duplicate metadata field")
                fields.add(field["field"])
                choices = field.get("choices", [])
                ids = [c.get("id") for c in choices]
                require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids),
                        "Invalid metadata choices")
            for candidate in obj["candidates"]:
                cid = candidate.get("id")
                require(isinstance(cid, str) and cid and cid not in self.by_candidate, "Duplicate or missing candidate ID")
                candidate["object_id"] = oid
                candidate.setdefault("eligible", False)
                candidate.setdefault("complete", False)
                candidate.setdefault("role", "candidate")
                require(type(candidate["eligible"]) is bool and type(candidate["complete"]) is bool,
                        "Candidate eligibility/completeness must be boolean")
                members = candidate.get("members", [])
                require(isinstance(members, list) and members, "Candidate needs ordered members")
                seen = set()
                for member in members:
                    path = Path(member.get("path", ""))
                    require(path.is_absolute(), "Member paths must be absolute")
                    require(str(path) not in seen, "Duplicate representation member")
                    require(path not in {self.root / n for n in (
                        DECISIONS, PROJECTION, ".reconciliation.lock",
                        "master-decisions.csv", "candidate-dispositions.csv")},
                            "Source member overlaps a workbench output")
                    seen.add(str(path))
                    self.checked_scope(path)
                    require(isinstance(member.get("size_bytes"), int) and member["size_bytes"] >= 0 and
                            isinstance(member.get("mtime_ns"), int), "Members require recorded size and mtime_ns")
                    pages = member.setdefault("pages", 1)
                    require(type(pages) is int and 1 <= pages <= 100000, "Invalid page count")
                    digest = member.get("sha256", "")
                    require(not digest or (isinstance(digest, str) and len(digest) == 64 and
                            all(c in "0123456789abcdef" for c in digest)), "Invalid stored SHA-256")
                self.by_candidate[cid] = candidate
        self.receipts = []
        for name in self.config.get("completion_receipts", []):
            path = Path(name)
            require(path.is_absolute(), "Completion receipt paths must be absolute")
            self.receipts.append(path)
        # These conventional unit receipts freeze editing whenever present, even if malformed.
        self.receipts += [self.root / name for name in (
            "accession-receipt.json", "accession-completion.json", "publication.json")]
        self.frozen = bool(self.config.get("read_only")) or any(p.exists() or p.is_symlink() for p in self.receipts)

    def load_csv(self, profile=None):
        """Read simple Bibliothek/591-style prepared evidence, never legacy DBs."""
        rows = self.csv("objects.csv")
        candidates = self.csv("candidates.csv")
        initial = self.csv("master-decisions.csv", optional=True)
        self.legacy_rows = initial
        self.legacy_candidates = candidates
        if profile:
            key = "object_key" if profile == "bibliothek" else "md_object_id"
            required = {key, "selected_candidate_id", "decision", "actor", "decided_at", "note", "deletion_authorized"}
            require(initial and all(required <= row.keys() for row in initial), "Incomplete legacy decision schema")
            require(all(row.get(key) for row in rows + candidates + initial), "CSV profile/object keys disagree")
            require(len(initial) == len(rows) and {r[key] for r in initial} == {r[key] for r in rows},
                    "Legacy decisions must cover exactly the prepared objects")
            require(bool(candidates), "CSV handoff requires prepared candidates")
        require(not (self.root / "master-candidates.csv").exists(),
                "This unit needs an explicit reconciliation-input.json adapter; refusing to guess its representation rules")
        objects = []
        for row in rows:
            oid = row.get("object_key") or row.get("md_object_id")
            obj = {"id": oid, "title": row.get("title", ""), "inventory": row.get("inventory", ""),
                   "description": row.get("description", ""),
                   "provenance": row.get("object_url") or row.get("md_url", ""),
                   "source_record": dict(row), "candidates": [], "metadata": []}
            for c in candidates:
                if (c.get("object_key") or c.get("md_object_id")) != oid:
                    continue
                require(c.get("eligible") in ("true", "false"), "CSV candidate must explicitly declare eligible")
                members = json.loads(c["members_json"])
                for m in members:
                    m["path"] = m.get("path") or m.get("source_path")
                    m["size_bytes"] = int(m["size_bytes"])
                    m["mtime_ns"] = int(m["mtime_ns"])
                    m["pages"] = int(m.get("pages") or 1)
                obj["candidates"].append({"id": c["candidate_id"], "label": c.get("label") or c["candidate_id"],
                    "eligible": flag(c["eligible"]), "complete": flag(c["eligible"]),
                    "role": c.get("role", "candidate"), "evidence": c.get("evidence", ""),
                    "basis": c.get("basis") or c.get("match_basis", ""),
                    "comparison": c.get("comparison") or c.get("quality_note", ""),
                    "source_record": dict(c), "members": members})
            objects.append(obj)
        known = {o["id"] for o in objects}
        require(all((c.get("object_key") or c.get("md_object_id")) in known for c in candidates),
                "Candidate belongs to an unknown object")
        require(not any(flag(r.get("deletion_authorized")) for r in initial), "Existing decisions carry deletion authority")
        config = {"title": self.root.name, "read_only": True,
                  "read_roots": [str(ELEMENTS)] if any(
                      Path(m["path"]).is_relative_to(ELEMENTS)
                      for o in objects for c in o["candidates"] for m in c["members"]) else []}
        decisions = [{"object_id": r.get("object_key") or r.get("md_object_id"),
                      "selected_candidate_id": r.get("selected_candidate_id", ""),
                      "metadata_choices": {}, "note": r.get("note") or r.get("decision_note", ""),
                      "actor": r.get("actor", ""), "decided_at": r.get("decided_at", ""),
                      "deletion_authorized": False} for r in initial]
        return config, objects, decisions

    def checked_scope(self, path):
        require(any(path.is_relative_to(root) for root in self.roots), "Evidence path is outside approved roots")
        resolved = path.resolve()
        require(resolved == path, "Evidence paths must be real paths without symlinks")
        require(any(resolved.is_relative_to(root) for root in self.roots), "Evidence symlink escapes approved roots")
        return resolved

    def checked_member(self, member):
        path = self.checked_scope(Path(member["path"]))
        # Resolve after mount validation too: a replacement mount/symlink must not pass.
        elements_guard([path])
        path = self.checked_scope(path)
        st = path.stat()
        require(path.is_file() and (st.st_size, st.st_mtime_ns) ==
                (member["size_bytes"], member["mtime_ns"]), "Source changed since inspection; refresh prepared evidence")
        return path

    def default_decisions(self):
        return {oid: {"object_id": oid, "selected_candidate_id": "", "metadata_choices": {},
                      "note": "", "actor": "", "decided_at": "", "deletion_authorized": False}
                for oid in self.by_object}

    def read_decisions(self):
        path = self.root / DECISIONS
        require(not path.is_symlink(), "Unsafe decisions file")
        raw = path.read_bytes() if path.exists() else b""
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()] if path.exists() else self.initial
        decisions = self.default_decisions()
        seen = set()
        for row in rows:
            oid = row.get("object_id")
            require(oid in decisions and oid not in seen, "Unknown or duplicate decision object")
            seen.add(oid)
            decisions[oid] = row
        if path.exists():
            require(seen == set(decisions), "Saved decisions do not cover prepared objects; explicit reconciliation required")
        self.validate(decisions)
        digest = hashlib.sha256()
        for name, data in sorted(self.inputs.items()):
            digest.update(name.encode() + b"\0" + data + b"\0")
        digest.update(raw)
        return decisions, digest.hexdigest()

    def validate(self, decisions):
        claims = {}
        for oid, row in decisions.items():
            require(row.get("deletion_authorized") is False, "Deletion authority is forbidden")
            require(isinstance(row.get("note"), str) and len(row["note"]) <= 2000, "Note must be at most 2000 characters")
            choices = row.get("metadata_choices")
            require(isinstance(choices, dict), "Invalid metadata choices")
            fields = {f["field"]: {c["id"] for c in f["choices"]} for f in self.by_object[oid]["metadata"]}
            require(all(key in fields and isinstance(value, str) and value in fields[key]
                        for key, value in choices.items()), "Unknown metadata choice")
            if "metadata_values" in row:
                effective = {f["field"]: next(c for c in f["choices"] if c["id"] == choices[f["field"]])
                             for f in self.by_object[oid]["metadata"] if f["field"] in choices}
                require(row["metadata_values"] == effective, "Previously selected metadata evidence changed")
            cid = row.get("selected_candidate_id")
            require(isinstance(cid, str), "Invalid selected candidate")
            if not cid:
                continue
            candidate = self.by_candidate.get(cid)
            require(candidate and candidate["object_id"] == oid, "Candidate belongs to another object")
            require(candidate["eligible"] and candidate["complete"] and
                    candidate["role"] not in ("derivative", "reference", "component", "excluded"),
                    "Choose an eligible complete Master representation")
            if "selected_members" in row:
                require(row["selected_members"] == candidate["members"],
                        "Selected representation evidence changed; reconcile the prepared input explicitly")
            for member in candidate["members"]:
                keys = ["path:" + member["path"]]
                if member.get("sha256"):
                    keys.append("sha256:" + member["sha256"])
                for key in keys:
                    require(key not in claims or claims[key] == oid, "Source identity already selected for another object")
                    claims[key] = oid

    def projection(self, decisions):
        rows = []
        for oid, obj in self.by_object.items():
            selected = decisions[oid]["selected_candidate_id"]
            for candidate in obj["candidates"]:
                status = "selected_master" if selected == candidate["id"] else ("redundant" if selected else "unresolved")
                if candidate["role"] == "excluded":
                    status = "excluded"
                for index, member in enumerate(candidate["members"], 1):
                    rows.append({"object_id": oid, "candidate_id": candidate["id"], "member_order": index,
                                 "source_path": member["path"], "disposition": status, "deletion_authorized": "false"})
        return csv_payload(rows, ["object_id", "candidate_id", "member_order", "source_path", "disposition", "deletion_authorized"])

    def refresh_projection(self, decisions):
        path = self.root / PROJECTION
        require(not path.is_symlink(), "Unsafe projection file")
        expected = self.projection(decisions)
        if not path.exists() or path.read_bytes() != expected:
            atomic(path, expected)
        for name, (rows, fields) in self.legacy_projection(decisions).items():
            path = self.root / name
            require(not path.is_symlink(), "Unsafe legacy handoff file")
            payload = csv_payload(rows, fields)
            if not path.exists() or path.read_bytes() != payload:
                atomic(path, payload)

    def legacy_projection(self, decisions):
        """The two adopted consumers retain their exact existing CSV meanings."""
        profile = self.config.get("csv_profile")
        if not profile:
            return {}
        key = "object_key" if profile == "bibliothek" else "md_object_id"
        rows = []
        for original in self.legacy_rows:
            row = dict(original)
            choice = decisions[row[key]]
            require(not choice["metadata_choices"], "Legacy accession has no metadata-choice mapping")
            row.update(selected_candidate_id=choice["selected_candidate_id"],
                       decision="selected_master" if choice["selected_candidate_id"] else "unresolved",
                       note=choice["note"], actor=choice["actor"], decided_at=choice["decided_at"],
                       deletion_authorized="false")
            rows.append(row)
        dispositions = []
        for candidate in self.legacy_candidates:
            oid = candidate[key]
            selected = decisions[oid]["selected_candidate_id"]
            status = ("selected_master" if selected == candidate["candidate_id"] else "redundant") if selected else "unresolved"
            if profile == "bibliothek" and candidate["eligible"] != "true":
                status = "excluded"
            members = json.loads(candidate["members_json"])
            paths = [m["path" if profile == "bibliothek" else "source_path"] for m in members]
            dispositions.append({key: oid, "candidate_id": candidate["candidate_id"], "disposition": status,
                                 "source_paths_json": json.dumps(paths), "deletion_authorized": "false"})
        return {"master-decisions.csv": (rows, list(rows[0])),
                "candidate-dispositions.csv": (dispositions, list(dispositions[0]))}

    def legacy_agrees(self, decisions):
        for name, (expected, _) in self.legacy_projection(decisions).items():
            path = self.root / name
            require(not path.is_symlink(), "Unsafe legacy handoff file")
            if not path.exists():
                return False
            with path.open(encoding="utf-8-sig", newline="") as handle:
                if list(csv.DictReader(handle)) != expected:
                    return False
        return True

    def snapshot(self, oid=None):
        with self.lock:
            self.load()
            decisions, revision = self.read_decisions()
            if oid is not None:
                require(oid in self.by_object, "Unknown object")
                obj = json.loads(json.dumps(self.by_object[oid]))
                for candidate in obj["candidates"]:
                    problems = []
                    for member in candidate["members"]:
                        try:
                            self.checked_member(member)
                        except (OSError, ValueError, subprocess.SubprocessError) as error:
                            problems.append(str(error))
                    candidate["available"] = not problems
                    candidate["unavailable_reason"] = "; ".join(dict.fromkeys(problems))
                return {"object": obj, "decision": decisions[oid], "revision": revision, "read_only": self.frozen}
            records = []
            for key, obj in self.by_object.items():
                row = decisions[key]
                pending = [f["field"] for f in obj["metadata"] if f["field"] not in row["metadata_choices"]]
                records.append({"id": key, "title": obj.get("title", key), "inventory": obj.get("inventory", ""),
                                "selected": row["selected_candidate_id"], "metadata_pending": pending,
                                "candidate_count": len(obj["candidates"]), "note": row["note"]})
            projection = self.root / PROJECTION
            require(not projection.is_symlink(), "Unsafe projection file")
            return {"title": self.config.get("title", self.root.name), "objects": records, "revision": revision,
                    "read_only": self.frozen, "actor": self.actor,
                    "legacy_handoff": self.config.get("csv_profile"),
                    "legacy_handoff_agrees": self.legacy_agrees(decisions),
                    "projection_agrees": projection.exists() and projection.read_bytes() == self.projection(decisions)}

    def save(self, payload):
        require(isinstance(payload, dict) and set(payload) ==
                {"object_id", "selected_candidate_id", "metadata_choices", "note", "revision"}, "Invalid decision fields")
        with self.locked():
            self.load()
            require(not self.frozen, "Completed/read-only unit: decisions are frozen")
            require(not (self.root / PROJECTION).is_symlink(), "Unsafe projection file")
            decisions, revision = self.read_decisions()
            require(payload["revision"] == revision, "Evidence or decisions changed; reload before saving")
            oid = payload["object_id"]
            require(isinstance(oid, str) and oid in decisions, "Unknown object")
            # Verify all displayed members, not just the newly selected source.
            for candidate in self.by_object[oid]["candidates"]:
                for member in candidate["members"]:
                    self.checked_member(member)
            row = {key: payload[key] for key in ("object_id", "selected_candidate_id", "metadata_choices", "note")}
            require(isinstance(row["note"], str), "Note must be text")
            row["note"] = row["note"].strip()
            row.update(actor=self.actor, decided_at=datetime.now(timezone.utc).isoformat(), deletion_authorized=False)
            decisions[oid] = row
            self.validate(decisions)
            self.legacy_projection(decisions)  # Refuse an unsupported handoff before committing.
            # Essential provenance and member ordering travel with the readable choice.
            for object_id, saved in decisions.items():
                selected = self.by_candidate.get(saved["selected_candidate_id"])
                saved["selected_members"] = selected["members"] if selected else []
                saved["metadata_values"] = {
                    field["field"]: next(choice for choice in field["choices"]
                                         if choice["id"] == saved["metadata_choices"][field["field"]])
                    for field in self.by_object[object_id]["metadata"]
                    if field["field"] in saved["metadata_choices"]
                }
            # Evidence preparation may be running independently of this server.
            # Recheck after source inspection, before committing readable choices.
            self.load()
            require(not self.frozen, "Completed/read-only unit: decisions are frozen")
            _, latest_revision = self.read_decisions()
            require(latest_revision == revision, "Evidence or decisions changed; reload before saving")
            self.validate(decisions)
            # One authoritative replacement; derived output is recoverable if interrupted.
            atomic(self.root / DECISIONS, b"".join((json.dumps(decisions[k], ensure_ascii=False) + "\n").encode()
                                                  for k in sorted(decisions)))
            try:
                self.refresh_projection(decisions)
            except OSError as error:
                raise ReviewError("Choices were saved, but the derived CSV could not be refreshed. "
                                  "Reload and run --refresh-projection after fixing the write failure.") from error
        return self.snapshot(oid)

    def repair_projection(self):
        with self.locked():
            self.load()
            require(not self.frozen, "Completed/read-only unit: projection writes are disabled")
            decisions, _ = self.read_decisions()
            self.refresh_projection(decisions)

    def media(self, cid, member_index, page, edge, as_text=False):
        with self.lock:
            self.load()
            require(cid in self.by_candidate, "Unknown candidate")
            candidate = self.by_candidate[cid]
            require(0 <= member_index < len(candidate["members"]), "Unknown representation member")
            member = candidate["members"][member_index]
            require(1 <= page <= member["pages"], "Page outside representation")
            require(edge in (1200, 2400, 4000), "Unsupported preview size")
            path = self.checked_member(member)
            require(not as_text or path.suffix.lower() == ".pdf", "Text comparison requires a PDF")
            with RENDER_LIMIT:
                return render(str(path), member["size_bytes"], member["mtime_ns"], page, edge, as_text)


def checked_legacy_handoff(root, profile):
    """Read-only gate used by the existing accession consumers after adoption.

    JSONL is authority. Both consumer CSVs must be its current projections;
    incomplete recovery or an independent legacy edit blocks accession planning.
    No completion, source or archive mutation is authorized by this check.
    """
    root = Path(root)
    path = root / DECISIONS
    if not path.exists() and not path.is_symlink():
        return False  # Original, unadopted unit workflow is unchanged.
    app = Workbench(root, "Accession handoff check")
    # A successful consolidated save has already created this lock. The check
    # opens it read-only and never creates state in an accession --check path.
    fd = os.open(app.root / ".reconciliation.lock", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            app.load()
            require(app.config.get("csv_profile") == profile,
                    "No explicit consolidated handoff mapping for this accession consumer")
            decisions, revision = app.read_decisions()
            require(all(r["selected_candidate_id"] for r in decisions.values()), "Unresolved Master decisions")
            require(app.legacy_agrees(decisions),
                    "Consolidated decisions and accession CSVs disagree; refresh projections before accession")
            for candidate in app.by_candidate.values():
                for member in candidate["members"]:
                    app.checked_member(member)
            app.load()
            require(app.read_decisions()[1] == revision, "Handoff evidence changed during validation")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    return revision


@lru_cache(maxsize=12)
def render(path, size, mtime, page, edge, as_text):
    """Preview bytes only. Keys contain observed stamps; no media files are written."""
    if Path(path).suffix.lower() == ".pdf":
        args = (["pdftotext", "-f", str(page), "-l", str(page), path, "-"] if as_text else
                ["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", str(edge), "-png", path])
        result = subprocess.run(args, capture_output=True, timeout=30, check=False)
        require(result.returncode == 0, "PDF preview failed; inspect source availability and installed Poppler tools")
        return (result.stdout[:200000], "text/plain; charset=utf-8") if as_text else (result.stdout, "image/png")
    require(not as_text, "No text layer for this image")
    from PIL import Image, ImageOps
    with Image.open(path) as image:
        image.seek(page - 1)
        image.thumbnail((edge, edge))
        image = ImageOps.exif_transpose(image).convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue(), "image/jpeg"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        require(address[0] == "127.0.0.1", "Workbench only binds to localhost")
        super().__init__(address, Handler)
        self.app = app
        self.token = secrets.token_urlsafe(32)
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, body, mime="application/json; charset=utf-8", status=200):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Host") not in self.server.hosts:
            return self.send({"error": "Local workbench host required"}, status=403)
        route = urlsplit(self.path)
        query = parse_qs(route.query)
        get = lambda key, default="": query.get(key, [default])[0]
        try:
            if route.path == "/":
                self.send((HERE / "reconciliation_workbench.html").read_bytes(), "text/html; charset=utf-8")
            elif route.path in ("/reconciliation_workbench.js", "/reconciliation_workbench.css"):
                path = HERE / route.path[1:]
                self.send(path.read_bytes(), "text/javascript" if path.suffix == ".js" else "text/css")
            elif route.path == "/api/overview":
                result = self.server.app.snapshot()
                result["token"] = self.server.token
                self.send(result)
            elif route.path == "/api/object":
                self.send(self.server.app.snapshot(get("id")))
            elif route.path in ("/media", "/text"):
                body, mime = self.server.app.media(get("candidate"), int(get("member", "0")),
                                                 int(get("page", "1")), int(get("edge", "1200")), route.path == "/text")
                self.send(body, mime)
            else:
                self.send({"error": "Unknown route"}, status=404)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
            self.send({"error": str(error)}, status=400)

    def do_POST(self):
        host = self.headers.get("Host")
        if (host not in self.server.hosts or self.headers.get("Origin") != f"http://{host}" or
                not secrets.compare_digest(self.headers.get("X-Review-Token", ""), self.server.token)):
            return self.send({"error": "Use the local workbench page to save"}, status=403)
        if self.path != "/api/decision":
            return self.send({"error": "Unknown route"}, status=404)
        try:
            require(self.headers.get("Content-Type") == "application/json", "Expected JSON")
            size = int(self.headers.get("Content-Length", "0"))
            require(0 < size <= 32000 and not self.headers.get("Transfer-Encoding"), "Invalid request size")
            payload = json.loads(self.rfile.read(size))
            self.send(self.server.app.save(payload))
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
            self.send({"error": str(error)}, status=409)


def create_demo(root):
    """Synthetic, explicitly requested work area; never read archive/source data."""
    from PIL import Image, ImageDraw
    root = Path(root).absolute()
    require(not root.is_symlink(), "Demo root cannot be a symlink")
    require(not root.exists() or not any(root.iterdir()), "Demo requires a new or empty directory")
    for protected in (ELEMENTS, Path("/mnt/volume/iss-archive"), Path("/mnt/volume/archive")):
        require(not root.resolve().is_relative_to(protected), "Demo cannot write into an archive/source root")
    root.mkdir(parents=True, exist_ok=True)
    images = []
    for name, label, color in [("original.png", "Synthetic scan · full extent", "#e5d7ac"),
                               ("alternative.png", "Synthetic scan · local alternative", "#d5e2ca"),
                               ("reference.png", "Synthetic reference · not a Master", "#cddfe2")]:
        image = Image.new("RGB", (1000, 1300), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 70, 930, 1230), outline="#385a4b", width=5)
        draw.text((100, 110), label, fill="#24352e", font_size=32)
        draw.rectangle((180, 300, 800, 850), fill="#567568")
        draw.text((110, 1140), "DEMO EVIDENCE / no archive media", fill="#24352e", font_size=26)
        image.save(root / name)
        images.append(image)
    images[0].save(root / "document.pdf", save_all=True, append_images=[images[1]])

    def candidate(cid, name, label, eligible=True, role="candidate", pages=1):
        path = root / name
        st = path.stat()
        return {"id": cid, "label": label, "eligible": eligible, "complete": True, "role": role,
                "evidence": "Prepared synthetic comparison; no automatic quality ranking.",
                "members": [{"path": str(path), "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "pages": pages}]}

    data = {"format": FORMAT, "title": "Reconciliation demonstration", "read_only": False, "read_roots": [],
            "objects": [
                {"id": "demo:image", "inventory": "DEMO-001", "title": "Image with a metadata conflict",
                 "provenance": "Synthetic local scan and catalogue evidence",
                 "candidates": [candidate("scan", "original.png", "Full source scan"),
                                candidate("alternative", "alternative.png", "Local alternative"),
                                candidate("reference", "reference.png", "Access reference", False, "derivative")],
                 "metadata": [{"field": "Title", "choices": [
                     {"id": "catalogue", "value": "Factory research team", "source": "Synthetic catalogue caption"},
                     {"id": "local", "value": "Laboratory staff", "source": "Synthetic folder caption"}]}]},
                {"id": "demo:document", "inventory": "DEMO-002", "title": "Complete two-page document",
                 "candidates": [candidate("pdf", "document.pdf", "Complete PDF · 2 pages", pages=2)], "metadata": []},
                {"id": "local:pages", "inventory": "LOCAL-003", "title": "Local-only ordered page set",
                 "candidates": [{"id": "pages", "label": "Two ordered original pages", "eligible": True,
                                 "complete": True, "role": "candidate", "evidence": "One representation with ordered members.",
                                 "members": [candidate("x", "alternative.png", "")['members'][0],
                                             candidate("y", "reference.png", "")['members'][0]]}], "metadata": []}]}
    (root / "reconciliation-input.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--check", action="store_true", help="Read-only input/decision validation")
    parser.add_argument("--refresh-projection", action="store_true", help="Rebuild only the derived disposition CSV")
    parser.add_argument("--demo", action="store_true", help="Create synthetic evidence in a new/empty work root")
    args = parser.parse_args()
    if args.demo:
        create_demo(args.work_root)
    app = Workbench(args.work_root, args.actor)
    if args.refresh_projection:
        app.repair_projection()
    if args.check or args.refresh_projection:
        for candidate in app.by_candidate.values():
            for member in candidate["members"]:
                app.checked_member(member)
        print(json.dumps(app.snapshot(), ensure_ascii=False, indent=2))
        return 0
    server = Server(("127.0.0.1", args.port), app)
    print(f"Reconciliation workbench on localhost port {server.server_port}; Ctrl-C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
