#!/usr/bin/env python3
"""Local newcomer workbench for the ISS preservation archive."""

from __future__ import annotations

import argparse
import csv
import html
import http.cookies
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import archive  # noqa: E402


WORKBENCH_VERSION = "0.7.1"
MAX_FORM_BYTES = 1024 * 1024
SOURCE_SEARCH_SECONDS = 2.5
PREDICATES = {
    "primary_master": "Primary preservation master",
    "additional_master": "Additional master or duplicate occurrence",
    "complete_reference": "Complete low-resolution carrier image",
    "high_resolution_part": "High-resolution part or detail",
    "upstream_scan": "Upstream scan retained as provenance",
    "source_metadata": "Source metadata or inventory evidence",
    "documents_event": "Documents this event",
    "depicts_event": "Depicts this event",
    "source_record": "Source record for testimony",
}
CONFIDENCE = {
    "certain": "Certain",
    "probable": "Probably related",
    "possible": "Possibly related",
    "unknown": "Not known",
}
STATUS_LABELS = {
    "accepted": "Accepted",
    "rejected": "Not the same / rejected",
    "unresolved": "Leave unresolved",
    "open": "Open",
    "reviewing": "Being reviewed",
    "resolved": "Resolved",
    "accepted_unknown": "Unknown accepted as final",
    "available": "Available",
    "missing": "Missing — identity blocked",
    "preview_unavailable": "Preview unavailable",
    "observed": "Observed source assertion",
    "read_only": "Read-only source",
    "writable_observation": "Writable source location",
    "extracted": "Text extracted",
    "identified": "Source identified",
    "needs_ocr": "Needs OCR",
    "error": "Extraction error",
    "pending": "Pending extraction",
    "deferred": "Deferred",
}
IDENTITY_OUTCOMES = {
    "same_object": "Same archival object",
    "component_of_carrier": "Component of carrier",
    "wrong_master": "Wrong master linked",
    "not_same": "Not the same object",
    "defer": "Defer",
}
IDENTITY_EDITOR_LABELS = {
    "same_object": "Yes — the images show the same item",
    "component_of_carrier": "This photograph is part of the larger archive page",
    "wrong_master": "The attached archive scan belongs to another record",
    "not_same": "No — the images show different items",
    "defer": "Not decided yet",
}
METADATA_ACTIONS = {
    "keep_local": "Keep source/local value",
    "accept_external": "Accept museum-digital value",
    "corrected": "Enter corrected value",
    "defer": "Defer",
}
METADATA_FIELDS = {
    "descriptive_title": "Descriptive title",
    "description": "Description",
    "material_technique": "Material / technique",
    "dimensions": "Dimensions",
    "depicted_event": "Depicted event",
}
METADATA_EDITOR_LABELS = {
    "descriptive_title": "Title",
    "description": "Description",
    "material_technique": "Material / technique",
    "dimensions": "Dimensions",
    "depicted_event": "Date of the scene",
}
METADATA_DECISION_LABELS = {
    "keep_local": "Archive uses the preserved source value",
    "accept_external": "Archive uses the museum-digital value",
    "corrected": "Archive uses an editor-written value",
    "split_event": "Saved as an event shown in the photograph",
    "component_events": "No single carrier scene date — component dates apply",
    "defer": "Not decided yet",
}
SEVERITY_LABELS = {
    "safe": "Healthy",
    "yellow": "Review soon",
    "red": "Action required",
    "critical": "Stop and investigate",
}


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def url(route: str, **parameters: Any) -> str:
    values = {key: str(value) for key, value in parameters.items() if value not in (None, "")}
    return route + (("?" + urllib.parse.urlencode(values)) if values else "")


def bytes_label(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or suffix == "TB":
            return f"{size:.0f} {suffix}" if suffix == "B" else f"{size:.1f} {suffix}"
        size /= 1024
    return f"{value} B"


def badge(value: str, severity: str | None = None) -> str:
    css = severity or value.replace("_", "-")
    return f'<span class="badge badge--{e(css)}">{e(STATUS_LABELS.get(value, value.replace("_", " ").title()))}</span>'


def hidden(name: str, value: Any) -> str:
    return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'


def component_source_value_applicable(
    field_name: str,
    source_value: str,
    parent_source_value: str = "",
) -> bool:
    source = re.sub(r"\s+", " ", source_value).strip()
    parent = re.sub(r"\s+", " ", parent_source_value).strip()
    if not source:
        return False
    if field_name == "dimensions":
        normalized = source.casefold().replace(".", "")
        if normalized in {"a2", "din a2"}:
            return False
        if parent and source.casefold() == parent.casefold():
            return False
    if field_name == "descriptive_title":
        if parent and source.casefold() == parent.casefold():
            return False
        if source.casefold().startswith("wandzeitung betriebschronik"):
            return False
    if field_name == "depicted_event" and parent and source.casefold() == parent.casefold():
        return False
    return True


def source_registry_run_summary(root: Path, run_id: str) -> dict[str, Any]:
    if not run_id:
        return {}
    summary_path = root / "work" / "source-registry" / "runs" / run_id / "summary.json"
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def option(value: str, label: str, selected: str = "") -> str:
    marker = " selected" if value == selected else ""
    return f'<option value="{e(value)}"{marker}>{e(label)}</option>'


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root: Path, port: int, validator_python: str):
        self.archive_root = root.resolve()
        self.paths = archive.root_paths(self.archive_root)
        if self.paths["mode"] == archive.catalog_core.CATALOG_MODE:
            raise archive.ArchiveError(
                "this legacy workbench has no minimal-core adapter; use the "
                "core commands until that bounded adapter is implemented"
            )
        if not self.paths["db"].is_file():
            raise archive.ArchiveError(f"working catalog does not exist: {self.paths['db']}")
        if self.paths["legacy_writable"]:
            with archive.connect_db(self.paths["db"]) as connection:
                archive.ensure_identity_review_schema(connection)
                archive.ensure_metadata_decision_schema(connection)
        else:
            with archive.connect_db(self.paths["db"], read_only=True):
                pass
        self.validator_python = validator_python
        super().__init__(("127.0.0.1", port), WorkbenchHandler)


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def log_message(self, message: str, *arguments: Any) -> None:
        sys.stderr.write("workbench: " + (message % arguments) + "\n")

    @property
    def root(self) -> Path:
        return self.server.archive_root

    def actor(self) -> str:
        raw = self.headers.get("Cookie", "")
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(raw)
        except http.cookies.CookieError:
            return ""
        morsel = cookie.get("iss_archive_actor")
        return urllib.parse.unquote(morsel.value).strip() if morsel else ""

    def send_bytes(self, content: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def send_html(self, title: str, content: str, active: str = "") -> None:
        actor = self.actor()
        navigation = "".join(
            f'<a href="{path}" class="nav__link{" is-active" if active == key else ""}">{label}</a>'
            for key, path, label in (
                ("overview", "/", "Overview"),
                ("browse", "/browse", "Browse"),
                ("sources", "/sources", "Source registry"),
                ("intake", "/intake", "Add material"),
                ("reconcile", "/reconcile/visual", "Compare records"),
                ("health", "/health", "Archive health"),
            )
        )
        actor_html = (
            f'<span>Working as <strong>{e(actor)}</strong></span> <a href="/session">Change</a>'
            if actor
            else '<a class="button button--small" href="/session">Start a work session</a>'
        )
        document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)} · ISS Archive</title>
  <link rel="stylesheet" href="/assets/workbench.css">
</head>
<body>
  <header class="site-header">
    <a class="brand" href="/">ISS Archive <span>Workbench</span></a>
    <div class="actor">{actor_html}</div>
  </header>
  <nav class="nav" aria-label="Main navigation">{navigation}</nav>
  <main class="main">{content}</main>
  <footer class="footer">Local archive workbench {e(WORKBENCH_VERSION)} · The catalog and BagIt units remain authoritative.</footer>
</body>
</html>
"""
        self.send_bytes(document.encode("utf-8"), "text/html; charset=utf-8")

    def redirect(self, location: str, cookie: str = "") -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def query(self) -> dict[str, str]:
        parsed = urllib.parse.urlsplit(self.path)
        return {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}

    def form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise archive.ArchiveError("invalid form length") from error
        if length <= 0 or length > MAX_FORM_BYTES:
            raise archive.ArchiveError("form is empty or too large")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            raise archive.ArchiveError("unsupported form encoding")
        raw = self.rfile.read(length).decode("utf-8")
        return {key: values[-1] for key, values in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def require_actor(self) -> str:
        actor = self.actor()
        if not actor:
            raise archive.ArchiveError("start a named work session before changing the archive")
        return actor

    def db_connection(self, read_only: bool = True) -> sqlite3.Connection:
        return archive.connect_db(self.server.paths["db"], read_only=read_only)

    def registry_connection(self, read_only: bool = True) -> sqlite3.Connection:
        return archive.connect_registry_db(
            self.server.paths["registry_db"], read_only=read_only
        )

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/":
                self.page_overview()
            elif path == "/session":
                self.page_session()
            elif path == "/browse":
                self.page_browse()
            elif path == "/sources":
                self.page_source_registry()
            elif path == "/object":
                self.page_object()
            elif path == "/intake":
                self.page_intake()
            elif path == "/source-files":
                self.page_source_files()
            elif path == "/reconcile":
                self.page_reconcile()
            elif path == "/reconcile/visual":
                self.page_visual_reconciliation()
            elif path == "/reconcile/visual-image":
                self.page_visual_reconciliation_image()
            elif path == "/health":
                self.page_health()
            elif path == "/preview":
                self.page_preview()
            elif path == "/assets/workbench.css":
                self.send_bytes((SCRIPT_DIR / "workbench.css").read_bytes(), "text/css; charset=utf-8")
            elif path == "/favicon.ico":
                self.send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (archive.ArchiveError, sqlite3.Error, OSError, ValueError) as error:
            self.error_page(error)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/session":
                self.action_session()
            elif path == "/intake":
                self.action_intake()
            elif path == "/unit/create":
                self.action_create_unit()
            elif path == "/reconcile/link":
                self.action_link()
            elif path == "/reconcile/create-object":
                self.action_create_object()
            elif path == "/reconcile/add-gap":
                self.action_add_gap()
            elif path == "/reconcile/status":
                self.action_status()
            elif path == "/reconcile/identity":
                self.action_identity_review()
            elif path == "/reconcile/metadata":
                self.action_metadata_decision()
            elif path == "/health/validate":
                self.action_validate()
            elif path == "/health/seal":
                self.action_seal()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (archive.ArchiveError, sqlite3.Error, OSError, ValueError, subprocess.SubprocessError) as error:
            self.error_page(error, status=HTTPStatus.CONFLICT)

    def error_page(self, error: Exception, status: int = HTTPStatus.BAD_REQUEST) -> None:
        content = f"""
<section class="page-heading"><p class="eyebrow">Nothing was hidden</p><h1>The operation stopped safely</h1></section>
<div class="notice notice--critical"><strong>{e(error)}</strong><p>No successful archive step was rolled back or concealed. Return to the previous page and review the values.</p></div>
<p><a class="button" href="/">Return to overview</a> <a class="button button--quiet" href="/health">Check archive health</a></p>
"""
        document = f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Operation stopped · ISS Archive</title><link rel='stylesheet' href='/assets/workbench.css'></head><body><main class='main'>{content}</main></body></html>"
        self.send_bytes(document.encode("utf-8"), "text/html; charset=utf-8", status)

    def page_session(self) -> None:
        content = f"""
<section class="page-heading"><p class="eyebrow">Accountability, not login</p><h1>Who is working?</h1><p>Your name is attached to every accepted change in the archive logbook.</p></section>
<form class="panel form form--narrow" method="post" action="/session">
  <label>Your name <input name="actor" value="{e(self.actor())}" maxlength="120" required autofocus></label>
  {hidden("return_to", self.query().get("return_to", "/"))}
  <button class="button" type="submit">Start work session</button>
</form>
"""
        self.send_html("Work session", content)

    def action_session(self) -> None:
        form = self.form()
        actor = form.get("actor", "").strip()
        if not actor or len(actor) > 120 or any(char in actor for char in "\r\n\x00"):
            raise archive.ArchiveError("enter a valid name")
        destination = form.get("return_to", "/")
        if not destination.startswith("/") or destination.startswith("//"):
            destination = "/"
        value = urllib.parse.quote(actor, safe="")
        self.redirect(destination, f"iss_archive_actor={value}; Path=/; SameSite=Strict; HttpOnly")

    def page_overview(self) -> None:
        report = archive.status_report(self.root, verify_catalog=False)
        with self.db_connection() as connection:
            counts = {
                "units": connection.execute("SELECT COUNT(*) FROM units").fetchone()[0],
                "objects": connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0],
                "files": connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
                "questions": connection.execute("SELECT COUNT(*) FROM gaps WHERE status IN ('open','reviewing')").fetchone()[0]
                + connection.execute("SELECT COUNT(*) FROM relationships WHERE status = 'unresolved'").fetchone()[0]
                + connection.execute("SELECT COUNT(*) FROM identifiers WHERE status = 'unresolved'").fetchone()[0]
                + connection.execute(
                    """SELECT COUNT(*) FROM source_files sf JOIN units u ON u.unit_id = sf.unit_id
                       WHERE u.status = 'open' AND sf.status = 'active' AND NOT EXISTS (
                           SELECT 1 FROM relationships r WHERE r.object_type = 'source_file'
                           AND r.object_id = sf.source_file_id AND r.status != 'rejected'
                       )"""
                ).fetchone()[0],
            }
            units = connection.execute("SELECT * FROM units ORDER BY unit_id").fetchall()
            recent = connection.execute("SELECT * FROM logbook ORDER BY at_utc DESC, log_id DESC LIMIT 8").fetchall()
        conditions = "".join(
            f'<li><span>{badge(SEVERITY_LABELS[row["severity"]], row["severity"])}</span><div><strong>{e(row["condition"].replace("_", " ").title())}</strong><p>{e(row["detail"])}</p></div></li>'
            for row in report["conditions"]
            if row["condition"] != "status_board_generated" or len(report["conditions"]) == 1
        )
        unit_rows = "".join(
            f'<tr><td><strong>{e(row["title"])}</strong><br><span class="technical">{e(row["unit_id"])}</span></td><td>{badge(row["status"])}</td><td>{e(row["kind"])}</td></tr>'
            for row in units
        ) or '<tr><td colspan="3">No units yet.</td></tr>'
        recent_rows = "".join(
            f'<li><strong>{e(row["actor"])}</strong> {e(row["action"])} {e(row["entity_type"].replace("_", " "))}<br><span class="technical">{e(row["at_utc"])} · {e(row["entity_id"])}</span></li>'
            for row in recent
        ) or "<li>No changes recorded yet.</li>"
        notice = self.query().get("notice", "")
        content = f"""
