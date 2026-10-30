#!/usr/bin/env python3
"""Prepare identity-first review for the 64 unbound Buchprojekt pictures.

The source archive stays read-only.  The tool groups exact visual copies, marks
TIFFs as possible Master candidates, carries the final book caption as proposed
metadata, and writes only review CSV/JSON files in the existing reconciliation
work area.  It never selects a Master or applies metadata automatically.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.1.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
HELPER = Path(__file__).with_name("tfa_buchprojekt_metadata_prepare.py")
CANDIDATES = REVIEW_ROOT / "buchprojekt-unbound-object-candidates.csv"
DECISIONS = REVIEW_ROOT / "buchprojekt-unbound-object-decisions.csv"
RECEIPT = REVIEW_ROOT / "buchprojekt-unbound-object-preparation.json"

EXPECTED_OBJECTS = 57
EXPECTED_STATES = Counter({"no_inventory": 49, "inventory_not_catalogued": 7, "local_object_unresolved": 1})
MD_IDENTITY_BINDINGS = {
    "012_5511230": ("md:65185", "TFA-5511230-17", "exact selected-Master visual match"),
    "013_551230-13": ("md:65183", "TFA-5511230-13", "book inventory typo plus bounded visual match"),
    "038_538691": ("md:85577", "TFA-538691-2", "bounded selected-Master visual match"),
}
TIFF_EXTENSIONS = {"tif", "tiff"}
IMAGE_EXTENSIONS = TIFF_EXTENSIONS | {"jpg", "jpeg", "png"}

CANDIDATE_FIELDS = (
    "candidate_id", "record_key", "inventory", "scope_state", "visual_group_id",
    "book_image_key", "book_page", "book_caption", "suggested_title",
    "suggested_description", "suggested_date_text", "suggested_object_type",
    "suggested_material", "suggested_dimensions", "suggested_keywords",
    "suggested_classification", "workbook_path",
    "workbook_sha256", "sheet_name", "row_number", "source_path",
    "relative_path", "filename", "extension", "size_bytes", "mtime_ns",
    "width_px", "height_px", "ahash", "dhash", "phash", "candidate_role",
    "selectable_master", "provenance_role", "recommendation_rank",
    "is_book_image", "existing_candidate_bindings_json", "group_candidate_count",
    "group_selectable_master_count", "deletion_authorized",
)

DECISION_FIELDS = (
    "record_key", "decision", "selected_candidate_id", "selected_master_path",
    "redundant_candidate_paths_json", "title", "description", "date_text",
    "object_type", "material", "dimensions", "keywords", "classification",
    "note", "decided_by",
    "decided_at", "book_image_key", "workbook_path", "workbook_sha256",
    "sheet_name", "row_number", "visual_group_id", "deletion_authorized",
)


class PreparationError(RuntimeError):
    pass


def helper() -> Any:
    specification = importlib.util.spec_from_file_location("buchprojekt_metadata_helper", HELPER)
    if specification is None or specification.loader is None:
        raise PreparationError("cannot load Buchprojekt metadata helper")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def visual_group_id(row: dict[str, Any]) -> str:
    signature = f"{row['ahash']}:{row['dhash']}:{row['phash']}"
    return "visual-" + hashlib.sha256(signature.encode()).hexdigest()[:16]


def visual_inventory(row: dict[str, Any]) -> str:
    return "TFA-VISUAL-" + visual_group_id(row).removeprefix("visual-").upper()


def candidate_id(record_key: str, path: str) -> str:
    return "BUC-" + hashlib.sha256(f"{record_key}|{path}".encode()).hexdigest()[:20].upper()


def provenance_role(relative_path: str, extension: str, book_path: str) -> str:
    if str(SOURCE_ROOT / relative_path) == book_path:
        return "canonical_book_tiff"
    if extension in TIFF_EXTENSIONS:
        return "archival_tiff_copy"
    return "existing_derivative_or_copy"


def recommendation_rank(relative_path: str, extension: str) -> int:
    if extension not in TIFF_EXTENSIONS:
        return 90
    priorities = (
        ("restore/archive/SCANS-fertig/", 0),
        ("restore/archive/Alle-Tiff-Positive/", 1),
        ("Originaldateien-für-Sutton-Verlag/Bilddaten/", 2),
        ("restore/archive/WF/Buchprojekt_Industriesalon-Schöneweide/Bilddaten/", 3),
        ("restore/archive/Buchprojekt_Bilddaten_Tif/", 4),
    )
    return next((rank for token, rank in priorities if token in relative_path), 10)


MONTH = r"(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)"
DATE_PATTERNS = (
    rf"\b\d{{1,2}}\.\d{{1,2}}\.\d{{4}}\b",
    rf"\b{MONTH}\s+\d{{4}}\b",
    r"\b(?:um|ca\.|circa)\s+\d{4}\b",
    r"\b(?:Anfang|Mitte|Ende|erste Hälfte|zweite Hälfte)\s+(?:der\s+)?\d{4}er(?:-Jahre|\s+Jahre)?\b",
    r"\b\d{4}er\s+Jahren?\b",
    r"\b\d{4}\s*(?:bis|–|-)\s*\d{4}\b",
    r"\b(?:19|20)\d{2}\b",
)

CLASS_DIVERSE = "Fotostelle WF - Diverses"
CLASS_BUILDINGS = "Fotostelle WF - Gebäude des WF"
CLASS_DEVICES = "Fotostelle WF - Geräte, Einschübe, Bauteile"
CLASS_PRODUCTION = "Fotostelle WF - In der Fertigung - Anlagen, Automaten, Arbeitspätze"
CLASS_PEOPLE = "Fotostelle WF - Menschen im WF"
CLASS_TUBES = "Fotostelle WF - Röhren und Halbleiter"
CLASS_DRAWINGS = "Fotostelle WF - Schaltbilder, Konstruktionzeichnungen, Kurven, Tabellen"
CLASS_TELECOM = "Fotostelle WF - Telekommunikation, Sende- und Fernsehtechnik"

# Classification is deliberately bounded to the existing museum-digital
# Fotostelle taxonomy.  Explicit per-picture assignments make the proposal
# reviewable and prevent a vague text classifier from changing archival scope.
CLASSIFICATION_BY_KEY = {
    "001_Karte-Oberschöneweide": (CLASS_DRAWINGS,),
    "005_549638-2": (CLASS_PRODUCTION,),
    "014_538687": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "016_5510586": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "021_546003": (CLASS_PRODUCTION,),
    "029_5511011-26": (CLASS_PEOPLE,),
    "113_6927557": (CLASS_PEOPLE,),
    "123_IMG_2020012": (CLASS_BUILDINGS,),
    "124_IMG_20200121_0011": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "125_IMG_20200125_0008": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "126_ IMG_20200124_0011": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "127_ IMG_20200125_0009": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "128_ JS52204_Raucherecke": (CLASS_DIVERSE,),
    "129_ IMG_20200204_0027": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "130_IMG_204_0029": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "132_ IMG_20200204_0024": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "133_IMG_20200124_0005": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "134_IMG_20200125_0012": (CLASS_PEOPLE,),
    "138_7329204_2": (CLASS_BUILDINGS,),
    "139_7329344": (CLASS_PEOPLE, CLASS_DEVICES, CLASS_TELECOM),
    "141_IMG_20200204_0026": (CLASS_PEOPLE,),
    "143_Bibliothek_0011": (CLASS_PEOPLE, CLASS_BUILDINGS),
    "145_7329358a": (CLASS_PEOPLE, CLASS_DEVICES),
    "147_IMG206_0013": (CLASS_BUILDINGS,),
    "148_Ausbildung _0017": (CLASS_PEOPLE,),
    "149_0031": (CLASS_PEOPLE,),
    "150_IMG_20200117_0004": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "151_IS50708": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "152_Brigade_0006": (CLASS_PEOPLE,),
    "153_0028": (CLASS_DRAWINGS,),
    "158_0027": (CLASS_PEOPLE,),
    "159_IMG_20200204_0032": (CLASS_PEOPLE,),
    "160_IMG_20200204_0031": (CLASS_PEOPLE,),
    "161_IMG_20200204_0030": (CLASS_PEOPLE,),
    "162_0013": (CLASS_BUILDINGS,),
    "163_IMG_20200124_0012": (CLASS_DIVERSE,),
    "164_IMG_20200125_0027": (CLASS_BUILDINGS,),
    "165_0020": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "166_0025": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "167_IMG_20200204_0010": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "168_ IMG_20200121_0003": (CLASS_DEVICES,),
    "170_IMG_20200204_0036": (CLASS_DIVERSE,),
    "171_ IMG206_0002": (CLASS_DIVERSE, CLASS_TUBES),
    "172_0001": (CLASS_TUBES,),
    "173_IM206_0006": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "174_IMG_20200204_0028": (CLASS_DEVICES, CLASS_TUBES),
    "175_IMG_20200121_0007": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "176_0037": (CLASS_PEOPLE,),
    "177_0034": (CLASS_PEOPLE,),
    "178_0035": (CLASS_PEOPLE,),
    "179_ IMG_20200124_0006": (CLASS_PRODUCTION,),
    "180_ IMG_20200125_0006": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "181_IMG_20200124_0010": (CLASS_PEOPLE, CLASS_DEVICES),
    "182_IMG_20200124_0015": (CLASS_PEOPLE, CLASS_PRODUCTION),
    "185_IS52173": (CLASS_DIVERSE,),
    "186_IS52208": (CLASS_DIVERSE,),
    "187_WF Werk_0001": (CLASS_BUILDINGS,),
}

KEYWORD_RULES = (
    (r"\b(frau|frauen|mitarbeiterin|mitarbeiterinnen|arbeiterin|lehrerin)\b", ("Frau",)),
    (r"\b(mann|männer|mitarbeiter|technologe|staatschef|betriebsdirektor|bildröhrenbearbeiter|auszubildender)\b", ("Mann",)),
    (r"\b(kinder|kindergarten)\b", ("Kind",)),
    (r"\b(produktion|produktionshalle|produktionsstraße|fertigung|wickeln|schneiderei|epitaxie|systemaufbau)\w*\b", ("Produktion", "Arbeitsplatz")),
    (r"\b(arbeit|arbeitsplatz|arbeitskräfte|kontrolle|kontrolliert|überprüfen|bedampfung|messplatz|meßplatz)\w*\b", ("Arbeitsalltag",)),
    (r"\b(produktionshalle|fabrikhalle)\w*\b", ("Fabriksaal", "Innenraumansicht")),
    (r"\b(schutzmaske|schutzkleidung|schutzanzug|clean-room)\w*\b", ("Schutzkleidung", "Arbeitsschutz", "Berufskleidung")),
    (r"\b(labor)\w*\b", ("Labor", "Innenraumansicht")),
    (r"\b(bibliothek|bücherei)\w*\b", ("Bibliothek", "Innenraumansicht")),
    (r"\b(ausbildung|auszubildende|auszubildender|betriebsschule|lehrerin|meister für morgen|mmm)\b", ("Ausbildung",)),
    (r"\b(bildröhre|bildröhren|bildschirmröhre|bildschirmröhren|farbbildröhre|farbbildröhren)\w*\b", ("Bildröhre",)),
    (r"\b(elektronenröhre|elektronenröhren|senderöhre|senderöhren|röhrenproduktion)\w*\b", ("Elektronenröhre",)),
    (r"\b(diode|dioden|halbleiter|chip|silicium|si-scheibe)\w*\b", ("Halbleiter",)),
    (r"\b(automat|automaten|pumpautomat)\w*\b", ("Automat", "Maschine")),
    (r"\b(vakuumpumpe|messgerät|meßgerät|prüfmittel|fernsteuerung)\w*\b", ("Messgerät", "Prüfmittel")),
    (r"\b(fernseher|fernsehgerät)\w*\b", ("Fernsehgerät",)),
    (r"\b(brigade|belegschaft|gruppe|mitarbeiterinnen|zwei mitarbeiter|drei frauen)\b", ("Gruppenfoto", "Arbeitsteam")),
    (r"\b(hauptgebäude|gebäude|einfahrt|gelände)\w*\b", ("Architektur", "Gebäude", "Außenaufnahme")),
    (r"\b(treppenaufgang|bibliothek|ausbildungskabinett|rechenzentrum|büromöbel)\b", ("Innenraumansicht",)),
    (r"\b(werbung)\w*\b", ("Plakat", "Sachaufnahme")),
    (r"\b(tafel|karte)\w*\b", ("Sachaufnahme",)),
    (r"\b(kindergarten)\w*\b", ("Kindergarten",)),
    (r"\b(büro|büromöbel)\w*\b", ("Büro",)),
    (r"\b(detailaufnahme|geräte|befestigungsteile|elektronenröhren bereit)\b", ("Sachaufnahme",)),
)


def caption_date(caption: str) -> str:
    hits: list[tuple[int, int, str]] = []
    for priority, pattern in enumerate(DATE_PATTERNS):
        for match in re.finditer(pattern, caption, re.IGNORECASE):
            hits.append((match.end(), -priority, match.group(0)))
    return max(hits)[2] if hits else ""


def proposed_keywords(
    key: str, caption: str, material: str, allowed_keywords: set[str],
) -> str:
    values = {"Elektrotechnik", "Industrie", "Schwarzweißfotografie"}
    if "Kleinbild" in material:
        values.add("Kleinbildnegativ")
    elif "Negativ" in material:
        values.add("Negativ (Fotografie)")
    else:
        values.add("Fotografie (Bild)")
    for pattern, keywords in KEYWORD_RULES:
        if re.search(pattern, caption, re.IGNORECASE):
            values.update(keywords)
    classifications = CLASSIFICATION_BY_KEY[key]
    if CLASS_PEOPLE in classifications:
        values.add("Momentaufnahme")
    elif CLASS_BUILDINGS not in classifications:
        values.add("Sachaufnahme")
    if key == "113_6927557":
        values.update({"Ausbildung", "Mann", "Szene (Handlung)", "Arbeitsplatz", "Maschine"})
    if key in {"158_0027", "159_IMG_20200204_0032", "160_IMG_20200204_0031", "161_IMG_20200204_0030"}:
        values.add("Kind")
    unknown = values - allowed_keywords
    if unknown:
        raise PreparationError(f"generated keywords left current MD vocabulary for {key}: {sorted(unknown)}")
    return "; ".join(sorted(values, key=str.casefold))


def proposed_metadata(
    module: Any, canonical: dict[str, Any], detail: dict[str, Any] | None,
    allowed_keywords: set[str], allowed_classifications: set[str],
) -> dict[str, str]:
    proposed = module.proposal(canonical, detail, {})
    key = canonical["key"]
    proposed["date_text"] = caption_date(canonical["caption"])
    proposed["object_type"] = "Foto"
    proposed["material"] = ""
    proposed["dimensions"] = ""
    if key == "113_6927557":
        proposed["material"] = "SW-Negativ, Planfilm"
        proposed["dimensions"] = "9x12 cm"
    elif key == "029_5511011-26":
        proposed["material"] = "Kleinbildfilm, SW-Negativ"
        proposed["dimensions"] = "35mm"
    elif int(book_slot_number(key)) >= 123:
        proposed["material"] = "SW-Negativ, Rollfilm"
        proposed["dimensions"] = "6x6 cm"
    elif key in {"005_549638-2", "016_5510586"}:
        proposed["material"] = "Fotoabzug"
    classifications = CLASSIFICATION_BY_KEY.get(key)
    if not classifications:
        raise PreparationError(f"book picture has no bounded MD classification proposal: {key}")
    unknown = set(classifications) - allowed_classifications
    if unknown:
        raise PreparationError(f"classification proposal left current MD taxonomy for {key}: {sorted(unknown)}")
    proposed["classification"] = "; ".join(classifications)
    proposed["keywords"] = proposed_keywords(
        key, canonical["caption"], proposed["material"], allowed_keywords,
    )
    return proposed


def book_slot_number(key: str) -> str:
    match = re.match(r"^(\d{3})", key)
    if match is None:
        raise PreparationError(f"book key has no sequence number: {key}")
    return match.group(1)


def current_objects(connection: sqlite3.Connection) -> dict[str, dict[str, str]]:
    return {
        str(row["inventory"]): dict(row)
        for row in connection.execute("SELECT record_key,inventory,record_state FROM objects")
    }


def prepare_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    module = helper()
    mount = module.source_mount()
    canonical, details = module.canonical_rows()
    book_files = module.book_files(canonical)
    book_assets, _selected = module.visual_rows()
    key_by_filename = {path.name: key for key, path in book_files.items()}
    book_by_key = {
        key_by_filename[Path(row["relative_path"]).name]: row for row in book_assets
    }
    workbook_sha = module.sha256_file(module.CAPTIONS_XLSX)

    with closing(sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        objects = current_objects(connection)
        allowed_classifications = {
            str(row[0]) for row in connection.execute(
                "SELECT label FROM subdivisions WHERE label LIKE 'Fotostelle WF - %'"
            )
        }
        allowed_keywords: set[str] = set()
        for row in connection.execute("SELECT tags FROM objects WHERE tags<>''"):
            allowed_keywords.update(
                value.strip() for value in re.split(r"\s*[;|]\s*", str(row[0])) if value.strip()
            )
        unresolved_local = {
            str(row["record_key"])
            for row in connection.execute(
                """SELECT o.record_key FROM objects o
                   LEFT JOIN local_object_decisions d ON d.record_key=o.record_key
                   WHERE o.record_state='local_tfa_review' AND d.record_key IS NULL"""
            )
        }
        bindings: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute("SELECT candidate_id,record_key,source_path FROM candidates"):
            bindings[str(row["source_path"])].append({
                "candidate_id": int(row["candidate_id"]), "record_key": str(row["record_key"]),
            })

    scope: list[tuple[str, str, str, str]] = []
    excluded_current_md: list[dict[str, str]] = []
    for key, canonical_row in canonical.items():
        inventory = module.book_inventory(key)
        current = objects.get(inventory) if inventory else None
        variant = None
        variant_match = re.match(r"^(TFA-\d+)-(\d+)$", inventory) if inventory else None
        if current is None and variant_match is not None:
            variant_inventory = f"{variant_match.group(1)}_{variant_match.group(2)}"
            variant = objects.get(variant_inventory)
        binding = MD_IDENTITY_BINDINGS.get(key)
        if binding is not None:
            bound = next((row for row in objects.values() if row["record_key"] == binding[0]), None)
            if (
                bound is None or bound["record_state"] != "current_museum_digital"
                or bound["inventory"] != binding[1]
            ):
                raise PreparationError(f"audited MD identity binding drifted: {key}")
            excluded_current_md.append({
                "book_image_key": key, "record_key": binding[0],
                "inventory": binding[1], "basis": binding[2],
            })
            continue
        if variant is not None and variant["record_state"] == "current_museum_digital":
            excluded_current_md.append({
                "book_image_key": key, "record_key": variant["record_key"],
                "inventory": variant["inventory"], "basis": "hyphen underscore inventory normalization",
            })
            continue
        if not inventory:
            book = book_by_key[key]
            inventory = visual_inventory(book)
            state = "no_inventory"
        elif current is None:
            state = "inventory_not_catalogued"
        elif current["record_key"] in unresolved_local:
            state = "local_object_unresolved"
        else:
            continue
        record_key = "local:" + inventory
        if current is not None and current["record_key"] != record_key:
            raise PreparationError(f"existing unresolved object key drifted: {key}")
        scope.append((key, inventory, record_key, state))
    if (
        len(scope) != EXPECTED_OBJECTS
        or Counter(item[3] for item in scope) != EXPECTED_STATES
        or len(excluded_current_md) != 7
    ):
        raise PreparationError(f"unbound Buchprojekt scope drifted: {Counter(item[3] for item in scope)}")

    signatures = {
        (book_by_key[key]["ahash"], book_by_key[key]["dhash"], book_by_key[key]["phash"])
        for key, _inventory, _record_key, _state in scope
    }
    exact_groups: defaultdict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    with closing(sqlite3.connect(f"file:{VISUAL_INDEX}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        root = connection.execute(
            "SELECT root_id FROM visual_roots WHERE root_path=?", (str(SOURCE_ROOT),)
        ).fetchone()
        if root is None:
            raise PreparationError("visual index is not bound to Elements")
        for row in connection.execute(
            """SELECT a.asset_id,a.relative_path,a.original_filename,a.extension,
                      a.size_bytes,a.mtime_ns,a.width_px,a.height_px,
                 MAX(CASE WHEN f.extractor_id='ahash-64-v2' THEN f.feature_hex ELSE '' END) ahash,
                 MAX(CASE WHEN f.extractor_id='dhash-64-v2' THEN f.feature_hex ELSE '' END) dhash,
                 MAX(CASE WHEN f.extractor_id='phash-dct-64-v2' THEN f.feature_hex ELSE '' END) phash
               FROM visual_assets a JOIN visual_features f ON f.asset_id=a.asset_id
               WHERE a.root_id=? AND a.status='indexed'
                 AND a.relative_path LIKE 'restore/archive/%'
               GROUP BY a.asset_id""",
            (root[0],),
        ):
            item = dict(row)
            signature = (item["ahash"], item["dhash"], item["phash"])
            extension = str(item["extension"]).casefold().lstrip(".")
            if signature in signatures and extension in IMAGE_EXTENSIONS:
                item["extension"] = extension
                exact_groups[signature].append(item)

    rows: list[dict[str, Any]] = []
    group_counts: Counter[str] = Counter()
    selectable_counts: Counter[str] = Counter()
    state_counts: Counter[str] = Counter()
    for key, inventory, record_key, state in sorted(scope):
        canonical_row = canonical[key]
        book = book_by_key[key]
        signature = (book["ahash"], book["dhash"], book["phash"])
        group = exact_groups.get(signature, [])
        book_path = str(book_files[key])
        if not group or book_path not in {str(SOURCE_ROOT / row["relative_path"]) for row in group}:
            raise PreparationError(f"canonical book TIFF left its exact visual group: {key}")
        proposed = proposed_metadata(
            module, canonical_row, details.get(key), allowed_keywords, allowed_classifications,
        )
        group_id = visual_group_id(book)
        state_counts[state] += 1
        group_counts[group_id] = len(group)
        selectable_counts[group_id] = sum(
            str(item["extension"]).casefold().lstrip(".") in TIFF_EXTENSIONS for item in group
        )
        if selectable_counts[group_id] < 1:
            raise PreparationError(f"unbound picture has no selectable TIFF: {key}")
        for item in sorted(group, key=lambda row: (
            recommendation_rank(str(row["relative_path"]), str(row["extension"])),
            -int(row["width_px"] or 0) * int(row["height_px"] or 0),
            -int(row["size_bytes"]), str(row["relative_path"]),
        )):
            source_path = str(SOURCE_ROOT / str(item["relative_path"]))
            extension = str(item["extension"])
            selectable = extension in TIFF_EXTENSIONS
            role = provenance_role(str(item["relative_path"]), extension, book_path)
            rows.append({
                "candidate_id": candidate_id(record_key, source_path),
                "record_key": record_key, "inventory": inventory, "scope_state": state,
                "visual_group_id": group_id, "book_image_key": key,
                "book_page": canonical_row["page"], "book_caption": canonical_row["caption"],
                "suggested_title": proposed["title"],
                "suggested_description": proposed["description"],
                "suggested_date_text": proposed["date_text"],
                "suggested_object_type": proposed["object_type"],
                "suggested_material": proposed["material"],
                "suggested_dimensions": proposed["dimensions"],
                "suggested_keywords": proposed["keywords"],
                "suggested_classification": proposed["classification"],
                "workbook_path": str(module.CAPTIONS_XLSX), "workbook_sha256": workbook_sha,
                "sheet_name": canonical_row["sheet"], "row_number": canonical_row["row_number"],
                "source_path": source_path, "relative_path": item["relative_path"],
                "filename": item["original_filename"], "extension": extension,
                "size_bytes": item["size_bytes"], "mtime_ns": item["mtime_ns"],
                "width_px": item["width_px"], "height_px": item["height_px"],
                "ahash": item["ahash"], "dhash": item["dhash"], "phash": item["phash"],
                "candidate_role": "plausible_master" if selectable else "existing_derivative_or_copy",
                "selectable_master": str(selectable).lower(), "provenance_role": role,
                "recommendation_rank": recommendation_rank(str(item["relative_path"]), extension),
                "is_book_image": str(source_path == book_path).lower(),
                "existing_candidate_bindings_json": json.dumps(bindings.get(source_path, []), ensure_ascii=False),
                "group_candidate_count": len(group),
                "group_selectable_master_count": selectable_counts[group_id],
                "deletion_authorized": "false",
            })

    if len({row["record_key"] for row in rows}) != EXPECTED_OBJECTS:
        raise PreparationError("unbound candidate object count drifted")
    if len({row["candidate_id"] for row in rows}) != len(rows):
        raise PreparationError("unbound candidate identifiers are not unique")
    object_rows = {row["record_key"]: row for row in rows}
    metadata_counts = {
        "date": sum(bool(row["suggested_date_text"]) for row in object_rows.values()),
        "object_type": sum(bool(row["suggested_object_type"]) for row in object_rows.values()),
        "material": sum(bool(row["suggested_material"]) for row in object_rows.values()),
        "dimensions": sum(bool(row["suggested_dimensions"]) for row in object_rows.values()),
        "keywords": sum(bool(row["suggested_keywords"]) for row in object_rows.values()),
        "classification": sum(bool(row["suggested_classification"]) for row in object_rows.values()),
    }
    if metadata_counts != {
        "date": 56, "object_type": 57, "material": 54, "dimensions": 52,
        "keywords": 57, "classification": 57,
    }:
        raise PreparationError(f"proposed metadata coverage drifted: {metadata_counts}")
    receipt = {
        "format": "iss-tfa-buchprojekt-unbound-object-review-v1",
        "version": VERSION, "result": "ok", "prepared_at": module.now(),
        "source_mount": mount, "source_read_only": True, "source_modified": False,
        "published_tfa_unit_changed": False, "automatic_master_selection": False,
        "automatic_metadata_application": False, "deletion_authorized": False,
        "object_count": EXPECTED_OBJECTS, "candidate_row_count": len(rows),
        "selectable_master_row_count": sum(row["selectable_master"] == "true" for row in rows),
        "derivative_or_copy_row_count": sum(row["selectable_master"] != "true" for row in rows),
        "scope_state_counts": dict(sorted(state_counts.items())),
        "excluded_current_md_objects": excluded_current_md,
        "proposed_metadata_coverage": metadata_counts,
        "inputs": {
            module.CAPTIONS_XLSX.name: "sha256:" + workbook_sha,
            module.FINAL_PDF.name: "sha256:" + module.sha256_file(module.FINAL_PDF),
        },
        "outputs": {
            "candidates": str(CANDIDATES), "decisions": str(DECISIONS), "receipt": str(RECEIPT),
        },
    }
    return rows, receipt


def validate_decisions(candidates: list[dict[str, str]], decisions: list[dict[str, str]]) -> None:
    by_id = {row["candidate_id"]: row for row in candidates}
    groups: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in candidates:
        groups[row["record_key"]].append(row)
    seen: set[str] = set()
    for decision in decisions:
        record_key = decision.get("record_key", "")
        action = decision.get("decision", "")
        candidate = by_id.get(decision.get("selected_candidate_id", ""))
        applied = action in {"apply_metadata", "apply_edited_metadata"}
        if (
            record_key not in groups or record_key in seen
            or action not in {"apply_metadata", "apply_edited_metadata", "reject_noise", "dismiss_object", "defer"}
            or decision.get("deletion_authorized") != "false"
            or applied and (
                candidate is None or candidate["record_key"] != record_key
                or candidate["selectable_master"] != "true"
                or decision.get("selected_master_path") != candidate["source_path"]
                or not decision.get("title", "")
            )
            or not applied and any(decision.get(key, "") for key in (
                "selected_candidate_id", "selected_master_path", "title", "description",
                "date_text", "object_type", "material", "dimensions", "keywords", "classification",
            ))
        ):
            raise PreparationError(f"invalid unbound Buchprojekt decision: {record_key}")
        seen.add(record_key)


def apply() -> dict[str, Any]:
    for path in (CANDIDATES, DECISIONS, RECEIPT):
        if path.exists():
            raise PreparationError(f"refusing to overwrite existing output: {path}")
    rows, receipt = prepare_rows()
    atomic_csv(CANDIDATES, CANDIDATE_FIELDS, rows)
    atomic_csv(DECISIONS, DECISION_FIELDS, [])
    atomic_json(RECEIPT, receipt)
    return receipt


def reproject() -> dict[str, Any]:
    module = helper()
    for path in (CANDIDATES, DECISIONS, RECEIPT):
        if not path.is_file():
            raise PreparationError(f"cannot reproject missing output: {path}")
    old_receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    rows, receipt = prepare_rows()
    new_groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        new_groups[row["record_key"]].append(row)
    old_decisions = read_csv(DECISIONS)
    retained: list[dict[str, str]] = []
    superseded: list[dict[str, str]] = []
    enriched: dict[str, list[str]] = {}
    excluded_by_key = {
        str(row["book_image_key"]): row for row in receipt["excluded_current_md_objects"]
    }
    for decision in old_decisions:
        record_key = decision["record_key"]
        group = new_groups.get(record_key)
        if group is None:
            excluded = excluded_by_key.get(decision.get("book_image_key", ""))
            if excluded is None:
                raise PreparationError(f"human decision unexpectedly left the projected scope: {record_key}")
            superseded.append({
                "record_key": record_key, "decision": decision["decision"],
                "book_image_key": decision["book_image_key"],
                "reason": "book picture is bound to a current museum-digital object",
                "bound_record_key": excluded["record_key"],
            })
            continue
        updated = dict(decision)
        book = next(row for row in group if row["is_book_image"] == "true")
        changed: list[str] = []
        if updated["decision"] in {"apply_metadata", "apply_edited_metadata"}:
            for field in (
                "date_text", "object_type", "material", "dimensions", "keywords", "classification",
            ):
                if not updated.get(field, "") and book.get("suggested_" + field, ""):
                    updated[field] = str(book["suggested_" + field])
                    changed.append(field)
        if changed:
            enriched[record_key] = changed
        retained.append(updated)
    if len(retained) + len(superseded) != len(old_decisions):
        raise PreparationError(
            f"decision preservation scope drifted: old={len(old_decisions)} retained={len(retained)} superseded={len(superseded)}"
        )
    validate_decisions(rows, retained)
    stamp = module.now().replace("-", "").replace(":", "")
    backups: dict[str, str] = {}
    for path in (CANDIDATES, DECISIONS, RECEIPT):
        backup = path.with_name(f"{path.name}.pre-keyword-enrichment-{stamp}")
        if backup.exists():
            raise PreparationError(f"backup already exists: {backup}")
        shutil.copy2(path, backup)
        backups[path.name] = str(backup)
    previous_reprojections = old_receipt.get("reprojections", [])
    if not previous_reprojections and old_receipt.get("reprojection"):
        previous_reprojections = [old_receipt["reprojection"]]
    receipt["reprojections"] = [*previous_reprojections, {
        "backups": backups, "retained_decisions": len(retained),
        "superseded_decisions": superseded, "enriched_existing_decision_fields": enriched,
    }]
    atomic_csv(CANDIDATES, CANDIDATE_FIELDS, rows)
    atomic_csv(DECISIONS, DECISION_FIELDS, retained)
    atomic_json(RECEIPT, receipt)
    return receipt


def check() -> dict[str, Any]:
    module = helper()
    mount = module.source_mount()
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    candidates = read_csv(CANDIDATES)
    decisions = read_csv(DECISIONS)
    if (
        receipt.get("format") != "iss-tfa-buchprojekt-unbound-object-review-v1"
        or receipt.get("result") != "ok"
        or receipt.get("source_read_only") is not True
        or receipt.get("automatic_master_selection") is not False
        or receipt.get("automatic_metadata_application") is not False
        or receipt.get("deletion_authorized") is not False
        or len(candidates) != int(receipt.get("candidate_row_count", -1))
        or len({row["record_key"] for row in candidates}) != EXPECTED_OBJECTS
        or any(row.get("deletion_authorized") != "false" for row in candidates)
        or len(receipt.get("excluded_current_md_objects", [])) != 7
        or receipt.get("proposed_metadata_coverage") != {
            "date": 56, "object_type": 57, "material": 54, "dimensions": 52,
            "keywords": 57, "classification": 57,
        }
    ):
        raise PreparationError("unbound Buchprojekt review output failed validation")
    validate_decisions(candidates, decisions)
    return {
        "result": "ok", "objects": EXPECTED_OBJECTS, "candidate_rows": len(candidates),
        "decision_rows": len(decisions), "source_mount": mount,
        "automatic_master_selection": False, "automatic_metadata_application": False,
        "deletion_authorized": False,
    }


def self_test() -> None:
    row = {"ahash": "0" * 16, "dhash": "1" * 16, "phash": "2" * 16}
    assert visual_inventory(row).startswith("TFA-VISUAL-")
    assert provenance_role("x/a.tif", "tif", str(SOURCE_ROOT / "x/a.tif")) == "canonical_book_tiff"
    assert provenance_role("x/a.jpg", "jpg", "other") == "existing_derivative_or_copy"
    assert recommendation_rank("restore/archive/SCANS-fertig/x.tif", "tif") == 0
    assert recommendation_rank("restore/archive/x.jpg", "jpg") == 90
    assert caption_date("Foto im Januar 1977.") == "Januar 1977"
    assert caption_date("Ansicht, zweite Hälfte 1970er Jahre.") == "zweite Hälfte 1970er Jahre"
    assert caption_date("s. Legende der Karte") == ""
    assert set(CLASSIFICATION_BY_KEY.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reproject", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if sum((args.apply, args.reproject, args.check, args.self_test)) != 1:
        parser.error("choose exactly one of --apply, --reproject, --check, or --self-test")
    if args.self_test:
        self_test(); print("self-test: ok"); return 0
    result = apply() if args.apply else reproject() if args.reproject else check()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
