#!/usr/bin/env python3
"""Read-only audiovisual census and reconciliation projection.

The source repositories are never modified.  All state is written below the
workbench directory supplied with --workbench.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from xml.etree import ElementTree


SCHEMA_VERSION = "1"
VIDEO_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".dv", ".flv", ".m2t", ".m2ts", ".m4v",
    ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".mxf", ".ogv",
    ".ts", ".vob", ".webm", ".wmv",
}
AUDIO_EXTENSIONS = {
    ".aac", ".ac3", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg",
    ".opus", ".wav", ".wma",
}
IMAGE_EXTENSIONS = {
    ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".psd", ".tif", ".tiff",
}
PROJECT_EXTENSIONS = {
    ".aep", ".drp", ".edl", ".kdenlive", ".mlt", ".ncor", ".prproj",
    ".ses", ".sesx", ".veg",
}
PROJECT_SIDECARS = {".cfa", ".pek", ".pkf", ".xmp", ".xmpses"}
DOCUMENT_EXTENSIONS = {
    ".csv", ".doc", ".docx", ".htm", ".html", ".json", ".md", ".ods",
    ".odt", ".pdf", ".ppt", ".pptx", ".rtf", ".sql", ".tsv", ".txt",
    ".xls", ".xlsx", ".xml",
}
ARCHIVE_EXTENSIONS = {".7z", ".gz", ".iso", ".rar", ".tar", ".tgz", ".zip"}
SKIP_DIRECTORIES = {"$RECYCLE.BIN", "System Volume Information", ".Trash-1000"}
EXTERNAL_RIGHTS_TOKENS = {
    "3sat", "ard", "bbc", "defa", "deutscher fernsehfunk", "dff", "dw",
    "mdr", "ndr", "rbb", "swr", "wdr", "zdf", "youtube", "spielfilm",
    "ganzer film", "dokumentation", "reportage", "umschau",
}


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_runs (
    run_id TEXT PRIMARY KEY,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
    roots_json TEXT NOT NULL,
    files_seen INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS roots (
    root_id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    source_role TEXT NOT NULL,
    device TEXT NOT NULL DEFAULT '',
    filesystem TEXT NOT NULL DEFAULT '',
    mount_options TEXT NOT NULL DEFAULT '',
    read_only_observed INTEGER NOT NULL DEFAULT 0,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    occurrence_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES roots(root_id),
    relative_path TEXT NOT NULL,
    parent_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    mtime_utc TEXT NOT NULL,
    depth INTEGER NOT NULL,
    file_kind TEXT NOT NULL,
    role_candidate TEXT NOT NULL,
    rights_status TEXT NOT NULL DEFAULT 'unknown',
    rights_risk_hint TEXT NOT NULL DEFAULT '',
    publication_status TEXT NOT NULL DEFAULT 'unknown',
    sha256 TEXT NOT NULL DEFAULT '',
    fixity_status TEXT NOT NULL DEFAULT 'deferred',
    present INTEGER NOT NULL DEFAULT 1,
    last_seen_run TEXT NOT NULL REFERENCES scan_runs(run_id),
    UNIQUE(root_id, relative_path)
);
CREATE INDEX IF NOT EXISTS files_root_idx ON files(root_id);
CREATE INDEX IF NOT EXISTS files_name_idx ON files(filename);
CREATE INDEX IF NOT EXISTS files_size_idx ON files(size_bytes);
CREATE INDEX IF NOT EXISTS files_kind_idx ON files(file_kind, role_candidate);
CREATE TABLE IF NOT EXISTS media_probe (
    occurrence_id TEXT PRIMARY KEY REFERENCES files(occurrence_id),
    probe_status TEXT NOT NULL,
    probed_at_utc TEXT NOT NULL,
    stat_size_bytes INTEGER NOT NULL,
    stat_mtime_utc TEXT NOT NULL,
    format_name TEXT NOT NULL DEFAULT '',
    duration_seconds REAL,
    overall_bit_rate INTEGER,
    stream_count INTEGER NOT NULL DEFAULT 0,
    video_codec TEXT NOT NULL DEFAULT '',
    width INTEGER,
    height INTEGER,
    display_aspect_ratio TEXT NOT NULL DEFAULT '',
    pixel_format TEXT NOT NULL DEFAULT '',
    field_order TEXT NOT NULL DEFAULT '',
    frame_rate TEXT NOT NULL DEFAULT '',
    video_bit_rate INTEGER,
    audio_codec TEXT NOT NULL DEFAULT '',
    audio_sample_rate INTEGER,
    audio_channels INTEGER,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS project_analysis (
    occurrence_id TEXT PRIMARY KEY REFERENCES files(occurrence_id),
    analysis_status TEXT NOT NULL,
    analyzed_at_utc TEXT NOT NULL,
    project_format TEXT NOT NULL,
    dependency_count INTEGER NOT NULL DEFAULT 0,
    resolved_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    ambiguous_count INTEGER NOT NULL DEFAULT 0,
    rerender_status TEXT NOT NULL DEFAULT 'unknown',
    dependencies_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS wordpress_videos (
    post_id INTEGER PRIMARY KEY,
    post_status TEXT NOT NULL,
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    video_url TEXT NOT NULL,
    youtube_video_id TEXT NOT NULL,
    source_family TEXT NOT NULL,
    source_label TEXT NOT NULL,
    source_url TEXT NOT NULL,
    original_date TEXT NOT NULL,
    duration TEXT NOT NULL,
    language TEXT NOT NULL,
    rights TEXT NOT NULL,
    transcript_status TEXT NOT NULL,
    transcript_source TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_matches (
    post_id INTEGER NOT NULL REFERENCES wordpress_videos(post_id),
    occurrence_id TEXT NOT NULL REFERENCES files(occurrence_id),
    score REAL NOT NULL,
    reasons TEXT NOT NULL,
    decision_status TEXT NOT NULL DEFAULT 'unreviewed',
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(post_id, occurrence_id)
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest.upper()}"


def normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def normalize_title(value: str) -> str:
    value = value.casefold()
    value = value.replace("ö", "oe").replace("ä", "ae").replace("ü", "ue").replace("ß", "ss")
    return " ".join(re.findall(r"[a-z0-9]+", value))


def parse_root_spec(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("root must be LABEL=/absolute/path")
    label, raw_path = value.split("=", 1)
    root_id = normalize_label(label)
    path = Path(raw_path).expanduser().resolve()
    if not root_id or not path.is_dir():
        raise argparse.ArgumentTypeError(f"invalid source root: {value}")
    return root_id, path


def connect(workbench: Path) -> sqlite3.Connection:
    data = workbench / "data"
    data.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(data / "inventory.sqlite")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,))
    connection.commit()
    return connection


def mount_info(path: Path) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["findmnt", "-J", "-T", str(path), "-o", "SOURCE,FSTYPE,OPTIONS"],
            check=False, capture_output=True, text=True, timeout=20,
        )
        payload = json.loads(result.stdout or "{}")
        filesystems = payload.get("filesystems", [])
        if filesystems:
            row = filesystems[0]
            options = str(row.get("options", ""))
            return {
                "device": str(row.get("source", "")),
                "filesystem": str(row.get("fstype", "")),
                "mount_options": options,
                "read_only": "1" if "ro" in options.split(",") else "0",
            }
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    flag = getattr(os, "ST_RDONLY", 1)
    return {
        "device": "", "filesystem": "", "mount_options": "",
        "read_only": "1" if os.statvfs(path).f_flag & flag else "0",
    }


def file_kind(extension: str) -> str:
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in PROJECT_EXTENSIONS:
        return "project"
    if extension in PROJECT_SIDECARS:
        return "project_sidecar"
    if extension in DOCUMENT_EXTENSIONS:
        return "document"
    if extension in ARCHIVE_EXTENSIONS:
        return "archive_container"
    return "other"


def classify(root_id: str, relative_path: str, kind: str, depth: int) -> tuple[str, str]:
    text = relative_path.casefold()
    name = PurePosixPath(relative_path).name.casefold()
    if "$recycle.bin" in text or "system volume information" in text:
        return "filesystem_noise", ""
    if "adobe premiere pro auto-save" in text:
        role = "project_autosave" if kind == "project" else "project_cache_or_preview"
    elif any(token in text for token in ("preview files", "/cache/", "/tmp/")) or kind == "project_sidecar":
        role = "project_cache_or_preview"
    elif kind == "project":
        role = "editable_project"
    elif "/thumb/" in f"/{text}" or kind == "image":
        role = "thumbnail_or_graphic"
    elif root_id == "is-videos" and depth == 1 and kind == "video":
        role = "publication_export_candidate"
    elif root_id == "is-videos" and "/export/" in f"/{text}" and kind in {"video", "audio"}:
        role = "intermediate_or_export"
    elif root_id == "is-videos" and kind in {"video", "audio"}:
        role = "source_capture_or_edit_asset"
    elif root_id == "mediathek-backup" and kind in {"video", "audio"}:
        role = "reference_av_collection"
    elif root_id == "mediathek-backup":
        role = "reference_collection"
    elif kind == "document":
        role = "documentation_or_rights_evidence"
    else:
        role = "unclassified"

    rights_hint = ""
    rights_text = normalize_title(relative_path)
    matched = sorted(token for token in EXTERNAL_RIGHTS_TOKENS if normalize_title(token) in rights_text)
    if root_id == "mediathek-backup" and kind in {"video", "audio"}:
        rights_hint = "reference collection; publication blocked pending rights review"
    elif matched and kind in {"video", "audio"}:
        rights_hint = "possible external broadcaster or copyrighted production: " + ", ".join(matched)
    elif kind in {"video", "audio"}:
        rights_hint = "publication rights not yet documented"
    if name in {"thumbs.db", ".ds_store"}:
        role = "filesystem_noise"
    return role, rights_hint


def observed_mtime(stat: os.stat_result) -> str:
    return datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def scan_root(connection: sqlite3.Connection, run_id: str, root_id: str, root: Path, progress_every: int) -> tuple[int, int]:
    source_role = "institutional-production" if root_id == "is-videos" else "mixed-reference"
    now = utc_now()
    info = mount_info(root)
    connection.execute(
        """INSERT INTO roots(root_id,root_path,source_role,device,filesystem,mount_options,read_only_observed,first_seen_utc,last_seen_utc)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(root_id) DO UPDATE SET root_path=excluded.root_path,source_role=excluded.source_role,
             device=excluded.device,filesystem=excluded.filesystem,mount_options=excluded.mount_options,
             read_only_observed=excluded.read_only_observed,last_seen_utc=excluded.last_seen_utc""",
        (root_id, str(root), source_role, info["device"], info["filesystem"], info["mount_options"], int(info["read_only"]), now, now),
    )
    connection.commit()
    count = 0
    errors = 0
    print(f"scan: {root_id}: {root}", flush=True)
    for directory, names, filenames in os.walk(root):
        names[:] = [name for name in names if name not in SKIP_DIRECTORIES]
        base = Path(directory)
        for filename in filenames:
            path = base / filename
            try:
                stat = path.stat()
                relative = path.relative_to(root).as_posix()
                extension = path.suffix.casefold()
                kind = file_kind(extension)
                depth = len(PurePosixPath(relative).parts)
                role, rights_hint = classify(root_id, relative, kind, depth)
                occurrence_id = stable_id("OCC", root_id, relative)
                connection.execute(
                    """INSERT INTO files(occurrence_id,root_id,relative_path,parent_path,filename,extension,size_bytes,
                           mtime_utc,depth,file_kind,role_candidate,rights_status,rights_risk_hint,
                           publication_status,present,last_seen_run)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,'unknown',?,'unknown',1,?)
                       ON CONFLICT(root_id,relative_path) DO UPDATE SET
                         parent_path=excluded.parent_path,filename=excluded.filename,extension=excluded.extension,
                         size_bytes=excluded.size_bytes,mtime_utc=excluded.mtime_utc,depth=excluded.depth,
                         file_kind=excluded.file_kind,role_candidate=excluded.role_candidate,
                         rights_risk_hint=excluded.rights_risk_hint,present=1,last_seen_run=excluded.last_seen_run""",
                    (occurrence_id, root_id, relative, str(PurePosixPath(relative).parent), filename,
                     extension, stat.st_size, observed_mtime(stat), depth, kind, role, rights_hint, run_id),
                )
                count += 1
                if count % 100 == 0:
                    connection.commit()
                if progress_every and count % progress_every == 0:
                    print(f"scan: {root_id}: {count} files", flush=True)
            except (OSError, sqlite3.Error, ValueError) as error:
                errors += 1
                print(f"scan-error: {path}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    connection.execute("UPDATE files SET present=0 WHERE root_id=? AND last_seen_run<>?", (root_id, run_id))
    connection.commit()
    print(f"scan: {root_id}: complete ({count} files, {errors} errors)", flush=True)
    return count, errors


def command_scan(args: argparse.Namespace) -> None:
    workbench = Path(args.workbench).expanduser().resolve()
    connection = connect(workbench)
    roots = [parse_root_spec(value) for value in args.source_root]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    connection.execute(
        "INSERT INTO scan_runs(run_id,started_at_utc,status,roots_json,note) VALUES(?,?,'running',?,?)",
        (run_id, utc_now(), json.dumps([{"root_id": label, "path": str(path)} for label, path in roots], ensure_ascii=False), args.note),
    )
    connection.commit()
    total = 0
    errors = 0
    try:
        for label, path in roots:
            seen, failed = scan_root(connection, run_id, label, path, args.progress_every)
            total += seen
            errors += failed
        connection.execute(
            "UPDATE scan_runs SET completed_at_utc=?,status='complete',files_seen=?,errors=? WHERE run_id=?",
            (utc_now(), total, errors, run_id),
        )
        connection.commit()
    except BaseException:
        connection.execute("UPDATE scan_runs SET completed_at_utc=?,status='failed',files_seen=?,errors=? WHERE run_id=?", (utc_now(), total, errors + 1, run_id))
        connection.commit()
        raise
    export_reports(connection, workbench)
    print(json.dumps({"run_id": run_id, "files_seen": total, "errors": errors, "database": str(workbench / 'data/inventory.sqlite')}, indent=2))


def fraction_text(value: str) -> str:
    if not value or value == "0/0":
        return ""
    try:
        fraction = Fraction(value)
        decimal = float(fraction)
        return f"{decimal:.3f}".rstrip("0").rstrip(".")
    except (ValueError, ZeroDivisionError):
        return value


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe_one(root: Path, row: sqlite3.Row, timeout: int) -> tuple[str, dict[str, Any]]:
    path = root / row["relative_path"]
    command = [
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return row["occurrence_id"], {"probe_status": "error", "error": result.stderr.strip()[:2000]}
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
        format_row = payload.get("format", {})
        return row["occurrence_id"], {
            "probe_status": "complete",
            "format_name": str(format_row.get("format_name", "")),
            "duration_seconds": float_or_none(format_row.get("duration")),
            "overall_bit_rate": int_or_none(format_row.get("bit_rate")),
            "stream_count": len(streams),
            "video_codec": str(video.get("codec_name", "")),
            "width": int_or_none(video.get("width")),
            "height": int_or_none(video.get("height")),
            "display_aspect_ratio": str(video.get("display_aspect_ratio", "")),
            "pixel_format": str(video.get("pix_fmt", "")),
            "field_order": str(video.get("field_order", "")),
            "frame_rate": fraction_text(str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")),
            "video_bit_rate": int_or_none(video.get("bit_rate")),
            "audio_codec": str(audio.get("codec_name", "")),
            "audio_sample_rate": int_or_none(audio.get("sample_rate")),
            "audio_channels": int_or_none(audio.get("channels")),
            "error": "",
        }
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        return row["occurrence_id"], {"probe_status": "error", "error": f"{type(error).__name__}: {error}"}


def command_probe(args: argparse.Namespace) -> None:
    workbench = Path(args.workbench).expanduser().resolve()
    connection = connect(workbench)
    roots = {row["root_id"]: Path(row["root_path"]) for row in connection.execute("SELECT * FROM roots")}
    where = ["f.present=1", "f.file_kind IN ('video','audio')"]
    params: list[Any] = []
    if args.root_id:
        where.append("f.root_id=?")
        params.append(args.root_id)
    if args.role:
        where.append("f.role_candidate=?")
        params.append(args.role)
    if not args.refresh:
        where.append("(p.occurrence_id IS NULL OR p.stat_size_bytes<>f.size_bytes OR p.stat_mtime_utc<>f.mtime_utc)")
    rows = connection.execute(
        "SELECT f.* FROM files f LEFT JOIN media_probe p ON p.occurrence_id=f.occurrence_id WHERE " + " AND ".join(where) + " ORDER BY f.root_id,f.relative_path",
        params,
    ).fetchall()
    print(f"probe: {len(rows)} media files queued", flush=True)
    completed = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(probe_one, roots[row["root_id"]], row, args.timeout): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            occurrence_id, data = future.result()
            connection.execute(
                """INSERT INTO media_probe(occurrence_id,probe_status,probed_at_utc,stat_size_bytes,stat_mtime_utc,
                       format_name,duration_seconds,overall_bit_rate,stream_count,video_codec,width,height,
                       display_aspect_ratio,pixel_format,field_order,frame_rate,video_bit_rate,audio_codec,
                       audio_sample_rate,audio_channels,error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(occurrence_id) DO UPDATE SET probe_status=excluded.probe_status,
                     probed_at_utc=excluded.probed_at_utc,stat_size_bytes=excluded.stat_size_bytes,
                     stat_mtime_utc=excluded.stat_mtime_utc,format_name=excluded.format_name,
                     duration_seconds=excluded.duration_seconds,overall_bit_rate=excluded.overall_bit_rate,
                     stream_count=excluded.stream_count,video_codec=excluded.video_codec,width=excluded.width,
                     height=excluded.height,display_aspect_ratio=excluded.display_aspect_ratio,
                     pixel_format=excluded.pixel_format,field_order=excluded.field_order,
                     frame_rate=excluded.frame_rate,video_bit_rate=excluded.video_bit_rate,
                     audio_codec=excluded.audio_codec,audio_sample_rate=excluded.audio_sample_rate,
                     audio_channels=excluded.audio_channels,error=excluded.error""",
                (occurrence_id, data["probe_status"], utc_now(), row["size_bytes"], row["mtime_utc"],
                 data.get("format_name", ""), data.get("duration_seconds"), data.get("overall_bit_rate"),
                 data.get("stream_count", 0), data.get("video_codec", ""), data.get("width"), data.get("height"),
                 data.get("display_aspect_ratio", ""), data.get("pixel_format", ""), data.get("field_order", ""),
                 data.get("frame_rate", ""), data.get("video_bit_rate"), data.get("audio_codec", ""),
                 data.get("audio_sample_rate"), data.get("audio_channels"), data.get("error", "")),
            )
            completed += 1
            errors += data["probe_status"] != "complete"
            if completed % 10 == 0 or completed == len(rows):
                connection.commit()
                print(f"probe: {completed}/{len(rows)} complete ({errors} errors)", flush=True)
    connection.commit()
    export_reports(connection, workbench)


def project_dependencies(path: Path) -> list[str]:
    if path.suffix.casefold() != ".prproj":
        return []
    tree = ElementTree.parse(path)
    values: list[str] = []
    for element in tree.iter():
        tag = element.tag.split("}")[-1]
        if tag not in {"RelativePath", "ActualMediaFilePath"}:
            continue
        value = (element.text or "").strip()
        if not value or value.isdigit():
            continue
        if tag == "ActualMediaFilePath" and any(token in value.casefold() for token in ("media cache", "adobecash", "encoderpresets")):
            continue
        values.append(value)
    relative_values = [value for value in values if value.startswith(".\\") or value.startswith("./")]
    chosen = relative_values or values
    return list(dict.fromkeys(chosen))


def command_projects(args: argparse.Namespace) -> None:
    workbench = Path(args.workbench).expanduser().resolve()
    connection = connect(workbench)
    roots = {row["root_id"]: Path(row["root_path"]) for row in connection.execute("SELECT * FROM roots")}
    indexes: dict[str, dict[str, list[sqlite3.Row]]] = {}
    for root_id in roots:
        by_name: dict[str, list[sqlite3.Row]] = {}
        for row in connection.execute("SELECT occurrence_id,relative_path,filename FROM files WHERE root_id=? AND present=1", (root_id,)):
            by_name.setdefault(row["filename"].casefold(), []).append(row)
        indexes[root_id] = by_name
    projects = connection.execute("SELECT * FROM files WHERE present=1 AND file_kind='project' ORDER BY root_id,relative_path").fetchall()
    for index, row in enumerate(projects, start=1):
        path = roots[row["root_id"]] / row["relative_path"]
        error = ""
        records: list[dict[str, Any]] = []
        status = "complete"
        try:
            dependencies = project_dependencies(path)
            project_parent = PurePosixPath(row["relative_path"]).parent
            for raw in dependencies:
                normalized = raw.replace("\\", "/")
                if normalized.startswith("./"):
                    normalized = normalized[2:]
                basename = PurePosixPath(normalized).name
                candidates = indexes[row["root_id"]].get(basename.casefold(), [])
                exact = [candidate for candidate in candidates if candidate["relative_path"] in {
                    str(project_parent / normalized),
                    str(project_parent.parent / normalized),
                }]
                if len(exact) == 1:
                    resolution = "resolved_exact"
                    selected = exact
                elif len(candidates) == 1:
                    resolution = "resolved_by_unique_basename"
                    selected = candidates
                elif len(candidates) > 1:
                    resolution = "ambiguous"
                    selected = candidates
                else:
                    resolution = "missing"
                    selected = []
                records.append({
                    "project_reference": raw,
                    "basename": basename,
                    "resolution": resolution,
                    "candidate_occurrence_ids": [candidate["occurrence_id"] for candidate in selected],
                    "candidate_paths": [candidate["relative_path"] for candidate in selected],
                })
        except (OSError, ElementTree.ParseError, ValueError) as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        resolved = sum(record["resolution"].startswith("resolved") for record in records)
        missing = sum(record["resolution"] == "missing" for record in records)
        ambiguous = sum(record["resolution"] == "ambiguous" for record in records)
        if status == "error":
            rerender = "unknown"
        elif not records:
            rerender = "manual_review_no_dependencies_extracted"
        elif missing == 0 and ambiguous == 0:
            rerender = "project_assets_present_candidate"
        elif resolved > 0:
            rerender = "partial_assets_present"
        else:
            rerender = "assets_not_resolved"
        connection.execute(
            """INSERT INTO project_analysis(occurrence_id,analysis_status,analyzed_at_utc,project_format,
                   dependency_count,resolved_count,missing_count,ambiguous_count,rerender_status,
                   dependencies_json,error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(occurrence_id) DO UPDATE SET analysis_status=excluded.analysis_status,
                 analyzed_at_utc=excluded.analyzed_at_utc,project_format=excluded.project_format,
                 dependency_count=excluded.dependency_count,resolved_count=excluded.resolved_count,
                 missing_count=excluded.missing_count,ambiguous_count=excluded.ambiguous_count,
                 rerender_status=excluded.rerender_status,dependencies_json=excluded.dependencies_json,
                 error=excluded.error""",
            (row["occurrence_id"], status, utc_now(), row["extension"], len(records), resolved,
             missing, ambiguous, rerender, json.dumps(records, ensure_ascii=False, sort_keys=True), error),
        )
        if index % 5 == 0:
            connection.commit()
        print(f"projects: {index}/{len(projects)} {row['relative_path']}: {rerender}", flush=True)
    connection.commit()
    export_reports(connection, workbench)


def youtube_id(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/|/embed/)([-_A-Za-z0-9]{6,})", url)
    return match.group(1) if match else ""


def command_wordpress(args: argparse.Namespace) -> None:
    workbench = Path(args.workbench).expanduser().resolve()
    connection = connect(workbench)
    php = r'''$posts=get_posts(["post_type"=>"video","post_status"=>"any","numberposts"=>-1,"orderby"=>"ID","order"=>"ASC"]); $out=[]; foreach($posts as $p){$out[]=["post_id"=>$p->ID,"post_status"=>$p->post_status,"title"=>$p->post_title,"slug"=>$p->post_name,"video_url"=>(string)get_post_meta($p->ID,"iss_video_url",true),"source_family"=>(string)get_post_meta($p->ID,"iss_video_source_family",true),"source_label"=>(string)get_post_meta($p->ID,"iss_video_source_label",true),"source_url"=>(string)get_post_meta($p->ID,"iss_video_source_url",true),"original_date"=>(string)get_post_meta($p->ID,"iss_video_original_date",true),"duration"=>(string)get_post_meta($p->ID,"iss_video_duration",true),"language"=>(string)get_post_meta($p->ID,"iss_video_language",true),"rights"=>(string)get_post_meta($p->ID,"iss_video_rights",true),"transcript_status"=>(string)get_post_meta($p->ID,"iss_video_transcript_status",true),"transcript_source"=>(string)get_post_meta($p->ID,"iss_video_transcript_source",true)];} echo json_encode($out,JSON_UNESCAPED_UNICODE|JSON_UNESCAPED_SLASHES);'''
    result = subprocess.run(
        ["docker", "compose", "run", "--rm", "wpcli", "eval", php],
        cwd=Path(args.compose_dir).expanduser().resolve(), capture_output=True, text=True,
        check=False, timeout=args.timeout,
    )
    if result.returncode != 0:
        raise SystemExit(f"WordPress export failed: {result.stderr.strip()}")
    start = result.stdout.find("[")
    if start < 0:
        raise SystemExit("WordPress export returned no JSON array")
    rows = json.loads(result.stdout[start:])
    observed = utc_now()
    connection.execute("DELETE FROM wordpress_videos")
    for row in rows:
        connection.execute(
            """INSERT INTO wordpress_videos(post_id,post_status,title,slug,video_url,youtube_video_id,
                   source_family,source_label,source_url,original_date,duration,language,rights,
                   transcript_status,transcript_source,observed_at_utc)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["post_id"], row["post_status"], row["title"], row["slug"], row["video_url"],
             youtube_id(row["video_url"]), row["source_family"], row["source_label"], row["source_url"],
             row["original_date"], row["duration"], row["language"], row["rights"],
             row["transcript_status"], row["transcript_source"], observed),
        )
    connection.commit()
    build_candidate_matches(connection)
    export_reports(connection, workbench)
    print(f"wordpress: imported {len(rows)} Video records", flush=True)