<section class="page-heading"><p class="eyebrow">Today</p><h1>Archive overview</h1><p>Start here. The technical preservation details remain available, but ordinary work is organized as human tasks.</p></section>
{f'<div class="notice notice--safe">{e(notice)}</div>' if notice else ''}
<section class="health-banner health-banner--{e(report['overall'])}"><div><p class="eyebrow">Archive health</p><h2>{e(SEVERITY_LABELS[report['overall']])}</h2></div><a class="button button--quiet" href="/health">See checks</a></section>
<section class="stats" aria-label="Archive totals">
  <a href="/browse"><strong>{counts['objects']}</strong><span>objects to browse</span></a>
  <a href="/intake"><strong>{counts['files']}</strong><span>preserved file occurrences</span></a>
  <a href="/reconcile"><strong>{counts['questions']}</strong><span>questions to resolve</span></a>
  <div><strong>{counts['units']}</strong><span>archive units</span></div>
</section>
<div class="two-column">
  <section class="panel"><div class="section-heading"><h2>Collections</h2></div><div class="table-wrap"><table><thead><tr><th>Unit</th><th>State</th><th>Kind</th></tr></thead><tbody>{unit_rows}</tbody></table></div></section>
  <section class="panel"><div class="section-heading"><h2>Latest recorded work</h2></div><ol class="activity">{recent_rows}</ol></section>
</div>
<section class="panel"><div class="section-heading"><h2>What needs attention</h2></div><ul class="condition-list">{conditions}</ul></section>
"""
        self.send_html("Overview", content, "overview")

    def page_browse(self) -> None:
        term = self.query().get("q", "").strip()
        like = f"%{term}%"
        with self.db_connection() as connection:
            rows = connection.execute(
                """
                SELECT o.*,
                    (SELECT r.object_id FROM relationships r
                     JOIN source_files sf ON sf.source_file_id = r.object_id
                     WHERE r.subject_type = 'object' AND r.subject_id = o.object_id
                       AND r.object_type = 'source_file' AND r.status != 'rejected'
                     ORDER BY CASE r.predicate WHEN 'primary_master' THEN 0 ELSE 1 END, r.relationship_id LIMIT 1
                    ) AS preview_source_id
                FROM objects o
                WHERE ? = '' OR o.object_id LIKE ? OR o.display_identifier LIKE ? OR o.archival_label LIKE ?
                   OR EXISTS (SELECT 1 FROM identifiers i WHERE i.entity_id = o.object_id AND (i.value LIKE ? OR i.scheme LIKE ?))
                   OR EXISTS (SELECT 1 FROM relationships r JOIN source_files sf ON sf.source_file_id = r.object_id
                              WHERE r.subject_id = o.object_id AND r.object_type = 'source_file'
                                AND (sf.original_filename LIKE ? OR sf.relative_path LIKE ?))
                ORDER BY o.archival_label, o.object_id LIMIT 200
                """,
                (term, like, like, like, like, like, like, like),
            ).fetchall()
        cards = "".join(
            f"""<article class="object-card">
  <a class="object-card__image" href="{url('/object', id=row['object_id'])}">{f'<img src="{url("/preview", id=row["preview_source_id"])}" alt="">' if row['preview_source_id'] else '<span>No preview</span>'}</a>
  <div class="object-card__body"><p class="technical">{e(row['display_identifier'] or row['object_id'])}</p><h2><a href="{url('/object', id=row['object_id'])}">{e(row['archival_label'])}</a></h2><p>{e(row['object_type'])} · {badge(row['publication_status'])}</p></div>
</article>"""
            for row in rows
        ) or '<div class="empty"><h2>Nothing matched</h2><p>Try an inventory number, title, filename, or ISS identifier.</p></div>'
        content = f"""
<section class="page-heading"><p class="eyebrow">Read without changing</p><h1>Browse the archive</h1><p>Search by title, inventory number, filename, or archival ID.</p></section>
<form class="search" method="get" action="/browse"><label><span class="visually-hidden">Search</span><input type="search" name="q" value="{e(term)}" placeholder="Search objects and source files" autofocus></label><button class="button" type="submit">Search</button></form>
<p class="result-count">{len(rows)} object{'s' if len(rows) != 1 else ''}{f' matching “{e(term)}”' if term else ''}</p>
<section class="object-grid">{cards}</section>
"""
        self.send_html("Browse", content, "browse")

    def page_source_registry(self) -> None:
        query = self.query()
        term = query.get("q", "").strip()
        record_id = query.get("record", "").strip()
        like = f"%{term}%"
        inventory_term = (
            term.upper()
            if term and archive.source_registry.INVENTORY_RE.fullmatch(term.upper())
            else ""
        )
        keyword_search_stopped = False
        if not self.server.paths["registry_db"].is_file():
            content = """
<section class="page-heading"><p class="eyebrow">Evidence before decisions</p><h1>Source registry</h1><p>No separate source registry has been built yet.</p></section>
<div class="notice notice--yellow"><strong>Run the source-registry scan first.</strong><p>The authoritative archive catalog remains available and no preservation source has been changed.</p></div>
"""
            self.send_html("Source registry", content, "sources")
            return
        with self.registry_connection() as connection:
            if not archive.source_registry.table_exists(connection, "source_registry_blobs"):
                content = """
<section class="page-heading"><p class="eyebrow">Evidence before decisions</p><h1>Source registry</h1><p>The registry schema has not been installed in this working catalog yet.</p></section>
<div class="notice notice--yellow"><strong>Run the source-registry scan first.</strong><p>No preservation source has been changed.</p></div>
"""
                self.send_html("Source registry", content, "sources")
                return
            if record_id:
                record = connection.execute(
                    """
                    SELECT r.*, b.extension, b.size_bytes, b.parser AS extraction_method,
                           b.extraction_status, b.extraction_error
                    FROM source_registry_records r
                    JOIN source_registry_blobs b ON b.blob_id = r.blob_id
                    WHERE r.record_id = ?
                    """,
                    (record_id,),
                ).fetchone()
                if not record:
                    raise archive.ArchiveError("source registry record not found")
                assertions = connection.execute(
                    """
                    SELECT field_name, raw_value, status
                    FROM source_registry_assertions
                    WHERE record_id = ?
                    ORDER BY field_name, raw_value
                    """,
                    (record_id,),
                ).fetchall()
                occurrences = connection.execute(
                    """
                    SELECT o.*, roots.root_path, roots.read_only_observed
                    FROM source_registry_occurrences o
                    JOIN source_registry_roots roots ON roots.root_id = o.root_id
                    WHERE o.blob_id = ?
                    ORDER BY roots.root_kind, o.root_id, o.relative_path
                    """,
                    (record["blob_id"],),
                ).fetchall()
                try:
                    raw_fields = json.loads(record["raw_text"])
                except (TypeError, json.JSONDecodeError):
                    raw_fields = {}
                field_rows = "".join(
                    f"<tr><th scope=\"row\">{e(str(key).replace('_', ' ').title())}</th><td>{e(value or 'Not recorded')}</td></tr>"
                    for key, value in raw_fields.items()
                ) if isinstance(raw_fields, dict) else ""
                assertion_rows = "".join(
                    f"<tr><th scope=\"row\">{e(row['field_name'].replace('_', ' ').title())}</th><td>{e(row['raw_value'])}</td><td>{badge(row['status'])}</td></tr>"
                    for row in assertions
                ) or '<tr><td colspan="3">No field assertions were extracted from this record.</td></tr>'
                occurrence_rows = "".join(
                    f"""<li><strong>{e(row['original_filename'])}</strong> {badge('read_only') if row['read_only_observed'] else badge('writable_observation')}
<br><span class="technical">{e(str(Path(row['root_path']) / row['relative_path']))}</span>
<br><span class="technical">{e(row['occurrence_id'])}</span></li>"""
                    for row in occurrences
                ) or "<li>No preserved occurrence is currently registered.</li>"
                content = f"""
<section class="page-heading"><p class="eyebrow">Exact preserved evidence</p><h1>{e(record['entity_hint'] or record['record_id'])}</h1><p>This is the precise extracted source record used by the comparison workspace.</p></section>
<div class="notice notice--safe"><strong>{e(record['locator'])}</strong><p>{e(record['record_id'])} · {e(record['record_kind'].replace('_', ' '))} · {e(record['extension'] or 'file')} · {bytes_label(record['size_bytes'])}</p></div>
<div class="two-column">
  <section class="panel"><h2>Preserved occurrences</h2><p>These paths contain byte-identical copies of the source blob. They remain separate and none is deleted.</p><ul class="plain-list">{occurrence_rows}</ul></section>
  <section class="panel"><h2>Blob identity</h2><dl><dt>SHA-256 blob</dt><dd class="hash">{e(record['blob_id'])}</dd><dt>Extraction</dt><dd>{e(record['extraction_method'] or 'Not recorded')} · {badge(record['extraction_status'])}</dd></dl>{f"<p>{e(record['extraction_error'])}</p>" if record['extraction_error'] else ""}</section>
