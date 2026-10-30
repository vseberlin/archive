#!/usr/bin/env python3
"""Resumable read-only visual indexer for archive master candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import source_registry
import visual_index


TOOL_VERSION = "0.1.3"
SCHEMA_VERSION = 1
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
EXTRACTORS = (
    ("ahash-64-v2", "ahash", "2", 64),
    ("dhash-64-v2", "dhash", "2", 64),
    ("phash-dct-64-v2", "phash-dct", "2", 64),
)
FEATURE_KEYS = {
    "ahash-64-v2": "ahash_64",
    "dhash-64-v2": "dhash_64",
    "phash-dct-64-v2": "phash_dct_64",
}

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS visual_index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_runs (
    run_id TEXT PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('running','complete','failed')),
    roots_json TEXT NOT NULL,
    files_seen INTEGER NOT NULL DEFAULT 0,
    images_seen INTEGER NOT NULL DEFAULT 0,
    indexed INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS visual_roots (
    root_id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    root_kind TEXT NOT NULL CHECK (root_kind IN ('archive','intake')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS visual_assets (
    asset_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES visual_roots(root_id),
    relative_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    mtime_ns INTEGER NOT NULL,
    width_px INTEGER,
    height_px INTEGER,
    status TEXT NOT NULL CHECK (
        status IN ('pending','indexed','error','missing')
    ),
    last_seen_run TEXT NOT NULL REFERENCES index_runs(run_id),
    indexed_at TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    UNIQUE (root_id, relative_path, size_bytes, mtime_ns)
);
CREATE INDEX IF NOT EXISTS visual_assets_root_status_idx
ON visual_assets(root_id, status);
CREATE INDEX IF NOT EXISTS visual_assets_path_idx
ON visual_assets(root_id, relative_path);
CREATE TABLE IF NOT EXISTS feature_extractors (
    extractor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    bit_length INTEGER NOT NULL,
    parameters_json TEXT NOT NULL,
    implementation TEXT NOT NULL,
    UNIQUE (name, version, parameters_json)
);
CREATE TABLE IF NOT EXISTS visual_features (
    asset_id TEXT NOT NULL REFERENCES visual_assets(asset_id),
    extractor_id TEXT NOT NULL REFERENCES feature_extractors(extractor_id),
    feature_hex TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, extractor_id)
);
"""


class VisualIndexerError(RuntimeError):
    """Raised when a complete visual-index run cannot be proven."""


def stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32].upper()}"


def connect_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 60000")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        """INSERT OR REPLACE INTO visual_index_meta (key, value)
           VALUES ('schema_version', ?)""",
        (str(SCHEMA_VERSION),),
    )
    connection.execute(
        """INSERT OR REPLACE INTO visual_index_meta (key, value)
           VALUES ('tool_version', ?)""",
        (TOOL_VERSION,),
    )
    parameters = json.dumps(
        {
            "normalization": (
                "libvips-8.16-first-image-auto-orient-max-64x64-"
                "single-thread"
            )
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.executemany(
        """INSERT OR IGNORE INTO feature_extractors (
               extractor_id, name, version, bit_length, parameters_json,
               implementation
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        [
            (
                extractor_id,
                name,
                version,
                bits,
                parameters,
                f"visual-index-{visual_index.TOOL_VERSION}",
            )
            for extractor_id, name, version, bits in EXTRACTORS
        ],
    )
    connection.commit()
    return connection


def _root_kind(path: Path) -> str:
    if path == Path("/mnt/volume/archive").resolve():
        return "archive"
    if path == Path("/mnt/volume/archive-intake").resolve():
        return "intake"
    name = path.name.casefold()
    return "intake" if "intake" in name else "archive"


