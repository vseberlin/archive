#!/usr/bin/env python3
"""Exception-only closeout workbench for the Kurt Schwarz MD unit.

The 1,668 published preservation Masters and their object/MD decisions are
frozen.  The indexer reuses the completed SHA-256 inventory and existing object
links, while Czkawka supplies exception evidence only.  The web UI confirms
Redundant or Noise decisions for residual files; it cannot select a new Master,
edit metadata, create a local object, or record a cross-unit decision.  It has
no source mutation, accession, move, rename, or deletion operation.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import html
import json
import os
import sqlite3
import subprocess
import sys
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse


SOURCE_ROOT = Path("/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz/work/simple-workbench")
HASH_INVENTORY = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/source-hashes/runs/"
    "20260730T003000Z-kurt-schwarz-source-hashes/source-hashes.csv"
)
LEGACY_DB = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/reconciliation/"
    "unit-reconciliation-v1/reconciliation.sqlite"
)
METADATA_CSV = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/reconciliation/object-census/"
    "20260804T-kurt-object-census-v10/object-census.csv"
)
CZKAWKA = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/tools/czkawka_cli-12.0.1"
)
PUBLISHED_UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
PUBLISHED_CATALOG = PUBLISHED_UNIT / "CATALOG.csv"
LEGACY_LIST_VIEW = SOURCE_ROOT / "List View.html"
PUBLISHED_REPRESENTATIONS = (
    PUBLISHED_UNIT / "db-restore/manifests/representations.csv"
)
EXPECTED_FROZEN_MASTERS = 1668
LEGACY_PREVIEW_CACHE = LEGACY_DB.parent / "cache/previews"
LEGACY_PREVIEW_VERSION = "3"
IMAGE_EXTENSIONS = {
    ".arw",
    ".avif",
    ".bmp",
    ".cr2",
    ".dng",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".jxl",
    ".nef",
    ".png",
    ".psd",
    ".tif",
    ".tiff",
    ".webp",
}
TERMINAL_LEGACY_STATES = {
    "context_not_object",
    "cross_unit_hold",
    "existing_object",
    "new_local_metadata_right",
    "redundant",
    "same_new_candidate",
    "selected_master",
    "surrogate_master",
    "wrong_match",
    "wrong_object",
}
EDITABLE_METADATA_FIELDS = {
    "title": ("Title", ("current_title", "title")),
    "description": ("Description", ("current_description", "description")),
    "date_text": ("Date", ("current_date_text", "date_text")),
    "material": ("Material", ("current_material", "material")),
    "dimensions": ("Dimensions", ("current_dimensions", "dimensions")),
    "places": ("Places", ("current_places", "places")),
    "tags": ("Tags", ("current_tags", "tags")),
}
BROWSE_PAGE_SIZE = 120
METADATA_PAGE_SIZE = 60
METADATA_DISPOSITIONS = {"keep_current", "propose_legacy", "unresolved"}
DEFICIENCY_LABELS = {
    "high_resolution_residual": "TIFF/high-resolution residual",
}
csv.field_size_limit(64 * 1024 * 1024)


class WorkbenchError(RuntimeError):
    """Fail-closed workbench error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_metadata_text(value: str) -> str:
    return " ".join((value or "").split())


class LegacyListViewParser(HTMLParser):
    """Read the fixed ACDSee-style table without adding a parser dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(normalize_metadata_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None


def read_legacy_list_view(path: Path) -> list[dict[str, str]]:
    parser = LegacyListViewParser()
    parser.feed(path.read_text(encoding="utf-8"))
    fields = (
        "row_number",
        "preview",
        "filename",
        "md_object_id",
        "title",
        "description",
        "tags",
        "source_context",
        "places",
        "date_text",
    )
    return [
        dict(zip(fields, row[: len(fields)], strict=True))
        for row in parser.rows
        if len(row) >= len(fields) and row[0].isdigit()
    ]


def read_published_masters(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [
            dict(row)
            for row in csv.DictReader(handle)
            if row.get("file_role") == "preservation_master"
        ]


def published_access_derivatives(catalog: Path, unit: Path) -> dict[str, Path]:
    unit = unit.resolve(strict=True)
    result: dict[str, Path] = {}
    with catalog.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("file_role") != "access_derivative":
                continue
            record_key = row.get("record_key", "")
            relative_path = row.get("final_relative_path", "")
            if not record_key or not relative_path:
                continue
            path = (unit / relative_path).resolve(strict=True)
            path.relative_to(unit)
            result[record_key] = path
    return result


def split_legacy_description(value: str) -> tuple[str, str]:
    value = normalize_metadata_text(value)
    if "///" in value:
        core, note = value.split("///", 1)
        return core.strip(), note.strip()
    marker = re.search(
        r"\s+\|\s*(?=(?:Originaldateiname|Bildnummer|Taschenbeschriftung|Kasten)\b)",
        value,
        flags=re.IGNORECASE,
    )
    if marker:
        return value[: marker.start()].strip(), value[marker.end() :].strip()
    return value, ""


def metadata_discrepancy_id(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"KSMD-{digest}"


def derive_metadata_discrepancies(
    legacy_rows: list[dict[str, str]], current_rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Return rebuildable assertions; never choose which assertion is authoritative."""
    current_by_inventory: dict[str, list[dict[str, str]]] = defaultdict(list)
    current_by_md: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in current_rows:
        current_by_inventory[normalize_metadata_text(row.get("inventory", "")).casefold()].append(row)
        if str(row.get("md_object_id", "")).isdigit():
            current_by_md[str(row["md_object_id"])].append(row)

    discrepancies: list[dict[str, str]] = []
    def add(
        current: dict[str, str] | None,
        legacy: dict[str, str] | None,
        kind: str,
        field_name: str,
        current_value: str,
        legacy_value: str,
        explanation: str,
    ) -> None:
        record_key = (current or {}).get("record_key", "")
        inventory = (current or {}).get("inventory", "") or Path(
            (legacy or {}).get("filename", "")
        ).stem
        md_object_id = (current or {}).get("md_object_id", "") or (
            legacy or {}
        ).get("md_object_id", "")
        legacy_row_number = (legacy or {}).get("row_number", "")
        stable_record = record_key or f"legacy-row:{legacy_row_number}"
        discrepancies.append(
            {
                "discrepancy_id": metadata_discrepancy_id(
                    stable_record, kind, field_name, legacy_row_number
                ),
                "record_key": record_key,
                "inventory": inventory,
                "md_object_id": md_object_id,
                "kind": kind,
                "field_name": field_name,
                "current_value": normalize_metadata_text(current_value),
                "legacy_value": normalize_metadata_text(legacy_value),
                "legacy_filename": (legacy or {}).get("filename", ""),
                "legacy_row_number": legacy_row_number,
                "explanation": explanation,
            }
        )

    for legacy in legacy_rows:
        inventory = Path(legacy.get("filename", "")).stem
        inventory_matches = current_by_inventory.get(inventory.casefold(), [])
        md_id = legacy.get("md_object_id", "")
        md_matches = current_by_md.get(md_id, []) if md_id.isdigit() else []
        current = None
        if len(inventory_matches) == 1:
            current = inventory_matches[0]
        elif len(md_matches) == 1:
            current = md_matches[0]
        if current is None:
            continue

        legacy_description, legacy_note = split_legacy_description(
            legacy.get("description", "")
        )
        current_description = current.get("description", "")
        if normalize_metadata_text(current_description) != normalize_metadata_text(
            legacy_description
        ):
            add(
                current,
                legacy,
                "field_difference"
                if current_description and legacy_description
                else "missing_value",
                "description",
                current_description,
                legacy_description,
                "Current saved description and legacy caption differ.",
            )
        if legacy_note:
            add(
                current,
                legacy,
                "legacy_source_note",
                "description_enrichment",
                current_description,
                legacy_note,
                "Legacy caption carries a provenance/source note that may enrich the saved description.",
            )
    return sorted(
        discrepancies,
        key=lambda row: (
            row["inventory"].casefold(),
            row["kind"],
            row["field_name"],
            row["discrepancy_id"],
        ),
    )


def ensure_inside(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve(strict=True))
    return resolved


def require_read_only_source(source: Path) -> str:
    source = source.resolve(strict=True)
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(result.stdout).get("filesystems", [])
    if len(filesystems) != 1:
        raise WorkbenchError(f"cannot resolve source mount for {source}")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if "ro" not in options or "rw" in options:
        raise WorkbenchError(f"source mount is not read-only: {mount}")
    return f"{mount.get('source')} {mount.get('target')} {mount.get('fstype')} ro"


def open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def frozen_master_paths(
    representations: Path, source: Path, expected_count: int
) -> set[str]:
    """Return the accepted MD-scope Masters without reopening their decisions."""
    source.resolve(strict=True)
    with representations.open(newline="", encoding="utf-8-sig") as handle:
        masters = {
            row["source_path"]
            for row in csv.DictReader(handle)
            if row.get("role") == "preservation_master"
            and row.get("source_path")
        }
    if len(masters) != expected_count:
        raise WorkbenchError(
            f"frozen Master scope drifted: expected {expected_count}, found {len(masters)}"
        )
    return masters