</div>
<section><div class="section-heading"><h2>Catalog assertions <span>{len(assertions)}</span></h2><p>These are evidence claims, not automatically accepted archive wording.</p></div><div class="table-wrap"><table><thead><tr><th>Field</th><th>Source value</th><th>Status</th></tr></thead><tbody>{assertion_rows}</tbody></table></div></section>
{f'<section><div class="section-heading"><h2>Complete extracted row</h2><p>All columns retained from this source record.</p></div><div class="table-wrap"><table><tbody>{field_rows}</tbody></table></div></section>' if field_rows else ''}
<p><a class="button button--quiet" href="{url('/sources', q=record['entity_hint'])}">Show all evidence for this inventory number</a> <a class="button" href="/reconcile/visual">Return to comparisons</a></p>
"""
                self.send_html("Exact source record", content, "sources")
                return
            scan_running = bool(
                connection.execute(
                    "SELECT 1 FROM source_registry_runs WHERE status='running' LIMIT 1"
                ).fetchone()
            )
            latest_complete = connection.execute(
                """SELECT * FROM source_registry_runs
                   WHERE status='complete' ORDER BY completed_at_utc DESC LIMIT 1"""
            ).fetchone()
            completed_summary = source_registry_run_summary(
                self.root,
                latest_complete["run_id"] if latest_complete else "",
            )
            counts = {
                "blobs": completed_summary.get(
                    "unique_blobs",
                    latest_complete["unique_blobs"] if latest_complete else 0,
                ),
                "occurrences": completed_summary.get(
                    "occurrences_recorded",
                    latest_complete["occurrences_recorded"] if latest_complete else 0,
                ),
                "duplicates": "scanning" if scan_running else completed_summary.get("duplicate_groups", "not counted"),
                "conflicts": "scanning" if scan_running else completed_summary.get("conflict_groups", "not counted"),
                "deferred": completed_summary.get("hashes_deferred", "not counted"),
            }
            roots = connection.execute("SELECT * FROM source_registry_roots ORDER BY root_kind, root_id").fetchall()
            runs = connection.execute("SELECT * FROM source_registry_runs ORDER BY started_at_utc DESC LIMIT 8").fetchall()
            claims = []
            sources = []
            claim_select = """SELECT a.entity_hint, a.field_name, MIN(a.raw_value) raw_value,
                          a.normalized_value, COUNT(DISTINCT a.record_id) record_count,
                          COUNT(DISTINCT r.blob_id) blob_count,
                          COUNT(DISTINCT o.occurrence_id) occurrence_count,
                          GROUP_CONCAT(DISTINCT o.original_filename) filenames,
                          CASE WHEN EXISTS (SELECT 1 FROM source_registry_assertions c
                                    WHERE c.entity_hint=a.entity_hint AND c.field_name=a.field_name
                                      AND c.normalized_value != a.normalized_value LIMIT 1)
                               THEN 1 ELSE 0 END is_conflict
                   FROM source_registry_assertions a
                   JOIN source_registry_records r ON r.record_id=a.record_id
                   LEFT JOIN source_registry_occurrences o ON o.blob_id=r.blob_id"""
            claim_group = """ GROUP BY a.entity_hint, a.field_name, a.normalized_value
                   ORDER BY is_conflict DESC, a.entity_hint, a.field_name, a.normalized_value LIMIT 240"""
            source_select = """SELECT b.*, COUNT(o.occurrence_id) occurrence_count,
                          GROUP_CONCAT(o.root_id || ':' || o.relative_path, ' | ') paths
                   FROM source_registry_blobs b JOIN source_registry_occurrences o ON o.blob_id=b.blob_id"""
            source_group = """ GROUP BY b.blob_id
                   ORDER BY CASE WHEN COUNT(o.occurrence_id) > 1 THEN 0 ELSE 1 END,
                            COUNT(o.occurrence_id) DESC, MAX(o.original_filename) LIMIT 120"""
            if inventory_term:
                claims = connection.execute(
                    claim_select + " WHERE a.entity_hint = ?" + claim_group,
                    (inventory_term,),
                ).fetchall()
                sources = connection.execute(
                    source_select
                    + """ WHERE EXISTS (SELECT 1 FROM source_registry_records sr
                                       WHERE sr.blob_id=b.blob_id AND sr.entity_hint = ?)"""
                    + source_group,
                    (inventory_term,),
                ).fetchall()
            elif term:
                deadline = time.monotonic() + SOURCE_SEARCH_SECONDS
                connection.set_progress_handler(
                    lambda: int(time.monotonic() >= deadline),
                    10_000,
                )
                try:
                    claims = connection.execute(
                        """WITH matched_assertions AS MATERIALIZED (
                               SELECT * FROM source_registry_assertions
                               WHERE entity_hint LIKE ? OR raw_value LIKE ?
                               LIMIT 480
                           )
                           SELECT a.entity_hint, a.field_name, MIN(a.raw_value) raw_value,
                                  a.normalized_value, COUNT(DISTINCT a.record_id) record_count,
                                  COUNT(DISTINCT r.blob_id) blob_count,
                                  COUNT(DISTINCT o.occurrence_id) occurrence_count,
                                  GROUP_CONCAT(DISTINCT o.original_filename) filenames,
                                  CASE WHEN EXISTS (SELECT 1 FROM source_registry_assertions c
                                            WHERE c.entity_hint=a.entity_hint AND c.field_name=a.field_name
                                              AND c.normalized_value != a.normalized_value LIMIT 1)
                                       THEN 1 ELSE 0 END is_conflict
                           FROM matched_assertions a
                           JOIN source_registry_records r ON r.record_id=a.record_id
                           LEFT JOIN source_registry_occurrences o ON o.blob_id=r.blob_id"""
                        + claim_group,
                        (like, like),
                    ).fetchall()
                    sources = connection.execute(
                        source_select
                        + """ WHERE o.original_filename LIKE ? OR o.relative_path LIKE ?"""
                        + source_group,
                        (like, like),
                    ).fetchall()
                except sqlite3.OperationalError as error:
                    if "interrupted" not in str(error).casefold():
                        raise
                    keyword_search_stopped = True
                    claims = []
                    sources = []
                finally:
                    connection.set_progress_handler(None, 0)
        root_rows = "".join(
            f'<li><strong>{e(row["root_id"])}</strong> {badge("read_only") if row["read_only_observed"] else badge("writable_observation")}<br><span class="technical">{e(row["root_path"])}</span></li>'
            for row in roots
        ) or "<li>No roots scanned yet.</li>"
        run_rows = "".join(
            f'<li><strong>{e(row["status"].title())}</strong> · {row["candidates_seen"]} candidates, {row["unique_blobs"]} unique blobs, {row["errors"]} errors<br><span class="technical">{e(row["started_at_utc"])} · {e(row["run_id"])}</span></li>'
            for row in runs
        ) or "<li>No registry scans yet.</li>"
        claim_rows = "".join(
            f'''<article class="claim-card{' claim-card--conflict' if row['is_conflict'] else ''}">
  <p class="eyebrow">{e(row['entity_hint'] or 'Unassigned source record')}</p>
  <h3>{e(row['field_name'].replace('_', ' ').title())}</h3>
  <p class="claim-card__value">{e(row['raw_value'])}</p>
  <p>{row['blob_count']} distinct source blob{'s' if row['blob_count'] != 1 else ''} · {row['occurrence_count']} preserved occurrence{'s' if row['occurrence_count'] != 1 else ''}</p>
  {badge('unresolved') if row['is_conflict'] else badge('observed')}
  <details><summary>Source filenames</summary><p class="technical">{e((row['filenames'] or '').replace(',', ', '))}</p></details>
</article>'''
            for row in claims
        ) or (
            '<div class="empty"><p>No extracted assertion matched this search.</p></div>'
            if term
            else '<div class="empty"><p>Search an inventory number or phrase to inspect assertions.</p></div>'
        )
        source_rows = "".join(
            f'''<article class="source-group">
  <div><p class="eyebrow">{e(row['extension'] or 'file')}</p><h3>{e((row['paths'] or '').split(' | ')[0].split(':', 1)[-1].rsplit('/', 1)[-1])}</h3><p>{bytes_label(row['size_bytes'])} · {badge(row['extraction_status'])}</p></div>
  <div><strong>{row['occurrence_count']} occurrence{'s' if row['occurrence_count'] != 1 else ''}</strong><p>{'Exact duplicate paths are grouped; no file was deleted.' if row['occurrence_count'] > 1 else 'One observed file occurrence.'}</p></div>
  <details><summary>All paths and digest</summary><p class="technical">{e(row['paths'])}</p><p class="hash">{e(row['blob_id'])}</p>{f'<p>{e(row["extraction_error"])}</p>' if row['extraction_error'] else ''}</details>
</article>'''
            for row in sources
        ) or (
            '<div class="empty"><p>No source file matched this search.</p></div>'
            if term
            else '<div class="empty"><p>Search to display grouped source blobs and their paths.</p></div>'
        )
        content = f"""
<section class="page-heading"><p class="eyebrow">One umbrella, no invented consensus</p><h1>Source registry</h1><p>Exact copies are grouped for reading while every path remains preserved. Repeated exports do not count as independent corroboration, and contradictions remain visible.</p></section>
<form class="search" method="get" action="/sources"><label><span class="visually-hidden">Search sources</span><input type="search" name="q" value="{e(term)}" placeholder="Inventory number, filename, phrase, or value" autofocus></label><button class="button" type="submit">Search evidence</button></form>
{('<div class="notice notice--yellow"><strong>Keyword scan stopped at the editor safety limit.</strong><p>No complete assertion result is shown. Try an exact inventory number or filename; compact full-text indexing remains required before curator handoff.</p></div>' if keyword_search_stopped else '<div class="notice notice--yellow"><strong>Keyword results are deliberately bounded.</strong><p>The first matching assertion rows are grouped so the editor remains responsive while the registry grows. Search an exact inventory number for its exhaustive entity view.</p></div>' if term and not inventory_term else '')}
<section class="stats" aria-label="Source registry totals">
  <div><strong>{counts['blobs']}</strong><span>unique source blobs</span></div>
  <div><strong>{counts['occurrences']}</strong><span>preserved occurrences</span></div>
  <div><strong>{counts['duplicates']}</strong><span>exact duplicate groups</span></div>
  <div><strong>{counts['conflicts']}</strong><span>conflicting field groups</span></div>
  <div><strong>{counts['deferred']}</strong><span>hashes deferred or failed</span></div>
</section>
<div class="two-column"><section class="panel"><h2>Scanned roots</h2><ul class="plain-list">{root_rows}</ul></section><section class="panel"><h2>Latest scans</h2><ol class="activity">{run_rows}</ol></section></div>
<section><div class="section-heading"><h2>Assertions <span>{len(claims)}</span></h2><p>Values are grouped by entity, field, and normalized value. Occurrence count is not a confidence score.</p></div><div class="claim-grid">{claim_rows}</div></section>
<section><div class="section-heading"><h2>Source files <span>{len(sources)}</span></h2><p>One card per byte-identical blob, with every known path underneath.</p></div><div class="registry-source-list">{source_rows}</div></section>
"""
        self.send_html("Source registry", content, "sources")

    def page_object(self) -> None:
        object_id = self.query().get("id", "")
        with self.db_connection() as connection:
            row = connection.execute("SELECT * FROM objects WHERE object_id = ?", (object_id,)).fetchone()
            if not row:
                raise archive.ArchiveError("object not found")
            identifiers = connection.execute("SELECT * FROM identifiers WHERE entity_type = 'object' AND entity_id = ? ORDER BY scheme, value", (object_id,)).fetchall()
            files = connection.execute(
                """SELECT sf.*, r.predicate, r.confidence, r.status AS relationship_status
                   FROM relationships r JOIN source_files sf ON sf.source_file_id = r.object_id
                   WHERE r.subject_type = 'object' AND r.subject_id = ? AND r.object_type = 'source_file'
                   ORDER BY CASE r.predicate WHEN 'primary_master' THEN 0 ELSE 1 END, sf.original_filename""",
                (object_id,),
            ).fetchall()
            gaps = connection.execute("SELECT * FROM gaps WHERE entity_type = 'object' AND entity_id = ? ORDER BY status, gap_type", (object_id,)).fetchall()
        identifier_rows = "".join(f'<li><strong>{e(item["scheme"])}</strong>: {e(item["value"])} {badge(item["status"])}</li>' for item in identifiers) or "<li>No historical identifiers recorded.</li>"
        file_cards = "".join(
            f"""<article class="evidence-card"><a href="{url('/preview', id=item['source_file_id'])}" class="evidence-card__preview"><img src="{url('/preview', id=item['source_file_id'])}" alt=""></a><div><h3>{e(item['original_filename'])}</h3><p>{e(PREDICATES.get(item['predicate'], item['predicate']))} · {badge(item['relationship_status'])}</p><p>{bytes_label(item['size_bytes'])} · {e(item['role'])}</p><details><summary>Technical evidence</summary><dl><dt>Source occurrence</dt><dd>{e(item['source_file_id'])}</dd><dt>Original path</dt><dd>{e(item['relative_path'])}</dd><dt>SHA-256</dt><dd class="hash">{e(item['file_id'])}</dd></dl></details></div></article>"""
            for item in files
        ) or '<div class="empty"><p>No source files are linked to this object.</p></div>'
        gap_rows = "".join(f'<li><strong>{e(item["gap_type"].replace("_", " ").title())}</strong> {badge(item["status"])}<p>{e(item["detail"])}</p></li>' for item in gaps) or "<li>No recorded open questions.</li>"
        content = f"""
<p><a href="/browse">← Back to browse</a></p>
<section class="page-heading"><p class="eyebrow">{e(row['display_identifier'] or row['object_id'])}</p><h1>{e(row['archival_label'])}</h1><p>{e(row['object_type'].title())} · {badge(row['status'])} · Publication {badge(row['publication_status'])}</p></section>
<div class="two-column two-column--detail"><section class="panel"><h2>Identifiers</h2><ul class="plain-list">{identifier_rows}</ul></section><section class="panel"><h2>Notes</h2><p>{e(row['note']) or 'No descriptive note.'}</p><p class="technical">Archival identity: {e(row['object_id'])}<br>Unit: {e(row['unit_id'])}</p></section></div>
<section><div class="section-heading"><h2>Preserved evidence</h2><p>Each card represents one source occurrence, even when the bytes are identical.</p></div><div class="evidence-grid">{file_cards}</div></section>
<section class="panel"><h2>Open questions and decisions</h2><ul class="condition-list">{gap_rows}</ul></section>
"""
        self.send_html(row["archival_label"], content, "browse")

    def page_intake(self) -> None:
        query = self.query()
        with self.db_connection() as connection:
            units = connection.execute("SELECT * FROM units WHERE status = 'open' ORDER BY unit_id").fetchall()
            roots = connection.execute("SELECT DISTINCT source_root_id, source_root_path FROM source_batches WHERE source_root_path != '' ORDER BY source_root_id").fetchall()
        unit_options = "".join(option(row["unit_id"], f'{row["title"]} ({row["unit_id"]})') for row in units)
        root_options = "".join(f'<option value="{e(row["source_root_path"])}">{e(row["source_root_id"])}</option>' for row in roots)
        root_links = "".join(f'<li><a href="{url("/source-files", root_id=row["source_root_id"])}">Browse {e(row["source_root_id"])}</a><br><span class="technical">{e(row["source_root_path"])}</span></li>' for row in roots)
        notice = query.get("notice", "")
        source_root = query.get("source_root", "")
        source_path = query.get("path", "")
        source_root_id = query.get("source_root_id", "archive-intake")
        content = f"""
<section class="page-heading"><p class="eyebrow">Safe, verified intake</p><h1>Add material</h1><p>The original stays untouched. The workbench hashes it, copies it, hashes the copy, and records where it came from.</p></section>
{f'<div class="notice notice--safe">{e(notice)}</div>' if notice else ''}
{'' if units else '<div class="notice notice--yellow">There is no open unit. Create a collection or year below before adding material.</div>'}
<details class="panel create-unit" {'open' if not units else ''}><summary>Create a new collection or year</summary>
  <form class="form form--compact" method="post" action="/unit/create">
    <label>Plain-language name <input name="title" placeholder="Paragon collection" required></label>
    <div class="form-grid"><label>Archive unit code <input name="unit" placeholder="WF-PARAGON" pattern="[A-Z0-9]+(?:-[A-Z0-9]+)+" required><small>Stable uppercase code, such as WF-PARAGON or ISS-EVENTS-2026.</small></label><label>Kind <select name="kind"><option value="retrospective">Historical collection</option><option value="contemporary">Current or recent events</option><option value="fixture">Practice unit — never preserve as real</option></select></label></div>
    <label>Why is this unit being opened? <input name="note" required></label>
    <button class="button button--quiet" type="submit">Create open unit</button>
  </form>
</details>
{f'<aside class="panel known-sources"><h2>Choose from a known source location</h2><ul class="plain-list">{root_links}</ul></aside>' if roots else ''}
<form class="panel form" method="post" action="/intake">
  <div class="form-grid">
    <label>Collection or year <select name="unit" required>{unit_options}</select></label>
    <label>What kind of file is it? <select name="role"><option value="master">Preservation master</option><option value="source">Source evidence, spreadsheet, export, or testimony</option></select></label>
  </div>
  <label>Source folder <input name="source_root" list="known-source-roots" value="{e(source_root)}" placeholder="/mnt/volume/archive-intake" required><datalist id="known-source-roots">{root_options}</datalist><small>The mounted folder containing the material. It will not be changed.</small></label>
  <label>File inside that folder <input name="path" value="{e(source_path)}" placeholder="Paragon/Scans/VLV-1112.tif" required><small>Choose it above, or enter the path relative to the source folder.</small></label>
  <details class="technical-fields"><summary>Technical intake details</summary><div class="form-grid"><label>Transfer group <input name="batch" value="intake-{datetime.now(UTC).date().isoformat()}" required></label><label>Source location label <input name="source_root_id" value="{e(source_root_id)}" required></label></div><label>Evidence or reference <input name="evidence" placeholder="Rsync manifest, donor note, or transfer record"></label><label>Operator note <textarea name="note" rows="2"></textarea></label></details>
  <button class="button" type="submit" {'disabled' if not units else ''}>Copy and verify file</button>