def register_root(
    connection: sqlite3.Connection,
    root: Path,
) -> str:
    stat = root.stat()
    root_id = stable_id("VROOT", str(root))
    now = visual_index.utc_now()
    connection.execute(
        """INSERT INTO visual_roots (
               root_id, root_path, root_kind, first_seen_at, last_seen_at,
               device, inode
           ) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(root_id) DO UPDATE SET
               last_seen_at = excluded.last_seen_at,
               device = excluded.device,
               inode = excluded.inode""",
        (
            root_id,
            str(root),
            _root_kind(root),
            now,
            now,
            stat.st_dev,
            stat.st_ino,
        ),
    )
    return root_id


def inventory_root(
    connection: sqlite3.Connection,
    *,
    root: Path,
    root_id: str,
    run_id: str,
) -> tuple[int, int, float]:
    started = time.monotonic()
    files_seen = 0
    images_seen = 0
    pending = [root]
    batch = 0
    scan_errors: list[str] = []
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        files_seen += 1
                        suffix = Path(entry.name).suffix.casefold()
                        if suffix not in visual_index.IMAGE_SUFFIXES:
                            continue
                        images_seen += 1
                        stat = entry.stat(follow_symlinks=False)
                        relative = str(Path(entry.path).relative_to(root))
                        asset_id = stable_id(
                            "VASSET",
                            root_id,
                            relative,
                            str(stat.st_size),
                            str(stat.st_mtime_ns),
                        )
                        connection.execute(
                            """INSERT INTO visual_assets (
                                   asset_id, root_id, relative_path,
                                   original_filename, extension, size_bytes,
                                   mtime_ns, width_px, height_px, status,
                                   last_seen_run, indexed_at, attempts, error
                               ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                                         'pending', ?, '', 0, '')
                               ON CONFLICT(
                                   root_id, relative_path, size_bytes, mtime_ns
                               ) DO UPDATE SET
                                   last_seen_run = excluded.last_seen_run,
                                   status = CASE
                                       WHEN visual_assets.status = 'missing'
                                       THEN 'pending'
                                       ELSE visual_assets.status
                                   END""",
                            (
                                asset_id,
                                root_id,
                                relative,
                                entry.name,
                                suffix.lstrip("."),
                                stat.st_size,
                                stat.st_mtime_ns,
                                run_id,
                            ),
                        )
                        batch += 1
                        if batch >= 1000:
                            connection.commit()
                            batch = 0
                            print(
                                f"visual-indexer: inventory {root_id} "
                                f"files={files_seen} images={images_seen}",
                                flush=True,
                            )
                    except OSError as error:
                        scan_errors.append(f"{entry.path}: {error}")
        except OSError as error:
            scan_errors.append(f"{directory}: {error}")
    if scan_errors:
        connection.rollback()
        raise VisualIndexerError(
            "inventory encountered "
            f"{len(scan_errors)} read errors; first: {scan_errors[0]}"
        )
    connection.execute(
        """UPDATE visual_assets
           SET status = 'missing'
           WHERE root_id = ? AND last_seen_run <> ?""",
        (root_id, run_id),
    )
    connection.commit()
    return files_seen, images_seen, time.monotonic() - started