def walk_strings(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        for item in value:
            found.extend(walk_strings(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(walk_strings(item))
    return found


def latest_legacy_states(database: Path, source: Path) -> dict[str, str]:
    prefix = str(source.resolve(strict=True)) + os.sep
    states: dict[str, str] = {}
    with open_read_only(database) as connection:
        media = {
            row["candidate_id"]: row["disposition"]
            for row in connection.execute(
                """SELECT decision.candidate_id, decision.disposition
                   FROM media_decisions AS decision
                   JOIN (
                       SELECT candidate_id, MAX(decision_id) AS decision_id
                       FROM media_decisions GROUP BY candidate_id
                   ) AS latest USING (candidate_id, decision_id)"""
            )
        }
        for row in connection.execute(
            "SELECT candidate_id, source_occurrences_json FROM media_candidates"
        ):
            try:
                value = json.loads(row["source_occurrences_json"] or "[]")
            except json.JSONDecodeError:
                continue
            for candidate in walk_strings(value):
                if candidate.startswith(prefix):
                    states[candidate] = media.get(row["candidate_id"], "pending")

        census = {
            row["candidate_id"]: row["disposition"]
            for row in connection.execute(
                """SELECT decision.candidate_id, decision.disposition
                   FROM object_census_decisions AS decision
                   JOIN (
                       SELECT candidate_id, MAX(decision_id) AS decision_id
                       FROM object_census_decisions GROUP BY candidate_id
                   ) AS latest USING (candidate_id, decision_id)"""
            )
        }
        for row in connection.execute(
            """SELECT candidate_id, primary_source_path, source_paths_json
               FROM object_census_candidates"""
        ):
            paths: list[str] = []
            try:
                paths.extend(json.loads(row["source_paths_json"] or "[]"))
            except json.JSONDecodeError:
                pass
            if row["primary_source_path"]:
                paths.append(row["primary_source_path"])
            for candidate in paths:
                if candidate.startswith(prefix):
                    states[candidate] = census.get(row["candidate_id"], "pending")
    return states


def selected_catalog_metadata(database: Path, source: Path) -> dict[str, dict]:
    prefix = str(source.resolve(strict=True)) + os.sep
    result: dict[str, dict] = {}
    keys = (
        "current_inventory",
        "current_title",
        "current_description",
        "current_date_text",
        "current_material",
        "current_dimensions",
        "current_places",
        "current_tags",
        "md_object_id",
        "record_key",
    )
    with open_read_only(database) as connection:
        for row in connection.execute("SELECT catalog_json FROM objects"):
            try:
                catalog = json.loads(row["catalog_json"] or "{}")
            except json.JSONDecodeError:
                continue
            compact = {key: catalog.get(key) for key in keys if catalog.get(key)}
            paths = str(catalog.get("all_candidate_paths", "")).split("; ")
            selected = catalog.get("selected_source_path")
            if selected:
                paths.append(selected)
            for candidate in paths:
                if candidate.startswith(prefix):
                    result[candidate] = compact
    return result


def selected_master_links(database: Path, source: Path) -> dict[str, set[str]]:
    """Map each catalogued candidate path to its selected source Master."""
    prefix = str(source.resolve(strict=True)) + os.sep
    result: dict[str, set[str]] = defaultdict(set)
    with open_read_only(database) as connection:
        masters_by_record = {
            row["record_key"]: row["selected_source_path"]
            for row in connection.execute(
                "SELECT record_key, selected_source_path FROM objects"
            )
            if row["selected_source_path"]
            and row["selected_source_path"].startswith(prefix)
        }
        for row in connection.execute(
            "SELECT selected_source_path, catalog_json FROM objects"
        ):
            selected = row["selected_source_path"]
            if not selected or not selected.startswith(prefix):
                continue
            try:
                catalog = json.loads(row["catalog_json"] or "{}")
            except json.JSONDecodeError:
                continue
            paths = str(catalog.get("all_candidate_paths", "")).split("; ")
            paths.append(selected)
            for candidate in paths:
                if candidate.startswith(prefix):
                    result[candidate].add(selected)

        census_paths: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT candidate_id, primary_source_path, source_paths_json "
            "FROM object_census_candidates"
        ):
            paths: list[str] = []
            try:
                paths.extend(json.loads(row["source_paths_json"] or "[]"))
            except json.JSONDecodeError:
                pass
            if row["primary_source_path"]:
                paths.append(row["primary_source_path"])
            census_paths[row["candidate_id"]] = [
                path for path in paths if path.startswith(prefix)
            ]
        targets = {
            row["candidate_id"]: row["target_record_key"]
            for row in connection.execute(
                """SELECT decision.candidate_id, decision.target_record_key
                   FROM object_census_decisions AS decision
                   JOIN (
                     SELECT candidate_id, MAX(decision_id) AS decision_id
                     FROM object_census_decisions GROUP BY candidate_id
                   ) AS latest USING (candidate_id, decision_id)
                   WHERE decision.disposition='existing_object'
                     AND decision.target_record_key IS NOT NULL"""
            )
        }
        same_photo = list(
            connection.execute(
                """SELECT decision.candidate_a_id, decision.candidate_b_id
                   FROM object_census_similarity_decisions AS decision
                   JOIN (
                     SELECT pair_id, MAX(decision_id) AS decision_id
                     FROM object_census_similarity_decisions GROUP BY pair_id
                   ) AS latest USING (pair_id, decision_id)
                   WHERE decision.outcome='same_photo'"""
            )
        )
        changed = True
        while changed:
            changed = False
            for row in same_photo:
                left, right = row[0], row[1]
                if left in targets and right not in targets:
                    targets[right] = targets[left]
                    changed = True
                elif right in targets and left not in targets:
                    targets[left] = targets[right]
                    changed = True
        for candidate_id, record_key in targets.items():
            master = masters_by_record.get(record_key)
            if not master:
                continue
            for candidate in census_paths.get(candidate_id, []):
                result[candidate].add(master)
    return result


def simple_decision_links(database: Path) -> dict[str, set[str]]:
    if not database.is_file():
        return {}
    result: dict[str, set[str]] = defaultdict(set)
    with open_read_only(database) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'"
        ).fetchone():
            return {}
        for row in connection.execute(
            "SELECT path, disposition, master_path, actor FROM decisions"
        ):
            if row["actor"] == "workbench":
                continue
            master = row["path"] if row["disposition"] == "master" else row["master_path"]
            if master:
                result[row["path"]].add(master)
    return result


def _similarity_functions():
    tools = str(Path(__file__).resolve().parent)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from kurt_schwarz_similarity import extended_hashes
    from kurt_schwarz_visual_verify import DCT_32, hamming, image_hashes

    return image_hashes, extended_hashes, hamming, DCT_32


def selected_master_records(
    database: Path, source: Path, frozen_masters: set[str]
) -> list[dict]:
    prefix = str(source.resolve(strict=True)) + os.sep
    records: list[dict] = []
    with open_read_only(database) as connection:
        objects = {
            row["record_key"]: dict(row)
            for row in connection.execute(
                "SELECT record_key, selected_source_path, catalog_json FROM objects"
            )
        }
        rows = connection.execute(
            """SELECT media.candidate_id, media.record_key, media.final_path,
                      analysis.width_px, analysis.height_px
               FROM media_candidates AS media
               LEFT JOIN technical_analysis AS analysis
                 ON analysis.candidate_id=media.candidate_id
               JOIN media_decisions AS decision
                 ON decision.candidate_id=media.candidate_id
               JOIN (
                 SELECT candidate_id, MAX(decision_id) AS decision_id
                 FROM media_decisions GROUP BY candidate_id
               ) AS latest USING (candidate_id, decision_id)
               WHERE decision.disposition='selected_master'
               ORDER BY media.record_key"""
        )
        for row in rows:
            object_row = objects.get(row["record_key"])
            if not object_row:
                continue
            master = object_row["selected_source_path"]
            if (
                not master
                or not master.startswith(prefix)
                or master not in frozen_masters
            ):
                continue
            published = (PUBLISHED_UNIT / row["final_path"]).resolve(strict=True)
            published.relative_to(PUBLISHED_UNIT.resolve(strict=True))
            stat = published.stat()
            digest = hashlib.sha256(
                (
                    f"jpeg-preview-v{LEGACY_PREVIEW_VERSION}\0"
                    f"{row['candidate_id']}\0{stat.st_size}\0{stat.st_mtime_ns}"
                ).encode()
            ).hexdigest()
            preview = LEGACY_PREVIEW_CACHE / f"{digest}.jpg"
            if not preview.is_file() or preview.stat().st_size == 0:
                continue
            try:
                catalog = json.loads(object_row["catalog_json"] or "{}")
            except json.JSONDecodeError:
                catalog = {}
            records.append(
                {
                    "path": master,
                    "candidate_id": row["candidate_id"],
                    "record_key": row["record_key"],
                    "inventory": catalog.get("current_inventory", ""),
                    "title": catalog.get("current_title", ""),
                    "catalog": catalog,
                    "preview": preview,
                    "width_px": row["width_px"],
                    "height_px": row["height_px"],
                }
            )
    return records


TOKEN_STOP_WORDS = {
    "ausschnitt",
    "berlin",
    "bild",
    "black",
    "foto",
    "image",
    "mitte",
    "schwarz",
    "scan",
    "strasse",
    "upload",
}


def search_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = normalized.encode("ascii", "ignore").decode()
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) >= 4 and token not in TOKEN_STOP_WORDS
    }


def matching_tokens(left: set[str], right: set[str]) -> set[str]:
    return {
        token
        for token in left
        if any(
            token == candidate
            or (
                min(len(token), len(candidate)) >= 6
                and (token.startswith(candidate) or candidate.startswith(token))
            )
            for candidate in right
        )
    }


def hash_values(path: Path) -> tuple[int, int, int, int, int]:
    import numpy as np
    from PIL import Image, ImageOps

    _, _, _, dct = _similarity_functions()
    try:
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert("L")
    except TypeError:
        # Some legacy TIFFs contain tuple-valued EXIF orientation data.
        with Image.open(path) as opened:
            image = opened.convert("L")

    pixels = np.asarray(
        image.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64
    )
    coefficients = dct @ pixels @ dct.T
    values = coefficients[:8, :8].flatten()[1:]
    phash = 0
    for bit in values >= np.median(values):
        phash = (phash << 1) | int(bit)

    difference = np.asarray(
        image.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16
    )
    dhash = 0
    for bit in (difference[:, 1:] > difference[:, :-1]).flat:
        dhash = (dhash << 1) | int(bit)

    mean_pixels = np.asarray(
        image.resize((16, 16), Image.Resampling.LANCZOS), dtype=np.float64
    )
    mean_hash = 0
    for bit in (mean_pixels >= np.median(mean_pixels)).flat:
        mean_hash = (mean_hash << 1) | int(bit)
    gradient_pixels = np.asarray(
        image.resize((17, 16), Image.Resampling.LANCZOS), dtype=np.int16
    )
    gradient_hash = 0
    for bit in (gradient_pixels[:, 1:] > gradient_pixels[:, :-1]).flat:
        gradient_hash = (gradient_hash << 1) | int(bit)
    block_hash = 0
    for top in range(0, 16, 4):
        for left in range(0, 16, 4):
            block = mean_pixels[top : top + 4, left : left + 4]
            for bit in (block >= block.mean()).flat:
                block_hash = (block_hash << 1) | int(bit)
    return phash, dhash, mean_hash, gradient_hash, block_hash


def crop_hash_distance(
    query: tuple[int, int, int, int, int], preview: Path
) -> tuple[int, int]:
    import numpy as np
    from PIL import Image, ImageOps

    _, _, hamming, dct = _similarity_functions()

    def hashes(image: Image.Image) -> tuple[int, int]:
        grayscale = ImageOps.autocontrast(image.convert("L"))
        pixels = np.asarray(
            grayscale.resize((32, 32), Image.Resampling.LANCZOS),
            dtype=np.float64,
        )
        coefficients = dct @ pixels @ dct.T
        values = coefficients[:8, :8].flatten()[1:]
        median = float(np.median(values))
        phash = 0
        for bit in values >= median:
            phash = (phash << 1) | int(bit)
        difference = np.asarray(
            grayscale.resize((9, 8), Image.Resampling.LANCZOS),
            dtype=np.int16,
        )
        dhash = 0
        for bit in (difference[:, 1:] > difference[:, :-1]).flat:
            dhash = (dhash << 1) | int(bit)
        return phash, dhash

    best = (65, 65)
    with Image.open(preview) as opened:
        master = ImageOps.exif_transpose(opened).convert("L")
        width, height = master.size
        for width_fraction in (1.0, 0.9, 0.8, 0.7):
            for height_fraction in (1.0, 0.9, 0.8, 0.7):
                crop_width = round(width * width_fraction)
                crop_height = round(height * height_fraction)
                for horizontal in (0.0, 0.5, 1.0):
                    for vertical in (0.0, 0.5, 1.0):
                        left = round((width - crop_width) * horizontal)
                        top = round((height - crop_height) * vertical)
                        phash, dhash = hashes(
                            master.crop(
                                (
                                    left,
                                    top,
                                    left + crop_width,
                                    top + crop_height,
                                )
                            )
                        )
                        distance = (
                            hamming(query[0], phash),
                            hamming(query[1], dhash),
                        )
                        if (sum(distance), *distance) < (sum(best), *best):
                            best = distance
    return best


def compact_match(value: dict) -> dict:
    keys = (
        "inventory",
        "title",
        "description",
        "date_text",
        "material",
        "dimensions",
        "places",
        "tags",
        "source_document",
        "sheet_name",
        "row_number",
        "match_basis",
    )
    return {key: value.get(key) for key in keys if value.get(key)}


def census_metadata(path: Path, source: Path) -> dict[str, dict]:
    prefix = str(source.resolve(strict=True)) + os.sep
    result: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            paths: list[str] = []
            try:
                paths.extend(json.loads(row.get("source_paths_json") or "[]"))
            except json.JSONDecodeError:
                pass
            if row.get("primary_source_path"):
                paths.append(row["primary_source_path"])
            try:
                selected = json.loads(row.get("selected_metadata_json") or "{}")
            except json.JSONDecodeError:
                selected = {}
            try:
                matches = json.loads(row.get("metadata_matches_json") or "[]")
            except json.JSONDecodeError:
                matches = []
            value = {
                "selected": compact_match(selected),
                "summary": row.get("metadata_summary", ""),
                "match_count": int(row.get("metadata_match_count") or 0),
                "evidence": [compact_match(item) for item in matches[:8]],
            }
            for candidate in paths:
                if candidate.startswith(prefix):
                    result[candidate] = value
    return result