</form>
<aside class="explanation"><h2>What this does not do</h2><p>It does not rename, move, delete, convert, or write metadata into the source file. A file is not considered reconciled merely because its filename looks familiar.</p></aside>
"""
        self.send_html("Add material", content, "intake")

    def page_source_files(self) -> None:
        query = self.query()
        root_id = query.get("root_id", "")
        with self.db_connection() as connection:
            root_row = connection.execute(
                "SELECT source_root_id, source_root_path FROM source_batches WHERE source_root_id = ? AND source_root_path != '' ORDER BY created_at DESC LIMIT 1",
                (root_id,),
            ).fetchone()
        if not root_row:
            raise archive.ArchiveError("known source location not found")
        source_root = Path(root_row["source_root_path"]).resolve()
        relative = archive.safe_relative_path(query["dir"]) if query.get("dir") else PurePosixPath()
        directory = archive.require_under(source_root / Path(*relative.parts), source_root)
        if not directory.is_dir():
            raise archive.ArchiveError("source folder is not available")
        entries: list[tuple[bool, str, str]] = []
        with os.scandir(directory) as iterator:
            for index, entry in enumerate(iterator):
                if index >= 500:
                    break
                if entry.is_symlink():
                    continue
                if not (entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)):
                    continue
                entry_path = Path(entry.path)
                entry_relative = entry_path.relative_to(source_root).as_posix()
                entries.append((entry.is_dir(follow_symlinks=False), entry.name, entry_relative))
        entries.sort(key=lambda item: (not item[0], item[1].casefold()))
        rows = []
        if relative.parts:
            parent = PurePosixPath(*relative.parts[:-1])
            rows.append(f'<li class="source-entry source-entry--folder"><a href="{url("/source-files", root_id=root_id, dir=str(parent) if parent.parts else None)}">← Parent folder</a></li>')
        for is_directory, name, entry_relative in entries:
            if is_directory:
                action = f'<a href="{url("/source-files", root_id=root_id, dir=entry_relative)}">Open folder</a>'
                kind = "Folder"
            else:
                action = f'<a class="button button--small button--quiet" href="{url("/intake", source_root=source_root, path=entry_relative, source_root_id=root_id)}">Use this file</a>'
                kind = "File"
            rows.append(f'<li class="source-entry"><div><span class="eyebrow">{kind}</span><strong>{e(name)}</strong></div>{action}</li>')
        content = f"""