def _image_dimensions(source: Path) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(source) as image:
            return int(image.width), int(image.height)
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        values = []
        for field in ("width", "height"):
            completed = subprocess.run(
                ["/usr/bin/vipsheader", "-f", field, str(source)],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise VisualIndexerError(
                    f"image dimensions unavailable: {detail}"
                )
            values.append(int(completed.stdout.strip()))
        return values[0], values[1]


def process_pending(
    connection: sqlite3.Connection,
    *,
    cache: Path,
    timeout: float,
    root_ids: tuple[str, ...] | None = None,
) -> tuple[int, int]:
    root_clause = ""
    parameters: list[Any] = [len(EXTRACTORS)]
    if root_ids:
        root_clause = " AND a.root_id IN (" + ",".join("?" for _ in root_ids) + ")"
        parameters.extend(root_ids)
    rows = connection.execute(
        """SELECT a.*, r.root_path
           FROM visual_assets a
           JOIN visual_roots r ON r.root_id = a.root_id
           WHERE a.status <> 'missing'
             AND (
                 a.status <> 'indexed'
                 OR (
                     SELECT count(*) FROM visual_features f
                     WHERE f.asset_id = a.asset_id
                 ) < ?
             )
           """
        + root_clause
        + " ORDER BY a.root_id, a.relative_path",
        parameters,
    ).fetchall()
    indexed = 0
    errors = 0
    for position, row in enumerate(rows, 1):
        source = visual_index.protected_source(
            Path(str(row["root_path"])) / str(row["relative_path"])
        )
        stat = source.stat()
        if (
            stat.st_size != int(row["size_bytes"])
            or stat.st_mtime_ns != int(row["mtime_ns"])
        ):
            state = "error"
            error = "source stat changed after inventory"
            features: dict[str, str] = {}
            width = height = None
        else:
            try:
                width, height = _image_dimensions(source)
                preview, _elapsed = visual_index.normalized_preview(
                    source,
                    cache,
                    timeout,
                )
                features = visual_index.feature_hashes(preview)
                state = "indexed"
                error = ""
            except Exception as exception:
                width = height = None
                features = {}
                state = "error"
                error = str(exception)[:2000]
        now = visual_index.utc_now()
        try:
            connection.execute("BEGIN")
            if state == "indexed":
                for extractor_id, _name, _version, _bits in EXTRACTORS:
                    connection.execute(
                        """INSERT OR REPLACE INTO visual_features (
                               asset_id, extractor_id, feature_hex, created_at
                           ) VALUES (?, ?, ?, ?)""",
                        (
                            row["asset_id"],
                            extractor_id,
                            features[FEATURE_KEYS[extractor_id]],
                            now,
                        ),
                    )
            connection.execute(
                """UPDATE visual_assets
                   SET width_px = ?, height_px = ?, status = ?,
                       indexed_at = ?, attempts = attempts + 1, error = ?
                   WHERE asset_id = ?""",
                (
                    width,
                    height,
                    state,
                    now if state == "indexed" else "",
                    error,
                    row["asset_id"],
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        if state == "indexed":
            indexed += 1
        else:
            errors += 1
        if position % 100 == 0 or position == len(rows):
            print(
                f"visual-indexer: features {position}/{len(rows)} "
                f"indexed={indexed} errors={errors}",
                flush=True,
            )
    return indexed, errors


def _exclusive_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise VisualIndexerError(
            f"refusing to replace completion marker: {path}"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


def run_index(
    *,
    database: Path,
    work_root: Path,
    roots: list[Path],
    token: str,
    timeout: float,
    min_free_bytes: int = 5 * 1024**3,
) -> dict[str, Any]:
    if not TOKEN_RE.fullmatch(token):
        raise VisualIndexerError("completion token must be 32 lowercase hex characters")
    work_root = work_root.expanduser().resolve()
    database = database.expanduser().resolve()
    try:
        database.relative_to(work_root)
    except ValueError as error:
        raise VisualIndexerError("visual index database escaped its work root") from error
    if shutil.disk_usage(work_root).free < min_free_bytes:
        raise VisualIndexerError("visual index requires at least 5 GB free")
    protected_roots = [visual_index.protected_source(path) for path in roots]
    for root in protected_roots:
        if (
            source_registry.root_kind("visual-index", root) == "usb"
            and not source_registry.root_is_read_only(root)
        ):
            raise VisualIndexerError(
                f"USB source root must be mounted read-only: {root}"
            )
    before = {
        str(root): visual_index.root_fingerprint(root)
        for root in protected_roots
    }
    roots_json = json.dumps(
        [str(root) for root in protected_roots],
        separators=(",", ":"),
    )
    started = visual_index.utc_now()
    cache = work_root / "tmp"
    with closing(connect_index(database)) as connection:
        prior = connection.execute(
            """SELECT run_id, status, roots_json
               FROM index_runs WHERE token = ?""",
            (token,),
        ).fetchone()
        if prior:
            if str(prior["roots_json"]) != roots_json:
                raise VisualIndexerError(
                    "refusing to resume token with a different source-root set"
                )
            if str(prior["status"]) == "complete":
                raise VisualIndexerError(
                    "completion token already belongs to a complete run"
                )
            run_id = str(prior["run_id"])
            connection.execute(
                """UPDATE index_runs
                   SET completed_at = '', status = 'running', note = ?
                   WHERE run_id = ?""",
                (
                    json.dumps(
                        {"resumed_at": started},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    run_id,
                ),
            )
            print(
                f"visual-indexer: resuming run {run_id} token={token}",
                flush=True,
            )
        else:
            run_id = f"VINDEX-{uuid.uuid4().hex.upper()}"
            connection.execute(
                """INSERT INTO index_runs (
                       run_id, token, started_at, completed_at, status,
                       roots_json
                   ) VALUES (?, ?, ?, '', 'running', ?)""",
                (run_id, token, started, roots_json),
            )
        connection.commit()
        total_files = 0
        total_images = 0
        walk_seconds: dict[str, float] = {}
        try:
            registered_root_ids = []
            for root in protected_roots:
                root_id = register_root(connection, root)
                registered_root_ids.append(root_id)
                files, images, seconds = inventory_root(
                    connection,
                    root=root,
                    root_id=root_id,
                    run_id=run_id,
                )
                total_files += files
                total_images += images
                walk_seconds[str(root)] = round(seconds, 6)
                print(
                    f"visual-indexer: inventory complete {root} "
                    f"files={files} images={images} seconds={seconds:.3f}",
                    flush=True,
                )
            indexed, errors = process_pending(
                connection,
                cache=cache,
                timeout=timeout,
                root_ids=tuple(registered_root_ids),
            )
            after = {
                str(root): visual_index.root_fingerprint(root)
                for root in protected_roots
            }
            if after != before:
                raise VisualIndexerError(
                    "protected source-root fingerprint changed during indexing"
                )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise VisualIndexerError(
                    f"visual index integrity check failed: {integrity}"
                )
            completed = visual_index.utc_now()
            connection.execute(
                """UPDATE index_runs
                   SET completed_at = ?, status = 'complete', files_seen = ?,
                       images_seen = ?, indexed = ?, errors = ?, note = ?
                   WHERE run_id = ?""",
                (
                    completed,
                    total_files,
                    total_images,
                    indexed,
                    errors,
                    json.dumps(
                        {"walk_seconds": walk_seconds},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    run_id,
                ),
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as error:
            connection.rollback()
            connection.execute(
                """UPDATE index_runs
                   SET completed_at = ?, status = 'failed', note = ?
                   WHERE run_id = ?""",
                (visual_index.utc_now(), str(error)[:2000], run_id),
            )
            connection.commit()
            raise
    database_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    marker = {
        "format": "iss-visual-index-completion-v1",
        "tool_version": TOOL_VERSION,
        "token": token,
        "run_id": run_id,
        "state": "complete",
        "database": str(database),
        "database_sha256": database_digest,
        "roots": [str(root) for root in protected_roots],
        "files_seen": total_files,
        "images_seen": total_images,
        "indexed": indexed,
        "errors": errors,
        "completed_at": completed,
        "source_roots_unchanged": True,
    }
    marker_path = work_root / "completions" / f"{token}.json"
    _exclusive_json(marker_path, marker)
    marker["marker"] = str(marker_path)
    return marker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, action="append", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_index(
            database=args.database,
            work_root=args.work_root,
            roots=args.source_root,
            token=args.token,
            timeout=args.timeout,
        )
    except Exception as error:
        raise SystemExit(f"error: {error}") from error
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