def load_hash_inventory(path: Path, source: Path) -> list[dict]:
    prefix = str(source.resolve(strict=True)) + os.sep
    files: list[dict] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            candidate = row["source_path"]
            if not candidate.startswith(prefix):
                continue
            extension = Path(candidate).suffix.lower()
            if extension not in IMAGE_EXTENSIONS or row["hash_state"] != "hashed":
                continue
            files.append(
                {
                    "path": candidate,
                    "relative_path": Path(candidate).relative_to(source).as_posix(),
                    "sha256": row["sha256"],
                    "size_bytes": int(row["size_bytes"]),
                    "mtime_ns": int(row["mtime_ns"]),
                    "extension": extension,
                }
            )
    return files


def run_czkawka(
    binary: Path,
    source: Path,
    candidates: list[Path],
    work: Path,
    difference: int,
) -> Path:
    binary = binary.resolve(strict=True)
    output = work / "czkawka-groups.json"
    cache = work / "czkawka-cache"
    cache.mkdir(parents=True, exist_ok=True)
    temporary = work / f".czkawka-groups-{os.getpid()}.json"
    environment = os.environ.copy()
    environment["CZKAWKA_CACHE_PATH"] = str(cache)
    environment["CZKAWKA_CONFIG_PATH"] = str(cache)
    command = [
        str(binary),
        "image",
        "-x",
        ",".join(sorted(extension.removeprefix(".") for extension in IMAGE_EXTENSIONS)),
        "-g",
        "Gradient",
        "-c",
        "16",
        "-s",
        str(difference),
        "-p",
        str(temporary),
        "-N",
        "-W",
        "-T",
        "4",
    ]
    for candidate in candidates:
        command.extend(["-d", str(ensure_inside(candidate, source))])
    command_bytes = sum(len(value.encode()) + 1 for value in command)
    if command_bytes > os.sysconf("SC_ARG_MAX") - 65536:
        raise WorkbenchError(
            f"unresolved file list exceeds command limit: {command_bytes} bytes"
        )
    subprocess.run(command, check=True, env=environment)
    temporary.replace(output)
    return output


class Components:
    def __init__(self, members: set[str]):
        self.parent = {member: member for member in members}

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        first, second = self.find(left), self.find(right)
        if first != second:
            self.parent[max(first, second)] = min(first, second)

    def groups(self) -> list[list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            grouped[self.find(item)].append(item)
        return [sorted(items) for items in grouped.values() if len(items) > 1]


def grouped_unresolved(
    files: list[dict], legacy: dict[str, str], czkawka_groups: list
) -> tuple[list[list[str]], dict[str, int]]:
    unresolved = {
        item["path"]
        for item in files
        if legacy.get(item["path"], "pending") not in TERMINAL_LEGACY_STATES
    }
    components = Components(unresolved)
    difference: dict[str, int] = defaultdict(int)
    by_digest: dict[str, list[str]] = defaultdict(list)
    for item in files:
        if item["path"] in unresolved:
            by_digest[item["sha256"]].append(item["path"])
    for paths in by_digest.values():
        for candidate in paths[1:]:
            components.union(paths[0], candidate)
    for group in czkawka_groups:
        members = [item for item in group if item.get("path") in unresolved]
        if len(members) < 2:
            continue
        first = members[0]["path"]
        for item in members:
            components.union(first, item["path"])
            difference[item["path"]] = max(
                difference[item["path"]], int(item.get("difference", 0))
            )
    return components.groups(), difference


def include_singleton_groups(
    groups: list[list[str]], files: list[dict], legacy: dict[str, str]
) -> list[list[str]]:
    """Make every unreconciled image reviewable without changing similarity groups."""
    grouped = {path for members in groups for path in members}
    singletons = [
        [item["path"]]
        for item in files
        if item["path"] not in grouped
        and legacy.get(item["path"], "pending") not in TERMINAL_LEGACY_STATES
    ]
    return groups + singletons


def master_references(
    groups: list[list[str]],
    links: dict[str, set[str]],
    legacy: dict[str, str],
    known_paths: set[str],
) -> list[list[str]]:
    references: list[list[str]] = []
    for members in groups:
        selected = {
            master
            for member in members
            for master in links.get(member, set())
            if master in known_paths and legacy.get(master) == "selected_master"
        }
        references.append(sorted(selected))
    return references


def suggested_master_references(
    groups: list[list[str]],
    references: list[list[str]],
    files: list[dict],
    masters: list[dict],
) -> tuple[list[list[dict]], int]:
    by_path = {item["path"]: item for item in files}
    hashed_masters: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(hash_values, item["preview"]): item for item in masters
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                values = future.result()
            except (OSError, TypeError, ValueError):
                continue
            catalog_text = json.dumps(
                item["catalog"], ensure_ascii=False, sort_keys=True
            )
            hashed_masters.append(
                {**item, "hashes": values, "tokens": search_tokens(catalog_text)}
            )
    hashed_masters.sort(key=lambda item: item["path"])
    token_frequency = Counter(
        token for item in hashed_masters for token in item["tokens"]
    )
    _, _, hamming, _ = _similarity_functions()
    suggestions: list[list[dict]] = []
    for members, resolved in zip(groups, references, strict=True):
        if resolved:
            suggestions.append([])
            continue
        available = [by_path[path] for path in members if path in by_path]
        if not available:
            suggestions.append([])
            continue
        representative = min(
            available, key=lambda item: (item["size_bytes"], item["path"])
        )
        try:
            candidate_hashes = hash_values(Path(representative["path"]))
        except (OSError, TypeError, ValueError):
            suggestions.append([])
            continue
        tokens = set()
        for item in available:
            tokens.update(search_tokens(Path(item["path"]).name))
        possible: list[dict] = []
        for master in hashed_masters:
            distances = tuple(
                hamming(left, right)
                for left, right in zip(candidate_hashes, master["hashes"])
            )
            text_matches = matching_tokens(tokens, master["tokens"])
            text_is_distinctive = (
                len(text_matches) >= 2
                and any(
                    token.isdigit() and 1900 <= int(token) <= 2099
                    for token in text_matches
                )
                and any(
                    not token.isdigit() and token_frequency[token] <= 10
                    for token in text_matches
                )
            )
            bounded = (
                distances[0] <= 8 and distances[1] <= 10
            ) or (
                distances[2] <= 16
                and distances[3] <= 20
                and distances[4] <= 20
            )
            if not bounded and not text_is_distinctive:
                continue
            crop_phash, crop_dhash = distances[0], distances[1]
            basis = "bounded selected-Master perceptual hashes"
            if not bounded:
                crop_phash, crop_dhash = crop_hash_distance(
                    candidate_hashes, master["preview"]
                )
                if crop_phash > 18 or crop_dhash > 20:
                    continue
                basis = "filename evidence plus crop-aware perceptual hashes"
            possible.append(
                {
                    "path": master["path"],
                    "difference": crop_phash + crop_dhash,
                    "evidence": (
                        f"{basis}; pHash {crop_phash}; dHash {crop_dhash}"
                        + (
                            "; tokens " + ", ".join(sorted(text_matches))
                            if text_matches
                            else ""
                        )
                    ),
                }
            )
        possible.sort(
            key=lambda item: (item["difference"], item["path"])
        )
        suggestions.append(possible[:3])
    return suggestions, len(hashed_masters)


def automatic_redundant_decisions(
    group_id: str,
    members: list[str],
    references: list[str],
    links: dict[str, set[str]],
    existing: dict[str, str],
    decided_at: str,
) -> list[tuple]:
    if len(references) != 1:
        return []
    master = references[0]
    if any(existing.get(member) == "master" for member in members):
        return []
    note = (
        "Established object candidate relationship; frozen Master remains unchanged."
    )
    return [
        (
            member,
            group_id,
            "redundant",
            master,
            note,
            "workbench",
            decided_at,
        )
        for member in members
        if member not in existing and links.get(member, set()) == {master}
    ]


def fill_group_dimensions(
    files: list[dict], groups: list[list[str]], dimensions: dict[str, tuple[int, int]]
) -> None:
    from PIL import Image

    grouped_paths = {path for group in groups for path in group}
    by_digest: dict[str, list[str]] = defaultdict(list)
    for item in files:
        if item["path"] in grouped_paths:
            by_digest[item["sha256"]].append(item["path"])
    missing: list[list[str]] = []
    for paths in by_digest.values():
        known = next((dimensions[path] for path in paths if path in dimensions), None)
        if known is not None:
            for path in paths:
                dimensions[path] = known
        else:
            missing.append(paths)

    def read_dimensions(path: str) -> tuple[int, int]:
        with Image.open(path) as image:
            return int(image.width), int(image.height)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(read_dimensions, paths[0]): paths for paths in missing
        }
        for future in as_completed(futures):
            paths = futures[future]
            try:
                known = future.result()
            except (OSError, TypeError, ValueError):
                continue
            for path in paths:
                dimensions[path] = known