<p><a href="/intake">← Back to Add material</a></p>
<section class="page-heading"><p class="eyebrow">Known source location</p><h1>Choose a file</h1><p>The workbench is reading this folder only. Selecting a file returns it to the verified intake form.</p></section>
<div class="notice"><strong>{e(root_id)}</strong><p class="technical">{e(source_root)}{('/' + e(str(relative))) if relative.parts else ''}</p></div>
<ul class="source-list">{''.join(rows) or '<li class="empty">This folder contains no regular files or folders.</li>'}</ul>
"""
        self.send_html("Choose a source file", content, "intake")

    def action_create_unit(self) -> None:
        actor = self.require_actor()
        form = self.form()
        if not form.get("title", "").strip() or not form.get("note", "").strip():
            raise archive.ArchiveError("name and opening reason are required")
        result = archive.command_create_unit(SimpleNamespace(
            root=str(self.root), actor=actor, note=form.get("note", ""), unit=form.get("unit", "").strip().upper(),
            title=form.get("title", "").strip(), kind=form.get("kind", "retrospective"),
        ))
        self.redirect(url("/intake", notice=f'{result["unit_id"]} is open. Material can now be added.'))

    def action_intake(self) -> None:
        actor = self.require_actor()
        form = self.form()
        if any(not form.get(field, "").strip() for field in ("unit", "batch", "source_root_id", "source_root", "path")):
            raise archive.ArchiveError("collection, source location, transfer group, and file are required")
        result = archive.command_intake(SimpleNamespace(
            root=str(self.root), actor=actor, note=form.get("note", ""), evidence=form.get("evidence", ""),
            unit=form.get("unit", ""), batch=form.get("batch", ""), source_root_id=form.get("source_root_id", ""),
            source_root=form.get("source_root", ""), path=form.get("path", ""), role=form.get("role", "master"),
        ))
        self.redirect(url("/reconcile", source=result["source_file_id"], notice="File copied and verified. Now describe what it belongs to."))

    def unmatched_files(self, connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT sf.* FROM source_files sf JOIN units u ON u.unit_id = sf.unit_id
            WHERE u.status = 'open' AND sf.status = 'active' AND NOT EXISTS (
                SELECT 1 FROM relationships r
                WHERE r.object_type = 'source_file' AND r.object_id = sf.source_file_id AND r.status != 'rejected'
            ) ORDER BY sf.observed_at_utc, sf.source_file_id
            """
        ).fetchall()

    def visual_reconciliation_run(self, requested: str = "") -> tuple[Path, list[dict[str, str]], list[dict[str, str]]]:
        base = self.root / "work" / "reconciliation" / "museum-digital"
        if not base.is_dir():
            raise archive.ArchiveError("no museum-digital reconciliation run is available")
        candidates = sorted(
            (
                path
                for path in base.iterdir()
                if path.is_dir()
                and (path / "records.csv").is_file()
                and (path / "visual-comparison.csv").is_file()
            ),
            reverse=True,
        )
        if requested:
            if Path(requested).name != requested:
                raise archive.ArchiveError("invalid reconciliation run")
            run = archive.require_under(base / requested, base)
            if run not in candidates:
                raise archive.ArchiveError("visual reconciliation run not found")
        elif candidates:
            run = candidates[0]
        else:
            raise archive.ArchiveError("no visual reconciliation manifest is available; run the comparison with image roots first")
        with (run / "records.csv").open(encoding="utf-8", newline="") as handle:
            records = list(csv.DictReader(handle))
        with (run / "visual-comparison.csv").open(encoding="utf-8", newline="") as handle:
            images = list(csv.DictReader(handle))
        return run, records, images

    def comparison_source_registry_record(
        self,
        connection: sqlite3.Connection,
        record: dict[str, str],
        digest_cache: dict[Path, str],
    ) -> sqlite3.Row | None:
        row_number = record.get("source_record_row", "").strip()
        if not row_number:
            return None
        sheet = record.get("source_record_sheet", "").strip()
        locator = f"sheet:{sheet}/row:{row_number}" if sheet else f"row:{row_number}"
        source_path_text = record.get("source_record_file", "").strip()
        blob_id = ""
        if source_path_text:
            source_path = Path(source_path_text)
            try:
                if source_path not in digest_cache:
                    digest_cache[source_path] = archive.sha256_file(source_path)
                blob_id = f"sha256:{digest_cache[source_path]}"
            except OSError:
                blob_id = ""
        inventory = (
            record.get("source_inventory", "").strip()
            or record.get("local_inventory", "").strip()
        )
        inventories = [
            value.strip()
            for value in inventory.split(";")
            if value.strip()
        ]
        if blob_id:
            match = connection.execute(
                """
                SELECT r.*, COUNT(o.occurrence_id) AS occurrence_count,
                       GROUP_CONCAT(o.root_id || ':' || o.relative_path, ' | ') AS paths
                FROM source_registry_records r
                LEFT JOIN source_registry_occurrences o ON o.blob_id = r.blob_id
                WHERE r.blob_id = ? AND r.locator = ?
                GROUP BY r.record_id
                """,
                (blob_id, locator),
            ).fetchone()
            if match:
                return match
        if not inventories:
            return None
        placeholders = ",".join("?" for _ in inventories)
        matches = connection.execute(
            f"""
            SELECT r.*, COUNT(o.occurrence_id) AS occurrence_count,
                   GROUP_CONCAT(o.root_id || ':' || o.relative_path, ' | ') AS paths
            FROM source_registry_records r
            LEFT JOIN source_registry_occurrences o ON o.blob_id = r.blob_id
            WHERE r.entity_hint IN ({placeholders}) AND r.locator = ?
            GROUP BY r.record_id
            ORDER BY r.record_id
            LIMIT 2
            """,
            (*inventories, locator),
        ).fetchall()
        return matches[0] if len(matches) == 1 else None

    def visual_image_card(self, run: Path, row: dict[str, str]) -> str:
        role_labels = {
            "preservation_master": "Archive image",
            "complete_carrier_reference": "Complete carrier image — low resolution",
            "high_resolution_part": "High-resolution part of this carrier",
            "source_record_image": "Image from the preserved source",
            "museum_digital_snapshot": "Image currently shown by museum-digital",
        }
        source_file_id = row.get("source_file_id", "")
        if row.get("status") == "available" and source_file_id:
            image_url = url("/preview", id=source_file_id)
            full_url = image_url
        elif row.get("status") == "available" and row.get("snapshot_relpath"):
            image_url = url("/reconcile/visual-image", run=run.name, image=row.get("image_id"))
            full_url = url("/reconcile/visual-image", run=run.name, image=row.get("image_id"), full="yes")
        else:
            image_url = ""
            full_url = ""
        dimensions = " × ".join(value for value in (row.get("width", ""), row.get("height", "")) if value)
        size = bytes_label(int(row["size_bytes"])) if row.get("size_bytes", "").isdigit() else ""
        facts = " · ".join(value for value in (dimensions, size) if value)
        facts_html = f' <span class="technical">{e(facts)}</span>' if facts else ""
        media = (
            f'<a class="visual-card__image" href="{e(full_url)}"><img src="{e(image_url)}" alt="{e(row.get("label", ""))}"></a>'
            if image_url
            else '<div class="visual-card__missing"><strong>Image unavailable</strong><span>The match cannot be decided yet</span></div>'
        )
        detail = row.get("detail", "")
        return f"""<article class="visual-card visual-card--{e(row.get('status', 'missing'))}">
  <header><p class="eyebrow">{e(role_labels.get(row.get('role', ''), row.get('role', 'Image evidence')))}</p><h3>{e(row.get('label', 'Untitled image'))}</h3></header>
  {media}
  <div class="visual-card__body"><p>{badge(row.get('status', 'missing'))}{facts_html}</p>
  {f'<p>{e(detail)}</p>' if detail else ''}
  <details><summary>Show file details</summary><dl><dt>Original path</dt><dd>{e(row.get('original_path', '') or 'Not available')}</dd><dt>SHA-256</dt><dd class="hash">{e(row.get('sha256', '') or 'Not available')}</dd></dl></details></div>
</article>"""

    def page_visual_reconciliation(self) -> None:
        run, records, image_rows = self.visual_reconciliation_run(self.query().get("run", ""))
        records_by_object = {
            row.get("local_archival_id", ""): row
            for row in records
            if row.get("local_archival_id", "")
        }
        requested_object = self.query().get("object", "")
        if requested_object:
            records = [row for row in records if row.get("local_archival_id") == requested_object]
        images_by_object: dict[str, list[dict[str, str]]] = {}
        for image_row in image_rows:
            images_by_object.setdefault(image_row.get("local_archival_id", ""), []).append(image_row)
        role_order = {
            "preservation_master": 0,
            "complete_carrier_reference": 0,
            "source_record_image": 1,
            "museum_digital_snapshot": 2,
            "high_resolution_part": 3,
        }
        with self.db_connection() as connection:
            object_rows = connection.execute(
                """
                SELECT o.object_id, o.unit_id, o.display_identifier, o.object_type,
                       o.archival_label,
                       o.descriptive_title, o.description, o.material_technique, o.dimensions,
                       u.status AS unit_status
                FROM objects o JOIN units u ON u.unit_id = o.unit_id
                ORDER BY o.unit_id, o.archival_label, o.object_id
                """
            ).fetchall()
            objects_by_id = {row["object_id"]: row for row in object_rows}
            unit_status = {row["object_id"]: row["unit_status"] for row in object_rows}
            object_units = {row["object_id"]: row["unit_id"] for row in object_rows}
            objects_by_unit: dict[str, list[sqlite3.Row]] = {}
            for row in object_rows:
                objects_by_unit.setdefault(row["unit_id"], []).append(row)
            master_relationships: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                """
                SELECT r.*, sf.original_filename
                FROM relationships r
                JOIN source_files sf
                  ON r.object_type = 'source_file' AND r.object_id = sf.source_file_id
                WHERE r.subject_type = 'object'
                  AND r.predicate IN ('primary_master', 'additional_master')
                  AND r.status != 'rejected'
                ORDER BY r.subject_id, r.predicate, sf.original_filename
                """
            ):
                master_relationships.setdefault(row["subject_id"], []).append(row)
            context_events: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                """
                SELECT r.subject_id, r.predicate, e.event_id, e.title, e.starts_on,
                       e.ends_on, e.location, e.description
                FROM relationships r
                JOIN events e
                  ON r.object_type = 'event' AND r.object_id = e.event_id
                WHERE r.subject_type = 'object'
                  AND r.predicate IN ('published_in_context', 'depicts_event')
                  AND r.status = 'accepted' AND e.status = 'active'
                ORDER BY r.subject_id,
                  CASE r.predicate WHEN 'published_in_context' THEN 0 ELSE 1 END,
                  e.starts_on, e.event_id
                """
            ):
                context_events.setdefault(row["subject_id"], []).append(row)
            component_events: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                """
                SELECT carrier.object_id AS carrier_id,
                       component.object_id AS component_id,
                       component.display_identifier AS component_identifier,
                       component.archival_label AS component_label,
                       event.event_id, event.title, event.starts_on, event.ends_on
                FROM relationships membership
                JOIN objects component
                  ON membership.subject_type = 'object'
                 AND membership.subject_id = component.object_id
                JOIN objects carrier
                  ON membership.object_type = 'object'
                 AND membership.object_id = carrier.object_id
                JOIN relationships depiction
                  ON depiction.subject_type = 'object'
                 AND depiction.subject_id = component.object_id
                 AND depiction.predicate = 'depicts_event'
                 AND depiction.object_type = 'event'
                 AND depiction.status = 'accepted'
                JOIN events event
                  ON depiction.object_id = event.event_id
                 AND event.status = 'active'
                WHERE membership.predicate = 'component_of'
                  AND membership.status = 'accepted'
                ORDER BY carrier.object_id, component.display_identifier,
                         event.starts_on, event.event_id
                """
            ):
                component_events.setdefault(row["carrier_id"], []).append(row)
            component_parents: dict[str, sqlite3.Row] = {}
            carrier_children: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                """
                SELECT membership.subject_id AS component_id,
                       membership.object_id AS carrier_id,
                       component.display_identifier AS component_identifier,
                       component.archival_label AS component_label,
                       carrier.display_identifier AS carrier_identifier,
                       carrier.archival_label AS carrier_label
                FROM relationships membership
                JOIN objects component ON component.object_id = membership.subject_id
                JOIN objects carrier ON carrier.object_id = membership.object_id
                WHERE membership.subject_type = 'object'
                  AND membership.predicate = 'component_of'
                  AND membership.object_type = 'object'
                  AND membership.status = 'accepted'
                ORDER BY carrier.display_identifier, component.display_identifier
                """
            ):
                component_parents[row["component_id"]] = row
                carrier_children.setdefault(row["carrier_id"], []).append(row)
            identity_reviews: dict[tuple[str, str, str], sqlite3.Row] = {}
            if archive.source_registry.table_exists(connection, "external_identity_reviews"):
                for row in connection.execute(
                    """
                    SELECT * FROM external_identity_reviews
                    ORDER BY reviewed_at_utc DESC, rowid DESC
                    """
                ):
                    identity_reviews.setdefault(
                        (row["local_object_id"], row["external_system"], row["external_id"]),
                        row,
                    )
            metadata_decisions: dict[tuple[str, str], sqlite3.Row] = {}
            if archive.source_registry.table_exists(connection, "metadata_decisions"):
                for row in connection.execute(
                    """
                    SELECT * FROM metadata_decisions
                    ORDER BY decided_at_utc DESC, rowid DESC
                    """
                ):
                    metadata_decisions.setdefault((row["object_id"], row["field_name"]), row)
        source_registry_records: dict[str, sqlite3.Row] = {}
        digest_cache: dict[Path, str] = {}
        if self.server.paths["registry_db"].is_file():
            with self.registry_connection() as registry:
                for record in records:
                    object_id = record.get("local_archival_id", "")
                    source_record = self.comparison_source_registry_record(
                        registry,
                        record,
                        digest_cache,
                    )
                    if source_record:
                        source_registry_records[object_id] = source_record
        case_entries: list[dict[str, str]] = []
        for record in records:
            object_id = record.get("local_archival_id", "")
            evidence = sorted(images_by_object.get(object_id, []), key=lambda row: (role_order.get(row.get("role", ""), 9), row.get("label", "")))
            available_roles = {row.get("role") for row in evidence if row.get("status") == "available"}
            ready = (
                bool(
                    {"preservation_master", "complete_carrier_reference"}
                    & available_roles
                )
                and {"source_record_image", "museum_digital_snapshot"}.issubset(
                    available_roles
                )
            )
            cards = "".join(self.visual_image_card(run, row) for row in evidence)
            object_row = objects_by_id.get(object_id)
            object_context_events = context_events.get(object_id, [])
            publication_events = [
                row
                for row in object_context_events
                if row["predicate"] == "published_in_context"
            ]
            active_depicted_events = [
                row
                for row in object_context_events
                if row["predicate"] == "depicts_event"
            ]
            linked_component_events = component_events.get(object_id, [])
            component_parent = component_parents.get(object_id)
            parent_record = (
                records_by_object.get(component_parent["carrier_id"], {})
                if component_parent
                else {}
            )
            is_compound_carrier = bool(
                object_row
                and (
                    "carrier" in str(object_row["object_type"]).casefold()
                    or (
                        "complete_carrier_reference" in available_roles
                        and "high_resolution_part" in available_roles
                    )
                )
            )
            current_date_scope = "; ".join(
                filter(
                    None,
                    (
                        *(
                            f'Publication context: {row["starts_on"] or row["title"]}'
                            for row in publication_events
                        ),
                        *(
                            f'Component {row["component_identifier"] or row["component_id"]}: '
                            f'{row["starts_on"] or row["title"]}'
                            for row in linked_component_events
                        ),
                        *(
                            f'Depicted event: {row["starts_on"] or row["title"]}'
                            for row in active_depicted_events
                        ),
                    ),
                )
            )
            field_values = (
                ("descriptive_title", "Title", "source_title", "museum_digital_title", "source_to_museum_digital_title"),
                ("description", "Description", "source_description", "museum_digital_description", "source_to_museum_digital_description"),
                ("material_technique", "Material / technique", "source_material_technique", "museum_digital_material_technique", "source_to_museum_digital_material"),
                ("dimensions", "Dimensions", "source_dimensions", "museum_digital_dimensions", "source_to_museum_digital_dimensions"),
            )
            fields = tuple(
                (
                    field_name,
                    label,
                    record.get(source_key, ""),
                    object_row[field_name] if object_row else "",
                    record.get(external_key, ""),
                    record.get(comparison_key, ""),
                    (
                        not component_parent
                        or component_source_value_applicable(
                            field_name,
                            record.get(source_key, ""),
                            parent_record.get(source_key, ""),
                        )
                    ),
                )
                for field_name, label, source_key, external_key, comparison_key in field_values
            ) + (
                (
                    "depicted_event",
                    "Date scope",
                    record.get("source_date", ""),
                    current_date_scope or "Not accepted yet",
                    record.get("museum_digital_event_times", ""),
                    record.get("source_to_museum_digital_date", ""),
                    (
                        not component_parent
                        or component_source_value_applicable(
                            "depicted_event",
                            record.get("source_date", ""),
                            parent_record.get("source_date", ""),
                        )
                    ),
                ),
            )
            field_rows = "".join(
                f'<tr class="comparison--{e(comparison)}"><th scope="row">{e(label)}</th><td>{e(source or "Not recorded")}{"" if source_applicable else " — carrier-level value, not applicable to this component"}</td><td>{e(local or "Not accepted yet")}</td><td>{e(md or "Not recorded")}</td><td>{badge(comparison or "unresolved")}</td></tr>'
                for _, label, source, local, md, comparison, source_applicable in fields
            )
            state = unit_status.get(object_id, "")
            external_id = record.get("museum_digital_id", "").strip()
            current_review = identity_reviews.get((object_id, "museum-digital", external_id))
            source_record = source_registry_records.get(object_id)
            source_file_label = Path(record.get("source_record_file", "")).name
            source_location = " · ".join(
                value
                for value in (
                    source_file_label,
                    record.get("source_record_sheet", ""),
                    f"row {record.get('source_record_row', '')}" if record.get("source_record_row", "") else "",
                )
                if value
            )
            if source_record:
                registry_evidence_html = f"""
<div class="notice notice--safe"><strong>Preserved source record: {e(source_location or source_record['locator'])}</strong>
<p>Registry locator <span class="technical">{e(source_record['locator'])}</span> links this comparison to {source_record['occurrence_count']} byte-identical preserved occurrence{'s' if source_record['occurrence_count'] != 1 else ''}.</p>
<p><a class="button button--quiet button--small" href="{url('/sources', record=source_record['record_id'])}">Open exact source evidence</a></p></div>
"""
            elif record.get("source_record_status") == "exact_inventory":
                registry_evidence_html = f"""
<div class="notice notice--yellow"><strong>Preserved source row: {e(source_location or 'recorded, locator unavailable')}</strong>
<p>The comparison values remain visible, but this row is not yet linked to one unambiguous registry record.</p></div>
"""
            else:
                registry_evidence_html = ""
            identity_accepted = bool(
                current_review
                and current_review["status"] == "resolved"
                and current_review["outcome"] in {"same_object", "component_of_carrier"}
            )
            current_review_html = ""
            if current_review:
                current_review_html = (
                    '<div class="notice notice--safe"><strong>Image-match answer: '
                    + e(IDENTITY_EDITOR_LABELS.get(current_review["outcome"], current_review["outcome"]))
                    + f'</strong><details><summary>Show decision record</summary><p>{e(current_review["reason"])} · '
                    + f'{e(current_review["actor"])} · {e(current_review["reviewed_at_utc"])}</p></details></div>'
                )
            md_snapshot = next(
                (row for row in evidence if row.get("role") == "museum_digital_snapshot"),
                {},
            )
            carrier_options = "".join(
                option(
                    row["object_id"],
                    f'{row["archival_label"]} — {row["display_identifier"] or row["object_id"]}',
                )
                for row in objects_by_unit.get(object_units.get(object_id, ""), [])
                if row["object_id"] != object_id
                and "carrier" in str(row["object_type"]).casefold()
            )
            master_options = "".join(
                option(
                    row["relationship_id"],
                    f'{PREDICATES.get(row["predicate"], row["predicate"])} — {row["original_filename"]}',
                )
                for row in master_relationships.get(object_id, [])
            )
            controls_allowed = ready and state == "open" and bool(external_id)
            if controls_allowed:
                identity_hidden = (
                    hidden("run", run.name)
                    + hidden("unit", object_units.get(object_id, ""))
                    + hidden("local_object_id", object_id)
                    + hidden("external_system", "museum-digital")
                    + hidden("external_id", external_id)
                    + hidden("snapshot_digest", md_snapshot.get("sha256", ""))
                )
                simple_choices = (
                    (
                        "same_object",
                        "Yes — they show the same item",
                        "The archive image, preserved source image, and museum-digital image represent the same archival item.",
                        "Confirm same item",
                        "certain",
                    ),
                    (
                        "not_same",
                        "No — they show different items",
                        "The images should not share the same museum-digital record.",
                        "Record different items",
                        "certain",
                    ),
                    (
                        "defer",
                        "I’m not sure yet",
                        "Leave the question open. Nothing is accepted or rejected.",
                        "Decide later",
                        "unknown",
                    ),
                )
                simple_choice_html = "".join(
                    f"""<form class="decision-choice" method="post" action="/reconcile/identity">
  {identity_hidden}{hidden('outcome', outcome)}{hidden('confidence', confidence)}
  <h4>{e(title)}</h4><p>{e(description)}</p>
  <details class="decision-note"><summary>Add a note (optional)</summary><label>Editor note <textarea name="editor_note"></textarea></label></details>
  <button class="button{' button--quiet' if outcome != 'same_object' else ''}" type="submit">{e(button_label)}</button>
</form>"""
                    for outcome, title, description, button_label, confidence in simple_choices
                )
                advanced_choices = f"""<details class="decision-advanced">
  <summary>The archive scan is a larger page, or the wrong scan is attached</summary>
  <div class="decision-choice-grid">
    <form class="decision-choice" method="post" action="/reconcile/identity">
      {identity_hidden}{hidden('outcome', 'component_of_carrier')}{hidden('confidence', 'certain')}
      <h4>This photograph is part of a larger archive page</h4>
      <p>Use this when the photograph appears inside a newspaper page, sheet, or other larger item.</p>
      <label>Which larger page contains it? <select name="related_object_id" required><option value="">Choose the larger page</option>{carrier_options}</select></label>
      <details class="decision-note"><summary>Add a note (optional)</summary><label>Editor note <textarea name="editor_note"></textarea></label></details>
      <button class="button" type="submit">Link photograph to larger page</button>
    </form>
    <form class="decision-choice" method="post" action="/reconcile/identity">
      {identity_hidden}{hidden('outcome', 'wrong_master')}{hidden('confidence', 'certain')}
      <h4>The attached archive scan belongs to another record</h4>
      <p>Use this only when the archive file shown above is attached to the wrong catalog item.</p>
      <label>Which attached archive scan is wrong? <select name="target_relationship_id" required><option value="">Choose the wrong attachment</option>{master_options}</select></label>
      <details class="decision-note"><summary>Add a note (optional)</summary><label>Editor note <textarea name="editor_note"></textarea></label></details>
      <button class="button button--quiet" type="submit">Mark attachment as wrong</button>
    </form>
  </div>
</details>"""
                identity_question = f"""<section class="decision-step">
  <p class="eyebrow">Step 1</p><h3>Do these images show the same archive item?</h3>
  <p class="editor-help">Compare the pictures above. Choose the sentence that best describes what you see.</p>
  <div class="decision-choice-grid">{simple_choice_html}</div>
  {advanced_choices}
</section>"""
                identity_controls = (
                    f"<details class=\"change-decision\"><summary>Change this answer</summary>{identity_question}</details>"
                    if current_review
                    else identity_question
                )
            elif state != "open":
                identity_controls = '<p class="technical">This historical unit is sealed. Its image-match answer can be read but not changed.</p>'
            else:
                identity_controls = (
                    '<div class="notice notice--critical"><strong>The image match cannot be decided yet.</strong>'
                    '<p>A complete local image, preserved source image, and museum-digital image must all be available.</p></div>'
                )
            metadata_controls = ""
            if controls_allowed and identity_accepted:
                metadata_forms = []
                for field_name, label, source_value, local_value, external_value, _, source_applicable in fields:
                    latest = metadata_decisions.get((object_id, field_name))
                    latest_html = ""
                    if latest:
                        latest_html = (
                            f'<div class="notice notice--safe"><strong>{e(METADATA_DECISION_LABELS.get(latest["action"], latest["action"]))}</strong>'
                            f'<p>{e(latest["accepted_value"] or "No archive wording was changed.")}</p>'
                            f'<details><summary>Show decision record</summary><p>{e(latest["reason"])} · {e(latest["actor"])} · '
                            f'{e(latest["decided_at_utc"])}</p></details></div>'
                        )
                    common_hidden = (
                        hidden("run", run.name)
                        + hidden("unit", object_units.get(object_id, ""))
                        + hidden("object_id", object_id)
                        + hidden("field_name", field_name)
                    )
                    if field_name == "depicted_event":
                        date_value = external_value or source_value
                        event_title = record.get("museum_digital_title", "") or record.get("source_title", "") or "Depicted event"
                        publication_context_html = "".join(
                            f"<li><strong>{e(row['starts_on'] or 'Undated')}</strong> — {e(row['title'])}</li>"
                            for row in publication_events
                        )
                        component_context_html = "".join(
                            f"<li><strong>{e(row['component_identifier'] or row['component_id'])}: "
                            f"{e(row['starts_on'] or 'Undated')}</strong> — {e(row['title'])}</li>"
                            for row in linked_component_events
                        )
                        current_depicted_html = "".join(
                            f"<li><strong>{e(row['starts_on'] or 'Undated')}</strong> — {e(row['title'])}</li>"
                            for row in active_depicted_events
                        )
                        context_html = (
                            '<div class="notice"><strong>Dates already represented in the archive</strong><ul>'
                            + (
                                f"<li>Page or publication context<ul>{publication_context_html}</ul></li>"
                                if publication_context_html
                                else ""
                            )
                            + (
                                f"<li>Linked component photographs<ul>{component_context_html}</ul></li>"
                                if component_context_html
                                else ""
                            )
                            + (
                                f"<li>Current object-wide depicted event<ul>{current_depicted_html}</ul></li>"
                                if current_depicted_html
                                else ""
                            )
                            + "</ul></div>"
                            if publication_context_html
                            or component_context_html
                            or current_depicted_html
                            else ""
                        )
                        replace_help = (
                            '<div class="notice notice--yellow"><strong>This replaces the current object-wide scene date.</strong>'
                            '<p>The prior event remains in the decision history, but its active relationship is superseded. '
                            'Publication context and dates on linked components are not changed.</p></div>'
                            if active_depicted_events
                            else ""
                        )
                        event_form = f"""<form class="form form--compact event-editor" method="post" action="/reconcile/metadata">
    {common_hidden}{hidden('action', 'split_event')}
    <label>What happened? <input name="event_title" value="{e(event_title)}"></label>
    <label>Date of the scene <input name="starts_on" value="{e(date_value)}" placeholder="1974 or 1974-05-01"><small>Enter a year, or an exact date when it is known.</small></label>
    <details class="optional-fields"><summary>Add an end date, place, or description (optional)</summary>
      <div class="form form--compact">
        <label class="check"><input name="circa" type="checkbox" value="yes"> The date is approximate</label>
        <label>End date <input name="ends_on"><small>Only for an event lasting more than one day.</small></label>
        <label>Place <input name="location"></label>
        <label>Short event description <textarea name="event_description">{e(record.get("museum_digital_description", ""))}</textarea></label>
      </div>
    </details>
    <details class="decision-note"><summary>Add a note about this choice (optional)</summary><label>Editor note <textarea name="editor_note"></textarea></label></details>
    <button class="button" type="submit">{'Replace current scene date' if active_depicted_events else 'Save event shown in photograph'}</button>
  </form>"""
                        if is_compound_carrier:
                            metadata_forms.append(f"""<section class="field-decision" id="date-scope-{e(object_id)}">
  <p class="eyebrow">Date scope</p><h3>Does this complete carrier have one scene date?</h3>{latest_html}
  <p class="editor-help">This carrier contains several photographs. Keep its page or publication date separate and preserve pictured-event dates on the individual component records.</p>
  {context_html}
  <div class="value-choices">
    <div class="value-option"><span>Preserved source date</span><p>{e(source_value or "Not recorded")}</p></div>
    <div class="value-option"><span>Museum-digital date</span><p>{e(external_value or "Not recorded")}</p></div>
  </div>
  <form class="form form--compact" method="post" action="/reconcile/metadata">
    {common_hidden}{hidden('action', 'component_events')}
    <p><strong>Use the dates already scoped above.</strong> This resolves the carrier without assigning one misleading scene date to the whole montage.</p>
    <details class="decision-note"><summary>Add a note about this choice (optional)</summary><label>Editor note <textarea name="editor_note"></textarea></label></details>
    <button class="button" type="submit">Use publication and component dates</button>
  </form>
  <details class="decision-advanced"><summary>Record one event depicted by the entire carrier instead</summary>
    {replace_help}{event_form}
  </details>
  <form class="defer-form" method="post" action="/reconcile/metadata">{common_hidden}{hidden('action', 'defer')}<button class="button button--quiet button--small" type="submit">Decide the date later</button></form>
</section>""")
                        else:
                            metadata_forms.append(f"""<section class="field-decision" id="date-scope-{e(object_id)}">
  <p class="eyebrow">Date</p><h3>When did the scene in the photograph happen?</h3>{latest_html}
  <p class="editor-help">A year may describe when a newspaper page or retrospective was made, while another year describes what the photograph shows. Save the scene date here; page and publication dates stay separate.</p>
  {context_html}{replace_help}
  <div class="value-choices">
    <div class="value-option"><span>Preserved source date</span><p>{e(source_value or "Not recorded")}</p></div>
    <div class="value-option"><span>Museum-digital date</span><p>{e(external_value or "Not recorded")}</p></div>
  </div>
  {event_form}
  <form class="defer-form" method="post" action="/reconcile/metadata">{common_hidden}{hidden('action', 'defer')}<button class="button button--quiet button--small" type="submit">Decide the date later</button></form>
</section>""")
                    else:
                        suggested_value = (
                            local_value
                            or (source_value if source_applicable else "")
                            or external_value
                        )
                        source_button = (
                            f'<form method="post" action="/reconcile/metadata">{common_hidden}{hidden("action", "keep_local")}<button class="button button--quiet" type="submit">{"Keep current archive wording" if local_value else "Use preserved source wording"}</button></form>'
                            if local_value or (source_value and source_applicable)
                            else ""
                        )
                        external_button = (
                            f'<form method="post" action="/reconcile/metadata">{common_hidden}{hidden("action", "accept_external")}<button class="button button--quiet" type="submit">Use museum-digital wording</button></form>'
                            if external_value
                            else ""
                        )
                        metadata_forms.append(f"""<section class="field-decision">
  <p class="eyebrow">Archive wording</p><h3>{e(label)}</h3>{latest_html}
  <div class="value-choices">
    <div class="value-option"><span>{'Carrier source says — not a component value' if source_value and not source_applicable and not local_value else 'Archive now says' if local_value else 'Preserved source says'}</span><p>{e((local_value or source_value) or "Not recorded")}</p></div>
    <div class="value-option"><span>Museum-digital says</span><p>{e(external_value or "Not recorded")}</p></div>
  </div>
  <div class="field-actions">{source_button}{external_button}</div>
  <details class="write-own-value"><summary>Write a better archive version</summary>
    <form class="form form--compact" method="post" action="/reconcile/metadata">
      {common_hidden}{hidden('action', 'corrected')}
      <label>Archive wording <textarea name="accepted_value" required>{e(suggested_value)}</textarea></label>
      <label>Why is this clearer? <textarea name="editor_note"></textarea></label>
      <button class="button" type="submit">Use this archive wording</button>
    </form>
  </details>
  <form class="defer-form" method="post" action="/reconcile/metadata">{common_hidden}{hidden('action', 'defer')}<button class="button button--quiet button--small" type="submit">Decide later</button></form>
</section>""")
                metadata_controls = (
                    '<section class="metadata-editor"><p class="eyebrow">Step 2</p><h3>Choose what the archive should say</h3>'
                    '<p class="editor-help">Choose between the visible source values, or write a clearer archive version. Every choice is logged in the catalog. The image files are not changed.</p>'
                    + "".join(metadata_forms)
                    + "</section>"
                )
            elif state != "open":
                metadata_controls = '<p class="technical">This historical unit is sealed. Its archive wording can be read but not changed.</p>'
            elif not identity_accepted:
                metadata_controls = '<div class="notice"><strong>Archive wording opens after Step 1.</strong><p>First confirm whether the images show the same item, or whether the photograph belongs to a larger page.</p></div>'
            component_context_html = (
                f"""<div class="notice notice--yellow"><strong>High-resolution component of {e(component_parent['carrier_identifier'] or component_parent['carrier_id'])}</strong>
<p>This TIFF represents one photograph from the complete carrier “{e(component_parent['carrier_label'])}”. Carrier-level title, date, and DIN A2 dimensions are context only and cannot be accepted as this component's own metadata.</p></div>"""
                if component_parent
                else ""
            )
            case_html = f"""<article class="visual-case" id="{e(object_id)}">
  <header class="visual-case__heading"><div><p class="eyebrow">Record {e(record.get('local_inventory', ''))}</p><h2>{e(record.get('local_archival_label', object_id))}</h2><details><summary>Show catalog IDs</summary><p class="technical">{e(object_id)} · museum-digital {e(record.get('museum_digital_id', 'not matched'))} · unit {e(state or 'unknown')}</p></details></div>{badge('available' if ready else 'missing')}</header>
  {component_context_html}
  <div class="visual-strip">{cards or '<div class="empty">No image evidence is represented.</div>'}</div>
  <div class="notice {'notice--safe' if ready else 'notice--critical'}"><strong>{'Images are ready for review.' if ready else 'The image match cannot be decided: a required image is missing.'}</strong><p>{'Compare what is pictured. Matching inventory numbers alone do not prove that the images show the same item.' if ready else 'No catalog decision is available until a complete local image, preserved source image, and museum-digital image can all be compared.'}</p></div>
  {current_review_html}
  {identity_controls}
  {registry_evidence_html}
  <details class="comparison-fields"><summary>Show all source values in one table (optional)</summary><div class="table-wrap"><table><thead><tr><th>Field</th><th>Preserved source</th><th>Archive now</th><th>Museum-digital</th><th>Comparison</th></tr></thead><tbody>{field_rows}</tbody></table></div></details>
  {metadata_controls}
</article>"""
            case_entries.append(
                {
                    "object_id": object_id,
                    "parent_id": component_parent["carrier_id"] if component_parent else "",
                    "html": case_html,
                }
            )
        run_options = "".join(
            option(path.name, path.name, run.name)
            for path in sorted(run.parent.iterdir(), reverse=True)
            if path.is_dir() and (path / "visual-comparison.csv").is_file()
        )
        entries_by_id = {entry["object_id"]: entry for entry in case_entries}
        rendered: set[str] = set()
        grouped_cases: list[str] = []
        for entry in case_entries:
            object_id = entry["object_id"]
            if entry["parent_id"] or object_id in rendered:
                continue
            children = [
                entries_by_id[row["component_id"]]
                for row in carrier_children.get(object_id, [])
                if row["component_id"] in entries_by_id
            ]
            if children:
                grouped_cases.append(
                    f"""<section class="carrier-group">
  <header class="carrier-group__heading"><p class="eyebrow">Complete carrier and component photographs</p><h2>{e(objects_by_id[object_id]['display_identifier'] or object_id)} family</h2><p>Edit the complete A2 carrier separately from each high-resolution component TIFF.</p></header>
  <div class="carrier-group__carrier">{entry['html']}</div>
  <div class="carrier-group__components"><h3>High-resolution component photographs</h3>{''.join(child['html'] for child in children)}</div>
</section>"""
                )
                rendered.update({object_id, *(child["object_id"] for child in children)})
            else:
                grouped_cases.append(entry["html"])
                rendered.add(object_id)
        for entry in case_entries:
            if entry["object_id"] not in rendered:
                grouped_cases.append(entry["html"])
                rendered.add(entry["object_id"])
        content = f"""
<section class="page-heading"><p class="eyebrow">Archive review</p><h1>Review one archive item at a time</h1><p>Compare the pictures, answer a simple match question, then choose the wording and date scope the archive should keep.</p></section>
{f'<div class="notice notice--safe">{e(self.query().get("notice", ""))}</div>' if self.query().get("notice", "") else ''}
<div class="notice notice--yellow"><strong>What happens here</strong><ol><li>Compare the three images.</li><li>Say whether they show the same item.</li><li>Choose the archive wording and the correct date scope.</li></ol><p>Only catalog decisions are recorded. This page never moves, deletes, or rewrites an image file.</p></div>
<details class="run-tools"><summary>Choose another comparison set</summary><form class="run-picker" method="get" action="/reconcile/visual"><label>Comparison set <select name="run">{run_options}</select></label><button class="button button--quiet" type="submit">Open set</button><a href="/reconcile">Review intake questions</a></form><p class="technical">Run {e(run.name)} · {len(records)} record{'s' if len(records) != 1 else ''}</p></details>
<section class="visual-cases">{''.join(grouped_cases) or '<div class="empty"><h2>No reconciliation candidates</h2></div>'}</section>
"""
        self.send_html("Visual reconciliation", content, "reconcile")

    def page_visual_reconciliation_image(self) -> None:
        run, _, image_rows = self.visual_reconciliation_run(self.query().get("run", ""))
        image_id = self.query().get("image", "")
        row = next((item for item in image_rows if item.get("image_id") == image_id), None)
        if not row or row.get("status") != "available":
            raise archive.ArchiveError("comparison image is not available")
        key = "evidence_relpath" if self.query().get("full") == "yes" and row.get("evidence_relpath") else "snapshot_relpath"
        relative = archive.safe_relative_path(row.get(key, ""))
        path = archive.require_under(run / Path(*relative.parts), run)
        if not path.is_file():
            raise archive.ArchiveError("comparison image snapshot is missing")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_bytes(path.read_bytes(), content_type)

    def page_reconcile(self) -> None:
        selected_source = self.query().get("source", "")
        notice = self.query().get("notice", "")
        with self.db_connection() as connection:
            files = self.unmatched_files(connection)
            entities: dict[str, list[tuple[str, str, str]]] = {}
            for unit in connection.execute("SELECT unit_id FROM units WHERE status = 'open'"):
                values: list[tuple[str, str, str]] = []
                values.extend(("object", row["object_id"], row["archival_label"]) for row in connection.execute("SELECT object_id, archival_label FROM objects WHERE unit_id = ? ORDER BY archival_label", (unit["unit_id"],)))
                values.extend(("event", row["event_id"], row["title"]) for row in connection.execute("SELECT event_id, title FROM events WHERE unit_id = ? ORDER BY title", (unit["unit_id"],)))
                values.extend(("testimony", row["testimony_id"], f'Testimony by {row["contributor"]}') for row in connection.execute("SELECT testimony_id, contributor FROM testimonies WHERE unit_id = ? ORDER BY contributor", (unit["unit_id"],)))
                entities[unit["unit_id"]] = values
            gaps = connection.execute("SELECT g.*, u.status AS unit_status FROM gaps g JOIN units u ON u.unit_id = g.unit_id WHERE g.status IN ('open','reviewing') ORDER BY g.status, g.gap_type").fetchall()
            unresolved_relationships = connection.execute("SELECT r.*, u.status AS unit_status FROM relationships r JOIN units u ON u.unit_id = r.unit_id WHERE r.status = 'unresolved' ORDER BY r.relationship_id").fetchall()
            unresolved_identifiers = connection.execute("SELECT i.*, u.status AS unit_status FROM identifiers i JOIN units u ON u.unit_id = i.unit_id WHERE i.status = 'unresolved' ORDER BY i.scheme, i.value").fetchall()
        file_cards = []
        for row in files:
            entity_options = "".join(option(f"{kind}:{identifier}", f"{label} — {identifier}") for kind, identifier, label in entities.get(row["unit_id"], []))
            prominent = " is-prominent" if row["source_file_id"] == selected_source else ""
            default_predicate = "primary_master" if row["role"] == "master" else "source_metadata"
            predicate_options = "".join(option(value, label, default_predicate) for value, label in PREDICATES.items())
            confidence_options = "".join(option(value, label, "certain") for value, label in CONFIDENCE.items())
            file_cards.append(f"""<article class="question-card{prominent}" id="{e(row['source_file_id'])}">
  <div class="question-card__media"><img src="{url('/preview', id=row['source_file_id'])}" alt=""></div>
  <div class="question-card__content"><p class="eyebrow">New {e(row['role'])}</p><h2>{e(row['original_filename'])}</h2><p>{bytes_label(row['size_bytes'])} · from <span class="technical">{e(row['relative_path'])}</span></p>
    <form class="form form--compact" method="post" action="/reconcile/link">{hidden('source_file_id', row['source_file_id'])}{hidden('unit', row['unit_id'])}
      <label>What does this file belong to? <select name="entity" required><option value="">Choose an existing object, event, or testimony</option>{entity_options}</select></label>
      <div class="form-grid"><label>Connection <select name="predicate">{predicate_options}</select></label><label>How sure are you? <select name="confidence">{confidence_options}</select></label></div>
      <label>Evidence or reason <input name="evidence" placeholder="Spreadsheet row, filename, inscription, or personal knowledge"></label>
      <button class="button" type="submit">Connect to existing record</button>
    </form>
    <details><summary>Create a new object for this file</summary><form class="form form--compact" method="post" action="/reconcile/create-object">{hidden('source_file_id', row['source_file_id'])}{hidden('unit', row['unit_id'])}
      <div class="form-grid"><label>Known inventory number <input name="display_identifier"></label><label>Kind of object <input name="object_type" value="photograph" required></label></div>
      <label>Plain-language title <input name="label" required></label><div class="form-grid"><label>Year the ISS ID is assigned <input type="number" name="assignment_year" value="{datetime.now(UTC).year}" min="1900" max="2200" required></label><label>May it be published? <select name="publication_status"><option value="unknown">Not known</option><option value="allowed">Allowed</option><option value="blocked">Blocked</option></select></label></div>
      <label>Evidence or reason <input name="evidence"></label><button class="button" type="submit">Create object and connect file</button>
    </form></details>
    <details><summary>Leave an explicit open question</summary><form class="form form--compact" method="post" action="/reconcile/add-gap">{hidden('source_file_id', row['source_file_id'])}{hidden('unit', row['unit_id'])}<label>What is unknown? <textarea name="detail" required></textarea></label><label>Suggested next step <input name="next_action"></label><button class="button button--quiet" type="submit">Record open question</button></form></details>
  </div>
</article>""")
        gap_cards = "".join(self.status_card("gap", row["gap_id"], row["unit_id"], row["unit_status"], row["gap_type"].replace("_", " ").title(), row["detail"], row["status"], ("reviewing", "resolved", "accepted_unknown")) for row in gaps)
        relationship_cards = "".join(self.status_card("relationship", row["relationship_id"], row["unit_id"], row["unit_status"], PREDICATES.get(row["predicate"], row["predicate"]), f'{row["subject_type"]} {row["subject_id"]} → {row["object_type"]} {row["object_id"]}', row["status"], ("accepted", "rejected")) for row in unresolved_relationships)
        identifier_cards = "".join(self.status_card("identifier", row["identifier_id"], row["unit_id"], row["unit_status"], f'{row["scheme"]}: {row["value"]}', f'{row["entity_type"]} {row["entity_id"]}', row["status"], ("accepted", "rejected")) for row in unresolved_identifiers)
        content = f"""
<section class="page-heading"><p class="eyebrow">Uncertainty is allowed</p><h1>Resolve questions</h1><p>Connect evidence when you know enough. If you do not know, leave the question visible instead of guessing.</p></section>
{f'<div class="notice notice--safe">{e(notice)}</div>' if notice else ''}
<div class="notice"><strong>Metadata or identity conflict?</strong><p>Use the side-by-side visual workspace before accepting external identifiers or descriptive changes.</p><a class="button button--quiet" href="/reconcile/visual">Open visual comparison</a></div>
<section><div class="section-heading"><h2>Files waiting for a connection <span>{len(files)}</span></h2><p>Each file occurrence needs a documented relationship to an object, event, or source record.</p></div><div class="question-list">{''.join(file_cards) or '<div class="empty"><h3>No unmatched files</h3><p>Every file in an open unit has at least one recorded connection.</p></div>'}</div></section>
<section><div class="section-heading"><h2>Open questions <span>{len(gaps)}</span></h2></div><div class="decision-grid">{gap_cards or '<div class="empty"><p>No open gaps.</p></div>'}</div></section>
<section><div class="section-heading"><h2>Unresolved connections <span>{len(unresolved_relationships)}</span></h2></div><div class="decision-grid">{relationship_cards or '<div class="empty"><p>No unresolved connections.</p></div>'}</div></section>
<section><div class="section-heading"><h2>Unresolved identifiers <span>{len(unresolved_identifiers)}</span></h2></div><div class="decision-grid">{identifier_cards or '<div class="empty"><p>No unresolved identifiers.</p></div>'}</div></section>
"""
        self.send_html("Resolve questions", content, "reconcile")

    def status_card(self, kind: str, record_id: str, unit_id: str, unit_status: str, title: str, detail: str, current: str, choices: tuple[str, ...]) -> str:
        buttons = "".join(f'<button class="button button--small button--quiet" name="status" value="{e(value)}" type="submit">{e(STATUS_LABELS[value])}</button>' for value in choices)
        controls = f'<form class="button-row" method="post" action="/reconcile/status">{hidden("kind", kind)}{hidden("record_id", record_id)}{hidden("unit", unit_id)}{buttons}</form>' if unit_status == "open" else '<p class="technical">Sealed unit: retained as recorded.</p>'
        return f'<article class="decision-card"><p class="technical">{e(record_id)}</p><h3>{e(title)}</h3><p>{e(detail)}</p><p>{badge(current)}</p>{controls}</article>'

    def source_row(self, source_file_id: str, open_only: bool = True) -> sqlite3.Row:
        with self.db_connection() as connection:
            row = connection.execute("SELECT sf.*, u.status AS unit_status FROM source_files sf JOIN units u ON u.unit_id = sf.unit_id WHERE sf.source_file_id = ?", (source_file_id,)).fetchone()
        if not row:
            raise archive.ArchiveError("source file record not found")
        if open_only and row["unit_status"] != "open":
            raise archive.ArchiveError("sealed units are read-only")
        return row

    def action_link(self) -> None:
        actor = self.require_actor()
        form = self.form()
        source = self.source_row(form.get("source_file_id", ""))
        entity = form.get("entity", "").split(":", 1)
        if len(entity) != 2 or entity[0] not in ("object", "event", "testimony"):
            raise archive.ArchiveError("choose a valid archive record")
        result = archive.command_link(SimpleNamespace(
            root=str(self.root), actor=actor, note="Linked through newcomer workbench", evidence=form.get("evidence", ""), unit=source["unit_id"],
            subject_type=entity[0], subject_id=entity[1], predicate=form.get("predicate", ""), object_type="source_file", object_id=source["source_file_id"],
            status="accepted" if form.get("confidence") == "certain" else "unresolved", confidence=form.get("confidence", "unknown"),
        ))
        self.redirect(url("/reconcile", notice=f'Connection recorded as {result["relationship_id"]}.'))

    def action_create_object(self) -> None:
        actor = self.require_actor()
        form = self.form()
        if not form.get("label", "").strip() or not form.get("object_type", "").strip():
            raise archive.ArchiveError("object title and kind are required")
        source = self.source_row(form.get("source_file_id", ""))
        created = archive.command_create_object(SimpleNamespace(
            root=str(self.root), actor=actor, note="Created through newcomer workbench", evidence=form.get("evidence", ""), unit=source["unit_id"],
            assignment_year=int(form.get("assignment_year", "0")), display_identifier=form.get("display_identifier", ""), object_type=form.get("object_type", ""),
            label=form.get("label", ""), publication_status=form.get("publication_status", "unknown"), status="active",
        ))
        archive.command_link(SimpleNamespace(
            root=str(self.root), actor=actor, note="Initial file connection from newcomer workbench", evidence=form.get("evidence", ""), unit=source["unit_id"],
            subject_type="object", subject_id=created["object_id"], predicate="primary_master" if source["role"] == "master" else "source_metadata",
            object_type="source_file", object_id=source["source_file_id"], status="accepted", confidence="certain",
        ))
        self.redirect(url("/object", id=created["object_id"], notice="Object created and file connected."))

    def action_add_gap(self) -> None:
        actor = self.require_actor()
        form = self.form()
        if not form.get("detail", "").strip():
            raise archive.ArchiveError("describe what remains unknown")
        source = self.source_row(form.get("source_file_id", ""))
        archive.command_add_gap(SimpleNamespace(
            root=str(self.root), actor=actor, note="Recorded through newcomer workbench", evidence="operator review", unit=source["unit_id"],
            entity_type="source_file", entity_id=source["source_file_id"], gap_type="unmatched_source", detail=form.get("detail", ""),
            next_action=form.get("next_action", ""), status="open",
        ))
        self.redirect(url("/reconcile", notice="The uncertainty is recorded and remains visible."))

    def action_status(self) -> None:
        actor = self.require_actor()
        form = self.form()
        specifications = {
            "gap": ("gaps", "gap_id", {"reviewing", "resolved", "accepted_unknown"}),
            "relationship": ("relationships", "relationship_id", {"accepted", "rejected"}),
            "identifier": ("identifiers", "identifier_id", {"accepted", "rejected"}),
        }
        kind = form.get("kind", "")
        if kind not in specifications:
            raise archive.ArchiveError("unsupported decision type")
        table, key, allowed = specifications[kind]
        status = form.get("status", "")
        if status not in allowed:
            raise archive.ArchiveError("unsupported decision")
        with self.db_connection(read_only=False) as connection:
            unit = archive.require_unit(connection, form.get("unit", ""), open_only=True)
            row = connection.execute(f'SELECT * FROM "{table}" WHERE "{key}" = ? AND unit_id = ?', (form.get("record_id", ""), unit["unit_id"])).fetchone()
            if not row:
                raise archive.ArchiveError("decision record not found")
            archive.set_context(connection, actor, evidence="newcomer workbench decision", note=f"Set {kind} to {status}")
            connection.execute(f'UPDATE "{table}" SET status = ? WHERE "{key}" = ?', (status, row[key]))
            connection.commit()
        self.redirect(url("/reconcile", notice=f'{kind.title()} marked “{STATUS_LABELS[status]}”.'))

    def action_identity_review(self) -> None:
        actor = self.require_actor()
        form = self.form()
        run = form.get("run", "")
        outcome = form.get("outcome", "")
        reasons = {
            "same_object": "Editor confirmed that the archive image, preserved source image, and museum-digital image show the same archival item.",
            "component_of_carrier": "Editor confirmed that this photograph is contained in the selected larger archive page.",
            "wrong_master": "Editor confirmed that the selected archive scan is attached to the wrong catalog record.",
            "not_same": "Editor confirmed that the compared images do not show the same archival item.",
            "defer": "Editor could not determine the image match from the available pictures.",
        }
        reason = reasons.get(outcome, form.get("reason", "")).strip()
        editor_note = form.get("editor_note", "").strip()
        if editor_note:
            reason = f"{reason} Editor note: {editor_note}"
        result = archive.command_review_external_identity(
            SimpleNamespace(
                root=str(self.root),
                actor=actor,
                note="",
                evidence=f"visual reconciliation run {run}",
                unit=form.get("unit", ""),
                local_object_id=form.get("local_object_id", ""),
                external_system=form.get("external_system", ""),
                external_id=form.get("external_id", ""),
                snapshot_digest=form.get("snapshot_digest", ""),
                outcome=outcome,
                related_object_id=form.get("related_object_id", ""),
                target_relationship_id=form.get("target_relationship_id", ""),
                confidence=form.get("confidence", "certain"),
                reason=reason,
            )
        )
        label = IDENTITY_EDITOR_LABELS.get(result["outcome"], result["outcome"])
        self.redirect(
            url(
                "/reconcile/visual",
                run=run,
                object=form.get("local_object_id", ""),
                notice=f'Image-match answer recorded: “{label}”.',
            )
        )

    def action_metadata_decision(self) -> None:
        actor = self.require_actor()
        form = self.form()
        run_name = form.get("run", "")
        _, records, image_rows = self.visual_reconciliation_run(run_name)
        object_id = form.get("object_id", "")
        record = next(
            (row for row in records if row.get("local_archival_id") == object_id),
            None,
        )
        if not record:
            raise archive.ArchiveError("metadata comparison record is unavailable")
        object_images = [
            row for row in image_rows if row.get("local_archival_id") == object_id
        ]
        available_roles = {
            row.get("role") for row in object_images if row.get("status") == "available"
        }
        if not (
            {"preservation_master", "complete_carrier_reference"} & available_roles
            and {"source_record_image", "museum_digital_snapshot"}.issubset(
                available_roles
            )
        ):
            raise archive.ArchiveError("metadata decision requires all visual evidence roles")
        md_snapshot = next(
            row
            for row in object_images
            if row.get("role") == "museum_digital_snapshot"
            and row.get("status") == "available"
        )
        field_name = form.get("field_name", "")
        field_sources = {
            "descriptive_title": ("source_title", "museum_digital_title"),
            "description": ("source_description", "museum_digital_description"),
            "material_technique": (
                "source_material_technique",
                "museum_digital_material_technique",
            ),
            "dimensions": ("source_dimensions", "museum_digital_dimensions"),
        }
        with self.db_connection() as connection:
            archive_object = connection.execute(
                """
                SELECT descriptive_title, description, material_technique, dimensions
                FROM objects WHERE object_id = ? AND unit_id = ?
                """,
                (object_id, form.get("unit", "")),
            ).fetchone()
            component_parent = connection.execute(
                """
                SELECT object_id AS carrier_id
                FROM relationships
                WHERE unit_id = ? AND subject_type = 'object' AND subject_id = ?
                  AND predicate = 'component_of' AND object_type = 'object'
                  AND status = 'accepted'
                ORDER BY relationship_id
                LIMIT 1
                """,
                (form.get("unit", ""), object_id),
            ).fetchone()
        if not archive_object:
            raise archive.ArchiveError("metadata object is unavailable")
        action = form.get("action", "")
        if action == "accept_external":
            if field_name not in field_sources:
                raise archive.ArchiveError("external value cannot be accepted for this field")
            accepted_value = record.get(field_sources[field_name][1], "")
        elif action == "keep_local":
            if field_name not in field_sources:
                raise archive.ArchiveError("source/local value cannot be accepted for this field")
            source_value = record.get(field_sources[field_name][0], "")
            parent_record = (
                next(
                    (
                        row
                        for row in records
                        if row.get("local_archival_id")
                        == component_parent["carrier_id"]
                    ),
                    {},
                )
                if component_parent
                else {}
            )
            parent_source_value = parent_record.get(
                field_sources[field_name][0],
                "",
            )
            if (
                component_parent
                and not archive_object[field_name]
                and not component_source_value_applicable(
                    field_name,
                    source_value,
                    parent_source_value,
                )
            ):
                raise archive.ArchiveError(
                    "carrier-level source metadata cannot be accepted as component metadata"
                )
            accepted_value = (
                archive_object[field_name]
                or source_value
            )
        elif action == "corrected":
            accepted_value = form.get("accepted_value", "")
        else:
            accepted_value = ""
        if (
            component_parent
            and field_name == "dimensions"
            and accepted_value.strip().casefold().replace(".", "") in {"a2", "din a2"}
        ):
            raise archive.ArchiveError(
                "DIN A2 describes the complete carrier, not this component"
            )
        field_label = METADATA_EDITOR_LABELS.get(field_name, field_name)
        reasons = {
            "keep_local": f"Editor selected the preserved source value for {field_label.lower()}.",
            "accept_external": f"Editor selected the museum-digital value for {field_label.lower()}.",
            "corrected": f"Editor entered a corrected archive value for {field_label.lower()}.",
            "defer": f"Editor deferred the {field_label.lower()} decision.",
            "split_event": "Editor recorded this date as an event shown in the photograph, separate from page or publication context.",
            "component_events": "Editor confirmed that the carrier has no single depicted-event date; publication context and linked component dates remain separately authoritative.",
        }
        reason = reasons.get(action, form.get("reason", "")).strip()
        editor_note = form.get("editor_note", "").strip()
        if editor_note:
            reason = f"{reason} Editor note: {editor_note}"
        starts_on = form.get("starts_on", "")
        assignment_year_raw = form.get("assignment_year", "").strip()
        year_match = re.search(r"\b([12][0-9]{3})\b", starts_on)
        assignment_year = int(
            assignment_year_raw
            or (year_match.group(1) if year_match else str(datetime.now(UTC).year))
        )
        result = archive.command_decide_metadata(
            SimpleNamespace(
                root=str(self.root),
                actor=actor,
                note="",
                evidence=f"visual reconciliation run {run_name}",
                unit=form.get("unit", ""),
                object_id=object_id,
                field_name=field_name,
                action=action,
                accepted_value=accepted_value,
                external_system="museum-digital",
                external_id=record.get("museum_digital_id", ""),
                snapshot_digest=md_snapshot.get("sha256", ""),
                reason=reason,
                assignment_year=assignment_year,
                event_title=form.get("event_title", ""),
                starts_on=starts_on,
                ends_on=form.get("ends_on", ""),
                circa=form.get("circa", "") == "yes",
                location=form.get("location", ""),
                event_description=form.get("event_description", ""),
                confidence="certain",
            )
        )
        label = METADATA_EDITOR_LABELS.get(result["field_name"], result["field_name"])
        if result["status"] == "deferred":
            notice = f"{label} decision deferred."
        elif result["action"] == "component_events":
            notice = "Carrier date scope saved to the archive catalog."
        else:
            notice = f"{label} saved to the archive catalog."
        self.redirect(
            url(
                "/reconcile/visual",
                run=run_name,
                object=object_id,
                notice=notice,
            )
        )

    def page_health(self) -> None:
        report = archive.status_report(self.root, verify_catalog=False)
        with self.db_connection() as connection:
            units = connection.execute("SELECT * FROM units ORDER BY unit_id").fetchall()
            preservation_root = connection.execute("SELECT value FROM meta WHERE key = 'preservation_root'").fetchone()
        conditions = "".join(f'<li><span>{badge(SEVERITY_LABELS[row["severity"]], row["severity"])}</span><div><strong>{e(row["condition"].replace("_", " ").title())}</strong><p>{e(row["detail"])}</p></div></li>' for row in report["conditions"])
        unit_rows = "".join(
            f'<article class="decision-card"><h3>{e(row["title"])}</h3><p class="technical">{e(row["unit_id"])}</p><p>{badge(row["status"])}</p><form method="post" action="/health/validate">{hidden("unit", row["unit_id"])}<button class="button button--small button--quiet" type="submit" {"" if row["status"] == "sealed" else "disabled"}>Verify preserved copies</button></form></article>'
            for row in units
        )
        open_options = "".join(option(row["unit_id"], f'{row["title"]} ({row["unit_id"]})') for row in units if row["status"] == "open")
        content = f"""
<section class="page-heading"><p class="eyebrow">Plain-language checks</p><h1>Archive health</h1><p>Green means the current interactive checks passed. Warnings stay visible until their cause is addressed. The full catalog integrity check remains part of the deliberate command-line preservation check so page navigation never launches a scan of the entire catalog.</p></section>
<section class="health-banner health-banner--{e(report['overall'])}"><div><p class="eyebrow">Current result</p><h2>{e(SEVERITY_LABELS[report['overall']])}</h2><p>Checked {e(report['checked_at_utc'])}</p></div></section>
<section class="panel"><h2>Checks</h2><ul class="condition-list">{conditions}</ul></section>
<section><div class="section-heading"><h2>Verify sealed units</h2></div><div class="decision-grid">{unit_rows}</div></section>
<section class="panel"><div class="section-heading"><h2>Seal a reviewed unit</h2><p>This freezes the unit and creates a verified copy on another filesystem device. The current pilot cannot reopen it.</p></div>
  <form class="form" method="post" action="/health/seal"><label>Reviewed open unit <select name="unit" required><option value="">Choose unit</option>{open_options}</select></label><label>Independent preservation location <input name="preservation_root" value="{e(preservation_root[0] if preservation_root else '')}" required></label><label>Final operator note <textarea name="note" required></textarea></label><label class="check"><input type="checkbox" name="confirmed" value="yes" required> I have reviewed this unit and understand that this pilot cannot reopen it.</label><button class="button" type="submit" {'disabled' if not open_options else ''}>Seal and verify both copies</button></form>
</section>
"""
        self.send_html("Archive health", content, "health")

    def action_validate(self) -> None:
        form = self.form()
        unit_id = form.get("unit", "")
        with self.db_connection() as connection:
            unit = archive.require_unit(connection, unit_id)
            if unit["status"] != "sealed":
                raise archive.ArchiveError("only sealed units can be verified here")
            copy = connection.execute("SELECT * FROM preservation_copies WHERE unit_id = ?", (unit_id,)).fetchone()
        primary = archive.validate_bag(self.server.paths["units"] / unit_id, self.server.validator_python)
        if not copy:
            raise archive.ArchiveError("independent preservation copy is not recorded")
        preservation = archive.validate_bag(Path(copy["bag_path"]), self.server.validator_python)
        if primary["manifest_sha256"] != preservation["manifest_sha256"] or primary["tagmanifest_sha256"] != preservation["tagmanifest_sha256"]:
            raise archive.ArchiveError("primary and preservation copy manifests differ")
        content = f"""<section class="page-heading"><p class="eyebrow">Mechanical verification</p><h1>Both copies passed</h1><p>{e(unit_id)} was checked independently at both recorded locations.</p></section><div class="notice notice--safe"><strong>Primary and preservation manifests match.</strong><p class="hash">{e(primary['manifest_sha256'])}</p></div><p><a class="button" href="/health">Return to archive health</a></p>"""
        self.send_html("Verification passed", content, "health")

    def action_seal(self) -> None:
        actor = self.require_actor()
        form = self.form()
        if form.get("confirmed") != "yes":
            raise archive.ArchiveError("final review confirmation is required")
        result = archive.command_seal(SimpleNamespace(
            root=str(self.root), actor=actor, note=form.get("note", ""), unit=form.get("unit", ""),
            preservation_root=form.get("preservation_root", ""), validator_python=self.server.validator_python,
        ))
        self.redirect(url("/", notice=f'{result["unit_id"]} sealed and verified at both locations.'))

    def source_payload_path(self, row: sqlite3.Row) -> Path:
        if row["unit_status"] == "sealed" and row["archive_path"]:
            relative = archive.safe_relative_path(row["archive_path"])
            return archive.require_under(self.server.paths["units"] / row["unit_id"] / Path(*relative.parts), self.server.paths["units"] / row["unit_id"])
        relative = archive.safe_relative_path(row["intake_relpath"])
        return archive.require_under(self.root / Path(*relative.parts), self.root)

    def page_preview(self) -> None:
        row = self.source_row(self.query().get("id", ""), open_only=False)
        source = self.source_payload_path(row)
        if not source.is_file():
            raise archive.ArchiveError("preserved file is missing")
        thumbnails = self.server.paths["derived"] / "workbench-thumbnails"
        thumbnails.mkdir(parents=True, exist_ok=True)
        output = thumbnails / f'{row["source_file_id"]}.jpg'
        if not output.is_file():
            magick = shutil.which("magick") or shutil.which("convert")
            if magick:
                temporary = output.with_suffix(".partial.jpg")
                result = subprocess.run(
                    [magick, f"{source}[0]", "-auto-orient", "-thumbnail", "960x960>", "-strip", "-quality", "82", str(temporary)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=90,
                )
                if result.returncode == 0 and temporary.is_file():
                    temporary.replace(output)
                else:
                    temporary.unlink(missing_ok=True)
        if output.is_file():
            self.send_bytes(output.read_bytes(), "image/jpeg")
            return
        label = e(row["original_filename"])
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="640" viewBox="0 0 960 640"><rect width="960" height="640" fill="#e8e5df"/><text x="480" y="300" text-anchor="middle" font-family="sans-serif" font-size="30" fill="#34322f">Preview unavailable</text><text x="480" y="350" text-anchor="middle" font-family="sans-serif" font-size="20" fill="#68645e">{label}</text></svg>'
        self.send_bytes(svg.encode("utf-8"), "image/svg+xml; charset=utf-8")


def create_server(root: Path, port: int = 8765, validator_python: str = sys.executable) -> WorkbenchServer:
    return WorkbenchServer(root, port, validator_python)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", required=True, help="Archive root containing work/catalog.sqlite"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--validator-python", default=sys.executable)
    parser.add_argument("--no-browser", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server = create_server(Path(args.root), args.port, args.validator_python)
    except (archive.ArchiveError, OSError, sqlite3.Error) as error:
        print(f"workbench could not start: {error}", file=sys.stderr)
        return 3
    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"ISS Archive Workbench: {address}")
    print("Press Ctrl+C to stop. The server accepts local connections only.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