def build_candidate_matches(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM candidate_matches WHERE decision_status='unreviewed'")
    files = connection.execute(
        "SELECT occurrence_id,filename,relative_path,role_candidate FROM files WHERE present=1 AND file_kind='video' AND root_id='is-videos'"
    ).fetchall()
    for post in connection.execute("SELECT * FROM wordpress_videos"):
        post_tokens = set(normalize_title(post["title"]).split())
        for row in files:
            file_tokens = set(normalize_title(Path(row["filename"]).stem).split())
            if not post_tokens or not file_tokens:
                continue
            intersection = post_tokens & file_tokens
            union = post_tokens | file_tokens
            score = len(intersection) / len(union)
            reasons = []
            if intersection:
                reasons.append("shared title tokens: " + ", ".join(sorted(intersection)))
            if row["role_candidate"] == "publication_export_candidate":
                score += 0.10
                reasons.append("top-level publication export candidate")
            if score >= 0.18:
                connection.execute(
                    "INSERT OR IGNORE INTO candidate_matches(post_id,occurrence_id,score,reasons) VALUES(?,?,?,?)",
                    (post["post_id"], row["occurrence_id"], min(score, 1.0), "; ".join(reasons)),
                )
    connection.commit()


def write_query_csv(connection: sqlite3.Connection, path: Path, query: str, params: tuple[Any, ...] = ()) -> None:
    rows = connection.execute(query, params).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not rows:
            handle.write("")
            return
        writer = csv.writer(handle)
        writer.writerow(rows[0].keys())
        writer.writerows([tuple(row) for row in rows])


def human_size(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return str(value)


def export_reports(connection: sqlite3.Connection, workbench: Path) -> None:
    data = workbench / "data"
    reports = workbench / "reports"
    data.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    write_query_csv(connection, data / "files.csv", "SELECT * FROM files ORDER BY root_id,relative_path")
    write_query_csv(
        connection, data / "media.csv",
        "SELECT f.root_id,f.relative_path,f.filename,f.size_bytes,f.role_candidate,f.rights_status,f.rights_risk_hint,f.publication_status,p.* FROM files f LEFT JOIN media_probe p USING(occurrence_id) WHERE f.file_kind IN ('video','audio') ORDER BY f.root_id,f.relative_path",
    )
    write_query_csv(
        connection, data / "projects.csv",
        "SELECT f.root_id,f.relative_path,f.filename,f.size_bytes,p.* FROM files f LEFT JOIN project_analysis p USING(occurrence_id) WHERE f.file_kind='project' ORDER BY f.root_id,f.relative_path",
    )
    write_query_csv(
        connection, data / "rights-review.csv",
        "SELECT root_id,relative_path,filename,file_kind,role_candidate,rights_status,rights_risk_hint,publication_status FROM files WHERE present=1 AND file_kind IN ('video','audio') AND rights_status<>'cleared' ORDER BY root_id,role_candidate,relative_path",
    )
    write_query_csv(
        connection, data / "duplicate-size-candidates.csv",
        """SELECT size_bytes,COUNT(*) AS occurrences,GROUP_CONCAT(root_id||':'||relative_path,' | ') AS paths
             FROM files WHERE present=1 AND size_bytes>0 GROUP BY size_bytes HAVING COUNT(*)>1 ORDER BY size_bytes DESC""",
    )
    write_query_csv(connection, data / "wordpress-videos.csv", "SELECT * FROM wordpress_videos ORDER BY post_id")
    write_query_csv(
        connection, data / "candidate-matches.csv",
        """SELECT w.post_id,w.post_status,w.title,w.video_url,w.source_family,w.rights AS wordpress_rights,
                    f.relative_path,f.size_bytes,m.duration_seconds,m.width,m.height,m.video_codec,
                    c.score,c.reasons,c.decision_status,c.note
             FROM candidate_matches c JOIN wordpress_videos w USING(post_id)
             JOIN files f USING(occurrence_id) LEFT JOIN media_probe m USING(occurrence_id)
             ORDER BY w.post_id,c.score DESC,f.relative_path""",
    )
    roots = connection.execute(
        "SELECT r.*,COUNT(f.occurrence_id) files,COALESCE(SUM(f.size_bytes),0) bytes FROM roots r LEFT JOIN files f ON f.root_id=r.root_id AND f.present=1 GROUP BY r.root_id ORDER BY r.root_id"
    ).fetchall()
    roles = connection.execute(
        "SELECT root_id,role_candidate,COUNT(*) files,SUM(size_bytes) bytes FROM files WHERE present=1 GROUP BY root_id,role_candidate ORDER BY root_id,bytes DESC"
    ).fetchall()
    media_total = connection.execute("SELECT COUNT(*) FROM files WHERE present=1 AND file_kind IN ('video','audio')").fetchone()[0]
    probed = connection.execute("SELECT COUNT(*) FROM media_probe WHERE probe_status='complete'").fetchone()[0]
    projects = connection.execute("SELECT COUNT(*) FROM files WHERE present=1 AND file_kind='project'").fetchone()[0]
    project_ready = connection.execute("SELECT COUNT(*) FROM project_analysis WHERE rerender_status='project_assets_present_candidate'").fetchone()[0]
    zero = connection.execute("SELECT COUNT(*) FROM files WHERE present=1 AND size_bytes=0").fetchone()[0]
    wp_count = connection.execute("SELECT COUNT(*) FROM wordpress_videos").fetchone()[0]
    report = [
        "# Video Inventory Status", "", f"Generated: `{utc_now()}`", "",
        "## Scope", "",
    ]
    for row in roots:
        report.append(f"- `{row['root_id']}`: {row['files']} files, {human_size(row['bytes'])}, source role `{row['source_role']}`, read-only observed `{bool(row['read_only_observed'])}`")
    report.extend([
        "", "## Current census", "",
        f"- AV files: {media_total}", f"- Successfully probed AV files: {probed}",
        f"- Editable project files: {projects}", f"- Projects with all extracted dependencies resolved: {project_ready}",
        f"- Zero-byte files: {zero}", f"- WordPress Video records imported: {wp_count}",
        "", "## Candidate roles", "",
    ])
    for row in roles:
        report.append(f"- `{row['root_id']}` / `{row['role_candidate']}`: {row['files']} files, {human_size(row['bytes'])}")
    report.extend([
        "", "## Interpretation rules", "",
        "- Role labels are discovery candidates, not curatorial decisions.",
        "- Rights and publication status remain `unknown` until reviewed.",
        "- A duplicate-size row is only a candidate; byte identity requires SHA-256.",
        "- Project asset resolution proves files are locatable, not that the legacy application can render the project correctly.",
        "- Source repositories were read only; this workbench is a rebuildable projection.",
    ])
    (reports / "inventory-status.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def command_export(args: argparse.Namespace) -> None:
    workbench = Path(args.workbench).expanduser().resolve()
    connection = connect(workbench)
    export_reports(connection, workbench)
    print(workbench / "reports/inventory-status.md")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbench", default="/mnt/volume/iss-archive-work/video-inventory")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="Census source roots without changing them")
    scan.add_argument("--source-root", action="append", required=True, help="LABEL=/absolute/path")
    scan.add_argument("--note", default="")
    scan.add_argument("--progress-every", type=int, default=1000)
    scan.set_defaults(func=command_scan)

    probe = commands.add_parser("probe", help="Run ffprobe over indexed audio/video")
    probe.add_argument("--root-id", default="")
    probe.add_argument("--role", default="")
    probe.add_argument("--workers", type=int, default=2)
    probe.add_argument("--timeout", type=int, default=120)
    probe.add_argument("--refresh", action="store_true")
    probe.set_defaults(func=command_probe)

    projects = commands.add_parser("projects", help="Resolve Premiere project dependencies")
    projects.set_defaults(func=command_projects)

    wordpress = commands.add_parser("wordpress", help="Import current local WordPress Video records")
    wordpress.add_argument("--compose-dir", default="/home/vladimir/wp-website")
    wordpress.add_argument("--timeout", type=int, default=120)
    wordpress.set_defaults(func=command_wordpress)

    export = commands.add_parser("export", help="Regenerate CSV and Markdown projections")
    export.set_defaults(func=command_export)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