def create_database(
    target: Path,
    source: Path,
    files: list[dict],
    groups: list[list[str]],
    references: list[list[str]],
    suggestions: list[list[dict]],
    differences: dict[str, int],
    dimensions: dict[str, tuple[int, int]],
    legacy: dict[str, str],
    links: dict[str, set[str]],
    metadata: dict[str, dict],
    mount: str,
    frozen_master_count: int,
) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    old_decisions: list[tuple] = []
    old_metadata_edits: list[tuple] = []
    old_metadata_discrepancy_decisions: list[tuple] = []
    if target.exists():
        with open_read_only(target) as old:
            old_decisions = list(
                old.execute(
                    """SELECT path, group_id, disposition, master_path, note,
                              actor, decided_at FROM decisions"""
                )
            )
            if old.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_edits'"
            ).fetchone():
                old_metadata_edits = list(
                    old.execute(
                        """SELECT master_path, field_name, value, group_id, actor,
                                  decided_at FROM metadata_edits"""
                    )
                )
            if old.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='metadata_discrepancy_decisions'"
            ).fetchone():
                decision_columns = {
                    row[1]
                    for row in old.execute(
                        "PRAGMA table_info(metadata_discrepancy_decisions)"
                    )
                }
                context_columns = {
                    "record_key",
                    "inventory",
                    "md_object_id",
                    "kind",
                    "field_name",
                    "legacy_filename",
                    "legacy_row_number",
                    "legacy_source_path",
                }
                context_select = (
                    "record_key, inventory, md_object_id, kind, field_name, "
                    "legacy_filename, legacy_row_number, legacy_source_path"
                    if context_columns.issubset(decision_columns)
                    else "'', '', '', '', '', '', '', ''"
                )
                old_metadata_discrepancy_decisions = list(
                    old.execute(
                        f"""SELECT discrepancy_id, {context_select}, disposition,
                                   chosen_value, note, actor, decided_at,
                                   current_value, legacy_value
                            FROM metadata_discrepancy_decisions
                            ORDER BY decision_id"""
                    )
                )
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    connection = sqlite3.connect(temporary)
    automatic_decisions: list[tuple] = []
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                extension TEXT NOT NULL,
                width_px INTEGER,
                height_px INTEGER,
                legacy_state TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE groups (
                group_id TEXT NOT NULL,
                member_path TEXT NOT NULL REFERENCES files(path),
                difference INTEGER NOT NULL,
                is_master_reference INTEGER NOT NULL CHECK (
                    is_master_reference IN (0, 1, 2)
                ),
                evidence TEXT NOT NULL,
                PRIMARY KEY (group_id, member_path)
            );
            CREATE TABLE decisions (
                path TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK (
                    disposition IN ('master', 'redundant', 'noise', 'cross_unit')
                ),
                master_path TEXT,
                note TEXT NOT NULL,
                actor TEXT NOT NULL,
                decided_at TEXT NOT NULL
            );
            CREATE TABLE metadata_edits (
                master_path TEXT NOT NULL REFERENCES files(path),
                field_name TEXT NOT NULL,
                value TEXT NOT NULL,
                group_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                PRIMARY KEY (master_path, field_name)
            );
            CREATE TABLE metadata_discrepancies (
                discrepancy_id TEXT PRIMARY KEY,
                record_key TEXT NOT NULL,
                inventory TEXT NOT NULL,
                md_object_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                field_name TEXT NOT NULL,
                current_value TEXT NOT NULL,
                legacy_value TEXT NOT NULL,
                legacy_filename TEXT NOT NULL,
                legacy_row_number TEXT NOT NULL,
                legacy_source_path TEXT NOT NULL,
                explanation TEXT NOT NULL
            );
            CREATE TABLE metadata_discrepancy_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                discrepancy_id TEXT NOT NULL,
                record_key TEXT NOT NULL,
                inventory TEXT NOT NULL,
                md_object_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                field_name TEXT NOT NULL,
                legacy_filename TEXT NOT NULL,
                legacy_row_number TEXT NOT NULL,
                legacy_source_path TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK (
                    disposition IN ('keep_current', 'propose_legacy', 'unresolved')
                ),
                chosen_value TEXT NOT NULL,
                note TEXT NOT NULL,
                actor TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                current_value TEXT NOT NULL,
                legacy_value TEXT NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        connection.executemany(
            """INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    item["path"],
                    item["relative_path"],
                    item["sha256"],
                    item["size_bytes"],
                    item["mtime_ns"],
                    item["extension"],
                    dimensions.get(item["path"], (None, None))[0],
                    dimensions.get(item["path"], (None, None))[1],
                    legacy.get(item["path"], "pending"),
                    json.dumps(metadata.get(item["path"], {}), ensure_ascii=False),
                )
                for item in files
            ],
        )
        old_decisions = [
            row
            for row in old_decisions
            if row[5] != "workbench"
            or (
                row[2] == "redundant"
                and row[3] is not None
                and links.get(row[0], set()) == {row[3]}
            )
        ]
        existing = {row[0]: row[2] for row in old_decisions}
        decided_at = utc_now()
        for members, selected, proposed in zip(
            groups, references, suggestions, strict=True
        ):
            digest = hashlib.sha256("\0".join(members).encode()).hexdigest()[:24]
            group_id = f"KSG-{digest}"
            connection.executemany(
                "INSERT INTO groups VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        group_id,
                        member,
                        differences.get(member, 0),
                        0,
                        "Czkawka unresolved-image group",
                    )
                    for member in members
                ]
                + [
                    (
                        group_id,
                        master,
                        0,
                        1,
                        "Previous reconciliation selected this Master",
                    )
                    for master in selected
                ]
                + [
                    (
                        group_id,
                        item["path"],
                        item["difference"],
                        2,
                        item["evidence"],
                    )
                    for item in proposed
                    if item["path"] not in selected
                ],
            )
            automatic_decisions.extend(
                automatic_redundant_decisions(
                    group_id,
                    members,
                    selected,
                    links,
                    existing,
                    decided_at,
                )
            )
        known_paths = {item["path"] for item in files}
        connection.executemany(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [row for row in old_decisions if row[0] in known_paths],
        )
        connection.executemany(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
            automatic_decisions,
        )
        connection.executemany(
            "INSERT INTO metadata_edits VALUES (?, ?, ?, ?, ?, ?)",
            [row for row in old_metadata_edits if row[0] in known_paths],
        )
        connection.executemany(
            """INSERT INTO metadata_discrepancy_decisions(
                 discrepancy_id, record_key, inventory, md_object_id, kind,
                 field_name, legacy_filename, legacy_row_number,
                 legacy_source_path, disposition, chosen_value, note, actor,
                 decided_at, current_value, legacy_value
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            old_metadata_discrepancy_decisions,
        )
        settings = {
            "built_at": utc_now(),
            "source_root": str(source),
            "source_mount": mount,
            "czkawka": "12.0.1 Gradient 16 difference 5",
            "scope": "frozen-kurt-md-closeout",
            "frozen_master_count": str(frozen_master_count),
            "deletion_authorized": "false",
        }
        connection.executemany("INSERT INTO settings VALUES (?, ?)", settings.items())
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise WorkbenchError("replacement database failed quick_check")
    finally:
        connection.close()
    temporary.replace(target)
    return len(automatic_decisions)


def build(args: argparse.Namespace) -> None:
    source = args.source.resolve(strict=True)
    mount = require_read_only_source(source)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    files = load_hash_inventory(args.hash_inventory, source)
    frozen_masters = frozen_master_paths(
        args.representations_csv, source, args.expected_frozen_masters
    )
    legacy = latest_legacy_states(args.legacy_db, source)
    links = selected_master_links(args.legacy_db, source)
    links = {
        candidate: masters & frozen_masters
        for candidate, masters in links.items()
        if masters & frozen_masters
    }
    previous_simple_links = simple_decision_links(work / "workbench.sqlite")
    for candidate, masters in previous_simple_links.items():
        links.setdefault(candidate, set()).update(masters)
        if candidate in masters:
            legacy[candidate] = "selected_master"
    selected_masters = selected_master_records(
        args.legacy_db, source, frozen_masters
    )
    metadata = selected_catalog_metadata(args.legacy_db, source)
    for candidate, value in census_metadata(args.metadata_csv, source).items():
        metadata[candidate] = value
    scan_by_digest: dict[str, Path] = {}
    for item in files:
        if legacy.get(item["path"], "pending") not in TERMINAL_LEGACY_STATES:
            scan_by_digest.setdefault(item["sha256"], Path(item["path"]))
    scan_paths = sorted(scan_by_digest.values())
    result_path = work / "czkawka-groups.json"
    if args.reuse_czkawka:
        if not result_path.is_file():
            raise WorkbenchError(f"Czkawka result does not exist: {result_path}")
    else:
        result_path = run_czkawka(
            args.czkawka, source, scan_paths, work, args.max_difference
        )
    czkawka_groups = json.loads(result_path.read_text(encoding="utf-8"))
    groups, differences = grouped_unresolved(files, legacy, czkawka_groups)
    groups = include_singleton_groups(groups, files, legacy)
    references = master_references(
        groups, links, legacy, {item["path"] for item in files}
    )
    suggestions, compared_master_count = suggested_master_references(
        groups, references, files, selected_masters
    )
    dimensions = {
        item["path"]: (int(item["width"]), int(item["height"]))
        for group in czkawka_groups
        for item in group
        if item.get("width") and item.get("height")
    }
    dimensions.update(
        {
            item["path"]: (int(item["width_px"]), int(item["height_px"]))
            for item in selected_masters
            if item.get("width_px") and item.get("height_px")
        }
    )
    fill_group_dimensions(
        files,
        [
            members
            + selected
            + [item["path"] for item in proposed]
            for members, selected, proposed in zip(
                groups, references, suggestions, strict=True
            )
        ],
        dimensions,
    )
    automatic_redundant = create_database(
        work / "workbench.sqlite",
        source,
        files,
        groups,
        references,
        suggestions,
        differences,
        dimensions,
        legacy,
        links,
        metadata,
        mount,
        len(frozen_masters),
    )
    with connect_database(work / "workbench.sqlite") as connection:
        write_decisions_csv(connection, work / "decisions.csv")
        write_metadata_edits_csv(connection, work / "metadata-edits.csv")
    print(
        json.dumps(
            {
                "source_mount": mount,
                "frozen_master_count": len(frozen_masters),
                "image_files": len(files),
                "czkawka_scanned_files": len(scan_paths),
                "czkawka_groups": len(czkawka_groups),
                "unresolved_groups": len(groups),
                "master_references": sum(map(len, references)),
                "master_suggestions": sum(map(len, suggestions)),
                "selected_masters_compared": compared_master_count,
                "automatic_redundant": automatic_redundant,
                "database": str(work / "workbench.sqlite"),
                "deletion_authorized": False,
            },
            indent=2,
        )
    )


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def pending_group_ids(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            """SELECT groups.group_id
               FROM groups
               LEFT JOIN decisions ON decisions.path=groups.member_path
               GROUP BY groups.group_id
               HAVING SUM(CASE WHEN groups.is_master_reference=1
                                     THEN 1 ELSE 0 END) >= 1
                  AND SUM(CASE WHEN groups.is_master_reference=0
                                     AND decisions.path IS NULL THEN 1 ELSE 0 END) >= 1
               ORDER BY MIN(groups.difference), groups.group_id"""
        )
    ]


def group_rows(connection: sqlite3.Connection, group_id: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """SELECT files.*, groups.difference, groups.is_master_reference,
                      groups.evidence,
                      decisions.disposition,
                      COALESCE((
                        SELECT json_group_object(field_name, value)
                        FROM metadata_edits WHERE master_path=files.path
                      ), '{}') AS proposed_metadata_json
               FROM groups JOIN files ON files.path=groups.member_path
               LEFT JOIN decisions ON decisions.path=files.path
               WHERE groups.group_id=?
               ORDER BY groups.is_master_reference DESC,
                        files.size_bytes DESC, files.path""",
            (group_id,),
        )
    )


def write_decisions_csv(connection: sqlite3.Connection, target: Path) -> None:
    rows = list(
        connection.execute(
            """SELECT path, group_id, disposition, master_path, note, actor,
                      decided_at FROM decisions ORDER BY path"""
        )
    )
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "source_path",
                "group_id",
                "disposition",
                "master_path",
                "note",
                "actor",
                "decided_at",
                "deletion_authorized",
            ]
        )
        for row in rows:
            writer.writerow([*row, "false"])
    temporary.replace(target)


def write_metadata_edits_csv(
    connection: sqlite3.Connection, target: Path
) -> None:
    rows = list(
        connection.execute(
            """SELECT master_path, field_name, value, group_id, actor, decided_at
               FROM metadata_edits ORDER BY master_path, field_name"""
        )
    )
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "master_path",
                "field_name",
                "proposed_value",
                "group_id",
                "actor",
                "decided_at",
            ]
        )
        writer.writerows(rows)
    temporary.replace(target)


def ensure_metadata_discrepancy_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata_discrepancies (
            discrepancy_id TEXT PRIMARY KEY,
            record_key TEXT NOT NULL,
            inventory TEXT NOT NULL,
            md_object_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            field_name TEXT NOT NULL,
            current_value TEXT NOT NULL,
            legacy_value TEXT NOT NULL,
            legacy_filename TEXT NOT NULL,
            legacy_row_number TEXT NOT NULL,
            legacy_source_path TEXT NOT NULL DEFAULT '',
            explanation TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS metadata_discrepancy_decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discrepancy_id TEXT NOT NULL,
            record_key TEXT NOT NULL DEFAULT '',
            inventory TEXT NOT NULL DEFAULT '',
            md_object_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            field_name TEXT NOT NULL DEFAULT '',
            legacy_filename TEXT NOT NULL DEFAULT '',
            legacy_row_number TEXT NOT NULL DEFAULT '',
            legacy_source_path TEXT NOT NULL DEFAULT '',
            disposition TEXT NOT NULL CHECK (
                disposition IN ('keep_current', 'propose_legacy', 'unresolved')
            ),
            chosen_value TEXT NOT NULL,
            note TEXT NOT NULL,
            actor TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            current_value TEXT NOT NULL,
            legacy_value TEXT NOT NULL
        );
        """
    )
    expected_columns = {
        "metadata_discrepancies": ("legacy_source_path",),
        "metadata_discrepancy_decisions": (
            "record_key",
            "inventory",
            "md_object_id",
            "kind",
            "field_name",
            "legacy_filename",
            "legacy_row_number",
            "legacy_source_path",
        ),
    }
    for table, columns in expected_columns.items():
        existing = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for column in columns:
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} "
                    "TEXT NOT NULL DEFAULT ''"
                )


def sync_metadata_discrepancies(
    connection: sqlite3.Connection, list_view: Path, catalog: Path
) -> int:
    discrepancies = derive_metadata_discrepancies(
        read_legacy_list_view(list_view), read_published_masters(catalog)
    )
    for row in discrepancies:
        row["legacy_source_path"] = str(list_view)
    ensure_metadata_discrepancy_schema(connection)
    with connection:
        connection.execute("DELETE FROM metadata_discrepancies")
        connection.executemany(
            """INSERT INTO metadata_discrepancies(
                 discrepancy_id, record_key, inventory, md_object_id,
                 kind, field_name, current_value, legacy_value,
                 legacy_filename, legacy_row_number, legacy_source_path,
                 explanation
               ) VALUES (
                 :discrepancy_id, :record_key, :inventory, :md_object_id,
                 :kind, :field_name, :current_value, :legacy_value,
                 :legacy_filename, :legacy_row_number, :legacy_source_path,
                 :explanation
               )""",
            discrepancies,
        )
    return len(discrepancies)


def metadata_discrepancy_rows(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """WITH latest AS (
             SELECT decisions.*
             FROM metadata_discrepancy_decisions decisions
             JOIN (
               SELECT discrepancy_id, MAX(decision_id) AS decision_id
               FROM metadata_discrepancy_decisions GROUP BY discrepancy_id
             ) selected USING(discrepancy_id, decision_id)
           )
           SELECT discrepancies.*, latest.disposition, latest.note,
                  latest.actor, latest.decided_at, latest.chosen_value,
                  latest.current_value AS decided_current_value,
                  latest.legacy_value AS decided_legacy_value
           FROM metadata_discrepancies discrepancies
           LEFT JOIN latest USING(discrepancy_id)
           ORDER BY inventory COLLATE NOCASE, kind, field_name"""
    )
    prepared = []
    for source in rows:
        row = dict(source)
        stale = bool(row["disposition"]) and (
            row["current_value"] != row["decided_current_value"]
            or row["legacy_value"] != row["decided_legacy_value"]
        )
        row["stale"] = stale
        row["status"] = "pending" if not row["disposition"] or stale else "reviewed"
        prepared.append(row)
    return prepared


def metadata_difference_html(row: dict) -> str:
    current = str(row.get("current_value") or "")
    legacy = str(row.get("legacy_value") or "")
    labels = {
        "current_only_record": "No List View match",
        "field_difference": "Changed value",
        "identifier_conflict": "Conflicting identifier",
        "legacy_only_record": "No current Master match",
        "legacy_source_context": "Additional technical context",
        "legacy_source_note": "Uncarried legacy note",
        "missing_value": "Value missing on one side",
    }
    detail = ""
    if row.get("kind") == "legacy_source_note":
        detail = (
            '<div class=diff-add><strong>+ Provenance/source to consider:</strong> '
            + html.escape(legacy[:500])
            + "</div>"
        )
    elif current and legacy:
        removed: list[str] = []
        added: list[str] = []
        current_words = current.split()
        legacy_words = legacy.split()
        for operation, left_start, left_end, right_start, right_end in difflib.SequenceMatcher(
            None, current_words, legacy_words
        ).get_opcodes():
            if operation in {"delete", "replace"}:
                removed.extend(current_words[left_start:left_end])
            if operation in {"insert", "replace"}:
                added.extend(legacy_words[right_start:right_end])
        if removed:
            detail += (
                '<div class=diff-remove><strong>− Current:</strong> '
                + html.escape(" ".join(removed)[:240])
                + "</div>"
            )
        if added:
            detail += (
                '<div class=diff-add><strong>+ Legacy:</strong> '
                + html.escape(" ".join(added)[:240])
                + "</div>"
            )
    elif legacy:
        detail = "<div class=diff-add><strong>+ Missing from current catalogue</strong></div>"
    elif current:
        detail = "<div class=diff-remove><strong>− Not present in List View</strong></div>"
    else:
        detail = "<div class=muted>No comparable value; record linkage needs review.</div>"
    return (
        f"<strong>{html.escape(labels.get(str(row.get('kind')), 'Metadata discrepancy'))}</strong>"
        f"<div>Field: {html.escape(str(row.get('field_name', '')).replace('_', ' '))}</div>"
        + detail
    )


def initial_description_enrichment(current: str) -> str:
    """Begin with saved prose; raw legacy evidence must never be appended blindly."""
    return normalize_metadata_text(current)


def write_metadata_discrepancy_decisions_csv(
    connection: sqlite3.Connection, target: Path
) -> None:
    rows = connection.execute(
        """SELECT decision_id, discrepancy_id, record_key, inventory,
                  md_object_id, kind, field_name, legacy_filename,
                  legacy_row_number, legacy_source_path, disposition,
                  chosen_value, current_value, legacy_value, note, actor,
                  decided_at
           FROM metadata_discrepancy_decisions ORDER BY decision_id"""
    )
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "decision_id",
                "discrepancy_id",
                "record_key",
                "inventory",
                "md_object_id",
                "discrepancy_kind",
                "field_name",
                "legacy_filename",
                "legacy_row_number",
                "legacy_source_path",
                "disposition",
                "chosen_value",
                "current_value_at_decision",
                "legacy_value_at_decision",
                "note",
                "actor",
                "decided_at",
                "catalog_modified",
                "deletion_authorized",
            ]
        )
        for row in rows:
            writer.writerow([*row, "false", "false"])
    temporary.replace(target)


def save_metadata_discrepancy_decision(
    connection: sqlite3.Connection,
    work: Path,
    discrepancy_id: str,
    disposition: str,
    note: str,
    actor: str,
    proposed_value: str = "",
) -> None:
    if disposition not in METADATA_DISPOSITIONS:
        raise WorkbenchError("invalid metadata discrepancy decision")
    row = connection.execute(
        "SELECT * FROM metadata_discrepancies WHERE discrepancy_id=?",
        (discrepancy_id,),
    ).fetchone()
    if row is None:
        raise WorkbenchError("metadata discrepancy no longer exists")
    chosen_value = ""
    if disposition == "keep_current":
        chosen_value = row["current_value"]
    elif disposition == "propose_legacy":
        if not row["legacy_value"]:
            raise WorkbenchError("List View has no value to propose")
        if row["kind"] == "legacy_source_note":
            chosen_value = normalize_metadata_text(proposed_value)
            if not chosen_value:
                raise WorkbenchError("proposed enriched description is empty")
            if chosen_value == normalize_metadata_text(row["current_value"]):
                raise WorkbenchError(
                    "description was not enriched; retain current instead"
                )
        else:
            chosen_value = row["legacy_value"]
    with connection:
        connection.execute(
            """INSERT INTO metadata_discrepancy_decisions(
                 discrepancy_id, record_key, inventory, md_object_id, kind,
                 field_name, legacy_filename, legacy_row_number,
                 legacy_source_path, disposition, chosen_value, note, actor,
                 decided_at, current_value, legacy_value
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                discrepancy_id,
                row["record_key"],
                row["inventory"],
                row["md_object_id"],
                row["kind"],
                row["field_name"],
                row["legacy_filename"],
                row["legacy_row_number"],
                row["legacy_source_path"],
                disposition,
                chosen_value,
                note.strip()[:1000],
                actor,
                utc_now(),
                row["current_value"],
                row["legacy_value"],
            ),
        )
    write_metadata_discrepancy_decisions_csv(
        connection, work / "metadata-discrepancy-decisions.csv"
    )


def indexed_metadata_values(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    selected = data.get("selected", data)
    if not selected and data.get("evidence"):
        selected = data["evidence"][0]
    values: dict[str, str] = {}
    for field_name, (_, aliases) in EDITABLE_METADATA_FIELDS.items():
        value = next((selected.get(alias) for alias in aliases if selected.get(alias)), "")
        if value:
            values[field_name] = str(value)
    return values


def browse_rows(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """WITH group_context AS (
             SELECT group_id,
                    MAX(CASE WHEN is_master_reference=1 THEN 1 ELSE 0 END)
                      AS has_master,
                    MAX(CASE WHEN is_master_reference=2 THEN 1 ELSE 0 END)
                      AS has_suggestion,
                    SUM(CASE WHEN is_master_reference=0 THEN 1 ELSE 0 END)
                      AS candidate_count
             FROM groups GROUP BY group_id
           )
           SELECT files.*, groups.group_id, groups.difference,
                  group_context.has_master, group_context.has_suggestion,
                  group_context.candidate_count
           FROM groups
           JOIN files ON files.path=groups.member_path
           JOIN group_context USING(group_id)
           LEFT JOIN decisions ON decisions.path=files.path
           WHERE groups.is_master_reference=0 AND decisions.path IS NULL
             AND group_context.has_master=1
           ORDER BY files.relative_path, files.path"""
    )
    return [dict(row) for row in rows]


def deficiency_keys(row: dict) -> list[str]:
    result: list[str] = []
    width = int(row.get("width_px") or 0)
    height = int(row.get("height_px") or 0)
    pixels = width * height
    extension = str(row.get("extension") or "").casefold()
    high_resolution = max(width, height) >= 2000 and pixels >= 4_000_000
    high_resolution_jpeg = (
        extension in {".jpg", ".jpeg"}
        and high_resolution
    )
    if extension in {".tif", ".tiff"} or high_resolution_jpeg:
        result.append("high_resolution_residual")
    return result


def browse_filter(
    rows: list[dict], deficiency: str, search: str
) -> tuple[list[dict], dict[str, int]]:
    counts = {key: 0 for key in DEFICIENCY_LABELS}
    prepared: list[dict] = []
    search_text = search.casefold().strip()
    for row in rows:
        item = {**row, "deficiencies": deficiency_keys(row)}
        for key in item["deficiencies"]:
            counts[key] += 1
        if deficiency and deficiency not in item["deficiencies"]:
            continue
        if search_text and search_text not in (
            str(item["relative_path"]) + " " + str(item["path"])
        ).casefold():
            continue
        prepared.append(item)
    return prepared, counts


def save_decision(
    connection: sqlite3.Connection,
    work: Path,
    group_id: str,
    master: str | None,
    noise: set[str],
    note: str,
    actor: str,
    metadata_edits: dict[str, str] | None = None,
    cross_unit: set[str] | None = None,
) -> None:
    cross_unit = cross_unit or set()
    if cross_unit:
        raise WorkbenchError("cross-unit decisions are paused for this MD closeout")
    rows = group_rows(connection, group_id)
    members = {
        row["path"]
        for row in rows
        if not row["is_master_reference"] and row["disposition"] is None
    }
    references = {
        row["path"] for row in rows if row["is_master_reference"] == 1
    }
    suggestions = {
        row["path"] for row in rows if row["is_master_reference"] == 2
    }
    if len(members) < 1:
        raise WorkbenchError("group has no undecided members")
    if len(references) > 1:
        raise WorkbenchError("group has conflicting existing Master references")
    if references:
        reference = next(iter(references))
        if master and master != reference:
            raise WorkbenchError("existing selected Master cannot be replaced here")
        master = reference
    elif master and master not in suggestions:
        raise WorkbenchError("only a frozen existing Master may be confirmed")
    if (
        not noise.issubset(members)
        or (master and master in noise)
    ):
        raise WorkbenchError("invalid Noise selection")
    if not master and not noise:
        raise WorkbenchError("confirm a frozen Master relationship or select Noise")
    now = utc_now()
    metadata_edits = {
        key: value.strip()[:4000]
        for key, value in (metadata_edits or {}).items()
        if value.strip()
    }
    if metadata_edits:
        raise WorkbenchError("metadata is frozen for this MD closeout")
    master_row = next((row for row in rows if row["path"] == master), None)
    if metadata_edits and master_row is None:
        raise WorkbenchError("metadata target is not in this group")
    if master_row:
        populated = indexed_metadata_values(master_row["metadata_json"])
        if set(metadata_edits) & set(populated):
            raise WorkbenchError("indexed metadata cannot be overwritten")
    decisions: list[tuple] = []
    if master:
        for path in members:
            disposition = "redundant"
            if path in noise:
                disposition = "noise"
            decisions.append(
                (path, group_id, disposition, master, note, actor, now)
            )
    else:
        decisions.extend(
            (path, group_id, "noise", None, note, actor, now) for path in noise
        )
    with connection:
        connection.executemany(
            """INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 group_id=excluded.group_id,
                 disposition=excluded.disposition,
                 master_path=excluded.master_path,
                 note=excluded.note,
                 actor=excluded.actor,
                 decided_at=excluded.decided_at""",
            decisions,
        )
        connection.executemany(
            """INSERT INTO metadata_edits VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(master_path, field_name) DO UPDATE SET
                 value=excluded.value,
                 group_id=excluded.group_id,
                 actor=excluded.actor,
                 decided_at=excluded.decided_at""",
            [
                (master, field_name, value, group_id, actor, now)
                for field_name, value in metadata_edits.items()
            ],
        )
    write_decisions_csv(connection, work / "decisions.csv")
    write_metadata_edits_csv(connection, work / "metadata-edits.csv")


def save_single_classification(
    connection: sqlite3.Connection,
    work: Path,
    group_id: str,
    path: str,
    disposition: str,
    actor: str,
) -> None:
    if disposition != "noise":
        raise WorkbenchError("invalid browse decision")
    row = connection.execute(
        """SELECT groups.is_master_reference, decisions.disposition
           FROM groups
           LEFT JOIN decisions ON decisions.path=groups.member_path
           WHERE groups.group_id=? AND groups.member_path=?""",
        (group_id, path),
    ).fetchone()
    if row is None or row["is_master_reference"] != 0:
        raise WorkbenchError("image is not an unresolved group member")
    if row["disposition"] is not None:
        raise WorkbenchError("image already has a decision")
    label = "Noise"
    now = utc_now()
    with connection:
        connection.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (
                path,
                group_id,
                disposition,
                f"{label} selected in unreconciled-image browse view.",
                actor,
                now,
            ),
        )
    write_decisions_csv(connection, work / "decisions.csv")
    write_metadata_edits_csv(connection, work / "metadata-edits.csv")


def format_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


def metadata_html(raw: str) -> str:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return "<p>Indexed metadata could not be read.</p>"
    if not data:
        return "<p>No indexed metadata is linked to this image.</p>"
    selected = data.get("selected", data)
    if not selected and data.get("evidence"):
        selected = data["evidence"][0]
    labels = {
        "current_inventory": "Inventory",
        "inventory": "Inventory",
        "current_title": "Title",
        "title": "Title",
        "current_description": "Description",
        "description": "Description",
        "current_date_text": "Date",
        "date_text": "Date",
        "current_material": "Material",
        "material": "Material",
        "current_dimensions": "Dimensions",
        "dimensions": "Dimensions",
        "current_places": "Places",
        "places": "Places",
        "current_tags": "Tags",
        "tags": "Tags",
        "md_object_id": "museum-digital",
    }
    items = []
    for key, label in labels.items():
        if selected.get(key):
            items.append(
                f"<dt>{html.escape(label)}</dt><dd>{html.escape(str(selected[key]))}</dd>"
            )
    summary = data.get("summary")
    provenance = ""
    if data.get("match_count"):
        provenance = (
            f"<p class=muted>{int(data['match_count'])} indexed metadata matches. "
            "Source conflicts remain evidence.</p>"
        )
    return ("<dl>" + "".join(items) + "</dl>" if items else "") + (
        f"<p>{html.escape(str(summary))}</p>" if summary else ""
    ) + provenance


def metadata_editor_html(raw: str, proposed_raw: str, enabled: bool) -> str:
    indexed = indexed_metadata_values(raw)
    try:
        proposed = json.loads(proposed_raw or "{}")
    except json.JSONDecodeError:
        proposed = {}
    controls = []
    disabled = "" if enabled else " disabled"
    for field_name, (label, _) in EDITABLE_METADATA_FIELDS.items():
        if field_name in indexed:
            continue
        value = html.escape(str(proposed.get(field_name, "")), quote=True)
        if field_name == "description":
            control = (
                f'<textarea name="metadata_{field_name}" data-metadata-input'
                f'{disabled}>{value}</textarea>'
            )
        else:
            control = (
                f'<input name="metadata_{field_name}" value="{value}" '
                f'data-metadata-input{disabled}>'
            )
        controls.append(f"<label>{html.escape(label)}{control}</label>")
    if not controls:
        return "<p class=muted>Indexed metadata is complete.</p>"
    return (
        '<div class="metadata-editor"><p><strong>Fill empty metadata only</strong></p>'
        + "".join(controls)
        + "</div>"
    )


CSS = """
:root{font-family:system-ui,sans-serif;color:#171717;background:#f4f1eb}
body{margin:0}main{max-width:1500px;margin:auto;padding:24px}header{display:flex;
justify-content:space-between;gap:20px;align-items:baseline}.cards{display:grid;
grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px}.card{background:white;
border:1px solid #ccc;border-radius:8px;padding:14px;min-width:0}.card img{width:100%;
height:50vh;object-fit:contain;background:#222}.path{overflow-wrap:anywhere;font-family:monospace;
font-size:.82rem}.choice{display:flex;gap:18px;margin:12px 0;padding:10px;background:#eee}
dt{font-weight:700;margin-top:8px}dd{margin:2px 0;white-space:pre-wrap}.muted{color:#666}
textarea{width:100%;min-height:70px}button,.button{display:inline-block;background:#191919;color:white;
padding:11px 16px;border:0;border-radius:5px;text-decoration:none;cursor:pointer}.noise{color:#8b2500}
footer{margin-top:20px;display:flex;gap:12px;align-items:center}.master-reference{border:3px solid #27632a}
.badge{display:inline-block;padding:5px 8px;border-radius:4px;background:#27632a;color:white;font-weight:700}
.badge.suggested{background:#875900}.suggested-reference{border:3px solid #875900}
.metadata-editor label{display:block;font-weight:700;margin:10px 0}.metadata-editor input{display:block;width:100%;box-sizing:border-box;padding:8px}
.browse-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.browse-card{background:white;border:1px solid #ccc;border-radius:8px;padding:10px;min-width:0}
.browse-card img{width:100%;height:260px;object-fit:contain;background:#222}
.browse-card h2{font-size:1rem}.browse-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}
.browse-actions button,.browse-actions .button{padding:8px 10px}.cross-unit{background:#315b78}
.noise-button{background:#8b2500}.deficiency{background:#76521b;margin:2px;font-size:.78rem}
.filters,.pagination{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:14px 0}
.filters a{padding:7px 10px;background:white;border:1px solid #aaa;border-radius:4px;text-decoration:none;color:#171717}
.filters a.active{background:#171717;color:white}.browse-search input{padding:9px;min-width:280px}
.metadata-table{width:100%;border-collapse:collapse;background:white}.metadata-table th,
.metadata-table td{padding:10px;border:1px solid #ccc;vertical-align:top;text-align:left;
max-width:none}.metadata-value{white-space:pre-wrap;overflow-wrap:anywhere;min-width:220px}
.metadata-actions{min-width:190px}.metadata-actions label{display:block;margin:5px 0}
.metadata-actions textarea{min-height:48px}.stale{background:#fff1c7}.kind{white-space:nowrap}
.metadata-object{min-width:230px}.metadata-thumb{display:block;width:220px;height:165px;
object-fit:contain;background:#222;margin-bottom:9px}.diff-remove,.diff-add{margin-top:8px;
padding:6px;white-space:normal;overflow-wrap:anywhere}.diff-remove{background:#f8dddd}
.diff-add{background:#dff2df}
"""


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: "WorkbenchServer"

    def send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        request = urlparse(self.path)
        if request.path == "/image":
            self.serve_image(parse_qs(request.query).get("path", [""])[0])
            return
        if request.path == "/metadata-thumb":
            self.serve_metadata_thumb(
                parse_qs(request.query).get("record_key", [""])[0]
            )
            return
        if request.path not in {"/", "/browse", "/metadata-discrepancies"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(request.query)
        if request.path == "/metadata-discrepancies":
            status = query.get("status", ["pending"])[0]
            if status not in {"pending", "reviewed", "all"}:
                status = "pending"
            kind = query.get("kind", [""])[0]
            search = query.get("q", [""])[0].strip()[:200]
            try:
                page_number = max(1, int(query.get("page", ["1"])[0]))
            except ValueError:
                page_number = 1
            with connect_database(self.server.database) as connection:
                rows = metadata_discrepancy_rows(connection)
            kinds = Counter(row["kind"] for row in rows)
            search_text = search.casefold()
            filtered = [
                row
                for row in rows
                if (status == "all" or row["status"] == status)
                and (not kind or row["kind"] == kind)
                and (
                    not search_text
                    or search_text
                    in " ".join(
                        str(row[key])
                        for key in (
                            "inventory",
                            "md_object_id",
                            "field_name",
                            "current_value",
                            "legacy_value",
                            "legacy_filename",
                        )
                    ).casefold()
                )
            ]
            self.send_html(
                self.metadata_discrepancies_page(
                    filtered, kinds, status, kind, search, page_number
                )
            )
            return
        if request.path == "/browse":
            deficiency = query.get("deficiency", [""])[0]
            if deficiency not in {"", *DEFICIENCY_LABELS}:
                deficiency = ""
            search = query.get("q", [""])[0].strip()[:200]
            try:
                page_number = max(1, int(query.get("page", ["1"])[0]))
            except ValueError:
                page_number = 1
            with connect_database(self.server.database) as connection:
                rows, counts = browse_filter(
                    browse_rows(connection), deficiency, search
                )
            self.send_html(
                self.browse_page(rows, counts, deficiency, search, page_number)
            )
            return
        with connect_database(self.server.database) as connection:
            pending = pending_group_ids(connection)
            group_id = query.get("group", [pending[0] if pending else ""])[0]
            rows = group_rows(connection, group_id) if group_id else []
        selected = query.get("master", [""])[0]
        self.send_html(self.page(group_id, rows, len(pending), selected))

    def do_POST(self) -> None:  # noqa: N802
        request_path = urlparse(self.path).path
        if request_path not in {
            "/decide",
            "/browse-decide",
            "/metadata-discrepancy-decide",
        }:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        if request_path == "/metadata-discrepancy-decide":
            discrepancy_id = fields.get("discrepancy_id", [""])[0]
            disposition = fields.get("disposition", [""])[0]
            note = fields.get("note", [""])[0]
            proposed_value = fields.get("proposed_value", [""])[0]
            return_to = fields.get(
                "return_to", ["/metadata-discrepancies"]
            )[0]
            if not return_to.startswith("/metadata-discrepancies") or "\n" in return_to:
                return_to = "/metadata-discrepancies"
            try:
                with connect_database(self.server.database) as connection:
                    save_metadata_discrepancy_decision(
                        connection,
                        self.server.work,
                        discrepancy_id,
                        disposition,
                        note,
                        self.server.actor,
                        proposed_value,
                    )
            except (sqlite3.Error, WorkbenchError) as error:
                self.send_html(
                    f"<h1>Decision not saved</h1><p>{html.escape(str(error))}</p>",
                    400,
                )
                return
            self.redirect(return_to)
            return
        if request_path == "/browse-decide":
            group_id = fields.get("group_id", [""])[0]
            member_path = fields.get("path", [""])[0]
            action = fields.get("action", [""])[0]
            return_to = fields.get("return_to", ["/browse"])[0]
            if not return_to.startswith("/browse") or "\n" in return_to:
                return_to = "/browse"
            try:
                with connect_database(self.server.database) as connection:
                    save_single_classification(
                        connection,
                        self.server.work,
                        group_id,
                        member_path,
                        action,
                        self.server.actor,
                    )
            except (sqlite3.Error, WorkbenchError) as error:
                self.send_html(
                    f"<h1>Decision not saved</h1><p>{html.escape(str(error))}</p>",
                    400,
                )
                return
            self.redirect(return_to)
            return
        group_id = fields.get("group_id", [""])[0]
        master = fields.get("master", [""])[0] or None
        noise = set(fields.get("noise", []))
        cross_unit = set(fields.get("cross_unit", []))
        note = fields.get("note", [""])[0].strip()[:1000]
        metadata_edits: dict[str, str] = {}
        try:
            with connect_database(self.server.database) as connection:
                save_decision(
                    connection,
                    self.server.work,
                    group_id,
                    master,
                    noise,
                    note,
                    self.server.actor,
                    metadata_edits,
                    cross_unit,
                )
        except (sqlite3.Error, WorkbenchError) as error:
            self.send_html(
                f"<h1>Decision not saved</h1><p>{html.escape(str(error))}</p>", 400
            )
            return
        self.redirect("/")

    def serve_image(self, source_text: str) -> None:
        with connect_database(self.server.database) as connection:
            row = connection.execute(
                "SELECT sha256 FROM files WHERE path=?", (source_text,)
            ).fetchone()
        if row is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            source = ensure_inside(Path(source_text), self.server.source)
        except (FileNotFoundError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        previews = self.server.work / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(source_text.encode()).hexdigest() + ".jpg"
        target = previews / name
        if not target.exists():
            temporary = previews / f".{name}.tmp-{os.getpid()}"
            try:
                subprocess.run(
                    [
                        "magick",
                        f"{source}[0]",
                        "-auto-orient",
                        "-thumbnail",
                        "1400x1400>",
                        "-strip",
                        f"jpg:{temporary}",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                temporary.replace(target)
            except (OSError, subprocess.SubprocessError):
                temporary.unlink(missing_ok=True)
                self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
        payload = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(payload)

    def serve_metadata_thumb(self, record_key: str) -> None:
        source = self.server.access_derivatives.get(record_key)
        if source is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            source = ensure_inside(source, self.server.published_unit)
        except (FileNotFoundError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        previews = self.server.work / "metadata-thumbs"
        previews.mkdir(parents=True, exist_ok=True)
        fingerprint = f"{source}:{source.stat().st_size}:{source.stat().st_mtime_ns}"
        name = hashlib.sha256(fingerprint.encode()).hexdigest() + ".jpg"
        target = previews / name
        if not target.exists():
            temporary = previews / (
                f".{name}.tmp-{os.getpid()}-{threading.get_ident()}"
            )
            try:
                subprocess.run(
                    [
                        "magick",
                        str(source),
                        "-auto-orient",
                        "-thumbnail",
                        "440x440>",
                        "-strip",
                        f"jpg:{temporary}",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                temporary.replace(target)
            except (OSError, subprocess.SubprocessError):
                temporary.unlink(missing_ok=True)
                self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
        payload = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(payload)

    def page(
        self,
        group_id: str,
        rows: list[sqlite3.Row],
        pending: int,
        selected_path: str = "",
    ) -> str:
        cards = []
        references = [
            row for row in rows if row["is_master_reference"] == 1
        ]
        suggestions = [
            row for row in rows if row["is_master_reference"] == 2
        ]
        visible = [
            row
            for row in rows
            if row["is_master_reference"] or row["disposition"] is None
        ]
        for row in visible:
            path = row["path"]
            escaped = html.escape(path)
            image_url = "/image?path=" + quote(path)
            is_reference = row["is_master_reference"] == 1
            is_suggestion = row["is_master_reference"] == 2
            if is_reference:
                choice = '<p><span class=badge>Frozen accepted Master</span></p>'
            elif is_suggestion:
                choice = f"""<div class=choice>
                  <label><input type=radio name=master value="{escaped}"
                    onchange="chooseMaster(this)"{' checked' if selected_path == path else ''}>
                    Confirm Redundant to this frozen Master</label>
                  <span class="badge suggested">Relationship suggestion</span>
                </div>"""
            elif not references:
                choice = f"""<div class=choice>
                  <label class=noise><input type=checkbox name=noise value="{escaped}"
                    onchange="chooseNoise(this)"> Noise</label>
                  <span class=muted>Otherwise leave unresolved for a later local/cross-unit pass.</span>
                </div>"""
            else:
                choice = f"""<div class=choice>
                  <span>Confirm candidate as Redundant to the frozen Master</span>
                  <label class=noise><input type=checkbox name=noise value="{escaped}"
                    onchange="chooseNoise(this)">
                    Noise instead</label>
                </div>"""
            evidence = ""
            if is_suggestion:
                evidence = (
                    f'<p class=muted>{html.escape(row["evidence"])}</p>'
                )
            cards.append(
                f"""<article class="card{' master-reference' if is_reference else ' suggested-reference' if is_suggestion else ''}">
                <img src="{image_url}" alt="{html.escape(row['relative_path'])}">
                <h2>{html.escape(Path(path).name)}</h2>
                <p class=path>{escaped}</p>
                <p>{html.escape(row['extension'])} · {row['width_px'] or '?'}×{row['height_px'] or '?'} px
                · {format_size(row['size_bytes'])}{'' if is_reference or is_suggestion else f" · Czkawka difference {row['difference']}"}</p>
                <p class=path>{html.escape(row['sha256'])}</p>
                {choice}{evidence}
                </article>"""
            )
        if not rows:
            content = "<p>No unresolved similar-image groups remain.</p>"
        else:
            existing_master = ""
            guidance = "Without a proven frozen-Master link, only explicit Noise is saved."
            button = "Save review"
            if len(references) == 1:
                existing_master = (
                    f'<input type=hidden name=master value="'
                    f'{html.escape(references[0]["path"])}">'
                )
                guidance = "The accepted Master stays frozen; confirm residual candidates as Redundant or explicit Noise."
                button = "Save closeout decisions"
            elif len(references) > 1:
                guidance = "Conflicting existing Masters: leave this group unresolved."
            content = f"""<form method=post action=/decide>
              <input type=hidden name=group_id value="{html.escape(group_id)}">
              {existing_master}
              <div class=cards>{''.join(cards)}</div>
              <footer><label>Note <textarea name=note></textarea></label>
              <button type=submit>{button}</button>
              <span class=muted>{guidance}</span></footer>
              </form>
              <script>
              function chooseMaster(input){{
                const card=input.closest('.card');
                const noise=card.querySelector('[name=noise]');
                if(noise) noise.checked=false;
              }}
              function chooseNoise(input){{
                if(!input.checked) return;
                const card=input.closest('.card');
                const master=card.querySelector('[name=master]');
                if(master) master.checked=false;
              }}
              </script>"""
        return f"""<!doctype html><html lang=en><meta charset=utf-8>
          <meta name=viewport content="width=device-width,initial-scale=1">
          <title>Kurt Schwarz MD-unit closeout</title><style>{CSS}</style>
          <main><header><h1>Kurt Schwarz MD-unit closeout</h1>
          <p><a href=/browse>Browse MD-linked exceptions</a> ·
          <a href=/metadata-discrepancies>Metadata discrepancies</a> ·
          {pending} MD-linked unresolved groups · source read-only</p></header>
          <p class=muted>The 1,668 accepted Masters and MD metadata are frozen.
          Only residual files already linked to a frozen Master are shown here.
          Confirm them as Redundant or Noise; leave every uncertain or cross-unit
          candidate unresolved. No decision authorizes deletion.</p>
          {content}</main></html>"""

    def browse_page(
        self,
        rows: list[dict],
        counts: dict[str, int],
        deficiency: str,
        search: str,
        page_number: int,
    ) -> str:
        total = len(rows)
        page_count = max(1, (total + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
        page_number = min(page_number, page_count)
        start = (page_number - 1) * BROWSE_PAGE_SIZE
        visible = rows[start : start + BROWSE_PAGE_SIZE]

        def browse_url(**changes: str | int) -> str:
            values: dict[str, str | int] = {}
            if deficiency:
                values["deficiency"] = deficiency
            if search:
                values["q"] = search
            values.update(changes)
            values = {key: value for key, value in values.items() if value != ""}
            return "/browse" + ("?" + urlencode(values) if values else "")

        filters = [
            f'<a class="{"active" if not deficiency else ""}" href="{browse_url(deficiency="", page=1)}">All</a>'
        ]
        for key, label in DEFICIENCY_LABELS.items():
            filters.append(
                f'<a class="{"active" if deficiency == key else ""}" '
                f'href="{browse_url(deficiency=key, page=1)}">'
                f"{html.escape(label)} ({counts[key]})</a>"
            )

        cards = []
        return_to = browse_url(page=page_number)
        for row in visible:
            path = str(row["path"])
            escaped_path = html.escape(path, quote=True)
            group_id = str(row["group_id"])
            group_query = urlencode({"group": group_id, "master": path})
            labels = "".join(
                f'<span class="badge deficiency">{html.escape(DEFICIENCY_LABELS[key])}</span>'
                for key in row["deficiencies"]
            )
            if row["has_master"]:
                label = "Review frozen Master"
            elif row["has_suggestion"]:
                label = "Review suggested relationship"
            else:
                label = "Review unresolved group"
            master_control = (
                f'<a class=button href="/?{urlencode({"group": group_id})}">{label}</a>'
            )
            context = []
            if int(row["candidate_count"]) > 1:
                context.append(f'{int(row["candidate_count"])} candidates in group')
            if row["has_suggestion"]:
                context.append("existing Master suggestion available")
            cards.append(
                f"""<article class=browse-card>
                <img loading=lazy src="/image?path={quote(path)}"
                  alt="{html.escape(str(row['relative_path']), quote=True)}">
                <h2>{html.escape(Path(path).name)}</h2>
                <p class=path>{escaped_path}</p>
                <p>{html.escape(str(row['extension']))} · {row['width_px'] or '?'}×{row['height_px'] or '?'} px
                  · {format_size(int(row['size_bytes']))}</p>
                <p>{labels}</p>
                <p class=muted>{html.escape(' · '.join(context))}</p>
                <div class=browse-actions>{master_control}
                  <form method=post action=/browse-decide>
                    <input type=hidden name=group_id value="{html.escape(group_id, quote=True)}">
                    <input type=hidden name=path value="{escaped_path}">
                    <input type=hidden name=return_to value="{html.escape(return_to, quote=True)}">
                    <button class=noise-button name=action value=noise>Noise</button>
                  </form>
                </div></article>"""
            )

        pagination = []
        if page_number > 1:
            pagination.append(
                f'<a class=button href="{browse_url(page=page_number - 1)}">Previous</a>'
            )
        pagination.append(f"Page {page_number} of {page_count}")
        if page_number < page_count:
            pagination.append(
                f'<a class=button href="{browse_url(page=page_number + 1)}">Next</a>'
            )
        empty = "<p>No unreconciled images match this filter.</p>" if not cards else ""
        return f"""<!doctype html><html lang=en><meta charset=utf-8>
          <meta name=viewport content="width=device-width,initial-scale=1">
          <title>Kurt Schwarz MD-linked closeout exceptions</title><style>{CSS}</style>
          <main><header><h1>Kurt Schwarz MD-linked closeout exceptions</h1>
          <p><a href=/>Grouped review</a> ·
          <a href=/metadata-discrepancies>Metadata discrepancies</a> · source read-only</p></header>
          <p>The accepted Masters and metadata remain frozen. This view contains only
          residual files already linked to those Masters. Local-only files and suggested
          matches are outside this review and cannot block MD-unit finalization.</p>
          <form class=browse-search method=get action=/browse>
            <input type=search name=q value="{html.escape(search, quote=True)}" placeholder="Filename or path">
            <input type=hidden name=deficiency value="{html.escape(deficiency, quote=True)}">
            <button>Search</button>
          </form>
          <nav class=filters>{''.join(filters)}</nav>
          <p>{total} matching MD-linked images · showing {start + 1 if cards else 0}–{start + len(cards)}</p>
          <nav class=pagination>{''.join(pagination)}</nav>{empty}
          <div class=browse-cards>{''.join(cards)}</div>
          <nav class=pagination>{''.join(pagination)}</nav>
          </main></html>"""

    def metadata_discrepancies_page(
        self,
        rows: list[dict],
        kinds: Counter,
        status: str,
        kind: str,
        search: str,
        page_number: int,
    ) -> str:
        total = len(rows)
        page_count = max(1, (total + METADATA_PAGE_SIZE - 1) // METADATA_PAGE_SIZE)
        page_number = min(page_number, page_count)
        start = (page_number - 1) * METADATA_PAGE_SIZE
        visible = rows[start : start + METADATA_PAGE_SIZE]

        def page_url(**changes: str | int) -> str:
            values: dict[str, str | int] = {"status": status}
            if kind:
                values["kind"] = kind
            if search:
                values["q"] = search
            values.update(changes)
            values = {key: value for key, value in values.items() if value != ""}
            return "/metadata-discrepancies?" + urlencode(values)

        status_filters = [
            f'<a class="{"active" if status == value else ""}" '
            f'href="{page_url(status=value, page=1)}">{label}</a>'
            for value, label in (
                ("pending", "Pending"),
                ("reviewed", "Reviewed"),
                ("all", "All"),
            )
        ]
        kind_filters = [
            f'<a class="{"active" if not kind else ""}" '
            f'href="{page_url(kind="", page=1)}">All kinds</a>'
        ] + [
            f'<a class="{"active" if kind == value else ""}" '
            f'href="{page_url(kind=value, page=1)}">'
            f"{html.escape(value.replace('_', ' ').title())} ({count})</a>"
            for value, count in sorted(kinds.items())
        ]
        return_to = page_url(page=page_number)
        table_rows = []
        for row in visible:
            current_value = row["current_value"] or "—"
            legacy_value = row["legacy_value"] or "—"
            reviewed = row["status"] == "reviewed"
            selected = row["disposition"] if reviewed else ""
            stale = " stale" if row["stale"] else ""
            status_note = ""
            if reviewed:
                status_note = (
                    f'<p class=muted>Latest: {html.escape(row["disposition"].replace("_", " "))} '
                    f'by {html.escape(row["actor"])} at {html.escape(row["decided_at"])}</p>'
                )
            elif row["stale"]:
                status_note = "<p><strong>Values changed since the latest decision; review again.</strong></p>"
            legacy_disabled = " disabled" if not row["legacy_value"] else ""
            proposal_control = ""
            proposal_label = "Propose List View description"
            if row["kind"] == "legacy_source_note":
                proposal_label = "Propose selectively enriched description"
                proposed = initial_description_enrichment(row["current_value"])
                if selected == "propose_legacy" and row["chosen_value"]:
                    proposed = row["chosen_value"]
                proposal_control = (
                    '<p class=muted>Start with the saved description. Add only '
                    "verified facts specific to this photograph; the complete raw "
                    "legacy note remains preserved as evidence.</p>"
                    '<label>Proposed saved description'
                    f'<textarea name=proposed_value>{html.escape(proposed)}</textarea>'
                    "</label>"
                )
            thumbnail = ""
            if row["record_key"] in self.server.access_derivatives:
                thumbnail = (
                    f'<img class=metadata-thumb loading=lazy '
                    f'src="/metadata-thumb?{urlencode({"record_key": row["record_key"]})}" '
                    f'alt="Preview of {html.escape(row["inventory"], quote=True)}">'
                )
            else:
                thumbnail = "<div class=muted>No published access preview</div>"
            table_rows.append(
                f"""<tr class="{stale.strip()}">
                <td class=metadata-object>{thumbnail}<strong>{html.escape(row['inventory'] or 'Unlinked record')}</strong>
                  <div class=path>{html.escape(row['record_key'])}</div>
                  <div>MD {html.escape(row['md_object_id'] or '—')}</div>
                  <div class=muted>List row {html.escape(row['legacy_row_number'] or '—')} · {html.escape(row['legacy_filename'] or '—')}</div></td>
                <td>{metadata_difference_html(row)}<br>
                  <span class=muted>{html.escape(row['explanation'])}</span></td>
                <td class=metadata-value>{html.escape(current_value)}</td>
                <td class=metadata-value>{html.escape(legacy_value)}</td>
                <td class=metadata-actions><form method=post action=/metadata-discrepancy-decide>
                  <input type=hidden name=discrepancy_id value="{html.escape(row['discrepancy_id'], quote=True)}">
                  <input type=hidden name=return_to value="{html.escape(return_to, quote=True)}">
                  <label><input type=radio name=disposition value=keep_current{' checked' if selected == 'keep_current' else ''}> Retain current</label>
                  <label><input type=radio name=disposition value=propose_legacy{legacy_disabled}{' checked' if selected == 'propose_legacy' else ''}> {proposal_label}</label>
                  <label><input type=radio name=disposition value=unresolved{' checked' if selected == 'unresolved' else ''}> Leave unresolved</label>
                  {proposal_control}
                  <textarea name=note placeholder="Short reconciliation note">{html.escape(row['note'] or '')}</textarea>
                  <button>Record decision</button>{status_note}
                </form></td></tr>"""
            )
        pagination = []
        if page_number > 1:
            pagination.append(
                f'<a class=button href="{page_url(page=page_number - 1)}">Previous</a>'
            )
        pagination.append(f"Page {page_number} of {page_count}")
        if page_number < page_count:
            pagination.append(
                f'<a class=button href="{page_url(page=page_number + 1)}">Next</a>'
            )
        body = (
            '<table class=metadata-table><thead><tr><th>Object</th><th>Discrepancy</th>'
            '<th>Current CATALOG.csv</th><th>Legacy List View</th><th>Human decision</th>'
            f"</tr></thead><tbody>{''.join(table_rows)}</tbody></table>"
            if table_rows
            else "<p>No metadata discrepancies match this view.</p>"
        )
        return f"""<!doctype html><html lang=en><meta charset=utf-8>
          <meta name=viewport content="width=device-width,initial-scale=1">
          <title>Kurt Schwarz metadata reconciliation</title><style>{CSS}</style>
          <main><header><h1>Kurt Schwarz metadata reconciliation</h1>
          <p><a href=/>Grouped image review</a> · <a href=/browse>Image wall</a> · source and published catalogue read-only</p></header>
          <p>This pass compares only the saved <code>description</code> with the legacy
          caption and its provenance/source tail. Enrichment starts from the current
          description; reviewers add only verified, object-specific facts. The raw note
          remains evidence and is never appended automatically. “Propose” records the exact
          proposed description but does not alter the catalogue. No decision authorizes deletion.</p>
          <form class=browse-search method=get action=/metadata-discrepancies>
            <input type=search name=q value="{html.escape(search, quote=True)}" placeholder="Inventory, MD ID, filename, or value">
            <input type=hidden name=status value="{html.escape(status, quote=True)}">
            <input type=hidden name=kind value="{html.escape(kind, quote=True)}">
            <button>Search</button></form>
          <nav class=filters>{''.join(status_filters)}</nav>
          <nav class=filters>{''.join(kind_filters)}</nav>
          <p>{total} matching discrepancies · showing {start + 1 if visible else 0}–{start + len(visible)}</p>
          <nav class=pagination>{''.join(pagination)}</nav>{body}
          <nav class=pagination>{''.join(pagination)}</nav></main></html>"""

    def log_message(self, message: str, *args: object) -> None:
        sys.stderr.write(f"{self.address_string()} - {message % args}\n")


class WorkbenchServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        database: Path,
        work: Path,
        source: Path,
        published_unit: Path,
        access_derivatives: dict[str, Path],
        actor: str,
    ):
        super().__init__(address, WorkbenchHandler)
        self.database = database
        self.work = work
        self.source = source
        self.published_unit = published_unit
        self.access_derivatives = access_derivatives
        self.actor = actor


def serve(args: argparse.Namespace) -> None:
    source = args.source.resolve(strict=True)
    mount = require_read_only_source(source)
    list_view = ensure_inside(args.list_view, source)
    catalog = args.catalog.resolve(strict=True)
    access_derivatives = published_access_derivatives(catalog, PUBLISHED_UNIT)
    database = args.work / "workbench.sqlite"
    if not database.is_file():
        raise WorkbenchError(f"replacement database does not exist: {database}")
    with connect_database(database) as connection:
        discrepancy_count = sync_metadata_discrepancies(
            connection, list_view, catalog
        )
        write_metadata_discrepancy_decisions_csv(
            connection, args.work / "metadata-discrepancy-decisions.csv"
        )
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise WorkbenchError("replacement database failed quick_check")
    print(
        f"Serving http://127.0.0.1:{args.port}/ ({mount}; "
        f"{discrepancy_count} metadata discrepancies)"
    )
    WorkbenchServer(
        ("127.0.0.1", args.port),
        database,
        args.work,
        source,
        PUBLISHED_UNIT,
        access_derivatives,
        args.actor,
    ).serve_forever()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build the replacement index")
    build_parser.add_argument("--source", type=Path, default=SOURCE_ROOT)
    build_parser.add_argument("--work", type=Path, default=WORK_ROOT)
    build_parser.add_argument("--hash-inventory", type=Path, default=HASH_INVENTORY)
    build_parser.add_argument("--legacy-db", type=Path, default=LEGACY_DB)
    build_parser.add_argument("--metadata-csv", type=Path, default=METADATA_CSV)
    build_parser.add_argument(
        "--representations-csv", type=Path, default=PUBLISHED_REPRESENTATIONS
    )
    build_parser.add_argument(
        "--expected-frozen-masters", type=int, default=EXPECTED_FROZEN_MASTERS
    )
    build_parser.add_argument("--czkawka", type=Path, default=CZKAWKA)
    build_parser.add_argument("--max-difference", type=int, default=5, choices=range(41))
    build_parser.add_argument(
        "--reuse-czkawka",
        action="store_true",
        help="reuse the completed Czkawka JSON when only rebuilding projections",
    )
    build_parser.set_defaults(function=build)
    serve_parser = subparsers.add_parser("serve", help="serve the replacement workbench")
    serve_parser.add_argument("--source", type=Path, default=SOURCE_ROOT)
    serve_parser.add_argument("--work", type=Path, default=WORK_ROOT)
    serve_parser.add_argument("--list-view", type=Path, default=LEGACY_LIST_VIEW)
    serve_parser.add_argument("--catalog", type=Path, default=PUBLISHED_CATALOG)
    serve_parser.add_argument("--actor", required=True)
    serve_parser.add_argument("--port", type=int, default=8767)
    serve_parser.set_defaults(function=serve)
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        args.function(args)
    except (OSError, sqlite3.Error, subprocess.SubprocessError, WorkbenchError) as error:
        raise SystemExit(f"error: {error}") from error


if __name__ == "__main__":
    main()
